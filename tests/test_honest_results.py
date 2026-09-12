"""M9 gate: honest results (FR-26..FR-34, NFR-11, NFR-12, AC-20..AC-26).

Written before the implementation, against the owner-approved specification of
2026-09-12. Each property is asserted over a class of cases rather than one
example: every shape a cut-off response can take, every way a run can end,
every way a model can be chosen, every kind of body an error response carries.

The in-memory tests need no network and no credentials. The persisted ones need
DATABASE_URL and fail rather than skip without it (M5's lesson). Every row they
write is removed afterwards.

Names M9 adds (ModelPricing, call_cost, ReasoningEffort) are reached through
their modules rather than imported by name, so that before the implementation
each test fails on its own instead of the whole file failing to import.

Round 2 (after the round 1 rejection) adds, again before the repair: a failure
injected at every point a run passes through after its model answers (R2), error
bodies that fail to arrive over a real socket (R1), and the reviewer's caveats.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import os
import pathlib
import shutil
import subprocess
import sys
import uuid
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from dotenv import load_dotenv

import agentsdk.model as model_module
import agentsdk.registry as registry_module
from agentsdk import AgentSpec, Persistence, RunConfig, Runner, RunStatus, migrate
from agentsdk.config import REDACTED, normalise_database_url
from agentsdk.errors import ModelError, ModelProviderUnavailable, ModelRateLimited
from agentsdk.events import EventType, InMemoryEventSink
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.migrate import apply_migrations
from agentsdk.model import ModelRequest, ModelResponse, StopReason, Usage
from agentsdk.permissions import AllowlistPermissionChecker
from agentsdk.postgres import SCHEMA_PATH
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.providers import OpenAICompatibleModelClient, RetryPolicy
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelRegistry
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import Tool, ToolSpec

load_dotenv()

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
REPO = pathlib.Path(__file__).resolve().parents[1]
TENANT, PROJECT = "SYN-m9", "p-m9"
KEY = "secret-key-value"
SIX = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
)
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


# --- shared helpers --------------------------------------------------------------


def pricing(**prices):
    return registry_module.ModelPricing(**prices)


def call_cost(usage, model_pricing):
    return registry_module.call_cost(usage, model_pricing)


def text(content, stop=StopReason.END_TURN, usage=None):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, content=content),
        stop_reason=stop,
        usage=usage if usage is not None else Usage(10, 5, 15),
    )


def calls(*tool_calls, content=None, stop=StopReason.TOOL_CALLS, usage=None):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, content=content, tool_calls=tuple(tool_calls)),
        stop_reason=stop,
        usage=usage if usage is not None else Usage(10, 5, 15),
    )


class Scripted:
    """Replays responses; an exception in the script is raised instead."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        item = self.script.pop(0) if self.script else text("done")
        if isinstance(item, BaseException):
            raise item
        return item


class Spy:
    def __init__(self):
        self.texts = []

    def __call__(self, text):
        self.texts.append(text)
        return text


def echo_tool(spy=None):
    return Tool(
        spec=ToolSpec(name="echo", description="Echo text back.", input_schema=ECHO_SCHEMA),
        fn=spy if spy is not None else (lambda text: text),
    )


class RecordingChecker:
    """Records every call id that reached the permission check."""

    def __init__(self):
        self.ids = []
        self._inner = AllowlistPermissionChecker({"echo"})

    def check(self, tool_call, principal_context=None):
        self.ids.append(tool_call.id)
        return self._inner.check(tool_call, principal_context)


class RecordingHook(RuntimeHook):
    def __init__(self):
        self.before_tool_ids = []

    def before_tool(self, tool_call):
        self.before_tool_ids.append(tool_call.id)
        return super().before_tool(tool_call)


class Recorder:
    """A RunRecorder that keeps what the Runner hands the store."""

    records_accounting = True

    def __init__(self):
        self.started = []
        self.finished = []

    def start_run(self, scope, **fields):
        self.started.append(fields)

    def finish_run(self, scope, status, usage=None, cost_usd=None):
        self.finished.append((status, usage, cost_usd))


def recording(runner, recorder):
    runner._persistence = SimpleNamespace(
        runs=recorder,
        session_store_for=lambda scope: InMemorySessionStore(),
        event_sink_for=lambda scope: InMemoryEventSink(scope.tenant_id, scope.project_id, scope.run_id),
    )
    return runner


def events_of(result, event_type):
    return [e for e in result.events if e.event_type is event_type]


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def completion(content="ok", tool_calls=None, finish_reason="stop", usage=None, model="model-a"):
    return {
        "id": "resp-1",
        "model": model,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
            }
        ],
        "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
    }


def http_client(handler, model="model-a", **kwargs):
    return OpenAICompatibleModelClient(
        base_url="https://gateway.example/",
        api_key=KEY,
        model=model,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def request():
    return ModelRequest(messages=(Message(role=Role.USER, content="hi"),))


def registry(**pricings):
    """One entry per model id; a value of None registers the model unpriced."""
    return ModelRegistry(
        [
            ModelEntry(
                provider="test",
                model_id=model_id,
                model_version="1",
                adapter_version="test/1",
                capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=model_pricing),
            )
            for model_id, model_pricing in pricings.items()
        ]
    )


PRICE_A = dict(
    input=Decimal("0.000003"), output=Decimal("0.000015"),
    cache_read=Decimal("0.0000003"), cache_write=Decimal("0.00000375"),
)
PRICE_B = dict(
    input=Decimal("0.0000005"), output=Decimal("0.000002"),
    cache_read=Decimal("0.00000005"), cache_write=Decimal("0.000001"),
)


# --- database fixtures -------------------------------------------------------------


def test_the_m9_suite_has_a_database():
    """Not skippable: FR-31's persisted half is only provable against a store."""
    assert DSN, "M9 persists usage and cost; DATABASE_URL must be set"


@pytest.fixture(scope="module")
def persistence():
    assert DSN, "DATABASE_URL must be set"
    return Persistence.postgres(DSN)


@pytest.fixture
def written():
    """Run ids a test wrote; every row for them is removed afterwards."""
    ids = []
    yield ids
    if ids and DSN:
        with psycopg.connect(DSN, autocommit=True) as conn:
            for table in ("run_events", "messages", "execution_manifests", "runs"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s::uuid[])", (ids,))


# --- AC-20 / FR-26: a cut-off or filtered response fails the run -------------------------

CUT_TEXT = "The sea is wide and"
UNFINISHED = [StopReason.MAX_TOKENS, StopReason.CONTENT_FILTER]
WARM = ToolCall(id="warm-1", name="echo", arguments={"text": "warm"})


def cut_shape(shape, stop):
    decodable = ToolCall(id="cut-1", name="echo", arguments={"text": "CUT"})
    if shape == "text_only":
        return text(CUT_TEXT, stop=stop), CUT_TEXT
    if shape == "undecodable_call":
        broken = ToolCall(id="cut-1", name="echo", arguments={}, arguments_error="JSONDecodeError: Unterminated string")
        return calls(broken, stop=stop), None
    if shape == "decodable_call":
        return calls(decodable, stop=stop), None
    if shape == "several_calls":
        second = ToolCall(id="cut-2", name="echo", arguments={"text": "CUT"})
        return calls(decodable, second, stop=stop), None
    if shape == "text_with_calls":
        return calls(decodable, content=CUT_TEXT, stop=stop), CUT_TEXT
    raise AssertionError(shape)


SHAPES = ["text_only", "undecodable_call", "decodable_call", "several_calls", "text_with_calls"]
POSITIONS = {"first": 5, "middle": 5, "last": 2}  # position -> max_turns


