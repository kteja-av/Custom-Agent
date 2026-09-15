"""M14 gate: timings and a minimal OpenTelemetry exporter (FR-57..FR-60, NFR-15, NFR-19, AC-45..AC-47).

Written before the implementation, against the specification as amended at the M14
pre-flight on 2026-09-15 (DECISION-ea07ecbd): RunStarted records parent_run_id, and
ModelCalled records the provider name a client declares. Names M14 adds are reached
at call time, so before the implementation each test fails on its own.

Timings are asserted as properties, never as stopwatch readings of a consequence
(KNOWLEDGE-3afb9c70): a lower bound where a scripted client or tool is made to take
at least that long by perf_counter, and exact agreement between an event's fields and
the span built from them.

FR-58 and FR-60 leave the exporter's methods to the implementation, so the surface
these tests hold it to is named here:
  * OpenTelemetryExporter(tracer_provider, *, max_queue_size=...) builds its queue and
    its worker thread and nothing else on the calling thread;
  * export(run) takes a finished RunResult, or a RunHandle whose events it follows
    from the running event loop, and returns at once;
  * flush(timeout) returns True once every event handed to it has been turned into
    spans; it blocks, so these tests call it through asyncio.to_thread;
  * shutdown(timeout) stops the worker; dropped and errors are ints.
FR-58's "linked" is read as an OpenTelemetry span link, not a parent: a child run's
spans stay in a trace of their own, which a link to the parent's invoke_agent span joins.

The persisted half needs DATABASE_URL and fails rather than skips without it; every row
written is removed, and no assertion prints a credential.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import importlib
import os
import pathlib
import re
import socket
import subprocess
import sys
import textwrap
import threading
import time
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import psycopg
import pytest
from dotenv import dotenv_values, load_dotenv
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentsdk import AgentSpec, Persistence, RunConfig, Runner, RunResult, RunStatus
from agentsdk.config import normalise_database_url
from agentsdk.errors import ModelProviderUnavailable
from agentsdk.events import EventType, RunEvent
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry, call_cost
from agentsdk.scheduler import SchedulerLimits
from agentsdk.tools import Tool, ToolSpec

load_dotenv()

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
REPO = pathlib.Path(__file__).resolve().parents[1]
TENANT, PROJECT = "SYN-m14", "p-m14"
SEMCONV = "1.40.0"
TIMINGS = ("started_at", "duration_ms", "queued_ms")
TERMINAL = ("RunCompleted", "RunFailed", "RunCancelled")
KEY_SCHEMA = {
    "type": "object",
    "properties": {"key": {"type": "string"}},
    "required": ["key"],
    "additionalProperties": False,
}
# prompt, completion, total, cache read, cache write: every count FR-59 exports is non-zero.
CALL_USAGE = Usage(10, 5, 15, 3, 2)
OTEL_PINS = {
    "opentelemetry-api==1.44.0",
    "opentelemetry-sdk==1.44.0",
    "opentelemetry-exporter-otlp-proto-http==1.44.0",
}

# NFR-15: the payload keys each event carried before M14, and what FR-57 adds to them.
BEFORE_M14 = {
    "RunStarted": {"agent_spec_id", "model", "provider", "max_turns", "principal_context"},
    "ModelCalled": {"turn", "stop_reason", "tool_calls", "usage", "cost_usd", "provider_response_id"},
    "ToolCalled": {"tool_call_id", "name", "is_error", "error_type", "original_length", "truncated"},
    "RunCompleted": {"status", "turns", "reason"},
    "RunFailed": {"status", "turns", "reason"},
    "RunCancelled": {"status", "turns", "reason"},
}
ADDED_BY_M14 = {
    "RunStarted": {"parent_run_id"},
    "ModelCalled": {"started_at", "duration_ms", "queued_ms", "model", "provider", "provider_name"},
    "ToolCalled": {"started_at", "duration_ms", "queued_ms"},
    "RunCompleted": {"started_at", "duration_ms"},
    "RunFailed": {"started_at", "duration_ms"},
    "RunCancelled": {"started_at", "duration_ms"},
}


# --- shared helpers --------------------------------------------------------------------------------


def telemetry():
    return importlib.import_module("agentsdk.telemetry")


def text(content="done", stop=StopReason.END_TURN):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content), stop_reason=stop, usage=CALL_USAGE)


def calls(*tool_calls):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, tool_calls=tuple(tool_calls)),
        stop_reason=StopReason.TOOL_CALLS,
        usage=CALL_USAGE,
    )


def call(name, key, call_id=None):
    return ToolCall(id=call_id or key, name=name, arguments={"key": key})


class Script:
    """A scripted model client: replays responses in order, raises an exception item, and
    awaits `before` ahead of each answer. It declares neither a default model nor a name."""

    def __init__(self, *items, before=None):
        self.items = list(items)
        self.before = before
        self.received = []

    async def send(self, request):
        self.received.append(request)
        if self.before is not None:
            await self.before()
        item = self.items.pop(0) if self.items else text()
        if isinstance(item, BaseException):
            raise item
        return item


class Declaring(Script):
    provider_name = "acme"


class DefaultModel(Script):
    default_model_id = "priced"


class RaisingName(Script):
    @property
    def provider_name(self):
        raise RuntimeError("the name cannot be read")


class UnstorableName(Script):
    provider_name = "ac\x00me"


class NotTextName(Script):
    provider_name = 7


class CountedName(Script):
    def __init__(self, *items, **kwargs):
        super().__init__(*items, **kwargs)
        self.reads = 0

    @property
    def provider_name(self):
        self.reads += 1
        return "counted"


async def take_at_least(seconds):
    started = time.perf_counter()
    while time.perf_counter() - started < seconds:
        await asyncio.sleep(seconds / 5)


async def until(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


def tool(name, fn, **spec):
    return Tool(spec=ToolSpec(name=name, description=name, input_schema=KEY_SCHEMA, **spec), fn=fn)


async def lookup(key):
    return f"value of {key}"


def registry():
    # Every token class CALL_USAGE reports is priced: a count with no price makes the cost
    # unknown (FR-30, NFR-11), which would leave nothing for agentsdk.cost_usd to carry.
    pricing = ModelPricing(input="0.000001", output="0.000002", cache_read="0.0000005", cache_write="0.00000125")
    return ModelRegistry([
        ModelEntry(
            provider="test",
            model_id="priced",
            model_version="1",
            adapter_version="test/1",
            capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=pricing),
        )
    ])


def agent(*profile, **fields):
    fields.setdefault("preferred_model", "m:priced")
    fields.setdefault("instructions", "go")
    return AgentSpec(id="m14", tool_profile=tuple(profile), **fields)


def config(**fields):
    return RunConfig(tenant_id=TENANT, project_id=PROJECT, **fields)


def make_runner(client, persistence, **kwargs):
    return Runner({"m": client}, persistence=persistence, model_registry=registry(), **kwargs)


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def drop_runs():
    with psycopg.connect(DSN, autocommit=True) as conn:
        ids = [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE tenant_id=%s", (TENANT,)).fetchall()]
        for table in ("run_events", "messages", "execution_manifests"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        drop_runs()


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    if request.param == "postgres":
        assert DSN, "the persisted half needs DATABASE_URL and fails rather than skips"
        return Persistence.postgres(DSN)
    return None


def recorded(persistence, result):
    """(event_type, payload) in sequence order, read back from the store that recorded them."""
    if persistence is None:
        return [(event.event_type.value, dict(event.payload)) for event in result.events]
    rows = query("SELECT event_type, payload FROM run_events WHERE run_id = %s ORDER BY sequence_no", (result.run_id,))
    return [(event_type, payload) for event_type, payload in rows]


def payloads(events, kind):
    return [payload for event_type, payload in events if event_type == kind]


def utc(value, where):
    assert isinstance(value, str), f"{where}: started_at {value!r} is not ISO-8601 text"
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0), f"{where}: started_at {value!r} is not UTC"
    return parsed


def measure(value, where, name):
    assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{where}: {name} is {value!r}"
    assert value >= 0, f"{where}: {name} is {value!r}"
    return value


def epoch_ns(started_at):
    moment = datetime.fromisoformat(started_at)
    return (moment - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(microseconds=1) * 1000


# --- spans -----------------------------------------------------------------------------------------


def provider_with(*processors):
    provider = TracerProvider(shutdown_on_exit=False)
    for processor in processors:
        provider.add_span_processor(processor)
    return provider


def in_memory():
    memory = InMemorySpanExporter()
    return provider_with(SimpleSpanProcessor(memory)), memory


async def flushed(exporter, timeout=15):
    assert await asyncio.to_thread(exporter.flush, timeout), "the exporter did not turn its events into spans in time"


async def shut(exporter):
    await asyncio.to_thread(exporter.shutdown, 5)


def by_operation(spans, operation):
    return [span for span in spans if span.attributes.get("gen_ai.operation.name") == operation]


def assert_timed(span, payload):
    start = epoch_ns(payload["started_at"])
    assert abs(span.start_time - start) <= 1_000, (span.name, span.start_time, start)
    length = span.end_time - span.start_time
    assert abs(length - payload["duration_ms"] * 1_000_000) <= 2_000, (span.name, length, payload["duration_ms"])


class Blocking(SpanExporter):
    """Holds the thread that exports until the test releases it."""

    def __init__(self):
        self.entered, self.release, self.threads = threading.Event(), threading.Event(), []

    def export(self, spans):
        self.threads.append(threading.get_ident())
        self.entered.set()
        self.release.wait(30)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.release.set()


class RaisingExporter(SpanExporter):
    def export(self, spans):
        raise RuntimeError("the exporter broke")

    def shutdown(self):
        pass


class Recording(SpanExporter):
    def __init__(self):
        self.threads = []

    def export(self, spans):
        self.threads.append(threading.get_ident())
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class ThreadSpy(SpanProcessor):
    """Records the thread every span is started and ended on."""

    def __init__(self):
        self.threads = []

    def on_start(self, span, parent_context=None):
        self.threads.append(("start", threading.get_ident()))

    def on_end(self, span):
        self.threads.append(("end", threading.get_ident()))

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class HoldsTheFirstStart(SpanProcessor):
    """Holds the worker inside the first span it starts."""

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()

    def on_start(self, span, parent_context=None):
        if not self.entered.is_set():
            self.entered.set()
            self.release.wait(30)

    def on_end(self, span):
        pass

    def shutdown(self):
        self.release.set()

    def force_flush(self, timeout_millis=30000):
        return True


class RaisingOnEnd(SpanProcessor):
    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        raise RuntimeError("the span processor broke")

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class RaisingProvider:
    def get_tracer(self, *args, **kwargs):
        raise RuntimeError("the tracer provider broke")


def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# A run with two model calls and one parallel batch of three tool calls: one succeeds,
# one fails, one is truncated. Every piece of content carries a marker FR-59 must not export.
MARKERS = ("TASKMARKER", "INSTRUCTIONMARKER", "ARGMARKER", "RESULTMARKER", "ANSWERMARKER")


def rich_runner(persistence=None, client_class=Script, secrets=("", ""), **kwargs):
    async def fetchish(key):
        if key.endswith("boom"):
            raise RuntimeError(f"RESULTMARKER the tool failed {secrets[0]}")
        if key.endswith("long"):
            return "RESULTMARKER " * 10
        return f"RESULTMARKER {secrets[1]}"

    client = client_class(
        calls(
            ToolCall(id="c1", name="fetchish", arguments={"key": "ARGMARKER-ok"}),
            ToolCall(id="c2", name="fetchish", arguments={"key": "ARGMARKER-boom"}),
            ToolCall(id="c3", name="fetchish", arguments={"key": "ARGMARKER-long"}),
        ),
        text(f"ANSWERMARKER {secrets[1]}"),
    )
    tools = [tool("fetchish", fetchish, concurrency_safe=True, max_output_chars=40)]
    return make_runner(client, persistence, tools=tools, **kwargs)


def rich_agent():
    return agent("fetchish", instructions="INSTRUCTIONMARKER")


# =================================================================================================
# FR-57, NFR-15, AC-45: timings on every event
# =================================================================================================


ENDINGS = [
    "completed",
    "failed by the model",
    "failed at max_tokens",
    "failed by a raising hook",
    "max turns exceeded",
    "a denied call",
    "cancelled during a tool call",
]


async def run_ending(ending, persistence):
    if ending == "completed":
        runner = make_runner(Script(calls(call("lookup", "a")), text("answer")), persistence, tools=[tool("lookup", lookup)])
        return await runner.run(agent("lookup"), "go", config()), RunStatus.COMPLETED
    if ending == "failed by the model":
        runner = make_runner(Script(ModelProviderUnavailable("gateway down")), persistence)
        return await runner.run(agent(), "go", config()), RunStatus.FAILED
    if ending == "failed at max_tokens":
        runner = make_runner(Script(text("cut", stop=StopReason.MAX_TOKENS)), persistence)
        return await runner.run(agent(), "go", config()), RunStatus.FAILED
    if ending == "failed by a raising hook":
        class Breaks(RuntimeHook):
            def after_model(self, response):
                raise RuntimeError("the hook broke")

        runner = make_runner(Script(text()), persistence, hook=Breaks())
        return await runner.run(agent(), "go", config()), RunStatus.FAILED
    if ending == "max turns exceeded":
        runner = make_runner(Script(calls(call("lookup", "a"))), persistence, tools=[tool("lookup", lookup)])
        return await runner.run(agent("lookup"), "go", config(max_turns=1)), RunStatus.MAX_TURNS_EXCEEDED
    if ending == "a denied call":
        runner = make_runner(Script(calls(call("lookup", "a")), text()), persistence, tools=[tool("lookup", lookup)])
        return await runner.run(agent(), "go", config()), RunStatus.COMPLETED
    if ending == "cancelled during a tool call":
        entered, gate = asyncio.Event(), asyncio.Event()

        async def waits(key):
            entered.set()
            await gate.wait()
            return "never"

        runner = make_runner(Script(calls(call("waits", "a")), text()), persistence, tools=[tool("waits", waits)])
        handle = await runner.start(agent("waits"), "go", config())
        await asyncio.wait_for(entered.wait(), 10)
        handle.cancel()
        return await handle.result(), RunStatus.CANCELLED
    raise AssertionError(ending)


@pytest.mark.parametrize("ending", ENDINGS)
async def test_every_event_carries_its_timings_on_every_ending(backend, ending):
    result, status = await run_ending(ending, backend)
    assert result.status is status, (result.status, result.error)
    events = recorded(backend, result)
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "RunStarted" and kinds[-1] in TERMINAL, kinds

    for kind, payload in events:
        where = f"{ending}: {kind}"
        # NFR-15: exactly the fields FR-57 adds, on top of what the event carried before.
        assert set(payload) - BEFORE_M14[kind] == ADDED_BY_M14[kind], (where, sorted(payload))
        if kind == "RunStarted":
            assert payload["parent_run_id"] is None, where
        elif kind == "ModelCalled":
            utc(payload["started_at"], where)
            assert type(payload["duration_ms"]) is float, where
            measure(payload["duration_ms"], where, "duration_ms")
            measure(payload["queued_ms"], where, "queued_ms")
            assert payload["model"] == "priced" and payload["provider"] == "m", where
            assert payload["provider_name"] is None, where
        elif kind == "ToolCalled":
            if payload["started_at"] is None:
                assert payload["duration_ms"] == 0 and payload["queued_ms"] == 0, where
            else:
                utc(payload["started_at"], where)
                assert type(payload["duration_ms"]) is float, where
                measure(payload["duration_ms"], where, "duration_ms")
                measure(payload["queued_ms"], where, "queued_ms")
        else:
            run_started = utc(payload["started_at"], where)
            assert type(payload["duration_ms"]) is float, where
            measure(payload["duration_ms"], where, "duration_ms")
            for model in payloads(events, "ModelCalled"):
                assert run_started <= utc(model["started_at"], where), "a model call began before its run"
                assert payload["duration_ms"] >= model["duration_ms"], "a model call outlasted its run"

    tools = payloads(events, "ToolCalled")
    if ending == "failed by the model":
        assert "ModelCalled" not in kinds, "a model call that raised emitted ModelCalled"
    if ending in ("completed", "max turns exceeded", "cancelled during a tool call"):
        assert [t["started_at"] is not None for t in tools] == [True], tools
    if ending == "a denied call":
        assert [(t["started_at"], t["duration_ms"], t["queued_ms"]) for t in tools] == [(None, 0, 0)], tools


async def test_a_client_taking_at_least_50_ms_records_a_duration_of_at_least_50(backend):
    client = Script(text(), before=lambda: take_at_least(0.05))
    result = await make_runner(client, backend).run(agent(), "go", config())
    [model] = payloads(recorded(backend, result), "ModelCalled")
    assert model["duration_ms"] >= 50, model["duration_ms"]


async def test_the_second_of_two_calls_under_one_run_slot_records_its_wait(backend):
    async def slow(key):
        # Only the first call is slow. Added after the first mutation run (X3 survived): the
        # second, instant once it runs, must record its wait as queued_ms, not as duration_ms.
        if key == "first":
            await take_at_least(0.05)
        return key

    client = Script(calls(call("slow", "first"), call("slow", "second")), text())
    runner = make_runner(
        client, backend, tools=[tool("slow", slow, concurrency_safe=True)],
        scheduler_limits=SchedulerLimits(max_concurrent_tools=1),
    )
    result = await runner.run(agent("slow"), "go", config())
    ran = sorted(payloads(recorded(backend, result), "ToolCalled"), key=lambda p: utc(p["started_at"], "ToolCalled"))
    assert len(ran) == 2, ran
    assert ran[0]["duration_ms"] >= 50, ran[0]
    assert ran[1]["queued_ms"] >= 40, ran[1]
    assert ran[1]["duration_ms"] < ran[1]["queued_ms"], ran[1]


async def test_a_model_call_waiting_behind_a_provider_limit_records_the_wait():
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hold_the_first_call():
        if not entered.is_set():
            entered.set()
            await gate.wait()

    client = Script(text(), text(), before=hold_the_first_call)
    runner = make_runner(client, None, scheduler_limits=SchedulerLimits(provider_concurrency_limits={"m": 1}))
    first = await runner.start(agent(), "go", config())
    await asyncio.wait_for(entered.wait(), 10)
    second = await runner.start(agent(), "go", config())
    await until(lambda: second.state().turns == 1, "the second run to reach its first turn")
    await asyncio.sleep(0.05)
    gate.set()
    results = [await first.result(), await second.result()]
    model_calls = [payloads(recorded(None, r), "ModelCalled")[0] for r in results]
    queued = [call["queued_ms"] for call in model_calls]
    assert queued[1] > 0 and queued[1] >= 20, queued
    # Added after the first mutation run (L3 survived): the wait is no part of the call.
    # The second client answers at once, so its duration is far below its wait.
    assert model_calls[1]["duration_ms"] < queued[1], model_calls[1]


@pytest.mark.parametrize("case", ["denied by permission", "invalid arguments", "an unknown tool", "refused by before_tool"])
async def test_a_call_stopped_before_step_6_records_no_start_and_zero_measures(case):
    class Refuses(RuntimeHook):
        def before_tool(self, tool_call):
            return HookOutcome(HookAction.REJECT, reason="refused")

    issued = {
        "denied by permission": ToolCall(id="c", name="lookup", arguments={"key": "a"}),
        "invalid arguments": ToolCall(id="c", name="lookup", arguments={"key": 7}),
        "an unknown tool": ToolCall(id="c", name="missing", arguments={"key": "a"}),
        "refused by before_tool": ToolCall(id="c", name="lookup", arguments={"key": "a"}),
    }[case]
    profile = () if case == "denied by permission" else ("lookup", "missing")
    hook = Refuses() if case == "refused by before_tool" else None
    runner = make_runner(Script(calls(issued), text()), None, tools=[tool("lookup", lookup)], hook=hook)
    result = await runner.run(agent(*profile), "go", config())
    [payload] = payloads(recorded(None, result), "ToolCalled")
    assert payload["is_error"] is True, payload
    assert (payload["started_at"], payload["duration_ms"], payload["queued_ms"]) == (None, 0, 0), payload


async def test_a_call_cancelled_while_waiting_for_its_slot_records_no_start():
    entered, gate = asyncio.Event(), asyncio.Event()

    async def blocks(key):
        entered.set()
        await gate.wait()
        return key

    client = Script(calls(call("blocks", "running"), call("blocks", "waiting")), text())
    runner = make_runner(
        client, None, tools=[tool("blocks", blocks, concurrency_safe=True)],
        scheduler_limits=SchedulerLimits(max_concurrent_tools=1),
    )
    handle = await runner.start(agent("blocks"), "go", config())
    await asyncio.wait_for(entered.wait(), 10)
    handle.cancel()
    result = await handle.result()
    assert result.status is RunStatus.CANCELLED
    by_call = {p["tool_call_id"]: p for p in payloads(recorded(None, result), "ToolCalled")}
    utc(by_call["running"]["started_at"], "the call that ran")
    assert (by_call["waiting"]["started_at"], by_call["waiting"]["duration_ms"], by_call["waiting"]["queued_ms"]) == (None, 0, 0)


async def test_model_is_what_the_client_received_when_a_before_model_hook_changes_it(backend):
    class Switches(RuntimeHook):
        def before_model(self, request):
            settings = {**request.model_settings, "model": "switched"}
            return HookOutcome(HookAction.MODIFY, replacement=dataclasses.replace(request, model_settings=settings))

    client = Script(text())
    result = await make_runner(client, backend, hook=Switches()).run(agent(), "go", config())
    received = client.received[0].model_settings.get("model")
    assert received == "switched", "the premise failed: the hook did not change the model sent"
    [model] = payloads(recorded(backend, result), "ModelCalled")
    assert model["model"] == received


@pytest.mark.parametrize(
    "named, expected",
    [("sw\x00itched", None), (7, "priced"), ("", "priced")],
    ids=["an unstorable model", "a model that is not text", "an empty model"],
)
async def test_a_hook_naming_no_usable_model_records_what_the_call_is_priced_by(backend, named, expected):
    """Added after the first mutation run (L7 survived), corrected in round 2 (C2,
    DECISION-95a84cb0). A before_model hook can put anything in model_settings, and the run
    completes on both stores. A named model no column can hold is recorded as unknown, never as
    another model, and nothing prices it. A value that names no model, not text or empty, is
    the run's recorded model, for the record and the price alike."""

    class Names(RuntimeHook):
        def before_model(self, request):
            settings = {**request.model_settings, "model": named}
            return HookOutcome(HookAction.MODIFY, replacement=dataclasses.replace(request, model_settings=settings))

    result = await make_runner(Script(text()), backend, hook=Names()).run(agent(), "go", config())
    assert result.status is RunStatus.COMPLETED, result.error
    [model] = payloads(recorded(backend, result), "ModelCalled")
    assert model["model"] == expected, model["model"]
    assert (model["cost_usd"] is None) is (expected is None), model["cost_usd"]


