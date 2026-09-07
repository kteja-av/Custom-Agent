"""M5 gate: Postgres persistence (FR-9, FR-10, FR-11, NFR-2, AC-5, AC-6, AC-7).

These tests need a real database. They are skipped, never silently passed, when
DATABASE_URL is absent -- a green suite that quietly proved nothing would be
worse than a visible skip.

Each test runs against its own schema-qualified namespace? No: it uses a unique
run_id per test and asserts only on that run, so tests are isolated without
tearing down tables another developer may be looking at.
"""

import json
import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from agentsdk import AgentSpec, RunConfig, Runner, RunStatus
from agentsdk.config import normalise_database_url
from agentsdk.events import EventType
from agentsdk.manifest import build_manifest
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.persistence import Persistence
from agentsdk.registry import default_registry
from agentsdk.postgres import (
    SCHEMA_PATH,
    PostgresEventStore,
    PostgresRunStore,
    PostgresSessionStore,
    PostgresTrace,
    RunScope,
    apply_schema,
)
from agentsdk.primitives import (
    ContentProvenance,
    unstorable_reason,
    Message,
    Origin,
    Role,
    TaintFlag,
    ToolCall,
    ToolResult,
    TrustZone,
)
from agentsdk.tools import Tool, ToolSpec
from agentsdk.version import __version__ as agentsdk_version

from dotenv import load_dotenv

# The Genesis gate runs this file through cmd.exe, which has none of the shell's
# exported variables. Without this, every test below would SKIP and the gate
# would report a pass having proved nothing -- a vacuous green is worse than a
# red one, because nobody investigates it.
load_dotenv()

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))

# Rows written before this session belong to earlier runs of the suite; the
# whole-table AC-6 property is asserted only over what this session created.
SUITE_STARTED_AT = datetime.now(timezone.utc)


def test_the_database_is_actually_configured():
    """Deliberately NOT skippable.

    This is M5's gate: its entire purpose is to prove Postgres persistence. If
    DATABASE_URL is missing, the gate must go red rather than skip its way to a
    green that means nothing.
    """
    assert DSN, (
        "DATABASE_URL is not set (checked the environment and .env). "
        "M5's gate cannot pass without a real database."
    )


GUARD = "test_the_database_is_actually_configured"


@pytest.fixture(autouse=True)
def _requires_database(request):
    """Skip the Postgres tests without a database -- but never the guard above,
    which is what stops a database-less run from reporting a green M5 gate."""
    if request.node.name != GUARD and not DSN:
        pytest.skip("DATABASE_URL not set; Postgres-backed tests cannot run")

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
TABLES = ("runs", "messages", "run_events", "execution_manifests")


@pytest.fixture(scope="module", autouse=True)
def schema():
    if not DSN:
        return  # the guard test reports this; do not bury it under fixture errors
    apply_schema(DSN)


@pytest.fixture
def scope():
    return RunScope(run_id=str(uuid.uuid4()), tenant_id="t-test", project_id="p-test")


def a_manifest(**overrides):
    fields = dict(
        sdk_version="0.1.0",
        agent_spec_id="spec-1",
        instructions="be terse",
        tool_profile=("echo",),
        tool_spec_hashes=["abc"],
        model_id="openai.gpt-4o-mini",
        model_version="2024-07-18",
        model_adapter_version="openai-compatible/1",
        policy_version="AllowlistPermissionChecker",
    )
    return build_manifest(**{**fields, **overrides})


@pytest.fixture
def started(scope):
    PostgresRunStore(DSN).start_run(
        scope,
        agent_spec_id="spec-1",
        max_turns=5,
        model_id="m",
        principal_context=None,
        manifest=a_manifest(),
    )
    return scope


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


class ScriptedModel:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return self._responses.pop(0) if self._responses else text("done")


def text(content):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, content=content),
        stop_reason=StopReason.END_TURN,
        usage=Usage(10, 5, 15),
    )


def tool_call(call_id="c1", name="echo", arguments=None):
    return ModelResponse(
        message=Message(
            role=Role.ASSISTANT,
            tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments or {"text": "hi"}),),
        ),
        stop_reason=StopReason.TOOL_CALLS,
        usage=Usage(10, 5, 15),
    )


def echo_tool():
    return Tool(
        spec=ToolSpec(name="echo", description="Echo", input_schema=ECHO_SCHEMA),
        fn=lambda text: text,
    )


# --- FR-9: SessionStore -----------------------------------------------------


def test_append_and_history_round_trip_in_order(started):
    store = PostgresSessionStore(DSN).bind(started)
    store.append(started.run_id, Message(role=Role.USER, content="first"))
    store.append(started.run_id, Message(role=Role.ASSISTANT, content="second"))

    history = store.history(started.run_id)
    assert [m.role for m in history] == [Role.USER, Role.ASSISTANT]
    assert [m.content for m in history] == ["first", "second"]


def test_sequence_no_is_assigned_by_the_store_not_the_caller(started):
    store = PostgresSessionStore(DSN).bind(started)
    for i in range(4):
        store.append(started.run_id, Message(role=Role.USER, content=str(i)))

    rows = query("SELECT sequence_no FROM messages WHERE run_id=%s ORDER BY sequence_no", (started.run_id,))
    assert [r[0] for r in rows] == [1, 2, 3, 4]


def test_appending_without_a_bound_scope_is_refused():
    """A row that cannot say which tenant it belongs to must not be writable."""
    unbound = PostgresSessionStore(DSN)
    with pytest.raises(ValueError, match="tenant_id and project_id are mandatory"):
        unbound.append(str(uuid.uuid4()), Message(role=Role.USER, content="x"))


def test_a_bound_store_refuses_a_different_run(started):
    store = PostgresSessionStore(DSN).bind(started)
    with pytest.raises(ValueError):
        store.append(str(uuid.uuid4()), Message(role=Role.USER, content="x"))


