"""M11 gate: concurrency foundations (FR-43..FR-47, NFR-15..NFR-18, AC-34..AC-38, AC-43).

Written before the implementation, against the owner-approved specification of
2026-09-14. Wherever an order or a count is claimed it is taken with gates, not
sleeps: a tool or a model client blocks on an asyncio.Event the test opens, so
"at most four at once" is counted while four calls are provably waiting and a
fifth has had time to start.

The in-memory tests need no network. The persisted ones need DATABASE_URL and
fail rather than skip without it; every row they write is removed afterwards,
and tests/conftest.py checks the whole session (AC-44).

Names M11 adds (SchedulerLimits, ToolSpec.concurrency_safe,
set_file_tool_threads) are reached through their modules or passed as keyword
arguments, so before the implementation each test fails on its own rather than
the file failing to import.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import hashlib
import importlib
import inspect
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys
import textwrap
import threading
import time
import uuid

import psycopg
import pytest
from dotenv import load_dotenv

import agentsdk
from agentsdk import AgentSpec, Persistence, RunConfig, Runner, RunStatus, migrate, postgres
from agentsdk.config import normalise_database_url
from agentsdk.errors import ToolError
from agentsdk.events import EventType
from agentsdk.executor import ToolExecutor
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.migrate import apply_migrations
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.outcomes import Failed
from agentsdk.permissions import AllowlistPermissionChecker, Decision, PermissionResult
from agentsdk.postgres import SCHEMA_PATH, RunScope
from agentsdk.primitives import Message, Role, ToolCall, unstorable_reason
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import Tool, ToolOutput, ToolRegistry, ToolSpec

load_dotenv()

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
REPO = pathlib.Path(__file__).resolve().parents[1]
TENANT, PROJECT = "SYN-m11", "p-m11"
# Time given to a call that is NOT held by a limit to start, before the test
# counts. Every such call is ready on the event loop well inside it.
SETTLE = 0.1
FILE_TOOL_THREADS = 4  # P2-D6
FILE_THREAD_PREFIX = "agentsdk-file-tool-"
KEY_SCHEMA = {
    "type": "object",
    "properties": {"key": {"type": "string"}},
    "required": ["key"],
    "additionalProperties": False,
}
SIX = [f"c{i}" for i in range(1, 7)]


# --- shared helpers -------------------------------------------------------------------------------


def limits(**fields):
    return agentsdk.SchedulerLimits(**fields)


def builtin():
    return importlib.import_module("agentsdk.builtin_tools")


def spec(name, *, safe=False, **fields):
    if safe:
        fields["concurrency_safe"] = True
    return ToolSpec(name=name, description=f"The {name} tool.", input_schema=KEY_SCHEMA, **fields)


def text(content="done"):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, content=content),
        stop_reason=StopReason.END_TURN,
        usage=Usage(1, 1, 2),
    )


def calls(*tool_calls):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, tool_calls=tuple(tool_calls)),
        stop_reason=StopReason.TOOL_CALLS,
        usage=Usage(1, 1, 2),
    )


def call(name, key):
    return ToolCall(id=key, name=name, arguments={"key": key})


def rank(key):
    return int(re.sub(r"\D", "", key))


def config(**fields):
    return RunConfig(tenant_id=TENANT, project_id=PROJECT, **fields)


def agent(*profile, **fields):
    return AgentSpec(id="m11", instructions="go", tool_profile=tuple(profile), **fields)


class Scripted:
    """Replays model responses, then answers done."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return self.script.pop(0) if self.script else text()


