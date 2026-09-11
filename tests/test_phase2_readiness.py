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
import statistics
import threading
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

    def __init__(self, baseline=True):
        self.baseline = baseline
        self.name = "m7_" + uuid.uuid4().hex[:8]
        # libpq options, so migrations running on their OWN connection still
        # land here without the production API growing a test-shaped argument.
        self.dsn = DSN + ("&" if "?" in DSN else "?") + f"options=-csearch_path%3D{self.name}"

    def __enter__(self):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{self.name}"')
            conn.execute(f'SET search_path TO "{self.name}"')
            if self.baseline:
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


# --- AC-12: event sequence numbers come from the database ----------------------


class Run:
    """A real run row to write against, removed afterwards."""

    def __init__(self, tag):
        self.scope = RunScope(
            run_id=str(uuid.uuid4()), tenant_id="SYN-m7-" + tag,
            project_id="p-" + tag,
        )

    def __enter__(self):
        Persistence.postgres(DSN).runs.start_run(
            self.scope,
            agent_spec_id="m7",
            max_turns=4,
            model_id="m7",
            principal_context=None,
            manifest=build_manifest(
                sdk_version="m7", agent_spec_id="m7", instructions="m7",
                tool_profile=(), tool_spec_hashes=[], model_id="m7",
            ),
        )
        return self.scope

    def __exit__(self, *exc):
        """Remove this run AND every run descended from it, in one statement.

        Deleting only this run's row fails whenever a child still references it
        through parent_run_id -- and it failed AFTER the parent's manifest had
        already been deleted. So a test that failed with a child created left
        the parent behind with no manifest and the child with one: exactly what
        a mutant dropping the parent project check leaked, two SYN rows and one
        new manifest-less orphan. Leaked debris has falsified this project's
        mutation matrices before, so cleanup must survive the failure it is
        cleaning up after.
        """
        with psycopg.connect(DSN, autocommit=True) as conn:
            ids = [
                row[0] for row in conn.execute(
                    "WITH RECURSIVE tree AS ("
                    " SELECT run_id FROM runs WHERE run_id = %s"
                    " UNION SELECT r.run_id FROM runs r JOIN tree t ON r.parent_run_id = t.run_id"
                    ") SELECT run_id FROM tree",
                    (self.scope.run_id,),
                ).fetchall()
            ]
            if ids:
                for table in ("run_events", "messages", "execution_manifests"):
                    conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
                # One statement, so the self-referencing foreign key is checked
                # after parent and children are both gone.
                conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (ids,))
        return False


def sinks_for(scope, count):
    from agentsdk.postgres import PostgresEventStore

    return [
        PostgresEventStore(DSN, scope.tenant_id, scope.project_id, scope.run_id)
        for _ in range(count)
    ]


def test_a_second_event_sink_continues_the_sequence_instead_of_restarting_it():
    """AC-12. This is the subagent case, and also every resumed run.

    Before FR-18 each sink counted with len(self._buffer) + 1, so a second sink
    for one run started at 1 again and collided with rows already stored:
    measured as UniqueViolation on the second emit, with one of the two events
    lost. A sequence number that is only correct while one process owns the run
    is not a sequence number, it is a local variable.
    """
    from agentsdk.events import EventType

    with Run("ac12") as scope:
        first, second = sinks_for(scope, 2)

        a = first.emit(EventType.RUN_STARTED, {"who": "parent"})
        b = second.emit(EventType.MODEL_CALLED, {"who": "child"})
        c = first.emit(EventType.RUN_COMPLETED, {"who": "parent"})

        stored = [
            row[0] for row in query(
                "SELECT sequence_no FROM run_events WHERE run_id=%s ORDER BY sequence_no",
                (scope.run_id,),
            )
        ]
        assert stored == [1, 2, 3], f"sequences are not unique and contiguous: {stored}"
        assert [a.sequence_no, b.sequence_no, c.sequence_no] == [1, 2, 3], (
            "the returned events disagree with the stored rows, so an in-memory "
            "trace and a reconstructed one would order differently (NFR-3)"
        )


def test_a_fresh_sink_resumes_from_the_stored_maximum():
    """The resumed-run case, and the reason the first version of this test was
    worthless.

    Written first as "the returned number equals the stored number" with a
    single sink from scratch -- which passed against the UNFIXED code, because
    a lone sink counting from 1 agrees with the database by coincidence. A test
    that cannot fail for the defect it names is a coverage test
    (KNOWLEDGE-41611bbf). The property only has teeth when the process's own
    count and the stored maximum DISAGREE, so this seeds rows from one sink and
    then makes a brand-new one continue them, exactly as a resumed run does.
    """
    from agentsdk.events import EventType

    with Run("ac12b") as scope:
        (writer,) = sinks_for(scope, 1)
        for i in range(3):
            writer.emit(EventType.MODEL_CALLED, {"i": i})

        # A new sink: empty buffer, three rows already stored.
        (resumed,) = sinks_for(scope, 1)
        fourth = resumed.emit(EventType.TOOL_CALLED, {"i": 3})
        fifth = resumed.emit(EventType.RUN_COMPLETED, {"i": 4})

        assert [fourth.sequence_no, fifth.sequence_no] == [4, 5], (
            "a fresh sink restarted the sequence instead of continuing it"
        )
        stored = {
            row[0]: str(row[1]) for row in query(
                "SELECT sequence_no, event_id FROM run_events WHERE run_id=%s",
                (scope.run_id,),
            )
        }
        assert sorted(stored) == [1, 2, 3, 4, 5]
        for event in (fourth, fifth):
            assert stored[event.sequence_no] == str(event.event_id), (
                "the returned event disagrees with the row it created, so an "
                "in-memory trace and a reconstructed one would order "
                "differently (NFR-3)"
            )


# --- AC-13: concurrent writers are available, not merely safe -------------------