@pytest.mark.parametrize("persisted", [False, True], ids=["memory", "postgres"])
@pytest.mark.parametrize("position", list(POSITIONS))
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("stop", UNFINISHED, ids=lambda s: s.value)
async def test_a_cut_off_or_filtered_response_fails_the_run_and_runs_none_of_its_calls(
    stop, shape, position, persisted, request, written
):
    response, expected_output = cut_shape(shape, stop)
    script = [response] if position == "first" else [calls(WARM), response]
    spy, checker, hook = Spy(), RecordingChecker(), RecordingHook()
    sessions = InMemorySessionStore()
    runner = Runner(
        {"gw": Scripted(*script)},
        tools=[echo_tool(spy)],
        hook=hook,
        session_store=sessions,
        persistence=request.getfixturevalue("persistence") if persisted else None,
    )
    agent = AgentSpec(id="m9-cut", instructions="i", preferred_model="gw:model-a",
                      tool_profile=("echo",), permission_policy=checker)

    result = await runner.run(agent, "go", RunConfig(tenant_id=TENANT, project_id=PROJECT,
                                                     max_turns=POSITIONS[position]))
    if persisted:
        written.append(result.run_id)

    assert result.status is RunStatus.FAILED
    assert result.error == stop.value
    assert result.output == expected_output
    warm_texts = [] if position == "first" else ["warm"]
    assert spy.texts == warm_texts, "a tool implementation ran for the cut-off response"
    assert not [i for i in checker.ids if i.startswith("cut")], "a cut-off call reached the permission check"
    assert not [i for i in hook.before_tool_ids if i.startswith("cut")], "a cut-off call reached before_tool"
    assert not [e for e in events_of(result, EventType.TOOL_CALLED)
                if str(e.payload.get("tool_call_id", "")).startswith("cut")]
    assert events_of(result, EventType.MODEL_CALLED)[-1].payload["stop_reason"] == stop.value
    assert events_of(result, EventType.RUN_FAILED)[-1].payload["reason"] == stop.value

    if persisted:
        rows = query("SELECT role, tool_calls, tool_results FROM messages WHERE run_id=%s ORDER BY sequence_no",
                     (result.run_id,))
        results = [r for _, _, tool_results in rows for r in (tool_results or [])]
        assert not [r for r in results if r["tool_call_id"].startswith("cut")]
        assert rows[-1][0] == "assistant", "the cut-off response itself was not recorded"
        assert query("SELECT status FROM runs WHERE run_id=%s", (result.run_id,))[0][0] == "failed"
        stops = [p["stop_reason"] for (p,) in query(
            "SELECT payload FROM run_events WHERE run_id=%s AND event_type='ModelCalled' ORDER BY sequence_no",
            (result.run_id,))]
        assert stops[-1] == stop.value
    else:
        history = sessions.history(result.run_id)
        assert history[-1].role is Role.ASSISTANT, "the cut-off response itself was not recorded"
        assert not [r for m in history for r in m.tool_results if r.tool_call_id.startswith("cut")]


async def test_the_same_responses_with_normal_stop_reasons_complete_and_run_their_calls():
    """The control: without it, a loop that failed every run would pass AC-20."""
    spy = Spy()
    call = ToolCall(id="cut-1", name="echo", arguments={"text": "CUT"})
    result = await Runner({"gw": Scripted(calls(call), text(CUT_TEXT))}, tools=[echo_tool(spy)]).run(
        AgentSpec(id="m9", instructions="i", preferred_model="gw:model-a", tool_profile=("echo",)),
        "go", RunConfig(tenant_id=TENANT, project_id=PROJECT),
    )
    assert result.status is RunStatus.COMPLETED
    assert result.output == CUT_TEXT
    assert spy.texts == ["CUT"]


async def test_an_unrecognised_stop_reason_behaves_as_it_did_before_m9():
    result = await Runner({"gw": Scripted(text(CUT_TEXT, stop=StopReason.OTHER))}, tools=[echo_tool()]).run(
        AgentSpec(id="m9", instructions="i", preferred_model="gw:model-a", tool_profile=("echo",)),
        "go", RunConfig(tenant_id=TENANT, project_id=PROJECT),
    )
    assert result.status is RunStatus.COMPLETED
    assert result.output == CUT_TEXT


# --- AC-21 / FR-27 / FR-28: output limit and reasoning effort -------------------------------

TOKENS = {"neither": (None, None), "agent": (100, None), "run": (None, 200), "both": (100, 200)}
EFFORTS = {"neither": (None, None), "agent": ("low", None), "run": (None, "high"), "both": ("low", "high")}


@pytest.mark.parametrize("effort_case", list(EFFORTS))
@pytest.mark.parametrize("tokens_case", list(TOKENS))
async def test_output_limit_and_reasoning_effort_reach_the_payload_and_the_manifest(tokens_case, effort_case):
    agent_tokens, run_tokens = TOKENS[tokens_case]
    agent_effort, run_effort = EFFORTS[effort_case]
    expected_tokens = run_tokens if run_tokens is not None else agent_tokens
    expected_effort = run_effort if run_effort is not None else agent_effort
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=completion())

    agent_fields = {}
    if agent_tokens is not None:
        agent_fields["max_output_tokens"] = agent_tokens
    if agent_effort is not None:
        agent_fields["reasoning_effort"] = agent_effort
    run_fields = {}
    if run_tokens is not None:
        run_fields["max_output_tokens"] = run_tokens
    if run_effort is not None:
        run_fields["reasoning_effort"] = run_effort

    agent = AgentSpec(id="m9-settings", instructions="i", preferred_model="gw:model-a", **agent_fields)
    config = RunConfig(tenant_id=TENANT, project_id=PROJECT, **run_fields)
    recorder = Recorder()
    runner = recording(Runner({"gw": http_client(handler)}), recorder)

    if expected_effort is not None and expected_tokens is None:
        # D2: the adapter's 1024 default makes Claude answer HTTP 400.
        with pytest.raises(ValueError, match="max_output_tokens"):
            await runner.run(agent, "go", config)
        assert seen == [] and recorder.started == [], "the run started before the configuration error"
        return

    result = await runner.run(agent, "go", config)
    assert result.status is RunStatus.COMPLETED, result.error
    payload = seen[0]
    assert payload["max_tokens"] == (expected_tokens if expected_tokens is not None else 1024)
    if expected_effort is None:
        assert "reasoning_effort" not in payload
    else:
        assert payload["reasoning_effort"] == expected_effort
    manifest = recorder.started[0]["manifest"]
    assert manifest["max_output_tokens"] == expected_tokens
    assert manifest["reasoning_effort"] == expected_effort


BAD_TOKENS = [True, False, 1.5, "10", 0, -1, 2**31, [100]]


@pytest.mark.parametrize("bad", BAD_TOKENS, ids=repr)
@pytest.mark.parametrize("owner", ["agent", "run"])
def test_an_invalid_output_limit_is_refused_at_construction_by_name(owner, bad):
    with pytest.raises(ValueError, match="max_output_tokens"):
        if owner == "agent":
            AgentSpec(id="a", instructions="i", max_output_tokens=bad)
        else:
            RunConfig(tenant_id="t", project_id="p", max_output_tokens=bad)


BAD_EFFORTS = ["extreme", "LOW", "", 3, True, b"low", ["low"]]


@pytest.mark.parametrize("bad", BAD_EFFORTS, ids=repr)
@pytest.mark.parametrize("owner", ["agent", "run"])
def test_an_invalid_reasoning_effort_is_refused_at_construction_by_name(owner, bad):
    with pytest.raises(ValueError, match="reasoning_effort"):
        if owner == "agent":
            AgentSpec(id="a", instructions="i", reasoning_effort=bad)
        else:
            RunConfig(tenant_id="t", project_id="p", reasoning_effort=bad)


def test_valid_limits_and_efforts_are_accepted_in_every_spelling():
    effort = model_module.ReasoningEffort
    assert {e.value for e in effort} == {"low", "medium", "high"}
    assert RunConfig(tenant_id="t", project_id="p", max_output_tokens=2**31 - 1).max_output_tokens == 2**31 - 1
    assert AgentSpec(id="a", instructions="i", reasoning_effort="medium").reasoning_effort is effort.MEDIUM
    assert RunConfig(tenant_id="t", project_id="p", reasoning_effort=effort.HIGH).reasoning_effort is effort.HIGH


# --- AC-22 / FR-29: usage detail ---------------------------------------------------------------

