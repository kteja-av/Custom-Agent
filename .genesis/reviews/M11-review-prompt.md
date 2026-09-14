You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** Across M5 to M10, nearly every defect was found in a region
the previous reviewer had not examined. If you have reviewed this project
before, say so and ask for a different session. Re-derive the requirements from
`SPEC.md` rather than inheriting the framing below.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

Both gates need `DATABASE_URL` from `.env`; the regression gate also needs
`BASE_URL` and `MODEL_API_KEY`. **Never print credentials or a connection
string** (the database password was printed once, by an M10 probe). Install
first: `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M11 adds
no dependency (NFR-18). **Windows Developer Mode must be on** (D7): the M10
confinement corpus in the regression gate creates real symlinks.

## Task under review

**M11-concurrency-foundations**: "Calls to tools that declare concurrency_safe run
in parallel under per-run, per-tool and per-provider limits, file tools stop
starving the store on their own thread pool, and the M10 carry-overs are closed"

First review round, risk **high**. Requirements FR-43..FR-47, NFR-15..NFR-18,
AC-34..AC-38, AC-43 and AC-44 are in `SPEC.md` under "Phase 2, increment 1",
approved by the owner on 2026-09-14 with decisions P2-D5..P2-D11
(DECISION-103f30f6) and P2-D14 (parallel eligibility is opt-in,
DECISION-976c5942). D13 applies throughout (DECISION-2bad84bb): in-process caller
code is trusted, and forgery past constructors is outside the threat model.

**The M11 code is not committed.** HEAD is `7abfe47`. Review it with:

```bash
git diff 7abfe47 -- agentsdk tests README.md
git status --short   # new: agentsdk/scheduler.py, agentsdk/migrations/0004_scheduler_limits.sql,
                     #      tests/test_concurrency.py, tests/conftest.py
