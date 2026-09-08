"""M6 gate: the golden eval (AC-1..AC-4, AC-9, AC-10, NFR-3, NFR-4, NFR-7).

One task, one tool set, one set of assertions -- run twice: once against a
scripted ModelClient so every path is exercised on every run, and once against
each live provider AC-9 names. Each acceptance criterion is asserted on its own
terms rather than inferred from the final status, because SPEC.md names "a
single golden eval carrying three assertions may pass for the wrong reason" as
a risk of exactly this file.

AC-3 IS exercised live. An earlier version of this file argued it could not be,
on the grounds that a validation failure cannot be driven by prompting: asked
to call echo with the number 42, openai.gpt-4o-mini sends 42 and trips
validation while bedrock.anthropic.claude-haiku-4-5 coerces it to "42". That
measurement was real but the conclusion generalised from a single shape. A
constraint the instructed input CANNOT satisfy -- register_code advertises
maxLength 8 and the task supplies 36 characters -- fails validation on both
providers, 4 runs out of 4, with the implementation never invoked. Found by the
M6 reviewer; reproduced here before being believed.

The assertions deliberately target the property each criterion names rather
than something adjacent to it:

  * AC-2's "surfaced to the model" is asserted on what the model was actually
    SENT -- on the canonical request in the scripted half, and on the wire
    bytes in the live half, over every error result the run stored.
  * AC-4's "fully populated" is asserted over every field ContentProvenance
    declares, read off the dataclass, not over a list of field names.
  * AC-9's "unchanged" is asserted against what the live runs actually used,
    not against specs this file rebuilds for itself afterwards.
  * AC-10 scans whole rows across every table, not an enumerated column list.
  * NFR-4 is asserted on the wire body, not on the ModelRequest object.
  * NFR-7's "never read" is asserted by showing the decision does not change
    with the principal, not by checking an error type.

Every one of those was previously asserted one step removed from its claim, and
the tests passed for reasons weaker than the claims they underwrote. Three of
them survived a round of that correction and had to be found again -- the shape
is easier to remove from the test you are looking at than from the file.
"""

import dataclasses
import json
import os
import uuid

import httpx
import psycopg
import pytest
from dotenv import load_dotenv

from agentsdk import AgentSpec, Persistence, RunConfig, Runner, RunStatus
from agentsdk.config import normalise_database_url
from agentsdk.errors import ModelError
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import PostgresTrace, RunScope, apply_schema
from agentsdk.primitives import ContentProvenance, Message, Role, ToolCall
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
# AC-3's live trigger: a limit the task's own input cannot satisfy, so the
# failure does not depend on the model being careless.
CODE_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string", "maxLength": 8}},
    "required": ["code"],
    "additionalProperties": False,
}
LONG_CODE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

TASK = (
    "Complete every step, using one tool call per turn:\n"
    "1. call echo with text 'alpha'\n"
    "2. call echo with text 'beta'\n"
    "3. call echo with text 'gamma'\n"
    "4. call purge_records with scope 'all'\n"
    f"5. call register_code with code '{LONG_CODE}' exactly as written, "
    "all 36 characters\n"
    "Then summarise every result you received, including any errors."
)