class _ModelName(enum.StrEnum):
    OTHER = "other"


class _TaggedModel(str):
    pass


# A second model priced a thousand times higher, so pricing by the wrong one cannot pass.
OTHER_PRICING = ModelPricing(input="0.001", output="0.002", cache_read="0.0005", cache_write="0.00125")


@pytest.mark.parametrize("named", [_ModelName.OTHER, _TaggedModel("other")], ids=["a StrEnum member", "a str subclass"])
async def test_a_hook_naming_the_model_as_str_subclass_records_and_prices_that_model(backend, named):
    """Round 2 (M14 round 1 review, C2; DECISION-95a84cb0). A StrEnum member or another str
    subclass is ordinary caller code. ModelCalled.model is then the exact text the client
    received, and the call is priced as that model, not as the run's own (AC-45, FR-30)."""

    class Names(RuntimeHook):
        def before_model(self, request):
            settings = {**request.model_settings, "model": named}
            return HookOutcome(HookAction.MODIFY, replacement=dataclasses.replace(request, model_settings=settings))

    models = registry()
    models.register(ModelEntry(
        provider="test", model_id="other", model_version="1", adapter_version="test/1",
        capabilities=ModelCapabilities(max_context_tokens=100_000, pricing=OTHER_PRICING),
    ))
    client = Script(text())
    runner = Runner({"m": client}, persistence=backend, model_registry=models, hook=Names())
    result = await runner.run(agent(), "go", config())
    assert result.status is RunStatus.COMPLETED, result.error
    assert client.received[0].model_settings["model"] is named, "the premise failed: the client got another model"

    [model] = payloads(recorded(backend, result), "ModelCalled")
    assert type(model["model"]) is str and model["model"] == "other", model["model"]
    expected = call_cost(CALL_USAGE, OTHER_PRICING)
    assert model["cost_usd"] == str(expected) and result.cost_usd == expected, (model["cost_usd"], expected)


