"""Versioned schema migrations (FR-17, AC-11).

`schema.sql` is the BASELINE -- version 0001 in effect, which is why the first
file in migrations/ is 0002. Every statement in it is `CREATE ... IF NOT
EXISTS`, which makes it perfect for creating a database and useless for changing
one: on a database that already exists, a column added to that file is silently
a no-op, and the code expecting the column fails later at insert time. So
changes after the baseline live in migrations/ as numbered files, applied in
order and recorded in `schema_migrations`.

  * `schema.sql` answers "how do I create this database from nothing".
  * `migrations/` answers "how do I bring an existing one forward".

Not every requirement needs a migration. FR-18 was written expecting a column
and needed none: event sequence numbers come from the stored maximum inside the
insert, exactly as message sequence numbers always did.

Concurrency. Phase 2 means several workers, and `Persistence.postgres()`
applies the schema on construction, so ALL of it -- the baseline included --
runs under one session-level advisory lock. The first version locked only the
migrations and ran schema.sql outside the lock while this docstring said
concurrent callers serialise: eight workers initialising an empty database at
once lost seven to UniqueViolation, in three trials in each of two review
rounds.
"""

from __future__ import annotations

import hashlib
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

# Added after the table already existed on real databases, so it arrives the
# way every later change must: as an idempotent ALTER, never as an edit to the
# CREATE above, which an existing database would silently ignore.
_CHECKSUM_COLUMN = "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum TEXT"


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


def checksum(path: Path) -> str:
    """SHA-256 of a migration's TEXT, newline-normalised.

    The committed repository is LF, but working copies are not: git settings
    decide what a checkout writes, and on the author's machine every file that
    a Windows patch script rewrote came back CRLF while the rest stayed LF.
    Hashing raw bytes would report an untouched migration as edited the first
    time it reached a machine with different settings, and a checksum that
    cries wolf gets deleted. read_text() already folds CRLF to LF.
    """
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def applied_versions(dsn: str) -> list[str]:
    """What this database has already had applied, oldest first.

    Read-only. The first version created the bookkeeping table here, outside
    any lock -- the same unlocked-DDL race the baseline had.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is None:
            return []
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    return [row[0] for row in rows]


def schema_version(dsn: str) -> str | None:
    """The highest applied version, or None on a database with no migrations."""
    versions = applied_versions(dsn)
    return versions[-1] if versions else None


def apply_migrations(dsn: str, *, baseline: Path | None = None) -> list[str]:
    """Bring a database to the current version. Returns the versions applied.

    `baseline`, when given, runs first and under the same lock; apply_schema
    passes schema.sql. Idempotent: a second call applies nothing.

    Each migration commits in its OWN transaction together with its bookkeeping
    row. A migration that fails leaves neither its changes nor a record claiming
    it ran, and everything before it stays applied and recorded, so the next
    attempt resumes instead of repeating. The first version ran every pending
    migration in one transaction while this docstring described one each.

    An applied migration whose file has since changed is refused by name rather
    than skipped: editing history is the other way a schema change appears to
    succeed and does not. All checksums are verified before anything is applied.
    """
    applied_now: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Session-scoped, because the work spans several transactions. Released
        # below, and by the server if this process dies holding it, so a crashed
        # worker cannot wedge the others.
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            if baseline is not None:
                with conn.transaction():
                    conn.execute(baseline.read_text(encoding="utf-8"))
            with conn.transaction():
                conn.execute(_MIGRATIONS_TABLE)
                conn.execute(_CHECKSUM_COLUMN)
            recorded = dict(
                conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
            )
            on_disk = [(version, path, checksum(path)) for version, path in discover()]

            for version, path, digest in on_disk:
                if version not in recorded:
                    continue
                stored = recorded[version]
                if stored is None:
                    # Applied before checksums existed. Trusted on first sight:
                    # the alternative refuses every database migrated before
                    # this change, which is every database there is.
                    with conn.transaction():
                        conn.execute(
                            "UPDATE schema_migrations SET checksum = %s"
                            " WHERE version = %s AND checksum IS NULL",
                            (digest, version),
                        )
                elif stored != digest:
                    raise ValueError(
                        f"migration {path.name} was changed after it was applied "
                        f"(recorded {stored[:12]}, file now {digest[:12]}); write a "
                        "new migration rather than editing an applied one"
                    )

            for version, path, digest in on_disk:
                if version in recorded:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT INTO schema_migrations (version, filename, checksum)"
                        " VALUES (%s, %s, %s)",
                        (version, path.name, digest),
                    )
                applied_now.append(version)
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            except psycopg.Error:
                # The session is already broken, and the server releases a
                # session lock when its session ends. Raising here would only
                # replace the real error with this one.
                pass
    return applied_now
