"""The public API (FR-1, FR-48, NFR-5, LLD 3.3).

`Runner.run()` is the only method most application code calls. Everything else in
this package is an internal collaborator that Runner composes -- AgentLoop,
ToolExecutor, ContextAssembler, ModelClient. That visibility boundary is the
entire point of Runner: it is what lets Phase 2 replace the loop with an
orchestrator without any caller noticing.

Since M12, `Runner.start()` starts a run and returns a `RunHandle` for it at once,
through which a caller streams the run's events, reads its state, awaits its
result or cancels it. `Runner.run()` is a run started that way and awaited.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from functools import partial
from typing import Any

import uuid
from dataclasses import dataclass, field
from enum import Enum

from .context import ContextAssembler
from .errors import describe_exception
from .events import EventSink, EventType, InMemoryEventSink, RunEvent
from .executor import ToolExecutor
from .handle import PublishingSink, RunControl, RunHandle, RunState
from .hooks import RuntimeHook
from .identity import PrincipalContext
from .loop import AgentLoop, RunMeter
from .manifest import build_manifest
from .model import ModelClient, ModelRequest, ReasoningEffort, Usage
from .permissions import AllowlistPermissionChecker, PermissionChecker
from .persistence import Persistence
from .postgres import RunScope, column_rejection_reason
from .primitives import unstorable_reason
from .registry import ModelRegistry, call_cost, default_registry
from .scheduler import ProviderSlots, RunSlots, SchedulerLimits
from .session import InMemorySessionStore, SessionStore
from .tools import Tool, ToolRegistry
from .version import __version__

__all__ = ["AgentSpec", "RunConfig", "RunHandle", "RunResult", "RunState", "RunStatus", "Runner"]


class RunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    MAX_TURNS_EXCEEDED = "max_turns_exceeded"
    # FR-50, P2-D3 (DECISION-a44db7e4): a new terminal status rather than failed
    # with a reason, so a cancelled run can be told from a failure without
    # parsing its error.
    CANCELLED = "cancelled"


# The INTEGER column's ceiling, named once (see RunConfig.__post_init__).
_MAX_TURNS_CEILING = 2**31 - 1


def _refuse_bad_output_limit(value: object, owner: str) -> None:
    """max_output_tokens (FR-27), refused by name at construction like max_turns.

    Explicit about type rather than trusting a comparison: a bool passes every
    range check (True >= 1), and a string raises a TypeError that names nothing.
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{owner}.max_output_tokens must be an int or None, got {type(value).__name__}"
        )
    if not 1 <= value <= _MAX_TURNS_CEILING:
        raise ValueError(
            f"{owner}.max_output_tokens must be between 1 and {_MAX_TURNS_CEILING} "
            f"(execution_manifests.max_output_tokens is an INTEGER column), got {value}"
        )


def _reasoning_effort(value: object, owner: str) -> ReasoningEffort | None:
    """reasoning_effort (FR-28): a ReasoningEffort, its exact value, or None."""
    if value is None or isinstance(value, ReasoningEffort):
        return value
    if isinstance(value, str):
        try:
            return ReasoningEffort(value)
        except ValueError:
            pass
    raise ValueError(
        f"{owner}.reasoning_effort must be one of "
        f"{[effort.value for effort in ReasoningEffort]} or None, got {value!r}"
    )


def _refuse_bad_scheduler_limits(value: object, owner: str) -> None:
    """scheduler_limits (FR-43): a SchedulerLimits or None, refused by name."""
    if value is not None and not isinstance(value, SchedulerLimits):
        raise ValueError(
            f"{owner} scheduler_limits must be a SchedulerLimits or None, got {type(value).__name__}"
        )


def _cancellation_reason(value: object) -> str:
    """What a cancelled run records as its error (FR-50): the reason given, when it
    is text a store can hold, and `cancelled` otherwise."""
    if isinstance(value, str) and value:
        text = str.__getitem__(value, slice(None))
        if unstorable_reason(text) is None:
            return text
    return "cancelled"


