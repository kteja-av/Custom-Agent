"""M12 gate: run handles, runtime event streaming and cancellation (FR-48..FR-52, NFR-15, NFR-17, AC-39..AC-41).

Written before the implementation, against the approved specification and the
owner's decision of 2026-09-14 that a tool which ignores cancellation is a
declared limitation (DECISION-29e21dd0): no tool here is given a reason to
ignore it. Every cancellation point is reached with gates -- a model client or a
tool waiting on an event the test opens, or a store call held on a
threading.Event -- never with sleeps, and each runs on both stores.

Every run uses a priced model. On an unpriced model cost_usd is None whatever
happens, which would make "cost_usd is None exactly when a model call was in
flight" (AC-40, P2-D7) impossible to fail.

The persisted half needs DATABASE_URL and fails rather than skips without it;
every row written is removed. Names M12 adds are reached at call time, so before
the implementation each test fails on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import uuid
from decimal import Decimal

import psycopg
import pytest
from dotenv import load_dotenv

import agentsdk
from agentsdk import AgentSpec, Persistence, RunConfig, Runner, RunStatus, migrate
from agentsdk import errors as errors_module
from agentsdk.config import normalise_database_url
from agentsdk.errors import ModelProviderUnavailable, ToolError
from agentsdk.events import EventType, InMemoryEventSink
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.manifest import build_manifest
from agentsdk.migrate import apply_migrations
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import SCHEMA_PATH, PostgresEventStore, PostgresRunStore, PostgresSessionStore, RunScope
from agentsdk.primitives import InstructionAuthority, Message, Origin, Role, TaintFlag, ToolCall, TrustZone
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry
from agentsdk.scheduler import SchedulerLimits
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import ResultProvenance, Tool, ToolSpec

load_dotenv()

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
TENANT, PROJECT = "SYN-m12", "p-m12"
SETTLE = 0.1
KEY_SCHEMA = {
    "type": "object",
    "properties": {"key": {"type": "string"}},
    "required": ["key"],
    "additionalProperties": False,
}
CALL_USAGE = Usage(10, 5, 15)
# 10 input tokens at 0.000001 plus 5 output tokens at 0.000002.
CALL_COST = Decimal("0.000020")
TERMINAL = ("RunCompleted", "RunFailed", "RunCancelled")
DECLARED = dict(
    origin=Origin.EXTERNAL_TOOL,
    trust_zone=TrustZone.UNTRUSTED,
    instruction_authority=InstructionAuthority.DATA_ONLY,
    taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT, TaintFlag.PROMPT_INJECTION_RISK}),
)
EXECUTOR = dict(
    origin=Origin.INTERNAL_TOOL,
    trust_zone=TrustZone.TRUSTED_SOURCE,
    instruction_authority=InstructionAuthority.DATA_ONLY,
    taint_flags=frozenset(),
)
CANCELLED_SOURCE = "urn:agentsdk:tool-error:ToolCancelled"


# --- shared helpers --------------------------------------------------------------------------------


def cancelled():
    return RunStatus("cancelled")


def text(content="done", stop=StopReason.END_TURN):
    return ModelResponse(message=Message(role=Role.ASSISTANT, content=content), stop_reason=stop, usage=CALL_USAGE)


def calls(*tool_calls):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, tool_calls=tuple(tool_calls)),
        stop_reason=StopReason.TOOL_CALLS,
        usage=CALL_USAGE,
    )


def call(name, key):
    return ToolCall(id=key, name=name, arguments={"key": key})


def registry():
    return ModelRegistry([
        ModelEntry(
            provider="test",
            model_id="priced",
            model_version="1",
            adapter_version="test/1",
            capabilities=ModelCapabilities(
                max_context_tokens=100_000, pricing=ModelPricing(input="0.000001", output="0.000002")
            ),
        )
    ])


def agent(*profile, **fields):
    fields.setdefault("preferred_model", "m:priced")
    return AgentSpec(id="m12", instructions="go", tool_profile=tuple(profile), **fields)


def config(**fields):
    return RunConfig(tenant_id=TENANT, project_id=PROJECT, **fields)


def make_runner(client, persistence, **kwargs):
    return Runner({"m": client}, persistence=persistence, model_registry=registry(), **kwargs)


async def until(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def collect(stream):
    return [event async for event in stream]


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def drop_runs(tenant=TENANT):
    with psycopg.connect(DSN, autocommit=True) as conn:
        ids = [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE tenant_id=%s", (tenant,)).fetchall()]
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


def history(sessions, persistence, run_id):
    if persistence is None:
        return sessions.history(run_id)
    return persistence.session_store_for(RunScope(run_id=run_id, tenant_id=TENANT, project_id=PROJECT)).history(run_id)


def tool_results(sessions, persistence, run_id):
    return [r for m in history(sessions, persistence, run_id) if m.role is Role.TOOL for r in m.tool_results]


class Hold:
    """A model response the client waits for until the test releases it."""

    def __init__(self, then=None):
        self.then = then if then is not None else text()
        self.entered = asyncio.Event()
        self.released = asyncio.Event()


class Script:
    """Replays model responses; raises an exception item; waits on a Hold first."""

    def __init__(self, *items, log=None):
        self.items = list(items)
        self.log = log if log is not None else []
        self.sends = 0

    async def send(self, request):
        self.sends += 1
        self.log.append(("send", self.sends))
        item = self.items.pop(0) if self.items else text()
        if isinstance(item, Hold):
            item.entered.set()
            await item.released.wait()
            item = item.then
        if isinstance(item, BaseException):
            raise item
        return item


class PerRun:
    """One client shared by several runs, scripted per run by the run's task text."""

    def __init__(self, scripts, log):
        self.scripts, self.log = scripts, log

    async def send(self, request):
        run = request.messages[0].content
        self.log.append(("send", run))
        item = self.scripts[run].pop(0) if self.scripts[run] else text()
        if isinstance(item, Hold):
            item.entered.set()
            await item.released.wait()
            item = item.then
        return item


