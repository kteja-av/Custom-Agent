"""M13 gate: artifacts (FR-53..FR-56, NFR-16, AC-42, AC-44).

Written before the implementation, against the approved specification with FR-56
as amended on 2026-09-15 (DECISION-40ae2d24). Every property of AC-42 is asserted
on both stores through their coroutines, from one parametrised fixture, so the
in-memory store cannot drift from the Postgres one.

The tests reach into three places, named here so the implementation keeps them:
  * `for_scope(tenant_id, project_id)` on both stores returns a store over the same
    artifacts bound to another scope. Without it, "another tenant's artifact is not
    found" is vacuous in memory: two unrelated in-memory stores never share rows.
  * `InMemoryArtifactStore._rows` maps artifact_id to (ArtifactRef, bytes), so the
    integrity check can alter stored content there as an UPDATE does on Postgres.
  * The Postgres store reaches the database through `postgres._pool`, so the
    thread-identity spy of AC-14 sees every checkout.

The persisted half needs DATABASE_URL and fails rather than skips without it.
Every row written is removed, artifacts before the runs they name (FR-55), and no
assertion here prints a credential. Names M13 adds are reached through their
modules at call time, so before the implementation each test fails on its own.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import inspect
import os
import pathlib
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from dotenv import dotenv_values, load_dotenv

import agentsdk
from agentsdk import Persistence, migrate, postgres
from agentsdk.config import normalise_database_url
from agentsdk.errors import AgentSDKError
from agentsdk.manifest import build_manifest
from agentsdk.migrate import apply_migrations
from agentsdk.postgres import SCHEMA_PATH, PostgresRunStore, RunScope
from agentsdk.primitives import ContentProvenance, InstructionAuthority, Origin, TaintFlag, TrustZone

load_dotenv()

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
REPO = pathlib.Path(__file__).resolve().parents[1]
TENANT, PROJECT = "SYN-m13", "p-m13"
OTHER_TENANT, OTHER_PROJECT = "SYN-m13-other", "p-m13-other"
T0 = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
CAP = 64
DEFAULT_CAP = 10_485_760  # P2-D8
FIELDS = [
    "artifact_id", "tenant_id", "project_id", "uri", "mime_type", "content_hash", "size",
    "created_by_agent", "source_run", "source_task", "provenance", "classification", "created_at", "expires_at",
]
EXTERNAL = ContentProvenance(
    origin=Origin.EXTERNAL_TOOL,
    instruction_authority=InstructionAuthority.DATA_ONLY,
    trust_zone=TrustZone.UNTRUSTED,
    taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT, TaintFlag.PROMPT_INJECTION_RISK}),
    source_uri_or_hash="https://source.test/report",
)


# --- shared helpers --------------------------------------------------------------------------------


def artifacts():
    return importlib.import_module("agentsdk.artifacts")


def not_found():
    return importlib.import_module("agentsdk.errors").ArtifactNotFound


def integrity_error():
    return importlib.import_module("agentsdk.errors").ArtifactIntegrityError


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def remove_rows():
    with psycopg.connect(DSN, autocommit=True) as conn:
        # Artifacts first: they reference the runs they name (FR-55).
        if conn.execute("SELECT to_regclass('artifacts')").fetchone()[0] is not None:
            conn.execute("DELETE FROM artifacts WHERE tenant_id LIKE 'SYN-m13%%'")
        ids = [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE tenant_id LIKE 'SYN-m13%%'").fetchall()]
        for table in ("run_events", "messages", "execution_manifests"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        remove_rows()


def start_run(tenant, project):
    scope = RunScope(run_id=str(uuid.uuid4()), tenant_id=tenant, project_id=project)
    PostgresRunStore(DSN).start_run(
        scope, agent_spec_id="m13", max_turns=1, model_id=None, principal_context=None,
        manifest=build_manifest(sdk_version="m13", agent_spec_id="m13", instructions="i", tool_profile=(),
                                tool_spec_hashes=[], model_id=None),
    )
    return scope.run_id


class Stores:
    """Builds either kind of store. The in-memory one admits exactly the runs this test made."""

    def __init__(self, kind):
        self.kind = kind
        self.known_runs = set()

    def run(self, tenant=TENANT, project=PROJECT):
        run_id = start_run(tenant, project) if self.kind == "postgres" else str(uuid.uuid4())
        self.known_runs.add((tenant, project, run_id))
        return run_id

    def store(self, tenant=TENANT, project=PROJECT, **options):
        if self.kind == "postgres":
            return Persistence.postgres(DSN).artifact_store(tenant, project, **options)
        options.setdefault("runs", lambda t, p, r: (t, p, r) in self.known_runs)
        return artifacts().InMemoryArtifactStore(tenant, project, **options)

    def stored_ids(self, store):
        if self.kind == "postgres":
            return {str(row[0]) for row in query("SELECT artifact_id FROM artifacts WHERE tenant_id LIKE 'SYN-m13%%'")}
        return set(store._rows)

    def alter_content(self, store, artifact_id, content):
        if self.kind == "postgres":
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute("UPDATE artifacts SET content=%s WHERE artifact_id=%s", (content, artifact_id))
        else:
            ref, _ = store._rows[artifact_id]
            store._rows[artifact_id] = (ref, content)


@pytest.fixture(params=["memory", "postgres"])
def stores(request):
    if request.param == "postgres":
        assert DSN, "the persisted half needs DATABASE_URL and fails rather than skips"
    return Stores(request.param)


def put_fields(**overrides):
    fields = dict(mime_type="text/plain", provenance=EXTERNAL, created_by_agent="writer")
    fields.update(overrides)
    return fields


# =================================================================================================
# FR-53: the public surface
# =================================================================================================


def test_the_new_public_names_exist():
    for name in ("ArtifactRef", "ArtifactStore", "InMemoryArtifactStore", "ArtifactNotFound", "ArtifactIntegrityError"):
        assert name in agentsdk.__all__ and hasattr(agentsdk, name), name
    errors = importlib.import_module("agentsdk.errors")
    assert issubclass(errors.ArtifactNotFound, AgentSDKError) and issubclass(errors.ArtifactIntegrityError, AgentSDKError)
    ref_type = artifacts().ArtifactRef
    assert dataclasses.is_dataclass(ref_type) and ref_type.__dataclass_params__.frozen
    assert [f.name for f in dataclasses.fields(ref_type)] == FIELDS
    assert artifacts().DEFAULT_MAX_CONTENT_BYTES == DEFAULT_CAP


def test_the_provenance_check_moved_from_the_executor_to_primitives():
    primitives = importlib.import_module("agentsdk.primitives")
    assert callable(primitives.checked_provenance)
    source = inspect.getsource(importlib.import_module("agentsdk.executor"))
    assert "def _checked_provenance" not in source and "checked_provenance(" in source
    assert primitives.checked_provenance(EXTERNAL) == EXTERNAL
    assert isinstance(primitives.checked_provenance("not provenance"), str)


# =================================================================================================
# FR-54, FR-55, AC-42: behaviour, identically on both stores
# =================================================================================================


class _Bytes(bytes):
    pass


@pytest.mark.parametrize("form", ["bytes", "bytearray", "memoryview", "a bytes subclass"])
async def test_an_artifact_round_trips_through_every_method(stores, form):
    clock = Clock()
    run_id = stores.run()
    store = stores.store(clock=clock)
    raw = b"report\x00\xff body"
    content = {"bytes": raw, "bytearray": bytearray(raw), "memoryview": memoryview(bytearray(raw)), "a bytes subclass": _Bytes(raw)}[form]
    expires = T0 + timedelta(hours=1)

    ref = await store.put(content, source_run=run_id, classification="internal", expires_at=expires, **put_fields())
    if form in ("bytearray", "memoryview"):
        content[0] = ord("X")  # changing the caller's buffer afterwards changes nothing stored

    assert str(uuid.UUID(ref.artifact_id)) == ref.artifact_id
    assert (ref.tenant_id, ref.project_id) == (TENANT, PROJECT)
    assert ref.uri == f"urn:agentsdk:artifact:{ref.artifact_id}"
    assert ref.content_hash == hashlib.sha256(raw).hexdigest() and ref.size == len(raw)
    assert (ref.mime_type, ref.created_by_agent, ref.source_run, ref.source_task, ref.classification) == (
        "text/plain", "writer", run_id, None, "internal"
    )
    assert ref.created_at == T0 and ref.expires_at == expires and ref.created_at.tzinfo is not None
    assert await store.metadata(ref.artifact_id) == ref
    got = await store.get(ref.artifact_id)
    assert type(got) is bytes and got == raw
    if stores.kind == "memory":
        assert type(store._rows[ref.artifact_id][1]) is bytes

    await store.delete(ref.artifact_id)
    for method in (store.get, store.metadata, store.delete):
        with pytest.raises(not_found()):
            await method(ref.artifact_id)


async def test_identical_content_is_never_deduplicated(stores):
    store = stores.store()
    first = await store.put(b"same", **put_fields())
    second = await store.put(b"same", **put_fields())
    assert first.artifact_id != second.artifact_id and first.content_hash == second.content_hash
    assert stores.stored_ids(store) == {first.artifact_id, second.artifact_id}


async def test_provenance_reads_back_as_the_enums_own_members(stores):
    store = stores.store()
    ref = await store.put(b"x", **put_fields(mime_type="application/json"))
    read = (await store.metadata(ref.artifact_id)).provenance
    assert read == EXTERNAL
    assert read.origin is Origin.EXTERNAL_TOOL
    assert read.trust_zone is TrustZone.UNTRUSTED
    assert read.instruction_authority is InstructionAuthority.DATA_ONLY
    assert type(read.taint_flags) is frozenset
    assert all(any(flag is member for member in TaintFlag) for flag in read.taint_flags)


async def test_content_altered_in_the_stored_row_is_refused_by_get(stores):
    store = stores.store()
    ref = await store.put(b"original", **put_fields())
    stores.alter_content(store, ref.artifact_id, b"tampered")
    with pytest.raises(integrity_error()):
        await store.get(ref.artifact_id)
    assert await store.metadata(ref.artifact_id) == ref


@pytest.mark.parametrize("method", ["get", "metadata", "delete"])
async def test_every_artifact_a_scope_cannot_see_is_not_found_indistinguishably(stores, method):
    clock = Clock()
    store = stores.store(clock=clock)
    other_tenant = store.for_scope(OTHER_TENANT, PROJECT)
    other_project = store.for_scope(TENANT, OTHER_PROJECT)
    theirs = await other_tenant.put(b"another tenant", **put_fields())
    neighbours = await other_project.put(b"another project", **put_fields())
    deleted = await store.put(b"deleted", **put_fields())
    await store.delete(deleted.artifact_id)
    expired = await store.put(b"expired", expires_at=T0 + timedelta(minutes=1), **put_fields())
    clock.advance(minutes=1)  # expires_at at the clock is expired, before expire() runs

    ids = {
        "another tenant's artifact": theirs.artifact_id,
        "another project's artifact": neighbours.artifact_id,
        "a deleted artifact": deleted.artifact_id,
        "an expired artifact": expired.artifact_id,
        "an id never created": str(uuid.uuid4()),
        "a malformed id": "not-an-artifact-id",
    }
    messages = {}
    for label, artifact_id in ids.items():
        with pytest.raises(not_found()) as caught:
            await getattr(store, method)(artifact_id)
        messages[label] = str(caught.value).replace(artifact_id, "<id>")
    assert len(set(messages.values())) == 1, messages
    only = next(iter(messages.values()))
    assert "<id>" in only and TENANT not in only and PROJECT not in only

    assert await other_tenant.get(theirs.artifact_id) == b"another tenant", "another tenant's artifact was changed"
    assert await other_project.get(neighbours.artifact_id) == b"another project"


class _ForgedOrigin(str):
    pass


class _FlagSet(frozenset):
    """A frozenset subclass can answer iteration differently from what it holds."""


class _ShortReport(str):
    """Text that reports a length it does not hold (M10 review round 2, caveat 1)."""

    def __len__(self):
        return 3


class _Tagged(str):
    pass


def invalid_puts():
    yield pytest.param({"content": b"x" * (CAP + 1)}, "content", id="content one byte over the cap")
    yield pytest.param({"content": "text"}, "content", id="content that is a str")
    yield pytest.param({"content": None}, "content", id="content that is None")
    for label, value in {
        "without a slash": "text", "empty": "", "not text": 7, "over 255 characters": "text/" + "p" * 251,
        "holding a NUL": "text/pl\x00ain", "with two slashes": "text/plain/more", "with an empty subtype": "text/",
    }.items():
        yield pytest.param({"mime_type": value}, "mime_type", id=f"mime_type {label}")
    for field in ("created_by_agent", "classification"):
        for label, value in {
            "not text": 7, "over 255 characters": "a" * 256, "holding a NUL": "wri\x00ter", "holding a lone surrogate": "wri\ud800ter",
        }.items():
            yield pytest.param({field: value}, field, id=f"{field} {label}")
    for label, value in {
        "naive": datetime(2026, 9, 16, 12), "equal to the clock": T0, "before the clock": T0 - timedelta(seconds=1),
        "not a datetime": "2026-09-16T12:00:00+00:00",
    }.items():
        yield pytest.param({"expires_at": value}, "expires_at", id=f"expires_at {label}")
    forged = dataclasses.replace(EXTERNAL)
    object.__setattr__(forged, "origin", str.__new__(Origin, "system"))
    # Added after the first mutation run (P2 and P4 survived).
    subclassed_flags = dataclasses.replace(EXTERNAL)
    object.__setattr__(subclassed_flags, "taint_flags", _FlagSet({TaintFlag.EXTERNAL_CONTENT}))
    lying_source = dataclasses.replace(EXTERNAL)
    object.__setattr__(lying_source, "source_uri_or_hash", _ShortReport("s" * 8193))
    for label, value in {
        "not a ContentProvenance": {"origin": "external_tool"},
        "with a label that is no member of its enum": forged,
        "with a source over 8192 characters": dataclasses.replace(EXTERNAL, source_uri_or_hash="s" * 8193),
        "with taint flags in a frozenset subclass": subclassed_flags,
        "with a source that reports a false length": lying_source,
    }.items():
        yield pytest.param({"provenance": value}, "provenance", id=f"provenance {label}")
    for label, value in {"not a uuid": "run-1", "not canonical": str(uuid.uuid4()).upper(), "not text": 12}.items():
        yield pytest.param({"source_run": value}, "source_run", id=f"source_run {label}")


@pytest.mark.parametrize("overrides, field", list(invalid_puts()))
async def test_every_invalid_put_is_refused_by_field_name_with_no_row_written(stores, overrides, field):
    store = stores.store(clock=Clock(), max_content_bytes=CAP)
    fields = put_fields()
    fields.update(overrides)
    content = fields.pop("content", b"ok")
    with pytest.raises(ValueError, match=field):
        await store.put(content, **fields)
    assert stores.stored_ids(store) == set()


async def test_content_at_exactly_the_cap_is_accepted(stores):
    store = stores.store(max_content_bytes=CAP)
    ref = await store.put(b"x" * CAP, **put_fields())
    assert ref.size == CAP and await store.get(ref.artifact_id) == b"x" * CAP


@pytest.mark.parametrize("bad", [True, 1.5, 0, -1, "10", None])
def test_the_size_cap_is_a_positive_int_refused_at_construction(bad):
    assert DSN, "DATABASE_URL must be set"
    with pytest.raises(ValueError, match="max_content_bytes"):
        artifacts().InMemoryArtifactStore(TENANT, PROJECT, max_content_bytes=bad)
    with pytest.raises(ValueError, match="max_content_bytes"):
        Persistence.postgres(DSN).artifact_store(TENANT, PROJECT, max_content_bytes=bad)


async def test_expire_removes_exactly_the_expired_artifacts_and_counts_them(stores):
    clock = Clock()
    store = stores.store(clock=clock)
    other = store.for_scope(OTHER_TENANT, PROJECT)
    soon = await store.put(b"soon", expires_at=T0 + timedelta(minutes=10), **put_fields())
    later = await store.put(b"later", expires_at=T0 + timedelta(minutes=20), **put_fields())
    never = await store.put(b"never", **put_fields())
    elsewhere = await other.put(b"another scope", expires_at=T0 + timedelta(minutes=10), **put_fields())

    clock.advance(minutes=15)
    assert await store.get(later.artifact_id) == b"later"
    assert await store.expire() == 1
    assert stores.stored_ids(store) == {later.artifact_id, never.artifact_id, elsewhere.artifact_id}, (
        "expire removed something other than this scope's expired artifacts"
    )
    assert soon.artifact_id not in stores.stored_ids(store)
    clock.advance(minutes=10)
    assert await store.expire() == 1
    assert await store.expire() == 0
    assert await store.get(never.artifact_id) == b"never"


async def test_a_source_run_must_belong_to_the_store_scope(stores):
    mine = stores.run()
    theirs = stores.run(OTHER_TENANT, PROJECT)
    neighbours = stores.run(TENANT, OTHER_PROJECT)
    store = stores.store()
    ref = await store.put(b"x", source_run=mine, **put_fields())
    for run_id in (theirs, neighbours, str(uuid.uuid4())):
        with pytest.raises(ValueError, match="source_run"):
            await store.put(b"x", source_run=run_id, **put_fields())
    assert stores.stored_ids(store) == {ref.artifact_id}


async def test_the_in_memory_store_refuses_every_source_run_without_a_callable():
    store = artifacts().InMemoryArtifactStore(TENANT, PROJECT)
    with pytest.raises(ValueError, match="source_run"):
        await store.put(b"x", source_run=str(uuid.uuid4()), **put_fields())
    assert (await store.put(b"x", **put_fields())).source_run is None


@pytest.mark.parametrize(
    "tenant, project",
    [("", PROJECT), (TENANT, ""), ("SYN-m13\x00", PROJECT), (TENANT, "p\ud800"), (None, PROJECT)],
    ids=["an empty tenant", "an empty project", "a tenant with a NUL", "a project with a lone surrogate", "a tenant that is None"],
)
def test_a_store_refuses_an_empty_or_unstorable_scope(tenant, project):
    assert DSN, "DATABASE_URL must be set"
    with pytest.raises(ValueError):
        Persistence.postgres(DSN).artifact_store(tenant, project)
    with pytest.raises(ValueError):
        artifacts().InMemoryArtifactStore(tenant, project)


# --- Added after the first mutation run: A18 and G12 survived, and a source run given as
# --- uuid.UUID was refused, M7 round 2's shape. Written before the fix they test.


async def test_a_source_run_or_an_id_given_as_a_uuid_object_is_its_canonical_text(stores):
    """get_run and PostgresTrace hand run ids back as uuid.UUID, and column_rejection_reason
    accepts one for a UUID column (M7 round 2), so a put naming its source run that way is
    not refused by the SDK's own output type. The same holds for an artifact id."""
    mine = stores.run()
    store = stores.store()
    ref = await store.put(b"x", source_run=uuid.UUID(mine), **put_fields())
    assert ref.source_run == mine and type(ref.source_run) is str
    as_object = uuid.UUID(ref.artifact_id)
    assert (await store.metadata(as_object)).source_run == mine
    assert await store.get(as_object) == b"x"
    with pytest.raises(ValueError, match="source_run"):
        await store.put(b"x", source_run=uuid.uuid4(), **put_fields())
    await store.delete(as_object)
    assert stores.stored_ids(store) == set()


