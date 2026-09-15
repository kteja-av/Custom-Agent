You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** Across M5 to M11, nearly every defect was found in a region
the previous reviewer had not examined. If you have reviewed this project
before, say so and ask for a different session. Re-derive the requirements from
`SPEC.md` rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

Both gates need `DATABASE_URL` from `.env`; the regression gate also needs
`BASE_URL` and `MODEL_API_KEY`. **Never print credentials or a connection
string.** Install first: `.venv\Scripts\python.exe -m pip install -r
requirements.txt`. M12 adds no dependency. **Windows Developer Mode must be on**
(D7).

**Only one test session at a time.** Every pytest session compares the store's
run ids at its start and end (AC-44, `tests/conftest.py`), and the development
database is shared: two sessions at once fail each other at teardown. Before
running tests or gates, check that no other `pytest` or `genesis.mjs gate`
process is running. Run your own probes with `-p no:cacheprovider`: Genesis
hashes `.pytest_cache`, so a cache write can make a gate read stale with no code
change.

## Task under review

**M12-run-handles-and-cancellation**: "A caller can start a run, stream its events
in order and cancel it, and a cancelled run ends cancelled with a complete
record, RunCancelled as its last event, and no run left running"

First review round, risk **high**. Requirements FR-48..FR-52, NFR-15, NFR-17 and
AC-39..AC-41 are in `SPEC.md` under "Phase 2, increment 1" (M12), approved on
2026-09-14. Decisions that bear on it: P2-D3, a new terminal status `cancelled`
(DECISION-a44db7e4); P2-D7, a run cancelled with a model call in flight reports
cost None; P2-D9, ToolCalled events of a batch in finishing order; P2-D10,
migration `0005`; and, taken at this milestone's pre-flight, **DECISION-29e21dd0**:
a tool that catches `CancelledError` and carries on is a declared limitation
(cancellation stays cooperative, no grace period), now a risk in `SPEC.md`.
D13 applies (DECISION-2bad84bb): in-process caller code is trusted.

**The M12 code is not committed.** HEAD is `1662ede`. Review it with:

```bash
git diff 1662ede -- agentsdk tests README.md SPEC.md
git status --short   # new: agentsdk/handle.py, agentsdk/migrations/0005_cancelled_status.sql,
                     #      tests/test_run_handles.py
```

## What changed

- `agentsdk/handle.py` (new):
  - `RunControl`, one run's cancellation state and progress. `request()` marks
    the run and cancels its task only once the run's coroutine has started, only
    when asked from outside that task, and never once the terminal event is
    being written. `checkpoint()` raises `CancelledError` if the run was asked
    to stop. `store()` runs a store call on a worker thread, shielded: a
    cancellation that arrives meanwhile is absorbed until the call finishes
    (with `Task.uncancel`), and the run stops at its next checkpoint.
  - `RunHandle`: `run_id`; `events()`, an async generator over a buffer keyed by
    `sequence_no`, woken by futures; `result()`, a shield over the run's task;
    `state()`; `cancel()`.
  - `RunState`, the snapshot.
  - `PublishingSink`, which wraps the run's sink and hands each recorded event to
    the handle through `call_soon_threadsafe`.
- `agentsdk/api.py`:
  - `RunStatus.CANCELLED`.
  - `Runner.start()`: `_open()` holds every check `run()` made before starting,
    then the run is a task running `_drive()`.
  - `Runner.run()` is `start()` plus `result()`. If its caller is cancelled it
    cancels the run, waits for it with `_settled()` (absorbing further cancels),
    and re-raises. For any run that ended cancelled it raises `CancelledError`.
  - `_drive()` catches `CancelledError` and records the run through
    `_cancelled()`. That writes `RunCancelled` with the reason and the turn, then
    `finish_run` with status `cancelled`, the usage, and a cost of None if a
    model call was in flight.
  - An exception after a cancellation request also ends `cancelled`.
  - `_run()` raises `CancelledError` just before the terminal event if a request
    arrived during the last store writes, then marks the terminal write.
  - Every store call of the Runner goes through `control.store`.