def echo_tool(log, name="echo"):
    async def fn(key):
        log.append(("tool", key))
        return f"{name} {key}"

    return Tool(spec=ToolSpec(name=name, description="Echoes.", input_schema=KEY_SCHEMA), fn=fn)


def gated_tool(log, gates, stopped, name="slow", *, safe=True, external=True):
    async def fn(key):
        log.append(("tool", key))
        try:
            await gates.setdefault(key, asyncio.Event()).wait()
        except asyncio.CancelledError:
            stopped.append(key)
            raise
        return f"{name} {key}"

    fields = {}
    if safe:
        fields["concurrency_safe"] = True
    if external:
        fields["result_provenance"] = ResultProvenance.external()
    return Tool(spec=ToolSpec(name=name, description="Waits on a gate.", input_schema=KEY_SCHEMA, **fields), fn=fn)


class HoldInStore:
    """Holds one store call inside the store, on a worker thread, until released."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.used = False

    def wrap(self, real, predicate):
        def held(store, *args, **kwargs):
            if not self.used and predicate(*args, **kwargs):
                self.used = True
                self.entered.set()
                self.release.wait(30)
            return real(store, *args, **kwargs)

        return held


def model_usage(result):
    total = Usage()
    for event in result.events:
        if event.event_type is EventType.MODEL_CALLED:
            total = total + Usage(**event.payload["usage"])
    return total


def labels(provenance):
    return {name: getattr(provenance, name) for name in DECLARED}


def assert_cancelled(result, persistence, *, in_flight, reason="cancelled"):
    """FR-50's terminal state, on the result and, when persisted, in the store."""
    assert result.status is cancelled(), (result.status, result.error)
    assert result.output is None
    assert result.error == reason
    numbers = [event.sequence_no for event in result.events]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    kinds = [event.event_type.value for event in result.events]
    assert kinds.count("RunCancelled") == 1 and kinds[-1] == "RunCancelled", kinds
    assert result.events[-1].payload.get("reason") == reason
    assert result.usage == model_usage(result), "usage is not the sum of the ModelCalled events"
    answered = [Decimal(e.payload["cost_usd"]) for e in result.events if e.event_type is EventType.MODEL_CALLED]
    expected_cost = None if in_flight else sum(answered, Decimal(0))
    assert result.cost_usd == expected_cost, (result.cost_usd, expected_cost)
    if persistence is not None:
        [(status, prompt, completion, cost, ended)] = query(
            "SELECT status, prompt_tokens, completion_tokens, cost_usd, completed_at IS NOT NULL FROM runs WHERE run_id=%s",
            (result.run_id,),
        )
        assert status == "cancelled" and ended, status
        assert (prompt, completion) == (result.usage.prompt_tokens, result.usage.completion_tokens)
        assert cost == expected_cost
        stored = [row[0] for row in query("SELECT event_type FROM run_events WHERE run_id=%s ORDER BY sequence_no", (result.run_id,))]
        assert stored == kinds, "the stored events differ from RunResult.events"
    return kinds


def assert_nothing_started_after_cancel(log):
    at = log.index(("cancel",))
    late = [entry for entry in log[at + 1:] if entry[0] in ("send", "tool")]
    assert not late, f"started after cancellation took effect: {late}"


