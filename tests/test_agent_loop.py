"""M4 gate: loop, context assembly and the public API.

Covers FR-1, FR-7, FR-14, NFR-5, NFR-6, AC-8.

The model is a scripted stub, not a mock of our own adapter: the loop must be
provable with no network and no credentials.
"""

import ast
import pathlib

import pytest

from agentsdk import AgentSpec, RunConfig, Runner, RunResult, RunStatus
from agentsdk.context import ContextAssembler
from agentsdk.events import EventType, InMemoryEventSink, RunEvent
from agentsdk.errors import ModelProviderUnavailable, ModelRateLimited, ModelTimeout
from agentsdk.identity import PrincipalContext
from agentsdk.model import ModelClient, ModelResponse, StopReason, Usage
from agentsdk.permissions import AllowlistPermissionChecker
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
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import Tool, ToolRegistry, ToolSpec

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class ScriptedModel:
    """Replays a list of ModelResponses, recording every request it received."""

    def __init__(self, *responses, raises=None):
        self._responses = list(responses)
        self._raises = list(raises or [])
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        if self._raises:
            exc = self._raises.pop(0)
            if exc is not None:
                raise exc
        if not self._responses:
            return text_response("done")
        return self._responses.pop(0)


def text_response(content, usage=Usage(10, 5, 15)):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, content=content),
        stop_reason=StopReason.END_TURN,
        usage=usage,
    )


def tool_response(name="echo", arguments=None, call_id="c1", usage=Usage(10, 5, 15)):
    return ModelResponse(
        message=Message(
            role=Role.ASSISTANT,
            content=None,
            tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments or {"text": "hi"}),),
        ),
        stop_reason=StopReason.TOOL_CALLS,
        usage=usage,
    )


def echo_tool(fn=None):
    return Tool(
        spec=ToolSpec(name="echo", description="Echo text", input_schema=ECHO_SCHEMA),
        fn=fn or (lambda text: text),
    )


def make_runner(model, tools=None, **kwargs):
    return Runner({"gw": model}, tools=tools if tools is not None else [echo_tool()], **kwargs)


def spec(**overrides):
    defaults = dict(
        id="test-agent",
        instructions="You are a test agent.",
        preferred_model="gw:openai.gpt-4o-mini",
        tool_profile=("echo",),
    )
    defaults.update(overrides)
    return AgentSpec(**defaults)


def config(**overrides):
    defaults = dict(tenant_id="t1", project_id="p1", max_turns=5)
    defaults.update(overrides)
    return RunConfig(**defaults)


# --- FR-1: the run reaches a terminal status --------------------------------


async def test_a_text_only_answer_completes_immediately():
    result = await make_runner(ScriptedModel(text_response("the answer"))).run(
        spec(), "what is the answer?", config()
    )
    assert isinstance(result, RunResult)
    assert result.status is RunStatus.COMPLETED
    assert result.succeeded
    assert result.output == "the answer"
    assert result.error is None


async def test_a_tool_using_task_runs_the_tool_then_completes():
    seen = []
    model = ScriptedModel(tool_response(arguments={"text": "hello"}), text_response("I echoed it"))
    result = await make_runner(model, tools=[echo_tool(lambda text: seen.append(text) or text)]).run(
        spec(), "echo hello", config()
    )
    assert result.status is RunStatus.COMPLETED
    assert result.output == "I echoed it"
    assert seen == ["hello"]


async def test_usage_accumulates_across_every_turn():
    model = ScriptedModel(
        tool_response(usage=Usage(10, 5, 15)),
        text_response("done", usage=Usage(20, 7, 27)),
    )
    result = await make_runner(model).run(spec(), "go", config())
    assert result.usage == Usage(30, 12, 42)


async def test_the_tool_result_is_fed_back_to_the_model():
    model = ScriptedModel(tool_response(arguments={"text": "abc"}), text_response("saw it"))
    await make_runner(model).run(spec(), "go", config())

    second = model.requests[1]
    tool_messages = [m for m in second.messages if m.role is Role.TOOL]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_results[0].content == "abc"


async def test_run_id_is_returned_and_unique_per_run():
    runner = make_runner(ScriptedModel(text_response("a"), text_response("b")))
    first = await runner.run(spec(), "one", config())
    second = await runner.run(spec(), "two", config())
    assert first.run_id and second.run_id
    assert first.run_id != second.run_id


