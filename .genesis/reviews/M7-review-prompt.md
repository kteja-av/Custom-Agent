You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** M5 took nine rounds, M6 three, and this is M7's second.
Every defect but one across those rounds was found in a region the previous
reviewer had not examined; reviewer rotation, not reviewer effort, is what
moved them. If you have reviewed this project before, say so and ask for a
different session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

**This milestone needs a real database.** `DATABASE_URL` is in `.env`; the
regression gate also needs `BASE_URL` and `MODEL_API_KEY` because it runs M6's
live golden eval. **Never print credentials.** Install dependencies first:
`.venv\Scripts\python.exe -m pip install -r requirements.txt` (`psycopg_pool`
is new in M7).

## Task under review

**M7-store-hardening**: "Concurrent writers to one run all commit, event
sequence numbers come from the database, persistence stops blocking the event
loop, and a schema change can reach an existing database, so Phase 2 fan-out
lands on a store that survives it."

Round 1 **rejected**; this is the repair. Both executable gates pass.

### Requirements

FR-17 (migrations), FR-18 (DB-assigned event sequence), FR-19 (concurrent
appends available, not merely safe), FR-20 (persistence does not block the
event loop; pooled connections), FR-21 (parent run linkage), NFR-8 (stall and
wall-clock bounds), AC-11..AC-15. Read them in `SPEC.md` under "Phase 2
readiness"; each AC states what failed before the fix.

## Round 1: rejected, and what changed

Reporting something already fixed costs a round. Finding the same SHAPE
somewhere nobody looked is what has worked every time.

### D1: the event-loop offload was incomplete. Fixed.

FR-20 moved the session store and the loop's own emit onto a worker thread
and left three event writes on the loop: `RunStarted` and the terminal event
in `api.py`, and `ToolCalled`, which the executor called through a synchronous
callback the Runner therefore could not offload. Each takes the event stream's
advisory lock, so the reviewer held that lock from another connection and
measured a 479 ms whole-loop stall with an unrelated run frozen too, against
14 ms on the offloaded messages path.

Repair: all three emits are threaded, and `ToolExecutor._safe_emit` and
`_failed` are now `async` and await whatever the emit callback returns (the
rule `_invoke` already applied to tools). **Detector:** a thread-identity spy
records the thread every store call runs on, across four terminal paths
(completed with a tool call, model failure, total-boundary failure, max turns
exhausted). Against the unrepaired code it found on-loop writes on **every**
path, not only the completed one (counts 3, 2, 1 and 4). The reviewer's
lock-hold probe is also now a test, with detection thresholds far from both
outcomes (800 ms hold, 200 ms threshold).

### D2: the gate was unreliable in both directions. Fixed by replacing the detector, not the bound.

The reviewer showed the six-run NFR-8 test passing 6 of 6 with the offload
reverted, and the 24-run test failing 1 in 4 with it in place. The author then
measured the worst-stall distribution (six samples each) across three builds:

| build | 6 runs median / max | 24 runs median / max |
|---|---|---|
| repaired | 12.6 / 15.5 ms | 13.9 / 15.9 ms |
| round 1 as reviewed | 7.7 / 19.0 ms | 15.2 / 28.2 ms |
| no offload at all | 15.9 / 60.3 ms | 62.0 / 81.2 ms |

**Neither timing test can see the defect round 1 was rejected for**: that
build stays well under 50 ms. The 24-run test was **removed**: it cannot
detect round 1's defect, catches a full revert about half the time, and
false-alarmed for the reviewer. The six-run test is **kept as AC-14's literal
acceptance measurement** and its docstring now says it is not a detector.