@dataclass(frozen=True)
class AgentSpec:
    id: str
    instructions: str
    name: str = ""
    role: str = ""
    preferred_model: str | None = None
    # Names selected from the Runner's registry, assigned ad hoc per spawn
    # (ADR-14). An EMPTY profile permits nothing -- see checker(). It does not
    # mean "everything"; an empty allowlist that opened the gates would fail
    # silently open, which is the wrong direction for a permission default.
    #
    # Note the profile currently gates EXECUTION but not VISIBILITY: the model
    # is still offered every registered tool's schema. That is deliberate for
    # Phase 0 (it keeps AC-2's permission-denial path reachable) and belongs to
    # ContextPolicy in Phase 2, which is the component that decides what an
    # agent may see rather than what it may do.
    tool_profile: tuple[str, ...] = ()
    permission_policy: PermissionChecker | None = None
    # FR-27 / FR-28. None leaves the model client's own default in place. A
    # value set on the RunConfig overrides the agent's.
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_profile", tuple(self.tool_profile))
        _refuse_bad_output_limit(self.max_output_tokens, "AgentSpec")
        object.__setattr__(
            self, "reasoning_effort", _reasoning_effort(self.reasoning_effort, "AgentSpec")
        )

    def checker(self) -> PermissionChecker:
        """Default policy: allow exactly the declared tool profile.

        A spec with no explicit policy and no profile therefore permits nothing,
        which is the right default for a permission layer -- an empty allowlist
        denies, it does not wave everything through.
        """
        if self.permission_policy is not None:
            return self.permission_policy
        return AllowlistPermissionChecker(set(self.tool_profile))


@dataclass(frozen=True)
class RunConfig:
    tenant_id: str
    project_id: str
    max_turns: int = 10
    model_override: str | None = None
    principal_context: PrincipalContext | None = None
    # The run that spawned this one (FR-21). None for a top-level run, which
    # is the common case. Phase 2's subagents are what populate it; the column
    # and the tenancy rule exist now so that phase adds a caller rather than a
    # migration to a table that already holds production rows.
    parent_run_id: str | uuid.UUID | None = None
    # FR-27 / FR-28: override the agent's values for this run only.
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort | str | None = None
    # FR-43: this run's per-run and per-tool limits, replacing the Runner's as a
    # whole. None runs under the Runner's.
    scheduler_limits: SchedulerLimits | None = None

    def __post_init__(self) -> None:
        # Before the range checks: a bool passes every one of them (True >= 1,
        # True <= the ceiling) and then fails the write as a SQL boolean.
        if isinstance(self.max_turns, bool):
            raise ValueError("max_turns must be an int, not a bool")
        if self.max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        # And bounded above, because runs.max_turns is an INTEGER column.
        # Refused HERE rather than at the write so the run fails the same way
        # with and without persistence: 2**31 is an ordinary Python int that
        # passed the lower bound, completed in memory, and failed against
        # Postgres with NumericValueOutOfRange (M5 round 8). A ceiling this
        # high is not a real constraint on anyone -- it is the point at which
        # "more turns" stops being a number the store can hold.
        if self.max_turns > _MAX_TURNS_CEILING:
            raise ValueError(
                f"max_turns must be at most {_MAX_TURNS_CEILING} "
                "(runs.max_turns is an INTEGER column)"
            )
        if not self.tenant_id or not self.project_id:
            raise ValueError("tenant_id and project_id are mandatory on every run (ADR-11)")
        # Refused HERE, by name, like max_turns above -- not only by the store.
        # M7 round 1 found RunConfig constructing happily around "not-a-uuid",
        # a NUL and an int, while every sibling field refused at construction:
        # M5 round 8's shape exactly, a caller-supplied value reaching a typed
        # column with no guard at the boundary the caller actually touches.
        # One implementation shared with the store, so the two cannot disagree.
        reason = column_rejection_reason(self.parent_run_id, "UUID")
        if reason is not None:
            raise ValueError(f"parent_run_id cannot be stored: {reason}")
        if isinstance(self.parent_run_id, uuid.UUID):
            # One type on the config, whichever form the caller had to hand.
            object.__setattr__(self, "parent_run_id", str(self.parent_run_id))
        _refuse_bad_output_limit(self.max_output_tokens, "RunConfig")
        object.__setattr__(
            self, "reasoning_effort", _reasoning_effort(self.reasoning_effort, "RunConfig")
        )
        _refuse_bad_scheduler_limits(self.scheduler_limits, "RunConfig")
        if self.scheduler_limits is not None and self.scheduler_limits.provider_concurrency_limits:
            # A provider limit is shared by every run of a Runner, so one run
            # cannot set it.
            raise ValueError(
                "RunConfig.scheduler_limits cannot carry provider_concurrency_limits: a provider "
                "limit is shared by every run of a Runner, so it is set on the Runner"
            )


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    output: str | None
    events: tuple[RunEvent, ...] = ()
    usage: Usage = field(default_factory=Usage)
    run_id: str = ""
    # Not in the design's four-field sketch, but a failed run that cannot say
    # why is not debuggable. None on success.
    error: str | None = None
    # What the run cost in USD (FR-30): the sum of its model calls' costs, from
    # the prices in the Runner's ModelRegistry. None when that cannot be known
    # -- a model with no price, or a call nothing could price -- never 0.
    cost_usd: Decimal | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.COMPLETED