def test_duplicate_sequence_numbers_are_impossible(started):
    """The UNIQUE constraint is what makes a concurrent-append race a visible
    error rather than a silently reordered history."""
    store = PostgresSessionStore(DSN).bind(started)
    store.append(started.run_id, Message(role=Role.USER, content="one"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "INSERT INTO messages (message_id, run_id, tenant_id, project_id,"
                " sequence_no, role, content) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (uuid.uuid4(), started.run_id, "t-test", "p-test", 1, "user", "collide"),
            )


def test_concurrent_appends_never_corrupt_the_order(started):
    """FR-9's real guarantee, measured rather than asserted.

    `INSERT ... SELECT MAX(sequence_no)+1` under READ COMMITTED lets two
    transactions read the same MAX. The UNIQUE constraint is what decides what
    happens next, and the point of this test is which of the two outcomes it is:

      SAFETY (guaranteed here): no duplicate or reordered sequence numbers ever
      commit. Whatever lands is a correct, contiguous history.

      AVAILABILITY (deliberately NOT guaranteed in Phase 0): a losing writer
      raises UniqueViolation rather than serialising behind the winner. Phase 0
      has exactly one writer per run, so this cannot occur; Phase 2's concurrent
      subagents will need a retry or an advisory lock, and that is recorded as a
      known limitation rather than discovered then.
    """
    import threading

    writers = 8
    store = PostgresSessionStore(DSN).bind(started)
    barrier = threading.Barrier(writers)
    failures = []

    def write(i):
        barrier.wait()  # maximise overlap
        try:
            store.append(started.run_id, Message(role=Role.USER, content=f"w{i}"))
        except psycopg.errors.UniqueViolation:
            failures.append(i)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    sequences = [
        r[0]
        for r in query(
            "SELECT sequence_no FROM messages WHERE run_id=%s ORDER BY sequence_no",
            (started.run_id,),
        )
    ]
    # Safety: whatever committed is unique and contiguous. This is the property
    # that must never regress.
    assert len(sequences) == len(set(sequences)), "duplicate sequence numbers committed"
    assert sequences == list(range(1, len(sequences) + 1)), "history is not contiguous"
    assert len(sequences) + len(failures) == writers, "a write vanished without raising"


def test_provenance_survives_the_round_trip(started):
    """A stored ToolResult that lost its provenance would break the invariant
    exactly where it matters most: after the fact."""
    provenance = ContentProvenance(
        origin=Origin.EXTERNAL_TOOL,
        instruction_authority=ContentProvenance.internal_tool().instruction_authority,
        trust_zone=TrustZone.UNTRUSTED,
        taint_flags={TaintFlag.PROMPT_INJECTION_RISK, TaintFlag.EXTERNAL_CONTENT},
        source_uri_or_hash="https://example.test/page",
    )
    store = PostgresSessionStore(DSN).bind(started)
    store.append(
        started.run_id,
        Message(
            role=Role.TOOL,
            tool_results=(ToolResult(tool_call_id="c1", content="scraped", provenance=provenance),),
        ),
    )

    restored = store.history(started.run_id)[0].tool_results[0]
    assert restored.provenance == provenance
    assert restored.provenance.trust_zone is TrustZone.UNTRUSTED
    assert TaintFlag.PROMPT_INJECTION_RISK in restored.provenance.taint_flags
    assert restored.provenance.source_uri_or_hash == "https://example.test/page"


def test_a_failed_tool_result_is_still_a_failure_after_a_round_trip(started):
    """is_error defaults to False on read. A dropped flag turns a stored failure
    into a stored success -- silently, and only visible after the fact."""
    store = PostgresSessionStore(DSN).bind(started)
    store.append(
        started.run_id,
        Message(
            role=Role.TOOL,
            tool_results=(
                ToolResult(
                    tool_call_id="c1",
                    content="boom",
                    provenance=ContentProvenance.internal_tool(),
                    is_error=True,
                ),
            ),
        ),
    )
    assert store.history(started.run_id)[0].tool_results[0].is_error is True


def test_history_orders_by_sequence_no_not_by_insertion(started):
    """ORDER BY was unpinned: an append-only table returns in insertion order
    anyway, so removing it changed nothing any test could see. Write the rows
    out of order to make the clause the only thing that can save the read."""
    with psycopg.connect(DSN) as conn:
        for sequence_no, content in ((3, "third"), (1, "first"), (2, "second")):
            conn.execute(
                "INSERT INTO messages (message_id, run_id, tenant_id, project_id,"
                " sequence_no, role, content) VALUES (%s,%s,%s,%s,%s,'user',%s)",
                (
                    uuid.uuid4(),
                    started.run_id,
                    started.tenant_id,
                    started.project_id,
                    sequence_no,
                    content,
                ),
            )
    history = PostgresSessionStore(DSN).bind(started).history(started.run_id)
    assert [m.content for m in history] == ["first", "second", "third"]


def test_the_trace_reconstructs_in_order_however_the_rows_were_written(started):
    """AC-7 says the trace reconstructs IN ORDER, so the ordering has to be
    what the reader guarantees rather than what the table happens to return.
    Both ORDER BY clauses here survived the round-1 matrix for the same reason
    history()'s did: nothing ever wrote a row out of order."""
    with psycopg.connect(DSN) as conn:
        for sequence_no, content in ((2, "second"), (3, "third"), (1, "first")):
            conn.execute(
                "INSERT INTO messages (message_id, run_id, tenant_id, project_id,"
                " sequence_no, role, content) VALUES (%s,%s,%s,%s,%s,'user',%s)",
                (uuid.uuid4(), started.run_id, started.tenant_id,
                 started.project_id, sequence_no, content),
            )
        for sequence_no, event_type in ((3, "RunCompleted"), (1, "RunStarted"), (2, "ToolCalled")):
            conn.execute(
                "INSERT INTO run_events (event_id, schema_version, sequence_no, event_type,"
                " tenant_id, project_id, run_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (uuid.uuid4(), 1, sequence_no, event_type, started.tenant_id,
                 started.project_id, started.run_id),
            )

    trace = PostgresTrace(DSN).reconstruct(started)
    assert [m["sequence_no"] for m in trace["messages"]] == [1, 2, 3]
    assert [m["content"] for m in trace["messages"]] == ["first", "second", "third"]
    assert [e["sequence_no"] for e in trace["events"]] == [1, 2, 3]
    assert [e["event_type"] for e in trace["events"]] == [
        "RunStarted", "ToolCalled", "RunCompleted"
    ]


def test_event_payloads_survive_values_json_cannot_hold(started):
    """_json_safe's str() fallback runs on arbitrary objects. It must neither
    raise nor drop the entry -- an event that cannot be written is an event the
    audit trail silently lacks."""

    class Awkward:
        def __str__(self):
            return "awkward-value"

    sink = PostgresEventStore(DSN, started.tenant_id, started.project_id, started.run_id)
    sink.emit(
        EventType.RUN_STARTED,
        {
            "object": Awkward(),
            "nested": {"list": [Awkward(), Role.USER], "when": datetime.now(timezone.utc)},
        },
    )
    payload = query(
        "SELECT payload FROM run_events WHERE run_id=%s", (started.run_id,)
    )[0][0]
    assert payload["object"] == "awkward-value"
    assert payload["nested"]["list"] == ["awkward-value", "user"]
    assert isinstance(payload["nested"]["when"], str)


