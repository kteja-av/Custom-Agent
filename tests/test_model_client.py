"""M3 gate: model contract, adapter, registry, retry (FR-3, FR-12, FR-15, NFR-1).

Everything here runs against httpx.MockTransport. The gate must be computable
with no network and no credentials; the live endpoint is exercised by M6.
"""

import asyncio
import dataclasses
import json

import httpx
import pytest

import agentsdk.providers.openai_compatible as oc_module
from agentsdk.config import REDACTED, Secret, Settings, normalise_database_url
from agentsdk.errors import (
    AgentSDKError,
    ModelError,
    ModelProviderUnavailable,
    ModelRateLimited,
    ModelTimeout,
    ToolValidationError,
)
from agentsdk.executor import ToolExecutor
from agentsdk.model import (
    ModelClient,
    ModelRequest,
    ModelResponse,
    StopReason,
    Usage,
    token_count,
)
from agentsdk.outcomes import Failed
from agentsdk.permissions import AllowlistPermissionChecker
from agentsdk.tools import Tool, ToolRegistry, ToolSpec
from agentsdk.primitives import (
    UNSTORABLE,
    ContentProvenance,
    Message,
    Role,
    ToolCall,
    ToolResult,
)
from agentsdk.providers import OpenAICompatibleModelClient, RetryPolicy
from agentsdk.registry import (
    ModelCapabilities,
    ModelEntry,
    ModelRegistry,
    default_registry,
    provider_of,
)


def completion(content=None, tool_calls=None, finish_reason="stop", usage=None):
    return {
        "id": "chatcmpl-abc123",
        "model": "openai.gpt-4o-mini",
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                },
            }
        ],
        "usage": usage or {"prompt_tokens": 51, "completion_tokens": 15, "total_tokens": 66},
    }


def build(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return OpenAICompatibleModelClient(
        base_url="https://gateway.example/",
        api_key="secret-key-value",
        model="openai.gpt-4o-mini",
        client=http,
        **kwargs,
    )


def simple(body, status=200):
    return lambda request: httpx.Response(status, json=body)


# --- FR-3: request translation ----------------------------------------------


async def test_request_carries_auth_and_hits_chat_completions():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(content="hi"))

    await build(handler).send(ModelRequest(messages=(Message(role=Role.USER, content="hello"),)))

    assert seen["url"] == "https://gateway.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key-value"
    assert seen["body"]["model"] == "openai.gpt-4o-mini"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hello"}]


async def test_instructions_become_a_leading_system_message():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(content="ok"))

    await build(handler).send(
        ModelRequest(
            messages=(Message(role=Role.USER, content="go"),),
            instructions="You are terse.",
        )
    )
    assert seen["body"]["messages"][0] == {"role": "system", "content": "You are terse."}
    assert seen["body"]["messages"][1]["role"] == "user"


async def test_tool_message_fans_out_to_one_wire_message_per_result():
    """A canonical tool Message may hold several results; OpenAI needs one
    message each, keyed by tool_call_id."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(content="done"))

    provenance = ContentProvenance.internal_tool()
    await build(handler).send(
        ModelRequest(
            messages=(
                Message(
                    role=Role.TOOL,
                    tool_results=(
                        ToolResult(tool_call_id="c1", content="one", provenance=provenance),
                        ToolResult(tool_call_id="c2", content="two", provenance=provenance),
                    ),
                ),
            )
        )
    )
    assert seen["body"]["messages"] == [
        {"role": "tool", "tool_call_id": "c1", "content": "one"},
        {"role": "tool", "tool_call_id": "c2", "content": "two"},
    ]


async def test_assistant_tool_calls_are_serialised_with_json_arguments():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(content="ok"))

    await build(handler).send(
        ModelRequest(
            messages=(
                Message(
                    role=Role.ASSISTANT,
                    content=None,
                    tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": "hi"}),),
                ),
            )
        )
    )
    call = seen["body"]["messages"][0]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "echo"
    assert json.loads(call["function"]["arguments"]) == {"text": "hi"}


async def test_tool_schemas_are_forwarded():
    seen = {}
    schema = {"type": "function", "function": {"name": "echo", "parameters": {}}}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(content="ok"))

    await build(handler).send(
        ModelRequest(messages=(Message(role=Role.USER, content="x"),), tools=(schema,))
    )
    assert seen["body"]["tools"] == [schema]


async def test_model_settings_override_the_default_model():
    """Per-request model_settings reach the wire payload.

    Deliberately NOT labelled a proof of NFR-1: asserting that a string
    round-trips into JSON would pass for any string. Cross-provider behaviour is
    proven by AC-9 against the live gateway in M6, and SPEC.md's Risks section
    records that even that proves model-agnosticism, not wire-format
    agnosticism.
    """
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(content="ok"))

    await build(handler).send(
        ModelRequest(
            messages=(Message(role=Role.USER, content="x"),),
            model_settings={"model": "bedrock.anthropic.claude-haiku-4-5", "temperature": 0},
        )
    )
    assert seen["body"]["model"] == "bedrock.anthropic.claude-haiku-4-5"
    assert seen["body"]["temperature"] == 0


def test_adapter_satisfies_the_model_client_protocol():
    """FR-3's central claim. Signature drift should fail here, not in M5."""
    client = OpenAICompatibleModelClient(
        base_url="https://gateway.example/", api_key="k", model="m"
    )
    assert isinstance(client, ModelClient)