async def test_with_no_model_named_model_is_the_recorded_model_of_fr32(backend):
    client = DefaultModel(text())
    runner = Runner({"m": client}, persistence=backend, model_registry=registry())
    result = await runner.run(AgentSpec(id="m14", instructions="go"), "go", config())
    events = recorded(backend, result)
    assert events[0][1]["model"] == "priced", "the premise failed: the client default was not recorded"
    [model] = payloads(events, "ModelCalled")
    assert model["model"] == "priced" and model["provider"] == "m"


@pytest.mark.parametrize(
    "client_class, expected",
    [(Script, None), (Declaring, "acme"), (RaisingName, None), (UnstorableName, None), (NotTextName, None)],
    ids=["no name declared", "a declared name", "a name that raises", "an unstorable name", "a name that is not text"],
)
async def test_provider_name_is_the_name_a_client_declares_or_none(backend, client_class, expected):
    result = await make_runner(client_class(text()), backend).run(agent(), "go", config())
    assert result.status is RunStatus.COMPLETED, result.error
    [model] = payloads(recorded(backend, result), "ModelCalled")
    assert model["provider_name"] == expected


async def test_provider_name_is_read_once_per_run():
    client = CountedName(calls(call("lookup", "a")), text())
    result = await make_runner(client, None, tools=[tool("lookup", lookup)]).run(agent("lookup"), "go", config())
    names = [p["provider_name"] for p in payloads(recorded(None, result), "ModelCalled")]
    assert names == ["counted", "counted"], names
    assert client.reads == 1, f"provider_name was read {client.reads} times in one run"