- `agentsdk/loop.py`:
  - Every store call goes through `control.store`.
  - Checkpoints at the top of each turn, after `before_model` and before
    `send`, before a response's tool calls, before each call's steps 1 to 5, and
    before a batch reaches step 6.
  - `in_flight` is set around `send`, and `cancelled_in_flight` when a
    cancellation interrupts a call that entered `send`.
  - `_execute_tool_calls` tracks outcomes by index. On `CancelledError` it pairs
    every call without an outcome with `executor.cancelled(...)` and appends the
    tool message, then re-raises.
- `agentsdk/executor.py`: `cancelled(item)` returns `Failed(ToolCancelled)` through
  `_failed`, so its `ToolCalled` is emitted. The call keeps its tool's declared
  provenance if it had reached step 6, and executor provenance otherwise.
- `agentsdk/events.py`: `EventType.RUN_CANCELLED`; `InMemoryEventSink` numbers and
  reads under a lock.
- `agentsdk/postgres.py`: `PostgresEventStore` keeps its buffer in `sequence_no`
  order under a class-level lock.
- `agentsdk/errors.py`: `ToolCancelled(ToolError)`.
- `agentsdk/migrations/0005_cancelled_status.sql`: drops every CHECK constraint on
  `runs.status`, found through `pg_constraint` and `pg_attribute` rather than by
  name, and adds `runs_status_check` admitting `cancelled`.
- `agentsdk/__init__.py`: exports `RunHandle`, `RunState`, `ToolCancelled`.
- `SPEC.md`: one risk added (DECISION-29e21dd0). `README.md`: a run-handle
  bullet, the streaming and cancel rows, the test count.
- `tests/test_run_handles.py` (new, 60 tests: 58 written first, 2 added for A10). **No existing test was edited.**

## What the author ran

1. **Pre-flight**, recorded as KNOWLEDGE-93fa7f44:
   - **Baseline:** 925 passed at `1662ede`.
   - **The in-memory sink race:** 0 duplicates at the default thread switch
     interval, 584 to 654 per trial at one microsecond.
   - **A persisted run cancelled during a model call:** its row stayed `running`,
     with NULL usage and cost, no `completed_at`, and `RunStarted` its only event.
   - **The live constraint:** named `runs_status_check`.
2. **Tests first.** Against the unchanged code: 55 failed, 3 passed.
   - **Pass 1:** the Postgres sink ordering test (A1).
   - **Passes 2 and 3:** the memory halves of the two `Runner.run` cancellation
     tests (A4).
   - **The failures:** a missing `Runner.start`, a missing migration, missing
     names, duplicate sequence numbers, and Postgres rows read back as
     `('running', None, None)`.
3. **After the implementation**, first run: `test_run_handles`,
   `test_agent_loop`, `test_tool_executor` and `test_concurrency` together, 265
   passed. Full suite: 983 passed in 143.6 s.