@pytest.mark.parametrize("method", ["get", "metadata", "delete"])
async def test_only_the_canonical_form_of_an_id_finds_its_artifact(stores, method):
    """uuid.UUID() and a Postgres uuid column both parse forms an issued id never has:
    uppercase, braced, prefixed with urn:uuid:, unhyphenated. Both stores treat each as an
    id never created, so neither finds an artifact the other would not."""
    store = stores.store()
    ref = await store.put(b"x", **put_fields())
    for form in (ref.artifact_id.upper(), "{" + ref.artifact_id + "}", "urn:uuid:" + ref.artifact_id,
                 ref.artifact_id.replace("-", "")):
        with pytest.raises(not_found()):
            await getattr(store, method)(form)
    assert await store.get(ref.artifact_id) == b"x", "a refused form removed or hid the artifact"


async def test_a_source_held_in_a_str_subclass_is_stored_as_the_text_it_holds(stores):
    """FR-47, as executor step 8 applies it (KNOWLEDGE-739aca22): a str subclass is copied
    into the exact text it holds, neither refused nor stored as itself."""
    provenance = dataclasses.replace(EXTERNAL)
    object.__setattr__(provenance, "source_uri_or_hash", _Tagged("https://source.test/tagged"))
    store = stores.store()
    ref = await store.put(b"x", **put_fields(provenance=provenance))
    for read in (ref, await store.metadata(ref.artifact_id)):
        source = read.provenance.source_uri_or_hash
        assert type(source) is str and source == "https://source.test/tagged"


