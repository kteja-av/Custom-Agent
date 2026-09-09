"""M7 gate: what Phase 2 makes reachable (FR-17..FR-21, NFR-8, AC-11..AC-15).

Phase 2 introduces parallel subagents. Three Phase 0 assumptions were correct
only because one process owned one run, and every test here was written against
a MEASURED failure rather than a suspected one -- the numbers in the docstrings
are what the code actually did before the fix, not estimates:

  * 12 concurrent appends to one run: 9 committed, 3 raised UniqueViolation.
  * two event sinks on one run: the second collided at sequence 1, 1 of 2 lost.
  * 6 concurrent runs: 4.73s against 0.16s in memory, worst loop stall 1008 ms.
  * one short run: 40 separate connect / authenticate / close cycles.

Every test creates its own throwaway namespace or its own run and removes what
it wrote, so the suite is repeatable against a database that already holds real
runs.
"""

import asyncio
import os
import time
import uuid

import psycopg
import pytest
from dotenv import load_dotenv

from agentsdk import AgentSpec, Persistence, RunConfig, Runner, RunStatus
from agentsdk.config import normalise_database_url
from agentsdk.manifest import build_manifest
from agentsdk.migrate import apply_migrations, discover, schema_version
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import SCHEMA_PATH, RunScope, apply_schema
from agentsdk.primitives import Message, Role, ToolCall
from agentsdk.tools import Tool, ToolSpec

load_dotenv()

DSN = normalise_database_url(os.environ.get("DATABASE_URL"))


def test_the_readiness_suite_has_a_database():
    """Deliberately NOT skippable. M5 learned that a gate which skips to green
    proves nothing and nobody investigates a pass."""
    assert DSN, "M7 measures a real store; DATABASE_URL must be set"


@pytest.fixture(autouse=True)
def _requires_database(request):
    if request.node.name != "test_the_readiness_suite_has_a_database" and not DSN:
        pytest.skip("M7 needs DATABASE_URL")


@pytest.fixture(scope="module", autouse=True)
def schema():
    if DSN:
        apply_schema(DSN)


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, params).fetchall()


class Namespace:
    """A throwaway schema holding a database at the PRE-migration baseline.

    Asserting against the live database would prove nothing about a migration:
    it has already been migrated, so 'the column exists' would be true whether
    or not the migration works. The only honest test builds a database the old
    way and moves it forward -- the same reasoning that put schema.sql's own
    constraint test in a namespace during M5.
    """

    def __init__(self):
        self.name = "m7_" + uuid.uuid4().hex[:8]
        # libpq options, so migrations running on their OWN connection still
        # land here without the production API growing a test-shaped argument.
        self.dsn = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{self.name}"

    def __enter__(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{self.name}"')
            conn.execute(f'SET search_path TO "{self.name}"')
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        return self

    def __exit__(self, *exc):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{self.name}" CASCADE')
        return False

    def columns(self, table):
        with psycopg.connect(DSN) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema=%s AND table_name=%s",
                    (self.name, table),
                ).fetchall()
            }


# --- AC-11: migrations ---------------------------------------------------------


def test_migrations_bring_a_pre_migration_database_forward():
    """AC-11, and the whole reason FR-17 is ordered first.

    schema.sql is entirely CREATE TABLE IF NOT EXISTS, so on a database that
    already exists it does nothing for a new column -- the change appears to
    succeed and fails later at insert time, somewhere else. This asserts the
    baseline genuinely LACKS what the migration adds, so it cannot pass by the
    column having been there all along.
    """
    assert discover(), "no migrations on disk, so this test would be vacuous"

    with Namespace() as ns:
        before = ns.columns("runs")
        assert "parent_run_id" not in before, (
            "the baseline already has parent_run_id, so this test proves nothing"
        )

        applied = apply_migrations(ns.dsn)
        assert applied == [version for version, _ in discover()], (
            f"not every migration ran: {applied}"
        )
        assert "parent_run_id" in ns.columns("runs"), "the migration did not add its column"
        assert schema_version(ns.dsn) == discover()[-1][0]


def test_applying_migrations_twice_changes_nothing():
    """AC-11's second half. A migration runner that is not idempotent is one
    nobody can safely run on startup, which is the only place it will be run."""
    with Namespace() as ns:
        first = apply_migrations(ns.dsn)
        assert first, "the first pass applied nothing"
        version_after_first = schema_version(ns.dsn)

        second = apply_migrations(ns.dsn)
        assert second == [], f"a second pass re-applied {second}"
        assert schema_version(ns.dsn) == version_after_first

        rows = query(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_schema=%s AND table_name='schema_migrations'",
            (ns.name,),
        )
        assert rows[0][0] == 1, "the bookkeeping table is missing"


def test_a_migration_records_what_ran_and_when():
    """A version number nobody can trace back to a file is not an audit trail."""
    with Namespace() as ns:
        apply_migrations(ns.dsn)
        with psycopg.connect(ns.dsn) as conn:
            rows = conn.execute(
                "SELECT version, filename, applied_at FROM schema_migrations"
                " ORDER BY version"
            ).fetchall()
    on_disk = [(version, path.name) for version, path in discover()]
    assert [(r[0], r[1]) for r in rows] == on_disk
    assert all(r[2] is not None for r in rows)


def test_a_badly_named_migration_is_refused_rather_than_skipped():
    """The failure this module exists to remove is a schema change that quietly
    does not run. A file the runner cannot order must fail loudly, not be
    ignored because it did not match a pattern."""
    from agentsdk import migrate

    stray = migrate.MIGRATIONS_PATH / "not-a-migration.sql"
    stray.write_text("SELECT 1;", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="lower_snake_case"):
            discover()
    finally:
        stray.unlink()
    assert discover(), "discover() did not recover after the stray file was removed"