async def until(predicate, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


def drop_runs(tenant):
    with psycopg.connect(DSN, autocommit=True) as conn:
        ids = [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE tenant_id=%s", (tenant,)).fetchall()]
        for table in ("run_events", "messages", "execution_manifests"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))


@pytest.fixture(autouse=True)
def remove_what_the_test_wrote():
    yield
    if DSN:
        drop_runs(TENANT)


@pytest.fixture(params=["memory", "postgres"])
def backend(request):
    """None for the in-memory stores, or Persistence on Postgres."""
    if request.param == "postgres":
        assert DSN, "the persisted half needs DATABASE_URL and fails rather than skips"
        return Persistence.postgres(DSN)
    return None


def history(sessions, persistence, run_id):
    if persistence is None:
        return sessions.history(run_id)
    scope = RunScope(run_id=run_id, tenant_id=TENANT, project_id=PROJECT)
    return persistence.session_store_for(scope).history(run_id)


def tool_results(sessions, persistence, run_id):
    return [r for m in history(sessions, persistence, run_id) if m.role is Role.TOOL for r in m.tool_results]


def assert_events_well_formed(result, persistence):
    """sequence_no unique and contiguous, and every ToolCalled envelope names its call (FR-44)."""
    numbers = sorted(e.sequence_no for e in result.events)
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    tool_called = [e for e in result.events if e.event_type is EventType.TOOL_CALLED]
    assert tool_called, "no ToolCalled event, so nothing here was checked"
    for event in tool_called:
        assert event.tool_call_id is not None and event.tool_call_id == event.payload["tool_call_id"], (
            event.tool_call_id,
            event.payload,
        )
    if persistence is not None:
        rows = query(
            "SELECT sequence_no, event_type, tool_call_id, payload->>'tool_call_id' FROM run_events"
            " WHERE run_id=%s ORDER BY sequence_no",
            (result.run_id,),
        )
        assert [row[0] for row in rows] == list(range(1, len(result.events) + 1))
        for _, kind, envelope, in_payload in rows:
            if kind == "ToolCalled":
                assert envelope is not None and envelope == in_payload, (envelope, in_payload)


class Probe:
    """Where each gated call is. A call enters, waits on its own gate, and leaves."""

    def __init__(self):
        self.inside = set()
        self.peak = 0
        self.log = []
        self.gates = {}

    def gate(self, key):
        return self.gates.setdefault(key, asyncio.Event())

    def enter(self, key):
        self.log.append(("enter", key))
        self.inside.add(key)
        self.peak = max(self.peak, len(self.inside))

    def leave(self, key):
        self.inside.discard(key)
        self.log.append(("leave", key))

    def release_all(self):
        for gate in self.gates.values():
            gate.set()

    def entered(self):
        return [key for kind, key in self.log if kind == "enter"]


def gated_tool(probe, name="gated", *, safe=True, **fields):
    async def fn(key):
        probe.enter(key)
        try:
            await probe.gate(key).wait()
        finally:
            probe.leave(key)
        return f"{name} {key}"

    return Tool(spec=spec(name, safe=safe, **fields), fn=fn)


async def drive(run, probe):
    """Run `run` to its end, opening gates newest-first once every call that can start has.

    Newest-first, so calls finish in the reverse of their issue order wherever
    more than one is waiting, and an order of results that merely follows
    completion is visible.
    """
    task = asyncio.ensure_future(run)
    try:
        while True:
            await until(lambda: probe.inside or task.done(), "a gated call to start or the run to end")
            if task.done():
                return task.result()
            await asyncio.sleep(SETTLE)
            for key in sorted(probe.inside, key=rank, reverse=True):
                probe.gate(key).set()
                await until(lambda key=key: key not in probe.inside, f"{key} to finish")
    finally:
        probe.release_all()
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


# =================================================================================================
# FR-43, AC-34: SchedulerLimits
# =================================================================================================


class _Key(str):
    pass


BAD_COUNTS = {
    "a bool": True,
    "a float": 4.0,
    "a str": "4",
    "None": None,
    "zero": 0,
    "negative": -1,
    "past the INTEGER column": 2**31,
}
BAD_KEYS = {
    "not a str": 1,
    "a str subclass": _Key("tool"),
    "empty": "",
    "holding a NUL": "to\x00ol",
    "holding a lone surrogate": "to\ud800ol",
}
NOT_MAPPINGS = {"a list of pairs": [("tool", 1)], "a str": "tool", "an int": 3}


def invalid_limit_cases():
    for label, value in BAD_COUNTS.items():
        yield pytest.param({"max_concurrent_tools": value}, "max_concurrent_tools", id=f"max_concurrent_tools is {label}")
    for field in ("tool_concurrency_limits", "provider_concurrency_limits"):
        for label, value in NOT_MAPPINGS.items():
            yield pytest.param({field: value}, field, id=f"{field} is {label}")
        for label, key in BAD_KEYS.items():
            yield pytest.param({field: {key: 1}}, field, id=f"{field} has a key {label}")
        for label, value in BAD_COUNTS.items():
            yield pytest.param({field: {"tool": value}}, field, id=f"{field} has a value that is {label}")


@pytest.mark.parametrize("fields, name", list(invalid_limit_cases()))
def test_every_invalid_scheduler_limit_is_refused_at_construction_by_field_name(fields, name):
    with pytest.raises(ValueError, match=name):
        limits(**fields)


def test_scheduler_limits_accept_the_largest_int_a_column_holds():
    ceiling = 2**31 - 1
    made = limits(
        max_concurrent_tools=ceiling,
        tool_concurrency_limits={"tool": ceiling},
        provider_concurrency_limits={"gw": ceiling},
    )
    assert made.max_concurrent_tools == ceiling


def test_scheduler_limits_are_frozen_and_hold_immutable_copies_of_their_mappings():
    default = limits()
    assert (default.max_concurrent_tools, dict(default.tool_concurrency_limits), dict(default.provider_concurrency_limits)) == (
        4,
        {},
        {},
    ), "P2-D5: 4 per run, no per-tool and no per-provider limit"
    source = {"tool": 2}
    made = limits(tool_concurrency_limits=source, provider_concurrency_limits={"gw": 3})
    source["tool"] = 99
    source["other"] = 1
    assert dict(made.tool_concurrency_limits) == {"tool": 2}, "the caller's dict is still the limit"
    with pytest.raises(TypeError):
        made.tool_concurrency_limits["tool"] = 5
    with pytest.raises(TypeError):
        made.provider_concurrency_limits["gw"] = 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        made.max_concurrent_tools = 9
    assert "SchedulerLimits" in agentsdk.__all__


def test_provider_limits_belong_to_a_runner_and_must_name_one_of_its_clients():
    with pytest.raises(ValueError, match="provider_concurrency_limits"):
        config(scheduler_limits=limits(provider_concurrency_limits={"m": 1}))
    with pytest.raises(ValueError, match="provider_concurrency_limits"):
        Runner({"m": Scripted()}, scheduler_limits=limits(provider_concurrency_limits={"other": 1}))
    Runner({"m": Scripted()}, scheduler_limits=limits(provider_concurrency_limits={"m": 1}))
    with pytest.raises(ValueError, match="scheduler_limits"):
        config(scheduler_limits={"max_concurrent_tools": 2})
    with pytest.raises(ValueError, match="scheduler_limits"):
        Runner({"m": Scripted()}, scheduler_limits={"max_concurrent_tools": 2})


async def test_a_runs_effective_limits_are_recorded_in_its_manifest_on_postgres():
    assert DSN, "DATABASE_URL must be set"
    persistence = Persistence.postgres(DSN)
    on_runner = limits(max_concurrent_tools=3, tool_concurrency_limits={"gated": 2}, provider_concurrency_limits={"m": 2})
    cases = {
        "no limits anywhere": (None, None, {"max_concurrent_tools": 4, "tool_concurrency_limits": {}, "provider_concurrency_limits": {}}),
        "the runner's": (on_runner, None, {"max_concurrent_tools": 3, "tool_concurrency_limits": {"gated": 2}, "provider_concurrency_limits": {"m": 2}}),
        "the run's replace the runner's per-run and per-tool limits as a whole": (
            on_runner,
            limits(max_concurrent_tools=6),
            {"max_concurrent_tools": 6, "tool_concurrency_limits": {}, "provider_concurrency_limits": {"m": 2}},
        ),
    }
    for label, (runner_limits, run_limits, expected) in cases.items():
        extra = {} if runner_limits is None else {"scheduler_limits": runner_limits}
        runner = Runner({"m": Scripted(text())}, persistence=persistence, **extra)
        result = await runner.run(agent(), "go", config() if run_limits is None else config(scheduler_limits=run_limits))
        assert result.status is RunStatus.COMPLETED, (label, result.error)
        [(stored,)] = query("SELECT scheduler_limits FROM execution_manifests WHERE run_id=%s", (result.run_id,))
        assert stored == expected, label


class Namespace:
    """A throwaway schema, as in M7 and M9: the live database is already migrated."""

    def __init__(self, baseline=True):
        self.baseline = baseline
        self.name = "m11_" + uuid.uuid4().hex[:8]
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
        return set(
            query(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                (self.name, table),
            )
        )


def test_migration_0004_brings_a_database_at_0003_forward_and_leaves_existing_manifests_null(monkeypatch):
    assert DSN, "DATABASE_URL must be set"
    real = migrate.discover()
    assert "0004" in [version for version, _ in real], "migration 0004 is not on disk"
    tables = ("runs", "messages", "run_events", "execution_manifests")

    with Namespace() as ns:
        monkeypatch.setattr(migrate, "discover", lambda: [m for m in real if m[0] <= "0003"])
        assert apply_migrations(ns.dsn) == ["0002", "0003"]
        run_id = str(uuid.uuid4())
        with psycopg.connect(ns.dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO runs (run_id, tenant_id, project_id, agent_spec_id, status, max_turns)"
                " VALUES (%s, 't', 'p', 's', 'completed', 3)",
                (run_id,),
            )
            conn.execute(
                "INSERT INTO execution_manifests (run_id, tenant_id, project_id, sdk_version,"
                " agent_spec_hash, instructions_hash) VALUES (%s, 't', 'p', 'v', 'h', 'h')",
                (run_id,),
            )
        assert "scheduler_limits" not in {name for name, _ in ns.columns("execution_manifests")}

        monkeypatch.setattr(migrate, "discover", lambda: real)
        assert apply_migrations(ns.dsn) == [v for v, _ in real if v > "0003"]
        assert ("scheduler_limits", "jsonb") in ns.columns("execution_manifests")
        with psycopg.connect(ns.dsn) as conn:
            [(stored,)] = conn.execute("SELECT scheduler_limits FROM execution_manifests").fetchall()
        assert stored is None, "an existing manifest was given limits it never ran with"

        assert apply_migrations(ns.dsn) == [], "a second application changed something"
        upgraded = {table: ns.columns(table) for table in tables}

    with Namespace(baseline=False) as fresh:
        apply_migrations(fresh.dsn, baseline=SCHEMA_PATH)
        assert {table: fresh.columns(table) for table in tables} == upgraded


# =================================================================================================
# FR-44, AC-35: parallel tool calls
# =================================================================================================


def test_concurrency_safe_defaults_to_false_and_enters_the_hash_only_when_true():
    base = ToolSpec(name="t", description="d", input_schema=KEY_SCHEMA)
    assert base.concurrency_safe is False
    before_m11 = hashlib.sha256(
        json.dumps(
            {"name": "t", "description": "d", "input_schema": KEY_SCHEMA, "risk_class": "read_only"},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert base.schema_hash() == before_m11, "a tool that declares nothing changed its hash"
    assert ToolSpec(name="t", description="d", input_schema=KEY_SCHEMA, concurrency_safe=False).schema_hash() == before_m11
    assert ToolSpec(name="t", description="d", input_schema=KEY_SCHEMA, concurrency_safe=True).schema_hash() != before_m11
    for bad in (1, "yes", None):
        with pytest.raises(ToolError, match="concurrency_safe"):
            ToolSpec(name="t", description="d", input_schema=KEY_SCHEMA, concurrency_safe=bad)


def test_the_builtin_read_only_tools_declare_concurrency_safe(tmp_path):
    module = builtin()

    class Backend:
        async def search(self, query, max_results):
            return []

    tools = [
        module.read_file_tool(tmp_path),
        module.list_directory_tool(tmp_path),
        module.glob_tool(tmp_path),
        module.grep_tool(tmp_path),
        module.fetch_tool([]),
        module.web_search_tool(Backend()),
    ]
    assert {tool.name: tool.spec.concurrency_safe for tool in tools} == {tool.name: True for tool in tools}


PEAK_CASES = {
    "default limits": (None, None, 4),
    "max_concurrent_tools=6": (None, dict(max_concurrent_tools=6), 6),
    "the tool limited to 2": (None, dict(tool_concurrency_limits={"gated": 2}), 2),
    "max_concurrent_tools=1": (None, dict(max_concurrent_tools=1), 1),
    "the runner's limits when the run sets none": (dict(max_concurrent_tools=3), None, 3),
    "a run's limits replace the runner's as a whole": (
        dict(max_concurrent_tools=2, tool_concurrency_limits={"gated": 1}),
        dict(max_concurrent_tools=5),
        5,
    ),
}


@pytest.mark.parametrize("case", PEAK_CASES)
async def test_parallel_calls_never_exceed_the_limits_in_force(case):
    runner_limits, run_limits, expected = PEAK_CASES[case]
    probe = Probe()
    sessions = InMemorySessionStore()
    extra = {} if runner_limits is None else {"scheduler_limits": limits(**runner_limits)}
    runner = Runner(
        {"m": Scripted(calls(*(call("gated", key) for key in SIX)), text())},
        tools=[gated_tool(probe)],
        session_store=sessions,
        **extra,
    )
    run_config = config() if run_limits is None else config(scheduler_limits=limits(**run_limits))
    result = await drive(runner.run(agent("gated"), "go", run_config), probe)

    assert result.status is RunStatus.COMPLETED, result.error
    assert probe.peak == expected, f"{probe.peak} calls ran at once where the limit in force is {expected}"
    assert sorted(probe.entered(), key=rank) == SIX
    if expected == 1:
        assert probe.entered() == SIX, "with one slot, calls must be invoked in the order the model issued them"
    else:
        finished = [e.payload["tool_call_id"] for e in result.events if e.event_type is EventType.TOOL_CALLED]
        assert finished != SIX, "the calls finished in issue order, so the order of the results proves nothing"
    assert [r.tool_call_id for r in tool_results(sessions, None, result.run_id)] == SIX
    assert_events_well_formed(result, None)


async def test_results_sit_in_issue_order_whatever_order_the_calls_finish_in(backend):
    probe = Probe()
    sessions = InMemorySessionStore()
    runner = Runner(
        {"m": Scripted(calls(*(call("gated", key) for key in SIX)), text())},
        tools=[gated_tool(probe)],
        session_store=sessions,
        persistence=backend,
    )
    result = await drive(runner.run(agent("gated"), "go", config(scheduler_limits=limits(max_concurrent_tools=6))), probe)

    assert result.status is RunStatus.COMPLETED, result.error
    finished = [e.payload["tool_call_id"] for e in result.events if e.event_type is EventType.TOOL_CALLED]
    assert finished == list(reversed(SIX)), f"the gates were opened newest first, but calls finished {finished}"
    stored = tool_results(sessions, backend, result.run_id)
    assert [(r.tool_call_id, r.content) for r in stored] == [(key, f"gated {key}") for key in SIX]
    assert_events_well_formed(result, backend)


async def test_calls_to_tools_that_do_not_declare_concurrency_safe_run_one_at_a_time_in_issue_order():
    probe = Probe()
    tools = [gated_tool(probe, "plain", safe=False), gated_tool(probe, "readonly", safe=False, read_only=True)]
    names = ["plain", "readonly", "plain", "readonly", "readonly", "plain"]
    runner = Runner({"m": Scripted(calls(*(call(n, k) for n, k in zip(names, SIX))), text())}, tools=tools)
    result = await drive(
        runner.run(agent("plain", "readonly"), "go", config(scheduler_limits=limits(max_concurrent_tools=6))), probe
    )
    assert result.status is RunStatus.COMPLETED, result.error
    assert probe.peak == 1, "a tool that only declares read_only ran in parallel"
    assert probe.entered() == SIX


async def test_an_unsafe_call_splits_the_batch_and_each_batch_prepares_every_call_before_running_any():
    probe = Probe()
    log = probe.log
    threads = []

    class Watching(RuntimeHook):
        def before_tool(self, tool_call):
            log.append(("before_tool", tool_call.id))
            threads.append(threading.get_ident())
            return HookOutcome()

        def after_tool(self, result):
            log.append(("after_tool", result.tool_call_id))
            threads.append(threading.get_ident())
            return HookOutcome()

    class Checking:
        def check(self, tool_call, principal_context=None):
            log.append(("permission", tool_call.id))
            threads.append(threading.get_ident())
            return PermissionResult(Decision.ALLOW, "allowed")

    tools = [gated_tool(probe, "gated"), gated_tool(probe, "unsafe", safe=False)]
    issued = [call("gated", "c1"), call("gated", "c2"), call("unsafe", "c3"), call("gated", "c4")]
    runner = Runner({"m": Scripted(calls(*issued), text())}, tools=tools, hook=Watching())
    result = await drive(runner.run(agent(permission_policy=Checking()), "go", config()), probe)

    assert result.status is RunStatus.COMPLETED, result.error
    at = {entry: index for index, entry in enumerate(log)}
    assert max(at[(step, key)] for step in ("permission", "before_tool") for key in ("c1", "c2")) < min(
        at[("enter", "c1")], at[("enter", "c2")]
    ), f"a call of the first batch ran before its sibling was prepared: {log}"
    first_batch_done = max(at[("after_tool", "c1")], at[("after_tool", "c2")])
    assert at[("permission", "c3")] > first_batch_done, "the unsafe call was prepared before the batch before it finished"
    assert at[("enter", "c3")] > first_batch_done, "the unsafe call ran before both safe calls finished"
    assert at[("permission", "c4")] > at[("after_tool", "c3")] and at[("enter", "c4")] > at[("after_tool", "c3")], (
        "the last safe call ran before the unsafe one finished"
    )
    assert probe.peak == 2
    assert set(threads) == {threading.get_ident()}, "a hook or permission checker was called off the event loop's thread"


async def test_a_failing_call_leaves_the_results_of_its_siblings_unchanged():
    async def ok(key):
        await asyncio.sleep(0)
        return f"ok {key}"

    async def raises(key):
        raise RuntimeError(f"boom {key}")

    async def slow(key):
        await asyncio.sleep(10)
        return "never"

    tools = [
        Tool(spec=spec("ok", safe=True), fn=ok),
        Tool(spec=spec("raises", safe=True), fn=raises),
        Tool(spec=spec("slow", safe=True, timeout_seconds=0.05), fn=slow),
    ]
    issued = [call("ok", "c1"), call("raises", "c2"), call("slow", "c3"), call("nope", "c4"), call("ok", "c5"), call("raises", "c6")]
    profile = ("ok", "raises", "slow", "nope")

    async def outcomes(*script):
        sessions = InMemorySessionStore()
        runner = Runner({"m": Scripted(*script, text())}, tools=tools, session_store=sessions)
        result = await runner.run(agent(*profile), "go", config(max_turns=10))
        assert result.status is RunStatus.COMPLETED, result.error
        assert_events_well_formed(result, None)
        return {r.tool_call_id: (r.content, r.is_error, r.provenance) for r in tool_results(sessions, None, result.run_id)}

    together = await outcomes(calls(*issued))
    alone = await outcomes(*(calls(c) for c in issued))
    assert together == alone
    assert together["c1"][:2] == ("ok c1", False) and together["c5"][:2] == ("ok c5", False)
    assert together["c3"][1] is True and "ToolTimeout" in together["c3"][0]


async def test_a_call_held_behind_the_limit_for_longer_than_its_timeout_still_completes():
    async def first(key):
        await asyncio.sleep(0.4)
        return "first done"

    async def second(key):
        return "second done"

    tools = [
        Tool(spec=spec("first", safe=True), fn=first),
        Tool(spec=spec("second", safe=True, timeout_seconds=0.1), fn=second),
    ]
    sessions = InMemorySessionStore()
    runner = Runner({"m": Scripted(calls(call("first", "c1"), call("second", "c2")), text())}, tools=tools, session_store=sessions)
    result = await runner.run(agent("first", "second"), "go", config(scheduler_limits=limits(max_concurrent_tools=1)))
    assert result.status is RunStatus.COMPLETED, result.error
    stored = {r.tool_call_id: (r.content, r.is_error) for r in tool_results(sessions, None, result.run_id)}
    assert stored["c2"] == ("second done", False), "the wait for a slot was counted against the tool's timeout"


class Escape(BaseException):
    """Not an Exception, so no boundary may absorb it."""


async def test_a_base_exception_escaping_one_call_cancels_and_awaits_its_siblings():
    probe = Probe()
    cleaned = []

    async def holder(key):
        probe.enter(key)
        try:
            await probe.gate(key).wait()
        except asyncio.CancelledError:
            # Cleanup that takes time: only a sibling that is awaited gets to finish it.
            await asyncio.sleep(0.05)
            cleaned.append(key)
            raise
        finally:
            probe.leave(key)
        return "released"

    async def escapes(key):
        await until(lambda: len(probe.inside) == 2, "both siblings to be waiting")
        raise Escape(key)

    tools = [Tool(spec=spec("holder", safe=True), fn=holder), Tool(spec=spec("escapes", safe=True), fn=escapes)]
    issued = [call("holder", "c1"), call("escapes", "c2"), call("holder", "c3")]
    runner = Runner({"m": Scripted(calls(*issued), text())}, tools=tools)
    try:
        with pytest.raises(Escape):
            await asyncio.wait_for(runner.run(agent("holder", "escapes"), "go", config()), 10)
        assert sorted(cleaned) == ["c1", "c3"], f"siblings cleaned up: {cleaned}; one was left running or not awaited"
        assert not probe.inside
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert not leftover, leftover
    finally:
        probe.release_all()


async def test_a_cancelled_error_escaping_one_call_cancels_and_awaits_its_siblings_at_once():
    """R1, M11 review round 1. CancelledError is a BaseException, and FR-44 says
    its siblings are cancelled and awaited. asyncio.wait(FIRST_EXCEPTION) does not
    wake for a task that ends cancelled, so the siblings ran to their own end: a
    sibling with no timeout held the run open without bound, and kept running
    after the run had failed. A tool meets this by awaiting a helper future that
    something else cancels."""
    probe = Probe()
    cleaned = []

    async def holder(key):
        probe.enter(key)
        try:
            await probe.gate(key).wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            cleaned.append(key)
            raise
        finally:
            probe.leave(key)
        return "released"

    async def awaits_a_cancelled_helper(key):
        await until(lambda: len(probe.inside) == 2, "both siblings to be waiting")
        loop = asyncio.get_running_loop()
        helper = loop.create_future()
        loop.call_soon(helper.cancel)
        await helper
        return "never"

    tools = [
        Tool(spec=spec("holder", safe=True, timeout_seconds=None), fn=holder),
        Tool(spec=spec("helper", safe=True), fn=awaits_a_cancelled_helper),
    ]
    issued = [call("holder", "c1"), call("helper", "c2"), call("holder", "c3")]
    runner = Runner({"m": Scripted(calls(*issued), text())}, tools=tools)
    started = time.monotonic()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner.run(agent("holder", "helper"), "go", config()), 5)
        assert time.monotonic() - started < 2, "the run waited for its siblings instead of cancelling them"
        assert sorted(cleaned) == ["c1", "c3"], f"siblings cleaned up: {cleaned}"
        assert not probe.inside
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert not leftover, leftover
    finally:
        probe.release_all()


async def test_cancelling_a_batch_twice_still_awaits_every_call_before_the_run_ends():
    """R2, M11 review round 1. A second cancel that landed while the batch waited
    for its cancelled calls ended the run at once, and the calls finished their
    cleanup after the run had already ended."""
    probe = Probe()
    cleaned = []

    async def holder(key):
        probe.enter(key)
        try:
            await probe.gate(key).wait()
        except asyncio.CancelledError:
            # Long enough for the second cancel to land while it runs.
            await asyncio.sleep(0.3)
            cleaned.append(key)
            raise
        finally:
            probe.leave(key)
        return "released"

    keys = ["c1", "c2", "c3"]
    runner = Runner(
        {"m": Scripted(calls(*(call("holder", key) for key in keys)), text())},
        tools=[Tool(spec=spec("holder", safe=True), fn=holder)],
    )
    task = asyncio.ensure_future(runner.run(agent("holder"), "go", config()))
    try:
        await until(lambda: len(probe.inside) == 3, "every call to be waiting")
        task.cancel()
        await asyncio.sleep(0.05)
        assert not cleaned and not task.done(), "the calls should still be cleaning up"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert sorted(cleaned) == keys, f"the run ended before its calls finished: cleaned {cleaned}"
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert not leftover, leftover
    finally:
        probe.release_all()
        if not task.done():
            task.cancel()


async def test_max_concurrent_tools_1_reproduces_a_run_whose_tools_do_not_declare_concurrency_safe():
    """NFR-15: same requests, history and events, where every call is allowed."""

    async def run(safe):
        async def ok(key):
            await asyncio.sleep(0)
            return f"ok {key}"

        tool = Tool(spec=spec("ok", safe=safe), fn=ok)
        client = Scripted(calls(call("ok", "c1"), call("ok", "c2"), call("ok", "c3")), calls(call("ok", "c4"), call("ok", "c5")), text())
        sessions = InMemorySessionStore()
        runner = Runner({"m": client}, tools=[tool], session_store=sessions)
        run_config = config(scheduler_limits=limits(max_concurrent_tools=1)) if safe else config()
        result = await runner.run(agent("ok"), "go", run_config)
        assert result.status is RunStatus.COMPLETED, result.error
        digest = tool.spec.schema_hash()

        def shape(value):
            # The tool's schema hash is the source of each result, and it moves
            # with concurrency_safe by design; everything else must not.
            return json.dumps(value, default=str, sort_keys=True).replace(digest, "<tool hash>")

        return (
            [shape([dataclasses.asdict(m) for m in r.messages] + [list(r.tools), r.model_settings, r.instructions]) for r in client.requests],
            shape([dataclasses.asdict(m) for m in sessions.history(result.run_id)]),
            [(e.event_type, shape(e.payload), e.tool_call_id) for e in result.events],
        )

    assert await run(safe=True) == await run(safe=False)


# =================================================================================================
# FR-45, AC-36: the file-tool thread pool
# =================================================================================================


class _Checkouts:
    """A real pool, recording the thread name of every connection checkout (AC-14's method)."""

    def __init__(self, pool, names):
        self._pool, self._names = pool, names

    def connection(self, *args, **kwargs):
        self._names.append(threading.current_thread().name)
        return self._pool.connection(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._pool, name)


def record_file_io_threads(monkeypatch):
    """The thread name of every call into the file layer (AC-29's method)."""
    fs = builtin()._fs
    names = []
    members = [name for name, member in inspect.getmembers(fs, callable) if not name.startswith("__")]
    assert members, "the file layer exposes no operations, so the spy sees nothing"
    for name in members:
        real = getattr(fs, name)

        def spy(*args, _real=real, **kwargs):
            names.append(threading.current_thread().name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(fs, name, spy)
    return names


async def test_file_tools_use_their_own_pool_so_a_store_call_is_never_starved(tmp_path, monkeypatch):
    assert DSN, "DATABASE_URL must be set"
    module = builtin()
    persistence = Persistence.postgres(DSN)  # schema work happens before anything is recorded
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8", newline="\n")
    gate = threading.Event()
    held, lock = [], threading.Lock()

    def seam(path):
        with lock:
            held.append(threading.current_thread().name)
        gate.wait(30)

    tool = module.grep_tool(tmp_path, _between_check_and_open=seam)
    file_threads = record_file_io_threads(monkeypatch)
    store_threads = []
    real_pool = postgres._pool
    monkeypatch.setattr(postgres, "_pool", lambda dsn: _Checkouts(real_pool(dsn), store_threads))
    # The default executor sized like the file pool, so file work that still
    # went through it would hold every one of its threads.
    asyncio.get_running_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=FILE_TOOL_THREADS))

    pending = [asyncio.ensure_future(tool.fn(text="needle", path=".")) for _ in range(3 * FILE_TOOL_THREADS)]
    try:
        await until(lambda: len(held) >= FILE_TOOL_THREADS, "every file-tool thread to be held")
        await asyncio.sleep(SETTLE)
        assert len(held) == FILE_TOOL_THREADS, f"{len(held)} calls entered a pool of {FILE_TOOL_THREADS} threads"
        missing = RunScope(run_id=str(uuid.uuid4()), tenant_id=TENANT, project_id=PROJECT)
        found = await asyncio.wait_for(asyncio.to_thread(persistence.runs.get_run, missing), 10)
        assert found is None
        assert not gate.is_set() and len(held) == FILE_TOOL_THREADS, "the store call returned only after file work moved"
    finally:
        gate.set()
        results = await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 60)

    assert all(isinstance(r, str) and "a.txt:1: needle" in r for r in results), results[:2]
    assert all(name.startswith(FILE_THREAD_PREFIX) for name in held), sorted(set(held))
    assert file_threads and all(name.startswith(FILE_THREAD_PREFIX) for name in file_threads), sorted(set(file_threads))
    assert store_threads and not any(name.startswith(FILE_THREAD_PREFIX) for name in store_threads), sorted(set(store_threads))