# --- FR-14 / AC-8: max_turns is a status, never an exception ----------------


async def test_max_turns_exceeded_is_a_terminal_status_not_a_raise():
    """AC-8. The model keeps asking for tools and never stops."""
    model = ScriptedModel(*[tool_response(call_id=f"c{i}") for i in range(10)])
    result = await make_runner(model).run(spec(), "loop forever", config(max_turns=3))

    assert result.status is RunStatus.MAX_TURNS_EXCEEDED
    assert result.output is None
    assert len(model.requests) == 3, "the loop ran more turns than max_turns allowed"


async def test_max_turns_of_one_still_allows_a_single_answer():
    result = await make_runner(ScriptedModel(text_response("quick"))).run(
        spec(), "go", config(max_turns=1)
    )
    assert result.status is RunStatus.COMPLETED
    assert result.output == "quick"


def test_max_turns_below_one_is_rejected_at_construction():
    with pytest.raises(ValueError, match="max_turns"):
        RunConfig(tenant_id="t", project_id="p", max_turns=0)


# --- a failing tool is a turn outcome, not a run failure ---------------------


async def test_a_denied_tool_does_not_fail_the_run():
    model = ScriptedModel(
        tool_response(name="forbidden", arguments={"text": "x"}), text_response("recovered")
    )
    runner = Runner(
        {"gw": model},
        tools=[
            echo_tool(),
            Tool(
                spec=ToolSpec(name="forbidden", description="no", input_schema=ECHO_SCHEMA),
                fn=lambda text: text,
            ),
        ],
    )
    result = await runner.run(spec(tool_profile=("echo",)), "try it", config())

    assert result.status is RunStatus.COMPLETED
    second = model.requests[1]
    tool_result = [m for m in second.messages if m.role is Role.TOOL][0].tool_results[0]
    assert tool_result.is_error is True
    assert "PermissionDenied" in tool_result.content


async def test_a_raising_tool_does_not_fail_the_run():
    def boom(text):
        raise RuntimeError("tool exploded")

    model = ScriptedModel(tool_response(), text_response("handled"))
    result = await make_runner(model, tools=[echo_tool(boom)]).run(spec(), "go", config())

    assert result.status is RunStatus.COMPLETED
    tool_result = [m for m in model.requests[1].messages if m.role is Role.TOOL][0].tool_results[0]
    assert tool_result.is_error is True
    assert "tool exploded" in tool_result.content


async def test_an_unknown_tool_name_does_not_fail_the_run():
    model = ScriptedModel(tool_response(name="ghost"), text_response("recovered"))
    result = await make_runner(model).run(spec(tool_profile=("echo", "ghost")), "go", config())
    assert result.status is RunStatus.COMPLETED


# --- a model failure DOES fail the run, but never raises ---------------------


async def test_exhausted_model_retries_fail_the_run_without_raising():
    model = ScriptedModel(raises=[ModelTimeout("t"), ModelTimeout("t"), ModelTimeout("t")])
    result = await make_runner(model).run(spec(), "go", config())

    assert result.status is RunStatus.FAILED
    assert result.output is None
    assert "ModelTimeout" in result.error


async def test_the_loop_does_not_retry_it_is_the_clients_job(monkeypatch):
    """FR-15 belongs to the ModelClient, and to exactly one layer.

    The loop used to retry as a "backstop" while the adapter retried too, so a
    single timeout became 3 x 3 = 9 HTTP calls where FR-15 permits 3. Retry
    lives where ModelTimeout and ModelRateLimited are classified. A client that
    declines to retry is making a policy decision the loop must not override.
    """
    model = ScriptedModel(raises=[ModelRateLimited("slow")])
    result = await make_runner(model).run(spec(), "go", config())

    assert result.status is RunStatus.FAILED
    assert len(model.requests) == 1, "the loop retried; retry must live in one layer only"