def test_tool_calls_survive_the_round_trip(started):
    store = PostgresSessionStore(DSN).bind(started)
    store.append(
        started.run_id,
        Message(
            role=Role.ASSISTANT,
            tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": "hi"}),),
        ),
    )
    restored = store.history(started.run_id)[0].tool_calls[0]
    assert (restored.id, restored.name, restored.arguments) == ("c1", "echo", {"text": "hi"})


def test_runs_are_isolated(started, scope):
    other = RunScope(run_id=str(uuid.uuid4()), tenant_id="t-test", project_id="p-test")
    PostgresRunStore(DSN).start_run(
        other,
        agent_spec_id="s",
        max_turns=1,
        model_id=None,
        principal_context=None,
        manifest=a_manifest(),
    )
    PostgresSessionStore(DSN).bind(started).append(
        started.run_id, Message(role=Role.USER, content="mine")
    )
    assert PostgresSessionStore(DSN).bind(other).history(other.run_id) == []


# --- FR-10: events ----------------------------------------------------------


def test_events_persist_with_the_full_envelope(started):
    sink = PostgresEventStore(DSN, started.tenant_id, started.project_id, started.run_id)
    sink.emit(EventType.RUN_STARTED, {"agent_spec_id": "spec-1"})
    sink.emit(EventType.MODEL_CALLED, {"turn": 1, "stop_reason": StopReason.END_TURN})

    rows = query(
        "SELECT sequence_no, event_type, schema_version, tenant_id, project_id,"
        " agent_id, task_id, tool_call_id, attempt_id, parent_event_id,"
        " correlation_id, timestamp, payload"
        " FROM run_events WHERE run_id=%s ORDER BY sequence_no",
        (started.run_id,),
    )
    assert [r[0] for r in rows] == [1, 2]
    assert [r[1] for r in rows] == ["RunStarted", "ModelCalled"]
    assert all(r[2] == 1 for r in rows)
    assert all(r[3] == "t-test" and r[4] == "p-test" for r in rows)
    # Phase 2+ slots exist and stay empty.
    assert all(r[5] is None and r[6] is None and r[7] is None and r[8] is None for r in rows)
    assert all(r[11] is not None for r in rows)
    # Enums in a payload must not break JSONB serialisation.
    assert rows[1][12]["stop_reason"] == "end_turn"


def test_event_sequence_numbers_are_unique_per_run(started):
    sink = PostgresEventStore(DSN, started.tenant_id, started.project_id, started.run_id)
    sink.emit(EventType.RUN_STARTED, {})
    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "INSERT INTO run_events (event_id, schema_version, sequence_no, event_type,"
                " tenant_id, project_id, run_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (uuid.uuid4(), 1, 1, "RunStarted", "t-test", "p-test", started.run_id),
            )


# --- FR-11 / AC-6: exactly one manifest -------------------------------------


def test_exactly_one_manifest_per_run_with_every_field_populated(started):
    """The fixture wrote this via start_run, so it is the real path under test."""
    rows = query(
        "SELECT sdk_version, agent_spec_hash, instructions_hash, model_id, model_version,"
        " model_adapter_version, tool_spec_hashes, policy_version, tenant_id, project_id"
        " FROM execution_manifests WHERE run_id=%s",
        (started.run_id,),
    )
    assert len(rows) == 1
    assert all(field is not None for field in rows[0])
    # Not merely non-null: the hashes the caller passed actually reached the row.
    assert rows[0][6] == ["abc"], "tool_spec_hashes stored something other than what was built"
    assert rows[0][3] == "openai.gpt-4o-mini"
    assert rows[0][7] == "AllowlistPermissionChecker"


def test_a_second_manifest_for_the_same_run_is_rejected(started):
    with pytest.raises(psycopg.errors.UniqueViolation):
        PostgresRunStore(DSN).write_manifest(started, a_manifest())


def test_a_run_row_is_never_written_without_its_manifest(scope):
    """AC-6 atomicity, at the store.

    The run row and the manifest were once written over two connections. A
    failure in the window between them left a `runs` row nothing could explain:
    no manifest, and no RunStarted event either, because that emit is sequenced
    after both writes. Here the manifest insert fails at the database; the run
    row must go with it.
    """
    unwritable = dict(a_manifest(), sdk_version=None)  # violates NOT NULL
    with pytest.raises(psycopg.errors.NotNullViolation):
        PostgresRunStore(DSN).start_run(
            scope,
            agent_spec_id="s",
            max_turns=1,
            model_id=None,
            principal_context=None,
            manifest=unwritable,
        )
    assert query("SELECT 1 FROM runs WHERE run_id=%s", (scope.run_id,)) == []
    assert query("SELECT 1 FROM execution_manifests WHERE run_id=%s", (scope.run_id,)) == []


def test_the_manifest_records_the_principal_context_run_settings(scope):
    """principal_context is lean metadata (ADR-27) but must survive the write."""
    PostgresRunStore(DSN).start_run(
        scope,
        agent_spec_id="s",
        max_turns=7,
        model_id="m",
        principal_context={"principal_id": "u-42", "roles": ["reader"]},
        manifest=a_manifest(),
    )
    stored = PostgresRunStore(DSN).get_run(scope)
    assert stored["principal_context"] == {"principal_id": "u-42", "roles": ["reader"]}
    assert stored["max_turns"] == 7


def test_manifest_hashes_are_stable_and_sensitive():
    base = dict(
        sdk_version="0.1.0",
        agent_spec_id="s",
        instructions="be terse",
        tool_profile=("echo",),
        tool_spec_hashes=[],
        model_id=None,
    )
    assert build_manifest(**base) == build_manifest(**base)
    assert (
        build_manifest(**base)["instructions_hash"]
        != build_manifest(**{**base, "instructions": "be verbose"})["instructions_hash"]
    )
    assert (
        build_manifest(**base)["agent_spec_hash"]
        != build_manifest(**{**base, "tool_profile": ("echo", "other")})["agent_spec_hash"]
    )
    # The spec hash must move when the instructions do: two runs with different
    # instructions are different configurations, and the manifest exists to say
    # so. Asserting only the tool_profile half left that half unpinned.
    assert (
        build_manifest(**base)["agent_spec_hash"]
        != build_manifest(**{**base, "instructions": "be verbose"})["agent_spec_hash"]
    )
    assert (
        build_manifest(**base)["agent_spec_hash"]
        != build_manifest(**{**base, "agent_spec_id": "other"})["agent_spec_hash"]
    )


