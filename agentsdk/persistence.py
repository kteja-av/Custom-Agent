"""Wiring that turns the in-memory Runner into a persisted one (M5).

`Persistence` is a small bundle rather than four constructor arguments, so that
`Runner(..., persistence=Persistence.postgres(dsn))` is the whole opt-in. With
no persistence the Runner behaves exactly as before, in memory: Phase 0 must
stay runnable without a database, or every test needs one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .events import EventSink
from .postgres import (
    PostgresEventStore,
    PostgresRunStore,
    PostgresSessionStore,
    RunScope,
    apply_schema,
)
from .session import SessionStore


class RunRecorder(Protocol):
    """What Runner needs from a durable backend, and nothing more.

    `write_manifest` is deliberately absent: the manifest is an argument to
    `start_run`, written in the same transaction as the run row, so a backend
    cannot offer the Runner a way to start a run without one (AC-6).
    """

    def start_run(self, scope: RunScope, *, manifest: dict[str, Any], **fields: Any) -> None: ...

    def finish_run(self, scope: RunScope, status: str) -> None: ...


@dataclass(frozen=True)
class Persistence:
    dsn: str
    runs: RunRecorder
    _sessions: PostgresSessionStore

    @classmethod
    def postgres(cls, dsn: str, *, create_schema: bool = True) -> Persistence:
        if create_schema:
            apply_schema(dsn)
        return cls(dsn=dsn, runs=PostgresRunStore(dsn), _sessions=PostgresSessionStore(dsn))

    def session_store_for(self, scope: RunScope) -> SessionStore:
        """Bound per run: append() cannot write a row without its tenant."""
        return self._sessions.bind(scope)

    def event_sink_for(self, scope: RunScope) -> EventSink:
        return PostgresEventStore(self.dsn, scope.tenant_id, scope.project_id, scope.run_id)