Two of the author's own claims are **retracted** in the code, not silently
rewritten: that a pool-only build failed six runs "5 of 5" (did not reproduce:
median 16 ms), and that the six-versus-24 detection "reversal" was
unexplained (did not reproduce: with the offload fully reverted, stall grows
with fan-out, as the reviewer's explanation predicts. The effect measured for
round 1's *partial* offload was smaller than the reviewer's "~5 ms from the
bound": 24-run median 15.2 ms, max 28.2 ms).

### D3: RunConfig accepted a malformed parent_run_id. Fixed.

`RunConfig` constructed around `"not-a-uuid"`, a NUL and an int while its
sibling fields refused theirs, and the test named for RunConfig tested the
store. `RunConfig.__post_init__` now calls the same
`column_rejection_reason(value, "UUID")` the store uses. The misnamed test is
renamed `test_the_run_store_refuses_a_malformed_parent_run_id`, and a real
RunConfig test exists.

### D4: found by the author during the repair, not by the reviewer

A differential (every UUID form the guard accepts, sent to a real `uuid`
column) found the guard accepting `urn:uuid:...`, which the column refuses:
`uuid.UUID()` strips that prefix. The guard now accepts only the canonical
form. That also refuses uppercase, braced and unhyphenated forms the column
*would* accept; this is deliberate, the safe direction, and every run id the
SDK issues is canonical. The differential is now a test.

### A correction to the round-1 prompt

The round-1 prompt said "Every other one of the 423 pre-existing tests is
unchanged." **That was false.** Two pre-existing test files changed during M7:

- `tests/test_golden_eval.py`, 12 lines, in commit `8cfca1f`: `LiveRun` carries
  `first_error` and the retry assertion prints it. The round-1 reviewer judged
  it strengthening, not weakening, and noted the non-disclosure as a caveat.
- `tests/test_persistence.py`, 7 lines: one unfit value for `parent_run_id`,
  required by a test that asserts on `inspect.signature(start_run)`.

Verify rather than trust: `git diff --stat e5e2c00 -- tests/`.

### Housekeeping

Two `SYN-why-*` runs the round-1 reviewer correctly left alone were the
author's own probe debris. Removed: 26 `run_events`, 24 `messages`, 2
`execution_manifests`, 2 `runs` (54 rows; the reviewer's count was 52).

## What to do

1. Read `SPEC.md`'s "Phase 2 readiness" sections and the knowledge in
   `.genesis/project.json`, especially `KNOWLEDGE-c0f23fea`,
   `KNOWLEDGE-3afb9c70` and `KNOWLEDGE-89b020d7` (round 1's lessons).
2. Re-run both gates:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_phase2_readiness.py -q   # 22
   .venv\Scripts\python.exe -m pytest -q                                  # 445
   ```
   Per file: `test_agent_loop 77` + `test_golden_eval 15` +
   `test_model_client 151` + `test_persistence 80` +
   `test_phase2_readiness 22` + `test_primitives 74` + `test_tool_executor 26`
   = **445**. A different number is itself a finding. (Round 1 was 442: +4 new
   detectors, -1 removed 24-run test.)
3. **Mutation-test.** The author's round-2 matrix, 6 of 6 killed, none errored:
   ```
   R1 executor stops awaiting the emit it is handed   -> 7 M5/M6 tests (ToolCalled never persisted)
   R2 ToolCalled emitted synchronously again          -> thread-identity test
   R3 RunStarted emitted on the loop again            -> thread-identity + lock-hold tests
   R4 terminal event emitted on the loop again        -> thread-identity test
   R5 RunConfig stops refusing parent_run_id          -> RunConfig test
   R6 UUID guard accepts non-canonical forms again    -> RunConfig + differential tests
   ```
   Re-run them and invent better ones. Restore in a `finally` and verify
   SHA-256. **A mutant that errors out did not run.** Source files are CRLF:
   a multi-line anchor matched against raw bytes silently fails to apply.

## Attack these first

- **The thread-identity spy is an enumeration.** It wraps exactly five method
  names (`append`, `history`, `emit`, `start_run`, `finish_run`) and asserts
  each was observed, but NOT that those five are every store I/O the Runner
  performs. A store call through any other method would be invisible to it.
  That is the shape that produced five of M5's nine rejections. Is there store
  I/O on the Runner path outside those five? Could one be added tomorrow
  without the test noticing?
- **The executor's total boundary now awaits inside its failure path.**
  `execute` catches `Exception` and `return await self._failed(...)`. What does
  a sink coroutine that hangs do to tool execution? What does cancellation
  during that await do? Is any guarantee the boundary made in M2 now weaker?
- **Event ordering through threads.** Within one run every emit is awaited
  before the next, so stored sequence should match emission order. Verify it.
  `PostgresEventStore._buffer` is appended from a worker thread; if two emits
  for one sink were ever in flight at once, buffer order and DB order could
  diverge. Is that reachable today, or only latent until Phase 2?
- **The lock-hold test only holds during `RunStarted`.** The run cannot pass
  that emit while the lock is held, so every later store path is covered only
  by the thread-identity test. Is that enough?
- **AC-14 as written in SPEC.md cannot detect the defect it exists to prevent.**
  The author did not amend the approved spec, since that is the owner's
  decision, and proposes it be raised there. Judge whether the milestone can
  pass with an AC whose only test is an acceptance measurement, given that
  untimed tests carry the detection.
- Round 1's reviewer accepted the `to_thread` reading of FR-20's "non-blocking
  I/O" and the NFR-7 contract defence. Do not re-litigate those without new
  evidence. Still open from round 1's prompt and not reported: the advisory
  lock across processes and under `hashtext` collisions; migrations run
  concurrently, failing halfway, or edited after being applied (there is no
  checksum); and the parent-tenancy check.

## Declared limitations: known, recorded, NOT findings

- No retention or partitioning; `messages` and `run_events` grow unbounded.
- Provider neutrality half-proven; Phase 0/1's second wire format deferred by
  choice, recorded in `SPEC.md`'s open questions with its evidence.
- `Usage` cannot represent cached tokens; `Message.content` is a single string
  against Anthropic's content blocks. Both additive.
- The UUID guard refuses non-canonical forms a `uuid` column accepts
  (deliberate; see D4).
- The older `independent-review` gates (SPEC-1, M3, M4, M5) compute as `stale`
  because the repo hash moved after those reviews. That is the hash working.
- About 994 `runs` rows have no manifest (pre-M5 development data), and roughly
  260 persisted tool results carry a null `source_uri_or_hash` from earlier
  mutation runs. Neither is produced by current code.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Distinguish a defect from a gate blind spot over correct code.
- Leave the database as you found it and say what you removed. Probe rows use a
  `SYN-` tenant prefix and throwaway schemas are named `m7_*`; both are zero now.

## Recording your decision

Use **single quotes** around `--reason`. Round 1's reason recorded whole this
way; an M6 reviewer lost a clause to backtick substitution without them.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M7-store-hardening --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M7-store-hardening --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found, and your verdict.
