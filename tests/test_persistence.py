"""M5 gate: Postgres persistence (FR-9, FR-10, FR-11, NFR-2, AC-5, AC-6, AC-7).

These tests need a real database. They are skipped, never silently passed, when
DATABASE_URL is absent -- a green suite that quietly proved nothing would be
worse than a visible skip.

Each test runs against its own schema-qualified namespace? No: it uses a unique
run_id per test and asserts only on that run, so tests are isolated without
tearing down tables another developer may be looking at.
"""

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
    history = PostgresSessionStore(DSN).history(started.run_id)
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

    trace = PostgresTrace(DSN).reconstruct(started.run_id)
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
    assert PostgresSessionStore(DSN).history(other.run_id) == []


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
    stored = PostgresRunStore(DSN).get_run(scope.run_id)
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

    trace = PostgresTrace(DSN).reconstruct(result.run_id)

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
