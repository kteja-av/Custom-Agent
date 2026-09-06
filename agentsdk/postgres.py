"""Postgres-backed stores (FR-9, FR-10, FR-11, NFR-2, LLD 2, 3.9).

One connection pool, several stores, one database. The protocols these
implement are defined in `session.py` and `events.py`, so the loop cannot tell
whether it is talking to memory or Postgres.

ADR-11 is enforced by the schema, not by these classes remembering: every table
declares tenant_id and project_id NOT NULL, so a row that cannot say who it
belongs to fails at the database rather than being caught by review.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .events import SCHEMA_VERSION, EventType, RunEvent
from .primitives import (
    ContentProvenance,
    InstructionAuthority,
    Message,
    Origin,
    Role,
    TaintFlag,
    ToolCall,
    ToolResult,
    TrustZone,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def apply_schema(dsn: str) -> None:
    """Idempotent: every statement is CREATE ... IF NOT EXISTS."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


# --- serialisation ----------------------------------------------------------
# Provenance is persisted in full. A stored ToolResult that lost its provenance
# would break the invariant precisely where it matters most -- after the fact,
# when someone is trying to establish where a claim came from.


def _provenance_to_json(p: ContentProvenance) -> dict[str, Any]:
    return {
        "origin": p.origin.value,
        "instruction_authority": p.instruction_authority.value,
        "trust_zone": p.trust_zone.value,
        "taint_flags": sorted(flag.value for flag in p.taint_flags),
        "source_uri_or_hash": p.source_uri_or_hash,
    }


def _provenance_from_json(raw: dict[str, Any]) -> ContentProvenance:
    return ContentProvenance(
        origin=Origin(raw["origin"]),
        instruction_authority=InstructionAuthority(raw["instruction_authority"]),
        trust_zone=TrustZone(raw["trust_zone"]),
        taint_flags=frozenset(TaintFlag(f) for f in raw.get("taint_flags") or ()),
        source_uri_or_hash=raw.get("source_uri_or_hash"),
    )


def _message_to_columns(message: Message) -> tuple[Any, Any]:
    tool_calls = (
        [{"id": c.id, "name": c.name, "arguments": c.arguments, "arguments_error": c.arguments_error}
         for c in message.tool_calls]
        if message.tool_calls
        else None
    )
    tool_results = (
        [
            {
                "tool_call_id": r.tool_call_id,
                "content": r.content,
                "is_error": r.is_error,
                "provenance": _provenance_to_json(r.provenance),
            }
            for r in message.tool_results
        ]
        if message.tool_results
        else None
    )
    return (Jsonb(tool_calls) if tool_calls else None, Jsonb(tool_results) if tool_results else None)


def _message_from_row(row: tuple) -> Message:
    role, content, tool_calls, tool_results = row
    return Message(
        role=Role(role),
        content=content,
        tool_calls=tuple(
            ToolCall(
                id=c["id"],
                name=c["name"],
                arguments=c.get("arguments") or {},
                arguments_error=c.get("arguments_error"),
            )
            for c in (tool_calls or ())
        ),
        tool_results=tuple(
            ToolResult(
                tool_call_id=r["tool_call_id"],
                content=r["content"],
                provenance=_provenance_from_json(r["provenance"]),
                is_error=r.get("is_error", False),
            )
            for r in (tool_results or ())
        ),
    )


# --- stores -----------------------------------------------------------------


@dataclass(frozen=True)
class RunScope:
    """Everything a row needs to be tenant-scoped. Passed, never inferred."""

    run_id: str
    tenant_id: str
    project_id: str