# --- NFR-2 / AC-5: tenancy enforced by the schema ---------------------------


@pytest.mark.parametrize("table", TABLES)
def test_tenant_and_project_are_not_null_in_the_schema(table):
    rows = query(
        "SELECT column_name, is_nullable FROM information_schema.columns"
        " WHERE table_name=%s AND column_name IN ('tenant_id','project_id')",
        (table,),
    )
    assert len(rows) == 2, f"{table} is missing a tenancy column"
    assert all(nullable == "NO" for _, nullable in rows), f"{table} allows a null tenant"


@pytest.mark.parametrize("table", TABLES)
def test_tenancy_columns_are_indexed(table):
    rows = query(
        "SELECT indexdef FROM pg_indexes WHERE tablename=%s", (table,)
    )
    assert any(
        "tenant_id" in definition and "project_id" in definition for definition, in rows
    ), f"{table} has no (tenant_id, project_id) index"


def test_schema_sql_itself_declares_the_constraints():
    """The tests above inspect the LIVE database, which proves nothing about
    schema.sql: every statement is CREATE ... IF NOT EXISTS, so editing the file
    leaves an existing database untouched. Breaking the file would ship a broken
    schema to the next fresh deployment with the gate still green.

    So apply schema.sql into a throwaway namespace and assert on THAT.
    """
    namespace = "m5_probe_" + uuid.uuid4().hex[:8]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{namespace}"')
        try:
            conn.execute(f'SET search_path TO "{namespace}"')
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

            # ADR-11: tenancy is NOT NULL on every table.
            for table in TABLES:
                rows = conn.execute(
                    "SELECT column_name, is_nullable FROM information_schema.columns"
                    " WHERE table_schema=%s AND table_name=%s"
                    " AND column_name IN ('tenant_id','project_id')",
                    (namespace, table),
                ).fetchall()
                assert len(rows) == 2, f"{table} is missing a tenancy column"
                assert all(n == "NO" for _, n in rows), f"{table} allows a null tenant"

            # Ordering is a database guarantee, not a convention.
            for table in ("messages", "run_events"):
                constraints = conn.execute(
                    "SELECT constraint_type FROM information_schema.table_constraints"
                    " WHERE table_schema=%s AND table_name=%s AND constraint_type='UNIQUE'",
                    (namespace, table),
                ).fetchall()
                assert constraints, f"{table} has no UNIQUE (run_id, sequence_no)"

            # AC-6: "exactly one manifest" is enforced by the key, not by care.
            pk = conn.execute(
                "SELECT constraint_type FROM information_schema.table_constraints"
                " WHERE table_schema=%s AND table_name='execution_manifests'"
                " AND constraint_type='PRIMARY KEY'",
                (namespace,),
            ).fetchall()
            assert pk, "execution_manifests has no primary key on run_id"

            # NFR-2: tenancy columns are indexed, not merely present.
            for table in TABLES:
                indexes = conn.execute(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s",
                    (namespace, table),
                ).fetchall()
                assert any(
                    "tenant_id" in d and "project_id" in d for d, in indexes
                ), f"{table} has no (tenant_id, project_id) index"
        finally:
            conn.execute(f'DROP SCHEMA "{namespace}" CASCADE')


def test_an_unregistered_model_is_recorded_as_such_not_as_null():
    """AC-6 needs every manifest field populated. A model absent from the
    registry must say so rather than leave a null that could equally mean the
    writer forgot."""
    from agentsdk.registry import ModelRegistry

    runner = Runner(
        {"gw": ScriptedModel(text("hi"))},
        tools=[echo_tool()],
        model_registry=ModelRegistry([]),  # deliberately empty
    )
    versions = runner._model_versions("some.unknown-model", "gw")
    assert versions["model_version"] == "unregistered"
    assert versions["model_adapter_version"]


async def test_a_run_with_an_unregistered_model_still_writes_a_full_manifest():
    runner = Runner(
        {"gw": ScriptedModel(text("hi"))},
        tools=[echo_tool()],
        persistence=Persistence.postgres(DSN),
    )
    result = await runner.run(
        AgentSpec(id="s", instructions="i", preferred_model="gw:totally.unknown", tool_profile=("echo",)),
        "go",
        RunConfig(tenant_id="t-unreg", project_id="p-unreg"),
    )
    row = query(
        "SELECT model_id, model_version, model_adapter_version FROM execution_manifests"
        " WHERE run_id=%s",
        (result.run_id,),
    )[0]
    assert all(field is not None for field in row)
    assert row[1] == "unregistered"


def test_the_database_refuses_a_row_without_a_tenant(started):
    """Enforced, not merely tagged (ADR-11)."""
    with pytest.raises(psycopg.errors.NotNullViolation):
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "INSERT INTO messages (message_id, run_id, tenant_id, project_id,"
                " sequence_no, role) VALUES (%s,%s,NULL,%s,%s,%s)",
                (uuid.uuid4(), started.run_id, "p-test", 99, "user"),
            )


# --- end to end through the public API --------------------------------------