def assert_cancelled_call(result, sessions, persistence, key, *, reached_step_6):
    [stored] = [r for r in tool_results(sessions, persistence, result.run_id) if r.tool_call_id == key]
    assert stored.is_error is True and stored.content.startswith("ToolCancelled"), stored.content
    assert labels(stored.provenance) == (DECLARED if reached_step_6 else EXECUTOR), key
    assert stored.provenance.source_uri_or_hash == CANCELLED_SOURCE
    kinds = [event.event_type.value for event in result.events]
    positions = [
        i for i, e in enumerate(result.events)
        if e.event_type is EventType.TOOL_CALLED and e.payload.get("tool_call_id") == key
    ]
    assert len(positions) == 1, f"{key} has {len(positions)} ToolCalled events"
    event = result.events[positions[0]]
    assert event.payload["is_error"] is True and event.payload["error_type"] == "ToolCancelled"
    assert positions[0] < kinds.index("RunCancelled"), "ToolCalled for a cancelled call came after RunCancelled"


def test_the_new_public_names_exist():
    assert "RunHandle" in agentsdk.__all__ and hasattr(agentsdk, "RunHandle")
    assert issubclass(errors_module.ToolCancelled, ToolError)
    assert cancelled().value == "cancelled"
    assert EventType("RunCancelled").value == "RunCancelled"


# =================================================================================================
# FR-52: event sinks number under a lock, and read back in order
# =================================================================================================


def test_eight_threads_emitting_into_one_in_memory_sink_get_unique_contiguous_numbers():
    """At the default switch interval the race is rare; at a microsecond it produced 584 to 654
    duplicates in each of 10 trials before M12 (KNOWLEDGE-93fa7f44)."""
    sink = InMemoryEventSink(TENANT, PROJECT, str(uuid.uuid4()))
    barrier = threading.Barrier(8)

    def work():
        barrier.wait()
        for _ in range(300):
            sink.emit(EventType.TOOL_CALLED, {"k": "v"})

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)
    assert [event.sequence_no for event in sink.events()] == list(range(1, 2401))


def test_the_postgres_sinks_events_are_in_sequence_order_under_concurrent_emits(monkeypatch):
    """The sink appends to its buffer after the insert commits, so another writer can commit
    and append in between. That window is too short to hit reliably (this test passed
    against the unfixed sink before it was widened), so odd-numbered events wait 6 ms
    between the commit and the append, letting the next writer overtake them."""
    assert DSN, "DATABASE_URL must be set"
    import dataclasses as real_dataclasses
    import types

    from agentsdk import postgres as postgres_module

    def replace_after_a_delay(instance, **changes):
        if changes.get("sequence_no", 0) % 2:
            time.sleep(0.006)
        return real_dataclasses.replace(instance, **changes)

    monkeypatch.setattr(postgres_module, "dataclasses", types.SimpleNamespace(replace=replace_after_a_delay))
    scope = RunScope(run_id=str(uuid.uuid4()), tenant_id=TENANT, project_id=PROJECT)
    PostgresRunStore(DSN).start_run(
        scope, agent_spec_id="m12", max_turns=1, model_id=None, principal_context=None,
        manifest=build_manifest(sdk_version="m12", agent_spec_id="m12", instructions="i", tool_profile=(),
                                tool_spec_hashes=[], model_id=None),
    )
    sink = PostgresEventStore(DSN, TENANT, PROJECT, scope.run_id)
    barrier = threading.Barrier(8)

    def work():
        barrier.wait()
        for _ in range(25):
            sink.emit(EventType.TOOL_CALLED, {"k": "v"})

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)
    assert [event.sequence_no for event in sink.events()] == list(range(1, 201))


# =================================================================================================
# FR-51, AC-41: migration 0005
# =================================================================================================


class Namespace:
    """A throwaway schema, as in M7, M9 and M11: the live database is already migrated."""

    def __init__(self, baseline=True):
        self.baseline = baseline
        self.name = "m12_" + uuid.uuid4().hex[:8]
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
        # Read over a connection whose search_path is this schema: from any other,
        # pg_get_constraintdef qualifies a foreign key's target with the schema name,
        # which differs between two namespaces and would never compare equal.
        with psycopg.connect(self.dsn) as conn:
            columns = set(conn.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
                " WHERE table_schema=%s AND table_name=%s",
                (self.name, table),
            ).fetchall())
            constraints = set(conn.execute(
                "SELECT c.conname, pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname=%s AND t.relname=%s",
                (self.name, table),
            ).fetchall())
        return columns, constraints


def insert_run(ns, status):
    with psycopg.connect(ns.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, tenant_id, project_id, agent_spec_id, status, max_turns) VALUES (%s, 't', 'p', 's', %s, 3)",
            (str(uuid.uuid4()), status),
        )