async def test_run_started_records_the_parent_run_id(backend):
    parent = await make_runner(Script(text()), backend).run(agent(), "go", config())
    child = await make_runner(Script(text()), backend).run(agent(), "go", config(parent_run_id=parent.run_id))
    assert recorded(backend, parent)[0][1]["parent_run_id"] is None
    assert recorded(backend, child)[0][1]["parent_run_id"] == parent.run_id


# =================================================================================================
# FR-58, FR-59, AC-46: the span tree and its attributes
# =================================================================================================


@pytest.mark.parametrize("consumed", ["a finished RunResult", "a RunHandle"])
async def test_a_run_becomes_one_invoke_agent_span_with_chat_and_execute_tool_children(consumed):
    provider, memory = in_memory()
    exporter = telemetry().OpenTelemetryExporter(provider)
    try:
        runner = rich_runner()
        if consumed == "a RunHandle":
            handle = await runner.start(rich_agent(), "TASKMARKER", config())
            exporter.export(handle)
            result = await handle.result()
        else:
            result = await runner.run(rich_agent(), "TASKMARKER", config())
            exporter.export(result)
        await flushed(exporter)
    finally:
        await shut(exporter)
    assert result.status is RunStatus.COMPLETED, result.error

    spans = memory.get_finished_spans()
    [run_span] = by_operation(spans, "invoke_agent")
    chats, tools = by_operation(spans, "chat"), by_operation(spans, "execute_tool")
    assert (len(chats), len(tools), len(spans)) == (2, 3, 6), [s.name for s in spans]
    for child in chats + tools:
        assert child.parent is not None and child.parent.span_id == run_span.context.span_id, child.name
        assert child.context.trace_id == run_span.context.trace_id, child.name
    assert run_span.parent is None and not run_span.links

    events = [(e.event_type.value, e.payload) for e in result.events]
    for span, payload in zip(sorted(chats, key=lambda s: s.start_time), payloads(events, "ModelCalled")):
        assert_timed(span, payload)
    tool_events = {p["tool_call_id"]: p for p in payloads(events, "ToolCalled")}
    for span in tools:
        assert_timed(span, tool_events[span.attributes["gen_ai.tool.call.id"]])
    assert_timed(run_span, events[-1][1])
    assert all("agentsdk.parent_run_id" not in span.attributes for span in spans), "a top-level run carries a parent"