WRITERS = 12
# One barrier release does not reliably collide: measured against the UNFIXED
# code, a single round of 12 message writers went cleanly through on 2 of 5
# attempts. A test that detects the defect most of the time is a flaky
# detector, and a flaky detector on a gate is how a regression gets in on a
# lucky afternoon. Three rounds against the same run makes a miss require
# three consecutive lucky schedules -- and it also exercises the case that
# matters most, contention against a sequence that is already non-empty.
ROUNDS = 3


async def _race(work):
    """Run `work(n)` for every writer, released together by a barrier.

    Without the barrier the writers start staggered and mostly miss each other:
    the original probe saw 9 of 12 commit precisely because they were not
    perfectly simultaneous. A test for a race that does not actually race is
    the coverage-vs-fitness shape wearing a stopwatch.
    """
    failures = []
    for rnd in range(ROUNDS):
        barrier = asyncio.Barrier(WRITERS)

        async def one(n, rnd=rnd):
            await barrier.wait()
            try:
                await asyncio.to_thread(work, rnd * WRITERS + n)
                return None
            except Exception as exc:  # noqa: BLE001 - the failure IS the measurement
                return f"{type(exc).__name__}: {exc}"

        failures.extend(await asyncio.gather(*(one(n) for n in range(WRITERS))))
    return failures


TOTAL = WRITERS * ROUNDS


async def test_twelve_concurrent_message_writers_all_commit():
    """AC-13. Before FR-19: 9 of 12 committed and 3 raised UniqueViolation --
    a subagent's message dropped, and a raw driver exception handed to a caller
    for a write that would have succeeded a millisecond later."""
    with Run("ac13m") as scope:
        store = Persistence.postgres(DSN).session_store_for(scope)

        failures = await _race(
            lambda n: store.append(
                scope.run_id, Message(role=Role.ASSISTANT, content=f"writer {n}")
            )
        )

        lost = [f for f in failures if f]
        assert not lost, (
            f"{len(lost)} of {TOTAL} writers lost their message: {sorted(set(lost))}"
        )
        seqs = [
            row[0] for row in query(
                "SELECT sequence_no FROM messages WHERE run_id=%s ORDER BY sequence_no",
                (scope.run_id,),
            )
        ]
        assert seqs == list(range(1, TOTAL + 1)), (
            f"sequences are not unique and contiguous: {seqs}"
        )
        contents = {
            row[0] for row in query(
                "SELECT content FROM messages WHERE run_id=%s", (scope.run_id,)
            )
        }
        assert contents == {f"writer {n}" for n in range(TOTAL)}, (
            "every writer committed a row, but not every writer's CONTENT is "
            "there -- a row was overwritten rather than appended"
        )


async def test_twelve_concurrent_event_writers_all_commit():
    """AC-13 for the other sequence space. Events race exactly as messages do,
    and were fixed by the same helper -- so they need their own assertion, or
    one of the two call sites could lose the lock with the suite still green."""
    from agentsdk.events import EventType

    with Run("ac13e") as scope:
        sinks = sinks_for(scope, WRITERS)

        failures = await _race(
            lambda n: sinks[n % WRITERS].emit(EventType.MODEL_CALLED, {"n": n})
        )

        lost = [f for f in failures if f]
        assert not lost, f"{len(lost)} of {TOTAL} events were lost: {sorted(set(lost))}"
        seqs = [
            row[0] for row in query(
                "SELECT sequence_no FROM run_events WHERE run_id=%s ORDER BY sequence_no",
                (scope.run_id,),
            )
        ]
        assert seqs == list(range(1, TOTAL + 1)), (
            f"sequences are not unique and contiguous: {seqs}"
        )
        payloads = {
            row[0]["n"] for row in query(
                "SELECT payload FROM run_events WHERE run_id=%s", (scope.run_id,)
            )
        }
        assert payloads == set(range(TOTAL))


async def test_a_write_for_someone_elses_run_still_fails():
    """FR-19 must buy availability, not silence.

    Serialising writers means a losing writer now waits instead of failing --
    which would be a defect if it also meant a write that SHOULD fail quietly
    succeeded. The tenancy check is the one that must survive the change.
    """
    with Run("ac13x") as scope:
        impostor = RunScope(
            run_id=scope.run_id, tenant_id="SYN-m7-other", project_id="p-other"
        )
        store = Persistence.postgres(DSN).session_store_for(impostor)
        with pytest.raises(ValueError, match="does not exist or belongs to someone else"):
            store.append(impostor.run_id, Message(role=Role.ASSISTANT, content="nope"))

        assert query(
            "SELECT count(*) FROM messages WHERE run_id=%s", (scope.run_id,)
        )[0][0] == 0


# --- AC-14 / NFR-8: persistence is not the concurrency ceiling -----------------


MAX_STALL_SECONDS = 0.050   # NFR-8
MAX_WALL_RATIO = 3.0        # NFR-8


class NoNetworkModel:
    """A model client that sleeps instead of calling anything.

    The point is to make the STORE the only thing that can be slow. With a real
    provider on the other end, a 1 ms store call hides inside a 900 ms round
    trip and no measurement means anything.
    """

    async def send(self, request):
        await asyncio.sleep(0.05)
        assistant_turns = sum(1 for m in request.messages if m.role is Role.ASSISTANT)
        if assistant_turns >= 2:
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, content="done"),
                stop_reason=StopReason.END_TURN,
                usage=Usage(1, 1, 2),
            )
        return ModelResponse(
            message=Message(
                role=Role.ASSISTANT,
                tool_calls=(ToolCall(id=f"c{assistant_turns}", name="noop", arguments={}),),
            ),
            stop_reason=StopReason.TOOL_CALLS,
            usage=Usage(1, 1, 2),
        )