async def test_for_scope_keeps_the_clock_and_the_size_cap(stores):
    """A store from for_scope is the same store bound elsewhere. One that fell back to the
    wall clock or the default cap would accept, and expire, differently from its origin."""
    clock = Clock(datetime(2100, 1, 1, tzinfo=timezone.utc))
    other = stores.store(clock=clock, max_content_bytes=CAP).for_scope(OTHER_TENANT, PROJECT)
    with pytest.raises(ValueError, match="content"):
        await other.put(b"x" * (CAP + 1), **put_fields())
    ref = await other.put(b"x", expires_at=clock.now + timedelta(minutes=1), **put_fields())
    clock.advance(minutes=2)
    with pytest.raises(not_found()):
        await other.metadata(ref.artifact_id)
    assert await other.expire() == 1


# =================================================================================================
# AC-42 on Postgres: I/O off the loop, no credential outside content
# =================================================================================================


class _Checkouts:
    def __init__(self, pool, log, current):
        self._pool, self._log, self._current = pool, log, current

    def connection(self, *args, **kwargs):
        self._log.append((self._current[0], threading.get_ident()))
        return self._pool.connection(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._pool, name)


async def test_no_artifact_store_io_runs_on_the_event_loop_thread(monkeypatch):
    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)  # schema work happens before anything is recorded
    log, current = [], ["setup"]
    real_pool = postgres._pool
    monkeypatch.setattr(postgres, "_pool", lambda dsn: _Checkouts(real_pool(dsn), log, current))
    clock = Clock()
    store = persistence.artifact_store(TENANT, PROJECT, clock=clock)
    loop_thread = threading.get_ident()

    current[0] = "put"
    ref = await store.put(b"payload", expires_at=T0 + timedelta(minutes=1), **put_fields())
    current[0] = "metadata"
    await store.metadata(ref.artifact_id)
    current[0] = "get"
    await store.get(ref.artifact_id)
    current[0] = "delete"
    await store.delete(ref.artifact_id)
    current[0] = "expire"
    await store.expire()
    monkeypatch.undo()

    seen = {name for name, _ in log}
    assert seen >= {"put", "metadata", "get", "delete", "expire"}, f"methods with no checkout, so nothing was spied: {seen}"
    on_loop = sorted({name for name, ident in log if ident == loop_thread})
    assert not on_loop, f"artifact store I/O ran on the event loop's thread: {on_loop}"