# --- FR-3: response translation ---------------------------------------------


async def test_text_response_maps_to_end_turn_with_usage():
    response = await build(simple(completion(content="hello there"))).send(
        ModelRequest(messages=(Message(role=Role.USER, content="hi"),))
    )
    assert isinstance(response, ModelResponse)
    assert response.message.role is Role.ASSISTANT
    assert response.message.content == "hello there"
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage == Usage(51, 15, 66)
    assert response.provider_response_id == "chatcmpl-abc123"
    assert response.tool_calls == ()


async def test_tool_call_response_is_parsed_into_canonical_tool_calls():
    body = completion(
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "call_8BHH",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text":"hello-phase0"}'},
            }
        ],
    )
    response = await build(simple(body)).send(
        ModelRequest(messages=(Message(role=Role.USER, content="call echo"),))
    )
    assert response.stop_reason is StopReason.TOOL_CALLS
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("call_8BHH", "echo", {"text": "hello-phase0"})


async def test_anthropic_style_tool_ids_parse_identically():
    """The same adapter handles bedrock.anthropic responses through the gateway;
    only the id prefix and argument spacing differ."""
    body = completion(
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "tooluse_Lz7LCunE",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "hello-phase0"}'},
            }
        ],
    )
    response = await build(simple(body)).send(
        ModelRequest(messages=(Message(role=Role.USER, content="x"),))
    )
    assert response.tool_calls[0].id == "tooluse_Lz7LCunE"
    assert response.tool_calls[0].arguments == {"text": "hello-phase0"}


async def test_malformed_tool_arguments_are_flagged_not_silently_emptied():
    """Empty arguments are not self-evidently invalid.

    A tool whose schema has no required properties would accept {} and execute,
    so garbled model output must be marked, not merely emptied.
    """
    body = completion(
        finish_reason="tool_calls",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{not json"}}
        ],
    )
    response = await build(simple(body)).send(
        ModelRequest(messages=(Message(role=Role.USER, content="x"),))
    )
    call = response.tool_calls[0]
    assert call.arguments == {}
    assert call.arguments_error is not None


async def test_json_arguments_that_are_not_an_object_are_flagged():
    body = completion(
        finish_reason="tool_calls",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "[1,2]"}}
        ],
    )
    response = await build(simple(body)).send(
        ModelRequest(messages=(Message(role=Role.USER, content="x"),))
    )
    assert response.tool_calls[0].arguments_error is not None


async def test_absent_arguments_are_not_flagged_as_malformed():
    """A tool legitimately called with no arguments must not be rejected."""
    body = completion(
        finish_reason="tool_calls",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "ping", "arguments": ""}}
        ],
    )
    response = await build(simple(body)).send(
        ModelRequest(messages=(Message(role=Role.USER, content="x"),))
    )
    assert response.tool_calls[0].arguments_error is None


async def test_a_flagged_tool_call_is_rejected_before_the_tool_runs():
    """End to end with ToolExecutor: the flag must actually stop execution, even
    for a schema that would happily accept {}."""
    ran = []
    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(
                name="get_status",
                description="No required arguments",
                input_schema={"type": "object", "properties": {}},  # {} is valid here
            ),
            fn=lambda **kw: ran.append(kw) or "ok",
        )
    )
    executor = ToolExecutor(registry, AllowlistPermissionChecker({"get_status"}))

    outcome = await executor.execute(
        ToolCall(id="c1", name="get_status", arguments={}, arguments_error="Expecting value")
    )
    assert isinstance(outcome, Failed)
    assert isinstance(outcome.error, ToolValidationError)
    assert ran == [], "tool executed on undecodable arguments"