NOOP_TOOLS = [
    Tool(
        spec=ToolSpec(
            name="noop",
            description="Does nothing.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        fn=lambda: "ok",
    )
]


async def _measure(concurrent_runs, persistence, tenant):
    """Wall time and the worst event-loop stall while `concurrent_runs` run.

    The heartbeat is an ordinary well-behaved coroutine asking to be woken every
    10 ms. However late it actually wakes is how long something else held the
    loop, which is the thing NFR-8 is about -- wall time alone cannot tell a
    slow store from a blocked one.
    """
    lags = []
    stop = False

    async def heartbeat():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            lags.append(now - last - 0.01)
            last = now

    async def one():
        runner = Runner({"m": NoNetworkModel()}, tools=NOOP_TOOLS, persistence=persistence)
        return await runner.run(
            AgentSpec(id="nfr8", instructions="go", tool_profile=("noop",)),
            "do it",
            RunConfig(tenant_id=tenant, project_id="p-nfr8", max_turns=6),
        )

    beat = asyncio.create_task(heartbeat())
    started = time.perf_counter()
    results = await asyncio.gather(*(one() for _ in range(concurrent_runs)))
    wall = time.perf_counter() - started
    stop = True
    await beat

    assert all(r.status is RunStatus.COMPLETED for r in results), (
        "a run failed, so the timing below measures the wrong thing"
    )
    return wall, max(lags) if lags else 0.0


def _drop_runs(tenant):
    with psycopg.connect(DSN, autocommit=True) as conn:
        ids = [
            row[0] for row in conn.execute(
                "SELECT run_id FROM runs WHERE tenant_id=%s", (tenant,)
            ).fetchall()
        ]
        for table in ("run_events", "messages", "execution_manifests"):
            conn.execute(f"DELETE FROM {table} WHERE run_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM runs WHERE tenant_id=%s", (tenant,))


ATTEMPTS = 3


async def _assert_within_nfr8(concurrent_runs, tag):
    """Assert NFR-8 on the MEDIAN of several attempts, not on one.

    A single sample makes this a flaky gate in both directions, which was not
    a prediction: at 24 runs it measured 15.6 ms standalone and then 50.5 ms
    against a 50 ms bound with no code change between, purely from what else
    the suite had been doing. Tuning the bound to make that pass would be
    fitting the requirement to the code.

    What the median does NOT do is make this a detector. Measured across three
    builds after M7 round 1: with the store offload fully reverted, six runs
    still stalled a median 16 ms and would pass; with three event writes left
    on the loop -- the defect round 1 rejected -- six and twenty-four runs both
    stayed under 30 ms. A bound this close to Windows' ~15 ms timer tick cannot
    tell those builds from the fixed one. So this is the acceptance measurement
    AC-14 asks for, and nothing more. Regressions are caught by the untimed
    thread-identity test and the lock-hold test below, both of which failed
    against the unrepaired code.

    An earlier version of this docstring claimed a pool-only build stalled
    51-57 ms on five consecutive tries, "so a median is just as red as a
    maximum there". That did not reproduce, and it is why this paragraph is
    here rather than silently rewritten.
    """
    tenant = "SYN-m7-" + tag
    samples = []
    try:
        for _ in range(ATTEMPTS):
            # Both halves in the SAME process, so the bound is about this code
            # and not about how fast the machine happens to be today.
            memory_wall, _ = await _measure(concurrent_runs, None, tenant)
            pg_wall, worst_stall = await _measure(
                concurrent_runs, Persistence.postgres(DSN), tenant
            )
            samples.append((worst_stall, pg_wall, memory_wall))
            _drop_runs(tenant)
    finally:
        _drop_runs(tenant)

    worst_stall = statistics.median(sample[0] for sample in samples)
    pg_wall = statistics.median(sample[1] for sample in samples)
    memory_wall = statistics.median(sample[2] for sample in samples)
    ratio = pg_wall / memory_wall if memory_wall else float("inf")
    assert worst_stall < MAX_STALL_SECONDS, (
        f"{concurrent_runs} concurrent runs stalled the event loop for "
        f"{worst_stall * 1000:.1f} ms, the median of {ATTEMPTS} attempts "
        f"({[round(x[0] * 1000, 1) for x in samples]} ms), against NFR-8's "
        f"{MAX_STALL_SECONDS * 1000:.0f} ms. "
        "Persistence is blocking the loop, which also starves any progress "
        "surface built on it."
    )
    assert ratio <= MAX_WALL_RATIO, (
        f"{concurrent_runs} concurrent runs took {pg_wall:.2f}s against "
        f"{memory_wall:.2f}s in memory ({ratio:.1f}x, NFR-8 allows "
        f"{MAX_WALL_RATIO:.0f}x)"
    )


async def test_six_concurrent_runs_stay_within_nfr8():
    """AC-14 exactly as specified -- an acceptance measurement, not a detector.

    Measured before any of FR-20: 4.73s against 0.16s in memory, worst stall
    1008 ms. With the repair: median 12.6 ms across six samples, max 15.5 ms.

    It cannot catch a regression. An earlier docstring here called it "the
    reliable detector of the two" on the strength of one session in which a
    pool-only build failed it 5 times out of 5. That did not reproduce: the M7
    round 1 reviewer saw it pass 6 of 6 with the offload reverted, and a later
    measurement put that build's median at 16 ms. Both sessions happened, and
    together they describe a test whose verdict depends on the afternoon.
    """
    await _assert_within_nfr8(6, "nfr8a")


def test_the_store_uses_one_pool_per_dsn_rather_than_a_connection_per_call():
    """FR-20's other half. A short run cost 40 connect / authenticate / close
    cycles before this; pooling is what makes a per-call connection affordable
    enough to stop being the thing that dominates."""
    from agentsdk import postgres

    first = postgres._pool(DSN)
    second = postgres._pool(DSN)
    assert first is second, "a second store built its own pool for the same DSN"
    assert first.max_size == postgres.POOL_MAX_SIZE
    # Distinct databases must not share one: the pool is keyed by DSN, and a
    # pool that ignored the key would hand out connections to the wrong server.
    other = postgres._pool(DSN + ("&" if "?" in DSN else "?") + "application_name=m7probe")
    assert other is not first
    other.close()
    postgres._POOLS.pop(
        DSN + ("&" if "?" in DSN else "?") + "application_name=m7probe", None
    )


# --- AC-15: a run may record the run that spawned it ---------------------------


def _start(scope, parent_run_id=None):
    Persistence.postgres(DSN).runs.start_run(
        scope,
        agent_spec_id="m7",
        max_turns=4,
        model_id="m7",
        principal_context=None,
        parent_run_id=parent_run_id,
        manifest=build_manifest(
            sdk_version="m7", agent_spec_id="m7", instructions="m7",
            tool_profile=(), tool_spec_hashes=[], model_id="m7",
        ),
    )


def test_a_child_run_records_its_parent_and_both_reconstruct():
    """AC-15. Phase 2's subagents are runs, and a subagent whose trace cannot
    be tied to its parent's leaves NFR-3 true of one run and useless for the
    tree that actually did the work."""
    from agentsdk.postgres import PostgresTrace

    with Run("ac15") as parent:
        child = RunScope(
            run_id=str(uuid.uuid4()),
            tenant_id=parent.tenant_id,
            project_id=parent.project_id,
        )
        try:
            _start(child, parent_run_id=parent.run_id)

            trace = PostgresTrace(DSN).reconstruct(child)
            assert str(trace["run"]["parent_run_id"]) == parent.run_id, (
                "the child's trace does not name its parent"
            )
            assert PostgresTrace(DSN).reconstruct(parent)["run"]["parent_run_id"] is None, (
                "a top-level run was given a parent"
            )

            # The read pattern the column exists for.
            children = query(
                "SELECT run_id FROM runs WHERE parent_run_id=%s", (parent.run_id,)
            )
            assert [str(row[0]) for row in children] == [child.run_id]
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute("DELETE FROM execution_manifests WHERE run_id=%s", (child.run_id,))
                conn.execute("DELETE FROM runs WHERE run_id=%s", (child.run_id,))


def test_a_child_carries_its_own_tenancy_rather_than_inheriting_it():
    """AC-15's second half. ADR-11 is not relaxed for child runs: the parent
    link is a lineage fact, never a substitute for tenancy."""
    from agentsdk.postgres import PostgresTrace

    with Run("ac15t") as parent:
        child = RunScope(
            run_id=str(uuid.uuid4()),
            tenant_id=parent.tenant_id,
            project_id=parent.project_id,
        )
        try:
            _start(child, parent_run_id=parent.run_id)
            row = query(
                "SELECT tenant_id, project_id FROM runs WHERE run_id=%s", (child.run_id,)
            )[0]
            assert row == (child.tenant_id, child.project_id), (
                "the child row does not carry its own tenancy"
            )
            # And it is invisible to anyone else, parent link or not.
            stranger = RunScope(
                run_id=child.run_id, tenant_id="SYN-m7-stranger", project_id="p-stranger"
            )
            assert PostgresTrace(DSN)._runs.get_run(stranger) is None
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute("DELETE FROM execution_manifests WHERE run_id=%s", (child.run_id,))
                conn.execute("DELETE FROM runs WHERE run_id=%s", (child.run_id,))


def test_a_parent_in_another_tenant_is_refused():
    """The foreign key alone is not enough, and this is the assertion that says
    so. `runs.parent_run_id REFERENCES runs (run_id)` is satisfied by ANY
    existing run, so without the tenancy check a run in tenant B could name a
    parent in tenant A -- putting one tenant's run id inside another tenant's
    row and making A's lineage readable from B. Refused in the same statement
    as the insert, so there is no window between the check and the write.
    """
    with Run("ac15a") as parent:
        intruder = RunScope(
            run_id=str(uuid.uuid4()),
            tenant_id="SYN-m7-other-tenant",
            project_id="p-other",
        )
        with pytest.raises(ValueError, match="may only descend from one its own tenant"):
            _start(intruder, parent_run_id=parent.run_id)

        assert query("SELECT 1 FROM runs WHERE run_id=%s", (intruder.run_id,)) == [], (
            "the run was written despite naming a parent it cannot see"
        )
        assert query(
            "SELECT 1 FROM execution_manifests WHERE run_id=%s", (intruder.run_id,)
        ) == [], "the manifest survived a refused run, so the two are not one transaction"


def test_a_parent_that_does_not_exist_is_refused():
    """A dangling link is worse than no link: it claims a lineage that cannot
    be followed."""
    ghost = RunScope(
        run_id=str(uuid.uuid4()), tenant_id="SYN-m7-ghost", project_id="p-ghost"
    )
    with pytest.raises(ValueError, match="does not exist in tenant"):
        _start(ghost, parent_run_id=str(uuid.uuid4()))
    assert query("SELECT 1 FROM runs WHERE run_id=%s", (ghost.run_id,)) == []


async def test_the_public_api_carries_a_parent_run_id_through_to_the_row():
    """FR-21 through the front door. RunConfig gains a field rather than the
    store gaining a private one, so Phase 2 adds a caller and not a migration
    to a table that by then holds production rows (NFR-7).
    """
    tenant = "SYN-m7-api"
    persistence = Persistence.postgres(DSN)
    try:
        parent = await Runner(
            {"m": NoNetworkModel()}, tools=NOOP_TOOLS, persistence=persistence
        ).run(
            AgentSpec(id="parent", instructions="go", tool_profile=("noop",)),
            "do it",
            RunConfig(tenant_id=tenant, project_id="p-api", max_turns=6),
        )
        assert parent.status is RunStatus.COMPLETED

        child = await Runner(
            {"m": NoNetworkModel()}, tools=NOOP_TOOLS, persistence=persistence
        ).run(
            AgentSpec(id="child", instructions="go", tool_profile=("noop",)),
            "do it",
            RunConfig(
                tenant_id=tenant,
                project_id="p-api",
                max_turns=6,
                parent_run_id=parent.run_id,
            ),
        )
        assert child.status is RunStatus.COMPLETED, child.error

        stored = query(
            "SELECT parent_run_id FROM runs WHERE run_id=%s", (child.run_id,)
        )[0][0]
        assert str(stored) == parent.run_id
        assert query(
            "SELECT parent_run_id FROM runs WHERE run_id=%s", (parent.run_id,)
        )[0][0] is None
    finally:
        _drop_runs(tenant)


def test_the_run_store_refuses_a_malformed_parent_run_id():
    """The STORE's guard. This test was first named for RunConfig while testing
    the store -- and RunConfig in fact accepted every malformed value, so the
    name claimed a refusal nothing enforced (M7 round 1). The configuration
    boundary now has its own test below.
    """
    ghost = RunScope(
        run_id=str(uuid.uuid4()), tenant_id="SYN-m7-bad", project_id="p-bad"
    )
    with pytest.raises(ValueError, match="parent_run_id cannot be stored"):
        _start(ghost, parent_run_id="not-a-uuid")
    assert query("SELECT 1 FROM runs WHERE run_id=%s", (ghost.run_id,)) == []


# --- M7 round 1: detectors that do not depend on a stopwatch -------------------


def test_run_config_refuses_a_malformed_parent_run_id():
    """D3. Configuration refuses by name, at construction, like max_turns.

    Before the repair RunConfig constructed around every one of these, while
    its sibling fields refused theirs -- M5 round 8's shape exactly.
    """
    bad = {
        "not a uuid": "not-a-uuid",
        "NUL": str(uuid.uuid4())[:-1] + chr(0),
        "int": 42,
        "urn form the column refuses": "urn:uuid:" + str(uuid.uuid4()),
    }
    for label, value in bad.items():
        with pytest.raises(ValueError, match="parent_run_id cannot be stored"):
            RunConfig(tenant_id="t", project_id="p", parent_run_id=value)
            pytest.fail(f"RunConfig accepted a parent_run_id that is {label}")
    # And it still lets the two legitimate cases through.
    RunConfig(tenant_id="t", project_id="p", parent_run_id=str(uuid.uuid4()))
    RunConfig(tenant_id="t", project_id="p")


def test_the_uuid_guard_never_accepts_what_the_column_refuses():
    """A differential, not an example list: every form the guard ACCEPTS must
    also be accepted by a real uuid column.

    The first guard used uuid.UUID(), which strips a "urn:uuid:" prefix that
    Postgres refuses, so that form passed the guard and failed at the write --
    found by running exactly this comparison. False positives (refusing a form
    the column would take) are allowed and expected; false negatives are the
    defect.
    """
    from agentsdk.postgres import column_rejection_reason

    u = uuid.uuid4()
    forms = {
        "canonical": str(u),
        "uppercase": str(u).upper(),
        "no hyphens": u.hex,
        "braces": "{" + str(u) + "}",
        "urn prefix": "urn:uuid:" + str(u),
        "surrounding whitespace": " " + str(u) + " ",
        "truncated": str(u)[:-2],
        "empty": "",
    }
    assert column_rejection_reason(str(u), "UUID") is None, (
        "the canonical form is refused, so this differential tests nothing"
    )
    false_negatives = []
    with psycopg.connect(DSN, autocommit=True) as conn:
        for label, value in forms.items():
            if column_rejection_reason(value, "UUID") is not None:
                continue
            try:
                conn.execute("SELECT %s::uuid", (value,))
            except psycopg.Error:
                false_negatives.append(label)
    assert not false_negatives, (
        f"the guard accepts forms a uuid column refuses: {false_negatives}"
    )


class _CheckoutSpy:
    """Wraps a real pool, recording the thread every connection checkout runs on.

    Every store method in postgres.py reaches the database through
    _pool(...).connection(), and test_stores_open_no_connections_of_their_own
    pins that premise. So a checkout on the loop thread IS store I/O on the
    loop, whichever method made it -- including one added tomorrow.

    The version before this spied on five method NAMES and asserted all five
    were seen, but never that five was all of them. M7 round 2 showed what that
    enumeration cost: an on-loop read through a sixth method passed it, and so
    did ToolCalled for a failed tool call, because no spied path ever failed a
    tool.
    """

    def __init__(self, pool, log):
        self._pool, self._log = pool, log

    def connection(self, *args, **kwargs):
        self._log.append(threading.get_ident())
        return self._pool.connection(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._pool, name)


class _OneToolCall:
    async def send(self, request):
        await asyncio.sleep(0)
        if any(m.role is Role.TOOL for m in request.messages):
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, content="done"),
                stop_reason=StopReason.END_TURN,
                usage=Usage(1, 1, 2),
            )
        return ModelResponse(
            message=Message(
                role=Role.ASSISTANT,
                tool_calls=(ToolCall(id="c1", name="noop", arguments={}),),
            ),
            stop_reason=StopReason.TOOL_CALLS,
            usage=Usage(1, 1, 2),
        )