def test_migration_0005_admits_cancelled_and_still_refuses_an_unknown_status(monkeypatch):
    assert DSN, "DATABASE_URL must be set"
    real = migrate.discover()
    assert "0005" in [version for version, _ in real], "migration 0005 is not on disk"
    tables = ("runs", "messages", "run_events", "execution_manifests")

    with Namespace() as ns:
        monkeypatch.setattr(migrate, "discover", lambda: [m for m in real if m[0] <= "0004"])
        assert apply_migrations(ns.dsn) == ["0002", "0003", "0004"]
        insert_run(ns, "completed")
        with pytest.raises(psycopg.errors.CheckViolation):
            insert_run(ns, "cancelled")

        monkeypatch.setattr(migrate, "discover", lambda: real)
        assert apply_migrations(ns.dsn) == [v for v, _ in real if v > "0004"]
        insert_run(ns, "cancelled")
        with pytest.raises(psycopg.errors.CheckViolation):
            insert_run(ns, "paused")
        assert apply_migrations(ns.dsn) == [], "a second application changed something"
        upgraded = {table: ns.shape(table) for table in tables}

    with Namespace(baseline=False) as fresh:
        apply_migrations(fresh.dsn, baseline=SCHEMA_PATH)
        assert {table: fresh.shape(table) for table in tables} == upgraded


# =================================================================================================
# FR-48, FR-49, AC-39: start, events, result, state
# =================================================================================================


class Halts(RuntimeHook):
    def before_model(self, request):
        return HookOutcome(action=HookAction.HALT, reason="halted by the test")


class Explodes(RuntimeHook):
    def before_model(self, request):
        raise RuntimeError("an unforeseen failure reaches the Runner's boundary")


def ending(name):
    """A fresh scripted run for one way a run ends."""
    log = []

    def boom(key):
        raise RuntimeError("the tool itself fails")

    failing = Tool(spec=ToolSpec(name="boom", description="Raises.", input_schema=KEY_SCHEMA), fn=boom)
    hold = Hold()
    table = {
        "completed": (Script(calls(call("echo", "c1")), text(), log=log), [echo_tool(log)], None, {}, RunStatus.COMPLETED),
        "a model error": (Script(ModelProviderUnavailable("gateway down"), log=log), [], None, {}, RunStatus.FAILED),
        "max_tokens": (Script(text("cut off", stop=StopReason.MAX_TOKENS), log=log), [], None, {}, RunStatus.FAILED),
        "max_turns_exceeded": (
            Script(*(calls(call("echo", f"c{i}")) for i in range(3)), log=log), [echo_tool(log)], None,
            {"max_turns": 2}, RunStatus.MAX_TURNS_EXCEEDED,
        ),
        "a tool that fails": (Script(calls(call("boom", "c1")), text(), log=log), [failing], None, {}, RunStatus.COMPLETED),
        "a hook halt": (Script(log=log), [], Halts(), {}, RunStatus.FAILED),
        "an exception at the Runner boundary": (Script(log=log), [], Explodes(), {}, RunStatus.FAILED),
        "cancelled": (Script(hold, log=log), [], None, {}, None),
    }
    client, tools, hook, fields, status = table[name]
    return dict(client=client, tools=tools, hook=hook, fields=fields, status=status, hold=hold)


ENDINGS = [
    "completed", "a model error", "max_tokens", "max_turns_exceeded",
    "a tool that fails", "a hook halt", "an exception at the Runner boundary", "cancelled",
]


# M14, NFR-15: the timings FR-57 adds differ between any two runs by nature, so they are
# left out of the comparison; every other field of every event must still match.
FR57_TIMINGS = ("started_at", "duration_ms", "queued_ms")


def shape(result):
    return (
        result.status,
        result.output,
        result.error,
        result.usage,
        result.cost_usd,
        [
            (
                e.event_type,
                e.sequence_no,
                e.tool_call_id,
                json.dumps({k: v for k, v in e.payload.items() if k not in FR57_TIMINGS}, sort_keys=True, default=str),
            )
            for e in result.events
        ],
    )


