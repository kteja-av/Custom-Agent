You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** M5 took nine rounds and M6 took three. Every defect but
one was found in a region the previous reviewer had not examined, and reviewer
rotation — not reviewer effort — is what moved them. If you have reviewed this
project before, say so and ask for a different session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

**This milestone needs a real database.** `DATABASE_URL` is in `.env`, and the
regression gate additionally needs `BASE_URL` and `MODEL_API_KEY` because it
runs M6's live golden eval. **Never print credentials.**

New dependency this milestone: `psycopg_pool==3.2.6`. Run
`.venv\Scripts\python.exe -m pip install -r requirements.txt` before anything.

## Task under review

**M7-store-hardening** — "Concurrent writers to one run all commit, event
sequence numbers come from the database, persistence stops blocking the event
loop, and a schema change can reach an existing database — so Phase 2 fan-out
lands on a store that survives it."

State: `active`, both gates `pass`, `independent-review` not yet run.

### Why this milestone exists

Phase 0 was approved and complete. Phase 2 (orchestrator, DAG, parallel
subagents) was the next milestone, and three Phase 0 assumptions were correct
only because one process owned one run. Rather than discover them mid-
orchestrator, they were measured first. **Every requirement here was written
against a reproduced failure, not a suspected one.** The measurements are in
`.genesis/project.json` as `KNOWLEDGE-e52fb4dc` and `KNOWLEDGE-010d12d3`.

### Requirements it claims to satisfy

- **FR-17**: a versioned migration path; idempotent; records what ran.
- **FR-18**: `run_events.sequence_no` assigned from the stored maximum inside
  the insert, never from an in-process counter.
- **FR-19**: concurrent appends to one run are **available**, not merely safe.
- **FR-20**: persistence does not block the event loop; pooled connections.
- **FR-21**: a run may record the run that spawned it.
- **NFR-8**: worst event-loop stall under 50 ms and wall time within 3x the
  same workload in memory, with several runs executing concurrently.
- **AC-11..AC-15**: the acceptance criteria for each of the above.

Read them in `SPEC.md` under "Phase 2 readiness". Each AC states **what failed
before the fix**, so you can reproduce the failure before trusting the repair.

### Files in scope

- `agentsdk/migrate.py` (118 lines, new) and `agentsdk/migrations/*.sql` (new).
- `agentsdk/postgres.py` — the pool, the advisory lock, DB-assigned event
  sequence numbers, the `UUID` column guard, the parent-tenancy check.
- `agentsdk/loop.py` and `agentsdk/api.py` — store calls moved to a worker
  thread.
- `tests/test_phase2_readiness.py` (800 lines, new — the gate).
- One line added to `tests/test_persistence.py`; see "the one edited test".

M1–M6 are approved, so a defect you find elsewhere is a regression in an
approved milestone and is still a defect.

## What to do

1. Read `SPEC.md`'s "Phase 2 readiness" sections, then the decisions,
   invariants, knowledge and limitations in `.genesis/project.json`.