class _NeverEnds:
    async def send(self, request):
        await asyncio.sleep(0)
        n = len(request.messages)
        return ModelResponse(
            message=Message(
                role=Role.ASSISTANT,
                tool_calls=(ToolCall(id=f"c{n}", name="noop", arguments={}),),
            ),
            stop_reason=StopReason.TOOL_CALLS,
            usage=Usage(1, 1, 2),
        )


class _ProviderDown:
    async def send(self, request):
        from agentsdk import ModelProviderUnavailable

        raise ModelProviderUnavailable("gateway down")


class _EveryToolFailure:
    """A denied tool, then invalid arguments, then a tool that raises, then done:
    every route by which the executor's _failed emits ToolCalled."""

    SCRIPT = (
        ToolCall(id="f1", name="forbidden", arguments={}),
        ToolCall(id="f2", name="noop", arguments={"unexpected": 1}),
        ToolCall(id="f3", name="boom", arguments={}),
    )

    async def send(self, request):
        await asyncio.sleep(0)
        done = sum(1 for m in request.messages if m.role is Role.TOOL)
        if done >= len(self.SCRIPT):
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, content="done"),
                stop_reason=StopReason.END_TURN,
                usage=Usage(1, 1, 2),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, tool_calls=(self.SCRIPT[done],)),
            stop_reason=StopReason.TOOL_CALLS,
            usage=Usage(1, 1, 2),
        )