@pytest.mark.parametrize("name", ENDINGS)
async def test_every_ending_streams_exactly_its_result_events_to_every_iterator(backend, name):
    run = ending(name)
    profile = [tool.name for tool in run["tools"]]
    handle = await make_runner(run["client"], backend, tools=run["tools"], hook=run["hook"]).start(
        agent(*profile), "go", config(**run["fields"])
    )
    assert isinstance(handle.run_id, str) and str(uuid.UUID(handle.run_id)) == handle.run_id
    early = asyncio.ensure_future(collect(handle.events()))
    other = asyncio.ensure_future(collect(handle.events()))
    if name == "cancelled":
        await until(run["hold"].entered.is_set, "the model call to begin")
        handle.cancel()
    result = await asyncio.wait_for(handle.result(), 10)
    assert await handle.result() is result, "result() built a second RunResult"
    assert result.run_id == handle.run_id
    assert result.status is (cancelled() if name == "cancelled" else run["status"]), (result.status, result.error)

    expected = list(result.events)
    assert [e.sequence_no for e in expected] == list(range(1, len(expected) + 1))
    assert expected[-1].event_type.value in TERMINAL
    assert await asyncio.wait_for(early, 10) == expected
    assert await asyncio.wait_for(other, 10) == expected
    assert await asyncio.wait_for(collect(handle.events()), 10) == expected, "an iterator opened after the end differs"
    if backend is not None:
        stored = [row[0] for row in query("SELECT event_type FROM run_events WHERE run_id=%s ORDER BY sequence_no", (result.run_id,))]
        assert stored == [e.event_type.value for e in expected]

    if name != "cancelled":
        again = ending(name)
        direct = await make_runner(again["client"], backend, tools=again["tools"], hook=again["hook"]).run(
            agent(*profile), "go", config(**again["fields"])
        )
        assert shape(direct) == shape(result), "Runner.run and RunHandle.result() disagree"


async def test_an_iterator_ends_when_the_terminal_event_cannot_be_recorded(backend, monkeypatch):
    sink_class = InMemoryEventSink if backend is None else PostgresEventStore
    real = sink_class.emit

    def refuses_terminal_events(sink, event_type, payload=None, **identifiers):
        if event_type.value in TERMINAL:
            raise RuntimeError("the store refuses the terminal event")
        return real(sink, event_type, payload, **identifiers)

    monkeypatch.setattr(sink_class, "emit", refuses_terminal_events)
    handle = await make_runner(Script(text()), backend).start(agent(), "go", config())
    stream = asyncio.ensure_future(collect(handle.events()))
    result = await asyncio.wait_for(handle.result(), 10)
    events = await asyncio.wait_for(stream, 10)
    assert events and events == list(result.events)
    assert events[-1].event_type is EventType.MODEL_CALLED


async def test_cancelling_a_task_that_awaits_result_or_iterates_events_leaves_the_run_to_complete(backend):
    hold = Hold()
    handle = await make_runner(Script(hold), backend).start(agent(), "go", config())
    waiting = asyncio.ensure_future(handle.result())
    streaming = asyncio.ensure_future(collect(handle.events()))
    try:
        await until(hold.entered.is_set, "the model call to begin")
        waiting.cancel()
        streaming.cancel()
        for task in (waiting, streaming):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await asyncio.sleep(SETTLE)
        assert handle.state().running is True, "cancelling a waiter cancelled the run"
    finally:
        hold.released.set()
    result = await asyncio.wait_for(handle.result(), 10)
    assert result.status is RunStatus.COMPLETED
    assert [e.event_type.value for e in await collect(handle.events())][-1] == "RunCompleted"


async def test_state_reports_progress_and_an_unknown_cost_while_a_model_call_is_in_flight(backend):
    hold = Hold()
    log = []
    handle = await make_runner(Script(calls(call("echo", "c1")), hold, log=log), backend, tools=[echo_tool(log)]).start(
        agent("echo"), "go", config()
    )
    try:
        await until(hold.entered.is_set, "the second model call to begin")
        state = handle.state()
        assert (state.running, state.status, state.usage, state.turns, state.cost_usd) == (True, None, CALL_USAGE, 2, None)
    finally:
        hold.released.set()
    result = await asyncio.wait_for(handle.result(), 10)
    final = handle.state()
    assert (final.running, final.status, final.usage, final.cost_usd, final.turns) == (
        False, RunStatus.COMPLETED, result.usage, result.cost_usd, 2
    )
    assert result.cost_usd == CALL_COST * 2


async def test_start_raises_configuration_errors_before_any_run_starts(backend):
    runner = make_runner(Script(), backend)
    with pytest.raises(ValueError, match="unknown model client"):
        await runner.start(agent(preferred_model="nope:x"), "go", config())
    with pytest.raises(ValueError, match="max_output_tokens"):
        await runner.start(agent(reasoning_effort="low"), "go", config())
    if backend is not None:
        assert query("SELECT count(*) FROM runs WHERE tenant_id=%s", (TENANT,)) == [(0,)]


# =================================================================================================
# FR-50, AC-40: cancellation at every point
# =================================================================================================


async def test_cancelled_before_its_first_model_call(backend):
    log = []
    client = Script(text(), log=log)
    handle = await make_runner(client, backend).start(agent(), "go", config())
    log.append(("cancel",))
    handle.cancel("stopped before it began")
    result = await asyncio.wait_for(handle.result(), 10)
    assert client.sends == 0
    kinds = assert_cancelled(result, backend, in_flight=False, reason="stopped before it began")
    assert kinds == ["RunStarted", "RunCancelled"]
    assert_nothing_started_after_cancel(log)


