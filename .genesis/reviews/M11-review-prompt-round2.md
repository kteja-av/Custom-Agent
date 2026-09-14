You are the independent L4 reviewer for round 2 of a bounded task in a
Genesis-governed repository. You did not write this code and you must not trust
its author's claims about it.

**Use a fresh model, not Claude Fable 5.1.** Round 1 was reviewed by Claude Fable
5.1; the owner asked for a different model for this round. If you have reviewed
this project before, say so. Re-derive the requirements from `SPEC.md` rather
than inheriting the framing below.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

Both gates need `DATABASE_URL` from `.env`; the regression gate also needs
`BASE_URL` and `MODEL_API_KEY`. **Never print credentials or a connection
string.** Install first: `.venv\Scripts\python.exe -m pip install -r
requirements.txt`. **Windows Developer Mode must be on** (D7).

## Task under review

**M11-concurrency-foundations**, round 2, risk **high**. Requirements
FR-43..FR-47, NFR-15..NFR-18, AC-34..AC-38, AC-43 and AC-44 in `SPEC.md` under
"Phase 2, increment 1". Round 1's full prompt is
`.genesis/reviews/M11-review-prompt.md`; read its "What changed" and "Attack
these first" sections, because this round's changes are small and sit inside
that code.

## Why there is a round 2

Round 1 (Claude Fable 5.1, 2026-09-14) approved with five caveats, recorded in
the Genesis control for the task. The owner then rechecked the reviewer's claims
and probes first-hand and judged **R1 blocking**: it breaks a guarantee FR-44
states outright. Round 1's approval was computed over source hash `76d9c077`;
the repairs below change the source, so that approval no longer covers it.

| id | round 1 finding | owner's recheck | disposition |
|---|---|---|---|
| R1 | `_wait_all_or_cancel` waited with `FIRST_EXCEPTION`, which asyncio does not wake for a task that ends cancelled. A call ending in `CancelledError` left its siblings running to their own end | Reproduced. FR-44: a `BaseException` escaping a call cancels and awaits its siblings, and `CancelledError` is one. A sibling with no timeout held the run open without bound (still blocked at a 4 s limit), its side effects continuing after the run failed. Reachable by a tool awaiting a helper future that something else cancels. No test and no mutant covered it | **repaired**: the wait wakes on every completion and treats a cancelled task as an escape |
| R2 | A second cancel landing in the `finally` await left the already-cancelled siblings unawaited | Reproduced: 4 tasks still pending after the run ended, finishing about 0.5 s later | **repaired**: the cancel-and-wait absorbs further cancellation until every sibling has finished, then raises what was already on its way out |
| R3 | `glob` counted a withheld folder twice when a pattern both descends into it and shows it (`**`, `**/*`) | Reproduced: 3 withheld where there are 2 | **repaired**: withheld unstorable entries are counted once per directory and name |
| R4 | AC-44's check is keyed on run ids per table, and the golden-eval cleanup deletes every `t-` run started after the module began | Agreed: cannot happen in the suite as it stands | not changed; declared below |
| R5 | `KeyboardInterrupt` and `SystemExit` end the loop and leave sibling teardown to `asyncio.run` | Agreed | not changed; declared below |

The owner also checked, as not a finding: `grep` given an unstorable folder as
its path would return a lone-surrogate path, but a model cannot send one (tool
arguments are checked for storability when a `ToolCall` is built,
`primitives.py:177`), so only trusted in-process code could (D13).

## What changed since round 1

The round 1 tree is not committed either: HEAD is still `7abfe47`. Review the
repairs with:

```bash
git diff 7abfe47 -- agentsdk/loop.py agentsdk/builtin_tools.py tests/test_concurrency.py
```

- `agentsdk/loop.py`: `_wait_all_or_cancel` waits with `FIRST_COMPLETED`, raises
  `CancelledError` for a task that ended cancelled and the exception of one that
  raised, and on any `BaseException` (its own cancellation included) calls the
  new `_cancel_and_wait`, which cancels the rest and loops until every one is
  done, absorbing further `CancelledError`s, before the original is re-raised.
- `agentsdk/builtin_tools.py`: `_glob` keeps a set of `(directory, name)` for
  entries withheld for an unstorable name and counts each once.
- `tests/test_concurrency.py`, written first and failing against the round 1
  code:
  - `test_a_cancelled_error_escaping_one_call_cancels_and_awaits_its_siblings_at_once`:
    a tool awaits a helper future that is cancelled while two siblings with no
    timeout wait on gates; the run must raise `CancelledError` within 2 s with
    both siblings' cleanup finished and no task left. Round 1 code:
    `TimeoutError` at the test's 5 s bound.
  - `test_cancelling_a_batch_twice_still_awaits_every_call_before_the_run_ends`:
    the task running the run is cancelled while three calls wait, then cancelled
    again during their 0.3 s cleanup; every cleanup must finish before the task
    ends. Round 1 code: `the run ended before its calls finished: cleaned []`.
  - `glob everything` (`**`) and `glob everything below` (`**/*`) added to the
    withheld-names cases, on both stores. Round 1 code: 3 withheld, expected 2.

