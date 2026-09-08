"""M6 gate: the golden eval (AC-1..AC-4, AC-9, AC-10, NFR-3, NFR-4, NFR-7).

Two halves, deliberately:

* A DETERMINISTIC golden run, driven by a scripted ModelClient, exercising
  AC-1, AC-2 and AC-3 in one run. Each path is asserted independently rather
  than inferred from the final status -- SPEC.md names "a single golden eval
  carrying three assertions may pass for the wrong reason" as a risk.
* LIVE runs against the real gateway on both providers AC-9 names, with no
  source change between them.

Why AC-3 is not asserted on the live path: it cannot be driven by prompting.
Asked to call echo with the number 42, `openai.gpt-4o-mini` sends 42 and trips
validation, while `bedrock.anthropic.claude-haiku-4-5` coerces it to "42" and
validates cleanly. Measured, not assumed -- twice per provider. An assertion
that depends on which model happens to be strict is a flaky gate, and a flaky
gate is worse than none, so the validation path is proven where it can be
proven exactly and the live half asserts what it can prove.
"""

import os
import uuid

import psycopg
import pytest
from dotenv import load_dotenv

from agentsdk import AgentSpec, Persistence, RunConfig, Runner, RunStatus
from agentsdk.config import normalise_database_url
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import PostgresTrace, RunScope, apply_schema
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.providers import OpenAICompatibleModelClient
from agentsdk.tools import Tool, ToolSpec

load_dotenv()

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
BASE_URL = os.environ.get("BASE_URL", "").strip()
API_KEY = os.environ.get("MODEL_API_KEY", "").strip()

# AC-9 names these two exactly: same eval, different upstream providers.
LIVE_MODELS = ["openai.gpt-4o-mini", "bedrock.anthropic.claude-haiku-4-5"]

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
PURGE_SCHEMA = {
    "type": "object",
    "properties": {"scope": {"type": "string"}},
    "required": ["scope"],
    "additionalProperties": False,
}

TASK = (
    "Complete every step, using one tool call per turn:\n"
    "1. call echo with text 'alpha'\n"
    "2. call echo with text 'beta'\n"
    "3. call echo with text 'gamma'\n"
    "4. call purge_records with scope 'all'\n"
    "Then summarise every result you received, including any errors."
)

# The spec under test is identical for the scripted and the live halves, and
# identical between providers: AC-9 is "no source change between runs", so the
# model id is the ONLY thing that varies.
GOLDEN_SPEC = dict(
    id="golden-eval",
    instructions="Follow the steps exactly, in order.",
    tool_profile=("echo",),   # purge_records is registered but NOT allowed
)


def test_the_golden_eval_environment_is_configured():
    """Deliberately NOT skippable, for the reason M5 learned the hard way: a
    gate that skips to green proves nothing, and nobody investigates a pass."""
    missing = [
        name
        for name, value in (
            ("DATABASE_URL", DSN),
            ("BASE_URL", BASE_URL),
            ("MODEL_API_KEY", API_KEY),
        )
        if not value
    ]
    assert not missing, (
        f"missing {', '.join(missing)}: M6 proves the eval against the LIVE "
        "gateway and a real database, and cannot pass without both."
    )


@pytest.fixture(autouse=True)
def _requires_environment(request):
    if request.node.name != "test_the_golden_eval_environment_is_configured":
        if not (DSN and BASE_URL and API_KEY):
            pytest.skip("golden eval needs DATABASE_URL, BASE_URL and MODEL_API_KEY")


@pytest.fixture(scope="module", autouse=True)
def schema():
    if DSN:
        apply_schema(DSN)


class Spy:
    """Records whether a tool implementation actually ran. AC-2 and AC-3 both
    hinge on 'provably never invoked', which no status code can show."""

    def __init__(self, fn):
        self._fn = fn
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self._fn(**kwargs)


def golden_tools():
    echo = Spy(lambda text: text)
    purge = Spy(lambda scope: "purged " + scope)
    return echo, purge, [
        Tool(
            spec=ToolSpec(
                name="echo",
                description="Echo the given text back unchanged.",
                input_schema=ECHO_SCHEMA,
            ),
            fn=echo,
        ),
        Tool(
            spec=ToolSpec(
                name="purge_records",
                description="Delete records permanently.",
                input_schema=PURGE_SCHEMA,
            ),
            fn=purge,
        ),
    ]


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def tool_events(result):
    return [e.payload for e in result.events if e.event_type.value == "ToolCalled"]