```

## What changed

- `agentsdk/scheduler.py` (new): `SchedulerLimits`, a frozen dataclass with
  `max_concurrent_tools` (default 4) and two name-to-limit mappings copied into
  `MappingProxyType`s, every value refused by field name as FR-43 lists;
  `RunSlots`, one run's slots (a tool's semaphore, then the run's); and
  `ProviderSlots`, a Runner's provider semaphores kept per event loop.
- `agentsdk/api.py`: `Runner(scheduler_limits=...)` and
  `RunConfig.scheduler_limits`, with the refusals FR-43 names; `_limits_for`
  gives a run its own per-run and per-tool limits as a whole, and the Runner's
  provider limits; the manifest records them; `ToolCalled` is emitted one at a
  time per run, under an `asyncio.Lock`, with the envelope's `tool_call_id` set.
- `agentsdk/executor.py`: the lifecycle split into `prepare` (steps 1 to 5) and
  `run_prepared(prepared, slot)` (6 to 9), each a total boundary; `execute` is
  the two in a row. The slot is entered immediately before step 6 and left after
  step 9, so `state.ran` (FR-40) and the timeout begin at step 6. Step 7 and
  `_checked_provenance` copy a `str`-subclass source into an exact `str` and bound
  the copy (FR-47).
- `agentsdk/loop.py`: `_batches` splits a response's calls into consecutive
  batches; `_run_batch` prepares every call of a batch in issue order, then runs
  the prepared ones as tasks (one prepared call runs inline), through
  `_wait_all_or_cancel`; results are assembled by index, in issue order. The
  model call is made inside the Runner's provider slot.
- `agentsdk/tools.py`: `ToolSpec.concurrency_safe`, exactly a bool, in
  `schema_hash` only when True.
- `agentsdk/builtin_tools.py`: `set_file_tool_threads`, a lazily built
  process-wide `ThreadPoolExecutor` whose threads are named
  `agentsdk-file-tool-<n>`, and `_on_file_pool`, which the four file tools now use
  instead of `asyncio.to_thread`; the six built-ins declare `concurrency_safe`;
  `list_directory`, `glob` and `grep` withhold and count an entry whose name the
  store cannot hold, and never descend into such a folder.
- `agentsdk/manifest.py`, `agentsdk/postgres.py`, migration `0004`: the
  `scheduler_limits` JSONB column, NULL for older manifests.
- `agentsdk/__init__.py`: exports `SchedulerLimits`. `README.md`: one bullet.
- `tests/test_concurrency.py` (new, 94 tests).
- `tests/conftest.py` (new): AC-44's session check. It applies the schema, takes
  the set of `run_id`s in `runs`, `messages`, `run_events` and
  `execution_manifests` at session start, and fails the session at its end if
  any set changed; it fails rather than skips without `DATABASE_URL`.
- **Existing test edited, as NFR-15 anticipated:** `tests/test_golden_eval.py`
  gains a module fixture that removes, when the module ends, the runs under
  tenants `t-%` that it started. Nothing else in an existing test changed.

## What the author ran

1. **Pre-flight**, recorded as KNOWLEDGE-0b28bdb9: the full suite at `7abfe47`
   passed 821 tests; a probe reproduced all three FR-47 carry-overs on the
   unchanged code; after a full run the store held 13 new runs, every one from
   the golden eval.
2. **Tests first.** Against the unchanged code: 88 failed, 6 passed. The 6 are
   the existing source bound (8,192 accepted, 8,193 refused, and a subclass
   holding 8,193 characters refused) on both stores, which M11 must keep.
3. **Gates**, on the final tree, run with `--timeout 600000` (Genesis's default
   limit is 120 s, and the full suite takes about two minutes):
   ```
   $ genesis gate . M11-concurrency-foundations --timeout 600000
   M11-concurrency-foundations: passed executable gates
   unit        exit 0  2026-09-14T08:45:17Z -> 08:45:33Z  94 passed in 15.82s
   regression  exit 0  2026-09-14T08:45:33Z -> 08:47:34Z  915 passed in 119.55s
   ```
   Evidence: `.genesis/evidence/M11-concurrency-foundations-{unit,regression}.json`,
   source hash `76d9c077...`. Full suite **915 tests**: `test_agent_loop 77` +
   `test_builtin_tools 87` + `test_concurrency 94` + `test_distribution 8` +
   `test_golden_eval 15` + `test_honest_results 271` + `test_model_client 152` +
   `test_persistence 80` + `test_phase2_readiness 31` + `test_primitives 74` +
   `test_tool_executor 26` = 915. The 821 tests before M11 are unchanged apart
   from the golden-eval cleanup fixture. The regression gate passing also means
   AC-44's session check passed: it fails the session at teardown otherwise.
   Genesis still shows "next: Run the task pre-flight"; that is the default
   text set when the task was added, and the pre-flight is KNOWLEDGE-0b28bdb9.
4. **Mutation matrix, 43 mutants, 43 killed**, each against the unit gate
   (`-x`), every file restored and SHA-256 verified, the whole `agentsdk` tree
   hashed before and after (`tree restored: True` on both runs), and a separate
   scan afterwards finding no mutant text left in any file:
   ```
   S1  FR-43 default max_concurrent_tools not 4            -> limits frozen and immutable copies
   S2  FR-43 a bool accepted as a count                    -> invalid limits [max_concurrent_tools is a bool]
   S3  FR-43 ceiling off by one                            -> invalid limits [past the INTEGER column]
   S4  FR-43 a str subclass key accepted                   -> invalid limits [key a str subclass]
   S5  FR-43 an unstorable key accepted                    -> invalid limits [key holding a NUL]
   S6  FR-43 the caller's mapping kept                     -> limits frozen and immutable copies
   S7  FR-43 RunConfig accepts provider limits             -> provider limits belong to a runner
   S8  FR-43 Runner accepts unknown provider keys          -> provider limits belong to a runner
   S9  FR-43 a run's limits ignored                        -> effective limits in the manifest
   S10 FR-43 a run's tool limits merged, not replacing     -> effective limits in the manifest
   S11 FR-43 manifest limits not built                     -> effective limits in the manifest
   S12 FR-43 manifest limits not written (postgres.py)     -> effective limits in the manifest (second run, A9)
   S13 FR-44 run slot not taken                            -> peak [default limits]
   S14 FR-44 tool limit ignored                            -> peak [the tool limited to 2]
   S15 FR-44 batches keyed on read_only                    -> undeclared tools run one at a time
   S16 FR-44 no call ever batched                          -> peak [default limits]
   S17 FR-44 every call batched                            -> undeclared tools run one at a time
   S18 FR-44 results in reverse order                      -> peak [default limits]
   S19 FR-44 siblings neither cancelled nor awaited        -> BaseException cancels and awaits siblings
   S20 FR-44 siblings cancelled but not awaited            -> BaseException cancels and awaits siblings
   S21 FR-44 slot not held around steps 6 to 9             -> peak [default limits]
   S22 FR-44 envelope tool_call_id not set                 -> peak [default limits]
   S23 FR-44 ToolCalled emissions not serialised           -> results in issue order [postgres]
   S24 FR-44 concurrency_safe always hashed                -> hash only when True
   S25 FR-44 a non-bool concurrency_safe accepted          -> hash only when True
   S26 FR-44 file tools not concurrency_safe               -> built-in tools declare it
   S27 FR-44 fetch not concurrency_safe                    -> built-in tools declare it
   S28 FR-45 file work on the default executor             -> store call never starved
   S29 FR-45 a queued call not cancelled by its timeout    -> queued call never starts [read] (second run, A9)
   S30 FR-45 thread count settable after the first call    -> setting after the first call refused
   S31 FR-45 threads misnamed                              -> store call never starved
   S32 FR-45 thread count setting ignored                  -> pool sized before its first call (subprocess)
   S33 FR-46 provider slot not taken                       -> provider limit caps concurrent sends
   S34 FR-46 provider limit per run, not per Runner        -> provider limit caps concurrent sends
   S35 FR-47 a str subclass source refused                 -> subclass source stored as exact text
   S36 FR-47 source bounded on its reported length         -> subclass source stored as exact text
   S37 FR-47 source bound written as >=                    -> 8192 accepted, 8193 refused [memory-8192]
   S38 FR-47 list does not withhold unstorable names       -> unstorable name withheld [memory-list]
   S39 FR-47 glob does not withhold unstorable names       -> unstorable name withheld [memory-glob]
   S40 FR-47 glob counts every unstorable entry it meets   -> unstorable name withheld [glob recursive]
   S41 FR-47 grep does not withhold unstorable names       -> unstorable name withheld [memory-grep]
   S42 FR-47 grep withholds without counting               -> unstorable name withheld [memory-grep]
   S43 FR-47 the unstorable-name test answers never        -> unstorable name withheld [memory-list]
   ```
   Not mutated, and so not claimed: migration `0004` (the live database records
   its checksum and refuses an edited file), `ProviderSlots`' per-loop keying and
   its lock, `_limits_by_name`'s guard for a mapping whose `items()` raises, the
   last-resort `except` around a slot's entry and exit in `run_prepared`, and the
   prepare-before-run ordering beyond what the batch-order test observes.
5. **AC-44 red check.** A throwaway test, copied into `tests/` only for the
   run, started one run and did not remove it. Run alone:
   ```
   ERROR at teardown of test_this_session_leaves_one_run_behind
   AssertionError: the test session changed the store's runs: {'runs': {'added': 1, 'removed': 0},
     'execution_manifests': {'added': 1, 'removed': 0}}
   1 passed, 1 error in 0.43s
   ```
   The probe file and its run were then removed (`probe runs removed: 1 | left: 0`).

## Issues the author found during M11

Labels `A` are the author's own and never share a reviewer's label.

| id | finding | disposition |
|---|---|---|
| A1 | **The spec disagrees with itself under `max_concurrent_tools=1`.** NFR-15 says such a run produces the same events as before Phase 2. FR-44 and AC-35 still batch `concurrency_safe` calls and prepare every call of a batch first, and AC-45 (M14) needs that (a second call queued behind the first). So in a batch where a later call is refused at steps 1 to 5, its `ToolCalled` is emitted before the earlier calls' events, and `before_tool` runs for every call before the first executes | implemented as FR-44 and AC-35 state; the NFR-15 test compares runs whose calls are all allowed. **Raised with the owner, not yet decided** |
| A2 | The NFR-8 test built its tool at import, so before the implementation the whole file would have failed to import | built per call, before the red run |
| A3 | A generator passed to `parametrize` raised a pytest deprecation warning | a list |
| A4 | `InMemoryEventSink` numbers an event by reading its length, then appending (KNOWLEDGE-e22f787f), and ToolCalled events of a batch now arrive together | the Runner emits one ToolCalled per run at a time; FR-52 (M12) moves numbering under each sink's own lock |
| A5 | `concurrency_safe` of `1` or `"yes"` would decide parallelism by truthiness | `ToolSpec` refuses anything but a bool, by field name. Not named by FR-44: a choice, declared here |
| A6 | An unstorable name met by `**` in `glob` would be counted twice: once by `**` and once by the part after it | counted only where the entry would be used: shown, or descended into |
| A7 | asyncio semaphores bind to the first loop that waits on them, and a Runner can outlive a loop | provider semaphores are kept per loop; runs on different loops share no limit |
| A8 | `agentsdk/postgres.py` has CRLF line endings in this working copy; every other changed file is LF. The first mutation run matched S12's LF snippet against raw bytes and reported "SNIPPET FOUND 0 TIMES", after a pre-check that read with universal newlines had passed it | the harness now matches CRLF files as they are on disk; S12 rerun and killed. `git diff --stat` shows 13 changed lines in `postgres.py`, so the edit did not rewrite its endings |
| A9 | S29's first run was recorded as killed by `ERROR asyncio: Future exception was never retrieved`, a log line, because the harness took the first line starting with ERROR | the harness now prefers a FAILED test line; S12 and S29 rerun, both killed by the tests named above (`tree restored: True`) |

## Attack these first

- **The batch boundary.** `_wait_all_or_cancel` and the executor's `run_prepared`.
  Cancel the task running a batch while siblings are waiting for a slot, inside
  `after_tool`, or inside the ToolCalled lock. A tool that swallows
  `CancelledError`. `KeyboardInterrupt` and `SystemExit`, which asyncio re-raises
  from the loop rather than setting on the task.
- **Slots.** A tool's slot is taken before the run's. Look for a deadlock or a
  starvation with mixed tool limits, and for a slot leaked on a path that raises
  between acquiring and step 6. Python 3.11.9 semaphore fairness under
  cancellation.
- **Step 6 is the line.** Timeout and declared provenance begin there. Is there a
  path where an error after the slot but before the tool is invoked carries the
  tool's labels, or where waiting for a slot counts against the timeout?
- **The file pool.** The window between `wait_for` expiring and a worker taking
  the item; `set_file_tool_threads` racing a first call on another thread; a walk
  still running at interpreter exit; contextvars copied into the worker.
- **FR-47.** A `str`-subclass source through a route the tests do not use; the
  8,192 bound on every route; an unstorable name in `_names_in` (the 8.3 check),
  as a reparse point met by `glob`, or at the `list_directory` entry cap.
- **AC-44.** The session check compares run ids in four tables. Can a test leave
  a row it does not see, or remove rows it did not write?
- **NFR-15.** Beyond A1: request payloads, history and events of runs whose tools
  declare nothing, and of turns with one call, against `7abfe47`.

## Declared limitations: known, recorded, NOT findings

- A1, until the owner decides it.
- A synchronous tool still runs inline on the event loop (FR-5) and gains nothing
  from a batch.
- A file tool already running cannot be interrupted, and holds its pool thread
  until its walk budget (SPEC risk). The fetch tool's name resolution still uses
  the default executor (SPEC risk).
- Provider limits are per Runner, not per process (SPEC risk).
- `RunResult.events` on Postgres, run handles, streaming and cancellation are
  M12. `queued_ms` and timings are M14.
- `PostgresTrace.reconstruct` does not return `scheduler_limits`; the test reads
  the column.
- Older `independent-review` gates compute `stale` because the repository hash
  moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Distinguish a defect from a gate blind spot over correct code.
- Restore every file you mutate and verify SHA-256, restoring in a `finally`, and
  kill the whole process tree on a timeout (`taskkill /T`). Do not mutate a
  migration file: the live database records its checksum and refuses an edited
  one.
- Remove every run you write, or AC-44's session check fails the next session.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$`
out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M11-concurrency-foundations --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M11-concurrency-foundations --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found, and your verdict.