4. **Mutation matrix**, 22 mutants against `tests/test_run_handles.py` and
   `tests/test_concurrency.py` (`-x`), every file restored and SHA-256 verified,
   the `agentsdk` tree hashed before and after (`tree restored: True`), and a
   scan afterwards finding no mutant text left. First run 21 of 22 killed; after
   the two test changes in A10, H13 and H15 were rerun and killed, so 22 of 22:
   ```
   H1  FR-52 in-memory sink numbers without its lock           -> eight threads, unique contiguous numbers
   H2  FR-52 postgres sink appends in arrival order            -> postgres sink events in sequence order
   H3  FR-50 checkpoints never stop the run                    -> cancelled before its first model call
   H4  FR-50 a store call is not shielded                      -> store write held [event emission]
   H5  FR-50 a cancel before the run begins cancels its task   -> cancelled before its first model call
   H6  P2-D7 in flight never recorded                          -> cancelled during a model call
   H7  P2-D7 waiting for a provider slot counted as in flight  -> cancelled while waiting for a provider slot
   H8  FR-50 no checkpoint before a model call                 -> cancelled between turns
   H9  FR-50 no checkpoint before a response's tool calls      -> store write held [event emission]
   H10 FR-50 unfinished calls not paired                       -> parallel batch pairs every call
   H11 FR-50 the paired tool message not appended              -> parallel batch pairs every call
   H12 FR-50 a cancelled call that ran loses its provenance    -> parallel batch pairs every call
   H13 FR-50 a cancel absorbed by the last writes is ignored   -> SURVIVED, then store write held [the final session append] (A10)
   H14 FR-50 Runner.run returns a cancelled run                -> collaborator CancelledError records and raises
   H15 FR-50 cancelling Runner.run does not cancel the run     -> 300 s timeout, then cancelling Runner.run's task (A10)
   H16 FR-50 an unstorable reason recorded                     -> reason recorded when it can be stored
   H17 P2-D7 cost known although a model call was in flight    -> cancelled during a model call
   H18 FR-49 events() ends before the run does                 -> every ending streams exactly its events
   H19 FR-49 result() not shielded                             -> cancelling a waiter leaves the run to complete
   H20 FR-49 recorded events never published                   -> every ending streams exactly its events
   H21 FR-49 state() reports a cost while a call is in flight  -> state reports an unknown cost in flight
   H22 FR-50 Runner.run obeys a second cancel of its caller    -> M11 cancelling a batch twice (test_concurrency)
   ```
   Not mutated, and so not claimed: see A9, plus `Task.uncancel` in
   `RunControl.store`, migration `0005` (the live database records its
   checksum), and `PublishingSink`'s guard for a closed loop.
5. **Gates**, on the final tree, with `--timeout 600000` and no other test
   process running:
   ```
   $ genesis gate . M12-run-handles-and-cancellation --timeout 600000
   M12-run-handles-and-cancellation: passed executable gates
   unit        exit 0  2026-09-14T17:08:37Z -> 17:08:48Z  60 passed in 10.59s
   regression  exit 0  2026-09-14T17:08:49Z -> 17:11:22Z  985 passed in 151.94s
   ```
   Both gates carry source hash `c4ea020a`, and `KICKOFF.md` reads `unit:pass,
   regression:pass, independent-review:pending`. Full suite **985 tests**:
   `test_agent_loop 77` + `test_builtin_tools 87` + `test_concurrency 104` +
   `test_distribution 8` + `test_golden_eval 15` + `test_honest_results 271` +
   `test_model_client 152` + `test_persistence 80` + `test_phase2_readiness 31` +
   `test_primitives 74` + `test_run_handles 60` + `test_tool_executor 26` = 985.
   The 925 tests before M12 are unchanged, and AC-44's session check passed with
   them.

## Issues the author found during M12

Labels `A` are the author's own and never share a reviewer's label.

| id | finding | disposition |
|---|---|---|
| A1 | The Postgres sink ordering test passed against the unfixed sink: the window between an insert's commit and its buffer append is too short to hit with 8 threads | the test delays odd-numbered events 6 ms in that window; it then read back `[1, 2, 4, 6, 3, 5, ...]` against the unfixed sink |
| A2 | The migration `0005` test compared constraint definitions read over the default connection, where `pg_get_constraintdef` qualifies a foreign key's target with the schema name, so two namespaces could never compare equal | each namespace's definitions are read over a connection whose search_path is that namespace |
| A3 | The in-memory numbering race does not reproduce at the default thread switch interval | the test sets `sys.setswitchinterval(1e-6)` and restores it |
| A4 | The memory halves of the two `Runner.run` cancellation tests pass on the old code: they check only that `CancelledError` reaches the caller and no task is left, which was already true | the Postgres halves carry the change: the row is `cancelled`, usage recorded, cost NULL |
| A5 | AC-39 compares `Runner.run` with `result()` for every ending, but `Runner.run` raises `CancelledError` for a cancelled run (FR-50) | equality is checked for the other seven endings; the cancelled ending is checked through the handle and the store |
| A6 | FR-49 does not say what `turns` means mid-turn | the turn the run is on: 2 while the second model call is in flight; the same value goes into `RunCancelled` |
| A7 | The Runner-level `asyncio.Lock` around ToolCalled emission (M11) could have been removed once FR-52 put locks in the sinks | kept: it keeps a batch's ToolCalled events in finishing order on Postgres, which M11's ordering test depends on |
| A8 | FR-50 pairs calls of a response "whose calls the run had begun to execute" | a response whose calls had not begun (cancelled before its first prepare) records its assistant message and no tool results |
| A9 | Not covered by any mutant or test: cancellation's precedence over an exception raised after a request (no test makes a store write fail after `cancel()`), and `request()` ignoring a request during the terminal write (a cancel that lands there is absorbed and changes nothing either way) | declared here, not claimed |
| A10 | Two gaps the mutation run found. H13 (the check before the terminal event removed) survived: a cancel that lands during the last store write of a run answering in text is stopped only by that check, and no test held that write. H15 (Runner.run not cancelling its run) was caught only by the 300 s harness timeout, because the test awaited `asyncio.wait_for(task, 10)`, whose own cancellation `run()` then absorbed while waiting for a run that never ended | a third case, "the final session append", holds the assistant append of a text-only run and requires the run to end `cancelled` (2 tests, both stores); the `Runner.run` test waits with `asyncio.wait` (a bound that does not cancel), asserts the task ended, and releases the held call in a `finally`. Both mutants rerun: killed, H15 in seconds. No implementation change |