async def test_every_attribute_fr59_names_carries_the_recorded_value():
    parent = await make_runner(Script(text()), None).run(agent(), "go", config())
    provider, memory = in_memory()
    exporter = telemetry().OpenTelemetryExporter(provider)
    try:
        result = await rich_runner().run(rich_agent(), "TASKMARKER", config(parent_run_id=parent.run_id))
        exporter.export(result)
        await flushed(exporter)
    finally:
        await shut(exporter)
    spans = memory.get_finished_spans()
    events = [(e.event_type.value, e.payload) for e in result.events]
    assert len(spans) == 6, [s.name for s in spans]

    for span in spans:
        attributes = span.attributes
        assert attributes["agentsdk.semconv_version"] == SEMCONV, span.name
        assert attributes["agentsdk.run_id"] == result.run_id, span.name
        assert (attributes["agentsdk.tenant_id"], attributes["agentsdk.project_id"]) == (TENANT, PROJECT), span.name
        assert attributes["agentsdk.parent_run_id"] == parent.run_id, span.name
        assert "gen_ai.provider.name" not in attributes, "a client that declares no provider was given one"

    [run_span] = by_operation(spans, "invoke_agent")
    started = events[0][1]
    assert run_span.attributes["gen_ai.agent.id"] == "m14"
    assert run_span.attributes["gen_ai.request.model"] == started["model"]
    assert run_span.attributes["agentsdk.model_client_key"] == started["provider"]

    chats = sorted(by_operation(spans, "chat"), key=lambda s: s.start_time)
    for span, payload in zip(chats, payloads(events, "ModelCalled")):
        attributes = span.attributes
        assert attributes["gen_ai.request.model"] == payload["model"]
        assert attributes["agentsdk.model_client_key"] == payload["provider"] == "m"
        assert attributes["gen_ai.usage.input_tokens"] == payload["usage"]["prompt_tokens"] == 10
        assert attributes["gen_ai.usage.output_tokens"] == payload["usage"]["completion_tokens"] == 5
        assert attributes["gen_ai.usage.cache_read.input_tokens"] == payload["usage"]["cache_read_tokens"] == 3
        assert attributes["gen_ai.usage.cache_creation.input_tokens"] == payload["usage"]["cache_write_tokens"] == 2
        assert list(attributes["gen_ai.response.finish_reasons"]) == [payload["stop_reason"]]
        assert payload["cost_usd"] is not None, "the premise failed: the priced call has no cost"
        assert attributes["agentsdk.cost_usd"] == payload["cost_usd"]
        Decimal(attributes["agentsdk.cost_usd"])

    tool_events = {p["tool_call_id"]: p for p in payloads(events, "ToolCalled")}
    seen = set()
    for span in by_operation(spans, "execute_tool"):
        attributes = span.attributes
        payload = tool_events[attributes["gen_ai.tool.call.id"]]
        assert attributes["gen_ai.tool.name"] == payload["name"] == "fetchish"
        assert attributes["agentsdk.tool.is_error"] is payload["is_error"]
        assert attributes["agentsdk.tool.truncated"] is payload["truncated"]
        if payload["is_error"]:
            assert attributes["error.type"] == payload["error_type"]
            seen.add("error")
        else:
            assert "error.type" not in attributes
        if payload["truncated"]:
            seen.add("truncated")
    assert seen == {"error", "truncated"}, "the premise failed: the run had no failed or no truncated call"


async def test_gen_ai_provider_name_carries_a_declared_name_and_is_absent_otherwise():
    for client_class, expected in ((Declaring, "acme"), (Script, None)):
        provider, memory = in_memory()
        exporter = telemetry().OpenTelemetryExporter(provider)
        try:
            result = await make_runner(client_class(text()), None).run(agent(), "go", config())
            exporter.export(result)
            await flushed(exporter)
        finally:
            await shut(exporter)
        [chat] = by_operation(memory.get_finished_spans(), "chat")
        assert chat.attributes.get("gen_ai.provider.name") == expected, client_class.__name__
        assert chat.attributes["agentsdk.model_client_key"] == "m"


async def test_an_unpriced_call_has_no_cost_attribute():
    provider, memory = in_memory()
    exporter = telemetry().OpenTelemetryExporter(provider)
    try:
        result = await make_runner(Script(text()), None).run(agent(preferred_model="m:unpriced"), "go", config())
        exporter.export(result)
        await flushed(exporter)
    finally:
        await shut(exporter)
    [model] = payloads(recorded(None, result), "ModelCalled")
    assert model["cost_usd"] is None, "the premise failed: the call was priced"
    [chat] = by_operation(memory.get_finished_spans(), "chat")
    assert "agentsdk.cost_usd" not in chat.attributes, "an unknown cost was exported"


async def test_a_child_run_is_linked_to_its_parent_span_only_by_the_exporter_that_started_it():
    provider, memory = in_memory()
    other_provider, other_memory = in_memory()
    exporter = telemetry().OpenTelemetryExporter(provider)
    stranger = telemetry().OpenTelemetryExporter(other_provider)
    try:
        parent = await make_runner(Script(text()), None).run(agent(), "go", config())
        exporter.export(parent)
        await flushed(exporter)
        child = await make_runner(Script(text()), None).run(agent(), "go", config(parent_run_id=parent.run_id))
        exporter.export(child)
        stranger.export(child)
        await flushed(exporter)
        await flushed(stranger)
    finally:
        await shut(exporter)
        await shut(stranger)

    runs = {s.attributes["agentsdk.run_id"]: s for s in by_operation(memory.get_finished_spans(), "invoke_agent")}
    parent_span, child_span = runs[parent.run_id], runs[child.run_id]
    assert [link.context.span_id for link in child_span.links] == [parent_span.context.span_id]
    assert child_span.attributes["agentsdk.parent_run_id"] == parent.run_id
    [unlinked] = by_operation(other_memory.get_finished_spans(), "invoke_agent")
    assert not unlinked.links, "an exporter that never started the parent linked to it"
    assert unlinked.parent is None
    assert unlinked.attributes["agentsdk.parent_run_id"] == parent.run_id