async def test_a_client_that_retries_internally_is_respected():
    """The client's own retry is invisible to the loop, and must stay that way."""

    class RetryingClient:
        """Retries inside send(), the way the real adapter does."""

        def __init__(self):
            self.attempts = 0
            self.sends = 0

        async def send(self, request):
            self.sends += 1
            for _ in range(3):
                self.attempts += 1
                if self.attempts < 3:
                    continue  # stands in for a transient failure it absorbs
                return text_response("eventually")
            raise ModelTimeout("exhausted")

    client = RetryingClient()
    result = await Runner({"gw": client}, tools=[echo_tool()]).run(spec(), "go", config())

    assert result.status is RunStatus.COMPLETED
    assert result.output == "eventually"
    assert client.sends == 1, "the loop called send once; the retrying is the client's own"
    assert client.attempts == 3


async def test_non_transient_model_errors_fail_immediately():
    model = ScriptedModel(raises=[ModelProviderUnavailable("down")])
    result = await make_runner(model).run(spec(), "go", config())

    assert result.status is RunStatus.FAILED
    assert len(model.requests) == 1


# --- FR-1 is a TOTAL promise, not an enumeration ----------------------------
# An independent reviewer escaped Runner.run() three ways: a hook that raises, a
# malformed tool input_schema, and a session store that raises. FR-1 promises a
# terminal status; a promise kept only for remembered failures is not a promise.


class ExplodingHook(RuntimeHook):
    def __init__(self, where):
        self.where = where

    def before_model(self, request):
        if self.where == "before_model":
            raise RuntimeError("hook exploded")
        return super().before_model(request)

    def after_model(self, response):
        if self.where == "after_model":
            raise RuntimeError("hook exploded")
        return super().after_model(response)

    def before_tool(self, tool_call):
        if self.where == "before_tool":
            raise RuntimeError("hook exploded")
        return super().before_tool(tool_call)

    def after_tool(self, result):
        if self.where == "after_tool":
            raise RuntimeError("hook exploded")
        return super().after_tool(result)


@pytest.mark.parametrize(
    "where", ["before_model", "after_model", "before_tool", "after_tool"]
)
async def test_a_raising_hook_never_escapes_the_runner(where):
    """RuntimeHook is an advertised FR-8 extension point; a third-party hook
    that raises must not crash a run."""
    model = ScriptedModel(tool_response(), text_response("done"))
    result = await make_runner(model, hook=ExplodingHook(where)).run(spec(), "go", config())

    assert isinstance(result, RunResult)
    assert result.status in (RunStatus.FAILED, RunStatus.COMPLETED)
    if result.status is RunStatus.FAILED:
        assert "hook exploded" in result.error


async def test_a_malformed_tool_schema_does_not_crash_the_run():
    """The most reachable of the three: a typo in a ToolSpec's input_schema
    raises jsonschema.SchemaError, not ValidationError."""
    broken = Tool(
        spec=ToolSpec(
            name="echo",
            description="broken schema",
            input_schema={"type": "not-a-real-type"},
        ),
        fn=lambda **kw: "never runs",
    )
    model = ScriptedModel(tool_response(), text_response("recovered"))
    result = await make_runner(model, tools=[broken]).run(spec(), "go", config())

    assert result.status is RunStatus.COMPLETED
    tool_result = [m for m in model.requests[1].messages if m.role is Role.TOOL][0].tool_results[0]
    assert tool_result.is_error is True
    assert "invalid input_schema" in tool_result.content


async def test_a_raising_session_store_fails_the_run_without_escaping():
    class BrokenStore:
        def append(self, run_id, message):
            raise ConnectionError("db down")

        def history(self, run_id):
            return []

    result = await Runner(
        {"gw": ScriptedModel(text_response("hi"))},
        tools=[echo_tool()],
        session_store=BrokenStore(),
    ).run(spec(), "go", config())

    assert result.status is RunStatus.FAILED
    assert "db down" in result.error


async def test_a_raising_assembler_fails_the_run_without_escaping():
    class BrokenAssembler:
        def build(self, *args, **kwargs):
            raise ValueError("assembler exploded")

    result = await Runner(
        {"gw": ScriptedModel(text_response("hi"))},
        tools=[echo_tool()],
        assembler=BrokenAssembler(),
    ).run(spec(), "go", config())

    assert result.status is RunStatus.FAILED
    assert "assembler exploded" in result.error