async def test_cancelled_during_a_model_call(backend):
    log = []
    hold = Hold()
    handle = await make_runner(Script(calls(call("echo", "c1")), hold, log=log), backend, tools=[echo_tool(log)]).start(
        agent("echo"), "go", config()
    )
    await until(hold.entered.is_set, "the second model call to begin")
    log.append(("cancel",))
    handle.cancel()
    handle.cancel("a second cancel changes nothing")
    result = await asyncio.wait_for(handle.result(), 10)
    assert_cancelled(result, backend, in_flight=True)
    assert result.usage == CALL_USAGE
    assert_nothing_started_after_cancel(log)


async def test_cancelled_while_its_model_call_waits_for_a_provider_slot(backend):
    log = []
    hold = Hold()
    client = PerRun({"A": [hold], "B": [text()]}, log)
    runner = Runner(
        {"m": client}, persistence=backend, model_registry=registry(),
        scheduler_limits=SchedulerLimits(provider_concurrency_limits={"m": 1}),
    )
    first = await runner.start(agent(), "A", config())
    try:
        await until(hold.entered.is_set, "run A to hold the only slot")
        second = await runner.start(agent(), "B", config())
        await asyncio.sleep(SETTLE)
        assert ("send", "B") not in log, "run B sent while run A held the only slot"
        log.append(("cancel",))
        second.cancel()
        result = await asyncio.wait_for(second.result(), 10)
    finally:
        hold.released.set()
    assert ("send", "B") not in log, "the call waiting for a slot was sent after cancellation"
    assert_cancelled(result, backend, in_flight=False)
    assert (await asyncio.wait_for(first.result(), 10)).status is RunStatus.COMPLETED


async def test_cancelled_during_a_parallel_batch_pairs_every_call_with_a_result(backend):
    log, gates, stopped = [], {}, []
    sessions = InMemorySessionStore()
    tools = [gated_tool(log, gates, stopped, "slow"), echo_tool(log, "after")]
    issued = [call("slow", "c1"), call("slow", "c2"), call("slow", "c3"), call("after", "c4")]
    handle = await make_runner(Script(calls(*issued), text(), log=log), backend, tools=tools, session_store=sessions).start(
        agent("slow", "after"), "go", config()
    )
    await until(lambda: sum(1 for entry in log if entry[0] == "tool") == 3, "the three safe calls to be running")
    log.append(("cancel",))
    handle.cancel()
    result = await asyncio.wait_for(handle.result(), 10)

    assert_cancelled(result, backend, in_flight=False)
    assert sorted(stopped) == ["c1", "c2", "c3"], "a running tool did not receive CancelledError"
    assert [r.tool_call_id for r in tool_results(sessions, backend, result.run_id)] == ["c1", "c2", "c3", "c4"]
    for key in ("c1", "c2", "c3"):
        assert_cancelled_call(result, sessions, backend, key, reached_step_6=True)
    assert_cancelled_call(result, sessions, backend, "c4", reached_step_6=False)
    assert ("tool", "c4") not in log
    assert_nothing_started_after_cancel(log)


async def test_cancelled_while_a_tool_call_waits_for_a_concurrency_slot(backend):
    log, gates, stopped = [], {}, []
    sessions = InMemorySessionStore()
    handle = await make_runner(
        Script(calls(call("slow", "c1"), call("slow", "c2")), text(), log=log), backend,
        tools=[gated_tool(log, gates, stopped)], session_store=sessions,
    ).start(agent("slow"), "go", config(scheduler_limits=SchedulerLimits(max_concurrent_tools=1)))
    await until(lambda: ("tool", "c1") in log, "the first call to be running")
    await asyncio.sleep(SETTLE)
    assert ("tool", "c2") not in log
    log.append(("cancel",))
    handle.cancel()
    result = await asyncio.wait_for(handle.result(), 10)

    assert_cancelled(result, backend, in_flight=False)
    assert stopped == ["c1"]
    assert_cancelled_call(result, sessions, backend, "c1", reached_step_6=True)
    assert_cancelled_call(result, sessions, backend, "c2", reached_step_6=False)
    assert_nothing_started_after_cancel(log)