async def test_a_whole_run_persists_and_reconstructs():
    """AC-5, AC-6, AC-7 together, driven through Runner alone."""
    model = ScriptedModel(tool_call(), text("all done"))
    runner = Runner(
        {"gw": model},
        tools=[echo_tool()],
        persistence=Persistence.postgres(DSN),
    )
    result = await runner.run(
        AgentSpec(
            id="persist-spec",
            instructions="be terse",
            preferred_model="gw:openai.gpt-4o-mini",
            tool_profile=("echo",),
        ),
        "echo something",
        RunConfig(tenant_id="t-e2e", project_id="p-e2e", max_turns=4),
    )
    assert result.status is RunStatus.COMPLETED

    trace = PostgresTrace(DSN).reconstruct(
        RunScope(run_id=result.run_id, tenant_id="t-e2e", project_id="p-e2e")
    )

    # AC-7: the trace reconstructs from state PLUS events, in order.
    assert trace["run"]["status"] == "completed"
    assert trace["run"]["tenant_id"] == "t-e2e"
    assert trace["run"]["completed_at"] is not None
    assert [m["role"] for m in trace["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert [m["sequence_no"] for m in trace["messages"]] == [1, 2, 3, 4]
    event_types = [e["event_type"] for e in trace["events"]]
    assert event_types[0] == "RunStarted" and event_types[-1] == "RunCompleted"
    assert "ToolCalled" in event_types
    assert [e["sequence_no"] for e in trace["events"]] == list(
        range(1, len(trace["events"]) + 1)
    )

    # AC-6: exactly one manifest, fully populated -- and populated with what
    # this run actually used. "not None" left four separate mutations to the
    # manifest the Runner builds (tool hashes, instructions, sdk_version,
    # agent_spec_id) alive: every one of them still produced a non-null row.
    assert trace["manifest"] is not None
    assert all(field is not None for field in trace["manifest"])
    (
        sdk_version, agent_spec_hash, instructions_hash, manifest_model_id,
        model_version, adapter_version, tool_spec_hashes, policy_version,
    ) = trace["manifest"]

    expected = build_manifest(
        sdk_version=agentsdk_version,
        agent_spec_id="persist-spec",
        instructions="be terse",
        tool_profile=("echo",),
        tool_spec_hashes=[ToolSpec(name="echo", description="Echo",
                                   input_schema=ECHO_SCHEMA).schema_hash()],
        model_id="openai.gpt-4o-mini",
    )
    assert sdk_version == agentsdk_version
    assert agent_spec_hash == expected["agent_spec_hash"]
    assert instructions_hash == expected["instructions_hash"]
    assert manifest_model_id == "openai.gpt-4o-mini"
    # The versions come from the model registry, not from a default: this model
    # is registered, so the manifest must carry its real entry.
    registered = default_registry().resolve("openai.gpt-4o-mini")
    assert (model_version, adapter_version) == (
        registered.model_version,
        registered.adapter_version,
    )
    assert tool_spec_hashes == expected["tool_spec_hashes"], (
        "the manifest's tool hashes are not the hashes of the tools this run had"
    )
    assert policy_version == "AllowlistPermissionChecker"

    # AC-4 persisted: every stored tool result still carries provenance.
    tool_messages = [m for m in trace["messages"] if m["role"] == "tool"]
    for message in tool_messages:
        for stored in message["tool_results"]:
            assert stored["provenance"]["origin"] == "internal_tool"

    # AC-5: every row of this run is tenant-scoped.
    for table in TABLES:
        rows = query(
            f"SELECT tenant_id, project_id FROM {table} WHERE run_id=%s", (result.run_id,)
        )
        assert rows, f"{table} has no row for this run"
        assert all(t == "t-e2e" and p == "p-e2e" for t, p in rows)


async def test_a_transient_failure_at_start_leaves_nothing_unexplainable(monkeypatch):
    """M5 round-1 review defect, reproduced through the public API.

    An ordinary transient error between the run write and the manifest write
    used to commit a `runs` row with no manifest (AC-6 wants exactly one) and
    no RunStarted event, since that emit comes after both. The two writes now
    share a transaction, so the run either exists explained or not at all.
    """

    def boom(conn, scope, manifest):
        raise psycopg.OperationalError("transient blip between two writes")

    monkeypatch.setattr(PostgresRunStore, "_insert_manifest", staticmethod(boom))

    runner = Runner(
        {"gw": ScriptedModel(text("hi"))},
        tools=[echo_tool()],
        persistence=Persistence.postgres(DSN),
    )
    result = await runner.run(
        AgentSpec(id="s", instructions="i", preferred_model="gw:m", tool_profile=("echo",)),
        "go",
        RunConfig(tenant_id="t-atomic", project_id="p-atomic"),
    )

    # The M4 boundary still holds: a psycopg error comes back as a failed run.
    assert result.status is RunStatus.FAILED
    # And it leaves no half-written run behind.
    assert query("SELECT 1 FROM runs WHERE run_id=%s", (result.run_id,)) == []
    assert query("SELECT 1 FROM execution_manifests WHERE run_id=%s", (result.run_id,)) == []


async def test_every_run_this_suite_started_has_exactly_one_manifest(started):
    """AC-6 as a standing property across runs, not a single one.

    A run row with no manifest is exactly the state the round-1 defect
    produced, so assert none exists rather than checking one run at a time.
    Scoped to this session's runs: rows written before the fix are still in the
    development database, and quietly deleting another developer's data to make
    a test pass would be the wrong way to earn a green.
    """
    orphans = query(
        "SELECT r.run_id FROM runs r LEFT JOIN execution_manifests m USING (run_id)"
        " WHERE m.run_id IS NULL AND r.started_at >= %s LIMIT 5",
        (SUITE_STARTED_AT,),
    )
    assert orphans == [], f"runs started with no execution manifest: {orphans}"


async def test_a_failed_run_is_recorded_as_failed():
    class Exploding:
        async def send(self, request):
            raise RuntimeError("model exploded")

    runner = Runner({"gw": Exploding()}, tools=[echo_tool()], persistence=Persistence.postgres(DSN))
    result = await runner.run(
        AgentSpec(id="s", instructions="i", preferred_model="gw:m", tool_profile=("echo",)),
        "go",
        RunConfig(tenant_id="t-fail", project_id="p-fail"),
    )
    assert result.status is RunStatus.FAILED
    assert query("SELECT status FROM runs WHERE run_id=%s", (result.run_id,))[0][0] == "failed"
    types = [r[0] for r in query(
        "SELECT event_type FROM run_events WHERE run_id=%s ORDER BY sequence_no", (result.run_id,)
    )]
    assert types[-1] == "RunFailed"


async def test_usage_is_reported_even_when_the_boundary_catches(monkeypatch):
    """Carried M4 limitation: usage was zeroed on a non-SDK exception."""

    class HalfBroken:
        def __init__(self):
            self.calls = 0

        async def send(self, request):
            self.calls += 1
            if self.calls == 1:
                return tool_call()
            raise RuntimeError("exploded after spending tokens")

    runner = Runner({"gw": HalfBroken()}, tools=[echo_tool()])
    result = await runner.run(
        AgentSpec(id="s", instructions="i", preferred_model="gw:m", tool_profile=("echo",)),
        "go",
        RunConfig(tenant_id="t", project_id="p"),
    )
    assert result.status is RunStatus.FAILED
    assert result.usage.total_tokens == 15, "tokens spent before the failure were dropped"


# --- M5 round 3: the store must not change the outcome ----------------------


def test_arguments_error_survives_the_round_trip(started):
    """The reviewer's blind spot, and a live hazard from Phase 6 (replay).

    A dropped arguments_error turns an undecodable tool call back into a valid
    empty-arguments call, which ToolExecutor step 2 would wave straight through
    -- exactly the defect M3 was rejected for and fixed. Nothing stopped
    persistence from undoing it.
    """
    store = PostgresSessionStore(DSN).bind(started)
    store.append(
        started.run_id,
        Message(
            role=Role.ASSISTANT,
            tool_calls=(
                ToolCall(id="c1", name="echo", arguments={}, arguments_error="bad JSON"),
            ),
        ),
    )
    restored = store.history(started.run_id)[0].tool_calls[0]
    assert restored.arguments_error == "bad JSON"
    assert restored.arguments == {}


def test_a_tool_call_the_store_cannot_hold_round_trips_as_an_error(started):
    """End of the same thread: the flag is set by ToolCall itself, so it is
    still set after the row comes back."""
    store = PostgresSessionStore(DSN).bind(started)
    store.append(
        started.run_id,
        Message(
            role=Role.ASSISTANT,
            tool_calls=(ToolCall(id="c1", name="echo", arguments={"n": float("inf")}),),
        ),
    )
    restored = store.history(started.run_id)[0].tool_calls[0]
    assert restored.arguments_error is not None
    assert "cannot be stored" in restored.arguments_error
    assert restored.arguments == {}


def test_a_message_cannot_be_filed_under_another_tenant(started):
    """messages.tenant_id is denormalised so isolation needs no join. That is
    only worth having if the copy cannot disagree with the run it belongs to."""
    impostor = RunScope(
        run_id=started.run_id, tenant_id="t-someone-else", project_id="p-test"
    )
    with pytest.raises(ValueError, match="belongs to someone else"):
        PostgresSessionStore(DSN).bind(impostor).append(
            started.run_id, Message(role=Role.USER, content="not mine")
        )
    assert query(
        "SELECT 1 FROM messages WHERE run_id=%s AND tenant_id=%s",
        (started.run_id, "t-someone-else"),
    ) == []


def test_appending_to_a_run_that_does_not_exist_is_refused():
    ghost = RunScope(run_id=str(uuid.uuid4()), tenant_id="t-test", project_id="p-test")
    with pytest.raises(ValueError, match="does not exist"):
        PostgresSessionStore(DSN).bind(ghost).append(
            ghost.run_id, Message(role=Role.USER, content="orphan")
        )


def test_the_stored_tenancy_comes_from_the_run_not_the_caller(started):
    store = PostgresSessionStore(DSN).bind(started)
    store.append(started.run_id, Message(role=Role.USER, content="mine"))
    rows = query(
        "SELECT tenant_id, project_id FROM messages WHERE run_id=%s", (started.run_id,)
    )
    assert rows == [(started.tenant_id, started.project_id)]


@pytest.mark.parametrize(
    "content,arguments",
    [
        (None, {"text": "x", "n": float("inf")}),
        (None, {"text": "a" + chr(0) + "b"}),
        # Round 4: a truncated surrogate escape is a legal RFC-8259 decode that
        # walked past every named check. Built from the wire form on purpose.
        (None, {"text": json.loads('"a' + chr(92) + 'ud800b"')}),
        (None, {"text": "x", "n": 10 ** 5000}),
    ],
    ids=["inf-argument", "nul-argument", "surrogate-argument", "huge-int-argument"],
)
async def test_persistence_does_not_change_the_outcome(content, arguments):
    """The defect itself, as a regression test.

    `1e400` and a U+0000 escape are valid RFC-8259 that json.loads accepts and
    JSONB refuses, so the same run used to complete in memory and fail against
    Postgres -- breaking postgres.py's own promise that the loop cannot tell
    which store it is talking to. A CUSTOM provider is used deliberately: the
    fix has to hold for any ModelClient, not just the adapter it was found in.
    """

    class Provider:
        async def send(self, request):
            return ModelResponse(
                message=Message(
                    role=Role.ASSISTANT,
                    content=content,
                    tool_calls=(ToolCall(id="c1", name="echo", arguments=arguments),),
                ),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(1, 1, 2),
            )

    async def run(persistence):
        return await Runner(
            {"gw": Provider()}, tools=[echo_tool()], persistence=persistence
        ).run(
            AgentSpec(id="s", instructions="i", preferred_model="gw:m",
                      tool_profile=("echo",)),
            "go",
            RunConfig(tenant_id="t-store", project_id="p-store", max_turns=2),
        )

    in_memory = await run(None)
    persisted = await run(Persistence.postgres(DSN))
    assert in_memory.status is persisted.status, (
        f"the store changed the outcome: {in_memory.status} vs {persisted.status} "
        f"({persisted.error})"
    )


# --- M5 round 5: the runs row as the Runner writes it ------------------------


@pytest.mark.parametrize(
    "responses,expected_status,max_turns",
    [
        ([], "completed", 4),
        ("explode", "failed", 4),
        ("loop", "max_turns_exceeded", 1),
    ],
    ids=["completed", "failed", "max_turns_exceeded"],
)
async def test_the_runs_row_records_the_real_outcome(responses, expected_status, max_turns):
    """AC-7 reconstructs the trace from runs.status, and nothing asserted it for
    any loop-path outcome: hardcoding finish_run to 'completed' left the whole
    suite green while every exhausted and failed run was recorded as a success.
    """

    class Exploding:
        async def send(self, request):
            raise RuntimeError("model exploded")

    class Looping:
        async def send(self, request):
            return tool_call()

    if responses == "explode":
        model = Exploding()
    elif responses == "loop":
        model = Looping()
    else:
        model = ScriptedModel(text("done"))

    result = await Runner(
        {"gw": model}, tools=[echo_tool()], persistence=Persistence.postgres(DSN)
    ).run(
        AgentSpec(id="outcome-spec", instructions="be terse",
                  preferred_model="gw:openai.gpt-4o-mini", tool_profile=("echo",)),
        "go",
        RunConfig(tenant_id="t-outcome", project_id="p-outcome", max_turns=max_turns),
    )

    assert result.status.value == expected_status
    row = query(
        "SELECT status, completed_at FROM runs WHERE run_id=%s", (result.run_id,)
    )[0]
    assert row[0] == expected_status, "runs.status disagrees with the RunResult"
    assert row[1] is not None, "a finished run has no completed_at"


async def test_the_runs_row_records_what_the_runner_was_asked_for():
    """model_id, agent_spec_id and max_turns were all unasserted: the row could
    have been written with any of them wrong and nothing would have noticed."""
    result = await Runner(
        {"gw": ScriptedModel(text("done"))},
        tools=[echo_tool()],
        persistence=Persistence.postgres(DSN),
    ).run(
        AgentSpec(id="recorded-spec", instructions="be terse",
                  preferred_model="gw:openai.gpt-4o-mini", tool_profile=("echo",)),
        "go",
        RunConfig(tenant_id="t-recorded", project_id="p-recorded", max_turns=9),
    )
    stored = PostgresRunStore(DSN).get_run(
        RunScope(run_id=result.run_id, tenant_id="t-recorded", project_id="p-recorded")
    )
    assert stored["agent_spec_id"] == "recorded-spec"
    assert stored["model_id"] == "openai.gpt-4o-mini"
    assert stored["max_turns"] == 9
    assert stored["tenant_id"] == "t-recorded"

    payload = query(
        "SELECT payload FROM run_events WHERE run_id=%s AND event_type='RunStarted'",
        (result.run_id,),
    )[0][0]
    assert payload["model"] == "openai.gpt-4o-mini"
    assert payload["agent_spec_id"] == "recorded-spec"
    assert payload["max_turns"] == 9


async def test_persisted_event_ids_are_the_ones_that_were_emitted():
    """Phase 2's parent_event_id will reference these. An event whose stored id
    is not the emitted one breaks that link before it is ever used, and nothing
    compared the two."""
    events = []

    class Watching:
        async def send(self, request):
            return text("done")

    runner = Runner(
        {"gw": Watching()}, tools=[echo_tool()], persistence=Persistence.postgres(DSN)
    )
    result = await runner.run(
        AgentSpec(id="s", instructions="i", preferred_model="gw:m", tool_profile=("echo",)),
        "go",
        RunConfig(tenant_id="t-eventid", project_id="p-eventid"),
    )
    emitted = [(str(e.event_id), e.event_type.value, e.sequence_no) for e in result.events]
    stored = [
        (str(r[0]), r[1], r[2])
        for r in query(
            "SELECT event_id, event_type, sequence_no FROM run_events"
            " WHERE run_id=%s ORDER BY sequence_no",
            (result.run_id,),
        )
    ]
    assert emitted == stored, "the stored events are not the events that were emitted"
    assert len({e[0] for e in stored}) == len(stored), "duplicate event ids"


def test_history_is_tenant_scoped_on_read(started):
    """A bound store used to return any tenant's messages given a run id.
    Recorded as a decision rather than left implicit: run ids being UUIDv4
    makes that hard to exploit, but NFR-2's premise is that tenancy is
    enforced, not that identifiers are unguessable."""
    PostgresSessionStore(DSN).bind(started).append(
        started.run_id, Message(role=Role.USER, content="tenant A's message")
    )
    intruder = RunScope(
        run_id=started.run_id, tenant_id="t-other", project_id="p-other"
    )
    assert PostgresSessionStore(DSN).bind(intruder).history(started.run_id) == []


def test_reading_history_without_a_bound_scope_is_refused():
    with pytest.raises(ValueError, match="tenancy is enforced on read"):
        PostgresSessionStore(DSN).history(str(uuid.uuid4()))


async def test_an_unstorable_provider_response_id_does_not_lose_the_audit_trail():
    """provider_response_id travels beside the message into the ModelCalled
    payload, so it reaches run_events.payload exactly as the message reaches
    messages. Round 5 found it unguarded because the check had been applied to
    the message and not to what rides alongside it."""

    class Provider:
        async def send(self, request):
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, content="done"),
                stop_reason=StopReason.END_TURN,
                usage=Usage(1, 1, 2),
                provider_response_id="chatcmpl" + chr(0) + "1",
            )

    async def run(persistence):
        return await Runner(
            {"gw": Provider()}, tools=[echo_tool()], persistence=persistence
        ).run(
            AgentSpec(id="s", instructions="i", preferred_model="gw:m",
                      tool_profile=("echo",)),
            "go",
            RunConfig(tenant_id="t-respid", project_id="p-respid", max_turns=2),
        )

    in_memory = await run(None)
    persisted = await run(Persistence.postgres(DSN))
    assert in_memory.status is persisted.status
    assert persisted.status is RunStatus.COMPLETED
    # The event was still written -- an event that cannot be written is an
    # event the trail simply lacks, which is worse than one marked unstorable.
    payload = query(
        "SELECT payload FROM run_events WHERE run_id=%s AND event_type='ModelCalled'",
        (persisted.run_id,),
    )[0][0]
    assert "unstorable" in str(payload["provider_response_id"])


