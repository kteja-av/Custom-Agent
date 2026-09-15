"""Runtime events (ADR-21, LLD 2.3, 4.12).

`RunEvent` is the ordered audit/telemetry stream. It is NOT the source of
execution truth: `runs` + `messages` are authoritative, and a trace reconstructs
from persisted state PLUS ordered events together (NFR-3).

The envelope carries identifiers Phase 0 never populates -- agent_id, task_id,
attempt_id, parent_event_id, correlation_id -- so Phase 2's subagents and
Phase 6's execution attempts add values, not columns.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

SCHEMA_VERSION = 1


class EventType(str, Enum):
    """The Phase 0 event set. Later phases add members, not a new envelope."""

    RUN_STARTED = "RunStarted"
    MODEL_CALLED = "ModelCalled"
    TOOL_CALLED = "ToolCalled"
    RUN_COMPLETED = "RunCompleted"
    RUN_FAILED = "RunFailed"
    # FR-52 (M12): a cancelled run's last event, carrying the reason.
    RUN_CANCELLED = "RunCancelled"


@dataclass(frozen=True)
class RunEvent:
    event_type: EventType
    tenant_id: str
    project_id: str
    run_id: str
    sequence_no: int
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = SCHEMA_VERSION
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Unpopulated in Phase 0 -- no subagents, plan nodes or retry attempts yet.
    agent_id: str | None = None
    task_id: str | None = None
    tool_call_id: str | None = None
    attempt_id: str | None = None
    parent_event_id: str | None = None
    correlation_id: str | None = None


@runtime_checkable
class EventSink(Protocol):
    def emit(
        self, event_type: EventType, payload: dict[str, Any], **identifiers: Any
    ) -> RunEvent: ...

    def events(self) -> tuple[RunEvent, ...]: ...


class InMemoryEventSink:
    """Phase 0 sink. M5 adds the Postgres-backed one behind the same protocol.

    Owns sequence numbering so ordering can never depend on the caller
    remembering to increment something.
    """

    def __init__(self, tenant_id: str, project_id: str, run_id: str) -> None:
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._run_id = run_id
        self._events: list[RunEvent] = []
        # FR-52. Emits arrive on worker threads, and numbering is a read of the
        # length followed by an append: with the thread switch interval at a
        # microsecond, 8 threads produced about 600 duplicate numbers per 1600
        # events before this lock (KNOWLEDGE-93fa7f44).
        self._lock = threading.Lock()

    def emit(
        self, event_type: EventType, payload: dict[str, Any] | None = None, **identifiers: Any
    ) -> RunEvent:
        with self._lock:
            event = RunEvent(
                event_type=event_type,
                tenant_id=self._tenant_id,
                project_id=self._project_id,
                run_id=self._run_id,
                sequence_no=len(self._events) + 1,
                payload=payload or {},
                **identifiers,
            )
            self._events.append(event)
        return event

    def events(self) -> tuple[RunEvent, ...]:
        with self._lock:
            return tuple(self._events)