class _HeldCheckouts:
    """Holds each connection checkout on its worker thread until released, so a
    cancellation lands while a write is under way where it cannot be stopped."""

    def __init__(self, pool, entered, release):
        self._pool, self._entered, self._release = pool, entered, release

    def connection(self, *args, **kwargs):
        self._entered.set()
        if not self._release.wait(30):
            raise TimeoutError("the held checkout was never released")
        return self._pool.connection(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._pool, name)


@pytest.mark.parametrize("method", ["put", "delete", "expire"])
async def test_a_cancelled_write_finishes_before_the_cancellation_is_reported(monkeypatch, method):
    """Added after the first mutation run (G8 and G13 survived); written before the fix.

    A write on its worker thread cannot be stopped, so the store waits for it, through a
    second cancel too, then raises CancelledError. A cancelled put then takes back what it
    wrote: its caller never receives the id, so nothing could reach the artifact. A
    cancelled delete or expire has removed what it removed."""
    assert DSN, "DATABASE_URL must be set"
    clock = Clock()
    store = Persistence.postgres(DSN).artifact_store(TENANT, PROJECT, clock=clock)
    existing = await store.put(b"existing", expires_at=T0 + timedelta(minutes=1), **put_fields())
    if method == "expire":
        clock.advance(minutes=2)
    calls = {
        "put": lambda: store.put(b"cancelled", **put_fields()),
        "delete": lambda: store.delete(existing.artifact_id),
        "expire": store.expire,
    }
    entered, release = threading.Event(), threading.Event()
    real_pool = postgres._pool
    monkeypatch.setattr(postgres, "_pool", lambda dsn: _HeldCheckouts(real_pool(dsn), entered, release))
    task = asyncio.ensure_future(calls[method]())
    try:
        assert await asyncio.to_thread(entered.wait, 10), f"{method} never reached the database"
        for attempt in ("a cancel", "a second cancel"):
            task.cancel()
            await asyncio.sleep(0.1)
            assert not task.done(), f"{attempt} made {method} report before its write finished"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()
    left = {str(row[0]) for row in query("SELECT artifact_id FROM artifacts WHERE tenant_id LIKE 'SYN-m13%%'")}
    assert left == ({existing.artifact_id} if method == "put" else set()), f"after a cancelled {method}: {left}"