async def test_a_raising_tool_registry_does_not_escape():
    class BrokenRegistry(ToolRegistry):
        def schemas(self):
            raise RuntimeError("registry exploded")

    result = await Runner(
        {"gw": ScriptedModel(text_response("hi"))}, tool_registry=BrokenRegistry()
    ).run(spec(), "go", config())
    assert result.status is RunStatus.FAILED
    assert "registry exploded" in result.error


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("nobody predicted this"), OverflowError(), MemoryError(), KeyError("k")],
    ids=lambda e: type(e).__name__,
)
async def test_no_unforeseen_exception_type_escapes_the_runner(exc):
    class Exploding:
        async def send(self, request):
            raise exc

    result = await Runner({"gw": Exploding()}, tools=[echo_tool()]).run(spec(), "go", config())
    assert result.status is RunStatus.FAILED
    assert result.error


async def test_a_failed_run_still_returns_its_events():
    class Exploding:
        async def send(self, request):
            raise RuntimeError("boom")

    result = await Runner({"gw": Exploding()}, tools=[echo_tool()]).run(spec(), "go", config())
    assert result.events, "a failed run must still carry the events it produced"
    assert result.events[-1].event_type is EventType.RUN_FAILED


async def test_an_exception_that_cannot_be_rendered_is_still_contained():
    """The boundary's own error path must not be able to raise.

    describe_exception() runs inside every except block in this SDK; a hostile
    __str__ would escape the mechanism built to contain it.
    """

    class ExplodingStr(Exception):
        def __str__(self):
            raise ValueError("__str__ explodes")

    class Exploding:
        async def send(self, request):
            raise ExplodingStr()

    result = await Runner({"gw": Exploding()}, tools=[echo_tool()]).run(spec(), "go", config())
    assert result.status is RunStatus.FAILED
    assert "ExplodingStr" in result.error


async def test_a_tool_raising_an_unrenderable_exception_is_contained():
    class ExplodingStr(Exception):
        def __str__(self):
            raise ValueError("__str__ explodes")

    def boom(text):
        raise ExplodingStr()

    model = ScriptedModel(tool_response(), text_response("handled"))
    result = await make_runner(model, tools=[echo_tool(boom)]).run(spec(), "go", config())
    assert result.status is RunStatus.COMPLETED
    tool_result = [m for m in model.requests[1].messages if m.role is Role.TOOL][0].tool_results[0]
    assert tool_result.is_error is True


async def test_cancellation_is_not_swallowed_by_the_total_boundary():
    """Control flow, not failure. Swallowing this would break cancellation."""
    import asyncio

    class Cancelling:
        async def send(self, request):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await Runner({"gw": Cancelling()}, tools=[echo_tool()]).run(spec(), "go", config())


async def test_configuration_errors_still_raise_loudly():
    """Deliberate exception to the total boundary: a caller bug must surface at
    the call site, not hide inside a RunResult nobody inspects."""
    with pytest.raises(ValueError, match="unknown model client"):
        await make_runner(ScriptedModel()).run(
            spec(preferred_model="nosuch:model"), "go", config()
        )


async def test_a_raising_event_sink_does_not_break_the_failure_path():
    """Telemetry must not be able to fail the thing it observes."""

    class Exploding:
        async def send(self, request):
            raise RuntimeError("model boom")

    def exploding_emit(*args, **kwargs):
        raise RuntimeError("sink boom")

    runner = Runner({"gw": Exploding()}, tools=[echo_tool()])
    result = await runner.run(spec(), "go", config())
    assert result.status is RunStatus.FAILED

    # And directly: an executor whose emit raises still returns an outcome.
    from agentsdk.executor import ToolExecutor

    executor = ToolExecutor(
        registry=ToolRegistry(),
        permission_checker=AllowlistPermissionChecker({"echo"}),
        emit=exploding_emit,
    )
    outcome = await executor.execute(ToolCall(id="c1", name="missing", arguments={}))
    assert outcome.result.is_error is True


# --- FR-7: context assembly --------------------------------------------------


def test_assembler_passes_history_through_and_forwards_schemas():
    history = [Message(role=Role.USER, content="hi")]
    schemas = [{"type": "function", "function": {"name": "echo"}}]
    request = ContextAssembler().build(history, schemas, instructions="be terse")

    assert request.messages == tuple(history)
    assert request.tools == tuple(schemas)
    assert request.instructions == "be terse"
    assert request.output_schema is None, "Phase 0 never populates output_schema"


