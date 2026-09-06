"""M2 gate: tool layer and the nine-step lifecycle (FR-4, FR-5, FR-6, FR-8)."""

import asyncio

import pytest

from agentsdk.errors import (
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolPermissionDenied,
    ToolTimeout,
    ToolValidationError,
)
from agentsdk.executor import ToolExecutor
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.identity import PrincipalContext
from agentsdk.outcomes import Completed, Failed
from agentsdk.permissions import (
    AllowlistPermissionChecker,
    Decision,
    DenyAllPermissionChecker,
)
from agentsdk.primitives import ContentProvenance, Origin, ToolCall
from agentsdk.tools import ApprovalPolicy, RiskClass, Tool, ToolRegistry, ToolSpec

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def echo_spec(**overrides):
    defaults = dict(
        name="echo",
        description="Echo text back unchanged",
        input_schema=ECHO_SCHEMA,
        risk_class=RiskClass.READ_ONLY,
    )
    defaults.update(overrides)
    return ToolSpec(**defaults)


class SpyTool:
    """Records whether the implementation was actually invoked."""

    def __init__(self, fn=None):
        self.calls = []
        self._fn = fn or (lambda text: text)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self._fn(**kwargs)


def build(allowed={"echo"}, tool_fn=None, spec=None, hook=None, events=None):
    registry = ToolRegistry()
    spy = SpyTool(tool_fn)
    registry.register(Tool(spec=spec or echo_spec(), fn=spy))
    emit = None
    if events is not None:
        emit = lambda event_type, payload: events.append((event_type, payload))
    executor = ToolExecutor(
        registry=registry,
        permission_checker=AllowlistPermissionChecker(allowed),
        hook=hook,
        emit=emit,
    )
    return executor, spy


# --- FR-4: registry ----------------------------------------------------------


def test_duplicate_registration_raises_at_registration_time():
    registry = ToolRegistry()
    registry.register(Tool(spec=echo_spec(), fn=lambda text: text))
    with pytest.raises(ToolError, match="already registered"):
        registry.register(Tool(spec=echo_spec(), fn=lambda text: text))


def test_unknown_tool_lookup_raises_tool_not_found():
    with pytest.raises(ToolNotFound):
        ToolRegistry().get("nope")


def test_registry_emits_openai_compatible_schemas():
    registry = ToolRegistry()
    registry.register(Tool(spec=echo_spec(), fn=lambda text: text))
    schema = registry.schemas()[0]
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["parameters"] == ECHO_SCHEMA


def test_spec_hash_is_stable_and_sensitive():
    assert echo_spec().schema_hash() == echo_spec().schema_hash()
    assert echo_spec().schema_hash() != echo_spec(description="different").schema_hash()


# --- FR-6: permission checker ------------------------------------------------


def test_allowlist_ignores_principal_context_in_phase0():
    checker = AllowlistPermissionChecker({"echo"})
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    with_principal = checker.check(call, PrincipalContext(agent_principal="a"))
    without = checker.check(call, None)
    assert with_principal.decision is Decision.ALLOW
    assert without.decision is Decision.ALLOW


def test_allowlist_denies_unknown_tool_with_a_reason():
    result = AllowlistPermissionChecker({"echo"}).check(
        ToolCall(id="c1", name="rm_rf", arguments={}), None
    )
    assert result.decision is Decision.DENY
    assert "rm_rf" in result.reason


# --- FR-5: the nine steps, in order ------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_completes_with_provenance():
    executor, spy = build()
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments={"text": "hello"})
    )
    assert isinstance(outcome, Completed)
    assert outcome.result.content == "hello"
    assert outcome.result.is_error is False
    assert isinstance(outcome.result.provenance, ContentProvenance)
    assert outcome.result.provenance.origin is Origin.INTERNAL_TOOL
    assert spy.calls == [{"text": "hello"}]


@pytest.mark.asyncio
async def test_step1_unresolved_tool_fails_without_touching_anything():
    executor, spy = build()
    outcome = await executor.execute(ToolCall(id="c1", name="ghost", arguments={}))
    assert isinstance(outcome, Failed)
    assert isinstance(outcome.error, ToolNotFound)
    assert spy.calls == []


@pytest.mark.asyncio
async def test_step2_validation_failure_never_reaches_the_tool():
    """THE ordering invariant: bad arguments stop before execution."""
    executor, spy = build()
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments={"text": 12345})
    )
    assert isinstance(outcome, Failed)
    assert isinstance(outcome.error, ToolValidationError)
    assert spy.calls == [], "tool ran despite invalid arguments"