@dataclass(frozen=True)
class _OpenedRun:
    """Everything a run needs that is decided before it starts."""

    scope: RunScope
    events: EventSink
    client_key: str
    model_id: str | None
    recorded_model: str | None
    max_output_tokens: int | None
    reasoning_effort: ReasoningEffort | None
    meter: RunMeter


class Runner:
    def __init__(
        self,
        model_clients: dict[str, ModelClient],
        *,
        session_store: SessionStore | None = None,
        tools: list[Tool] | None = None,
        tool_registry: ToolRegistry | None = None,
        hook: RuntimeHook | None = None,
        assembler: ContextAssembler | None = None,
        persistence: Persistence | None = None,
        model_registry: ModelRegistry | None = None,
        scheduler_limits: SchedulerLimits | None = None,
    ) -> None:
        if not model_clients:
            raise ValueError("Runner requires at least one model client")
        # Optional: with no persistence the Runner behaves exactly as before,
        # entirely in memory. Phase 0 must stay runnable without a database.
        self._persistence = persistence
        self._models = model_registry if model_registry is not None else default_registry()
        self._clients = dict(model_clients)
        # FR-43: the provider limits every run of this Runner shares, and the
        # per-run limits a run uses unless its RunConfig sets its own.
        _refuse_bad_scheduler_limits(scheduler_limits, "Runner")
        self._limits = scheduler_limits if scheduler_limits is not None else SchedulerLimits()
        unknown = sorted(set(self._limits.provider_concurrency_limits) - set(self._clients))
        if unknown:
            raise ValueError(
                f"provider_concurrency_limits names {unknown}, which are not model clients of this "
                f"Runner; registered: {sorted(self._clients)}"
            )
        self._provider_slots = ProviderSlots(self._limits.provider_concurrency_limits)
        self._sessions = session_store if session_store is not None else InMemorySessionStore()
        # `or` would be wrong here: ToolRegistry defines __len__, so an EMPTY
        # caller-supplied registry is falsy and would be silently discarded and
        # replaced with a fresh one. Identity, not truthiness.
        self._registry = tool_registry if tool_registry is not None else ToolRegistry()
        for tool in tools or ():
            self._registry.register(tool)
        self._hook = hook if hook is not None else RuntimeHook()
        self._assembler = assembler if assembler is not None else ContextAssembler()

    async def run(self, spec: AgentSpec, task: str, config: RunConfig) -> RunResult:
        """Drive one agent to a terminal status (FR-1): a run started with start()
        and awaited to its end (FR-48).

        TOTAL boundary. FR-1 promises a terminal status, and a promise honoured
        only for the failures someone remembered to enumerate is not a promise:
        that is the mistake the ModelClient boundary already made three times.
        A hook that raises, a session store that loses its connection, a
        malformed tool schema -- all become RunStatus.FAILED with a reason,
        never an exception reaching application code.

        Configuration errors are the deliberate exception. An unknown model
        client, an invalid RunConfig, or a reasoning effort with no output limit
        is a caller bug that must surface loudly at the call site, not be buried
        in a failed RunResult the caller might not inspect. Those raise before
        the run is considered started.

        Cancellation is recorded, then re-raised (FR-50, amending
        INVARIANT-af776957). Cancelling the task that awaits this, or a
        CancelledError raised by a collaborator, ends the run `cancelled` with its
        usage and cost recorded, and CancelledError then reaches the caller:
        before M12 it passed straight through and left a persisted run `running`
        forever (KNOWLEDGE-b3c2b462). Any other BaseException still passes
        through as control flow.
        """
        handle = await self.start(spec, task, config)
        try:
            result = await handle.result()
        except asyncio.CancelledError:
            handle.cancel()
            await handle._settled()
            raise
        if result.status is RunStatus.CANCELLED:
            raise asyncio.CancelledError()
        return result

    async def start(self, spec: AgentSpec, task: str, config: RunConfig) -> RunHandle:
        """Start a run and return its handle without waiting for it (FR-48).

        Configuration is validated exactly as run() validates it, and the same
        errors raise here, at the call site, before any run is started. The run
        belongs to the event loop that started it.
        """
        opened = self._open(spec, config)
        control = RunControl()
        handle = RunHandle(opened.scope.run_id, control, opened.meter)
        events = PublishingSink(opened.events, handle._publish, asyncio.get_running_loop())
        control.task = asyncio.ensure_future(self._drive(spec, task, config, opened, events, control))
        control.task.add_done_callback(handle._finished)
        return handle

    def _open(self, spec: AgentSpec, config: RunConfig) -> _OpenedRun:
        """Everything decided before a run starts; configuration errors raise here."""
        run_id = str(uuid.uuid4())
        scope = RunScope(run_id=run_id, tenant_id=config.tenant_id, project_id=config.project_id)
        events: EventSink = (
            self._persistence.event_sink_for(scope)
            if self._persistence is not None
            else InMemoryEventSink(config.tenant_id, config.project_id, run_id)
        )
        client_key, model_id = self._resolve_model(spec, config)
        max_output_tokens = (
            config.max_output_tokens
            if config.max_output_tokens is not None
            else spec.max_output_tokens
        )
        reasoning_effort = (
            config.reasoning_effort if config.reasoning_effort is not None else spec.reasoning_effort
        )
        if reasoning_effort is not None and max_output_tokens is None:
            # Decision D2. The SDK cannot see a client's own default output
            # limit, and a provider that budgets reasoning inside it refuses the
            # request: Claude over the gateway answers HTTP 400 when max_tokens
            # does not exceed the thinking budget (KNOWLEDGE-d625552b).
            raise ValueError(
                "reasoning_effort is set but max_output_tokens is not: set "
                "max_output_tokens on the AgentSpec or the RunConfig, large enough "
                "for the reasoning the model may spend as well as its answer"
            )
        # FR-32: a run records the model it used, including a client's own
        # default when nothing was named (KNOWLEDGE-862c2e9e).
        recorded_model = model_id or self._default_model_id(client_key)
        # The run's account, created here so that it outlives any exception the
        # loop raises: the failure path reports what the meter recorded, not what
        # the events happened to capture (R2).
        meter = RunMeter(no_call_cost=self._cost(recorded_model, Usage()))
        return _OpenedRun(
            scope=scope,
            events=events,
            client_key=client_key,
            model_id=model_id,
            recorded_model=recorded_model,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            meter=meter,
        )

    async def _drive(
        self,
        spec: AgentSpec,
        task: str,
        config: RunConfig,
        opened: _OpenedRun,
        events: EventSink,
        control: RunControl,
    ) -> RunResult:
        """The run itself, to a terminal status: the total boundary run() documents."""
        control.started = True
        scope, meter = opened.scope, opened.meter
        try:
            return await self._run(
                spec,
                task,
                config,
                scope,
                events,
                opened.client_key,
                opened.model_id,
                recorded_model=opened.recorded_model,
                max_output_tokens=opened.max_output_tokens,
                reasoning_effort=opened.reasoning_effort,
                meter=meter,
                control=control,
            )
        except asyncio.CancelledError:
            return await self._cancelled(scope, events, meter, control)
        except Exception as exc:  # noqa: BLE001
            if control.requested and not control.terminal:
                # Asked to stop first: the run ends as it was asked to, whatever
                # failed on the way out (FR-50).
                return await self._cancelled(scope, events, meter, control)
            reason = describe_exception(exc)
            control.terminal = True
            await self._safe_emit(events, EventType.RUN_FAILED, {"status": "failed", "reason": reason}, control)
            await self._safe_finish(scope, RunStatus.FAILED, meter.usage, meter.cost_usd, control)
            return RunResult(
                status=RunStatus.FAILED,
                output=None,
                events=events.events(),
                # The meter's totals: every call the model answered, recorded
                # before anything that could fail. Reporting zero, or only the
                # calls whose events were written, would under-report cost on
                # exactly the runs someone is investigating.
                usage=meter.usage,
                run_id=scope.run_id,
                error=reason,
                cost_usd=meter.cost_usd,
            )

    async def _cancelled(
        self, scope: RunScope, events: EventSink, meter: RunMeter, control: RunControl
    ) -> RunResult:
        """FR-50: record the run cancelled, as every terminal path records itself.

        RunCancelled is emitted once and is the run's last event; the row is
        finished `cancelled` with its usage and cost (FR-31). When a model call was
        in flight, that call may be billed and reports no usage, so the cost is
        unknown rather than understated (P2-D7, NFR-11).
        """
        control.terminal = True
        reason = _cancellation_reason(control.reason)
        cost = None if control.cancelled_in_flight else meter.cost_usd
        await self._safe_emit(
            events,
            EventType.RUN_CANCELLED,
            {"status": RunStatus.CANCELLED.value, "turns": control.turns, "reason": reason},
            control,
        )
        await self._safe_finish(scope, RunStatus.CANCELLED, meter.usage, cost, control)
        return RunResult(
            status=RunStatus.CANCELLED,
            output=None,
            events=events.events(),
            usage=meter.usage,
            run_id=scope.run_id,
            error=reason,
            cost_usd=cost,
        )

    async def _safe_finish(
        self,
        scope: RunScope,
        status: RunStatus,
        usage: Usage,
        cost_usd: Decimal | None,
        control: RunControl,
    ) -> None:
        # Threaded like every other store call (FR-20). This one runs once, on
        # a terminal path that is already failing or cancelled, so it is not what
        # NFR-8 measures -- but a store call that blocks the loop only when a run
        # is already failing is the kind of inconsistency that gets read as an
        # oversight later.
        if self._persistence is None:
            return
        try:
            await control.store(self._finish, scope, status, usage, cost_usd)
        except Exception:  # noqa: BLE001 - persistence must not mask the real outcome
            pass

    def _finish(
        self, scope: RunScope, status: RunStatus, usage: Usage, cost_usd: Decimal | None
    ) -> None:
        """The terminal write, carrying the run's usage and cost to a recorder
        that declares it records them (FR-31).

        Declared, not inferred. Round 1 offered accounting to any finish_run
        whose signature could bind it, and a pre-M9 recorder wrapped without
        functools.wraps -- which looks like (*args, **kwargs) -- took the
        arguments, raised, and turned a completed run FAILED with both terminal
        events emitted. A recorder that declares nothing is called exactly as
        it was before M9.
        """
        runs = self._persistence.runs
        if getattr(runs, "records_accounting", False) is True:
            runs.finish_run(scope, status.value, usage=usage, cost_usd=cost_usd)
        else:
            runs.finish_run(scope, status.value)

    @staticmethod
    async def _safe_emit(events: EventSink, event_type: EventType, payload: dict, control: RunControl) -> None:
        """Telemetry must not be able to fail a terminal path."""
        try:
            await control.store(events.emit, event_type, payload)
        except Exception:  # noqa: BLE001
            pass

    async def _run(
        self,
        spec: AgentSpec,
        task: str,
        config: RunConfig,
        scope: RunScope,
        events: EventSink,
        client_key: str,
        model_id: str | None,
        *,
        recorded_model: str | None,
        max_output_tokens: int | None,
        reasoning_effort: ReasoningEffort | None,
        meter: RunMeter,
        control: RunControl,
    ) -> RunResult:
        run_id = scope.run_id
        limits = self._limits_for(config)
        sessions = self._sessions
        if self._persistence is not None:
            sessions = self._persistence.session_store_for(scope)
            # FR-11: exactly one manifest row, written at start -- in the same
            # transaction as the run row, so no failure between the two can
            # leave a run that nothing can explain. The primary key guarantees
            # "at most one"; passing it here guarantees "at least one".
            await control.store(
                partial(self._persistence.runs.start_run, scope),
                agent_spec_id=spec.id,
                max_turns=config.max_turns,
                model_id=recorded_model,
                principal_context=(
                    config.principal_context.to_json() if config.principal_context else None
                ),
                parent_run_id=config.parent_run_id,
                manifest=build_manifest(
                    sdk_version=__version__,
                    agent_spec_id=spec.id,
                    instructions=spec.instructions,
                    tool_profile=spec.tool_profile,
                    tool_spec_hashes=[s.schema_hash() for s in self._registry.specs()],
                    model_id=recorded_model or "unspecified",
                    **self._model_versions(recorded_model, client_key),
                    policy_version=type(spec.checker()).__name__,
                    max_output_tokens=max_output_tokens,
                    reasoning_effort=reasoning_effort.value if reasoning_effort is not None else None,
                    pricing=self._pricing_record(recorded_model),
                    scheduler_limits=limits.to_json(),
                ),
            )

        # EVERY store call leaves the loop, not most of them (FR-20). M7 round 1
        # found this emit, ToolCalled's and the terminal one still made on the
        # loop -- four per run, scaling with fan-out -- and each takes the event
        # stream's advisory lock, so a lock held by another process became a
        # whole-loop stall: 479 ms, freezing unrelated runs with it, where the
        # messages path that HAD been offloaded stalled 14 ms under the same
        # probe. Moving three of four store paths off the loop is the defect
        # this comment exists to prevent recurring.
        await control.store(
            events.emit,
            EventType.RUN_STARTED,
            {
                "agent_spec_id": spec.id,
                "model": recorded_model,
                "provider": client_key,
                "max_turns": config.max_turns,
                # Recorded, never read in Phase 0 (ADR-27).
                "principal_context": (
                    config.principal_context.to_json() if config.principal_context else None
                ),
            },
        )

        # FR-44: the calls of a parallel batch can finish together. Emitting one
        # ToolCalled of this run at a time keeps their events in the order they
        # finish on both stores, beside the sinks' own numbering locks (FR-52).
        # Each carries its call's id in the envelope, which had stayed NULL since
        # Phase 0.
        tool_events = asyncio.Lock()

        async def emit_tool_called(event_type: str, payload: dict[str, Any]) -> None:
            call_id = payload.get("tool_call_id")
            async with tool_events:
                await control.store(
                    events.emit,
                    EventType.TOOL_CALLED,
                    payload,
                    tool_call_id=call_id if isinstance(call_id, str) else None,
                )

        executor = ToolExecutor(
            registry=self._registry,
            permission_checker=spec.checker(),
            hook=self._hook,
            # A coroutine, which the executor awaits: see ToolExecutor._safe_emit.
            emit=emit_tool_called,
        )

        def cost_of(request: ModelRequest, usage: Usage) -> Decimal | None:
            # Priced by the model the request was actually sent with, which a
            # before_model hook may have changed (FR-30).
            settings = getattr(request, "model_settings", None)
            sent = settings.get("model") if isinstance(settings, dict) else None
            return self._cost(sent if isinstance(sent, str) and sent else recorded_model, usage)

        loop = AgentLoop(
            model_client=self._clients[client_key],
            session_store=sessions,
            tool_executor=executor,
            tool_registry=self._registry,
            event_sink=events,
            assembler=self._assembler,
            hook=self._hook,
            cost_of=cost_of,
            meter=meter,
            tool_slot=RunSlots(limits).slot,
            model_slot=partial(self._provider_slots.slot, client_key),
            control=control,
        )

        # Only what the caller set: a run that sets none of M9's options sends
        # exactly the request it sent before M9 (NFR-12).
        model_settings: dict[str, Any] = {"model": model_id} if model_id else {}
        if max_output_tokens is not None:
            model_settings["max_tokens"] = max_output_tokens
        if reasoning_effort is not None:
            model_settings["reasoning_effort"] = reasoning_effort.value

        outcome = await loop.run(
            run_id,
            task,
            max_turns=config.max_turns,
            instructions=spec.instructions,
            model_settings=model_settings,
            principal_context=config.principal_context,
        )

        if outcome.exhausted_turns:
            status, error = RunStatus.MAX_TURNS_EXCEEDED, "max_turns_exceeded"
        elif outcome.error is not None:
            status, error = RunStatus.FAILED, outcome.error
        else:
            status, error = RunStatus.COMPLETED, None

        # FR-50: a cancellation that arrived during the loop's last store writes
        # takes effect here, before the terminal event. Once that event is being
        # written, a cancellation has no effect.
        if control.requested:
            raise asyncio.CancelledError()
        control.terminal = True
        await control.store(
            events.emit,
            EventType.RUN_COMPLETED if status is RunStatus.COMPLETED else EventType.RUN_FAILED,
            {"status": status.value, "turns": outcome.turns, "reason": error},
        )
        if self._persistence is not None:
            await control.store(self._finish, scope, status, meter.usage, meter.cost_usd)
        return RunResult(
            status=status,
            output=outcome.output,
            events=events.events(),
            usage=meter.usage,
            run_id=run_id,
            error=error,
            cost_usd=meter.cost_usd,
        )

    def _limits_for(self, config: RunConfig) -> SchedulerLimits:
        """The limits a run executes under (FR-43): its own per-run and per-tool
        limits as a whole when its RunConfig sets them, and always the Runner's
        provider limits."""
        chosen = config.scheduler_limits
        if chosen is None:
            return self._limits
        return SchedulerLimits(
            max_concurrent_tools=chosen.max_concurrent_tools,
            tool_concurrency_limits=chosen.tool_concurrency_limits,
            provider_concurrency_limits=self._limits.provider_concurrency_limits,
        )

    def _cost(self, model_id: str | None, usage: Usage) -> Decimal | None:
        """What `usage` cost on `model_id`, or None when that cannot be known.

        Guarded as a whole, the registry lookup included: the registry is the
        caller's, and accounting never fails a run (NFR-11).
        """
        try:
            entry = self._models.resolve(model_id) if model_id else None
            return call_cost(usage, entry.capabilities.pricing if entry is not None else None)
        except Exception:  # noqa: BLE001
            return None

    def _pricing_record(self, model_id: str | None) -> dict[str, str | None] | None:
        """The prices a run is costed with, for its manifest (FR-31)."""
        entry = self._models.resolve(model_id) if model_id else None
        pricing = entry.capabilities.pricing if entry is not None else None
        return pricing.to_json() if pricing is not None else None

    def _default_model_id(self, client_key: str) -> str | None:
        """The client's own default model, used when no model was named (FR-32).

        Optional on the client: ModelClient stays send()-only. A client without
        the attribute -- or with one that raises, or holds something no column
        can store -- records no model id, exactly as before M9.
        """
        try:
            candidate = getattr(self._clients[client_key], "default_model_id", None)
        except Exception:  # noqa: BLE001 - a client property is caller code
            return None
        if not isinstance(candidate, str) or not candidate:
            return None
        return candidate if column_rejection_reason(candidate, "TEXT") is None else None

    def _model_versions(self, model_id: str | None, client_key: str) -> dict[str, str]:
        """Version fields for the manifest (FR-11, AC-6).

        A model absent from the registry is reported as "unregistered" rather
        than left null: the manifest's job is to say exactly what produced a
        run, and "we did not know" is a more useful answer than an empty
        column that could equally mean the writer forgot.
        """
        entry = self._models.resolve(model_id) if model_id else None
        return {
            "model_version": entry.model_version if entry else "unregistered",
            "model_adapter_version": (
                entry.adapter_version if entry else type(self._clients[client_key]).__name__
            ),
        }

    def _resolve_model(self, spec: AgentSpec, config: RunConfig) -> tuple[str, str | None]:
        """`"<client_key>:<model_id>"`, or a bare model id when unambiguous."""
        reference = config.model_override or spec.preferred_model
        if not reference:
            if len(self._clients) != 1:
                raise ValueError(
                    "no preferred_model or model_override given and multiple model "
                    f"clients are registered: {sorted(self._clients)}"
                )
            return next(iter(self._clients)), None

        key, separator, model_id = reference.partition(":")
        if not separator:
            if len(self._clients) != 1:
                raise ValueError(
                    f"model reference {reference!r} has no '<client>:' prefix and "
                    f"multiple clients are registered: {sorted(self._clients)}"
                )
            return next(iter(self._clients)), reference
        if key not in self._clients:
            raise ValueError(
                f"unknown model client {key!r}; registered: {sorted(self._clients)}"
            )
        return key, model_id