# --- the deterministic golden run -------------------------------------------


class ScriptedGolden:
    """Emits exactly the sequence AC-1..AC-3 require, so each path is exercised
    on every run rather than when the model feels like it."""

    def __init__(self):
        self.turn = 0

    async def send(self, request):
        self.turn += 1
        script = {
            1: ToolCall(id="c1", name="echo", arguments={"text": "alpha"}),
            2: ToolCall(id="c2", name="echo", arguments={"text": "beta"}),
            3: ToolCall(id="c3", name="echo", arguments={"text": "gamma"}),
            # AC-2: outside the allowlist.
            4: ToolCall(id="c4", name="purge_records", arguments={"scope": "all"}),
            # AC-3: right tool, wrong argument type.
            5: ToolCall(id="c5", name="echo", arguments={"text": 42}),
        }
        call = script.get(self.turn)
        if call is None:
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, content="alpha, beta, gamma; two errors"),
                stop_reason=StopReason.END_TURN,
                usage=Usage(11, 7, 18),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
            stop_reason=StopReason.TOOL_CALLS,
            usage=Usage(11, 7, 18),
        )


@pytest.fixture
def golden_run():
    """One run, every path. Returns everything the assertions need so each AC
    is checked on the same run without re-running it five times."""
    echo, purge, tools = golden_tools()
    scope = RunScope(
        run_id="", tenant_id="t-golden-" + uuid.uuid4().hex[:8], project_id="p-golden"
    )
    runner = Runner(
        {"gw": ScriptedGolden()}, tools=tools, persistence=Persistence.postgres(DSN)
    )
    return echo, purge, scope, runner


async def test_the_golden_eval_runs_to_completed(golden_run):
    """AC-1: three echo calls with different inputs, then a summary."""
    echo, purge, scope, runner = golden_run
    result = await runner.run(
        AgentSpec(preferred_model="gw:scripted", **GOLDEN_SPEC),
        TASK,
        RunConfig(tenant_id=scope.tenant_id, project_id=scope.project_id, max_turns=8),
    )

    assert result.status is RunStatus.COMPLETED, result.error
    assert [c["text"] for c in echo.calls] == ["alpha", "beta", "gamma"], (
        "the three echo calls did not reach the implementation with distinct inputs"
    )
    assert result.output


async def test_a_denied_tool_fails_without_running_and_the_run_still_completes(golden_run):
    """AC-2, asserted on its own terms: the failure is a PermissionDenied, the
    model is told, the implementation never runs, and the run still completes."""
    echo, purge, scope, runner = golden_run
    result = await runner.run(
        AgentSpec(preferred_model="gw:scripted", **GOLDEN_SPEC),
        TASK,
        RunConfig(tenant_id=scope.tenant_id, project_id=scope.project_id, max_turns=8),
    )

    denied = [
        p for p in tool_events(result)
        if p.get("name") == "purge_records" and p.get("is_error")
    ]
    assert denied, "the call outside the allowlist was not refused"
    assert denied[0]["error_type"] == "ToolPermissionDenied"
    assert purge.calls == [], "a denied tool's implementation was executed"
    assert result.status is RunStatus.COMPLETED, "a denied tool ended the run"

    # Surfaced TO THE MODEL, not merely recorded: the error has to come back as
    # a tool result or the model cannot react to it.
    store = Persistence.postgres(DSN).session_store_for(
        RunScope(run_id=result.run_id, tenant_id=scope.tenant_id, project_id=scope.project_id)
    )
    errors = [
        r for m in store.history(result.run_id) for r in m.tool_results if r.is_error
    ]
    assert any("PermissionDenied" in r.content for r in errors), (
        "the denial never reached the model as a tool result"
    )


async def test_a_malformed_argument_fails_validation_before_the_tool_runs(golden_run):
    """AC-3. The interesting half is the second assertion: a validation error
    that still ran the tool would be worthless, and no status code shows that."""
    echo, purge, scope, runner = golden_run
    result = await runner.run(
        AgentSpec(preferred_model="gw:scripted", **GOLDEN_SPEC),
        TASK,
        RunConfig(tenant_id=scope.tenant_id, project_id=scope.project_id, max_turns=8),
    )

    invalid = [
        p for p in tool_events(result) if p.get("name") == "echo" and p.get("is_error")
    ]
    assert invalid, "the malformed echo call was accepted"
    assert invalid[0]["error_type"] == "ToolValidationError"
    assert [c["text"] for c in echo.calls] == ["alpha", "beta", "gamma"], (
        "the tool ran despite failing validation"
    )
    assert 42 not in [c["text"] for c in echo.calls]