def _span_texts(span):
    """Every piece of text a span exports, with the attribute it came from."""
    yield "name", span.name
    yield "status", str(span.status.description or "")
    for source in [span.attributes, *(e.attributes for e in span.events), *(link.attributes for link in span.links)]:
        for name, value in (source or {}).items():
            yield "attribute name", name
            values = value if isinstance(value, (tuple, list)) else (value,)
            for item in values:
                yield name, str(item)
    for event in span.events:
        yield "event name", event.name


async def test_no_span_exports_content_arguments_results_or_credentials():
    values = dotenv_values(REPO / ".env")
    key = (values.get("MODEL_API_KEY") or "").strip()
    database_url = (values.get("DATABASE_URL") or "").strip()
    password = urlsplit(database_url).password or ""
    assert key and password, "MODEL_API_KEY and a DATABASE_URL with a password must be set in .env for this check"

    provider, memory = in_memory()
    exporter = telemetry().OpenTelemetryExporter(provider)
    try:
        handle = await rich_runner(secrets=(key, password)).start(rich_agent(), f"TASKMARKER {key} {password}", config())
        exporter.export(handle)
        result = await handle.result()
        await flushed(exporter)
    finally:
        await shut(exporter)
    assert result.status is RunStatus.COMPLETED, "the premise failed: the run did not complete"
    spans = memory.get_finished_spans()
    assert len(spans) == 6, "the premise failed: the run was not exported"

    ids = {"agentsdk.run_id", "agentsdk.parent_run_id", "gen_ai.tool.call.id"}
    for span in spans:
        for source, piece in _span_texts(span):
            for marker in MARKERS:
                assert marker not in piece, f"span {span.name!r} exports content ({marker}) in {source}"
            assert key not in piece, f"span {span.name!r} exports the model API key in {source}"
            assert database_url not in piece, f"span {span.name!r} exports the database URL in {source}"
            if source not in ids:
                assert password not in piece, f"span {span.name!r} exports the database password in {source}"


# =================================================================================================
# FR-60, AC-47: telemetry never affects a run
# =================================================================================================


def _raising_exporter():
    return provider_with(SimpleSpanProcessor(RaisingExporter())), []


def _blocking_batch():
    blocking = Blocking()
    return provider_with(BatchSpanProcessor(blocking)), [blocking]


def _closed_port_otlp():
    exporter = OTLPSpanExporter(endpoint=f"http://127.0.0.1:{closed_port()}/v1/traces", timeout=1)
    return provider_with(SimpleSpanProcessor(exporter)), []


def _blocking_simple():
    blocking = Blocking()
    return provider_with(SimpleSpanProcessor(blocking)), [blocking]


def _raising_provider():
    return RaisingProvider(), []


FAILING = {
    "a span exporter that raises": _raising_exporter,
    "a span exporter that blocks": _blocking_batch,
    "an OTLP/HTTP exporter at a closed port": _closed_port_otlp,
    "a SimpleSpanProcessor over a blocking exporter": _blocking_simple,
    "a tracer provider that raises": _raising_provider,
}


def _scrub(row):
    return {k: v for k, v in row.items() if k not in {"run_id", "event_id", "message_id", "id", "timestamp"} and not k.endswith("_at")}


def summary(persistence, result):
    """Everything about a run a second identical run must match: its result, its events
    without their timings, and on Postgres its rows without ids and timestamps."""
    outcome = (result.status, result.output, result.error, result.usage, result.cost_usd)
    events = [(kind, {k: v for k, v in p.items() if k not in TIMINGS}) for kind, p in recorded(persistence, result)]
    if persistence is None:
        return outcome, events, None
    [(run_row,)] = query("SELECT to_jsonb(r) FROM runs r WHERE run_id = %s", (result.run_id,))
    messages = query("SELECT to_jsonb(m) FROM messages m WHERE run_id = %s ORDER BY sequence_no", (result.run_id,))
    stored = query("SELECT to_jsonb(e) - 'payload' FROM run_events e WHERE run_id = %s ORDER BY sequence_no", (result.run_id,))
    return outcome, events, (_scrub(run_row), [_scrub(m) for (m,) in messages], [_scrub(e) for (e,) in stored])


async def deterministic_run(persistence, exporter=None):
    runner = make_runner(Script(calls(call("lookup", "a")), text("answer")), persistence, tools=[tool("lookup", lookup)])
    handle = await runner.start(agent("lookup"), "go", config())
    if exporter is not None:
        exporter.export(handle)
    return await asyncio.wait_for(handle.result(), 30)


@pytest.mark.parametrize("setup", list(FAILING))
async def test_a_failing_or_stalling_telemetry_path_changes_nothing_about_a_run(backend, setup):
    without = await deterministic_run(backend)
    provider, blockers = FAILING[setup]()
    exporter = telemetry().OpenTelemetryExporter(provider)
    try:
        watched = await deterministic_run(backend, exporter)
        assert summary(backend, watched) == summary(backend, without)
    finally:
        for blocker in blockers:
            blocker.release.set()
        await shut(exporter)


async def test_run_and_result_return_while_the_blocking_exporter_is_still_blocked():
    blocking = Blocking()
    exporter = telemetry().OpenTelemetryExporter(provider_with(SimpleSpanProcessor(blocking)))
    try:
        runner = make_runner(Script(text(), text()), None)
        handle = await runner.start(agent(), "go", config())
        exporter.export(handle)
        followed = await asyncio.wait_for(handle.result(), 10)
        assert await asyncio.to_thread(blocking.entered.wait, 15), "the premise failed: the exporter never blocked"
        again = await asyncio.wait_for(runner.run(agent(), "go", config()), 10)
        exporter.export(again)
        assert followed.status is RunStatus.COMPLETED and again.status is RunStatus.COMPLETED
        assert blocking.entered.is_set() and not blocking.release.is_set(), "the exporter was not still blocked"
    finally:
        blocking.release.set()
        await shut(exporter)


async def test_no_span_is_created_ended_or_exported_on_the_event_loop_thread():
    spy, recording = ThreadSpy(), Recording()
    exporter = telemetry().OpenTelemetryExporter(provider_with(spy, SimpleSpanProcessor(recording)))
    loop_thread = threading.get_ident()
    try:
        handle = await rich_runner().start(rich_agent(), "TASKMARKER", config())
        exporter.export(handle)
        await handle.result()
        await flushed(exporter)
    finally:
        await shut(exporter)
    assert {kind for kind, _ in spy.threads} == {"start", "end"} and recording.threads, "nothing was spied"
    on_loop = sorted({kind for kind, ident in spy.threads if ident == loop_thread})
    assert not on_loop, f"spans were {on_loop}ed on the event loop's thread"
    assert loop_thread not in recording.threads, "a span was exported on the event loop's thread"