## Attack these first

- **Absorbed cancellation.** `RunControl.store` swallows `CancelledError` and calls
  `Task.uncancel`. Look for a path where an absorbed cancellation is lost:
  - a checkpoint that never comes before a model or tool call;
  - a run that completes although `cancel()` returned before its terminal write.

  Also look for one absorbed twice, or for `uncancel` misleading
  `asyncio.timeout` or `wait_for` further up.
- **Who calls `task.cancel()`.** `request()` from a hook (same task), from another
  task, from another thread, and before the task has started. Race `cancel()`
  against the run's first `await`.
- **The pairing path.** Cancel at every point of a batch: during a prepare's
  failure emission, while a sibling holds the ToolCalled lock, between one
  batch and the next, during an inline single call. Does every call get exactly
  one result and exactly one ToolCalled, all before `RunCancelled`?
- **`events()`.** Many iterators, iterators abandoned mid-stream (waiter futures
  left behind), an iterator opened after the loop's last callback, a sink whose
  emit raises for a non-terminal event, a Postgres sink whose events arrive in
  a different order from their publication.
- **`Runner.run` semantics.** A collaborator `CancelledError` now ends the run
  `cancelled` and re-raises. `KeyboardInterrupt`, `SystemExit` or another
  `BaseException` from a tool or a hook. A run task that ends with an exception
  nobody awaits.
- **NFR-8 and NFR-17.** Every store call now creates a task and a shield. Six
  concurrent runs against the stall bound (AC-14, AC-43), and no run left
  `running` after any ending.
- **Migration `0005`** on a database whose status check has another name, or two
  check constraints on `status`.

## Declared limitations: known, recorded, NOT findings

- A tool that ignores cancellation delays the end of its run (DECISION-29e21dd0,
  `SPEC.md` Risks). A file tool's worker thread cannot be interrupted; its
  result is discarded.
- A run cancelled with a model call in flight reports cost None (P2-D7); exact
  counting waits for the `ModelClient` contract change (DECISION-0e3ed41b).
- A9 above.
- Example scripts 11 and 12 belong to M13 (FR-56).
- A run belongs to the event loop that started it.
- NFR-15's amendment for batch ordering under `max_concurrent_tools=1`
  (DECISION-7a0b4459) is M11's, and unchanged.
- Older `independent-review` gates compute `stale` because the repository hash
  moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Restore every file you mutate and verify SHA-256, restoring in a `finally`, and
  kill the whole process tree on a timeout (`taskkill /T`). Do not mutate a
  migration file: the live database records its checksum.
- Remove every run, temporary folder and git worktree you create.
- Include every probe you ran in your report, including ones that showed nothing.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$`
out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M12-run-handles-and-cancellation --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M12-run-handles-and-cancellation --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found, and your verdict.