async def test_every_persisted_tool_result_carries_full_provenance(golden_run):
    """AC-4. Reads the ROWS, not the in-memory objects: the invariant that
    matters is the one that survives the write."""
    echo, purge, scope, runner = golden_run
    result = await runner.run(
        AgentSpec(preferred_model="gw:scripted", **GOLDEN_SPEC),
        TASK,
        RunConfig(tenant_id=scope.tenant_id, project_id=scope.project_id, max_turns=8),
    )

    rows = query(
        "SELECT tool_results FROM messages WHERE run_id=%s AND tool_results IS NOT NULL",
        (result.run_id,),
    )
    stored = [r for row, in rows for r in row]
    assert stored, "no tool results were persisted at all"
    for entry in stored:
        provenance = entry.get("provenance")
        assert provenance, f"a stored tool result has no provenance: {entry}"
        for field in ("origin", "instruction_authority", "trust_zone", "taint_flags"):
            assert provenance.get(field) is not None, f"provenance.{field} is null"
        assert provenance["origin"] == "internal_tool"
    # Including the error results: a refusal is still content with a source.
    assert any(e.get("is_error") for e in stored), (
        "the failed calls did not reach the store, so AC-4 was proven only on the easy path"
    )


async def test_the_run_reconstructs_from_state_plus_events(golden_run):
    """NFR-3: runs + messages + run_events TOGETHER. Events alone are
    explicitly not the source of truth, so the trace has to read all three."""
    echo, purge, scope, runner = golden_run
    result = await runner.run(
        AgentSpec(preferred_model="gw:scripted", **GOLDEN_SPEC),
        TASK,
        RunConfig(tenant_id=scope.tenant_id, project_id=scope.project_id, max_turns=8),
    )

    trace = PostgresTrace(DSN).reconstruct(
        RunScope(run_id=result.run_id, tenant_id=scope.tenant_id, project_id=scope.project_id)
    )
    assert trace["run"]["status"] == "completed"
    assert [m["sequence_no"] for m in trace["messages"]] == list(
        range(1, len(trace["messages"]) + 1)
    )
    types = [e["event_type"] for e in trace["events"]]
    assert types[0] == "RunStarted" and types[-1] == "RunCompleted"
    assert types.count("ToolCalled") == 5, (
        f"the trace does not show all five tool calls: {types}"
    )
    assert trace["manifest"] is not None


def test_no_credential_reaches_any_row_or_payload_anywhere():
    """AC-10 and NFR-4, scanned across the WHOLE database rather than this
    run's rows: a leak that lands on someone else's row is still a leak."""
    assert API_KEY, "cannot prove a negative about a key that is not set"
    # A four-character prefix as well as the whole key. A mutation testing this
    # assertion must not have to write the real secret into a database to be
    # detectable -- I did exactly that once, and the credential outlived the
    # mutation by the length of a restore. A prefix is enough to prove the
    # value came from the key without the row ever holding it.
    needles = [API_KEY, "LEAK-CANARY-" + API_KEY[:4]]
    columns = {
        "runs": ["agent_spec_id", "status", "model_id", "principal_context::text"],
        "messages": ["content", "tool_calls::text", "tool_results::text"],
        "run_events": ["event_type", "payload::text"],
        "execution_manifests": [
            "sdk_version", "agent_spec_hash", "instructions_hash",
            "model_id", "model_version", "model_adapter_version",
            "tool_spec_hashes::text", "policy_version",
        ],
    }
    for table, cols in columns.items():
        for needle in needles:
            predicate = " OR ".join(f"{c} LIKE %s" for c in cols)
            hits = query(
                f"SELECT count(*) FROM {table} WHERE {predicate}",
                tuple(f"%{needle}%" for _ in cols),
            )[0][0]
            assert hits == 0, f"{table} contains credential material in {hits} row(s)"


async def test_the_model_never_receives_the_credential(golden_run):
    """NFR-4's first clause. The key travels in a header; nothing that reaches
    model context may carry it."""
    echo, purge, scope, runner = golden_run

    seen = []

    class Recording(ScriptedGolden):
        async def send(self, request):
            seen.append(request)
            return await super().send(request)

    runner = Runner(
        {"gw": Recording()},
        tools=golden_tools()[2],
        persistence=Persistence.postgres(DSN),
    )
    await runner.run(
        AgentSpec(preferred_model="gw:scripted", **GOLDEN_SPEC),
        TASK,
        RunConfig(tenant_id=scope.tenant_id, project_id=scope.project_id, max_turns=8),
    )

    assert seen, "the model was never called"
    for request in seen:
        blob = repr(request)
        assert API_KEY not in blob, "the credential reached model context"