def _failing_tools():
    def boom():
        raise RuntimeError("the tool itself fails")

    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    return NOOP_TOOLS + [
        Tool(spec=ToolSpec(name="forbidden", description="Never allowed.", input_schema=empty),
             fn=lambda: "no"),
        Tool(spec=ToolSpec(name="boom", description="Raises.", input_schema=empty), fn=boom),
    ]


def test_stores_open_no_connections_of_their_own():
    """The checkout spy's premise, pinned rather than assumed.

    If a store method ever opened its own connection it would bypass the pool
    -- a performance regression -- AND be invisible to the spy, so this is the
    one fact that test's totality rests on. Schema application lives in
    migrate.py, which is the only module allowed a direct connect.
    """
    import inspect

    from agentsdk import postgres

    assert "psycopg.connect(" not in inspect.getsource(postgres), (
        "postgres.py opens a connection outside the pool, which the event-loop "
        "spy cannot see"
    )


async def test_no_store_call_runs_on_the_event_loop_thread_on_any_run_path(monkeypatch):
    """FR-20's property, asserted exactly and without a clock.

    Records the thread of every database connection checkout, across five run
    paths -- now including one where every kind of tool failure happens -- and
    requires none of them to be the event loop's thread.
    """
    from agentsdk import postgres
    from agentsdk.hooks import RuntimeHook

    class Explodes(RuntimeHook):
        def before_model(self, request):
            raise RuntimeError("an unforeseen failure reaches Runner's total boundary")

    tenant = "SYN-m7-threads"
    persistence = Persistence.postgres(DSN)  # schema DDL happens here, before recording
    log = []
    real_pool = postgres._pool
    monkeypatch.setattr(postgres, "_pool", lambda dsn: _CheckoutSpy(real_pool(dsn), log))
    loop_thread = threading.get_ident()
    paths = {
        "completed with a tool call": (_OneToolCall(), NOOP_TOOLS, ("noop",), None, 4, RunStatus.COMPLETED),
        "every kind of tool failure": (_EveryToolFailure(), _failing_tools(), ("noop", "boom"), None, 6, RunStatus.COMPLETED),
        "model failure": (_ProviderDown(), NOOP_TOOLS, ("noop",), None, 4, RunStatus.FAILED),
        "total-boundary failure": (_OneToolCall(), NOOP_TOOLS, ("noop",), Explodes(), 4, RunStatus.FAILED),
        "max turns exhausted": (_NeverEnds(), NOOP_TOOLS, ("noop",), None, 2, RunStatus.MAX_TURNS_EXCEEDED),
    }
    checkouts, offenders = {}, {}
    try:
        for label, (model, tools, profile, hook, max_turns, expected) in paths.items():
            first = len(log)
            result = await Runner({"m": model}, tools=tools, hook=hook, persistence=persistence).run(
                AgentSpec(id="threads", instructions="go", tool_profile=profile),
                "go",
                RunConfig(tenant_id=tenant, project_id="p-threads", max_turns=max_turns),
            )
            assert result.status is expected, f"{label}: {result.status} ({result.error})"
            if label == "every kind of tool failure":
                failed_calls = [
                    e for e in result.events
                    if e.event_type.value == "ToolCalled" and e.payload.get("is_error")
                ]
                assert len(failed_calls) == 3, (
                    f"the failure path produced {len(failed_calls)} failed tool calls, "
                    "not 3, so it does not exercise what it is named for"
                )
            mine = log[first:]
            checkouts[label] = len(mine)
            on_loop = sum(1 for ident in mine if ident == loop_thread)
            if on_loop:
                offenders[label] = f"{on_loop} of {len(mine)} checkouts"
    finally:
        monkeypatch.undo()
        _drop_runs(tenant)

    assert all(checkouts.values()), (
        f"a path checked out no connection at all, so it tested nothing: {checkouts}"
    )
    assert not offenders, f"store I/O ran ON the event loop thread: {offenders}"