@pytest.mark.parametrize("which", ["an event emission", "a session append", "the final session append"])
async def test_cancelled_while_a_store_write_is_held_inside_the_store(backend, which, monkeypatch):
    """"the final session append" is the assistant message of a run answering in text:
    no checkpoint follows that write, so only the one before the terminal event
    stops the run (mutant H13 survived without this case)."""
    log = []
    sessions = InMemorySessionStore()
    hold = HoldInStore()
    if which == "an event emission":
        store_class, method = (InMemoryEventSink if backend is None else PostgresEventStore), "emit"
        predicate = lambda event_type, *args, **kwargs: event_type is EventType.MODEL_CALLED  # noqa: E731
    else:
        store_class, method = (InMemorySessionStore if backend is None else PostgresSessionStore), "append"
        predicate = lambda run_id, message, *args, **kwargs: message.role is Role.ASSISTANT  # noqa: E731
    monkeypatch.setattr(store_class, method, hold.wrap(getattr(store_class, method), predicate))

    if which == "the final session append":
        client, tools, profile = Script(text(), log=log), [], ()
    else:
        client, tools, profile = Script(calls(call("echo", "c1")), text(), log=log), [echo_tool(log)], ("echo",)
    handle = await make_runner(client, backend, tools=tools, session_store=sessions).start(
        agent(*profile), "go", config()
    )
    try:
        await until(hold.entered.is_set, f"{which} to be held")
        log.append(("cancel",))
        handle.cancel()
        await asyncio.sleep(SETTLE)
        assert handle.state().running is True, "the run ended while its store call was still held"
    finally:
        hold.release.set()
    result = await asyncio.wait_for(handle.result(), 10)

    kinds = assert_cancelled(result, backend, in_flight=False)
    assert kinds.count("ModelCalled") == 1, "the held emission was not completed"
    assert [m.role for m in history(sessions, backend, result.run_id)] == [Role.USER, Role.ASSISTANT]
    assert_nothing_started_after_cancel(log)


async def test_cancelled_between_turns(backend):
    log, holder = [], {}

    class CancelsBeforeTheSecondTurn(RuntimeHook):
        def before_model(self, request):
            if any(message.role is Role.TOOL for message in request.messages):
                log.append(("cancel",))
                holder["handle"].cancel("between turns")
            return HookOutcome()

    client = Script(calls(call("echo", "c1")), text(), log=log)
    handle = await make_runner(client, backend, tools=[echo_tool(log)], hook=CancelsBeforeTheSecondTurn()).start(
        agent("echo"), "go", config()
    )
    holder["handle"] = handle
    result = await asyncio.wait_for(handle.result(), 10)
    assert client.sends == 1
    assert_cancelled(result, backend, in_flight=False, reason="between turns")
    assert_nothing_started_after_cancel(log)


async def test_a_cancel_that_loses_the_race_to_the_terminal_write_changes_nothing(backend, monkeypatch):
    hold = HoldInStore()
    sink_class = InMemoryEventSink if backend is None else PostgresEventStore
    monkeypatch.setattr(
        sink_class, "emit",
        hold.wrap(sink_class.emit, lambda event_type, *args, **kwargs: event_type is EventType.RUN_COMPLETED),
    )
    handle = await make_runner(Script(text()), backend).start(agent(), "go", config())
    try:
        await until(hold.entered.is_set, "the terminal event to be held")
        handle.cancel()
        await asyncio.sleep(SETTLE)
    finally:
        hold.release.set()
    result = await asyncio.wait_for(handle.result(), 10)
    assert result.status is RunStatus.COMPLETED and result.output == "done"
    assert "RunCancelled" not in [e.event_type.value for e in result.events]
    if backend is not None:
        assert query("SELECT status FROM runs WHERE run_id=%s", (result.run_id,)) == [("completed",)]


async def test_a_cancel_after_the_run_ended_changes_nothing(backend):
    handle = await make_runner(Script(text()), backend).start(agent(), "go", config())
    result = await asyncio.wait_for(handle.result(), 10)
    before = list(result.events)
    handle.cancel("too late")
    await asyncio.sleep(SETTLE)
    assert await handle.result() is result and result.status is RunStatus.COMPLETED
    assert await asyncio.wait_for(collect(handle.events()), 10) == before
    assert handle.state().status is RunStatus.COMPLETED
    if backend is not None:
        assert query("SELECT status FROM runs WHERE run_id=%s", (result.run_id,)) == [("completed",)]
        assert query("SELECT count(*) FROM run_events WHERE run_id=%s", (result.run_id,)) == [(len(before),)]