QUEUED = {
    "read": ("read_file_tool", "_read", {"path": "queued-marker.txt"}),
    "list": ("list_directory_tool", "_list", {"path": "queued-marker"}),
    "glob": ("glob_tool", "_glob", {"pattern": "queued-marker*"}),
    "grep": ("grep_tool", "_grep", {"text": "queued-marker", "path": "."}),
}


@pytest.mark.parametrize("kind", QUEUED)
async def test_a_queued_call_whose_timeout_expires_before_a_thread_is_free_never_starts(kind, tmp_path, monkeypatch):
    module = builtin()
    factory, function, arguments = QUEUED[kind]
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    entered = []
    real = getattr(module, function)

    def recording(*args):
        if any(isinstance(arg, str) and "queued-marker" in arg for arg in args):
            entered.append(kind)
        return real(*args)

    monkeypatch.setattr(module, function, recording)
    gate, held = threading.Event(), []

    def seam(path):
        held.append(threading.current_thread().name)
        gate.wait(30)

    holder = module.grep_tool(tmp_path, _between_check_and_open=seam)
    holders = [asyncio.ensure_future(holder.fn(text="x", path=".")) for _ in range(FILE_TOOL_THREADS)]
    tool = getattr(module, factory)(tmp_path)
    queued = Tool(spec=dataclasses.replace(tool.spec, timeout_seconds=0.2), fn=tool.fn)
    registry = ToolRegistry()
    registry.register(queued)
    executor = ToolExecutor(registry, AllowlistPermissionChecker({queued.name}))
    try:
        await until(lambda: len(held) >= FILE_TOOL_THREADS, "every file-tool thread to be held")
        outcome = await executor.execute(ToolCall(id="q1", name=queued.name, arguments=arguments))
        assert isinstance(outcome, Failed) and type(outcome.error).__name__ == "ToolTimeout", outcome
    finally:
        gate.set()
        await asyncio.wait_for(asyncio.gather(*holders, return_exceptions=True), 60)
    # Queued behind the timed-out call, so it runs only once the pool has passed it.
    await asyncio.wait_for(module.read_file_tool(tmp_path).fn(path="a.txt"), 30)
    assert entered == [], f"the timed-out {kind} call still ran its blocking function"