# Captured from the 2026-09-12 gateway probe, structure and numbers only.
OPENAI_CACHED = {
    "completion_tokens": 2, "prompt_tokens": 2620, "total_tokens": 2622,
    "completion_tokens_details": {"accepted_prediction_tokens": 0, "audio_tokens": 0,
                                  "reasoning_tokens": 0, "rejected_prediction_tokens": 0},
    "prompt_tokens_details": {"audio_tokens": 0, "cached_tokens": 2560},
}
BEDROCK_REASONING = {
    "completion_tokens": 45, "prompt_tokens": 48, "total_tokens": 93,
    "completion_tokens_details": {"reasoning_tokens": 25, "text_tokens": 20},
    "prompt_tokens_details": {"cached_tokens": 0, "text_tokens": 48, "cache_write_tokens": 0,
                              "cache_creation_tokens": 0},
    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
}
# The probe could not trigger Anthropic caching (no cache markers), so this is the
# probe's own structure with non-zero counts, each reported in both places.
BEDROCK_CACHED = {
    "completion_tokens": 9, "prompt_tokens": 1500, "total_tokens": 1509,
    "completion_tokens_details": {"reasoning_tokens": 0, "text_tokens": 9},
    "prompt_tokens_details": {"cached_tokens": 1200, "text_tokens": 100, "cache_write_tokens": 200,
                              "cache_creation_tokens": 200},
    "cache_creation_input_tokens": 200, "cache_read_input_tokens": 1200,
}


@pytest.mark.parametrize(
    "usage, expected",
    [
        (OPENAI_CACHED, (2620, 2, 2622, 2560, 0, 0)),
        (BEDROCK_REASONING, (48, 45, 93, 0, 0, 25)),
        (BEDROCK_CACHED, (1500, 9, 1509, 1200, 200, 0)),
    ],
    ids=["openai-cached", "bedrock-reasoning", "bedrock-cached-double-reported"],
)
async def test_both_gateway_usage_shapes_parse_to_six_counts_each_counted_once(usage, expected):
    client = http_client(lambda req: httpx.Response(200, json=completion(usage=usage)))
    response = await client.send(request())
    assert tuple(getattr(response.usage, name) for name in SIX) == expected


class HostileInt:
    def __int__(self):
        raise RuntimeError("no int for you")


HOSTILE = [float("nan"), float("inf"), float("-inf"), "12", "lots", None, True, [], {}, HostileInt(), -5, 10**5000]


def test_every_usage_field_survives_every_hostile_value():
    """Walks dataclasses.fields(Usage), so a field added later is covered."""
    names = {f.name for f in dataclasses.fields(Usage)}
    assert set(SIX) <= names, f"Usage lacks {sorted(set(SIX) - names)}"
    for field_, value in itertools.product(dataclasses.fields(Usage), HOSTILE):
        usage = Usage(**{field_.name: value})
        stored = getattr(usage, field_.name)
        assert type(stored) is int, f"{field_.name}={value!r} stored {stored!r}"


@pytest.mark.parametrize(
    "details",
    ["lots", [1, 2], None, {"cached_tokens": "NaN", "cache_write_tokens": float("inf")}, {"cached_tokens": []}],
    ids=repr,
)
async def test_hostile_usage_details_cannot_break_the_adapter(details):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
             "prompt_tokens_details": details, "completion_tokens_details": details,
             "cache_read_input_tokens": details, "cache_creation_input_tokens": details}
    # Sent as raw text: httpx's json= refuses to encode inf, but Infinity is a
    # legal json.loads result and exactly what a provider can put on the wire.
    body = json.dumps(completion(usage=usage)).encode()
    client = http_client(lambda req: httpx.Response(200, content=body,
                                                    headers={"content-type": "application/json"}))
    response = await client.send(request())
    assert all(type(getattr(response.usage, name)) is int for name in SIX)


async def test_a_run_failing_at_the_runner_boundary_reports_usage_summed_from_its_events():
    rich = Usage(100, 20, 120, cache_read_tokens=60, cache_write_tokens=10, reasoning_tokens=5)
    model = Scripted(calls(WARM, usage=rich), calls(WARM, usage=rich), RuntimeError("boom"))
    result = await Runner({"gw": model}, tools=[echo_tool()]).run(
        AgentSpec(id="m9", instructions="i", preferred_model="gw:model-a", tool_profile=("echo",)),
        "go", RunConfig(tenant_id=TENANT, project_id=PROJECT),
    )
    assert result.status is RunStatus.FAILED and "boom" in result.error
    summed = Usage()
    for event in events_of(result, EventType.MODEL_CALLED):
        assert set(SIX) <= set(event.payload["usage"]), "ModelCalled does not carry all six counts"
        summed = summed + Usage(**event.payload["usage"])
    assert result.usage == summed == rich + rich


# --- AC-23 / FR-30 / NFR-11: cost ----------------------------------------------------------------

# 10**18 stays inside a BIGINT, so the grid exercises arithmetic rather than storage.
COUNTS = [0, 1, 1000, -7, 10**18]


def expected_cost(usage, model_pricing):
    """The FR-30 rule, written out independently of the implementation.

    Exact rational arithmetic with no decimal context at all, so it cannot share
    the implementation's precision (round 1 caveat: lowering that precision to
    40 digits passed the old oracle). A token class's count is taken as
    reported, and "a non-zero count with no price" is read on that count before
    anything is clamped: a negative count is non-zero (round 1 caveat). Counts
    are clamped at zero only for the arithmetic, so a cost is never negative.
    """
    if model_pricing is None:
        return None
    p, c = usage.prompt_tokens, usage.completion_tokens
    cr, cw = usage.cache_read_tokens, usage.cache_write_tokens
    classes = [(p - cr - cw, model_pricing.input), (cr, model_pricing.cache_read),
               (cw, model_pricing.cache_write), (c, model_pricing.output)]
    if any(count != 0 and price is None for count, price in classes):
        return None
    return sum((Fraction(max(count, 0)) * Fraction(price) for count, price in classes if price is not None),
               Fraction(0))


def pricings():
    full = PRICE_A
    return {
        "full": pricing(**full),
        "no_input": pricing(**{**full, "input": None}),
        "no_output": pricing(**{**full, "output": None}),
        "no_cache_read": pricing(**{**full, "cache_read": None}),
        "no_cache_write": pricing(**{**full, "cache_write": None}),
        "free": pricing(input=0, output=0, cache_read=0, cache_write=0),
        # Sixty significant digits: beside a 10**18 count the exact sum needs
        # about eighty, so any precision that loses digits is caught.
        "long_prices": pricing(input="0." + "7" * 60, output="0." + "3" * 60,
                               cache_read="0." + "1" * 60, cache_write="0." + "9" * 60),
        "unpriced": None,
    }


def test_cost_over_a_grid_of_usages_and_pricings_is_none_or_finite_and_never_negative():
    checked = 0
    for name, model_pricing in pricings().items():
        for p, c, cr, cw, r in itertools.product(COUNTS, repeat=5):
            usage = Usage(p, c, p + c, cache_read_tokens=cr, cache_write_tokens=cw, reasoning_tokens=r)
            got = call_cost(usage, model_pricing)
            want = expected_cost(usage, model_pricing)
            if want is None:
                assert got is None, f"{name} {usage}: expected None, got {got}"
            else:
                assert isinstance(got, Decimal) and got.is_finite() and got >= 0, f"{name} {usage}: {got!r}"
                assert Fraction(got) == want, f"{name} {usage}: {got} != {float(want)}"
            # Reasoning is billed inside completion, never added again.
            assert got == call_cost(dataclasses.replace(usage, reasoning_tokens=0), model_pricing)
            checked += 1
    assert checked == 8 * len(COUNTS) ** 5


def test_cost_survives_counts_past_the_integer_digit_limit_and_hostile_usages():
    model_pricing = pricing(**PRICE_A)
    huge = Usage(prompt_tokens=10**5000, completion_tokens=10**5000)
    got = call_cost(huge, model_pricing)
    assert got is None or (got.is_finite() and got >= 0)
    for value in HOSTILE:
        got = call_cost(Usage(prompt_tokens=value, completion_tokens=value), model_pricing)
        assert got is None or (isinstance(got, Decimal) and got.is_finite() and got >= 0)


@pytest.mark.parametrize("bad", [-1, Decimal("-0.1"), float("nan"), float("inf"), "abc", True, [1]], ids=repr)
def test_an_invalid_price_is_refused_when_the_pricing_is_built(bad):
    with pytest.raises(ValueError):
        pricing(input=bad)