async def test_a_store_lock_held_elsewhere_does_not_freeze_the_event_loop():
    """The reviewer's reproduction of D1's consequence, kept as a test.

    Hold a lock on run_events from another connection, as a writer in another
    process would, while a persisted run and an unrelated in-memory run execute
    together. If any event write is made on the loop, the loop waits on the
    lock and the unrelated run freezes with it: M7 round 1 measured a 479 ms
    stall against NFR-8's 50 ms. If every write is offloaded, only the
    persisted run waits.

    The thresholds are detection thresholds, deliberately far from both
    outcomes -- a freeze lasts the whole hold, an unblocked loop jitters by a
    timer tick -- so this measures a freeze rather than re-importing the
    flakiness of a bound set near the noise floor.
    """
    hold_seconds = 0.8
    tenant = "SYN-m7-hold"
    # Built BEFORE the hold: apply_schema runs DDL against these tables.
    persistence = Persistence.postgres(DSN)
    acquired = threading.Event()
    released = {}

    def hold_the_table():
        # Releases on its own timer. Waiting on the loop to release it would
        # deadlock exactly in the case this test exists to catch.
        with psycopg.connect(DSN) as conn:
            with conn.transaction():
                conn.execute("LOCK TABLE run_events IN ACCESS EXCLUSIVE MODE")
                acquired.set()
                time.sleep(hold_seconds)
        released["at"] = time.perf_counter()

    holder = threading.Thread(target=hold_the_table, daemon=True)
    holder.start()
    assert await asyncio.to_thread(acquired.wait, 10), "could not take the table lock"

    lags = []
    stop = False

    async def heartbeat():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            lags.append(now - last - 0.01)
            last = now

    async def timed(coro):
        result = await coro
        return result, time.perf_counter()

    spec = AgentSpec(id="hold", instructions="go", tool_profile=("noop",))
    beat = asyncio.create_task(heartbeat())
    try:
        persisted = asyncio.create_task(timed(
            Runner({"m": NoNetworkModel()}, tools=NOOP_TOOLS, persistence=persistence).run(
                spec, "go", RunConfig(tenant_id=tenant, project_id="p-hold", max_turns=6)
            )
        ))
        bystander, bystander_done = await timed(
            Runner({"m": NoNetworkModel()}, tools=NOOP_TOOLS).run(
                spec, "go", RunConfig(tenant_id=tenant, project_id="p-bystander", max_turns=6)
            )
        )
        persisted_result, persisted_done = await persisted
    finally:
        stop = True
        await beat
        await asyncio.to_thread(holder.join, 10)
        _drop_runs(tenant)

    assert persisted_result.status is RunStatus.COMPLETED, persisted_result.error
    assert bystander.status is RunStatus.COMPLETED
    # Non-vacuity: the persisted run really did have to wait for the lock.
    assert persisted_done >= released["at"], (
        "the persisted run finished before the lock was released, so nothing "
        "in this test was ever blocked"
    )
    assert bystander_done < released["at"], (
        "an unrelated in-memory run could not finish while another run's event "
        "write waited on a lock -- that write is being made on the event loop"
    )
    worst = max(lags) if lags else 0.0
    assert worst < hold_seconds / 4, (
        f"the event loop froze for {worst * 1000:.0f} ms while a store lock was "
        f"held elsewhere for {hold_seconds * 1000:.0f} ms"
    )