def test_provenance_travels_as_metadata_not_as_model_readable_text():
    """FR-7's real requirement.

    Provenance in the prompt is content the model can be argued out of. As
    metadata it is a policy input that travels with the request untouched.
    """
    tainted = ContentProvenance(
        origin=Origin.EXTERNAL_TOOL,
        instruction_authority=ContentProvenance.internal_tool().instruction_authority,
        trust_zone=TrustZone.UNTRUSTED,
        taint_flags={TaintFlag.PROMPT_INJECTION_RISK},
    )
    history = [
        Message(role=Role.USER, content="go"),
        Message(
            role=Role.TOOL,
            tool_results=(ToolResult(tool_call_id="c1", content="scraped", provenance=tainted),),
        ),
    ]
    request = ContextAssembler().build(history)

    manifest = request.metadata["provenance"]
    assert manifest[0]["trust_zone"] == "untrusted"
    assert manifest[0]["taint_flags"] == ["prompt_injection_risk"]

    rendered = " ".join(m.content or "" for m in request.messages)
    assert "untrusted" not in rendered
    assert "prompt_injection_risk" not in rendered


def test_assembler_sends_the_whole_history_in_order():
    """Full-history assembly had nothing pinning it.

    An assembler that forwarded only the last message survived the entire
    suite: the model would silently lose its context and the run would degrade
    in quality rather than fail, which is the worst way for this to break.
    """
    history = [
        Message(role=Role.USER, content="first"),
        Message(role=Role.ASSISTANT, content="second"),
        Message(role=Role.USER, content="third"),
    ]
    request = ContextAssembler().build(history)

    assert len(request.messages) == 3, "history was truncated"
    assert [m.content for m in request.messages] == ["first", "second", "third"]


async def test_the_loop_resends_the_growing_history_every_turn():
    """Turn 2 must carry turn 1's exchange, not just the newest message."""
    model = ScriptedModel(tool_response(arguments={"text": "abc"}), text_response("done"))
    await make_runner(model).run(spec(), "the original task", config())

    first, second = model.requests[0], model.requests[1]
    assert len(first.messages) == 1
    assert len(second.messages) == 3, "the loop dropped earlier turns"
    assert [m.role for m in second.messages] == [Role.USER, Role.ASSISTANT, Role.TOOL]
    assert second.messages[0].content == "the original task"


# --- FR-8: hook outcomes beyond "continue" ----------------------------------


class HaltingHook(RuntimeHook):
    def __init__(self, where):
        self.where = where

    def before_model(self, request):
        if self.where == "before_model":
            return HookOutcome(action=HookAction.HALT, reason="halted before model")
        return super().before_model(request)

    def after_model(self, response):
        if self.where == "after_model":
            return HookOutcome(action=HookAction.HALT, reason="halted after model")
        return super().after_model(response)


@pytest.mark.parametrize("where", ["before_model", "after_model"])
async def test_a_hook_can_halt_the_run(where):
    model = ScriptedModel(text_response("should not be returned"))
    result = await make_runner(model, hook=HaltingHook(where)).run(spec(), "go", config())

    assert result.status is RunStatus.FAILED
    assert f"halted {where.replace('_', ' ')}".split()[0] in result.error
    assert result.output is None


async def test_a_hook_can_modify_the_request_before_the_model_sees_it():
    class Rewriting(RuntimeHook):
        def before_model(self, request):
            replacement = ContextAssembler().build(
                [Message(role=Role.USER, content="rewritten by hook")]
            )
            return HookOutcome(action=HookAction.MODIFY, replacement=replacement)

    model = ScriptedModel(text_response("ok"))
    await make_runner(model, hook=Rewriting()).run(spec(), "original", config())
    assert model.requests[0].messages[0].content == "rewritten by hook"


async def test_a_hook_can_modify_the_response_after_the_model_returns():
    class Rewriting(RuntimeHook):
        def after_model(self, response):
            return HookOutcome(
                action=HookAction.MODIFY,
                replacement=ModelResponse(
                    message=Message(role=Role.ASSISTANT, content="replaced by hook"),
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(),
                ),
            )

    result = await make_runner(ScriptedModel(text_response("original")), hook=Rewriting()).run(
        spec(), "go", config()
    )
    assert result.output == "replaced by hook"


