"""Versioned schema migrations (FR-17, AC-11).

`schema.sql` is the BASELINE: every statement in it is `CREATE ... IF NOT
EXISTS`, which makes it perfect for creating a database and useless for
changing one. On a database that already exists, adding a column to that file
is silently a no-op, and the code that expects the column then fails at insert
time with an undefined-column error -- a schema change that appears to succeed
and fails later, somewhere else.

So changes after the baseline live here instead, as numbered files applied in
order and recorded in `schema_migrations`. The split is deliberate:

  * `schema.sql` answers "how do I create this database from nothing".
  * `migrations/` answers "how do I bring an existing one forward".

`apply_schema` runs both, so every existing call site gets migrations without
knowing about them.

Concurrency is not hypothetical here -- Phase 2's whole point is several
workers at once, and `Persistence.postgres()` applies the schema on
construction. A transaction-scoped advisory lock makes simultaneous callers
serialise instead of racing on CREATE and INSERT.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg

MIGRATIONS_PATH = Path(__file__).with_name("migrations")

# One arbitrary but stable key, so every process in this codebase contends for
# the same lock and nothing else in the database contends for it by accident.
_LOCK_KEY = 8_675_309

_VERSION_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT        PRIMARY KEY,
    filename    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def discover() -> list[tuple[str, Path]]:
    """Every migration on disk, ordered by version.

    A file whose name does not match NNNN_lower_snake_case.sql is a mistake
    worth failing on rather than skipping quietly: a migration that silently
    does not run is the exact failure mode this module exists to remove.
    """
    if not MIGRATIONS_PATH.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for path in sorted(MIGRATIONS_PATH.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        match = _VERSION_PATTERN.match(path.name)
        if match is None:
            raise ValueError(
                f"migration {path.name!r} is not named NNNN_lower_snake_case.sql; "
                "rename it rather than leaving it unapplied"
            )
        found.append((match.group(1), path))
    versions = [version for version, _ in found]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise ValueError(f"duplicate migration version(s): {sorted(duplicates)}")
    return found


def applied_versions(dsn: str) -> list[str]:
    """What this database has already had applied, oldest first."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(_MIGRATIONS_TABLE)
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    return [row[0] for row in rows]


def schema_version(dsn: str) -> str | None:
    """The highest applied version, or None on a database with no migrations."""
    versions = applied_versions(dsn)
    return versions[-1] if versions else None


def apply_migrations(dsn: str) -> list[str]:
    """Apply every pending migration in order. Returns the versions applied.

    Idempotent: a second call returns an empty list and changes nothing. Each
    migration and its bookkeeping row commit in ONE transaction, so a migration
    that fails halfway leaves neither its changes nor a record claiming it ran.
    """
    pending_applied: list[str] = []
    with psycopg.connect(dsn) as conn:
        # Transaction-scoped: released on commit or rollback, including if this
        # process dies, so a crashed migration cannot wedge every other worker.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        conn.execute(_MIGRATIONS_TABLE)
        done = {
            row[0]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for version, path in discover():
            if version in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (version, filename) VALUES (%s, %s)",
                (version, path.name),
            )
            pending_applied.append(version)
    return pending_applied