async def test_no_column_but_content_carries_a_credential_and_content_is_stored_as_given():
    assert DSN, "DATABASE_URL must be set"
    values = dotenv_values(REPO / ".env")
    key = (values.get("MODEL_API_KEY") or "").strip()
    database_url = (values.get("DATABASE_URL") or "").strip()
    assert key and database_url, "MODEL_API_KEY and DATABASE_URL must be set in .env for this check"
    content = f"key={key} url={database_url}".encode()
    store = Persistence.postgres(DSN).artifact_store(TENANT, PROJECT)
    ref = await store.put(content, classification="holds secrets", **put_fields())

    columns = [row[0] for row in query(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'artifacts'"
    )]
    assert "content" in columns
    carrying = []
    for column in columns:
        if column == "content":
            continue
        [(text,)] = query(f"SELECT {column}::text FROM artifacts WHERE artifact_id = %s", (ref.artifact_id,))
        if text is not None and (key in text or database_url in text):
            carrying.append(column)
    assert not carrying, f"columns other than content carry a credential: {carrying}"
    [(stored,)] = query("SELECT content FROM artifacts WHERE artifact_id = %s", (ref.artifact_id,))
    unchanged = bytes(stored) == content
    assert unchanged, "content was not stored exactly as given"


# =================================================================================================
# FR-55, AC-42: migration 0006
# =================================================================================================