async def test_a_full_queue_drops_events_and_counts_them_without_raising():
    """Changed after the red run (review prompt, author issues): a run's spans are built at its
    terminal event, and a real first run's three events could lose that event to a queue of
    one, leaving nothing to hold the worker. A finished run of one terminal event is always
    accepted by an empty queue, and holds the worker inside its span's start."""
    holds = HoldsTheFirstStart()
    exporter = telemetry().OpenTelemetryExporter(provider_with(holds), max_queue_size=1)
    try:
        terminal = RunEvent(
            event_type=EventType.RUN_COMPLETED, tenant_id=TENANT, project_id=PROJECT, run_id=str(uuid.uuid4()),
            sequence_no=1,
            payload={"status": "completed", "turns": 1, "reason": None,
                     "started_at": datetime.now(timezone.utc).isoformat(), "duration_ms": 1.0},
        )
        exporter.export(RunResult(status=RunStatus.COMPLETED, output=None, events=(terminal,)))
        assert await asyncio.to_thread(holds.entered.wait, 10), "the premise failed: the worker never started a span"
        second = await rich_runner().run(rich_agent(), "TASKMARKER", config())
        started = time.perf_counter()
        exporter.export(second)
        assert time.perf_counter() - started < 1, "export waited for a full queue"
        assert exporter.dropped >= len(second.events) - 1, (exporter.dropped, len(second.events))
    finally:
        holds.release.set()
        await shut(exporter)


@pytest.mark.parametrize(
    "build",
    [lambda: provider_with(RaisingOnEnd()), RaisingProvider],
    ids=["a span processor that raises", "a tracer provider that raises"],
)
async def test_an_exception_on_the_telemetry_path_increments_errors_and_is_not_raised(build):
    exporter = telemetry().OpenTelemetryExporter(build())
    try:
        result = await make_runner(Script(text()), None).run(agent(), "go", config())
        exporter.export(result)
        await flushed(exporter)
        assert exporter.errors >= 1, exporter.errors
    finally:
        await shut(exporter)


async def test_flush_waits_for_a_followed_run_to_end():
    """Added after the first mutation run (T13 survived): flush returns True only once a followed
    RunHandle has ended and its events are handled, not whenever the queue happens to be empty."""
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await gate.wait()

    provider, memory = in_memory()
    exporter = telemetry().OpenTelemetryExporter(provider)
    try:
        handle = await make_runner(Script(text(), before=hold), None).start(agent(), "go", config())
        exporter.export(handle)
        await asyncio.wait_for(entered.wait(), 10)
        assert await asyncio.to_thread(exporter.flush, 0.2) is False, "flush returned while the run was still going"
        gate.set()
        assert (await handle.result()).status is RunStatus.COMPLETED
        await flushed(exporter)
    finally:
        gate.set()
        await shut(exporter)
    assert len(by_operation(memory.get_finished_spans(), "invoke_agent")) == 1


async def test_a_failed_run_exports_nothing_of_its_failure_reason():
    """Added after the first mutation run (T14 survived): a run's failure reason can quote a tool
    or a model, so no part of it reaches a span's attributes, name, status or events."""

    class Breaks(RuntimeHook):
        def after_model(self, response):
            raise RuntimeError("REASONMARKER the hook broke")

    provider, memory = in_memory()
    exporter = telemetry().OpenTelemetryExporter(provider)
    try:
        result = await make_runner(Script(text()), None, hook=Breaks()).run(agent(), "go", config())
        exporter.export(result)
        await flushed(exporter)
    finally:
        await shut(exporter)
    assert result.status is RunStatus.FAILED and "REASONMARKER" in (result.error or ""), "the premise failed"
    spans = memory.get_finished_spans()
    assert by_operation(spans, "invoke_agent"), "the premise failed: the run was not exported"
    for span in spans:
        for source, piece in _span_texts(span):
            assert "REASONMARKER" not in piece, f"span {span.name!r} exports the failure reason in {source}"


# --- Round 2 (M14 round 1 review, C1; DECISION-95a84cb0). Written before the fix. ----------------


@pytest.mark.parametrize("other_loop", ["still running", "closed before the run ends"])
@pytest.mark.parametrize("debug", [False, True], ids=["debug off", "debug on"])
async def test_a_handle_exported_from_another_threads_loop_strands_no_iterator_and_flushes(debug, other_loop):
    """FR-48 keeps a run on the loop that started it, but export(handle) can be called from
    another thread running its own loop, in debug mode or not, and that loop can end before
    the run does. The caller's own events() iterator still ends, flush finishes, and the
    run's spans are exported."""
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await gate.wait()

    provider, memory = in_memory()
    exporter = telemetry().OpenTelemetryExporter(provider)
    handle = await make_runner(Script(text(), before=hold), None).start(agent(), "go", config())
    await asyncio.wait_for(entered.wait(), 10)
    exported, release, outcome = threading.Event(), threading.Event(), {}

    def other_thread():
        async def main():
            try:
                exporter.export(handle)
                outcome["export"] = "returned"
            except BaseException as error:  # noqa: BLE001 - reported below
                outcome["export"] = type(error).__name__
            exported.set()
            if other_loop == "still running":
                await asyncio.to_thread(release.wait, 15)

        loop = asyncio.new_event_loop()
        loop.set_debug(debug)
        try:
            loop.run_until_complete(main())
        finally:
            loop.close()

    thread = threading.Thread(target=other_thread, daemon=True)
    thread.start()
    own = []
    try:
        assert await asyncio.to_thread(exported.wait, 10), "export never returned on the other thread"
        await asyncio.sleep(0.2)  # the exporter is following the run, ahead of the caller's iterator

        async def iterate():
            async for event in handle.events():
                own.append(event.event_type.value)

        own_task = asyncio.ensure_future(iterate())
        await asyncio.sleep(0.05)
        gate.set()
        assert (await asyncio.wait_for(handle.result(), 10)).status is RunStatus.COMPLETED
        try:
            await asyncio.wait_for(own_task, 5)
        except asyncio.TimeoutError:
            raise AssertionError(f"the caller's own events() iterator hung after {own}") from None
        assert own and own[-1] == "RunCompleted", own
        assert await asyncio.to_thread(exporter.flush, 5), "flush never finished"
    finally:
        gate.set()
        release.set()
        await asyncio.to_thread(thread.join, 20)
        await shut(exporter)
    assert outcome["export"] == "returned", outcome
    assert len(by_operation(memory.get_finished_spans(), "invoke_agent")) == 1


