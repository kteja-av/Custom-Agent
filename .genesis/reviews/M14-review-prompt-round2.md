You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**If you have reviewed `M14-timings-and-telemetry` before, stop and say so**, and
ask the human for a different model or a clean session. Round 1 was reviewed by
Opus 5, which also implemented the task; use another model. Re-derive the
requirements from `SPEC.md` rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need `DATABASE_URL` from `.env`, and the regression
  gate also needs `BASE_URL` and `MODEL_API_KEY`. **Never print credentials or a
  connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`.
  Round 2 adds no dependency.
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every
pytest session compares the store's run and artifact ids at its start and end
(AC-44), the development database is shared, and M14's timing tests assert lower
bounds. Before running tests or gates, check that no other `pytest`,
`genesis.mjs gate` or example process is running. Run your own probes with
`-p no:cacheprovider`: Genesis hashes `.pytest_cache`, so a cache write can make
a gate read stale with no code change.

## Task under review

**`M14-timings-and-telemetry`**: Every model and tool call records its timings, and an optional OpenTelemetry exporter turns a run into a span tree without ever affecting the run.

**Round 2.**
- **Round 1:** approved (KNOWLEDGE-0c0a96f9) with caveats C1 to C9 and no
  blocker. That approval stands for everything outside this change.
- **The owner's decision (DECISION-95a84cb0):** C2 is a defect, and C1 is fixed
  with it, before M14 is completed. This round covers **only that change**.
- **Out of scope:** C3 to C8 stay recorded caveats. C9 changed as a side effect,
  and that change is in scope (B5).
- **State:** `active`. `unit` and `regression` are rerun on the round 2 tree (see
  "What the author ran"), and `independent-review` is to be recorded by you. The
  gates passing is not sufficient and is not what you are judging.

### The caveats being fixed, as round 1 reported them

- **C2:** a `before_model` hook that names the model as a `StrEnum` member or a
  `str` subclass. `ModelCalled.model` recorded the run's model (`priced`) while
  the client received the named one (`other`), and the call was priced as
  `other` (2.000 against 0.002). Cause: `loop.py` accepted only an exact `str`
  for the record, while the Runner's `cost_of` priced any `str`.
- **C1:** `export(handle)` called from another thread's event loop could leave the
  caller's own `events()` iterator hung, and `flush()` never finishing. It
  happened when that loop ended or ran in debug mode. Cause: `handle.py` `_wake`
  emptied its waiter list and then resolved each waiter with `set_result` from
  the run's thread, so one raising left every waiter after it unresolved. Also,
  the exporter's follow task lived on the caller's loop.

### Requirements it claims to satisfy (verbatim from SPEC.md)

- FR-30 (excerpt): "The model priced is the one the request was sent with."
- FR-48 (excerpt): "A run belongs to the event loop that started it."
- FR-49 (excerpt): "Every `events()` iterator yields the whole stream from the run's first event, whenever it was opened, then each new event as it is recorded, in `sequence_no` order, holding an event back until every lower number has been yielded; it ends when the run ends, after its terminal event, or after the last recorded event if the terminal event could not be recorded. Several iterators may be open at once, and each receives every event."
- FR-57 (excerpt): "`model`, the model the request was sent with (a `before_model` hook may change it; with no model named it is FR-32's recorded model; FR-30 prices by it)"
- FR-58 (excerpt): "It consumes a `RunHandle` (FR-49) or a finished `RunResult`, so it adds nothing to the path a run takes."
- FR-60 (excerpt): "Telemetry never affects a run. The exporter owns one worker thread fed by a bounded queue, and every span is created and ended on that thread, whatever span processor the caller's provider uses, so a synchronous processor or a blocking exporter cannot stall the event loop."
- AC-45 (excerpt): "a `before_model` hook that changes the model yields a `ModelCalled.model` equal to the model the client received."

### Standing invariants that constrain every task

- Every `ToolResult` carries exactly one `ContentProvenance`.
- Every persisted row carries non-null `tenant_id` and `project_id`.
- `ToolExecutor` order is fixed: resolve → validate → permission → execute.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- Application code calls `Runner.run()` and nothing else.
- Only `AgentSDKError` subclasses may escape `ModelClient.send()`.
- A boundary's error path must not itself be able to raise.
- A cancellation is recorded, then re-raised (INVARIANT-73299c7c).

### Files in scope

- `agentsdk/loop.py`: `sent_model()` (was `_sent_model`), the new `_recordable()`,
  and the `ModelCalled` payload's `model`.
- `agentsdk/api.py`: `cost_of` inside `Runner._run`, and the import of
  `sent_model`.
- `agentsdk/handle.py`: `RunHandle._wake` and the new `_resolve`. **This file
  belongs to M12, an approved milestone:** breaking any FR-49 or FR-50 guarantee
  is a defect.
- `agentsdk/telemetry.py`:
  - `export`, whose docstring and handle branch changed;
  - the new `_follow_on_run_loop` and `_followed`;
  - `_follow`, whose `finally` is removed.
- `tests/test_telemetry.py`:
  - the round 2 tests (see "What changed");
  - one test changed from round 1, formerly
    `test_a_hook_naming_a_model_no_store_can_hold_records_the_run_model` (B2).

**Seeing only the round 2 change.** M14 is not committed; HEAD is still `22d71c6`,
so `git diff 22d71c6` shows round 1 and round 2 together. The round 2 patch for
the four code files is `.genesis/reviews/M14-round2.patch` (+101 -27).
- **How it was built:** by reversing each round 2 edit on the current tree, where
  every reversed snippet had to occur exactly once. It was not checked against
  the round 1 tree's hash (`6ce35a78`) itself.
- **What it leaves out:** the test changes. Those are the tests named under
  "What changed", in the "Round 2" section of `tests/test_telemetry.py`, plus
  the one changed round 1 test (B2).

## What changed

- **C2.** One function, `loop.sent_model(request, recorded)`, now decides the model
  a request was sent with, for `ModelCalled.model` and for the call's price alike.
  - **Text in `model_settings["model"]`:** the exact characters it holds, copied
    with `str.__getitem__(named, slice(None))`, so a `StrEnum` member or another
    `str` subclass is the text the client puts on the wire.
  - **Non-empty text:** is the model.
  - **Anything else** (not text, empty, absent): the run's recorded model (FR-32).
  - **The record:** `ModelCalled` stores it through `_recordable()`, which records
    `None` for a name no column can hold.
  - **The price:** `cost_of` returns `self._cost(sent_model(request,
    recorded_model), usage)`.
- **C1, in two layers.**
  - **`export(handle)`** follows the run on the run's own loop, whichever thread or
    loop it is called from:
    - a task on that loop when the caller is on it;
    - `asyncio.run_coroutine_threadsafe` onto it when that loop is running;
    - otherwise the run has ended or cannot progress, and the events the handle
      recorded are queued at once.
    - A follow is counted as ended in a done callback (`_followed`), not in
      `_follow`'s `finally`, which a follow cancelled before its first step never
      reaches.
  - **`RunHandle._wake`**, on the run's loop, resolves each waiter on the loop it
    belongs to: directly when that loop is the running one, through
    `call_soon_threadsafe` otherwise. It skips one whose loop has closed, so no
    waiter strands the rest.
- **Tests** in `tests/test_telemetry.py`, section "Round 2":
  - C2: a `StrEnum` member and a `str` subclass, on both stores; `ModelCalled.model`
    must be exactly `"other"` and the cost that of `other`. 4 tests.
  - C1: `export(handle)` from another thread's loop, with debug off and on, and that
    loop still running or closed before the run ends. The caller's own iterator
    must end, `flush` must finish, and the spans must be exported. 4 tests.
  - The handle alone:
    - an iterator abandoned on a closed foreign loop must not strand the run loop's
      iterator (1 test);
    - an iterator on a live foreign loop in debug mode must see every event
      (1 test).
  - A handle whose run loop has already closed exports the events it recorded
    (1 test).
  - Changed: the round 1 test for a hook naming an unusable model (B2).

## Issues the author found during round 2

Labels `B` are this round's and never share a label with round 1's `A` or `C`.

| id | finding | disposition |
|---|---|---|
| B1 | C2 was two rules for one fact: the record accepted an exact `str` and the price any `str` | one function for both. A `str` subclass is its held text, the way FR-47 treats a tool's source |
| B2 | The round 1 test added for mutant L7 expected an unstorable hook-set name (a NUL in it) to be recorded as the run's model. That is C2's mismatch in another form: the client received one name and the event named another | changed: an unstorable name records `None` and prices to unknown, because no registry entry matches it. A non-text or empty value names no model, and both record and price use the run's model. That test's cases now assert the cost too |
| B3 | The OpenAI-compatible client sends `model_settings.get("model", default)` as given (`providers/openai_compatible.py`), so an empty string or `7` reaches the provider | declared: such a value names no model under FR-57, and the event and price use the run's model. A real provider would refuse the request, so no `ModelCalled` would be emitted |
| B4 | Fixing only `_wake` would still leave the exporter's follow task on a foreign loop that can end before the run does; fixing only the exporter would leave any caller's own cross-loop iterator strandable | both layers fixed, each with its own test |
| B5 | **C9 changed as a side effect.** `export(handle)` with no running loop no longer raises `RuntimeError`: it follows the run on the run's loop, or queues the recorded events of a run whose loop has stopped | in scope for this review; not separately tested beyond the closed-run-loop test |
| B6 | Residual: a follow scheduled with `run_coroutine_threadsafe` onto a loop that stops before running the scheduled callback never completes, so `flush()` waits until its timeout. A run loop that is stopped but not closed is treated as ended | declared, not tested |
| B7 | Two tests were written after the fix, not before it: the live-foreign-loop iterator and the closed-run-loop export. They are the paths of mutants W4 and W6 | declared: they close gaps in the fix, and the mutation run proves they bite |
| B8 | The mutation run found no survivor, so no test was added after it | none needed. 41 of 41 killed on the round 2 tree |

## What the author ran

1. **Red run**, the round 2 tests against the round 1 code: 11 failed, 4 passed.
   - **C2 (4) and the unstorable name (2):** failed with `AssertionError: priced`.
   - **C1:** 3 failed with "the caller's own events() iterator hung after
     ['RunStarted']". The fourth (debug off, loop still running) failed with
     "flush never finished": the waiter was resolved from the wrong thread and its
     loop never woke.
   - **The abandoned-iterator test:** failed with "the run loop's iterator hung
     after ['RunStarted']".
   - **The 4 passes:** the non-text and empty cases on both stores, whose record
     and price already agreed.
2. **Green run** of `tests/test_telemetry.py`, `test_run_handles`,
   `test_agent_loop`, `test_honest_results` and `test_concurrency` together:
   605 passed in 71.40 s (telemetry 89, run handles 64, agent loop 77, honest
   results 271, concurrency 104). The M12 handle tests and M9 pricing tests pass
   unchanged.
3. **Mutation run** of all 41 M14 mutants on the round 2 tree, since the code
   changed in four files, not only the ten mutants that point at new code. The
   ten: L4, L5 and L7 re-pointed at the new snippets, plus W1 to W7 (listed
   below).
   - **Checks:** every file restored and SHA-256 verified, `tree restored: True`,
     and a scan before and after found every snippet exactly once and no mutant
     text left.
   - **Result: 41 of 41 killed.** The round 2 mutants were killed by:
     - W1: the C2 test [memory-a StrEnum member].
     - W2: the no-usable-model test [memory-an empty model], the first to fail
       under `-x`.
     - W3: the abandoned-iterator test.
     - W4: the live foreign loop test.
     - W5: the C1 test [debug off-closed before the run ends].
     - W6: the closed-run-loop test.
     - W7: the C1 test [debug off-still running].
   - **L4, L5, L7:** killed by the hook-model, no-usable-model [not text] and
     no-usable-model [unstorable] tests.
   ```
   W1 a str subclass names no model
   W2 a call priced by its own rule
   W3 a waiter on a closed loop strands the rest
   W4 a foreign waiter resolved from the wrong thread
   W5 a handle followed on the caller's loop
   W6 no fallback for a run loop that has closed
   W7 a follow on another loop never counted as ended
   ```
4. **Test counts**, verified immediately before handover by the gate run below,
   the last test run:
   - `tests/test_telemetry.py`: 89 (78 at round 1 plus the 11 round 2 tests).
   - The full suite: 1199 (1188 plus 11). `README.md` says 1199.
5. **Gates**, on the round 2 tree, with `--timeout 600000` and no other test
   process running:
   ```
   $ genesis gate . M14-timings-and-telemetry --timeout 600000
   M14-timings-and-telemetry: passed executable gates
   unit        exit 0  2026-09-15T15:48:57Z -> 15:49:21Z  89 passed in 23.14s
   regression  exit 0  2026-09-15T15:49:21Z -> 15:52:02Z  1199 passed in 160.76s
   ```
   - **Hash:** both gates carry source hash `f131773d`.
   - **State:** `KICKOFF.md` reads `unit:pass, regression:pass,
     independent-review:stale`. The round 1 approval was recorded at `6ce35a78`;
     this round's decision replaces it.

## What to do

1. Read the files in scope, the round 2 patch, `SPEC.md`, and DECISION-95a84cb0 and
   KNOWLEDGE-0c0a96f9 in `.genesis/project.json`.
2. Rerun the gates yourself. A different number is itself a finding.
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_telemetry.py -q     # expect 89 passed
   .venv\Scripts\python.exe -m pytest -q                             # expect 1199 passed
   ```