def test_prices_are_decimals_and_floats_are_taken_at_their_written_value():
    built = pricing(input=0.1, output="0.000015", cache_read=Decimal("0.0000003"), cache_write=None)
    assert built.input == Decimal("0.1") and isinstance(built.output, Decimal)
    assert built.cache_write is None


def test_the_default_registry_ships_no_prices():
    for entry in registry_module.default_registry().entries():
        assert entry.capabilities.pricing is None, f"{entry.model_id} carries a bundled price"


class HaltOnCall(RuntimeHook):
    def __init__(self, on_call):
        self.on_call, self.calls = on_call, 0

    def before_model(self, request):
        self.calls += 1
        if self.calls == self.on_call:
            return HookOutcome(action=HookAction.HALT, reason="halted by test")
        return super().before_model(request)


def path_scenario(path):
    """(script, max_turns, hook, status, usages that produced a ModelCalled event)."""
    # Built here, not at import: before M9, Usage refuses these fields, and a
    # module-level failure would hide which tests fail and why.
    U1 = Usage(1000, 200, 1200, cache_read_tokens=600, cache_write_tokens=100, reasoning_tokens=50)
    U2 = Usage(1500, 100, 1600, cache_read_tokens=1200)
    warm = calls(WARM, usage=U1)
    return {
        "completed": ([warm, text("done", usage=U2)], 5, None, RunStatus.COMPLETED, [U1, U2]),
        "model_error": ([warm, ModelProviderUnavailable("down")], 5, None, RunStatus.FAILED, [U1]),
        "max_tokens": ([warm, text("par", stop=StopReason.MAX_TOKENS, usage=U2)], 5, None, RunStatus.FAILED, [U1, U2]),
        "content_filter": ([warm, text("", stop=StopReason.CONTENT_FILTER, usage=U2)], 5, None, RunStatus.FAILED,
                           [U1, U2]),
        "max_turns_exceeded": ([warm, calls(WARM, usage=U2)], 2, None, RunStatus.MAX_TURNS_EXCEEDED, [U1, U2]),
        "hook_halt": ([warm, text("never")], 5, HaltOnCall(2), RunStatus.FAILED, [U1]),
        "boundary_exception": ([warm, RuntimeError("boom")], 5, None, RunStatus.FAILED, [U1]),
    }[path]


PATHS = ["completed", "model_error", "max_tokens", "content_filter", "max_turns_exceeded", "hook_halt",
         "boundary_exception"]


@pytest.mark.parametrize("persisted", [False, True], ids=["memory", "postgres"])
@pytest.mark.parametrize("priced", [True, False], ids=["priced", "unpriced"])
@pytest.mark.parametrize("path", PATHS)
async def test_cost_is_the_sum_of_per_call_costs_on_every_terminal_path(path, priced, persisted, request, written):
    script, max_turns, hook, status, called = path_scenario(path)
    model_pricing = pricing(**PRICE_A) if priced else None
    runner = Runner(
        {"gw": Scripted(*script)},
        tools=[echo_tool()],
        hook=hook,
        model_registry=registry(**{"model-a": model_pricing}),
        persistence=request.getfixturevalue("persistence") if persisted else None,
    )
    result = await runner.run(
        AgentSpec(id="m9-cost", instructions="i", preferred_model="gw:model-a", tool_profile=("echo",)),
        "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=max_turns),
    )
    if persisted:
        written.append(result.run_id)
    assert result.status is status, result.error

    per_call = [e.payload["cost_usd"] for e in events_of(result, EventType.MODEL_CALLED)]
    assert len(per_call) == len(called)
    if priced:
        expected = [call_cost(u, model_pricing) for u in called]
        assert [Decimal(c) for c in per_call] == expected
        assert result.cost_usd == sum(expected, Decimal(0))
    else:
        assert per_call == [None] * len(called)
        assert result.cost_usd is None, "an unpriced model reported a cost"

    if persisted:
        row = query(
            "SELECT status, " + ", ".join(SIX) + ", cost_usd FROM runs WHERE run_id=%s", (result.run_id,)
        )[0]
        assert row[0] == status.value
        assert tuple(row[1:7]) == tuple(getattr(result.usage, name) for name in SIX)
        assert row[7] == result.cost_usd


@pytest.mark.parametrize("priced", [True, False], ids=["priced", "unpriced"])
async def test_a_run_that_made_no_model_call_costs_zero_only_when_its_model_is_priced(priced):
    runner = Runner({"gw": Scripted()}, tools=[echo_tool()], hook=HaltOnCall(1),
                    model_registry=registry(**{"model-a": pricing(**PRICE_A) if priced else None}))
    result = await runner.run(AgentSpec(id="m9", instructions="i", preferred_model="gw:model-a"),
                              "go", RunConfig(tenant_id=TENANT, project_id=PROJECT))
    assert result.status is RunStatus.FAILED
    assert result.cost_usd == (Decimal(0) if priced else None)


# --- AC-24 / FR-31: migration 0003 -----------------------------------------------------------------

NEW_RUN_COLUMNS = set(SIX) | {"cost_usd"}
NEW_MANIFEST_COLUMNS = {"max_output_tokens", "reasoning_effort", "pricing"}