def test_an_event_payload_that_cannot_be_stored_is_marked_not_dropped(started):
    """_json_safe is the last line of defence for the audit trail: the
    primitives refuse unstorable values upstream, but a payload is assembled
    from many sources, and a row that will not write means no record at all."""
    sink = PostgresEventStore(DSN, started.tenant_id, started.project_id, started.run_id)
    sink.emit(EventType.RUN_STARTED, {"note": "bad" + chr(0) + "value", "ok": "fine"})

    payload = query(
        "SELECT payload FROM run_events WHERE run_id=%s", (started.run_id,)
    )[0][0]
    assert payload["ok"] == "fine"
    assert "unstorable" in payload["note"]
    assert "NUL" in payload["note"], "the marker should say why"


def test_a_stringified_object_that_renders_unstorably_is_also_marked(started):
    """_json_safe's str() fallback is a second doorway into the payload: an
    object whose repr carries a NUL reaches JSONB the same way a bare string
    does, and only the string branch was pinned."""

    class Awkward:
        def __str__(self):
            return "rendered" + chr(0) + "badly"

    sink = PostgresEventStore(DSN, started.tenant_id, started.project_id, started.run_id)
    sink.emit(EventType.RUN_STARTED, {"obj": Awkward()})

    payload = query(
        "SELECT payload FROM run_events WHERE run_id=%s", (started.run_id,)
    )[0][0]
    assert "unstorable" in payload["obj"] and "NUL" in payload["obj"]