async def test_a_hook_can_rewrite_a_tool_call_before_execution():
    seen = []

    class Rewriting(RuntimeHook):
        def before_tool(self, tool_call):
            return HookOutcome(
                action=HookAction.MODIFY,
                replacement=ToolCall(id=tool_call.id, name="echo", arguments={"text": "rewritten"}),
            )

    model = ScriptedModel(tool_response(arguments={"text": "original"}), text_response("done"))
    await make_runner(
        model, tools=[echo_tool(lambda text: seen.append(text) or text)], hook=Rewriting()
    ).run(spec(), "go", config())
    assert seen == ["rewritten"]


def test_assembler_emits_no_provenance_key_when_there_are_no_tool_results():
    request = ContextAssembler().build([Message(role=Role.USER, content="hi")])
    assert "provenance" not in request.metadata


async def test_instructions_from_the_spec_reach_the_request():
    model = ScriptedModel(text_response("ok"))
    await make_runner(model).run(spec(instructions="Be extremely terse."), "go", config())
    assert model.requests[0].instructions == "Be extremely terse."


# --- events ------------------------------------------------------------------


async def test_events_are_ordered_tenant_scoped_and_cover_the_run():
    model = ScriptedModel(tool_response(), text_response("done"))
    result = await make_runner(model).run(spec(), "go", config())

    types = [e.event_type for e in result.events]
    assert types[0] is EventType.RUN_STARTED
    assert types[-1] is EventType.RUN_COMPLETED
    assert EventType.TOOL_CALLED in types
    assert types.count(EventType.MODEL_CALLED) == 2

    assert [e.sequence_no for e in result.events] == list(range(1, len(result.events) + 1))
    for event in result.events:
        assert event.tenant_id == "t1" and event.project_id == "p1"
        assert event.run_id == result.run_id
        assert event.schema_version == 1
        assert event.timestamp.tzinfo is not None


async def test_a_failed_run_emits_run_failed_not_run_completed():
    model = ScriptedModel(raises=[ModelProviderUnavailable("down")])
    result = await make_runner(model).run(spec(), "go", config())
    assert result.events[-1].event_type is EventType.RUN_FAILED
    assert result.events[-1].payload["status"] == "failed"


async def test_max_turns_run_emits_run_failed_with_a_reason():
    model = ScriptedModel(*[tool_response(call_id=f"c{i}") for i in range(5)])
    result = await make_runner(model).run(spec(), "go", config(max_turns=2))
    assert result.events[-1].event_type is EventType.RUN_FAILED
    assert result.events[-1].payload["reason"] == "max_turns_exceeded"


def test_phase2_identifier_slots_exist_but_stay_empty_in_phase0():
    event = InMemoryEventSink("t", "p", "r").emit(EventType.RUN_STARTED, {})
    assert (event.agent_id, event.task_id, event.attempt_id) == (None, None, None)
    assert (event.parent_event_id, event.correlation_id) == (None, None)


# --- ADR-11 / ADR-27 ---------------------------------------------------------


def test_tenant_and_project_are_mandatory():
    with pytest.raises(ValueError, match="tenant_id and project_id"):
        RunConfig(tenant_id="", project_id="p1")


async def test_principal_context_is_recorded_but_never_read():
    model = ScriptedModel(text_response("ok"))
    principal = PrincipalContext(agent_principal="research-analyst-v1", scopes=("read",))
    result = await make_runner(model).run(spec(), "go", config(principal_context=principal))

    recorded = result.events[0].payload["principal_context"]
    assert recorded["agent_principal"] == "research-analyst-v1"
    # It reached no permission decision: the allowlist ignores it entirely.
    assert result.status is RunStatus.COMPLETED


# --- default permission posture ---------------------------------------------


def test_an_empty_tool_profile_denies_everything():
    """An empty allowlist denies; it does not wave everything through.

    A spec that declares no tools and no policy must permit nothing -- the
    failure mode of the opposite default is silent over-permission.
    """
    checker = AgentSpec(id="a", instructions="i").checker()
    assert not checker.check(ToolCall(id="c1", name="echo", arguments={}), None).allowed
    assert not checker.check(ToolCall(id="c2", name="anything", arguments={}), None).allowed