async def test_cancelling_the_task_that_awaits_runner_run_records_the_run_and_raises(backend):
    log = []
    hold = Hold()
    task = asyncio.ensure_future(
        make_runner(Script(calls(call("echo", "c1")), hold, log=log), backend, tools=[echo_tool(log)]).run(
            agent("echo"), "go", config()
        )
    )
    await until(hold.entered.is_set, "the second model call to begin")
    task.cancel()
    try:
        # A bounded wait that does not cancel the task a second time. wait_for would,
        # and a run() that never cancels its run absorbs that second cancel and waits
        # for ever: mutant H15 was caught only by the harness's 300 s timeout.
        done, _ = await asyncio.wait({task}, timeout=10)
        assert task in done, "Runner.run did not end after its caller was cancelled"
    finally:
        hold.released.set()
        if not task.done():
            await asyncio.wait({task}, timeout=10)
    with pytest.raises(asyncio.CancelledError):
        task.result()
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not leftover, leftover
    if backend is not None:
        [(run_id, status, prompt, cost, ended)] = query(
            "SELECT run_id, status, prompt_tokens, cost_usd, completed_at IS NOT NULL FROM runs WHERE tenant_id=%s", (TENANT,)
        )
        assert (status, prompt, cost, ended) == ("cancelled", 10, None, True)
        kinds = [row[0] for row in query("SELECT event_type FROM run_events WHERE run_id=%s ORDER BY sequence_no", (run_id,))]
        assert kinds.count("RunCancelled") == 1 and kinds[-1] == "RunCancelled", kinds


async def test_a_cancelled_error_from_a_collaborator_records_the_run_and_still_raises(backend):
    log = []
    runner = make_runner(Script(calls(call("echo", "c1")), asyncio.CancelledError(), log=log), backend, tools=[echo_tool(log)])
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(runner.run(agent("echo"), "go", config()), 10)
    if backend is not None:
        [(run_id, status, prompt, cost)] = query(
            "SELECT run_id, status, prompt_tokens, cost_usd FROM runs WHERE tenant_id=%s", (TENANT,)
        )
        assert (status, prompt, cost) == ("cancelled", 10, None)
        kinds = [row[0] for row in query("SELECT event_type FROM run_events WHERE run_id=%s ORDER BY sequence_no", (run_id,))]
        assert kinds[-1] == "RunCancelled", kinds


@pytest.mark.parametrize(
    "reason, recorded",
    [(None, "cancelled"), ("stopped by the operator", "stopped by the operator"), ("bad\x00reason", "cancelled")],
    ids=["no reason", "a reason", "an unstorable reason"],
)
async def test_the_cancellation_reason_is_recorded_when_it_can_be_stored(backend, reason, recorded):
    hold = Hold()
    handle = await make_runner(Script(hold), backend).start(agent(), "go", config())
    await until(hold.entered.is_set, "the model call to begin")
    handle.cancel(reason)
    result = await asyncio.wait_for(handle.result(), 10)
    assert_cancelled(result, backend, in_flight=True, reason=recorded)


@pytest.mark.parametrize("debug", [False, True], ids=["normal mode", "asyncio debug mode"])
def test_cancel_called_from_another_thread_takes_effect_promptly(backend, debug):
    """C1, M12 review round 1 (KNOWLEDGE-a23d69bd). RunControl.request called
    Task.cancel directly, which asyncio allows only on the loop's own thread. Called
    from another thread, the cancel took effect only when the loop next woke for
    some other reason, so a run waiting on a model call was not abandoned; under
    asyncio debug mode Task.cancel raised RuntimeError and the run could never end.

    The thread calls cancel() only after the loop is already waiting with nothing
    scheduled but a 5 s bound, so nothing else wakes it. The scenario runs on its
    own event loop in a daemon thread: a run that can never end then fails this
    test at the join, where in the test's own loop it hung the session at teardown
    (the first red run of this test was killed after 300 s)."""
    raised, outcome = [], {}

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_debug(debug)
        hold = Hold()
        handle = await make_runner(Script(hold), backend).start(agent(), "go", config())
        await until(hold.entered.is_set, "the model call to begin")

        def cancel_from_another_thread():
            time.sleep(0.2)
            try:
                handle.cancel("cancelled from another thread")
            except BaseException as exc:  # noqa: BLE001 - what the calling thread saw is the finding
                raised.append(type(exc).__name__)

        waiting = asyncio.ensure_future(handle.result())
        canceller = threading.Thread(target=cancel_from_another_thread)
        canceller.start()
        done, _ = await asyncio.wait({waiting}, timeout=5)
        canceller.join(5)
        outcome["ended"] = waiting in done
        if outcome["ended"]:
            outcome["result"] = waiting.result()
        else:
            hold.released.set()
            await asyncio.wait({waiting}, timeout=5)

    worker = threading.Thread(target=lambda: asyncio.run(scenario()), daemon=True)
    worker.start()
    worker.join(30)
    assert not raised, f"cancel() raised on the calling thread: {raised}"
    assert outcome.get("ended"), "the run did not end within 5 s of cancel() being called from another thread"
    assert_cancelled(outcome["result"], backend, in_flight=True, reason="cancelled from another thread")