# --- M5 round 6 --------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments_error",
    ["bad" + chr(0) + "json", "bad" + json.loads('"' + chr(92) + 'ud800"') + "json"],
    ids=["nul", "surrogate"],
)
async def test_an_unstorable_arguments_error_neither_diverges_nor_loses_usage(
    arguments_error,
):
    """arguments_error was the one field exempt from the walk that exists to
    end exemptions, and postgres.py writes it verbatim into JSONB.

    Two invariants broke at once: the store divergence round 5 was rejected
    for, and usage under-reporting -- append raises before the ModelCalled
    emit, so tokens that were really spent are reported as zero, reopening by
    another route the gap round 2's reconstruction exists to close.
    """

    class Provider:
        async def send(self, request):
            return ModelResponse(
                message=Message(
                    role=Role.ASSISTANT,
                    tool_calls=(
                        ToolCall(id="c1", name="echo", arguments={},
                                 arguments_error=arguments_error),
                    ),
                ),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(10, 6, 16),
            )

    async def run(persistence):
        return await Runner(
            {"gw": Provider()}, tools=[echo_tool()], persistence=persistence
        ).run(
            AgentSpec(id="s", instructions="i", preferred_model="gw:m",
                      tool_profile=("echo",)),
            "go",
            RunConfig(tenant_id="t-argerr", project_id="p-argerr", max_turns=1),
        )

    in_memory = await run(None)
    persisted = await run(Persistence.postgres(DSN))
    assert in_memory.status is persisted.status
    assert persisted.usage.total_tokens == 16, "spent tokens reported as zero"