def test_the_tool_profile_is_exactly_the_allowlist():
    checker = AgentSpec(id="a", instructions="i", tool_profile=("echo",)).checker()
    assert checker.check(ToolCall(id="c1", name="echo", arguments={}), None).allowed
    assert not checker.check(ToolCall(id="c2", name="other", arguments={}), None).allowed


def test_an_explicit_policy_overrides_the_profile_default():
    explicit = AllowlistPermissionChecker({"only_this"})
    spec_with_policy = AgentSpec(
        id="a", instructions="i", tool_profile=("echo",), permission_policy=explicit
    )
    assert spec_with_policy.checker() is explicit


async def test_a_spec_with_no_profile_cannot_run_any_tool():
    """End to end: the deny default must actually reach the executor."""
    model = ScriptedModel(tool_response(), text_response("blocked"))
    result = await make_runner(model).run(
        AgentSpec(id="a", instructions="i", preferred_model="gw:m"), "go", config()
    )
    assert result.status is RunStatus.COMPLETED
    tool_result = [m for m in model.requests[1].messages if m.role is Role.TOOL][0].tool_results[0]
    assert tool_result.is_error is True
    assert "PermissionDenied" in tool_result.content


# --- model resolution --------------------------------------------------------


async def test_model_override_beats_the_spec_preference():
    model = ScriptedModel(text_response("ok"))
    await make_runner(model).run(
        spec(), "go", config(model_override="gw:bedrock.anthropic.claude-haiku-4-5")
    )
    assert model.requests[0].model_settings["model"] == "bedrock.anthropic.claude-haiku-4-5"


async def test_an_unknown_client_prefix_is_rejected():
    with pytest.raises(ValueError, match="unknown model client"):
        await make_runner(ScriptedModel()).run(
            spec(preferred_model="nosuch:model"), "go", config()
        )


async def test_a_bare_model_id_works_when_only_one_client_is_registered():
    model = ScriptedModel(text_response("ok"))
    await make_runner(model).run(spec(preferred_model="openai.gpt-4o-mini"), "go", config())
    assert model.requests[0].model_settings["model"] == "openai.gpt-4o-mini"


def test_an_empty_caller_supplied_registry_is_not_silently_replaced():
    """ToolRegistry defines __len__, so an empty one is falsy.

    `tool_registry or ToolRegistry()` therefore discarded a caller's registry
    and substituted a fresh one -- silently, and only when it happened to be
    empty. Collaborator defaults must key on identity, not truthiness.
    """
    supplied = ToolRegistry()
    assert not supplied, "precondition: an empty registry is falsy"
    runner = Runner({"gw": ScriptedModel()}, tool_registry=supplied)
    assert runner._registry is supplied

    supplied.register(echo_tool())
    assert "echo" in runner._registry, "the runner kept a different registry object"


def test_a_falsy_session_store_is_not_silently_replaced():
    """Same trap as the registry: any collaborator may define __len__."""

    class CountingStore(InMemorySessionStore):
        def __len__(self):
            return 0  # always falsy

    store = CountingStore()
    assert not store, "precondition: this store is falsy"
    runner = Runner({"gw": ScriptedModel()}, session_store=store)
    assert runner._sessions is store


async def test_a_falsy_hook_and_assembler_are_not_silently_replaced():
    class FalsyHook(RuntimeHook):
        def __len__(self):
            return 0

    class FalsyAssembler(ContextAssembler):
        def __len__(self):
            return 0

    hook, assembler = FalsyHook(), FalsyAssembler()
    runner = Runner({"gw": ScriptedModel()}, hook=hook, assembler=assembler)
    assert runner._hook is hook
    assert runner._assembler is assembler


def test_a_runner_with_no_model_clients_is_rejected():
    with pytest.raises(ValueError, match="at least one model client"):
        Runner({})


# --- session store -----------------------------------------------------------


async def test_history_is_append_only_and_ordered():
    store = InMemorySessionStore()
    model = ScriptedModel(tool_response(), text_response("done"))
    runner = Runner({"gw": model}, tools=[echo_tool()], session_store=store)
    result = await runner.run(spec(), "the task", config())

    roles = [m.role for m in store.history(result.run_id)]
    assert roles == [Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]
    assert store.history(result.run_id)[0].content == "the task"