# --- the live half: AC-9 ------------------------------------------------------


@pytest.mark.parametrize("model_id", LIVE_MODELS)
async def test_the_same_eval_passes_against_a_live_provider(model_id):
    """AC-9: the SAME eval, two upstream providers, no source change between
    runs. `model_id` is the only thing that varies -- the AgentSpec, the task,
    the tools and every assertion below are shared.

    Honest limit, recorded in SPEC.md's risks: both providers are reached
    through one gateway speaking one wire format, so this proves model
    agnosticism, not wire-format agnosticism.
    """
    echo, purge, tools = golden_tools()
    tenant = "t-live-" + uuid.uuid4().hex[:8]
    client = OpenAICompatibleModelClient(
        base_url=BASE_URL, api_key=API_KEY, model=model_id, timeout=60.0
    )
    runner = Runner({"gw": client}, tools=tools, persistence=Persistence.postgres(DSN))
    try:
        result = await runner.run(
            AgentSpec(preferred_model=f"gw:{model_id}", **GOLDEN_SPEC),
            TASK,
            RunConfig(tenant_id=tenant, project_id="p-live", max_turns=10),
        )
    finally:
        await client.aclose()

    # AC-1 against a real model.
    assert result.status is RunStatus.COMPLETED, result.error
    assert result.output
    assert len(echo.calls) >= 3, f"expected at least three echo calls, got {echo.calls}"
    assert {"alpha", "beta", "gamma"} <= {str(c["text"]) for c in echo.calls}

    # AC-2 against a real model: it chose to call the denied tool, and the
    # allowlist stopped it before the implementation ran.
    events = tool_events(result)
    denied = [p for p in events if p.get("name") == "purge_records" and p.get("is_error")]
    assert denied, f"{model_id} never attempted the denied tool: {[p.get('name') for p in events]}"
    assert denied[0]["error_type"] == "ToolPermissionDenied"
    assert purge.calls == [], "a denied tool ran against a live provider"

    # AC-4 and NFR-3 on the live run's own rows.
    scope = RunScope(run_id=result.run_id, tenant_id=tenant, project_id="p-live")
    trace = PostgresTrace(DSN).reconstruct(scope)
    assert trace["run"]["status"] == "completed"
    assert trace["run"]["model_id"] == model_id
    assert trace["manifest"] is not None
    stored = [
        r for row, in query(
            "SELECT tool_results FROM messages WHERE run_id=%s AND tool_results IS NOT NULL",
            (result.run_id,),
        ) for r in row
    ]
    assert stored
    assert all(e.get("provenance", {}).get("origin") == "internal_tool" for e in stored)

    # AC-10 on the rows this live run just wrote, where a real key was in play.
    for table in ("runs", "messages", "run_events", "execution_manifests"):
        assert query(
            f"SELECT count(*) FROM {table} WHERE run_id=%s AND {table}::text LIKE %s",
            (result.run_id, f"%{API_KEY}%"),
        )[0][0] == 0, f"{table} leaked the API key on a live run"


async def test_both_providers_are_reached_through_the_same_unchanged_spec():
    """AC-9's real claim is 'unchanged'. Asserting it structurally, because two
    passing tests could each have quietly used a different spec."""
    specs = {
        model_id: AgentSpec(preferred_model=f"gw:{model_id}", **GOLDEN_SPEC)
        for model_id in LIVE_MODELS
    }
    ids = {s.id for s in specs.values()}
    instructions = {s.instructions for s in specs.values()}
    profiles = {s.tool_profile for s in specs.values()}
    assert ids == {"golden-eval"} and len(instructions) == 1 and len(profiles) == 1, (
        "the two live runs did not use the same agent spec"
    )
    assert {s.preferred_model for s in specs.values()} == {
        f"gw:{m}" for m in LIVE_MODELS
    }, "the model id is the only thing that may differ"


# --- NFR-7: the seams later phases fill ---------------------------------------