def test_the_trace_is_tenant_scoped(started):
    """AC-7's own vehicle answered the tenancy question the opposite way to
    history(), one function over. Same question, same answer now."""
    PostgresSessionStore(DSN).bind(started).append(
        started.run_id, Message(role=Role.USER, content="tenant A")
    )
    intruder = RunScope(run_id=started.run_id, tenant_id="t-other", project_id="p-other")
    trace = PostgresTrace(DSN).reconstruct(intruder)
    assert trace["run"] is None
    assert trace["messages"] == []
    assert trace["events"] == []
    assert trace["manifest"] is None


def test_finishing_a_run_is_tenant_scoped(started):
    intruder = RunScope(run_id=started.run_id, tenant_id="t-other", project_id="p-other")
    PostgresRunStore(DSN).finish_run(intruder, "completed")
    assert query("SELECT status FROM runs WHERE run_id=%s", (started.run_id,))[0][0] == (
        "running"
    ), "another tenant closed this run"


@pytest.mark.parametrize("field", ["run_id", "tenant_id", "project_id"])
def test_a_scope_that_cannot_be_stored_is_refused(field):
    """Caller-supplied configuration, not model output: there is no run to keep
    alive, so refusing beats degrading, and the message names the field rather
    than surfacing as an opaque psycopg error later."""
    values = {"run_id": str(uuid.uuid4()), "tenant_id": "t", "project_id": "p"}
    values[field] = "bad" + chr(0)
    with pytest.raises(ValueError, match=f"RunScope.{field} cannot be stored"):
        RunScope(**values)


def test_an_unstorable_agent_spec_id_is_refused(scope):
    with pytest.raises(ValueError, match="agent_spec_id cannot be stored"):
        PostgresRunStore(DSN).start_run(
            scope,
            agent_spec_id="spec" + chr(0),
            max_turns=1,
            model_id=None,
            principal_context=None,
            manifest=a_manifest(),
        )
    assert query("SELECT 1 FROM runs WHERE run_id=%s", (scope.run_id,)) == []


async def test_an_unstorable_principal_context_leaves_nothing_behind(scope):
    """Round 7's defect. principal_context went straight to Jsonb(), so
    start_run raised a raw psycopg error BEFORE RunStarted was emitted -- and
    the follow-up RunFailed then hit a foreign key against a run row that was
    never written, where _safe_emit swallowed it. Round 1's defect at least
    left a discoverable orphan; this left nothing at all.
    """
    with pytest.raises(ValueError, match="principal_context cannot be stored"):
        PostgresRunStore(DSN).start_run(
            scope,
            agent_spec_id="spec-1",
            max_turns=1,
            model_id=None,
            principal_context={"agent_principal": "ag" + chr(0) + "ent"},
            manifest=a_manifest(),
        )
    assert query("SELECT 1 FROM runs WHERE run_id=%s", (scope.run_id,)) == []


def test_every_value_start_run_writes_is_checked():
    """Structural guard on the guard. Round 6's caveat listed four fields, the
    repair implemented that list, and round 7 rejected on the fifth. Derived
    from the signature so a parameter added later fails here until it is
    covered."""
    import inspect

    checked = {"agent_spec_id", "model_id", "max_turns", "principal_context", "manifest"}
    parameters = {
        name
        for name, p in inspect.signature(PostgresRunStore.start_run).parameters.items()
        if name not in ("self", "scope")
    }
    assert parameters <= checked, (
        f"start_run writes {parameters - checked} without a storability check"
    )


async def test_an_unstorable_manifest_leaves_nothing_behind(scope):
    """Note which manifest fields can carry one: `instructions` and
    `tool_profile` are HASHED, so a NUL there comes out as clean hex. The raw
    passthrough fields -- model_id, versions, policy_version -- are the ones
    that reach the row unchanged."""
    with pytest.raises(ValueError, match="manifest cannot be stored"):
        PostgresRunStore(DSN).start_run(
            scope,
            agent_spec_id="spec-1",
            max_turns=1,
            model_id=None,
            principal_context=None,
            manifest=a_manifest(model_id="gpt" + chr(0) + "4"),
        )
    assert query("SELECT 1 FROM runs WHERE run_id=%s", (scope.run_id,)) == []


def test_hashed_manifest_fields_launder_an_unstorable_value():
    """Worth pinning as a fact, not an assumption: it is why the manifest test
    above uses model_id, and why a future non-hashed field would need one."""
    manifest = a_manifest(instructions="be" + chr(0) + "terse")
    assert unstorable_reason(manifest["instructions_hash"]) is None
    assert unstorable_reason(manifest) is None


def test_a_connection_failure_never_renders_the_password(monkeypatch):
    """NFR-4: no credential reaches a persisted row, and a psycopg error is
    copied verbatim into RunResult.error and a RunFailed payload.

    Prompted by a round-7 hygiene note: a reviewer's probe echoed a DSN
    fragment into its own tool output. Nothing in the SDK was covering this
    path. Uses the real host with a CANARY password, so the server answers for
    real and no true credential is ever in play.
    """
    canary = "hunter2-CANARY-do-not-log"
    head, tail = DSN.split("://", 1)
    creds, hostpart = tail.split("@", 1)
    user = creds.split(":", 1)[0]
    wrong = f"{head}://{user}:{canary}@{hostpart}"

    with pytest.raises(psycopg.OperationalError) as excinfo:
        Persistence.postgres(wrong)
    rendered = f"{type(excinfo.value).__name__}: {excinfo.value}"
    assert canary not in rendered, "the DSN password reached an exception message"
    assert canary not in repr(excinfo.value)