@pytest.mark.asyncio
async def test_step2_missing_required_argument_is_a_validation_error():
    executor, spy = build()
    outcome = await executor.execute(ToolCall(id="c1", name="echo", arguments={}))
    assert isinstance(outcome.error, ToolValidationError)
    assert spy.calls == []


@pytest.mark.asyncio
async def test_step2_precedes_step3_when_both_would_fail():
    """A call that is both invalid AND denied must report validation.

    If permission were checked first this would come back PermissionDenied,
    which is how an ordering regression would show itself.
    """
    registry = ToolRegistry()
    spy = SpyTool()
    registry.register(Tool(spec=echo_spec(), fn=spy))
    executor = ToolExecutor(registry, DenyAllPermissionChecker())
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments={"text": 999})
    )
    assert isinstance(outcome.error, ToolValidationError)
    assert spy.calls == []


@pytest.mark.asyncio
async def test_step3_denied_call_never_reaches_the_tool():
    executor, spy = build(allowed=set())
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    )
    assert isinstance(outcome, Failed)
    assert isinstance(outcome.error, ToolPermissionDenied)
    assert spy.calls == [], "denied tool was executed"


@pytest.mark.asyncio
async def test_step6_tool_exception_becomes_execution_error():
    def boom(text):
        raise RuntimeError("tool blew up")

    executor, _ = build(tool_fn=boom)
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    )
    assert isinstance(outcome.error, ToolExecutionError)
    assert "tool blew up" in str(outcome.error)


@pytest.mark.asyncio
async def test_step6_timeout_becomes_tool_timeout():
    async def slow(text):
        await asyncio.sleep(5)
        return text

    executor, _ = build(tool_fn=slow, spec=echo_spec(timeout_seconds=0.05))
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    )
    assert isinstance(outcome.error, ToolTimeout)


@pytest.mark.asyncio
async def test_async_tools_are_supported():
    async def async_echo(text):
        await asyncio.sleep(0)
        return text.upper()

    executor, _ = build(tool_fn=async_echo)
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    )
    assert isinstance(outcome, Completed)
    assert outcome.result.content == "HI"


# --- every failure is still a tool result the model can see ------------------


@pytest.mark.parametrize(
    "allowed,arguments",
    [(set(), {"text": "hi"}), ({"echo"}, {"text": 1})],
)
@pytest.mark.asyncio
async def test_failures_carry_an_error_result_with_provenance(allowed, arguments):
    executor, _ = build(allowed=allowed)
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments=arguments)
    )
    assert isinstance(outcome, Failed)
    assert outcome.result.is_error is True
    assert outcome.result.tool_call_id == "c1"
    assert isinstance(outcome.result.provenance, ContentProvenance)


# --- FR-8: hooks -------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_hook_does_not_interfere():
    executor, spy = build(hook=RuntimeHook())
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    )
    assert isinstance(outcome, Completed)
    assert spy.calls == [{"text": "hi"}]


@pytest.mark.asyncio
async def test_before_tool_hook_can_reject():
    class Rejecting(RuntimeHook):
        def before_tool(self, tool_call):
            return HookOutcome(action=HookAction.REJECT, reason="blocked by policy")

    executor, spy = build(hook=Rejecting())
    outcome = await executor.execute(
        ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    )
    assert isinstance(outcome, Failed)
    assert "blocked by policy" in str(outcome.error)
    assert spy.calls == []


def test_all_four_hook_points_default_to_continue():
    hook = RuntimeHook()
    for outcome in (
        hook.before_model(None),
        hook.after_model(None),
        hook.before_tool(None),
        hook.after_tool(None),
    ):
        assert outcome.is_continue


# --- step 9: emission --------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_called_event_is_emitted_on_success_and_failure():
    events = []
    executor, _ = build(events=events)
    await executor.execute(ToolCall(id="c1", name="echo", arguments={"text": "hi"}))
    await executor.execute(ToolCall(id="c2", name="echo", arguments={"text": 1}))
    assert [e[0] for e in events] == ["ToolCalled", "ToolCalled"]
    assert events[0][1]["is_error"] is False
    assert events[1][1]["is_error"] is True


# --- ADR-27 seam -------------------------------------------------------------


def test_principal_context_serialises_without_secrets():
    ctx = PrincipalContext(agent_principal="research-analyst-v1", scopes=["read"])
    payload = ctx.to_json()
    assert payload["agent_principal"] == "research-analyst-v1"
    assert payload["scopes"] == ["read"]
    assert not any("key" in k or "token" in k for k in payload)
