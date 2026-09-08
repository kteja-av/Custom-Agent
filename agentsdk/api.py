"""The public API (FR-1, NFR-5, LLD 3.3).

`Runner.run()` is the only method application code calls. Everything else in
this package is an internal collaborator that Runner composes -- AgentLoop,
ToolExecutor, ContextAssembler, ModelClient. That visibility boundary is the
entire point of Runner: it is what lets Phase 2 replace the loop with an
orchestrator without any caller noticing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from .context import ContextAssembler
from .errors import describe_exception
from .events import EventSink, EventType, InMemoryEventSink, RunEvent
from .executor import ToolExecutor
from .hooks import RuntimeHook
from .identity import PrincipalContext
from .loop import AgentLoop
from .manifest import build_manifest
from .model import ModelClient, Usage
from .permissions import AllowlistPermissionChecker, PermissionChecker
from .persistence import Persistence
from .postgres import RunScope
from .registry import ModelRegistry, default_registry
from .session import InMemorySessionStore, SessionStore
from .tools import Tool, ToolRegistry
from .version import __version__


def _usage_from_events(events: tuple[RunEvent, ...]) -> Usage:
    """Rebuild total usage from the ModelCalled events.

    Used when the total boundary catches a non-SDK exception and never sees the
    loop's accumulator. The tokens were spent either way; reporting zero would
    quietly under-report cost on exactly the runs someone is investigating.

    Runs on the error path, so it must not be able to raise: `Usage` coerces
    every field it is handed, which is what makes the bare reads below safe
    even when a ModelClient reported NaN, Infinity or a string.
    """
    total = Usage()
    for event in events:
        raw = event.payload.get("usage") if isinstance(event.payload, dict) else None
        if isinstance(raw, dict):
            total = total + Usage(
                prompt_tokens=raw.get("prompt_tokens", 0),
                completion_tokens=raw.get("completion_tokens", 0),
                total_tokens=raw.get("total_tokens", 0),
            )
    return total


class RunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    MAX_TURNS_EXCEEDED = "max_turns_exceeded"


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_profile", tuple(self.tool_profile))

    def checker(self) -> PermissionChecker:
        """Default policy: allow exactly the declared tool profile.

        A spec with no explicit policy and no profile therefore permits nothing,
        which is the right default for a permission layer -- an empty allowlist
        denies, it does not wave everything through.
        """
        if self.permission_policy is not None:
            return self.permission_policy
        return AllowlistPermissionChecker(set(self.tool_profile))


# The INTEGER column's ceiling, named once (see RunConfig.__post_init__).
_MAX_TURNS_CEILING = 2**31 - 1


@dataclass(frozen=True)
class RunConfig:
    tenant_id: str
    project_id: str
    max_turns: int = 10
    model_override: str | None = None
    principal_context: PrincipalContext | None = None

    def __post_init__(self) -> None:
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

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.COMPLETED


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
    ) -> None:
        if not model_clients:
            raise ValueError("Runner requires at least one model client")
        # Optional: with no persistence the Runner behaves exactly as before,
        # entirely in memory. Phase 0 must stay runnable without a database.
        self._persistence = persistence
        self._models = model_registry if model_registry is not None else default_registry()
        self._clients = dict(model_clients)
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
        """Drive one agent to a terminal status (FR-1).

        TOTAL boundary. FR-1 promises a terminal status, and a promise honoured
        only for the failures someone remembered to enumerate is not a promise:
        that is the mistake the ModelClient boundary already made three times.
        A hook that raises, a session store that loses its connection, a
        malformed tool schema -- all become RunStatus.FAILED with a reason,
        never an exception reaching application code.

        Configuration errors are the deliberate exception. An unknown model
        client or an invalid RunConfig is a caller bug that must surface loudly
        at the call site, not be buried in a failed RunResult the caller might
        not inspect. Those raise before the run is considered started.

        BaseException passes through: cancellation is control flow, not failure.
        """
        run_id = str(uuid.uuid4())
        scope = RunScope(run_id=run_id, tenant_id=config.tenant_id, project_id=config.project_id)
        events: EventSink = (
            self._persistence.event_sink_for(scope)
            if self._persistence is not None
            else InMemoryEventSink(config.tenant_id, config.project_id, run_id)
        )
        # Deliberately OUTSIDE the guard below -- see the docstring.
        client_key, model_id = self._resolve_model(spec, config)
        try:
            return await self._run(spec, task, config, scope, events, client_key, model_id)
        except Exception as exc:  # noqa: BLE001
            reason = describe_exception(exc)
            self._safe_emit(events, EventType.RUN_FAILED, {"status": "failed", "reason": reason})
            self._safe_finish(scope, RunStatus.FAILED)
            return RunResult(
                status=RunStatus.FAILED,
                output=None,
                events=events.events(),
                # Reconstructed from the ModelCalled events rather than reported
                # as zero: the tokens were spent, and a caller reconciling cost
                # should not have to know that a failed run under-reports.
                usage=_usage_from_events(events.events()),
                run_id=run_id,
                error=reason,
            )

    def _safe_finish(self, scope: RunScope, status: RunStatus) -> None:
        if self._persistence is None:
            return
        try:
            self._persistence.runs.finish_run(scope, status.value)
        except Exception:  # noqa: BLE001 - persistence must not mask the real failure
            pass

    @staticmethod
    def _safe_emit(events: EventSink, event_type: EventType, payload: dict) -> None:
        """Telemetry must not be able to fail the failure path."""
        try:
            events.emit(event_type, payload)
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
    ) -> RunResult:
        run_id = scope.run_id
        sessions = self._sessions
        if self._persistence is not None:
            sessions = self._persistence.session_store_for(scope)
            # FR-11: exactly one manifest row, written at start -- in the same
            # transaction as the run row, so no failure between the two can
            # leave a run that nothing can explain. The primary key guarantees
            # "at most one"; passing it here guarantees "at least one".
            self._persistence.runs.start_run(
                scope,
                agent_spec_id=spec.id,
                max_turns=config.max_turns,
                model_id=model_id,
                principal_context=(
                    config.principal_context.to_json() if config.principal_context else None
                ),
                manifest=build_manifest(
                    sdk_version=__version__,
                    agent_spec_id=spec.id,
                    instructions=spec.instructions,
                    tool_profile=spec.tool_profile,
                    tool_spec_hashes=[s.schema_hash() for s in self._registry.specs()],
                    model_id=model_id or "unspecified",
                    **self._model_versions(model_id, client_key),
                    policy_version=type(spec.checker()).__name__,
                ),
            )

        events.emit(
            EventType.RUN_STARTED,
            {
                "agent_spec_id": spec.id,
                "model": model_id,
                "provider": client_key,
                "max_turns": config.max_turns,
                # Recorded, never read in Phase 0 (ADR-27).
                "principal_context": (
                    config.principal_context.to_json() if config.principal_context else None
                ),
            },
        )

        executor = ToolExecutor(
            registry=self._registry,
            permission_checker=spec.checker(),
            hook=self._hook,
            emit=lambda event_type, payload: events.emit(EventType.TOOL_CALLED, payload),
        )
        loop = AgentLoop(
            model_client=self._clients[client_key],
            session_store=sessions,
            tool_executor=executor,
            tool_registry=self._registry,
            event_sink=events,
            assembler=self._assembler,
            hook=self._hook,
        )

        outcome = await loop.run(
            run_id,
            task,
            max_turns=config.max_turns,
            instructions=spec.instructions,
            model_settings={"model": model_id} if model_id else {},
            principal_context=config.principal_context,
        )

        if outcome.exhausted_turns:
            status, error = RunStatus.MAX_TURNS_EXCEEDED, "max_turns_exceeded"
        elif outcome.error is not None:
            status, error = RunStatus.FAILED, outcome.error
        else:
            status, error = RunStatus.COMPLETED, None

        events.emit(
            EventType.RUN_COMPLETED if status is RunStatus.COMPLETED else EventType.RUN_FAILED,
            {"status": status.value, "turns": outcome.turns, "reason": error},
        )
        if self._persistence is not None:
            self._persistence.runs.finish_run(scope, status.value)
        return RunResult(
            status=status,
            output=outcome.output,
            events=events.events(),
            usage=outcome.usage,
            run_id=run_id,
            error=error,
        )

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