async def test_setting_the_file_tool_thread_count_after_the_first_call_is_refused(tmp_path):
    module = builtin()
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    await module.read_file_tool(tmp_path).fn(path="a.txt")
    with pytest.raises(RuntimeError):
        module.set_file_tool_threads(8)


def test_the_file_tool_pool_is_sized_before_its_first_call_and_its_threads_are_named(tmp_path):
    """In a fresh interpreter, because the pool is process-wide and this suite has used it."""
    script = textwrap.dedent(
        """
        import asyncio, pathlib, sys, threading, time
        import agentsdk.builtin_tools as b

        for bad in (0, -1, True, 2.0, "2", None):
            try:
                b.set_file_tool_threads(bad)
            except ValueError:
                pass
            else:
                sys.exit(f"set_file_tool_threads accepted {bad!r}")
        b.set_file_tool_threads(2)
        root = pathlib.Path(sys.argv[1])
        (root / "a.txt").write_text("x", encoding="utf-8")
        names = set()

        def seam(path):
            names.add(threading.current_thread().name)
            time.sleep(0.05)

        tool = b.grep_tool(root, _between_check_and_open=seam)

        async def main():
            await asyncio.gather(*(tool.fn(text="x", path=".") for _ in range(8)))

        asyncio.run(main())
        try:
            b.set_file_tool_threads(3)
        except RuntimeError:
            print("REFUSED")
        print("THREADS", "|".join(sorted(names)))
        """
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REFUSED" in proc.stdout, proc.stdout
    names = proc.stdout.split("THREADS ", 1)[1].split()[0].split("|")
    assert len(names) == 2 and all(re.fullmatch(r"agentsdk-file-tool-\d+", name) for name in names), names


# =================================================================================================
# FR-46, AC-37: provider limits
# =================================================================================================


class GatedClient:
    def __init__(self, probe):
        self.probe = probe
        self.count = 0

    async def send(self, request):
        self.count += 1
        key = f"s{self.count}"
        self.probe.enter(key)
        try:
            await self.probe.gate(key).wait()
        finally:
            self.probe.leave(key)
        return text()


async def test_a_provider_limit_caps_concurrent_sends_across_every_run_of_a_runner():
    for limit, expected in ((2, 2), (None, 6)):
        probe = Probe()
        extra = {} if limit is None else {"scheduler_limits": limits(provider_concurrency_limits={"m": limit})}
        runner = Runner({"m": GatedClient(probe)}, **extra)
        results = await drive(asyncio.gather(*(runner.run(agent(), "go", config()) for _ in range(6))), probe)
        assert all(r.status is RunStatus.COMPLETED for r in results), [r.error for r in results]
        assert probe.peak == expected, f"limit {limit}: {probe.peak} sends at once"


class RetryingClient:
    """Fails its first attempt and retries after a backoff the test controls, all inside one send."""

    def __init__(self, log, backoff):
        self.log, self.backoff = log, backoff

    async def send(self, request):
        run = request.messages[0].content
        self.log.append(("send", run))
        self.log.append(("backing off", run))
        await self.backoff[run].wait()
        self.log.append(("retried", run))
        return text()


async def test_a_send_holds_its_provider_slot_through_the_clients_own_retries():
    for limit in (1, None):
        log = []
        backoff = {"A": asyncio.Event(), "B": asyncio.Event()}
        extra = {} if limit is None else {"scheduler_limits": limits(provider_concurrency_limits={"m": limit})}
        runner = Runner({"m": RetryingClient(log, backoff)}, **extra)
        a = asyncio.ensure_future(runner.run(agent(), "A", config()))
        try:
            await until(lambda: ("backing off", "A") in log, "run A to back off inside send")
            b = asyncio.ensure_future(runner.run(agent(), "B", config()))
            await asyncio.sleep(SETTLE)
            if limit is None:
                assert ("send", "B") in log, "without a limit run B should send at once, so the probe sees nothing"
            else:
                assert ("send", "B") not in log, "run B sent while run A held the only slot, backing off inside send"
            backoff["A"].set()
            await until(lambda: ("backing off", "B") in log, "run B to send")
            backoff["B"].set()
            results = await asyncio.wait_for(asyncio.gather(a, b), 10)
        finally:
            for event in backoff.values():
                event.set()
        assert all(r.status is RunStatus.COMPLETED for r in results)


# =================================================================================================
# FR-47, AC-38: the M10 carry-overs
# =================================================================================================


class _Source(str):
    pass


class ReportsShort(str):
    def __len__(self):
        return 3


class ReportsHuge(str):
    def __len__(self):
        return 10**9


SOURCES = {
    "a str subclass": lambda: _Source("https://source.test/a"),
    "a subclass reporting a short length": lambda: ReportsShort("https://source.test/" + "b" * 40),
    "a subclass reporting a huge length": lambda: ReportsHuge("https://source.test/c"),
}


async def run_with_source(persistence, source, via):
    async def sourced(key):
        return ToolOutput(f"content {key}", source_uri=source) if via == "tool output" else f"content {key}"

    class Replacing(RuntimeHook):
        def after_tool(self, result):
            provenance = dataclasses.replace(result.provenance, source_uri_or_hash=source)
            return HookOutcome(action=HookAction.MODIFY, replacement=dataclasses.replace(result, provenance=provenance))

    sessions = InMemorySessionStore()
    runner = Runner(
        {"m": Scripted(calls(call("sourced", "c1")), text())},
        tools=[Tool(spec=spec("sourced"), fn=sourced)],
        session_store=sessions,
        persistence=persistence,
        hook=Replacing() if via == "hook" else None,
    )
    result = await runner.run(agent("sourced"), "go", config())
    assert result.status is RunStatus.COMPLETED, result.error
    [stored] = tool_results(sessions, persistence, result.run_id)
    return result, stored


@pytest.mark.parametrize("via", ["tool output", "hook"])
@pytest.mark.parametrize("label", SOURCES)
async def test_a_str_subclass_source_is_stored_as_the_exact_text_it_holds(backend, label, via):
    source = SOURCES[label]()
    held = str.__getitem__(source, slice(None))
    _, stored = await run_with_source(backend, source, via)
    assert stored.is_error is False, stored.content
    assert type(stored.provenance.source_uri_or_hash) is str
    assert stored.provenance.source_uri_or_hash == held


async def test_the_source_bound_is_measured_on_the_text_held_not_the_length_reported(backend):
    _, stored = await run_with_source(backend, ReportsShort("s" * 8193), "tool output")
    assert stored.is_error is True and "8192" in stored.content, stored.content


@pytest.mark.parametrize("length, refused", [(8192, False), (8193, True)])
async def test_a_source_of_8192_characters_is_accepted_and_8193_refused(backend, length, refused):
    result, stored = await run_with_source(backend, "s" * length, "tool output")
    assert stored.is_error is refused, stored.content[:200]
    [event] = [e for e in result.events if e.event_type is EventType.TOOL_CALLED]
    assert event.payload["is_error"] is refused
    if not refused:
        assert stored.provenance.source_uri_or_hash == "s" * length


BAD_NAME = "bad\udc80"


def build_tree_with_unstorable_names(base):
    def write(path):
        path.write_text("needle\n", encoding="utf-8", newline="\n")

    write(base / "good.txt")
    (base / "gooddir").mkdir()
    write(base / "gooddir" / "inner.txt")
    write(base / f"{BAD_NAME}.txt")
    (base / f"{BAD_NAME}dir" / "deeper").mkdir(parents=True)
    write(base / f"{BAD_NAME}dir" / "inner.txt")
    write(base / f"{BAD_NAME}dir" / "deeper" / "deep.txt")


WITHHELD = {
    "list": ("list_directory_tool", {"path": "."}, ["good.txt", "gooddir/"], 2),
    "glob": ("glob_tool", {"pattern": "*.txt"}, ["good.txt"], 1),
    "glob recursive": ("glob_tool", {"pattern": "**/*.txt"}, ["good.txt", "gooddir/inner.txt"], 2),
    # R3, M11 review round 1: a pattern that both descends into a folder and
    # shows it counted a withheld folder twice.
    "glob everything": ("glob_tool", {"pattern": "**"}, ["good.txt", "gooddir/", "gooddir/inner.txt"], 2),
    "glob everything below": ("glob_tool", {"pattern": "**/*"}, ["good.txt", "gooddir/", "gooddir/inner.txt"], 2),
    # Met but never used, and so not counted: the unstorable file under '**'
    # when no later part matches it, and at a part that only descends. Without
    # these, counting every entry met (mutant S40) passed once R3 counted each
    # entry once.
    "glob a pattern the file does not match": ("glob_tool", {"pattern": "**/*.md"}, [], 1),
    "glob a part that only descends": ("glob_tool", {"pattern": "*/inner.txt"}, ["gooddir/inner.txt"], 1),
    "grep": ("grep_tool", {"text": "needle", "path": "."}, ["good.txt:1: needle", "gooddir/inner.txt:1: needle"], 2),
}


@pytest.mark.parametrize("case", WITHHELD)
async def test_a_name_the_store_cannot_hold_is_withheld_and_counted_rather_than_failing_the_call(backend, case, tmp_path):
    module = builtin()
    build_tree_with_unstorable_names(tmp_path)
    factory, arguments, expected, withheld = WITHHELD[case]
    tool = getattr(module, factory)(tmp_path)
    sessions = InMemorySessionStore()
    runner = Runner(
        {"m": Scripted(calls(ToolCall(id="c1", name=tool.name, arguments=arguments)), text())},
        tools=[tool],
        session_store=sessions,
        persistence=backend,
    )
    result = await runner.run(agent(tool.name), "go", config())
    assert result.status is RunStatus.COMPLETED, result.error
    [stored] = tool_results(sessions, backend, result.run_id)
    assert stored.is_error is False, stored.content
    assert unstorable_reason(stored.content) is None
    shown = sorted(line for line in stored.content.splitlines() if not line.startswith("["))
    assert shown == sorted(expected), stored.content
    counted = re.search(r"\[(\d+) entries withheld", stored.content)
    assert counted and int(counted.group(1)) == withheld, stored.content


# =================================================================================================
# AC-43: NFR-8 still holds with parallel batches
# =================================================================================================

MAX_STALL_SECONDS = 0.050  # NFR-8
MAX_WALL_RATIO = 3.0  # NFR-8
ATTEMPTS = 3


class BatchingModel:
    """Two turns of three parallel calls, then done; no network, so only the store can be slow."""

    async def send(self, request):
        await asyncio.sleep(0.05)
        turns = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if turns >= 2:
            return text()
        return calls(*(ToolCall(id=f"t{turns}c{i}", name="async_noop", arguments={}) for i in range(3)))


async def _async_noop():
    await asyncio.sleep(0)
    return "ok"


def async_tools():
    # Built per call, not at import: before M11 the field does not exist, and
    # this file must still import so each test fails on its own.
    return [
        Tool(
            spec=ToolSpec(
                name="async_noop",
                description="Does nothing, asynchronously.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                concurrency_safe=True,
            ),
            fn=_async_noop,
        )
    ]


async def _measure(concurrent_runs, persistence, tenant):
    lags = []
    stop = False

    async def heartbeat():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            lags.append(now - last - 0.01)
            last = now

    async def one():
        runner = Runner({"m": BatchingModel()}, tools=async_tools(), persistence=persistence)
        return await runner.run(
            AgentSpec(id="nfr8", instructions="go", tool_profile=("async_noop",)),
            "do it",
            RunConfig(tenant_id=tenant, project_id="p-nfr8", max_turns=6),
        )

    beat = asyncio.create_task(heartbeat())
    started = time.perf_counter()
    results = await asyncio.gather(*(one() for _ in range(concurrent_runs)))
    wall = time.perf_counter() - started
    stop = True
    await beat
    assert all(r.status is RunStatus.COMPLETED for r in results), "a run failed, so the timing measures the wrong thing"
    assert all(sum(1 for e in r.events if e.event_type is EventType.TOOL_CALLED) == 6 for r in results)
    return wall, max(lags) if lags else 0.0


async def test_six_concurrent_runs_issuing_parallel_batches_stay_within_nfr8():
    """AC-43, measured as AC-14 measures it: the median of three attempts, both halves in one process.

    An acceptance measurement, not a detector (see the M7 note in
    tests/test_phase2_readiness.py): the thread-identity tests are what catch
    store I/O on the loop.
    """
    assert DSN, "DATABASE_URL must be set"
    tenant = "SYN-m11-nfr8"
    samples = []
    try:
        for _ in range(ATTEMPTS):
            memory_wall, _ = await _measure(6, None, tenant)
            pg_wall, worst_stall = await _measure(6, Persistence.postgres(DSN), tenant)
            samples.append((worst_stall, pg_wall, memory_wall))
            drop_runs(tenant)
    finally:
        drop_runs(tenant)
    worst_stall = statistics.median(sample[0] for sample in samples)
    ratio = statistics.median(sample[1] for sample in samples) / statistics.median(sample[2] for sample in samples)
    assert worst_stall < MAX_STALL_SECONDS, f"stalled {worst_stall * 1000:.1f} ms: {[round(s[0] * 1000, 1) for s in samples]}"
    assert ratio <= MAX_WALL_RATIO, f"{ratio:.1f}x the in-memory wall time: {samples}"