@pytest.mark.parametrize(
    "finish_reason,expected",
    [
        ("stop", StopReason.END_TURN),
        ("tool_calls", StopReason.TOOL_CALLS),
        ("length", StopReason.MAX_TOKENS),
        ("content_filter", StopReason.CONTENT_FILTER),
        ("something_new", StopReason.OTHER),
    ],
)
async def test_stop_reason_mapping(finish_reason, expected):
    response = await build(simple(completion(content="x", finish_reason=finish_reason))).send(
        ModelRequest(messages=(Message(role=Role.USER, content="x"),))
    )
    assert response.stop_reason is expected


async def test_empty_choices_is_a_model_error_not_an_index_error():
    with pytest.raises(ModelError, match="no choices"):
        await build(simple({"choices": []})).send(
            ModelRequest(messages=(Message(role=Role.USER, content="x"),))
        )


# --- FR-3: NOTHING escapes this boundary -------------------------------------
# The adapter exists to guarantee that only AgentSDKError subclasses come out of
# send(). AgentLoop cannot classify a JSONDecodeError or an AttributeError, so
# each malformed shape below must arrive as a ModelError.


@pytest.mark.parametrize(
    "name,handler",
    [
        # This deployment sits behind Envoy, which returns non-JSON bodies.
        ("html body", lambda r: httpx.Response(200, text="<html>fault filter abort</html>")),
        ("empty body", lambda r: httpx.Response(200, text="")),
        ("json list", lambda r: httpx.Response(200, json=[1, 2, 3])),
        ("json string", lambda r: httpx.Response(200, json="just a string")),
        ("choices is a string", lambda r: httpx.Response(200, json={"choices": "nope"})),
        ("choice is a string", lambda r: httpx.Response(200, json={"choices": ["nope"]})),
        ("choices missing", lambda r: httpx.Response(200, json={"id": "x"})),
        ("message is a string", lambda r: httpx.Response(200, json={"choices": [{"message": "s"}]})),
        (
            "tool_calls is a string",
            lambda r: httpx.Response(200, json={"choices": [{"message": {"tool_calls": "s"}}]}),
        ),
        (
            "tool_call entry is a string",
            lambda r: httpx.Response(200, json={"choices": [{"message": {"tool_calls": ["s"]}}]}),
        ),
        (
            "usage is a string",
            lambda r: httpx.Response(200, json={"choices": [{"message": {}}], "usage": "lots"}),
        ),
        # dict.get() needs a HASHABLE key: an unchecked finish_reason raises
        # TypeError: unhashable type, which is not an AgentSDKError.
        (
            "finish_reason is a list",
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {}, "finish_reason": ["stop"]}]}
            ),
        ),
        (
            "finish_reason is a dict",
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {}, "finish_reason": {"a": 1}}]}
            ),
        ),
        (
            "finish_reason is a set-like nested list",
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {}, "finish_reason": [["stop"]]}]}
            ),
        ),
        (
            "usage values are strings",
            lambda r: httpx.Response(
                200,
                json={"choices": [{"message": {}}], "usage": {"prompt_tokens": "51"}},
            ),
        ),
        (
            "content is a list of parts",
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {"content": [{"type": "text"}]}}]}
            ),
        ),
        ("id is an int", lambda r: httpx.Response(200, json={"id": 7, "choices": [{"message": {}}]})),
    ],
)
async def test_malformed_success_bodies_never_leak_a_non_sdk_exception(name, handler):
    request = ModelRequest(messages=(Message(role=Role.USER, content="x"),))
    try:
        await build(handler).send(request)
    except AgentSDKError:
        pass  # correct: classifiable by AgentLoop
    except Exception as exc:  # noqa: BLE001 - that is the point of the test
        pytest.fail(f"{name} leaked {type(exc).__module__}.{type(exc).__name__}: {exc}")


@pytest.mark.parametrize("finish_reason", [["stop"], {"a": 1}, 42, None])
async def test_unhashable_or_odd_finish_reason_maps_to_other(finish_reason):
    body = {"choices": [{"message": {"content": "hi"}, "finish_reason": finish_reason}]}
    response = await build(simple(body)).send(
        ModelRequest(messages=(Message(role=Role.USER, content="x"),))
    )
    assert response.stop_reason is StopReason.OTHER


async def test_declared_types_are_not_a_lie():
    """Fields typed int/str feed FR-11's manifest and usage accounting."""
    body = {
        "id": 7,
        "choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": "51", "completion_tokens": None, "total_tokens": 66},
    }
    response = await build(simple(body)).send(
        ModelRequest(messages=(Message(role=Role.USER, content="x"),))
    )
    assert isinstance(response.provider_response_id, str)
    assert isinstance(response.message.content, str)
    for value in (
        response.usage.prompt_tokens,
        response.usage.completion_tokens,
        response.usage.total_tokens,
    ):
        assert isinstance(value, int)
    assert response.usage.prompt_tokens == 51
    assert response.usage.completion_tokens == 0