# Identical for the scripted and live halves, and between providers: AC-9 is
# "no source change between runs", so the model id is the only thing that
# varies anywhere in this file.
GOLDEN_SPEC = dict(
    id="golden-eval",
    instructions="Follow the steps exactly, in order.",
    tool_profile=("echo", "register_code"),  # purge_records is NOT allowed
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
    """The one tool set. Returns the spies so 'never invoked' is checkable."""
    echo = Spy(lambda text: text)
    purge = Spy(lambda scope: "purged " + scope)
    register = Spy(lambda code: "registered " + code)
    return echo, purge, register, [
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
        Tool(
            spec=ToolSpec(
                name="register_code",
                description="Register a short code. Maximum 8 characters.",
                input_schema=CODE_SCHEMA,
            ),
            fn=register,
        ),
    ]


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def tool_events(result):
    return [e.payload for e in result.events if e.event_type.value == "ToolCalled"]


def errors_for(result, name):
    return [p for p in tool_events(result) if p.get("name") == name and p.get("is_error")]


class ScriptedGolden:
    """Emits exactly the sequence TASK describes, so the scripted half exercises
    the same five steps the live half is asked for."""

    def __init__(self):
        self.turn = 0
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        self.turn += 1
        script = {
            1: ToolCall(id="c1", name="echo", arguments={"text": "alpha"}),
            2: ToolCall(id="c2", name="echo", arguments={"text": "beta"}),
            3: ToolCall(id="c3", name="echo", arguments={"text": "gamma"}),
            4: ToolCall(id="c4", name="purge_records", arguments={"scope": "all"}),
            5: ToolCall(id="c5", name="register_code", arguments={"code": LONG_CODE}),
        }
        call = script.get(self.turn)
        if call is None:
            return ModelResponse(
                message=Message(
                    role=Role.ASSISTANT,
                    content="alpha, beta, gamma echoed; purge denied; code rejected",
                ),
                stop_reason=StopReason.END_TURN,
                usage=Usage(11, 7, 18),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
            stop_reason=StopReason.TOOL_CALLS,
            usage=Usage(11, 7, 18),
        )


async def run_scripted(tenant_suffix, *, principal_context=None):
    """One scripted run, returning everything the assertions need."""
    echo, purge, register, tools = golden_tools()
    tenant = f"t-{tenant_suffix}-" + uuid.uuid4().hex[:8]
    model = ScriptedGolden()
    runner = Runner({"gw": model}, tools=tools, persistence=Persistence.postgres(DSN))
    result = await runner.run(
        AgentSpec(preferred_model="gw:scripted", **GOLDEN_SPEC),
        TASK,
        RunConfig(
            tenant_id=tenant,
            project_id="p-golden",
            max_turns=10,
            principal_context=principal_context,
        ),
    )
    scope = RunScope(run_id=result.run_id, tenant_id=tenant, project_id="p-golden")
    return result, scope, echo, purge, register, model


# --- AC-1, AC-2, AC-3: asserted on the scripted run ---------------------------


async def test_the_golden_eval_runs_to_completed():
    """AC-1: three echo calls with different inputs, then a summary."""
    result, _scope, echo, _purge, _register, _model = await run_scripted("ac1")

    assert result.status is RunStatus.COMPLETED, result.error
    assert [c["text"] for c in echo.calls] == ["alpha", "beta", "gamma"], (
        "the three echo calls did not reach the implementation with distinct inputs"
    )
    assert result.output


async def test_a_denied_tool_is_refused_and_the_model_is_told():
    """AC-2, asserted on what the model was SENT.

    The previous version read the error back out of the persisted history,
    which proves the store recorded it -- not that the model ever saw it. The
    criterion says "surfaced to the model", so the assertion belongs on the
    next request that actually went out.
    """
    result, _scope, _echo, purge, _register, model = await run_scripted("ac2")

    denied = errors_for(result, "purge_records")
    assert denied, "the call outside the allowlist was not refused"
    assert denied[0]["error_type"] == "ToolPermissionDenied"
    assert purge.calls == [], "a denied tool's implementation was executed"
    assert result.status is RunStatus.COMPLETED, "a denied tool ended the run"

    # The turn after the denial must carry it back as a tool result.
    after_denial = model.requests[4]
    results = [r for m in after_denial.messages for r in m.tool_results]
    assert any(
        r.is_error and "PermissionDenied" in r.content for r in results
    ), "the denial was never sent back to the model"


async def test_a_malformed_argument_fails_validation_before_the_tool_runs():
    """AC-3. The second assertion is the one that matters: a validation error
    that still ran the tool would be worthless, and no status code shows it."""
    result, _scope, _echo, _purge, register, model = await run_scripted("ac3")

    invalid = errors_for(result, "register_code")
    assert invalid, "the over-long code was accepted"
    assert invalid[0]["error_type"] == "ToolValidationError"
    assert register.calls == [], "the tool ran despite failing validation"

    after_failure = model.requests[5]
    results = [r for m in after_failure.messages for r in m.tool_results]
    assert any(
        r.is_error and "ValidationError" in r.content for r in results
    ), "the validation failure was never sent back to the model"


async def test_every_persisted_tool_result_carries_full_provenance():
    """AC-4, over every field ContentProvenance DECLARES -- not a list of them.

    The previous version named four fields, and the fifth, source_uri_or_hash,
    was null on every error result in the database: the enumeration was shaped
    around the one field that was broken, so it could not report it. Nothing
    asserted that field anywhere, which left dropping the schema hash from the
    success path green too. Reading the names off the dataclass covers a field
    a later phase adds without anyone remembering -- the same fix AC-10 got.

    Non-null is the letter of AC-4; the values are asserted as well, because a
    field populated with the wrong thing satisfies "non-null" and satisfies
    nothing else.
    """
    result, _scope, _echo, _purge, _register, _model = await run_scripted("ac4")
    schema_hashes = {tool.spec.schema_hash() for tool in golden_tools()[3]}

    rows = query(
        "SELECT tool_results FROM messages WHERE run_id=%s AND tool_results IS NOT NULL",
        (result.run_id,),
    )
    stored = [r for row, in rows for r in row]
    assert stored, "no tool results were persisted at all"

    declared = {f.name for f in dataclasses.fields(ContentProvenance)}
    # FR-2 names these five explicitly, so their absence is a spec violation
    # rather than a refactor; asserting it also keeps the loop below from going
    # vacuous if fields() ever returns nothing.
    assert declared >= {
        "origin", "instruction_authority", "trust_zone", "taint_flags",
        "source_uri_or_hash",
    }, f"ContentProvenance no longer declares FR-2's five fields: {declared}"

    for entry in stored:
        provenance = entry.get("provenance")
        assert provenance, f"a stored tool result has no provenance: {entry}"
        for field in declared:
            assert provenance.get(field) is not None, (
                f"provenance.{field} is null on a persisted result: {entry}"
            )
        assert provenance["origin"] == "internal_tool"
        source = provenance["source_uri_or_hash"]
        if entry.get("is_error"):
            # A failed call's content is the executor's rendering of the error;
            # naming the tool's schema hash would claim the tool produced text
            # it never produced -- on these two paths it was never reached.
            assert source.startswith("urn:agentsdk:tool-error:"), (
                f"an error result's provenance does not name what produced it: {source!r}"
            )
        else:
            assert source in schema_hashes, (
                f"a successful result's provenance does not identify its tool: {source!r}"
            )
    assert any(e.get("is_error") for e in stored), (
        "the failed calls did not reach the store, so AC-4 was proven only on "
        "the easy path"
    )


async def test_the_run_reconstructs_from_state_plus_events():
    """NFR-3: runs + messages + run_events TOGETHER. Events alone are
    explicitly not the source of truth, so the trace has to read all three."""
    result, scope, _echo, _purge, _register, _model = await run_scripted("nfr3")

    trace = PostgresTrace(DSN).reconstruct(scope)
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


# --- AC-10 and NFR-4: credentials ---------------------------------------------


def credential_needles():
    """Whole key, a prefix, and the mutation canary.

    The prefix matters: a mutation proving this assertion works should not have
    to write the real secret into a database to be detectable. One did, once,
    and the credential outlived the mutation by the length of the restore.
    """
    return [API_KEY, API_KEY[:8], "LEAK-CANARY-" + API_KEY[:4]]


def test_no_credential_reaches_any_row_or_payload_anywhere():
    """AC-10 says "no persisted row ... anywhere in the database", so the scan
    is over WHOLE ROWS of EVERY table.

    The previous version enumerated a column subset per table and searched only
    for the full key. It missed a canary written into run_events.tool_call_id
    on 120 rows -- the column list was the defect, the same shape that produced
    five of M5's nine rejections. `{table}::text` renders every column of the
    row, so a column added tomorrow is covered without anyone remembering.
    """
    assert API_KEY, "cannot prove a negative about a key that is not set"
    tables = [
        t for t, in query(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema='public' AND table_type='BASE TABLE'"
        )
    ]
    assert {"runs", "messages", "run_events", "execution_manifests"} <= set(tables), (
        f"the scan is not seeing the tables it must cover: {tables}"
    )
    for table in tables:
        for needle in credential_needles():
            hits = query(
                f'SELECT count(*) FROM "{table}" WHERE "{table}"::text LIKE %s',
                (f"%{needle}%",),
            )[0][0]
            assert hits == 0, (
                f"{table} contains credential material in {hits} row(s)"
            )


class RecordingTransport(httpx.AsyncHTTPTransport):
    """Forwards to the real gateway and keeps what actually crossed the wire.

    NFR-4 is about what reaches model context, and AC-2's "surfaced to the
    model" is the same question in the other direction. Asserting either on a
    ModelRequest object proves the SDK's own dataclass is right, not that the
    bytes the provider received are.

    Responses are kept too, and the body has to be read and re-attached here
    because the stream can only be consumed once.
    """

    def __init__(self):
        super().__init__()
        self.bodies = []
        self.auth_headers = []
        self.responses = []

    async def handle_async_request(self, request):
        self.bodies.append(request.content.decode("utf-8", "replace"))
        self.auth_headers.append(request.headers.get("authorization", ""))
        response = await super().handle_async_request(request)
        body = await response.aread()
        await response.aclose()
        self.responses.append(body)
        # aread() has already decoded any content-encoding, so those two headers
        # would now describe the body incorrectly.
        headers = [
            (k, v)
            for k, v in response.headers.raw
            if k.lower() not in (b"content-encoding", b"content-length")
        ]
        return httpx.Response(response.status_code, headers=headers, content=body)


# --- AC-9: the live half ------------------------------------------------------


@dataclasses.dataclass
class LiveRun:
    """What one live provider run actually used, and what came back.

    AC-9's claim is that the eval is UNCHANGED between providers, which can
    only be checked against what the runs really used. Keeping the spec, the
    tools and the task the run was given -- rather than rebuilding them later
    from the same constant -- is what makes that assertion able to fail.
    """

    model_id: str
    spec: AgentSpec
    tools: tuple
    task: str
    result: object
    tenant: str
    transport: RecordingTransport
    echo: Spy
    purge: Spy
    register: Spy
    attempts: int


_LIVE_RUNS: dict[str, LiveRun] = {}


async def live_run(model_id):
    """One live run per provider per session, shared by every test needing it.

    Cached so that observing the same run from two tests costs one call to the
    gateway rather than two, and so that the two tests are talking about the
    same run rather than about two runs that happen to agree.
    """
    if model_id not in _LIVE_RUNS:
        _LIVE_RUNS[model_id] = await _execute_live(model_id)
    return _LIVE_RUNS[model_id]


def _model_boundary_errors():
    """Every error class in the ModelError family, read off the hierarchy.

    A hand-written list is the shape this project has been rejected for five
    times, and it failed here too on the first try: the check was written as
    "ModelError" in result.error, and an unreachable gateway reports
    ModelProviderUnavailable, so the retry never fired for the one condition
    it exists for. Measured against a closed port, not assumed.
    """
    seen, stack = {ModelError}, [ModelError]
    while stack:
        for subclass in stack.pop().__subclasses__():
            if subclass not in seen:
                seen.add(subclass)
                stack.append(subclass)
    return tuple(f"{cls.__name__}: " for cls in seen)


_MODEL_BOUNDARY_ERRORS = _model_boundary_errors()


async def _execute_live(model_id):
    """Run the golden eval against a live provider.

    One retry, and only when the run failed AT THE MODEL BOUNDARY: `runner.run`
    is total, so an unreachable or rate-limiting gateway arrives as a completed
    call reporting RunStatus.FAILED with a ModelError-family reason, which is a
    fact about the network rather than about this SDK. Every other failure --
    including a run that completes and then fails an assertion -- is reported
    as-is, because a gate that retries until it agrees with itself is not a
    gate.

    No carve-out inside the family: a deterministic fault fails again on the
    second attempt and is reported, so the retry can only ever absorb a
    transient one. `attempts` is asserted on below, so a retry that did happen
    cannot pass unnoticed -- the gate stays honest about a sick gateway.
    """
    attempts = 0
    while True:
        attempts += 1
        echo, purge, register, tools = golden_tools()
        tenant = "t-live-" + uuid.uuid4().hex[:8]
        transport = RecordingTransport()
        spec = AgentSpec(preferred_model=f"gw:{model_id}", **GOLDEN_SPEC)
        task = TASK
        client = OpenAICompatibleModelClient(
            base_url=BASE_URL,
            api_key=API_KEY,
            model=model_id,
            client=httpx.AsyncClient(transport=transport, timeout=60.0),
            timeout=60.0,
        )
        runner = Runner(
            {"gw": client}, tools=tools, persistence=Persistence.postgres(DSN)
        )
        try:
            result = await runner.run(
                spec,
                task,
                RunConfig(tenant_id=tenant, project_id="p-live", max_turns=12),
            )
        finally:
            await client.aclose()

        gateway_fault = result.status is not RunStatus.COMPLETED and (
            result.error or ""
        ).startswith(_MODEL_BOUNDARY_ERRORS)
        if gateway_fault and attempts == 1:
            continue
        return LiveRun(
            model_id=model_id,
            spec=spec,
            tools=tuple(tools),
            task=task,
            result=result,
            tenant=tenant,
            transport=transport,
            echo=echo,
            purge=purge,
            register=register,
            attempts=attempts,
        )


@pytest.mark.parametrize("model_id", LIVE_MODELS)
async def test_the_same_eval_passes_against_a_live_provider(model_id):
    """AC-9: the SAME eval, two upstream providers, no source change. `model_id`
    is the only thing that varies -- task, tools, spec and assertions are shared
    with the scripted half above.

    Honest limit, recorded in SPEC.md's risks: both providers are reached
    through one gateway speaking one wire format, so this proves model
    agnosticism, not wire-format agnosticism.
    """
    run = await live_run(model_id)
    result, echo, purge, register = run.result, run.echo, run.purge, run.register

    # AC-1 against a real model.
    assert result.status is RunStatus.COMPLETED, result.error
    assert len(echo.calls) >= 3, f"expected at least three echo calls, got {echo.calls}"
    assert {"alpha", "beta", "gamma"} <= {str(c["text"]) for c in echo.calls}
    # The summary is the MODEL's, unlike the scripted half where it is the
    # script's own string, so it can be asserted to mention what happened.
    assert result.output and any(
        word in result.output.lower() for word in ("alpha", "echo")
    ), f"the model produced no usable summary: {result.output!r}"

    # AC-2 against a real model: it chose to call the denied tool, and the
    # allowlist stopped it before the implementation ran.
    denied = errors_for(result, "purge_records")
    assert denied, (
        f"{model_id} never attempted the denied tool: "
        f"{[p.get('name') for p in tool_events(result)]}"
    )
    assert denied[0]["error_type"] == "ToolPermissionDenied"
    assert purge.calls == [], "a denied tool ran against a live provider"

    # AC-3 against a real model. Deterministic because the constraint, not the
    # model's care, is what fails: 8 characters allowed, 36 supplied.
    invalid = errors_for(result, "register_code")
    assert invalid, f"{model_id} did not produce a validation failure"
    assert invalid[0]["error_type"] == "ToolValidationError"
    assert register.calls == [], "a tool ran despite failing validation"

    # AC-4 and NFR-3 on this run's own rows.
    scope = RunScope(run_id=result.run_id, tenant_id=run.tenant, project_id="p-live")
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
    assert all(
        e.get("provenance", {}).get("source_uri_or_hash") is not None for e in stored
    ), "AC-4's fifth field is null on a live run"

    # AC-2's third clause on the live path: "surfaced to the model as an error
    # tool result", in that same run. Asserted on the exact bytes, over EVERY
    # error result the run stored rather than an enumerated pair -- an adapter
    # sending every error to the model as the neutral word "done" passed all
    # 421 tests before this existed, while this file's own docstring claimed
    # the opposite was already true.
    wire = "".join(run.transport.bodies)
    failed = [e for e in stored if e.get("is_error")]
    assert failed, "the live run stored no error results, so AC-2 proved nothing"
    for entry in failed:
        encoded = json.dumps(entry["content"])[1:-1]
        assert encoded in wire, (
            f"an error result never reached the model: {entry['content']!r}"
        )

    # NFR-4 on the WIRE, with a real key in play.
    assert run.transport.bodies, "nothing was sent"
    for body in run.transport.bodies:
        assert API_KEY not in body, "the credential reached the request body"
    assert any(API_KEY in h for h in run.transport.auth_headers), (
        "the key never travelled in the Authorization header, so this test "
        "would pass even if the client stopped authenticating"
    )

    # A retry is legitimate but must never be silent.
    assert run.attempts == 1, (
        f"{model_id} needed {run.attempts} attempts: the first failed at the "
        "model boundary. The gate passed, but the gateway was not healthy."
    )

    # AC-10 on this run's rows, whole-row.
    for table in ("runs", "messages", "run_events", "execution_manifests"):
        for needle in credential_needles():
            assert query(
                f"SELECT count(*) FROM {table} WHERE run_id=%s AND {table}::text LIKE %s",
                (result.run_id, f"%{needle}%"),
            )[0][0] == 0, f"{table} leaked credential material on a live run"


async def test_both_providers_are_reached_through_the_same_unchanged_spec():
    """AC-9's real claim is "unchanged", asserted against what the live runs
    ACTUALLY used.

    The previous version rebuilt its own specs from GOLDEN_SPEC and never
    looked at the live half at all, so it could not fail: making the live half
    secretly use a different instructions string and a narrower tool_profile
    for one provider left it green -- the precise divergence its own docstring
    said it existed to catch.
    """
    runs = [await live_run(model_id) for model_id in LIVE_MODELS]
    specs = [run.spec for run in runs]

    assert {s.id for s in specs} == {"golden-eval"}
    assert len({s.instructions for s in specs}) == 1, "the instructions differed"
    assert len({s.tool_profile for s in specs}) == 1, "the tool profile differed"
    assert len({run.task for run in runs}) == 1, "the task differed"
    assert len({
        tuple((t.spec.name, t.spec.schema_hash()) for t in run.tools) for run in runs
    }) == 1, "the tools differed"
    assert {s.preferred_model for s in specs} == {
        f"gw:{m}" for m in LIVE_MODELS
    }, "the model id is the only thing that may differ"

    # AC-9's honest weak spot, previously unasserted: two model ids are no
    # evidence of two upstreams. trace["run"]["model_id"] is only what was
    # ASKED for, and the gateway echoes the alias straight back (measured:
    # both come back exactly as sent), so the response's own `model` field
    # proves nothing either. What the gateway does not normalise is the
    # envelope it relays: OpenAI's carries system_fingerprint and service_tier
    # and an OpenAI-format completion id, Bedrock's carries neither and a
    # UUID. Identical envelope shapes from both aliases is what ONE backend
    # answering for both would look like. Evidence, not proof -- SPEC.md's
    # wire-format limitation stands either way.
    shapes = []
    for run in runs:
        keys, id_shapes = set(), set()
        for raw in run.transport.responses:
            try:
                body = json.loads(raw)
            except ValueError:
                continue
            if isinstance(body, dict):
                keys |= set(body)
                id_shapes.add(str(body.get("id", "")).count("-"))
        shapes.append((frozenset(keys), frozenset(id_shapes)))
    assert all(shape[0] for shape in shapes), f"no JSON responses recorded: {shapes}"
    assert shapes[0] != shapes[1], (
        f"both model ids produced response envelopes of identical shape "
        f"({shapes[0]}), which is what one upstream answering for both would "
        "look like"
    )


# --- NFR-7: the seams later phases fill ---------------------------------------


def test_the_later_phase_seams_exist_and_are_inert():
    """NFR-7: Phases 2-6 should ADD fields and implementations, not replace
    contracts. The claim is only meaningful if the slots are present AND
    unused, so assert both."""
    import dataclasses

    from agentsdk.events import RunEvent
    from agentsdk.identity import PrincipalContext
    from agentsdk.model import ModelRequest
    from agentsdk.outcomes import ApprovalRequired, InputRequired, InterruptionKind, Pending

    request_fields = {f.name for f in dataclasses.fields(ModelRequest)}
    assert {"output_schema", "provider_state", "metadata"} <= request_fields, (
        "the structured-output and provider-state slots are missing (FR-16)"
    )

    event_fields = {f.name for f in dataclasses.fields(RunEvent)}
    assert {
        "agent_id", "task_id", "tool_call_id", "attempt_id",
        "parent_event_id", "correlation_id", "schema_version",
    } <= event_fields, "Phase 2/6 event columns are missing from the envelope"

    assert {k.name for k in InterruptionKind} >= {
        "TOOL_APPROVAL", "ADDITIONAL_USER_INPUT", "CREDENTIAL_REQUIRED"
    }, "the Phase 4/6 interruption vocabulary is missing"
    assert ApprovalRequired and InputRequired and Pending

    principal_fields = {f.name for f in dataclasses.fields(PrincipalContext)}
    assert {"agent_principal", "acting_on_behalf_of", "delegation_id", "scopes"} <= (
        principal_fields
    ), "the Phase 4 delegation fields are missing (ADR-27)"


async def test_phase_0_leaves_the_seams_empty_on_every_real_request():
    """The default being None is not evidence about the code path that fills
    it: populating output_schema in the assembler left the previous version of
    this assertion green. Assert on the requests the loop really sends."""
    _result, _scope, _echo, _purge, _register, model = await run_scripted("seams")

    assert model.requests, "the model was never called"
    for request in model.requests:
        assert request.output_schema is None, (
            "Phase 0 populated output_schema; Phase 2 would then be CHANGING a "
            "contract rather than extending one (NFR-7)"
        )
        assert request.provider_state is None


async def test_the_credential_never_reaches_model_context_on_the_scripted_path():
    """NFR-4's first clause, where there is no wire to inspect."""
    _result, _scope, _echo, _purge, _register, model = await run_scripted("nfr4")

    assert model.requests
    for request in model.requests:
        assert API_KEY not in repr(request), "the credential reached model context"


async def test_the_permission_decision_does_not_depend_on_the_principal():
    """ADR-27: principal_context is carried and persisted but read by nobody in
    Phase 0. Asserted by showing the DECISION does not move with the principal
    -- the previous version checked an error type, which would be identical
    whether or not the checker consulted it.
    """
    from agentsdk.identity import PrincipalContext

    principals = [
        None,
        PrincipalContext(agent_principal="agent-7", scopes=("read",), delegation_id="d-1"),
        PrincipalContext(agent_principal="agent-9", scopes=("admin", "purge_records")),
    ]
    reasons = []
    for principal in principals:
        result, _scope, _echo, purge, _register, _model = await run_scripted(
            "principal", principal_context=principal
        )
        denied = errors_for(result, "purge_records")
        assert denied, "the denial did not happen"
        reasons.append((denied[0]["error_type"], purge.calls == []))

    assert len(set(reasons)) == 1, (
        f"the permission decision changed with the principal: {reasons} -- "
        "a Phase 0 checker is reading principal_context, so Phase 4 would be "
        "changing a contract rather than extending one"
    )

    # And it IS persisted, unread: recorded now so Phase 4 has a history.
    result, _scope, _echo, _purge, _register, _model = await run_scripted(
        "principal", principal_context=principals[1]
    )
    stored = query(
        "SELECT principal_context FROM runs WHERE run_id=%s", (result.run_id,)
    )[0][0]
    assert stored["agent_principal"] == "agent-7"
    assert stored["scopes"] == ["read"]
    assert stored["delegation_id"] == "d-1"