class PostgresSessionStore:
    """FR-9. Insert-only; no update, no delete."""

    def __init__(self, dsn: str, scope: RunScope | None = None) -> None:
        self._dsn = dsn
        self._scope = scope

    def bind(self, scope: RunScope) -> PostgresSessionStore:
        """A per-run view. Runner binds this so append() knows the tenant."""
        return PostgresSessionStore(self._dsn, scope)

    def append(self, run_id: str, message: Message) -> None:
        scope = self._scope
        if scope is None or scope.run_id != run_id:
            raise ValueError(
                "PostgresSessionStore must be bound to the run's scope before appending; "
                "tenant_id and project_id are mandatory on every row (ADR-11)"
            )
        tool_calls, tool_results = _message_to_columns(message)
        with psycopg.connect(self._dsn) as conn:
            with conn.transaction():
                # sequence_no is computed INSIDE the insert's transaction, so
                # two concurrent appends cannot both read the same max and
                # produce a duplicate. The UNIQUE (run_id, sequence_no)
                # constraint is what makes the race a visible error instead of
                # a silently reordered history.
                conn.execute(
                    """
                    INSERT INTO messages (
                        message_id, run_id, tenant_id, project_id, sequence_no,
                        role, content, tool_calls, tool_results
                    )
                    SELECT %s, %s, %s, %s,
                           COALESCE(MAX(sequence_no), 0) + 1,
                           %s, %s, %s, %s
                    FROM messages WHERE run_id = %s
                    """,
                    (
                        uuid.uuid4(),
                        run_id,
                        scope.tenant_id,
                        scope.project_id,
                        message.role.value,
                        message.content,
                        tool_calls,
                        tool_results,
                        run_id,
                    ),
                )

    def history(self, run_id: str) -> list[Message]:
        with psycopg.connect(self._dsn) as conn:
            rows = conn.execute(
                """
                SELECT role, content, tool_calls, tool_results
                FROM messages WHERE run_id = %s ORDER BY sequence_no
                """,
                (run_id,),
            ).fetchall()
        return [_message_from_row(row) for row in rows]