async def test_infinite_token_count_does_not_leak_overflow_error():
    """json.loads accepts the bare literal Infinity; int(float('inf')) raises
    OverflowError, which is neither TypeError nor ValueError."""
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        for literal in ("Infinity", "-Infinity"):
            body = (
                '{"choices":[{"message":{"content":"hi"},"finish_reason":"stop"}],'
                f'"usage":{{"{field}": {literal}}}}}'
            )
            response = await build(
                lambda r, b=body: httpx.Response(
                    200, content=b.encode(), headers={"content-type": "application/json"}
                )
            ).send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))
            assert getattr(response.usage, field) == 0


async def test_deeply_nested_body_does_not_leak_recursion_error():
    body = ("[" * 6000) + ("]" * 6000)
    with pytest.raises(AgentSDKError):
        await build(
            lambda r: httpx.Response(
                200, content=body.encode(), headers={"content-type": "application/json"}
            )
        ).send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))


async def test_deeply_nested_tool_arguments_become_a_correctable_error():
    nested = ("[" * 6000) + ("]" * 6000)
    body = completion(
        finish_reason="tool_calls",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": nested}}],
    )
    response = await build(simple(body)).send(
        ModelRequest(messages=(Message(role=Role.USER, content="x"),))
    )
    assert response.tool_calls[0].arguments_error is not None


# --- the boundary is TOTAL, not an enumeration -------------------------------


class _Exploding:
    """Stands in for any unforeseen failure inside the adapter."""

    def __init__(self, exc):
        self.exc = exc

    def __call__(self, request):
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        OverflowError("cannot convert float infinity to integer"),
        RecursionError("maximum recursion depth exceeded"),
        MemoryError(),
        AttributeError("'list' object has no attribute 'get'"),
        TypeError("unhashable type: 'list'"),
        KeyError("choices"),
        UnicodeDecodeError("utf-8", b"\x00", 0, 1, "invalid"),
        ZeroDivisionError("division by zero"),
        OSError("socket exploded"),
        RuntimeError("something nobody predicted"),
        Exception("a bare exception"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_no_unforeseen_exception_type_can_escape_send(exc):
    """Three rounds of review each found ONE more exception type escaping.

    Enumerating known failures cannot be complete, so send() wraps anything that
    is not already an AgentSDKError. This test asserts the property, not another
    list of members.
    """
    with pytest.raises(AgentSDKError):
        await build(_Exploding(exc)).send(
            ModelRequest(messages=(Message(role=Role.USER, content="x"),))
        )


# --- the boundary's own error path cannot fail -------------------------------
# Found by an independent reviewer in round 4: str(exc) runs INSIDE the except
# block, so a hostile exception whose rendering raises escapes the very
# mechanism built to make the boundary total.


class _ExplodingStr(Exception):
    def __str__(self):
        raise ValueError("exception __str__ explodes")


class _NonStringStr(Exception):
    def __str__(self):
        return 42  # type: ignore[return-value]


class _BadArg:
    """No custom exception needed: str(Exception(obj)) renders obj."""

    def __str__(self):
        raise ValueError("str() explodes")

    def __repr__(self):
        raise ValueError("repr() explodes too")


class _HostileMeta(type):
    @property
    def __name__(cls):  # noqa: N805
        raise ValueError("even the type name explodes")


class _HostileName(Exception, metaclass=_HostileMeta):
    pass


@pytest.mark.parametrize(
    "exc",
    [
        _ExplodingStr(),
        _NonStringStr(),
        Exception(_BadArg()),
        _HostileName(),
    ],
    ids=["str-raises", "str-returns-int", "arg-str-raises", "type-name-raises"],
)
async def test_an_exception_that_cannot_be_rendered_still_cannot_escape(exc):
    """A boundary whose error path can raise is not a boundary."""
    with pytest.raises(AgentSDKError):
        await build(_Exploding(exc)).send(
            ModelRequest(messages=(Message(role=Role.USER, content="x"),))
        )


async def test_unrenderable_exception_still_yields_a_usable_message_and_cause():
    original = _ExplodingStr()
    with pytest.raises(ModelError) as excinfo:
        await build(_Exploding(original)).send(
            ModelRequest(messages=(Message(role=Role.USER, content="x"),))
        )
    rendered = str(excinfo.value)
    assert "model adapter failed" in rendered
    assert "_ExplodingStr" in rendered, "the type name is still worth reporting"
    assert excinfo.value.__cause__ is original


async def test_hostile_exception_message_is_still_redacted():
    class _LeakyStr(Exception):
        def __str__(self):
            return "boom with secret-key-value inside"

    with pytest.raises(ModelError) as excinfo:
        await build(_Exploding(_LeakyStr())).send(
            ModelRequest(messages=(Message(role=Role.USER, content="x"),))
        )
    assert "secret-key-value" not in str(excinfo.value)


@pytest.mark.parametrize(
    "exc", [KeyboardInterrupt(), SystemExit(), asyncio.CancelledError()],
    ids=["KeyboardInterrupt", "SystemExit", "CancelledError"],
)
async def test_control_flow_exceptions_are_never_swallowed(exc):
    """BaseException is control flow, not a provider fault. Swallowing
    CancelledError would silently break cancellation."""
    with pytest.raises(type(exc)):
        await build(_Exploding(exc)).send(
            ModelRequest(messages=(Message(role=Role.USER, content="x"),))
        )


async def test_wrapped_failure_keeps_the_original_cause_for_debugging():
    with pytest.raises(ModelError) as excinfo:
        await build(_Exploding(ZeroDivisionError("division by zero"))).send(
            ModelRequest(messages=(Message(role=Role.USER, content="x"),))
        )
    assert "ZeroDivisionError" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ZeroDivisionError)