3. Mutation-test the change. Rerun the author's mutants above, then invent your
   own: the useful question is which one still survives. Restore every mutated
   file in a `finally` and verify SHA-256.
4. Save every probe script you write where the human tells you, so each can be
   rerun one at a time.

## Attack these first

**Report every item below as "probed", with what you ran and saw, or "not
probed", with why.** An approval that is silent on an item will be read as not
probed.

1. **C2 in every form:**
   - `StrEnum`, and a `str` subclass overriding `__str__`, `__eq__`, `__len__` or
     `__getitem__`;
   - an unstorable name, whitespace, non-text, empty.

   On both stores, the recorded `model`, the recorded `cost_usd` and
   `RunResult.cost_usd` must agree with each other and with what the client
   received, and with what the OpenAI-compatible client serialises onto the
   wire.
2. **C1 through the exporter.** `export(handle)` from another thread and loop:
   - with debug on and off;
   - with that loop ending at every point (before the run starts sending, during
     it, after it);
   - from no loop at all (B5);
   - many handles at once;
   - a handle whose run loop has closed, and a run loop that is stopped but not
     closed (B6).

   Also the cancellation of a follow, and `flush` and `shutdown` timeouts.
3. **`_wake` against M12.**
   - Every FR-49 guarantee on both backends: ordering, several iterators,
     iterators opened late, cancelled iterators, waiter removal in `_stream`'s
     `finally`.
   - FR-50's cancellation paths, with iterators waiting.
   - Race `_wake` against an iterator being cancelled on another loop.