class PostgresEventStore:
    """FR-10. Owns sequence numbering, like the in-memory sink it replaces."""

    def __init__(self, dsn: str, tenant_id: str, project_id: str, run_id: str) -> None:
        self._dsn = dsn
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._run_id = run_id
        self._buffer: list[RunEvent] = []

    def emit(
        self, event_type: EventType, payload: dict[str, Any] | None = None, **identifiers: Any
    ) -> RunEvent:
        event = RunEvent(
            event_type=event_type,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
            run_id=self._run_id,
            sequence_no=len(self._buffer) + 1,
            payload=payload or {},
            **identifiers,
        )
        with psycopg.connect(self._dsn) as conn:
            conn.execute(
                """
                INSERT INTO run_events (
                    event_id, schema_version, sequence_no, event_type,
                    tenant_id, project_id, run_id,
                    agent_id, task_id, tool_call_id, attempt_id,
                    parent_event_id, correlation_id, timestamp, payload
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    event.event_id,
                    event.schema_version,
                    event.sequence_no,
                    event.event_type.value,
                    event.tenant_id,
                    event.project_id,
                    event.run_id,
                    event.agent_id,
                    event.task_id,
                    event.tool_call_id,
                    event.attempt_id,
                    event.parent_event_id,
                    event.correlation_id,
                    event.timestamp,
                    Jsonb(_json_safe(event.payload)),
                ),
            )
        self._buffer.append(event)
        return event

    def events(self) -> tuple[RunEvent, ...]:
        return tuple(self._buffer)


def _json_safe(value: Any) -> Any:
    """Payloads carry enums and datetimes; JSONB does not."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class PostgresRunStore:
    """The `runs` and `execution_manifests` tables (FR-11)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def start_run(
        self,
        scope: RunScope,
        *,
        agent_spec_id: str,
        max_turns: int,
        model_id: str | None,
        principal_context: dict[str, Any] | None,
        manifest: dict[str, Any],
    ) -> None:
        """The run row and its manifest are one transaction, not two.

        AC-6 requires exactly one manifest per run. Written over two
        connections that held only while nothing failed in between: an ordinary
        transient error after the first write left a `runs` row nothing could
        explain -- no manifest, and no RunStarted event either, because the
        emit is sequenced after both writes.

        The manifest is a parameter rather than a follow-up call, so "a run
        exists without its manifest" stops being a state this API can express.
        Either both rows commit or neither does.
        """
        with psycopg.connect(self._dsn) as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, tenant_id, project_id, agent_spec_id, status,
                        principal_context, max_turns, model_id
                    ) VALUES (%s,%s,%s,%s,'running',%s,%s,%s)
                    """,
                    (
                        scope.run_id,
                        scope.tenant_id,
                        scope.project_id,
                        agent_spec_id,
                        Jsonb(principal_context) if principal_context else None,
                        max_turns,
                        model_id,
                    ),
                )
                self._insert_manifest(conn, scope, manifest)

    def finish_run(self, run_id: str, status: str) -> None:
        with psycopg.connect(self._dsn) as conn:
            conn.execute(
                "UPDATE runs SET status = %s, completed_at = %s WHERE run_id = %s",
                (status, datetime.now(timezone.utc), run_id),
            )

    def write_manifest(self, scope: RunScope, manifest: dict[str, Any]) -> None:
        """Write a manifest for a run that already exists.

        Not on the Runner's path -- `start_run` writes the manifest atomically
        with the run row. This one cannot reintroduce that defect: it only ever
        adds a manifest, so it cannot leave a run without one. A second call
        for the same run is refused by the primary key, not by care.
        """
        with psycopg.connect(self._dsn) as conn:
            self._insert_manifest(conn, scope, manifest)

    @staticmethod
    def _insert_manifest(
        conn: psycopg.Connection, scope: RunScope, manifest: dict[str, Any]
    ) -> None:
        """Takes the caller's connection so it can join an open transaction."""
        conn.execute(
            """
                INSERT INTO execution_manifests (
                    run_id, tenant_id, project_id, sdk_version, agent_spec_hash,
                    instructions_hash, model_id, model_version,
                    model_adapter_version, tool_spec_hashes, policy_version
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                scope.run_id,
                scope.tenant_id,
                scope.project_id,
                manifest["sdk_version"],
                manifest["agent_spec_hash"],
                manifest["instructions_hash"],
                manifest.get("model_id"),
                manifest.get("model_version"),
                manifest.get("model_adapter_version"),
                Jsonb(manifest.get("tool_spec_hashes") or []),
                manifest.get("policy_version"),
            ),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self._dsn) as conn:
            row = conn.execute(
                """
                SELECT run_id, tenant_id, project_id, agent_spec_id, status,
                       principal_context, max_turns, model_id, started_at, completed_at
                FROM runs WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "run_id", "tenant_id", "project_id", "agent_spec_id", "status",
            "principal_context", "max_turns", "model_id", "started_at", "completed_at",
        )
        return dict(zip(keys, row))


class PostgresTrace:
    """AC-7: reconstruct a run from persisted state PLUS ordered events.

    Events alone were never the source of truth, so this reads all three and
    says so in its shape rather than pretending the event stream is enough.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._runs = PostgresRunStore(dsn)

    def reconstruct(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get_run(run_id)
        with psycopg.connect(self._dsn) as conn:
            messages = conn.execute(
                "SELECT sequence_no, role, content, tool_calls, tool_results"
                " FROM messages WHERE run_id = %s ORDER BY sequence_no",
                (run_id,),
            ).fetchall()
            events = conn.execute(
                "SELECT sequence_no, event_type, payload, timestamp"
                " FROM run_events WHERE run_id = %s ORDER BY sequence_no",
                (run_id,),
            ).fetchall()
            manifest = conn.execute(
                "SELECT sdk_version, agent_spec_hash, instructions_hash, model_id,"
                " model_version, model_adapter_version, tool_spec_hashes, policy_version"
                " FROM execution_manifests WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        return {
            "run": run,
            "messages": [
                {"sequence_no": m[0], "role": m[1], "content": m[2],
                 "tool_calls": m[3], "tool_results": m[4]}
                for m in messages
            ],
            "events": [
                {"sequence_no": e[0], "event_type": e[1], "payload": e[2], "timestamp": e[3]}
                for e in events
            ],
            "manifest": manifest,
        }