async def test_wrapped_failure_message_is_redacted():
    with pytest.raises(ModelError) as excinfo:
        await build(_Exploding(RuntimeError("boom with secret-key-value inside"))).send(
            ModelRequest(messages=(Message(role=Role.USER, content="x"),))
        )
    assert "secret-key-value" not in str(excinfo.value)


async def test_stream_error_is_wrapped_even_though_it_is_not_an_http_error():
    """httpx.StreamError descends from RuntimeError, not HTTPError."""
    assert not issubclass(httpx.StreamError, httpx.HTTPError)

    def handler(request):
        raise httpx.StreamError("stream went away")

    with pytest.raises(ModelProviderUnavailable):
        await build(handler).send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))


# --- FR-15: retry policy -----------------------------------------------------


async def test_rate_limit_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=completion(content="finally"))

    client = build(handler, retry=RetryPolicy(attempts=3, backoff_seconds=0))
    response = await client.send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))
    assert response.message.content == "finally"
    assert calls["n"] == 3


async def test_rate_limit_gives_up_after_the_configured_attempts():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"message": "nope"}})

    client = build(handler, retry=RetryPolicy(attempts=3, backoff_seconds=0))
    with pytest.raises(ModelRateLimited):
        await client.send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))
    assert calls["n"] == 3, "should be one initial attempt plus two retries"


async def test_timeout_is_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.TimeoutException("timed out")
        return httpx.Response(200, json=completion(content="ok"))

    client = build(handler, retry=RetryPolicy(attempts=3, backoff_seconds=0))
    assert (await client.send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))).message.content == "ok"
    assert calls["n"] == 2


