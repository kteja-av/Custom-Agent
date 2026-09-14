"""Checks that span the whole test session.

AC-44 / NFR-16 (M11): a full regression run leaves the store as it found it.
Before Phase 2 the golden-eval tests left 13 runs behind on every full run --
measured on 2026-09-14 against the M11 baseline -- and each file's own cleanup
could not see what another file left. So the set of run ids in every run-scoped
table is taken when the session starts and compared when it ends: a run added,
or one removed that the suite did not write, fails the session.

With no database configured this fails rather than skips (AC-19's rule): a
check that skips to green proves nothing.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from dotenv import load_dotenv

from agentsdk.config import normalise_database_url
from agentsdk.postgres import apply_schema

load_dotenv()

_DSN = normalise_database_url(os.environ.get("DATABASE_URL"))
_RUN_TABLES = ("runs", "messages", "run_events", "execution_manifests")


def _run_ids() -> dict[str, set]:
    with psycopg.connect(_DSN) as conn:
        return {
            table: {row[0] for row in conn.execute(f"SELECT DISTINCT run_id FROM {table}").fetchall()}
            for table in _RUN_TABLES
        }


@pytest.fixture(scope="session", autouse=True)
def the_store_is_left_as_it_was_found():
    assert _DSN, "AC-44 compares the store before and after the session: DATABASE_URL must be set"
    # So the tables exist to be read on a database this session is the first to use.
    apply_schema(_DSN)
    before = _run_ids()
    yield
    after = _run_ids()
    changed = {
        table: {"added": len(after[table] - before[table]), "removed": len(before[table] - after[table])}
        for table in _RUN_TABLES
        if after[table] != before[table]
    }
    assert not changed, f"the test session changed the store's runs: {changed}"