# --- M7 round 2: the pool survives a database restart ---------------------------


async def test_the_pool_replaces_connections_the_server_has_closed():
    """Defect A, M7 round 2, and a regression against the M6-approved store.

    The pool was built with no check, so it handed out connections the server
    had already closed -- which is what a Postgres restart, a failover or an
    idle-connection kill leaves behind. Measured: five pooled backends killed,
    and 3 of the next 8 runs completed; the rest failed with OperationalError,
    each a write a fresh connection would have completed. The per-call connect
    this pool replaced had simply reconnected: 8 of 8.

    The pool gets its own application_name, so the kill touches only it.
    """
    from agentsdk import postgres

    app = "m7deadpool" + uuid.uuid4().hex[:6]
    pool_dsn = DSN + ("&" if "?" in DSN else "?") + f"application_name={app}"
    tenant = "SYN-m7-deadpool"
    persistence = Persistence.postgres(pool_dsn)

    class Instant:
        async def send(self, request):
            await asyncio.sleep(0)
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, content="ok"),
                stop_reason=StopReason.END_TURN,
                usage=Usage(1, 1, 2),
            )

    async def failed_runs():
        results = await asyncio.gather(*(
            Runner({"m": Instant()}, persistence=persistence).run(
                AgentSpec(id="deadpool", instructions="go"),
                "go",
                RunConfig(tenant_id=tenant, project_id="p-deadpool", max_turns=2),
            )
            for _ in range(8)
        ))
        return [r for r in results if r.status is not RunStatus.COMPLETED]

    try:
        assert await failed_runs() == [], "the warm-up failed, so the kill measures nothing"
        with psycopg.connect(DSN, autocommit=True) as conn:
            killed = conn.execute(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                " WHERE application_name = %s",
                (app,),
            ).fetchone()[0]
        assert killed >= 1, "no pooled connection was idle to kill, so this tests nothing"

        failed = await failed_runs()
        assert not failed, (
            f"{len(failed)} of 8 runs drew a connection the server had closed: "
            f"{sorted({(r.error or '').split(':')[0] for r in failed})}"
        )
    finally:
        pool = postgres._POOLS.pop(pool_dsn, None)
        if pool is not None:
            pool.close()
        _drop_runs(tenant)


# --- M7 rounds 1 and 2: migrations under concurrency, and their integrity -------


WORKERS = 8


def _in_threads(work):
    barrier = threading.Barrier(WORKERS)
    outcomes = [None] * WORKERS

    def worker(i):
        barrier.wait()
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - the failure IS the measurement
            outcomes[i] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [o for o in outcomes if o]


def _tables(ns):
    return {
        row[0] for row in query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
            (ns.name,),
        )
    }


def test_eight_workers_initialising_an_empty_database_all_succeed():
    """Round 1's D4 -- which round 2's prompt dropped by reusing its label.

    apply_schema ran schema.sql in autocommit OUTSIDE the lock migrate.py said
    serialised concurrent callers, and Persistence.postgres() calls it on every
    construction. Eight workers starting against a fresh database -- a first
    deploy, a CI run, a new environment -- lost seven: 1 of 8 succeeded in
    three trials in each of two review rounds, the rest UniqueViolation on
    pg_type_typname_nsp_index. An existing database was unaffected, which is
    why no test against the live one could see it.
    """
    with Namespace(baseline=False) as ns:
        failures = _in_threads(lambda: apply_schema(ns.dsn))
        assert not failures, (
            f"{len(failures)} of {WORKERS} workers failed to initialise an empty "
            f"database: {sorted({f.split(':')[0] for f in failures})}"
        )
        assert {"runs", "messages", "run_events", "execution_manifests"} <= _tables(ns)
        assert "parent_run_id" in ns.columns("runs")
        assert schema_version(ns.dsn) == discover()[-1][0]