2. Re-run both gates:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_phase2_readiness.py -q   # 19
   .venv\Scripts\python.exe -m pytest -q                                  # 442
   ```
   Per file: `test_agent_loop 77` + `test_golden_eval 15` +
   `test_model_client 151` + `test_persistence 80` +
   `test_phase2_readiness 19` + `test_primitives 74` + `test_tool_executor 26`
   = **442**. A different number is itself a finding.
3. **Reproduce the four original failures before trusting any fix.** Each is a
   single-line revert:
   - Remove the two `_serialise_writers(...)` calls in `postgres.py` → the
     concurrency tests must go red.
   - Restore `sequence_no=len(self._buffer) + 1` and the old INSERT in
     `emit` → the sink tests must go red.
   - Remove the `asyncio.to_thread` wrappers in `loop.py` → the NFR-8 tests
     must go red (see the caveat about WHICH one, below).
   Restore every file in a `finally` and verify SHA-256. **A mutant that
   errors out did not run**: this project has produced three meaningless
   matrices — a SQL error counted as a kill, an unrecognised `--timeout` flag
   that made pytest exit 4 every time, and canary debris from one mutation
   failing a later one.
4. **Note on encoding**: source files are CRLF. A multi-line mutation anchor
   written with `\n` silently fails to match, and the author's own matrix once
   reported a mutant as NOT-APPLIED for exactly this. Check your mutation
   actually landed.

## Attack these first

- **The advisory lock is the least conventional decision here.** FR-19 is
  solved with `pg_advisory_xact_lock(hashtext(run_id), space)` rather than a
  retry loop. The reasoning is in the docstring: N simultaneous writers need N
  rounds of retry, so any cap small enough to be safe is too small to help.
  **Attack the lock instead**: what happens when a run's writers span two
  processes? When `hashtext` collides across run ids? Is there a path that
  writes a sequence WITHOUT taking the lock? Does anything hold the lock across
  a network call?
- **`asyncio.to_thread` is not "non-blocking I/O".** FR-20 says store calls
  "use non-blocking I/O and a connection pool". What was built moves blocking
  I/O to a worker thread; the loop is unblocked but the I/O is still blocking.
  The author judged that making `SessionStore`/`EventSink` async would REPLACE
  a contract NFR-7 says to extend, and measured the blast radius first — 94
  call sites, 38 in the suite M5 took nine rounds to approve. **Judge whether
  that satisfies FR-20 as written.** Of everything here it is the finding most
  likely to be right.
- **Thread-safety of the sync stores, now that they are called from threads.**
  `PostgresSessionStore` and `PostgresEventStore` were only ever called from
  one coroutine at a time. `PostgresEventStore._buffer` is a list appended from
  a worker thread and read by `events()`. Is every shared structure safe? Is
  the pool used correctly across threads?
- **NFR-8's tests are timing tests, and timing tests on a gate are a liability.**
  They assert on the MEDIAN of three attempts, which was NOT the first design:
  a single sample failed at 50.5 ms against a 50 ms bound with no code change,
  purely from what else the suite had run first. Judge whether the median is
  robust measurement or a bound quietly widened. Run them on a loaded machine.
  Can you make them pass with the fix reverted, or fail with it in place?
- **AC-14's fan-out.** The criterion asks for six concurrent runs. The author
  found that the connection pool ALONE passes six runs (16 ms) while still
  stalling 79 ms at 24 — so the criterion as written would have certified a
  store that still blocks where Phase 2 meets it. A 24-run test was added. Then
  the detection rates measured inside the suite came out REVERSED from the
  standalone probe (six runs caught it 5/5, twenty-four caught it 1/5) and the
  author has no confirmed explanation, which is recorded in the docstring
  rather than explained away. **This is an open question, not a closed one.**
- **The parent-tenancy check.** FR-21 refuses a parent outside the child's
  tenant, in the same statement as the insert. Can you defeat it? Does the
  `%s::uuid IS NULL` branch open a hole? What about a parent that exists in the
  right tenant but is itself a child of another tenant's run?
- **Migrations.** `apply_schema` now runs `schema.sql` and then migrations.
  What happens if two processes call it simultaneously? If a migration fails
  halfway? If someone edits an already-applied migration file? That last one is
  NOT handled — there is no checksum on applied migrations — and whether that
  matters at this profile is your call.

## The one edited test

`tests/test_persistence.py` gained one entry. M5's
`test_every_value_start_run_writes_is_actually_REFUSED_when_unfit` reads
`inspect.signature(PostgresRunStore.start_run)` and requires every writable
parameter to have an unfit value exercised, so `parent_run_id` failed it by
construction. Adding `"parent_run_id": "not-a-uuid"` is the test working as
designed rather than being worked around — but **check that reading**, because
"the test told me to" is exactly what someone weakening a test would say.

Every other one of the 423 pre-existing tests is unchanged. `git diff` against
`e5e2c00` (the last commit before M7) will show you.

## Declared limitations — known, recorded, NOT findings

- No retention policy or partitioning; `messages` and `run_events` still grow
  unbounded. Real, deployment-bound, deliberately out of M7's scope.
- Provider neutrality remains half-proven: both AC-9 providers are reached
  through one gateway speaking one wire format. Phase 0/1's second wire format
  is **deferred by choice**, recorded in `SPEC.md`'s open questions with the
  evidence behind the decision.
- `Usage` cannot represent cached tokens, and `Message.content` is a single
  string against Anthropic's content blocks. Both additive, both recorded.
- The four older `independent-review` gates (SPEC-1, M3, M4, M5) compute as
  `stale` because the repo content hash moved after those reviews. That is the
  hash working, not damage.
- About 994 `runs` rows have no manifest: development data predating the M5
  fix. Roughly 260 persisted tool results carry a null `source_uri_or_hash`
  from earlier mutation runs. Neither is produced by current code — verify by
  running the suite and querying only rows newer than the moment you started.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real command output for anything
  you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. If
  your only findings are latent, out-of-scope or cosmetic, approve and record
  them as caveats rather than blocking.
- Distinguish a defect from a gate blind spot where the code is correct — say
  which you found.
- Leave the database as you found it, and say what you removed. Probe rows in
  this milestone use a `SYN-` tenant prefix and throwaway schemas are named
  `m7_*`; both should be zero when you finish.

## Recording your decision

Use **single quotes** around `--reason`. An L4 reviewer lost a clause of their
M6 verdict to backtick command substitution doing exactly this.

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