async def test_an_iterator_left_on_a_closed_loop_strands_no_iterator_on_the_run_loop():
    """The handle itself: an events() iterator abandoned on another thread's loop, which then
    closes, leaves a waiter no one can resolve. Waking it must not stop the run loop's own
    iterators from being woken."""
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await gate.wait()

    handle = await make_runner(Script(text(), before=hold), None).start(agent(), "go", config())
    await asyncio.wait_for(entered.wait(), 10)
    waiting = threading.Event()

    def other_thread():
        async def abandon():
            async for _ in handle.events():  # RunStarted, then a wait for the next event
                waiting.set()

        loop = asyncio.new_event_loop()
        loop.create_task(abandon())
        try:
            loop.run_until_complete(asyncio.sleep(0.3))
        finally:
            loop.close()  # the waiting iterator is abandoned with its loop

    thread = threading.Thread(target=other_thread, daemon=True)
    thread.start()
    await asyncio.to_thread(thread.join, 10)
    assert waiting.is_set(), "the premise failed: the other loop never iterated"
    own = []

    async def iterate():
        async for event in handle.events():
            own.append(event.event_type.value)

    own_task = asyncio.ensure_future(iterate())
    await asyncio.sleep(0.05)
    gate.set()
    assert (await asyncio.wait_for(handle.result(), 10)).status is RunStatus.COMPLETED
    try:
        await asyncio.wait_for(own_task, 5)
    except asyncio.TimeoutError:
        raise AssertionError(f"the run loop's iterator hung after {own}") from None
    assert own and own[-1] == "RunCompleted", own


async def test_an_iterator_on_another_threads_running_loop_is_woken_on_that_loop():
    """An events() iterator on another thread's loop in debug mode, which refuses a future
    resolved from a thread other than its own, still sees every event of the run."""
    gate, entered = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await gate.wait()

    handle = await make_runner(Script(text(), before=hold), None).start(agent(), "go", config())
    await asyncio.wait_for(entered.wait(), 10)
    seen, done = [], threading.Event()

    def other_thread():
        async def iterate():
            async for event in handle.events():
                seen.append(event.event_type.value)

        loop = asyncio.new_event_loop()
        loop.set_debug(True)
        try:
            loop.run_until_complete(asyncio.wait_for(iterate(), 15))
        except BaseException as error:  # noqa: BLE001 - reported below
            seen.append(f"raised {type(error).__name__}")
        finally:
            loop.close()
            done.set()

    thread = threading.Thread(target=other_thread, daemon=True)
    thread.start()
    await asyncio.sleep(0.2)
    gate.set()
    assert (await asyncio.wait_for(handle.result(), 10)).status is RunStatus.COMPLETED
    assert await asyncio.to_thread(done.wait, 20), "the other loop's iterator never ended"
    await asyncio.to_thread(thread.join, 5)
    assert seen and seen[-1] == "RunCompleted", seen


async def test_a_handle_whose_run_loop_has_closed_exports_the_events_it_recorded():
    """A run that finished on a loop which has since closed: export(handle) from another loop
    queues the events the handle recorded, and flush finishes."""

    def finished_elsewhere():
        async def main():
            handle = await make_runner(Script(text()), None).start(agent(), "go", config())
            await handle.result()
            return handle

        return asyncio.run(main())

    handle = await asyncio.to_thread(finished_elsewhere)
    provider, memory = in_memory()
    exporter = telemetry().OpenTelemetryExporter(provider)
    try:
        exporter.export(handle)
        await flushed(exporter)
    finally:
        await shut(exporter)
    spans = memory.get_finished_spans()
    [run_span] = by_operation(spans, "invoke_agent")
    assert run_span.attributes["agentsdk.run_id"] == handle.run_id
    assert len(by_operation(spans, "chat")) == 1, [s.name for s in spans]


# =================================================================================================
# NFR-19: telemetry is optional
# =================================================================================================


def run_python(code):
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


def test_import_agentsdk_loads_no_opentelemetry_module():
    proc = run_python("import sys, agentsdk\nprint(sorted(m for m in sys.modules if m.split('.')[0] == 'opentelemetry'))")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "[]", proc.stdout


def test_telemetry_is_a_single_module():
    assert (REPO / "agentsdk" / "telemetry.py").is_file()
    assert not (REPO / "agentsdk" / "telemetry").exists()


def test_building_the_exporter_without_the_sdk_raises_an_error_naming_the_otel_extra():
    proc = run_python(textwrap.dedent("""
        import sys

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name == "opentelemetry.sdk" or name.startswith("opentelemetry.sdk."):
                    raise ModuleNotFoundError(f"No module named {name!r}")
                return None

        sys.meta_path.insert(0, Block())
        import agentsdk.telemetry as telemetry
        try:
            telemetry.OpenTelemetryExporter(object())
        except ImportError as error:
            print("ImportError:", error)
        else:
            print("BUILT")
    """))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.startswith("ImportError:"), proc.stdout
    assert "otel" in proc.stdout and "extra" in proc.stdout, proc.stdout


# =================================================================================================
# P2-D11, P2-D12, FR-60: pins, configuration, the example and the README
# =================================================================================================


def _pins(lines):
    return {line.split("#")[0].strip() for line in lines if line.split("#")[0].strip()}


def test_the_otel_extra_and_both_requirement_files_carry_the_p2_d12_pins():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert set(project["optional-dependencies"]["otel"]) == OTEL_PINS
    assert not any(dep.lower().startswith("opentelemetry") for dep in project["dependencies"]), "NFR-18"
    lines = (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
    test_line = next(i for i, line in enumerate(lines) if line.startswith("# --- test"))
    assert OTEL_PINS <= _pins(lines[test_line:])
    assert not any(pin.startswith("opentelemetry") for pin in _pins(lines[:test_line])), "a runtime dependency"
    assert OTEL_PINS <= _pins((REPO / "scripts" / "requirements.txt").read_text(encoding="utf-8").splitlines())


def test_env_example_names_the_otlp_endpoint_and_the_offline_run_removes_it():
    assert re.search(r"^OTEL_EXPORTER_OTLP_ENDPOINT=", (REPO / ".env.example").read_text(encoding="utf-8"), re.M)
    distribution = (REPO / "tests" / "test_distribution.py").read_text(encoding="utf-8")
    assert re.search(r'^CONFIG_VARS = \(.*"OTEL_EXPORTER_OTLP_ENDPOINT".*\)$', distribution, re.M)


def test_both_readmes_list_example_13_and_the_readme_shows_the_sql_route():
    for document in (REPO / "README.md", REPO / "scripts" / "README.md"):
        assert "13_telemetry.py" in document.read_text(encoding="utf-8"), f"{document.name} does not list 13_telemetry.py"
    blocks = re.findall(r"```sql\n(.*?)```", (REPO / "README.md").read_text(encoding="utf-8"), re.S)
    assert any("run_events" in block and "duration_ms" in block for block in blocks), (
        "the README shows no SQL over run_events reading the recorded timings"
    )


def test_example_13_runs_offline_and_shows_a_span_tree(tmp_path):
    removed = ("BASE_URL", "MODEL_API_KEY", "DATABASE_URL", "DEFAULT_MODEL", "OTEL_EXPORTER_OTLP_ENDPOINT")
    env = {k: v for k, v in os.environ.items() if k not in removed}
    env.update(PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "13_telemetry.py"), "--offline"],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    assert "[FAIL]" not in proc.stdout and "[PASS]" in proc.stdout, proc.stdout[-2000:]
    missing = [name for name in ("invoke_agent", "chat", "execute_tool") if name not in proc.stdout]
    assert not missing, f"the offline span tree does not show {missing}"