4. **No effect on a run (FR-60, NFR-17).** Status, result, events and stored rows
   with the exporter following from a foreign loop, on both stores.
5. **NFR-15.** Existing behaviour elsewhere is unchanged: the regression suite,
   and anything you think it misses.

## Declared limitations: known, recorded, NOT findings

- **Round 1 caveats C3 to C8** (KNOWLEDGE-0c0a96f9), and C9 as changed by B5.
- **B3 and B6** above.
- **Round 1's author issues and limitations** are in
  `.genesis/reviews/M14-review-prompt.md`.
- **Stale gates:** older `independent-review` gates compute `stale` because the
  repository hash moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything
  you assert.
- **Approve if it is sound.** Prior rejections do not mean another is owed. A defect
  must be reachable and must matter. If your only findings are latent,
  out-of-scope or cosmetic, approve and record them as caveats in your reason
  rather than blocking.
- **Name what you found:** a defect, or a gate blind spot where the code is
  correct.
- **Clean up.** Remove every run, artifact, container, temporary folder and git
  worktree you create, artifacts before the runs they name. Kill the whole process
  tree on a timeout (`taskkill /T`). Do not mutate a migration file.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$`
out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M14-timings-and-telemetry --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M14-timings-and-telemetry --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found per attack item, and
your verdict.