async def test_server_error_is_not_retried():
    """No side effect occurred, but retrying will not help -- propagate at once."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    client = build(handler, retry=RetryPolicy(attempts=3, backoff_seconds=0))
    with pytest.raises(ModelProviderUnavailable):
        await client.send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))
    assert calls["n"] == 1


async def test_default_retry_policy_makes_exactly_three_attempts(monkeypatch):
    """FR-15 pins two numbers, so the DEFAULT policy must be asserted.

    Every other retry test supplies its own RetryPolicy, which leaves the
    production defaults untested: raising `attempts` to 7 or the backoff to ten
    minutes would ship green.
    """
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(oc_module.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    # No retry= argument: this is what production constructs.
    client = build(handler)
    with pytest.raises(ModelRateLimited):
        await client.send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))

    assert calls["n"] == 3, "FR-15 says one initial attempt plus exactly two retries"
    assert delays == [0.5, 1.0], "FR-15 backoff must be exponential from 0.5s"


async def test_default_backoff_actually_grows(monkeypatch):
    """Guards the multiply itself: a flat or removed multiplier must fail here."""
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(oc_module.asyncio, "sleep", fake_sleep)

    def handler(request):
        raise httpx.TimeoutException("timed out")

    client = build(handler, retry=RetryPolicy(attempts=4, backoff_seconds=1.0, multiplier=2.0))
    with pytest.raises(ModelTimeout):
        await client.send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))

    assert delays == [1.0, 2.0, 4.0]
    assert all(b > a for a, b in zip(delays, delays[1:])), "backoff is not increasing"


async def test_default_policy_still_does_not_retry_non_transient(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(oc_module.asyncio, "sleep", fake_sleep)

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"message": "down"}})

    with pytest.raises(ModelProviderUnavailable):
        await build(handler).send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))
    assert calls["n"] == 1
    assert slept == []


async def test_client_error_surfaces_as_model_error_not_a_raw_provider_exception():
    handler = simple({"error": {"message": "Invalid model name passed in"}}, status=400)
    with pytest.raises(ModelError) as excinfo:
        await build(handler).send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))
    assert "Invalid model name" in str(excinfo.value)
    assert not isinstance(excinfo.value, (ModelTimeout, ModelRateLimited))


# --- NFR-4: no credential leakage --------------------------------------------


def test_client_repr_never_contains_the_api_key():
    client = OpenAICompatibleModelClient(
        base_url="https://gateway.example/", api_key="super-secret", model="m"
    )
    assert "super-secret" not in repr(client)


def test_settings_repr_never_contains_the_api_key():
    settings = Settings(base_url="https://x/", api_key="super-secret", database_url="postgresql://u:p@h/d")
    assert "super-secret" not in repr(settings)
    assert "p@h" not in repr(settings)


@pytest.mark.parametrize(
    "render",
    [
        repr,
        str,
        lambda s: f"{s}",
        lambda s: str(dataclasses.asdict(s)),
        lambda s: str(dataclasses.astuple(s)),
        lambda s: str(vars(s)),
        lambda s: str(list(s.__dict__.values())),
    ],
    ids=["repr", "str", "fstring", "asdict", "astuple", "vars", "dict-values"],
)
def test_no_serialisation_path_leaks_the_credential(render):
    """An overridden __repr__ protects repr/str/f-strings only.

    dataclasses.asdict is precisely the idiom that will write FR-11's
    ExecutionManifest row, so redaction has to live on the value.
    """
    settings = Settings(
        base_url="https://x/", api_key="super-secret", database_url="postgresql://u:pw@h/d"
    )
    rendered = render(settings)
    assert "super-secret" not in rendered
    assert "pw@h" not in rendered


@pytest.mark.parametrize(
    "render",
    [
        repr,
        str,
        lambda s: f"{s}",
        lambda s: f"{s!r}",
        lambda s: f"{s!s}",
        lambda s: "%s" % s,
        lambda s: "%r" % s,
        lambda s: "{}".format(s),
        lambda s: "{!s}".format(s),
        lambda s: format(s),
        lambda s: format(s, ">40"),
        lambda s: str([s]),
        lambda s: str({"key": s}),
    ],
    ids=[
        "repr", "str", "fstring", "fstring-r", "fstring-s", "percent-s",
        "percent-r", "format-method", "format-s", "format-builtin",
        "format-spec", "in-list", "in-dict",
    ],
)
def test_secret_itself_redacts_on_every_rendering_path(render):
    """Applied to the Secret directly, not through Settings.

    Routing every case through Settings.__repr__ exercises one dunder seven
    times: Secret.__str__ and Secret.__format__ were never touched, so
    `logging.info("key=%s", secret)` -- the commonest accidental-leak idiom
    there is -- would have shipped green.
    """
    secret = Secret("super-secret")
    assert "super-secret" not in render(secret)
    assert REDACTED in render(secret)


async def test_every_occurrence_of_the_key_is_redacted_not_just_the_first():
    """LiteLLM error envelopes carry provider_specific_fields that can echo the
    request context more than once."""
    def handler(request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": (
                        "rejected Bearer secret-key-value; retried with "
                        "secret-key-value; context secret-key-value"
                    )
                }
            },
        )

    with pytest.raises(ModelError) as excinfo:
        await build(handler).send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))
    assert "secret-key-value" not in str(excinfo.value)
    assert str(excinfo.value).count(REDACTED) == 3


def test_secret_does_not_compare_equal_to_its_raw_string():
    """An == against a bare str must not become a confirmation oracle."""
    secret = Secret("super-secret")
    assert secret != "super-secret"
    assert "super-secret" != secret
    assert secret == Secret("super-secret")
    assert secret != Secret("other")


def test_secret_refuses_to_pickle_rather_than_round_tripping_the_value():
    import pickle

    with pytest.raises(TypeError, match="refuses to be pickled"):
        pickle.dumps(Secret("super-secret"))


def test_secret_survives_logging_interpolation(caplog):
    import logging

    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("key=%s", Secret("super-secret"))
    assert "super-secret" not in caplog.text
    assert REDACTED in caplog.text


def test_secret_still_yields_its_value_to_deliberate_callers():
    settings = Settings(base_url="https://x/", api_key="super-secret", database_url="postgresql://u:pw@h/d")
    assert settings.api_key.reveal() == "super-secret"
    assert settings.dsn == "postgresql://u:pw@h/d"


async def test_provider_error_body_echoing_the_key_is_redacted():
    """A gateway that reflects the Authorization header must not put the key
    into a ModelError message that FR-10 will persist in a RunFailed payload."""
    def handler(request):
        return httpx.Response(
            400,
            json={"error": {"message": "bad request with Bearer secret-key-value attached"}},
        )

    with pytest.raises(ModelError) as excinfo:
        await build(handler).send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))
    assert "secret-key-value" not in str(excinfo.value)
    assert REDACTED in str(excinfo.value)


async def test_non_json_error_body_echoing_the_key_is_redacted():
    def handler(request):
        return httpx.Response(500, text="upstream rejected Bearer secret-key-value")

    with pytest.raises(ModelProviderUnavailable) as excinfo:
        await build(handler).send(ModelRequest(messages=(Message(role=Role.USER, content="x"),)))
    assert "secret-key-value" not in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 500])
async def test_key_straddling_the_truncation_point_leaves_no_fragment(status):
    """Truncate-then-redact leaves a recoverable fragment.

    The key is positioned so the 300-char cut falls through its middle: if the
    body is truncated before `replace` runs, half the credential survives into
    a ModelError message that FR-10 persists.
    """
    key = "secret-key-value"
    for offset in range(290, 302):
        body = ("x" * offset) + key + ("y" * 400)
        exc_type = ModelError if status == 400 else ModelProviderUnavailable
        with pytest.raises(exc_type) as excinfo:
            await build(lambda r, b=body: httpx.Response(status, text=b)).send(
                ModelRequest(messages=(Message(role=Role.USER, content="x"),))
            )
        rendered = str(excinfo.value)
        for size in range(6, len(key) + 1):
            for start in range(0, len(key) - size + 1):
                fragment = key[start : start + size]
                assert fragment not in rendered, (
                    f"offset {offset} leaked {size}-char fragment {fragment!r}"
                )


async def test_key_straddling_truncation_on_the_non_json_2xx_path():
    """Same bug, the other truncation site: the 200-char cut on a 2xx body."""
    key = "secret-key-value"
    for offset in range(192, 204):
        body = ("x" * offset) + key + ("y" * 300)
        with pytest.raises(ModelError) as excinfo:
            await build(lambda r, b=body: httpx.Response(200, text=b)).send(
                ModelRequest(messages=(Message(role=Role.USER, content="x"),))
            )
        assert key not in str(excinfo.value)
        assert key[:10] not in str(excinfo.value)


# --- config ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("postgresql+psycopg://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql://u:p@h/db"),
        (None, None),
    ],
)
def test_database_url_driver_suffix_is_normalised(raw, expected):
    assert normalise_database_url(raw) == expected


def test_settings_from_env_reports_every_missing_variable(monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        Settings.from_env(load_dotfile=False)
    assert "BASE_URL" in str(excinfo.value)
    assert "MODEL_API_KEY" in str(excinfo.value)


# --- FR-12: registry ---------------------------------------------------------


def test_resolve_orders_versions_naturally_not_lexicographically():
    """A string sort puts "10" before "2" and returns the wrong latest."""
    def entry(version):
        return ModelEntry(
            provider="openai",
            model_id="m",
            model_version=version,
            adapter_version="a",
            capabilities=ModelCapabilities(max_context_tokens=1),
        )

    registry = ModelRegistry([entry("2"), entry("10")])
    assert registry.resolve("m").model_version == "10"

    registry = ModelRegistry([entry("1.9.0"), entry("1.10.0")])
    assert registry.resolve("m").model_version == "1.10.0"


def test_registry_round_trip_and_primary_key():
    entry = ModelEntry(
        provider="openai",
        model_id="openai.gpt-4o-mini",
        model_version="2024-07-18",
        adapter_version="openai-compatible/1",
        capabilities=ModelCapabilities(max_context_tokens=128_000),
    )
    registry = ModelRegistry([entry])
    assert registry.get("openai", "openai.gpt-4o-mini", "2024-07-18") is entry
    assert entry.key == ("openai", "openai.gpt-4o-mini", "2024-07-18")
    assert registry.get("openai", "missing", "1") is None


def test_default_registry_holds_both_golden_eval_models():
    registry = default_registry()
    ids = {entry.model_id for entry in registry.entries()}
    assert ids == {"openai.gpt-4o-mini", "bedrock.anthropic.claude-haiku-4-5"}
    assert {entry.provider for entry in registry.entries()} == {"openai", "bedrock"}


@pytest.mark.parametrize(
    "model_id,provider",
    [
        ("bedrock.anthropic.claude-haiku-4-5", "bedrock"),
        ("openai.gpt-4o-mini", "openai"),
        ("vertex_ai.gemini-2.5-flash", "vertex_ai"),
        ("azure.gpt-4.1", "azure"),
        ("mystery-model", "unknown"),
    ],
)
def test_provider_is_derived_from_the_gateway_namespace(model_id, provider):
    assert provider_of(model_id) == provider


# --- Usage coerces, so no call site has to remember (M5 round 2) -------------
#
# Three rejections across three milestones were one defect at three call sites:
# M3 round 3 found int(float('inf')) raising OverflowError in the adapter, and
# M5 round 2 found the same shape in usage reconstruction -- on the total
# boundary's ERROR path, which must not be able to raise. Fixing each site as
# it was found guaranteed a fourth site would be a fresh defect, so the
# coercion moved onto Usage itself. These tests pin the type, not the callers.


class _RaisingInt:
    def __int__(self):
        raise RuntimeError("hostile __int__")


class _RaisingBaseInt:
    def __int__(self):
        raise KeyboardInterrupt("control flow, not a value")


HOSTILE_COUNTS = [
    (float("nan"), 0),
    (float("inf"), 0),
    (float("-inf"), 0),
    ("abc", 0),
    (None, 0),
    ([1, 2], 0),
    ({}, 0),
    (_RaisingInt(), 0),
    (True, 0),          # bool is an int in Python; a flag is not a token count
    ("12", 12),         # a provider sending a numeric string still means 12
    (10.9, 10),
    (7, 7),
]


@pytest.mark.parametrize(
    "value,expected", HOSTILE_COUNTS, ids=lambda v: repr(v)[:22]
)
def test_token_count_is_total(value, expected):
    assert token_count(value) == expected


@pytest.mark.parametrize("value,expected", HOSTILE_COUNTS, ids=lambda v: repr(v)[:22])
def test_usage_cannot_hold_a_value_that_is_not_an_int(value, expected):
    usage = Usage(value, value, value)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (
        expected,
        expected,
        expected,
    )
    assert all(
        type(getattr(usage, name)) is int
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    )


def test_a_coerced_usage_still_adds():
    """__add__ builds a new Usage, so coercion must not break accumulation."""
    assert Usage(float("inf"), "5", None) + Usage(3, 2, 1) == Usage(3, 7, 1)


def test_usage_token_count_lets_base_exception_propagate():
    """INVARIANT-3c123c38. Total means total over Exception, not over
    BaseException: a KeyboardInterrupt is control flow and must not be recorded
    as 0 tokens.

    Named for `usage` and `token_count` deliberately. A round-4 reviewer
    reported this invariant as unpinned after mutating it and seeing the
    usage-related subset stay green -- the mutation does turn the full suite
    red, but the test was called test_base_exception_... and no `-k usage` or
    `-k token_count` selection could find it. A guard nobody can locate is one
    a reviewer reasonably concludes is missing.
    """
    with pytest.raises(KeyboardInterrupt):
        Usage(_RaisingBaseInt(), 0, 0)


async def test_the_adapter_no_longer_coerces_token_counts_itself():
    """The adapter passes the provider's raw values straight to Usage. If a
    future adapter forgets a guard, the type still holds the line."""
    body = (
        '{"choices":[{"message":{"content":"hi"},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens": Infinity, "completion_tokens": "9",'
        ' "total_tokens": null}}'
    )
    client = build(
        lambda r: httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/json"}
        )
    )
    response = await client.send(
        ModelRequest(messages=(Message(role=Role.USER, content="go"),))
    )
    assert response.usage == Usage(0, 9, 0)


def test_model_response_identifiers_cannot_hold_an_unstorable_value():
    """Pinned at the type, not only end to end. `_json_safe` also guards the
    event payload, so an end-to-end test passes with this walk removed -- the
    two layers overlap deliberately, and each has to be pinned where it lives.
    """
    response = ModelResponse(
        message=Message(role=Role.ASSISTANT, content="fine"),
        stop_reason=StopReason.END_TURN,
        provider_response_id="chatcmpl" + chr(0) + "1",
    )
    assert response.provider_response_id == UNSTORABLE


def test_a_clean_provider_response_id_is_untouched():
    response = ModelResponse(
        message=Message(role=Role.ASSISTANT, content="fine"),
        stop_reason=StopReason.END_TURN,
        provider_response_id="chatcmpl-abc123",
    )
    assert response.provider_response_id == "chatcmpl-abc123"