## What the author ran

1. **Red first**, against the round 1 code: the 6 new tests failed, for the
   reasons above; the 94 existing tests of the file were deselected.
2. **After the repairs**: `tests/test_concurrency.py` and
   `tests/test_builtin_tools.py` together, 187 passed.
3. **Full suite** before the gates: 921 passed in 148 s. Two more glob cases
   were then added (item 4), making 925.
4. **Mutation matrix for the repairs**, same harness as round 1 (every file
   restored and SHA-256 verified, the `agentsdk` tree hashed before and after,
   then a separate scan finding no mutant text left):
   ```
   S19 siblings neither cancelled nor awaited (retargeted to the new code)  -> KILLED, BaseException test
   S20 siblings cancelled but not awaited (retargeted to _cancel_and_wait)  -> KILLED, BaseException test
   S44 R1 the batch waits on FIRST_EXCEPTION again                          -> KILLED, CancelledError test
   S45 R2 a second cancel obeyed                                            -> KILLED, cancelled-twice test
   S46 R3 glob counts a withheld entry each time it meets it                -> KILLED, [memory-glob everything]
   S40 glob counts every unstorable entry it meets                          -> SURVIVED, then KILLED (A10)
   ```
   The other 37 mutants were run against the round 1 tree only (43 of 43
   killed there) and not rerun against the repairs; their code is unchanged.
5. **Gates**, on the final tree. The first attempt's tool call was interrupted;
   its Genesis process kept running and finished on its own, and no process was
   left afterwards. Evidence:
   ```
   unit        exit 0  2026-09-14T11:43:51Z -> 11:44:08Z  104 passed in 15.84s
   regression  exit 0  2026-09-14T11:44:08Z -> 11:46:22Z  925 passed in 132.91s
   source hash 0b6df365 (round 1 approval covered 76d9c077)
   ```
   Full suite **925 tests**: round 1's 915, plus the 2 cancellation tests and 8
   glob cases (4 patterns on 2 stores) added in this round.

| id | issue the author found in round 2 | disposition |
|---|---|---|
| A10 | With R3's per-entry set in place, mutant S40 (count every unstorable entry glob meets) survived: on the fixture tree every entry met was also used, so the rule deciding what counts no longer changed any result | two cases where an entry is met and not used: `**/*.md` (1 withheld) and `*/inner.txt` (1 withheld). They pass on the round 1 code as well, so they close a mutation gap rather than prove a defect; S40 rerun and killed by `[memory-glob a pattern the file does not match]` |

## Attack these first

- **`_cancel_and_wait` absorbs cancellation.** Is there a way for it never to
  return: a sibling that swallows `CancelledError` and keeps waiting, a sibling
  cancelled while inside `run_prepared`'s slot or the ToolCalled lock, a batch
  whose outer task is cancelled repeatedly? Is the original exception always the
  one raised, and does a caller cancelling the run still see `CancelledError`?
- **Order of discovery.** When several tasks finish in one wakeup, one raising
  and one cancelled, which is raised, and does it matter?
- **Python 3.11.9 semantics.** Absorbing a `CancelledError` inside a task leaves
  its cancellation count raised. Check that nothing above the batch
  (`asyncio.wait_for` in the executor, `asyncio.timeout` in the fetch tool, a
  caller's `asyncio.timeout`) is misled by it.
- **R3's set.** Can one entry reach the counter under two different
  `directory` spellings, for example through a junction met twice?
- **Everything round 1 did not examine** in the M11 diff, as before.

## Declared limitations: known, recorded, NOT findings

- A1 (round 1 prompt), **now decided**: the owner amended NFR-15 on 2026-09-14
  (DECISION-7a0b4459) to name the ordering FR-44 requires under
  `max_concurrent_tools=1`: a batch of `concurrency_safe` calls still passes
  every call through steps 1 to 5 first, so a call refused there records its
  `ToolCalled` before the events of calls issued ahead of it. The code is
  unchanged. Check the amended NFR-15 against the code; the ordering itself is
  not a finding.
- The owner declined rotating the development database password
  (DECISION-1a5942ea); `SPEC.md` Risks was reworded to match.
- R4 and R5, above.
- A synchronous tool still runs inline on the event loop (FR-5). A file tool
  already running cannot be interrupted. Fetch name resolution uses the default
  executor. Provider limits are per Runner. Run handles, streaming and
  cancellation as a feature are M12; timings are M14.
- Older `independent-review` gates compute `stale` because the repository hash
  moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Restore every file you mutate and verify SHA-256, restoring in a `finally`, and
  kill the whole process tree on a timeout (`taskkill /T`). Do not mutate a
  migration file. Remove every run and temporary folder you create, and any git
  worktree; AC-44's session check fails the next session otherwise.
- Include every probe you ran in your report, including ones that showed nothing.

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
