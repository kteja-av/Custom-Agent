"""Postgres-backed stores (FR-9, FR-10, FR-11, NFR-2, LLD 2, 3.9).

One database, several stores. NOT one connection pool: every method opens its
own connection and closes it, which is fine for Phase 0's one-run-at-a-time
profile and is the first thing to change before any real load -- see the
recorded limitation. The protocols these
implement are defined in `session.py` and `events.py`, so the loop cannot tell
whether it is talking to memory or Postgres.

ADR-11 is enforced by the schema, not by these classes remembering: every table
declares tenant_id and project_id NOT NULL, so a row that cannot say who it
belongs to fails at the database rather than being caught by review.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

import psycopg
from psycopg.types.json import Jsonb

from .events import SCHEMA_VERSION, EventType, RunEvent
from .migrate import apply_migrations
from .model import Usage
from .primitives import (
    UNSTORABLE,
    refuse_unstorable_fields,
    ContentProvenance,
    InstructionAuthority,
    Message,
    Origin,
    Role,
    TaintFlag,
    ToolCall,
    ToolResult,
    TrustZone,
    unstorable_reason,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# One pool per DSN, shared by every store built on it (FR-20).
#
# Before this, every store method opened its own connection: a short run cost
# 40 separate connect / authenticate / close cycles, each a TCP handshake and
# an authentication round trip to do a single INSERT. That is affordable when
# one run happens at a time and is the first thing to collapse under Phase 2's
# fan-out.
#
# Keyed by DSN string rather than held on the store, because PostgresRunStore,
# PostgresSessionStore and PostgresEventStore are constructed separately for
# the same database and would otherwise each hold their own pool.
_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()

# Sized against the thread pool that will be calling in: asyncio.to_thread uses
# min(32, cpu_count + 4) workers by default, so a smaller pool would simply
# move the queue from one place to another.
POOL_MAX_SIZE = 32


def _pool(dsn: str) -> ConnectionPool:
    """The pool for this DSN, created once.

    Double-checked under a lock: two threads racing to create the pool for the
    same DSN would otherwise both build one, and whichever lost would leak its
    connections with nothing holding a reference to close them.
    """
    pool = _POOLS.get(dsn)
    if pool is not None:
        return pool
    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = ConnectionPool(
                dsn,
                min_size=1,
                max_size=POOL_MAX_SIZE,
                # A caller that waits forever for a connection is a hang with
                # no error; one that waits 30 seconds is a slow request with a
                # message naming the pool.
                timeout=30.0,
                # Validate on checkout (M7 round 2). Without it the pool handed
                # out connections the server had already closed -- a restart, a
                # failover, an idle kill -- and every run that drew one failed:
                # five dead connections, five failed runs, where the per-call
                # connections this pool replaced had simply reconnected. The
                # check is a round trip per checkout, made on a worker thread.
                #
                # A connection that dies DURING a write still fails that write,
                # and deliberately so: the insert may have committed before the
                # reply was lost, and retrying a MAX + 1 append would duplicate
                # the message rather than recover it.
                check=ConnectionPool.check_connection,
                open=True,
            )
            _POOLS[dsn] = pool
    return pool


def close_pools() -> None:
    """Close every pool. For process shutdown and for tests that count
    connections; not needed during normal operation."""
    with _POOLS_LOCK:
        while _POOLS:
            _, pool = _POOLS.popitem()
            pool.close()


def apply_schema(dsn: str) -> None:
    """Create the baseline, then bring it forward (FR-17) -- under one lock.

    schema.sql alone can only ever CREATE. On a database that already exists it
    is a no-op for anything new, so a column added to it would be silently
    absent and the code would fail later at insert time. Migrations run behind
    the same call, so every existing call site gets them without knowing.

    Both halves run inside apply_migrations' advisory lock. The first version
    ran schema.sql here, in autocommit and outside that lock, so workers
    initialising an empty database together raced on CREATE and all but one
    failed (M7 review rounds 1 and 2). This module now opens no connection of
    its own at all, which is what lets the event-loop test watch the pool
    alone.
    """
    apply_migrations(dsn, baseline=SCHEMA_PATH)


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

    def __post_init__(self) -> None:
        # These reach NOT NULL columns on every table, so an unstorable one
        # fails the write with an opaque psycopg error at some later point.
        # Refused here instead, where the caller can see which field it was.
        # The shared helper walks dataclasses.fields() rather than the three
        # names this used to list -- a field added here is covered.
        refuse_unstorable_fields(self)


# --- column fitness (M5 round 8) --------------------------------------------
#
# `unstorable_reason` answers "can this be serialised". That is not the same
# question as "does this fit the column it is going to", and round 8 rejected
# on the difference: max_turns was checked with the serialisability predicate
# while runs.max_turns is INTEGER, so 2**31 -- an ordinary int that passes
# RunConfig's own validation -- passed the guard and blew up at the write.
#
# A check that runs and returns the wrong answer is invisible to a test that
# only asks whether a check runs, which is exactly what the round-7 test did.
# So the checks below are keyed by the COLUMN TYPE each value is headed for.

_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1


def column_rejection_reason(value: Any, sql_type: str) -> str | None:
    """Why `value` cannot go into a column of this declared type."""
    if sql_type == "UUID" and isinstance(value, uuid.UUID):
        # Before the serialisability test, which refuses a uuid.UUID as a
        # TypeError -- and uuid.UUID is exactly what get_run and PostgresTrace
        # hand back for a run id, so passing one straight back as parent_run_id
        # was refused by the SDK's own output type (M7 round 2). A UUID object
        # cannot be malformed or non-canonical.
        return None
    reason = unstorable_reason(value)
    if reason is not None:
        return reason
    if sql_type == "UUID":
        # Refused HERE rather than at the write, for the same reason max_turns
        # is: a malformed id completes in memory and fails against Postgres
        # with a DataError several frames away from the caller that supplied
        # it. None is allowed -- a top-level run has no parent, which is the
        # common case rather than an exception.
        if value is not None:
            if not isinstance(value, str):
                return f"a {type(value).__name__} is not a UUID"
            try:
                canonical = str(uuid.UUID(value))
            except ValueError:
                return f"{value!r} is not a well-formed UUID"
            if value != canonical:
                # uuid.UUID() is more permissive than a Postgres uuid column:
                # it strips a "urn:uuid:" prefix the column refuses, so the
                # first version of this guard waved that form through and the
                # write failed with a DataError -- a guard answering a different
                # question from the column it protects, M5 round 8's shape,
                # found by a differential probe rather than by review. Only the
                # canonical form is accepted. That also refuses uppercase,
                # braced and unhyphenated forms the column WOULD take, which is
                # the safe direction: every run id this SDK issues is canonical.
                return f"{value!r} is not a canonical UUID (expected {canonical!r})"
    if sql_type == "INTEGER":
        if isinstance(value, bool):
            # The carve-out this replaces was the last surviving instance of
            # round 8's shape: a bool IS an int in Python, so it passed the
            # `int` test, and excluding it from the range check looked
            # harmless because True is trivially in range. But psycopg adapts
            # it to SQL boolean, and the column is integer -- DatatypeMismatch
            # at the write, after completing happily in memory. Copied from
            # token_count without re-asking what the exclusion was FOR.
            return "a bool is not an integer: an INTEGER column refuses it"
        if isinstance(value, int) and not (_INT32_MIN <= value <= _INT32_MAX):
            return (
                f"{value} is outside the range of an INTEGER column "
                f"({_INT32_MIN}..{_INT32_MAX})"
            )
    return None


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
        with _pool(self._dsn).connection() as conn:
            with conn.transaction():
                _serialise_writers(conn, run_id, _LOCK_MESSAGES)
                # sequence_no is computed INSIDE the insert's transaction, so
                # two concurrent appends cannot both read the same max and
                # produce a duplicate. The UNIQUE (run_id, sequence_no)
                # constraint is what makes the race a visible error instead of
                # a silently reordered history.
                #
                # tenant_id and project_id are taken from the RUN ROW, not from
                # the caller's scope. Trusting the scope let a caller file a
                # message under a tenant the run does not belong to, which
                # silently defeats the reason messages.tenant_id is
                # denormalised: isolation without a join is only worth having
                # if the denormalised copy cannot disagree with the original.
                # The scope is still matched in the WHERE, so a caller that
                # thinks it is writing for another tenant gets an error rather
                # than a quietly corrected row.
                cursor = conn.execute(
                    """
                    INSERT INTO messages (
                        message_id, run_id, tenant_id, project_id, sequence_no,
                        role, content, tool_calls, tool_results
                    )
                    SELECT %s, r.run_id, r.tenant_id, r.project_id,
                           COALESCE(MAX(m.sequence_no), 0) + 1,
                           %s, %s, %s, %s
                    FROM runs r LEFT JOIN messages m ON m.run_id = r.run_id
                    WHERE r.run_id = %s AND r.tenant_id = %s AND r.project_id = %s
                    GROUP BY r.run_id, r.tenant_id, r.project_id
                    """,
                    (
                        uuid.uuid4(),
                        message.role.value,
                        message.content,
                        tool_calls,
                        tool_results,
                        run_id,
                        scope.tenant_id,
                        scope.project_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"no run {run_id!r} for tenant {scope.tenant_id!r} / project "
                        f"{scope.project_id!r}: a message cannot be filed against a "
                        "run that does not exist or belongs to someone else"
                    )

    def history(self, run_id: str) -> list[Message]:
        """Tenant-scoped on READ as well as write.

        A bound store used to return any tenant's messages given a run id.
        Defensible on the grounds that run ids are UUIDv4 and FR-9's signature
        is history(run_id) -- but NFR-2's premise is that tenancy is enforced,
        not merely unguessable, and an id is a capability only until one leaks
        into a log or a support ticket. Enforcing it here costs one WHERE
        clause; the index on (tenant_id, project_id) already exists.
        """
        scope = self._scope
        if scope is None or scope.run_id != run_id:
            raise ValueError(
                "PostgresSessionStore must be bound to the run's scope before reading; "
                "tenancy is enforced on read as well as write (NFR-2)"
            )
        with _pool(self._dsn).connection() as conn:
            rows = conn.execute(
                """
                SELECT role, content, tool_calls, tool_results
                FROM messages
                WHERE run_id = %s AND tenant_id = %s AND project_id = %s
                ORDER BY sequence_no
                """,
                (run_id, scope.tenant_id, scope.project_id),
            ).fetchall()
        return [_message_from_row(row) for row in rows]


# The two sequence spaces one run owns. Separate keys so appending a message
# does not make an event wait behind it -- they are different sequences and
# have no reason to contend.
_LOCK_MESSAGES = 1
_LOCK_EVENTS = 2


def _serialise_writers(conn: Any, run_id: str, space: int) -> None:
    """Make concurrent writers to one run queue instead of race (FR-19).

    Both write paths compute their sequence number as MAX + 1 inside the
    insert. That is SAFE -- two writers cannot produce the same number
    unnoticed, because UNIQUE (run_id, sequence_no) turns the race into an
    error. It is not AVAILABLE: the loser's row is simply not written, and the
    caller gets a psycopg exception for a write that would have succeeded a
    millisecond later. Measured before this: 12 concurrent appends to one run,
    9 committed and 3 died.

    A bounded retry was the obvious alternative and is the wrong one here. Each
    round of contention lets exactly one writer through, so N simultaneous
    writers need N rounds, and any cap small enough to be safe is too small to
    help at the concurrency Phase 2 introduces.

    An advisory lock inverts that: writers queue, every one of them commits,
    and the number of round trips does not grow with contention. It is
    transaction-scoped, so it is released on commit, on rollback, and if this
    process dies -- a crashed writer cannot wedge a run. hashtext() may collide
    across different run ids, which costs two unrelated runs a moment of
    serialisation and can never cost correctness.

    The UNIQUE constraint stays exactly where it is. This lock is about
    availability; the constraint is what makes the invariant true at rest, and
    it still holds if a future writer forgets to take the lock.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s), %s)", (run_id, space))


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
        # sequence_no is assigned by the DATABASE below, not here (FR-18).
        # This process's own count is only ever right when this process is the
        # only writer, which stops being true the moment a run has a subagent
        # or is resumed: a second sink starts counting at 1 again and collides
        # with rows already stored. Measured before the fix -- two sinks on one
        # run, the second died with UniqueViolation and one of the two events
        # was lost. Zero is a placeholder that never reaches the database.
        event = RunEvent(
            event_type=event_type,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
            run_id=self._run_id,
            sequence_no=0,
            payload=payload or {},
            **identifiers,
        )
        with _pool(self._dsn).connection() as conn:
            _serialise_writers(conn, event.run_id, _LOCK_EVENTS)
            cursor = conn.execute(
                """
                INSERT INTO run_events (
                    event_id, schema_version, sequence_no, event_type,
                    tenant_id, project_id, run_id,
                    agent_id, task_id, tool_call_id, attempt_id,
                    parent_event_id, correlation_id, timestamp, payload
                )
                -- Tenancy from the RUN ROW, as messages already does
                -- (DECISION-aed7e4d8). Taking it from the caller let an event
                -- for tenant A's run be filed as tenant B, where it is
                -- invisible in A's trace -- the same decision made
                -- inconsistently one function over, for three rounds.
                --
                -- And the sequence number from the STORED maximum, computed
                -- inside this insert's transaction, exactly as
                -- PostgresSessionStore.append already does (FR-18). Two
                -- concurrent emits cannot both read the same max, and a second
                -- sink continues the sequence instead of restarting it.
                SELECT %s,%s,
                       COALESCE(MAX(e.sequence_no), 0) + 1,
                       %s, r.tenant_id, r.project_id, r.run_id,
                       %s,%s,%s,%s,%s,%s,%s,%s
                FROM runs r LEFT JOIN run_events e ON e.run_id = r.run_id
                WHERE r.run_id = %s AND r.tenant_id = %s AND r.project_id = %s
                GROUP BY r.run_id, r.tenant_id, r.project_id
                RETURNING sequence_no
                """,
                (
                    event.event_id,
                    event.schema_version,
                    event.event_type.value,
                    event.agent_id,
                    event.task_id,
                    event.tool_call_id,
                    event.attempt_id,
                    event.parent_event_id,
                    event.correlation_id,
                    event.timestamp,
                    Jsonb(_json_safe(event.payload)),
                    event.run_id,
                    self._tenant_id,
                    self._project_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                # Zero rows means the run does not exist or belongs to another
                # tenant. Raising rather than returning quietly: an event that
                # was not written is an entry the audit trail silently lacks,
                # which is the failure mode this whole milestone is about.
                raise ValueError(
                    f"no run {event.run_id!r} for tenant {self._tenant_id!r} / project "
                    f"{self._project_id!r}: an event cannot be filed against a run that "
                    "does not exist or belongs to someone else"
                )
        # The stored number, not the one this process guessed. events() and the
        # returned RunEvent must agree with the row, or an in-memory trace and
        # a reconstructed one disagree about order -- which is the thing NFR-3
        # exists to prevent.
        event = dataclasses.replace(event, sequence_no=row[0])
        self._buffer.append(event)
        return event

    def events(self) -> tuple[RunEvent, ...]:
        return tuple(self._buffer)


def _json_safe(value: Any) -> Any:
    """Payloads carry enums and datetimes; JSONB does not.

    Also the last line of defence for the audit trail. The primitives refuse
    unstorable values upstream, but a payload is assembled here from many
    sources -- ids, model names, a stringified object -- and an event that
    cannot be written is an event the trail simply lacks, which is strictly
    worse than one marked as unstorable. So a value that would fail the write
    is replaced with a marker naming the reason, rather than taking the run
    down with it.
    """
    if isinstance(value, dict):
        return {_json_safe(str(k)): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return _json_safe(value.value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        reason = unstorable_reason(value)
        return value if reason is None else f"{UNSTORABLE}: {reason}"
    rendered = str(value)
    reason = unstorable_reason(rendered)
    return rendered if reason is None else f"{UNSTORABLE}: {reason}"


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
        parent_run_id: str | None = None,
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
        # Every value this statement writes, not the two that were named in a
        # review caveat. Round 6's caveat listed tenant_id, project_id,
        # agent_spec_id and model_id; the repair implemented that list, and
        # round 7 rejected on principal_context -- the one caller-supplied
        # value the caveat had not enumerated. Keyed by column so a new column
        # is added here in the same edit that adds it to the INSERT.
        # Keyed by the column's declared type, not by one predicate for
        # everything: see column_rejection_reason.
        for name, value, sql_type in (
            ("agent_spec_id", agent_spec_id, "TEXT"),
            ("model_id", model_id, "TEXT"),
            ("max_turns", max_turns, "INTEGER"),
            ("principal_context", principal_context, "JSONB"),
            ("manifest", manifest, "JSONB"),
            ("parent_run_id", parent_run_id, "UUID"),
        ):
            reason = column_rejection_reason(value, sql_type)
            if reason is not None:
                raise ValueError(f"{name} cannot be stored: {reason}")
        with _pool(self._dsn).connection() as conn:
            with conn.transaction():
                cursor = conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, tenant_id, project_id, agent_spec_id, status,
                        principal_context, max_turns, model_id, parent_run_id
                    )
                    SELECT %s,%s,%s,%s,'running',%s,%s,%s,%s
                    -- A parent link may only point INSIDE the child's own
                    -- tenant and project (FR-21, ADR-11). The foreign key
                    -- alone would happily let a run in tenant B name a parent
                    -- in tenant A, which puts one tenant's run id in another
                    -- tenant's row and makes A's lineage readable from B.
                    -- Checked in the same statement as the insert, so there is
                    -- no window between the check and the write.
                    WHERE %s::uuid IS NULL
                       OR EXISTS (
                            SELECT 1 FROM runs parent
                            WHERE parent.run_id = %s::uuid
                              AND parent.tenant_id = %s
                              AND parent.project_id = %s
                          )
                    """,
                    (
                        scope.run_id,
                        scope.tenant_id,
                        scope.project_id,
                        agent_spec_id,
                        Jsonb(principal_context) if principal_context else None,
                        max_turns,
                        model_id,
                        parent_run_id,
                        parent_run_id,
                        parent_run_id,
                        scope.tenant_id,
                        scope.project_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"parent run {parent_run_id!r} does not exist in tenant "
                        f"{scope.tenant_id!r} / project {scope.project_id!r}: a run "
                        "may only descend from one its own tenant can see"
                    )
                self._insert_manifest(conn, scope, manifest)

    # FR-31: the Runner hands this store a run's usage and cost with its status.
    # Declared rather than inferred from finish_run's signature: see Runner._finish.
    records_accounting = True

    # FR-31: the columns migration 0003 created, one per Usage field. Named
    # here rather than read off Usage because they are what the migration made;
    # the M9 persistence test compares every one of them with the RunResult.
    _USAGE_COLUMNS = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    )

    def finish_run(
        self,
        scope: RunScope,
        status: str,
        usage: Usage | None = None,
        cost_usd: Decimal | None = None,
    ) -> None:
        """Tenant-scoped, like every other statement here.

        history() was scoped by DECISION-e692386f on the premise that NFR-2
        means tenancy is enforced rather than unguessable. Leaving the writes
        and the trace unscoped answered the same question the other way one
        function over, which is worse than either answer consistently applied.

        The run's token totals and cost go in the same statement as its status
        (FR-31), so a terminal row never exists without them. A value no column
        can hold is written NULL rather than failing the write -- accounting
        never fails a run (NFR-11) -- and a caller that passes neither, as every
        caller before M9 did, writes NULL: unknown, not zero.
        """
        counts = [self._bigint_or_null(getattr(usage, name, None)) for name in self._USAGE_COLUMNS]
        with _pool(self._dsn).connection() as conn:
            conn.execute(
                "UPDATE runs SET status = %s, completed_at = %s, "
                + ", ".join(f"{name} = %s" for name in self._USAGE_COLUMNS)
                + ", cost_usd = %s"
                " WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                (
                    status,
                    datetime.now(timezone.utc),
                    *counts,
                    self._numeric_or_null(cost_usd),
                    scope.run_id,
                    scope.tenant_id,
                    scope.project_id,
                ),
            )

    @staticmethod
    def _bigint_or_null(value: Any) -> int | None:
        """A token count as a BIGINT column holds it, or None if it cannot.

        Usage keeps any int a provider sends, and psycopg refuses one past the
        interpreter's digit limit before the database is even asked.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if -(2**63) <= value < 2**63 else None

    @staticmethod
    def _numeric_or_null(value: Any) -> Decimal | None:
        """A cost as a NUMERIC column holds it: at most 131072 digits before the
        point and 16383 after. None otherwise, and for anything not a finite
        Decimal."""
        try:
            if not isinstance(value, Decimal) or not value.is_finite():
                return None
            if value.as_tuple().exponent < -16383 or value.adjusted() >= 131072:
                return None
            return value
        except Exception:  # noqa: BLE001 - accounting never fails a run
            return None

    def write_manifest(self, scope: RunScope, manifest: dict[str, Any]) -> None:
        """Write a manifest for a run that already exists.

        Not on the Runner's path -- `start_run` writes the manifest atomically
        with the run row. This one cannot reintroduce that defect: it only ever
        adds a manifest, so it cannot leave a run without one. A second call
        for the same run is refused by the primary key, not by care.
        """
        with _pool(self._dsn).connection() as conn:
            self._insert_manifest(conn, scope, manifest)

    @staticmethod
    def _insert_manifest(
        conn: psycopg.Connection, scope: RunScope, manifest: dict[str, Any]
    ) -> None:
        """Takes the caller's connection so it can join an open transaction.

        Three columns arrived with migration 0003 (FR-31): the effective output
        limit, the reasoning effort, and the prices the run was costed with.
        The last arrived with 0004 (FR-43): the scheduler limits the run
        executed under. Each is NULL for a manifest built without it.
        """
        pricing = manifest.get("pricing")
        scheduler_limits = manifest.get("scheduler_limits")
        conn.execute(
            """
                INSERT INTO execution_manifests (
                    run_id, tenant_id, project_id, sdk_version, agent_spec_hash,
                    instructions_hash, model_id, model_version,
                    model_adapter_version, tool_spec_hashes, policy_version,
                    max_output_tokens, reasoning_effort, pricing, scheduler_limits
                )
                -- Tenancy from the run row, as everywhere else. Inside
                -- start_run the row is written in this same transaction, so
                -- the SELECT sees it.
                SELECT r.run_id, r.tenant_id, r.project_id, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                FROM runs r
                WHERE r.run_id = %s AND r.tenant_id = %s AND r.project_id = %s
            """,
            (
                manifest["sdk_version"],
                manifest["agent_spec_hash"],
                manifest["instructions_hash"],
                manifest.get("model_id"),
                manifest.get("model_version"),
                manifest.get("model_adapter_version"),
                Jsonb(manifest.get("tool_spec_hashes") or []),
                manifest.get("policy_version"),
                manifest.get("max_output_tokens"),
                manifest.get("reasoning_effort"),
                Jsonb(pricing) if pricing is not None else None,
                Jsonb(scheduler_limits) if scheduler_limits is not None else None,
                scope.run_id,
                scope.tenant_id,
                scope.project_id,
            ),
        )

    def get_run(self, scope: RunScope) -> dict[str, Any] | None:
        # The usage columns and cost_usd are appended after parent_run_id, so
        # no key an earlier caller reads changes (FR-31).
        keys = (
            "run_id", "tenant_id", "project_id", "agent_spec_id", "status",
            "principal_context", "max_turns", "model_id", "started_at",
            "completed_at", "parent_run_id", *self._USAGE_COLUMNS, "cost_usd",
        )
        with _pool(self._dsn).connection() as conn:
            row = conn.execute(
                f"SELECT {', '.join(keys)} FROM runs"
                " WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                (scope.run_id, scope.tenant_id, scope.project_id),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(keys, row))


class PostgresTrace:
    """AC-7: reconstruct a run from persisted state PLUS ordered events.

    Events alone were never the source of truth, so this reads all three and
    says so in its shape rather than pretending the event stream is enough.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._runs = PostgresRunStore(dsn)

    def reconstruct(self, scope: RunScope) -> dict[str, Any]:
        """AC-7's own vehicle, so it enforces tenancy rather than deciding it
        differently from the store it reads beside."""
        run = self._runs.get_run(scope)
        tenancy = (scope.run_id, scope.tenant_id, scope.project_id)
        with _pool(self._dsn).connection() as conn:
            messages = conn.execute(
                "SELECT sequence_no, role, content, tool_calls, tool_results"
                " FROM messages WHERE run_id = %s AND tenant_id = %s AND project_id = %s"
                " ORDER BY sequence_no",
                tenancy,
            ).fetchall()
            events = conn.execute(
                "SELECT sequence_no, event_type, payload, timestamp"
                " FROM run_events WHERE run_id = %s AND tenant_id = %s AND project_id = %s"
                " ORDER BY sequence_no",
                tenancy,
            ).fetchall()
            manifest = conn.execute(
                "SELECT sdk_version, agent_spec_hash, instructions_hash, model_id,"
                " model_version, model_adapter_version, tool_spec_hashes, policy_version"
                " FROM execution_manifests"
                " WHERE run_id = %s AND tenant_id = %s AND project_id = %s",
                tenancy,
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