class Namespace:
    """A throwaway schema, as in M7 to M12: the live database is already migrated."""

    def __init__(self, baseline=True):
        self.baseline = baseline
        self.name = "m13_" + uuid.uuid4().hex[:8]
        self.dsn = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{self.name}"

    def __enter__(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{self.name}"')
            if self.baseline:
                conn.execute(f'SET search_path TO "{self.name}"')
                conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        return self

    def __exit__(self, *exc):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{self.name}" CASCADE')
        return False

    def shape(self, table):
        # Over a connection whose search_path is this schema, so no definition is
        # qualified with the schema's name (the M12 lesson).
        with psycopg.connect(self.dsn) as conn:
            columns = set(conn.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                (self.name, table),
            ).fetchall())
            constraints = set(conn.execute(
                "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname=%s AND t.relname=%s",
                (self.name, table),
            ).fetchall())
            indexes = {row[0].replace(f"{self.name}.", "") for row in conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s", (self.name, table)
            ).fetchall()}
        return columns, constraints, indexes


def test_migration_0006_creates_artifacts_scoped_indexed_and_idempotent(monkeypatch):
    assert DSN, "DATABASE_URL must be set"
    real = migrate.discover()
    assert "0006" in [version for version, _ in real], "migration 0006 is not on disk"
    tables = ("runs", "messages", "run_events", "execution_manifests", "artifacts")

    with Namespace() as ns:
        monkeypatch.setattr(migrate, "discover", lambda: [m for m in real if m[0] <= "0005"])
        assert apply_migrations(ns.dsn) == ["0002", "0003", "0004", "0005"]
        assert ns.shape("artifacts") == (set(), set(), set()), "artifacts existed before 0006"

        monkeypatch.setattr(migrate, "discover", lambda: real)
        assert apply_migrations(ns.dsn) == [v for v, _ in real if v > "0005"]
        columns, constraints, indexes = ns.shape("artifacts")
        nullable = {name: flag for name, _, flag in columns}
        assert nullable.get("tenant_id") == "NO" and nullable.get("project_id") == "NO", nullable
        assert any(re.search(r"\(tenant_id, project_id\b", index) for index in indexes), indexes
        assert any("REFERENCES runs(run_id)" in definition for _, definition in constraints), constraints
        assert apply_migrations(ns.dsn) == [], "a second application changed something"
        upgraded = {table: ns.shape(table) for table in tables}

    with Namespace(baseline=False) as fresh:
        apply_migrations(fresh.dsn, baseline=SCHEMA_PATH)
        assert {table: fresh.shape(table) for table in tables} == upgraded


# =================================================================================================
# FR-56: the examples
# =================================================================================================


def test_both_readmes_list_the_m13_examples():
    for document in (REPO / "README.md", REPO / "scripts" / "README.md"):
        text = document.read_text(encoding="utf-8")
        for script in ("11_run_handle.py", "12_artifacts.py"):
            assert script in text, f"{document.relative_to(REPO)} does not list {script}"


@pytest.mark.parametrize(
    "script, shows",
    [
        ("11_run_handle.py", ["RunStarted", "RunCancelled", "status=cancelled"]),
        ("12_artifacts.py", ["urn:agentsdk:artifact:", "ArtifactNotFound"]),
    ],
)
def test_the_m13_examples_run_offline_and_show_what_fr56_names(script, shows, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("BASE_URL", "MODEL_API_KEY", "DATABASE_URL", "DEFAULT_MODEL")}
    env.update(PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / script), "--offline"],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    assert "[FAIL]" not in proc.stdout and "[PASS]" in proc.stdout, proc.stdout[-2000:]
    missing = [text for text in shows if text not in proc.stdout]
    assert not missing, f"{script} offline did not show {missing}"