def test_eight_workers_upgrading_the_same_database_all_succeed():
    """N4, M7 round 2: removing the migration lock passed the whole phase-2 gate,
    although it makes concurrent upgrades fail 2 to 4 of 8. Nothing tested the
    lock -- only the idempotence of a single caller."""
    with Namespace() as ns:  # the pre-migration baseline
        failures = _in_threads(lambda: apply_migrations(ns.dsn))
        assert not failures, (
            f"{len(failures)} of {WORKERS} concurrent upgrades failed: "
            f"{sorted({f.split(':')[0] for f in failures})}"
        )
        with psycopg.connect(ns.dsn) as conn:
            rows = conn.execute(
                "SELECT version, count(*) FROM schema_migrations GROUP BY version ORDER BY version"
            ).fetchall()
        assert rows == [(version, 1) for version, _ in discover()], (
            f"a migration was recorded more or less than once: {rows}"
        )


def test_an_applied_migration_that_was_edited_is_refused():
    """Raised as a caveat in both review rounds. An applied migration edited
    afterwards is otherwise skipped in silence -- the schema change appears to
    succeed and does not, which is the failure FR-17 exists to remove."""
    with Namespace() as ns:
        apply_migrations(ns.dsn)
        version = discover()[-1][0]
        with psycopg.connect(ns.dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE schema_migrations SET checksum = 'edited' WHERE version = %s",
                (version,),
            )
        with pytest.raises(ValueError, match="changed after it was applied"):
            apply_migrations(ns.dsn)


def test_a_migration_applied_before_checksums_existed_is_trusted_and_recorded():
    """Every real database migrated before checksums existed has none. Refusing
    them would refuse every database; they are trusted on first sight and the
    checksum recorded from then on."""
    from agentsdk.migrate import checksum

    with Namespace() as ns:
        apply_migrations(ns.dsn)
        with psycopg.connect(ns.dsn, autocommit=True) as conn:
            conn.execute("UPDATE schema_migrations SET checksum = NULL")
        assert apply_migrations(ns.dsn) == []
        with psycopg.connect(ns.dsn) as conn:
            stored = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
    assert stored == {version: checksum(path) for version, path in discover()}


def test_a_failed_migration_leaves_the_ones_before_it_applied(tmp_path, monkeypatch):
    """A round-1 caveat: the docstring described one transaction per migration
    while the code ran every pending migration in ONE, so a failure in the last
    rolled back all the others with it and the next attempt repeated them."""
    from agentsdk import migrate

    good = tmp_path / "0900_good.sql"
    good.write_text("CREATE TABLE m7_good (id int);", encoding="utf-8")
    bad = tmp_path / "0901_bad.sql"
    bad.write_text("CREATE TABLE m7_bad (id int); SELECT 1/0;", encoding="utf-8")

    with Namespace() as ns:
        monkeypatch.setattr(migrate, "discover", lambda: [("0900", good), ("0901", bad)])
        with pytest.raises(psycopg.errors.DivisionByZero):
            apply_migrations(ns.dsn)
        assert schema_version(ns.dsn) == "0900", "the migration before the failure was rolled back too"
        tables = _tables(ns)
        assert "m7_good" in tables and "m7_bad" not in tables

        monkeypatch.setattr(migrate, "discover", lambda: [("0900", good)])
        assert apply_migrations(ns.dsn) == [], "the next attempt repeated work already done"


# --- M7 round 2: gate blind spots over correct code ----------------------------


def test_a_parent_in_another_project_of_the_same_tenant_is_refused():
    """N3, M7 round 2: dropping the project comparison from the parent check
    passed every gate. The only cross-scope test changed tenant AND project
    together, so the tenant comparison alone refused it and the project
    comparison was never what the test depended on."""
    with Run("ac15p") as parent:
        sibling = RunScope(
            run_id=str(uuid.uuid4()),
            tenant_id=parent.tenant_id,
            project_id="p-another-project",
        )
        with pytest.raises(ValueError, match="may only descend from one its own tenant"):
            _start(sibling, parent_run_id=parent.run_id)
        assert query("SELECT 1 FROM runs WHERE run_id=%s", (sibling.run_id,)) == []


def test_a_uuid_object_is_accepted_wherever_a_run_id_is():
    """A round-2 caveat. uuid.UUID is what get_run and PostgresTrace return for
    a run id, and the canonical-form guard refused it as a TypeError -- so a
    caller passing a run id straight back from a trace as parent_run_id was
    refused by the SDK's own output type."""
    from agentsdk.postgres import PostgresTrace, column_rejection_reason

    as_object = uuid.uuid4()
    assert column_rejection_reason(as_object, "UUID") is None
    config = RunConfig(tenant_id="t", project_id="p", parent_run_id=as_object)
    assert config.parent_run_id == str(as_object), "RunConfig did not normalise to one type"

    with Run("uuidobj") as parent:
        handed_back = PostgresTrace(DSN)._runs.get_run(parent)["run_id"]
        assert isinstance(handed_back, uuid.UUID), (
            "the premise changed: get_run no longer returns uuid.UUID"
        )
        child = RunScope(
            run_id=str(uuid.uuid4()), tenant_id=parent.tenant_id, project_id=parent.project_id
        )
        try:
            _start(child, parent_run_id=handed_back)
            stored = query("SELECT parent_run_id FROM runs WHERE run_id=%s", (child.run_id,))[0][0]
            assert str(stored) == parent.run_id
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute("DELETE FROM execution_manifests WHERE run_id=%s", (child.run_id,))
                conn.execute("DELETE FROM runs WHERE run_id=%s", (child.run_id,))
