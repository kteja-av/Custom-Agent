"""Wiring that turns the in-memory Runner into a persisted one (M5).

`Persistence` is a small bundle rather than four constructor arguments, so that
`Runner(..., persistence=Persistence.postgres(dsn))` is the whole opt-in. With
no persistence the Runner behaves exactly as before, in memory: Phase 0 must
stay runnable without a database, or every test needs one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .artifacts import DEFAULT_MAX_CONTENT_BYTES
from .events import EventSink
from .postgres import (
    PostgresArtifactStore,
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

    `finish_run` takes the run's usage and cost as keyword arguments since M9
    (FR-31), and the Runner passes them only to a recorder that declares
    `records_accounting = True`. A recorder written before M9 declares nothing,
    keeps working exactly as it did, and records no accounting. Declared, not
    inferred: M9 round 1 read finish_run's signature, and a recorder wrapped
    without functools.wraps looked as though it accepted them.
    """

    def start_run(self, scope: RunScope, *, manifest: dict[str, Any], **fields: Any) -> None: ...

    def finish_run(
        self, scope: RunScope, status: str, usage: Any = None, cost_usd: Any = None
    ) -> None: ...


@dataclass(frozen=True)
class Persistence:
    # The DSN carries the database password, and a dataclass renders every field
    # in its repr: a log line, a traceback or a debugger showing a Persistence
    # printed the password (NFR-4; KNOWLEDGE-cb2f13f5). Hidden from repr, still
    # readable as an attribute, because the stores need it.
    dsn: str = field(repr=False)
    runs: RunRecorder
    _sessions: PostgresSessionStore

    @classmethod
    def postgres(cls, dsn: str, *, create_schema: bool = True) -> Persistence:
        """Persistence on `dsn`, bringing the schema up to date first.

        Schema application is BLOCKING DDL on the calling thread, under a
        database-wide advisory lock so concurrent starts serialise instead of
        racing. Build this once at process start -- not per run, and not from
        inside a running event loop. It waits on that lock, and on any open
        transaction holding locks on these tables: 1.54 s behind a single open
        writer, measured in M7 review round 2. A process that does not own the
        schema can pass create_schema=False and skip it entirely.
        """
        if create_schema:
            apply_schema(dsn)
        return cls(dsn=dsn, runs=PostgresRunStore(dsn), _sessions=PostgresSessionStore(dsn))

    def session_store_for(self, scope: RunScope) -> SessionStore:
        """Bound per run: append() cannot write a row without its tenant."""
        return self._sessions.bind(scope)

    def event_sink_for(self, scope: RunScope) -> EventSink:
        return PostgresEventStore(self.dsn, scope.tenant_id, scope.project_id, scope.run_id)

    def artifact_store(
        self,
        tenant_id: str,
        project_id: str,
        *,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> PostgresArtifactStore:
        """The Postgres artifact store for one tenant and project (FR-54).

        An empty or unstorable scope, and a size cap that is not a positive int,
        are refused here with ValueError.
        """
        return PostgresArtifactStore(
            self.dsn, tenant_id, project_id, max_content_bytes=max_content_bytes, clock=clock
        )