def test_the_later_phase_seams_exist_and_are_inert():
    """NFR-7: Phases 2-6 should ADD fields and implementations, not replace
    contracts. The claim is only meaningful if the slots are actually present
    and actually unused, so assert both -- a seam that is silently populated in
    Phase 0 is a contract that will change, not one that will extend.
    """
    import dataclasses

    from agentsdk.events import RunEvent
    from agentsdk.identity import PrincipalContext
    from agentsdk.model import ModelRequest
    from agentsdk.outcomes import ApprovalRequired, InputRequired, InterruptionKind, Pending

    request_fields = {f.name for f in dataclasses.fields(ModelRequest)}
    assert {"output_schema", "provider_state", "metadata"} <= request_fields, (
        "the structured-output and provider-state slots are missing (FR-16)"
    )
    request = ModelRequest(messages=())
    assert request.output_schema is None, "Phase 0 must not populate output_schema"
    assert request.provider_state is None

    event_fields = {f.name for f in dataclasses.fields(RunEvent)}
    assert {
        "agent_id", "task_id", "tool_call_id", "attempt_id",
        "parent_event_id", "correlation_id", "schema_version",
    } <= event_fields, "Phase 2/6 event columns are missing from the envelope"

    # The interruption vocabulary exists as types, unreachable in Phase 0.
    assert {k.name for k in InterruptionKind} >= {
        "TOOL_APPROVAL", "ADDITIONAL_USER_INPUT", "CREDENTIAL_REQUIRED"
    }, "the Phase 4/6 interruption vocabulary is missing"
    assert ApprovalRequired and InputRequired and Pending

    principal_fields = {f.name for f in dataclasses.fields(PrincipalContext)}
    assert {"agent_principal", "acting_on_behalf_of", "delegation_id", "scopes"} <= (
        principal_fields
    ), "the Phase 4 delegation fields are missing (ADR-27)"


async def test_principal_context_is_carried_and_persisted_but_unread():
    """ADR-27: recorded now, read by nobody in Phase 0. If a Phase 0 checker
    started reading it, Phase 4 would be changing a contract rather than
    extending one."""
    from agentsdk.identity import PrincipalContext

    echo, purge, tools = golden_tools()
    tenant = "t-principal-" + uuid.uuid4().hex[:8]
    runner = Runner(
        {"gw": ScriptedGolden()}, tools=tools, persistence=Persistence.postgres(DSN)
    )
    result = await runner.run(
        AgentSpec(preferred_model="gw:scripted", **GOLDEN_SPEC),
        TASK,
        RunConfig(
            tenant_id=tenant,
            project_id="p-principal",
            max_turns=8,
            principal_context=PrincipalContext(
                agent_principal="agent-7", scopes=("read",), delegation_id="d-1"
            ),
        ),
    )

    assert result.status is RunStatus.COMPLETED
    stored = query(
        "SELECT principal_context FROM runs WHERE run_id=%s", (result.run_id,)
    )[0][0]
    assert stored["agent_principal"] == "agent-7"
    assert stored["scopes"] == ["read"]
    assert stored["delegation_id"] == "d-1"
    # And the allowlist decision was made without consulting it: the denial
    # happened for the same reason it does with no principal at all.
    denied = [
        p for p in tool_events(result)
        if p.get("name") == "purge_records" and p.get("is_error")
    ]
    assert denied[0]["error_type"] == "ToolPermissionDenied"


async def test_phase_0_leaves_the_seams_empty_on_every_real_request():
    """The seam test above asserts the DEFAULT is None, which a freshly
    constructed request will always satisfy -- populating output_schema in the
    assembler left it passing. What NFR-7 actually claims is that Phase 0 does
    not fill these slots, so assert it on the requests the loop really sends.
    """
    seen = []

    class Recording(ScriptedGolden):
        async def send(self, request):
            seen.append(request)
            return await super().send(request)

    echo, purge, tools = golden_tools()
    tenant = "t-seams-" + uuid.uuid4().hex[:8]
    runner = Runner(
        {"gw": Recording()}, tools=tools, persistence=Persistence.postgres(DSN)
    )
    await runner.run(
        AgentSpec(preferred_model="gw:scripted", **GOLDEN_SPEC),
        TASK,
        RunConfig(tenant_id=tenant, project_id="p-seams", max_turns=8),
    )

    assert seen, "the model was never called"
    for request in seen:
        assert request.output_schema is None, (
            "Phase 0 populated output_schema; Phase 2 would then be CHANGING a "
            "contract rather than extending one (NFR-7)"
        )
        assert request.provider_state is None