def test_returned_history_cannot_mutate_the_store():
    store = InMemorySessionStore()
    store.append("r1", Message(role=Role.USER, content="one"))
    store.history("r1").append(Message(role=Role.USER, content="injected"))
    assert len(store.history("r1")) == 1


def test_runs_are_isolated_from_each_other():
    store = InMemorySessionStore()
    store.append("r1", Message(role=Role.USER, content="one"))
    store.append("r2", Message(role=Role.USER, content="two"))
    assert len(store.history("r1")) == 1
    assert store.history("r2")[0].content == "two"


# --- NFR-5: the public API boundary -----------------------------------------


def test_the_public_surface_is_importable_from_the_package_root():
    import agentsdk

    for name in ("AgentSpec", "RunConfig", "Runner", "RunResult"):
        assert name in agentsdk.__all__
        assert hasattr(agentsdk, name)


def test_internal_collaborators_are_not_exported_from_the_root():
    """A caller reaching for AgentLoop or ToolExecutor has left the API."""
    import agentsdk

    for internal in ("AgentLoop", "ToolExecutor", "ContextAssembler", "InMemorySessionStore"):
        assert internal not in agentsdk.__all__


async def test_a_whole_run_needs_only_the_public_names():
    """The golden eval in M6 must be writable with these four names alone."""
    import agentsdk

    Spec = agentsdk.AgentSpec
    Config = agentsdk.RunConfig
    result = await Runner({"gw": ScriptedModel(text_response("hi"))}, tools=[echo_tool()]).run(
        Spec(id="a", instructions="i", tool_profile=("echo",)),
        "task",
        Config(tenant_id="t", project_id="p"),
    )
    assert isinstance(result, agentsdk.RunResult)
    assert result.status is agentsdk.RunStatus.COMPLETED


# --- NFR-6: no vendor agent SDK anywhere in the package ---------------------

BANNED = {"langgraph", "langchain", "anthropic", "openai", "claude_agent_sdk", "agents"}


def test_no_module_imports_a_vendor_agent_sdk():
    """Static, not import-time: a lazily imported vendor SDK would still be a
    runtime dependency, and NFR-6 says the package must run without any."""
    package = pathlib.Path(__file__).resolve().parent.parent / "agentsdk"
    offenders = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in BANNED:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert not offenders, offenders


@pytest.mark.parametrize(
    "hostile",
    [float("nan"), float("inf"), float("-inf"), "abc", None, [1, 2]],
    ids=lambda v: repr(v)[:12],
)
async def test_misreported_usage_cannot_break_the_boundary_error_path(hostile):
    """M5 round-2 defect: a ModelClient that misreports usage took down
    Runner.run() from inside its own except block.

    The model must be called twice here -- the reconstruction only runs once a
    ModelCalled event exists, so a single-turn probe would pass while proving
    nothing.
    """

    class ToolThenExplode:
        def __init__(self):
            self.calls = 0

        async def send(self, request):
            self.calls += 1
            if self.calls == 1:
                return tool_response(usage=Usage(hostile, hostile, hostile))
            raise RuntimeError("exploded after reporting nonsense usage")

    client = ToolThenExplode()
    result = await Runner({"gw": client}, tools=[echo_tool()]).run(spec(), "go", config())

    assert client.calls == 2, "the error path was never reached"
    assert result.status is RunStatus.FAILED
    assert "exploded after reporting nonsense usage" in result.error
    assert result.usage == Usage(0, 0, 0)


async def test_reconstructed_usage_still_reports_what_was_actually_spent():
    """The guard must not turn every reconstructed usage into zero -- that was
    the M4 limitation this reconstruction exists to fix."""

    class ToolThenExplode:
        def __init__(self):
            self.calls = 0

        async def send(self, request):
            self.calls += 1
            if self.calls == 1:
                return tool_response(usage=Usage(11, 4, 15))
            raise RuntimeError("boom")

    result = await Runner({"gw": ToolThenExplode()}, tools=[echo_tool()]).run(
        spec(), "go", config()
    )
    assert result.status is RunStatus.FAILED
    assert result.usage == Usage(11, 4, 15)