class Namespace:
    """A throwaway schema, as in M7: the live database is already migrated, so
    asserting against it would prove nothing about the migration."""

    def __init__(self, baseline=True):
        self.baseline = baseline
        self.name = "m9_" + uuid.uuid4().hex[:8]
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

    def columns(self, table):
        return set(query(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema=%s AND table_name=%s", (self.name, table)))


def test_migration_0003_brings_a_database_at_0002_forward_and_leaves_existing_rows_null(monkeypatch):
    assert DSN, "DATABASE_URL must be set"
    real = migrate.discover()
    assert "0003" in [version for version, _ in real], "migration 0003 is not on disk"
    tables = ("runs", "execution_manifests")

    with Namespace() as ns:
        monkeypatch.setattr(migrate, "discover", lambda: [m for m in real if m[0] <= "0002"])
        assert apply_migrations(ns.dsn) == ["0002"]
        run_id = str(uuid.uuid4())
        with psycopg.connect(ns.dsn, autocommit=True) as conn:
            conn.execute("INSERT INTO runs (run_id, tenant_id, project_id, agent_spec_id, status, max_turns)"
                         " VALUES (%s, 't', 'p', 's', 'completed', 3)", (run_id,))
            conn.execute("INSERT INTO execution_manifests (run_id, tenant_id, project_id, sdk_version,"
                         " agent_spec_hash, instructions_hash) VALUES (%s, 't', 'p', 'v', 'h', 'h')", (run_id,))
        assert NEW_RUN_COLUMNS.isdisjoint({name for name, _ in ns.columns("runs")}), "the baseline already has them"

        monkeypatch.setattr(migrate, "discover", lambda: real)
        assert apply_migrations(ns.dsn) == [v for v, _ in real if v > "0002"]
        assert NEW_RUN_COLUMNS <= {name for name, _ in ns.columns("runs")}
        assert NEW_MANIFEST_COLUMNS <= {name for name, _ in ns.columns("execution_manifests")}
        with psycopg.connect(ns.dsn) as conn:
            run_row = conn.execute(f"SELECT {', '.join(sorted(NEW_RUN_COLUMNS))} FROM runs").fetchone()
            manifest_row = conn.execute(
                f"SELECT {', '.join(sorted(NEW_MANIFEST_COLUMNS))} FROM execution_manifests").fetchone()
        assert set(run_row) == {None} and set(manifest_row) == {None}, "existing rows were given values"

        assert apply_migrations(ns.dsn) == [], "a second application changed something"
        upgraded = {table: ns.columns(table) for table in tables}

    with Namespace(baseline=False) as fresh:
        apply_migrations(fresh.dsn, baseline=SCHEMA_PATH)
        assert {table: fresh.columns(table) for table in tables} == upgraded


async def test_persisted_manifest_records_the_effective_limit_effort_and_pricing(persistence, written):
    runner = Runner({"gw": Scripted()}, tools=[echo_tool()], persistence=persistence,
                    model_registry=registry(**{"model-a": pricing(**PRICE_A)}))
    result = await runner.run(
        AgentSpec(id="m9-manifest", instructions="i", preferred_model="gw:model-a",
                  max_output_tokens=300, reasoning_effort="low"),
        "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, reasoning_effort="high"),
    )
    written.append(result.run_id)
    assert result.status is RunStatus.COMPLETED, result.error
    tokens, effort, stored_pricing = query(
        "SELECT max_output_tokens, reasoning_effort, pricing FROM execution_manifests WHERE run_id=%s",
        (result.run_id,))[0]
    assert (tokens, effort) == (300, "high")
    assert {k: Decimal(v) for k, v in stored_pricing.items()} == PRICE_A


# --- AC-25 / FR-32: the model id is recorded however it was chosen -----------------------------------

RESOLUTION = {
    "client_prefixed_override": (None, "gw:model-b", "model-b"),
    "bare_override_one_client": (None, "model-b", "model-b"),
    "client_prefixed_preferred_model": ("gw:model-b", None, "model-b"),
    "bare_preferred_model": ("model-b", None, "model-b"),
    "no_model_named": (None, None, "model-a"),
}


@pytest.mark.parametrize("case", list(RESOLUTION))
async def test_every_record_of_the_model_names_the_model_the_request_used(case):
    preferred, override, expected = RESOLUTION[case]
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=completion())

    prices = {"model-a": pricing(**PRICE_A), "model-b": pricing(**PRICE_B)}
    recorder = Recorder()
    runner = recording(Runner({"gw": http_client(handler, model="model-a")},
                              model_registry=registry(**prices)), recorder)
    result = await runner.run(AgentSpec(id="m9-model", instructions="i", preferred_model=preferred),
                              "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, model_override=override))

    assert result.status is RunStatus.COMPLETED, result.error
    assert recorder.started[0]["model_id"] == expected
    assert recorder.started[0]["manifest"]["model_id"] == expected
    assert recorder.started[0]["manifest"]["model_version"] == "1", "the registry entry was not found"
    assert events_of(result, EventType.RUN_STARTED)[0].payload["model"] == expected
    assert seen[0]["model"] == expected
    assert result.cost_usd == call_cost(Usage(1000, 100, 1100), prices[expected])


async def test_a_run_on_the_client_default_model_persists_that_model(persistence, written):
    runner = Runner({"gw": http_client(lambda req: httpx.Response(200, json=completion()), model="model-a")},
                    persistence=persistence, model_registry=registry(**{"model-a": pricing(**PRICE_A)}))
    result = await runner.run(AgentSpec(id="m9-default", instructions="i"), "go",
                              RunConfig(tenant_id=TENANT, project_id=PROJECT))
    written.append(result.run_id)
    assert result.status is RunStatus.COMPLETED, result.error
    assert query("SELECT model_id FROM runs WHERE run_id=%s", (result.run_id,))[0][0] == "model-a"
    assert query("SELECT model_id FROM execution_manifests WHERE run_id=%s", (result.run_id,))[0][0] == "model-a"
    assert query("SELECT cost_usd FROM runs WHERE run_id=%s", (result.run_id,))[0][0] == result.cost_usd
    assert result.cost_usd is not None


class DefaultModelClient:
    def __init__(self, default):
        self._default = default

    @property
    def default_model_id(self):
        if isinstance(self._default, BaseException):
            raise self._default
        return self._default

    async def send(self, request):
        return text("ok")


class SendOnly:
    async def send(self, request):
        return text("ok")


@pytest.mark.parametrize(
    "client",
    [SendOnly(), DefaultModelClient(RuntimeError("no default")), DefaultModelClient(42),
     DefaultModelClient(""), DefaultModelClient("bad\x00id"), DefaultModelClient(None)],
    ids=["send-only", "raising", "not-a-string", "empty", "unstorable", "none"],
)
async def test_a_client_without_a_usable_default_model_id_still_runs_and_records_none(client):
    recorder = Recorder()
    runner = recording(Runner({"only": client}, model_registry=registry(**{"model-a": pricing(**PRICE_A)})),
                       recorder)
    result = await runner.run(AgentSpec(id="m9", instructions="i"), "go",
                              RunConfig(tenant_id=TENANT, project_id=PROJECT))
    assert result.status is RunStatus.COMPLETED, result.error
    assert recorder.started[0]["model_id"] is None
    assert recorder.started[0]["manifest"]["model_id"] == "unspecified"
    assert events_of(result, EventType.RUN_STARTED)[0].payload["model"] is None
    assert result.cost_usd is None


# --- AC-26 / FR-33: the two M3 limitations ----------------------------------------------------------

ERROR_BODIES = {
    "empty": b"",
    "non_json": b"<html>busy</html>",
    "invalid_utf8": b"\xff\xfe\xfd not text",
    "nested_past_recursion_limit": (b"[" * 100_000) + (b"]" * 100_000),
    "json_array": b"[1, 2, 3]",
    "json_null": b"null",
    "error_as_string": json.dumps({"error": "slow down"}).encode(),
    "error_without_message": json.dumps({"error": {"code": 429}}).encode(),
    "very_large": json.dumps({"error": {"message": "x" * 5_000_000}}).encode(),
}


@pytest.mark.parametrize("body", list(ERROR_BODIES))
async def test_every_429_is_rate_limited_and_retried_twice_whatever_its_body(body):
    attempts = []

    def handler(req):
        attempts.append(1)
        return httpx.Response(429, content=ERROR_BODIES[body], headers={"content-type": "application/json"})

    client = http_client(handler, retry=RetryPolicy(attempts=3, backoff_seconds=0))
    with pytest.raises(ModelRateLimited):
        await client.send(request())
    assert len(attempts) == 3, f"{len(attempts)} attempts; FR-15 allows exactly two retries"


@pytest.mark.parametrize("status", [500, 502, 503])
@pytest.mark.parametrize("body", list(ERROR_BODIES))
async def test_every_5xx_is_provider_unavailable_whatever_its_body(body, status):
    client = http_client(lambda req: httpx.Response(status, content=ERROR_BODIES[body]),
                         retry=RetryPolicy(attempts=3, backoff_seconds=0))
    with pytest.raises(ModelProviderUnavailable):
        await client.send(request())


# Bodies that cannot be decoded as their headers declare. Found by the author's
# own probe after the first green run: httpx decodes the body INSIDE the request,
# before anything looks at the status, so a 429 whose Content-Encoding did not
# match its bytes came back as ModelProviderUnavailable after one attempt.
MISDECLARED_ENCODINGS = {
    "gzip_header_plain_body": ({"content-encoding": "gzip"}, b'{"error": {"message": "slow down"}}'),
    "deflate_header_garbage": ({"content-encoding": "deflate"}, b"\x00\x01garbage"),
    "unknown_encoding": ({"content-encoding": "bogus"}, b"plain"),
    "unknown_charset": ({"content-type": "application/json; charset=not-a-charset"}, b'{"error": "x"}'),
}


class StreamedBody(httpx.AsyncByteStream):
    """A body read lazily, as it is from a real socket.

    httpx.Response(content=...) decodes its bytes in the constructor, so the
    DecodingError is raised inside the test's own handler, before the adapter is
    involved at all. The first version of this test did exactly that, and so did
    the probe: both went red before the fix and stayed red after it, which is a
    test that cannot tell broken code from fixed code.
    """

    def __init__(self, data):
        self._data = data

    async def __aiter__(self):
        yield self._data


@pytest.mark.parametrize("status", [429, 500, 503, 200])
@pytest.mark.parametrize("case", list(MISDECLARED_ENCODINGS))
async def test_a_body_that_cannot_be_decoded_as_declared_does_not_change_the_classification(case, status):
    expected = {429: ModelRateLimited, 500: ModelProviderUnavailable, 503: ModelProviderUnavailable,
                200: ModelError}[status]
    headers, body = MISDECLARED_ENCODINGS[case]
    attempts = []

    def handler(req):
        attempts.append(1)
        return httpx.Response(status, headers=headers, stream=StreamedBody(body))

    client = http_client(handler, retry=RetryPolicy(attempts=3, backoff_seconds=0))
    with pytest.raises(ModelError) as raised:
        await client.send(request())
    # Exact type: ModelRateLimited and ModelProviderUnavailable are ModelErrors
    # too, so pytest.raises alone would accept the misclassification.
    assert raised.type is expected, f"{raised.type.__name__}: {raised.value}"
    assert len(attempts) == (3 if status == 429 else 1)


def strings_in(value):
    """Every string reachable from a value, walked generically, never by field name."""
    found, stack = [], [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Enum):
            item = item.value
        if isinstance(item, str):
            found.append(item)
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            stack.extend(getattr(item, f.name) for f in dataclasses.fields(item))
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
    return found


def escaped(value):
    """A JSON-escaped rendering: a legal wire form that literal matching misses."""
    return "".join(f"\\u{ord(ch):04x}" for ch in value)


def success_body_with_the_key_everywhere(content_as_parts):
    inner_arguments = (
        '{"' + KEY + '": "' + KEY + '", "nested": ["' + escaped(KEY) + '", {"k": "plain ' + KEY + '"}]}'
    )
    content = (
        '[{"type": "text", "text": "part ' + KEY + ' and ' + escaped(KEY) + '"}]'
        if content_as_parts
        else '"plain ' + KEY + ' and escaped ' + escaped(KEY) + '"'
    )
    return (
        '{"id": "id-' + KEY + '", "model": "' + escaped(KEY) + '", "choices": [{"finish_reason": "' + KEY + '",'
        ' "message": {"role": "assistant", "content": ' + content + ', "tool_calls": ['
        '{"id": "call-' + KEY + '", "type": "function", "function": {"name": "tool-' + KEY + '",'
        ' "arguments": ' + json.dumps(inner_arguments) + '}},'
        '{"id": "call-2", "type": "function", "function": {"name": "t", "arguments": {"' + KEY + '": "' + KEY + '"}}}'
        ']}}], "usage": {"prompt_tokens": 1, "' + KEY + '": "' + KEY + '"}}'
    )


@pytest.mark.parametrize("content_as_parts", [False, True], ids=["string-content", "content-parts"])
async def test_no_string_in_a_response_from_a_2xx_body_carries_the_credential(content_as_parts):
    body = success_body_with_the_key_everywhere(content_as_parts)
    assert KEY in json.dumps(json.loads(body), ensure_ascii=False), "the fixture does not carry the key"
    client = http_client(lambda req: httpx.Response(200, content=body.encode(),
                                                    headers={"content-type": "application/json"}))
    response = await client.send(request())
    strings = strings_in(response)
    assert any(REDACTED in s for s in strings), "nothing was redacted, so the walk proves nothing"
    leaked = [s for s in strings if KEY in s]
    assert not leaked, f"{len(leaked)} strings in the ModelResponse still carry the credential"


# --- NFR-12: a run that sets no M9 option sends the payload it sent before M9 -------------------------

ECHO_WIRE = {"type": "function", "function": {"name": "echo", "description": "Echo text back.",
                                               "parameters": ECHO_SCHEMA}}


@pytest.mark.parametrize("named", [True, False], ids=["model-named", "client-default"])
async def test_a_run_setting_no_m9_option_sends_the_payload_it_sent_before_m9(named):
    """The expected payloads were written from the pre-M9 code and pass against it."""
    seen = []
    replies = [
        completion(content=None, finish_reason="tool_calls", tool_calls=[
            {"id": "call-1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]),
        completion(content="done"),
    ]

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=replies[len(seen) - 1])

    runner = Runner({"gw": http_client(handler, model="openai.gpt-4o-mini")}, tools=[echo_tool()])
    agent = AgentSpec(id="golden", instructions="Be brief.", tool_profile=("echo",),
                      preferred_model="gw:openai.gpt-4o-mini" if named else None)
    result = await runner.run(agent, "say hi", RunConfig(tenant_id=TENANT, project_id=PROJECT))
    assert result.status is RunStatus.COMPLETED, result.error

    first = {
        "model": "openai.gpt-4o-mini",
        "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "say hi"}],
        "tools": [ECHO_WIRE],
        "max_tokens": 1024,
    }
    second = {
        **first,
        "messages": first["messages"] + [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "hi"},
        ],
    }
    assert seen == [first, second]


# --- FR-34: the example ------------------------------------------------------------------------------


def test_the_limits_and_cost_example_shows_both_offline_outside_the_repository(tmp_path):
    script = REPO / "scripts" / "09_limits_and_cost.py"
    assert script.is_file(), "scripts/09_limits_and_cost.py does not exist"
    copy = tmp_path / "scripts" / script.name
    copy.parent.mkdir()
    shutil.copy2(script, copy)
    env = {k: v for k, v in os.environ.items()
           if k not in {"BASE_URL", "MODEL_API_KEY", "DATABASE_URL", "DEFAULT_MODEL"}}
    env.update(PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(copy), "--offline"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    lines = proc.stdout.splitlines()
    assert "truncated run: status=failed error=max_tokens" in lines
    # Matched at the start of a line: "priced run: cost_usd=None" is also a
    # substring of the unpriced line, which made the first version of this
    # check fail against a correct script.
    priced = [line for line in lines if line.startswith("priced run: cost_usd=")]
    assert priced and not priced[0].startswith("priced run: cost_usd=None"), priced
    # Round 1 caveat: the example's "illustrative" prices were a real model's
    # list prices, printed beside that model with no label in live mode.
    assert "illustrative" in priced[0], "the priced line does not say its prices are illustrative"
    assert any(line.startswith("unpriced run: cost_usd=None") for line in lines)
    for doc in (REPO / "README.md", REPO / "scripts" / "README.md"):
        assert script.name in doc.read_text(encoding="utf-8"), f"{doc.name} does not list {script.name}"


# =====================================================================================================
# M9 round 2: the round 1 rejection (R1, R2) and its caveats, each tested before the repair.
# =====================================================================================================


# --- R2: a failure anywhere after the model answers loses no billed call ------------------------------


class Faults:
    """Counts every call into every seam a run passes through, and raises at the
    k-th. Sweeping k over a whole run reaches every failure window there is,
    rather than the three windows someone thought to name."""

    def __init__(self, fail_at=0):
        self.fail_at, self.count, self.fired = fail_at, 0, None

    def tick(self, where):
        self.count += 1
        if self.count == self.fail_at:
            self.fired = where
            raise RuntimeError(f"injected failure at {where}")


class FaultySessions:
    def __init__(self, inner, faults):
        self._inner, self._faults = inner, faults

    def append(self, run_id, message):
        self._faults.tick("session.append")
        return self._inner.append(run_id, message)

    def history(self, run_id):
        self._faults.tick("session.history")
        return self._inner.history(run_id)


class FaultySink:
    def __init__(self, inner, faults):
        self._inner, self._faults = inner, faults

    def emit(self, event_type, payload=None, **identifiers):
        self._faults.tick(f"emit {event_type.value}")
        return self._inner.emit(event_type, payload, **identifiers)

    def events(self):
        return self._inner.events()


class FaultyHook(RuntimeHook):
    def __init__(self, faults):
        self._faults = faults

    def before_model(self, request):
        self._faults.tick("before_model")
        return super().before_model(request)

    def after_model(self, response):
        self._faults.tick("after_model")
        return super().after_model(response)

    def before_tool(self, tool_call):
        self._faults.tick("before_tool")
        return super().before_tool(tool_call)

    def after_tool(self, result):
        self._faults.tick("after_tool")
        return super().after_tool(result)


class Billing:
    """The provider's side of the ledger: every response the model returned."""

    def __init__(self, *script):
        self.script, self.billed = list(script), []

    async def send(self, request):
        response = self.script.pop(0) if self.script else text("done", usage=Usage(7, 3, 10))
        self.billed.append(response.usage)
        return response


def billed_script():
    u1 = Usage(1000, 200, 1200, cache_read_tokens=600, cache_write_tokens=100, reasoning_tokens=50)
    u2 = Usage(1500, 100, 1600, cache_read_tokens=1200)
    u3 = Usage(700, 50, 750)
    return [calls(ToolCall(id="f1", name="echo", arguments={"text": "a"}), usage=u1),
            calls(ToolCall(id="f2", name="echo", arguments={"text": "b"}), usage=u2),
            text("done", usage=u3)]


async def run_with_fault(fail_at, persistence):
    faults = Faults(fail_at)
    model = Billing(*billed_script())
    tool = Tool(spec=ToolSpec(name="echo", description="Echo text back.", input_schema=ECHO_SCHEMA),
                fn=lambda text: faults.tick("tool") or text)
    runner = Runner({"gw": model}, tools=[tool], hook=FaultyHook(faults),
                    model_registry=registry(**{"model-a": pricing(**PRICE_A)}))
    recorder = Recorder()
    if persistence is None:
        runner._persistence = SimpleNamespace(
            runs=recorder,
            session_store_for=lambda scope: FaultySessions(InMemorySessionStore(), faults),
            event_sink_for=lambda scope: FaultySink(
                InMemoryEventSink(scope.tenant_id, scope.project_id, scope.run_id), faults),
        )
    else:
        runner._persistence = SimpleNamespace(
            runs=persistence.runs,
            session_store_for=lambda scope: FaultySessions(persistence.session_store_for(scope), faults),
            event_sink_for=lambda scope: FaultySink(persistence.event_sink_for(scope), faults),
        )
    result = await runner.run(
        AgentSpec(id="m9-fault", instructions="i", preferred_model="gw:model-a", tool_profile=("echo",)),
        "go", RunConfig(tenant_id=TENANT, project_id=PROJECT, max_turns=5),
    )
    return result, faults, model, recorder


@pytest.mark.parametrize("persisted", [False, True], ids=["memory", "postgres"])
async def test_a_failure_at_any_point_after_the_model_answers_loses_no_billed_call(persisted, request, written):
    """R2. Round 1 rebuilt a failed run's totals from ModelCalled events written
    after the after_model hook and the session append, so a failure in that
    window dropped billed calls: the reviewer's run was billed 0.036 and reported
    0.006. This injects a failure at every seam a run passes through, one run per
    failure point, and holds the result, the ModelCalled events and the stored
    row to what the provider actually billed."""
    store = request.getfixturevalue("persistence") if persisted else None
    clean, clean_faults, clean_model, _ = await run_with_fault(0, store)
    if persisted:
        written.append(clean.run_id)
    assert clean.status is RunStatus.COMPLETED, clean.error
    assert clean_faults.count >= 20, f"only {clean_faults.count} failure points: the sweep proves little"
    model_pricing = pricing(**PRICE_A)

    fired = []
    for fail_at in range(1, clean_faults.count + 1):
        result, faults, model, recorder = await run_with_fault(fail_at, store)
        if persisted:
            written.append(result.run_id)
        assert faults.fired, f"failure point {fail_at} never fired"
        where = f"failure #{fail_at} at {faults.fired}"
        fired.append(faults.fired)

        billed = sum(model.billed, Usage())
        cost = sum((call_cost(u, model_pricing) for u in model.billed), Decimal(0))
        assert result.usage == billed, f"{where}: reported {result.usage}, billed {billed}"
        assert result.cost_usd == cost, f"{where}: reported {result.cost_usd}, billed {cost}"
        if faults.fired != "emit ModelCalled":
            # Unless the failure WAS that event's write, every answered call has one.
            event_costs = [Decimal(e.payload["cost_usd"]) for e in events_of(result, EventType.MODEL_CALLED)]
            assert len(event_costs) == len(model.billed), f"{where}: a billed call has no ModelCalled event"
            assert sum(event_costs, Decimal(0)) == cost, f"{where}: ModelCalled costs do not add up"
        if persisted:
            row = query("SELECT " + ", ".join(SIX) + ", cost_usd FROM runs WHERE run_id=%s", (result.run_id,))[0]
            assert tuple(row[:6]) == tuple(getattr(billed, name) for name in SIX), f"{where}: stored usage"
            assert row[6] == cost, f"{where}: stored cost {row[6]}, billed {cost}"
        else:
            _, stored_usage, stored_cost = recorder.finished[-1]
            assert (stored_usage, stored_cost) == (billed, cost), f"{where}: what the store was handed"

    # The sweep reached the windows round 1 missed, and the seams around them.
    assert {"after_model", "session.append", "emit ModelCalled", "session.history",
            "before_model", "tool", "emit RunCompleted"} <= set(fired), sorted(set(fired))


class HaltAfterModel(RuntimeHook):
    def __init__(self, on_call):
        self.on_call, self.calls = on_call, 0

    def after_model(self, response):
        self.calls += 1
        if self.calls == self.on_call:
            return HookOutcome(action=HookAction.HALT, reason="halted after the model answered")
        return super().after_model(response)


@pytest.mark.parametrize("persisted", [False, True], ids=["memory", "postgres"])
async def test_a_call_halted_after_the_model_answered_is_counted_in_the_run_its_event_and_the_row(
    persisted, request, written
):
    """R2's second shape: an after_model HALT left the event cost sum at 0.006
    against a run total of 0.036, and a mutant dropping the halted call's cost
    survived all 694 tests, because AC-23 halted only before the model."""
    model = Billing(*billed_script())
    model_pricing = pricing(**PRICE_A)
    runner = Runner({"gw": model}, tools=[echo_tool()], hook=HaltAfterModel(2),
                    model_registry=registry(**{"model-a": model_pricing}),
                    persistence=request.getfixturevalue("persistence") if persisted else None)
    result = await runner.run(
        AgentSpec(id="m9-halt", instructions="i", preferred_model="gw:model-a", tool_profile=("echo",)),
        "go", RunConfig(tenant_id=TENANT, project_id=PROJECT),
    )
    if persisted:
        written.append(result.run_id)
    assert result.status is RunStatus.FAILED and result.error == "halted after the model answered"
    assert len(model.billed) == 2
    billed = sum(model.billed, Usage())
    cost = sum((call_cost(u, model_pricing) for u in model.billed), Decimal(0))
    assert result.usage == billed and result.cost_usd == cost
    event_costs = [Decimal(e.payload["cost_usd"]) for e in events_of(result, EventType.MODEL_CALLED)]
    assert len(event_costs) == 2 and sum(event_costs, Decimal(0)) == cost
    if persisted:
        assert query("SELECT cost_usd FROM runs WHERE run_id=%s", (result.run_id,))[0][0] == cost


# --- R1: no way an error body can fail to arrive changes what its status means -------------------------

READ_TIMEOUT = 0.3
ERROR_JSON = b'{"error": {"message": "slow down"}}'


def response_head(status, headers):
    reason = {400: "Bad Request", 429: "Too Many Requests", 503: "Service Unavailable"}[status]
    lines = [f"HTTP/1.1 {status} {reason}", "Content-Type: application/json", "Connection: close"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def well_formed(writer, status):
    writer.write(response_head(status, {"Content-Length": len(ERROR_JSON)}) + ERROR_JSON)
    await writer.drain()


async def lying_content_length(writer, status):
    writer.write(response_head(status, {"Content-Length": 1000}) + ERROR_JSON)
    await writer.drain()  # and close: the client is still owed 965 bytes


async def truncated_chunked(writer, status):
    writer.write(response_head(status, {"Transfer-Encoding": "chunked"}) + b"40\r\n" + ERROR_JSON[:20])
    await writer.drain()


async def reset_mid_body(writer, status):
    writer.write(response_head(status, {"Content-Length": 1000}) + ERROR_JSON[:10])
    await writer.drain()
    writer.transport.abort()


async def trickling(writer, status):
    # Each byte arrives later than the client's read timeout.
    writer.write(response_head(status, {"Content-Length": len(ERROR_JSON)}))
    await writer.drain()
    for byte in ERROR_JSON:
        await asyncio.sleep(READ_TIMEOUT * 3)
        writer.write(bytes([byte]))
        await writer.drain()


async def slow_but_steady(writer, status):
    # Every byte inside the read timeout, forever: no read ever times out.
    writer.write(response_head(status, {"Transfer-Encoding": "chunked"}))
    await writer.drain()
    while True:
        await asyncio.sleep(READ_TIMEOUT / 4)
        writer.write(b"1\r\nx\r\n")
        await writer.drain()


async def endless(writer, status):
    # As fast as the socket takes it, forever.
    writer.write(response_head(status, {"Transfer-Encoding": "chunked"}))
    chunk = b"x" * 8192
    while True:
        writer.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
        await writer.drain()


async def misdeclared_gzip(writer, status):
    writer.write(response_head(status, {"Content-Encoding": "gzip", "Content-Length": len(ERROR_JSON)}) + ERROR_JSON)
    await writer.drain()


BEHAVIOURS = {
    "well_formed_control": well_formed,
    "lying_content_length": lying_content_length,
    "truncated_chunked": truncated_chunked,
    "reset_mid_body": reset_mid_body,
    "trickling_past_the_read_timeout": trickling,
    "slow_but_steady_forever": slow_but_steady,
    "endless": endless,
    "misdeclared_gzip": misdeclared_gzip,
}


class RawServer:
    """A 127.0.0.1 server that writes raw bytes, so HTTP/1.1 framing faults reach
    the client as a real network delivers them. MockTransport never parses
    framing, which is how 232 tests missed R1."""

    def __init__(self, status, behaviour):
        self.status, self.behaviour, self.connections = status, behaviour, 0
        self._handlers = set()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        for task in list(self._handlers):
            task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await self._server.wait_closed()

    async def _serve(self, reader, writer):
        self.connections += 1
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                           if line.lower().startswith(b"content-length:")), 0)
            await reader.readexactly(length)
            await BEHAVIOURS[self.behaviour](writer, self.status)
        except (OSError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            self._handlers.discard(task)
            writer.close()


@pytest.mark.parametrize("status", [429, 503, 400])
@pytest.mark.parametrize("behaviour", list(BEHAVIOURS))
async def test_no_way_an_error_body_can_fail_to_arrive_changes_what_its_status_means(behaviour, status):
    """R1. Round 1 guarded httpx.DecodingError and nothing else, so a 429 whose
    body was cut short, reset or slow came back as ModelProviderUnavailable or
    ModelTimeout -- and a slow 503 was retried three times. Over a real socket,
    with a well-formed control for each status so a failure cannot be blamed on
    the harness."""
    expected, attempts = {429: (ModelRateLimited, 3), 503: (ModelProviderUnavailable, 1),
                          400: (ModelError, 1)}[status]
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=READ_TIMEOUT))
    extra = {"error_body_timeout": 1.0} if behaviour == "slow_but_steady_forever" else {}
    async with RawServer(status, behaviour) as server:
        client = OpenAICompatibleModelClient(base_url=server.url, api_key=KEY, model="model-a", client=http,
                                             retry=RetryPolicy(attempts=3, backoff_seconds=0), **extra)
        try:
            with pytest.raises(ModelError) as raised:
                # Bounded, so a body that never ends fails this test rather than hanging it.
                await asyncio.wait_for(client.send(request()), timeout=8)
        finally:
            await http.aclose()
    assert raised.type is expected, f"{behaviour} {status}: {raised.type.__name__}: {raised.value}"
    assert server.connections == attempts, f"{behaviour} {status}: {server.connections} attempts"


class RaisingBody(httpx.AsyncByteStream):
    """A body whose source raises something httpx has never heard of."""

    async def __aiter__(self):
        yield b'{"error": '
        raise RuntimeError("the body source broke")


@pytest.mark.parametrize("status", [429, 503])
async def test_an_error_body_that_raises_any_exception_does_not_change_the_classification(status):
    expected, attempts = {429: (ModelRateLimited, 3), 503: (ModelProviderUnavailable, 1)}[status]
    seen = []

    def handler(req):
        seen.append(1)
        return httpx.Response(status, stream=RaisingBody())

    client = http_client(handler, retry=RetryPolicy(attempts=3, backoff_seconds=0))
    with pytest.raises(ModelError) as raised:
        await client.send(request())
    assert raised.type is expected, f"{raised.type.__name__}: {raised.value}"
    assert len(seen) == attempts


# --- round 1 caveats ------------------------------------------------------------------------------------


@pytest.mark.parametrize("price", [Decimal("1E-16384"), Decimal("1E+131001")], ids=str)
def test_a_price_whose_cost_no_numeric_column_could_hold_is_refused_when_built(price):
    """Caveat: a price like 1E-16384 USD per token made RunResult.cost_usd a
    number while the row stored NULL, blurring "unstorable" with "unknown"."""
    with pytest.raises(ValueError, match="output"):
        pricing(output=price)


@pytest.mark.parametrize("price", [Decimal("1E-16383"), Decimal("1E+131000")], ids=["smallest", "largest"])
async def test_at_the_accepted_price_limits_the_row_stores_exactly_the_cost_the_run_reports(
    price, persistence, written
):
    runner = Runner({"gw": Scripted(text("ok", usage=Usage(10, 10, 20)))}, persistence=persistence,
                    model_registry=registry(**{"model-a": pricing(input=price, output=price)}))
    result = await runner.run(AgentSpec(id="m9-limits", instructions="i", preferred_model="gw:model-a"),
                              "go", RunConfig(tenant_id=TENANT, project_id=PROJECT))
    written.append(result.run_id)
    assert result.status is RunStatus.COMPLETED, result.error
    assert result.cost_usd is not None
    assert query("SELECT cost_usd FROM runs WHERE run_id=%s", (result.run_id,))[0][0] == result.cost_usd


def test_a_negative_zero_price_is_stored_as_zero():
    built = pricing(input=Decimal("-0"), output="-0.000")
    assert built.input == 0 and not built.input.is_signed() and not built.output.is_signed()
    assert built.to_json()["input"] == "0" and built.to_json()["output"] == "0"


def test_a_negative_count_in_a_token_class_with_no_price_makes_the_cost_unknown():
    """Caveat: counts were clamped to zero before the no-price check, so a
    negative count in an unpriced class was priced as if absent. FR-30 says a
    non-zero count with no price makes the cost None, and -5 is not zero."""
    no_cache_write = pricing(**{**PRICE_A, "cache_write": None})
    assert call_cost(Usage(1000, 10, 1010, cache_write_tokens=-5), no_cache_write) is None
    assert call_cost(Usage(1000, 10, 1010), no_cache_write) is not None, "the control"


class MoveToModelB(RuntimeHook):
    def before_model(self, request):
        settings = {**request.model_settings, "model": "model-b"}
        return HookOutcome(action=HookAction.MODIFY, replacement=dataclasses.replace(request, model_settings=settings))


async def test_a_hook_that_moves_a_run_onto_a_priced_model_is_costed_by_that_model():
    """Caveat: the zero-call cost of the run's own model (None, when unpriced)
    stuck to the total, so every call priced on the model a hook chose was lost."""
    usage = Usage(1000, 100, 1100)
    runner = Runner({"gw": Scripted(text("ok", usage=usage))}, hook=MoveToModelB(),
                    model_registry=registry(**{"model-a": None, "model-b": pricing(**PRICE_B)}))
    result = await runner.run(AgentSpec(id="m9-move", instructions="i", preferred_model="gw:model-a"),
                              "go", RunConfig(tenant_id=TENANT, project_id=PROJECT))
    assert result.status is RunStatus.COMPLETED, result.error
    assert result.cost_usd == call_cost(usage, pricing(**PRICE_B))


async def test_a_recorder_that_does_not_declare_accounting_is_called_as_before_m9_even_when_wrapped():
    """Caveat: accounting was offered to any finish_run whose signature could
    take it, so a pre-M9 recorder wrapped without functools.wraps -- which looks
    like (*args, **kwargs) -- received it, raised, and turned a completed run
    FAILED with both RunCompleted and RunFailed emitted."""
    statuses = []

    class PreM9Recorder:
        def start_run(self, scope, **fields):
            return None

        def finish_run(self, scope, status):
            statuses.append(status)

    def logged(fn):
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        return wrapper

    recorder = PreM9Recorder()
    recorder.finish_run = logged(recorder.finish_run)
    runner = recording(Runner({"gw": Scripted(text("ok"))}, tools=[echo_tool()]), recorder)
    result = await runner.run(AgentSpec(id="m9-wrapped", instructions="i", preferred_model="gw:model-a"),
                              "go", RunConfig(tenant_id=TENANT, project_id=PROJECT))
    assert result.status is RunStatus.COMPLETED, result.error
    assert statuses == ["completed"]
    terminal = [e.event_type for e in result.events if e.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)]
    assert terminal == [EventType.RUN_COMPLETED]


def test_the_postgres_store_declares_that_it_records_accounting():
    from agentsdk.postgres import PostgresRunStore

    assert PostgresRunStore.records_accounting is True
