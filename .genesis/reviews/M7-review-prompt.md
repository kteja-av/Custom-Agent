You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** This is M7's third round. Across M5, M6 and M7, nearly
every defect was found in a region the previous reviewer had not examined;
reviewer rotation, not effort, is what moved them. If you have reviewed this
project before, say so and ask for a different session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

Needs a real database (`DATABASE_URL` in `.env`); the regression gate also needs
`BASE_URL` and `MODEL_API_KEY` for M6's live golden eval. **Never print
credentials.** First: `.venv\Scripts\python.exe -m pip install -r requirements.txt`.

## Task under review

**M7-store-hardening**: "Concurrent writers to one run all commit, event
sequence numbers come from the database, persistence stops blocking the event
loop, and a schema change can reach an existing database, so Phase 2 fan-out
lands on a store that survives it."

Rounds 1 and 2 **rejected**. Both executable gates pass. Requirements FR-17..FR-21,
NFR-8, AC-11..AC-15 are in `SPEC.md` under "Phase 2 readiness".

## Read the recorded verdicts, not summaries of them

Round 2 was rejected partly for a round-1 defect the author never fixed. Round
1's recorded reason named four defects and six caveats; the repair worked from
a shorter summary that named three, and the author then reused the label "D4"
for a different, self-found issue, which erased the reviewer's D4 from every
later document. Both full reasons are in `.genesis/project.json` under
`controls` (search `M7-store-hardening` with action `reject`). Read them. The
ledger below maps every finding in both to a disposition; **a finding missing
from the ledger, or a disposition that does not match the code, is itself a
finding.**

## Ledger: every finding from rounds 1 and 2

Labels are the reviewers' own. Author-found issues are labelled `A` and never
share a reviewer's label.

| id | finding (from the recorded reason) | disposition | evidence |
|---|---|---|---|
| R1-D1 | RunStarted, ToolCalled and the terminal event written on the event loop | fixed in round 2, verified closed by round 2 | thread tests, R2-R4 mutants |
| R1-D2 | NFR-8 timing gate unreliable in both directions | fixed in round 2, verified closed | untimed detectors; 24-run test removed |
| R1-D3 | RunConfig did not validate parent_run_id; test misnamed | fixed in round 2, verified closed | R5/R6 mutants |
| **R1-D4 = R2-B** | apply_schema ran schema.sql outside the advisory lock; 8 workers on an empty DB lost most | **dropped in round 2; fixed now** | baseline runs inside the lock; `test_eight_workers_initialising_an_empty_database_all_succeed` (red before: 7 of 8 failed) |
| R1-C1 | test_golden_eval.py edited without disclosure | disclosed in round 2 | `git diff --stat e5e2c00 -- tests/` |
| R1-C2 | SYN-why probe debris left in the DB | removed in round 2 | |
| **R1-C3** | no decision recorded for psycopg_pool or the advisory lock | **unaddressed in round 2; recorded now** | DECISION-0c58b3bf, DECISION-0a2105b4, plus DECISION-6b62d1f5 (offload), DECISION-01de1095 (migrations), DECISION-5582fa15 (UUID) |
| **R1-C4** | AC-11 and KNOWLEDGE-010d12d3 say FR-18 needs a column; numbering starts at 0002 | **unaddressed in round 2; corrected now, one part is the owner's** | KNOWLEDGE-a3ee55b3; `migrate.py` docstring names schema.sql baseline 0001. AC-11's wording in the approved spec is left for the owner |
| **R1-C5** | no checksum on applied migrations | **unaddressed; implemented now** | newline-normalised SHA-256; edited-migration and pre-checksum tests |
| **R1-C6** | all pending migrations committed in ONE transaction; docstring said one each | **unaddressed; fixed now** | one transaction per migration; `test_a_failed_migration_leaves_the_ones_before_it_applied` |
| R2-A | pool hands out connections the server already closed; a restart fails a batch; regression vs e5e2c00 | fixed | `check=ConnectionPool.check_connection`; `test_the_pool_replaces_connections_the_server_has_closed` (red before: 3 of 8 runs completed) |
| R2-N1 | ToolCalled for a FAILED tool written on the loop survived the full suite | closed | spy now watches pool checkouts, and a path hits every kind of tool failure |
| R2-N2 | an on-loop store read outside the five spied names passed | closed | spy is on the pool, not method names; premise test |
| R2-N3 | dropping the parent project comparison passed | closed | `test_a_parent_in_another_project_of_the_same_tenant_is_refused` |
| R2-N4 | removing the migration lock passed the gate | closed | concurrent upgrade and empty-DB tests |
| R2-C1 | AC-14 is an acceptance measurement, not a detector | accepted by round 2; **SPEC amendment is the owner's decision, pending** | |
| R2-C2 | Persistence.postgres() does blocking DDL on the caller thread (1.54 s behind an open writer) | declared | `Persistence.postgres` docstring; DECISION-01de1095; `create_schema=False` |
| R2-C3 | uuid.UUID (what get_run returns) refused as TypeError | fixed | `test_a_uuid_object_is_accepted_wherever_a_run_id_is` |
| R2-C4 | live eval `attempts == 1` makes the regression gate intermittently red on gateway health | **owner's decision, pending** | Genesis gates cannot be edited after `task add` (`task set` has no `--gate`) |
| R2-note | round-2 prompt said source files are CRLF | corrected: HEAD is LF, the working copy was mixed by the author's patch scripts | KNOWLEDGE-0bc27060; step 4 below |
| A1 | (labelled "D4" in the round-2 prompt) UUID guard accepted `urn:uuid:`, which the column refuses | fixed in round 2 | differential test |
| A2 | this round's first N3 mutant "killed" 16 tests via an untyped-parameter SQL error | re-run as valid SQL (`%s::text IS NOT NULL`): only the project test fails | |
| A3 | test cleanup leaked a parent with no manifest plus its child when a child existed: `Run.__exit__` hit the self-referencing foreign key after deleting the manifest | fixed: removes the run and its descendants in one statement; the leaked 2 runs and 1 manifest deleted; N3 re-run leaves no debris | |

## What changed in the code

- `agentsdk/postgres.py`: pool built with `check=ConnectionPool.check_connection`.
  `apply_schema` now delegates entirely to `apply_migrations(dsn,
  baseline=SCHEMA_PATH)`; this module opens no connection outside the pool.
  `column_rejection_reason` accepts `uuid.UUID` for UUID columns.
- `agentsdk/migrate.py`: rewritten around one session-level advisory lock
  covering the baseline and every migration; one transaction per migration;
  checksums verified before anything applies, trusted on first sight when a row
  predates them; `applied_versions` is read-only (it used to create the
  bookkeeping table outside any lock).
- `agentsdk/api.py`: `RunConfig.parent_run_id` accepts `uuid.UUID` and
  normalises to the canonical string.
- `agentsdk/persistence.py`: `Persistence.postgres` documents its construction
  cost.
- `tests/test_phase2_readiness.py`: nine new tests, the pool-checkout spy
  replacing the method-name spy, and cleanup that survives a leaked child.

**No other pre-existing test file changed this round.** Across all of M7, two
did: `test_golden_eval.py` (12 lines, `first_error`) and `test_persistence.py`
(7 lines, one unfit value). Verify with `git diff --stat e5e2c00 -- tests/`.

## What to do

1. Read both recorded reasons, the ledger, and the decisions and knowledge it cites.
2. Re-run both gates:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_phase2_readiness.py -q   # 31
   .venv\Scripts\python.exe -m pytest -q                                  # 454
   ```
   Per file: `test_agent_loop 77` + `test_golden_eval 15` +
   `test_model_client 151` + `test_persistence 80` +
   `test_phase2_readiness 31` + `test_primitives 74` + `test_tool_executor 26`
   = **454**. A different number is itself a finding.
3. **Mutation-test.** The author's matrix, 11 of 11 killed against the readiness
   file, none errored, each restored with SHA-256 verified:
   ```
   A   pool stops validating on checkout            -> dead-pool test
   B   baseline runs outside the lock again         -> empty-DB concurrency test
   N4  migration lock removed                       -> empty-DB and upgrade concurrency tests
   C   checksum mismatch no longer refused          -> edited-migration test
   N1  ToolCalled for a failed tool on the loop     -> thread test
   N2  on-loop get_run through an unspied method    -> thread test
   N3  parent check drops project (valid SQL)       -> other-project test only
   U   uuid.UUID refused again                      -> uuid-object test
   R2-R4 round-2 offload mutants                    -> thread test (R3 also lock-hold)
   ```
   Re-run them and invent better ones. **A mutant that errors out did not run,
   and a kill that fails sixteen unrelated tests is probably a SQL error, not a
   kill**; the author made exactly that mistake with N3 this round.
4. **Line endings: the repository is LF; working copies vary.** Every file in
   HEAD is LF. The author's working copy was mixed because the Python patch
   scripts rewrote files with `write_text`, which writes CRLF on Windows, so
   exactly the files they touched came out CRLF. Git normalises on commit, so
   diffs carry no line-ending noise. Your checkout may differ from the author's
   and from the previous reviewer's, which is how round 2's prompt called the
   files CRLF while that reviewer found LF lines. **Normalise newlines before
   matching an anchor, write back in the file's own convention, and confirm the
   mutation landed.**

## Attack these first

- **The spy's premise is a substring check.**
  `test_stores_open_no_connections_of_their_own` asserts `postgres.py` source
  does not contain `psycopg.connect(`. An alias (`import psycopg as pg`,
  `from psycopg import connect`) evades it; mutant B used exactly such an alias
  and was killed by the concurrency test, not by the premise test. Is the pool
  spy's totality actually defended?
- **The pool check itself.** `check_connection` runs on every checkout, on a
  worker thread. What does it do against a half-open TCP connection rather than
  a cleanly terminated backend? Does the pool's 30 s `timeout` bound it?
- **The migration lock is now session-level and spans the baseline.** A worker
  holding it while its baseline DDL waits on locks held by an open writer
  stalls every other starting worker. The unlock is swallowed if it fails.
  Reason about both.
- **Checksums trust on first sight.** An applied migration edited before its
  checksum was first recorded is accepted, and a deleted applied migration is
  not detected. Latent or reachable?
- **The dead-pool test** kills backends by `application_name`. Could it pass
  without the fix on a machine where the pool had no idle connections?
  (It asserts at least one was killed.)
- **The concurrency tests use threads, not processes.** They reproduced the
  race before the fix (7 of 8 failed). Are they faithful to multi-process
  startup?

## Declared limitations: known, recorded, NOT findings

- No retention or partitioning; `messages` and `run_events` grow unbounded.
- Provider neutrality half-proven; Phase 0/1 deferred by choice (`SPEC.md`).
- `Usage` cannot represent cached tokens; `Message.content` is a single string.
- The UUID guard refuses non-canonical strings a uuid column accepts (deliberate).
- A connection that dies during a write fails that write, with no retry
  (deliberate: a retried MAX + 1 append could duplicate).
- Checksums: trust on first sight for pre-checksum rows; deleted migrations undetected.
- `Persistence.postgres()` blocks on DDL and the migration lock; build it once.
- Older `independent-review` gates (SPEC-1, M3, M4, M5) compute `stale` because
  the repo hash moved after them.
- About 994 runs without a manifest (pre-M5 data); roughly 260 tool results with
  null `source_uri_or_hash` from earlier mutation runs. Neither produced by current code.

**Owner decisions pending, not findings:** amending AC-14 (untimed detection) and
AC-11 (FR-18 needs no column), and whether the live golden eval belongs inside
M7's regression gate.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Distinguish a defect from a gate blind spot over correct code.
- Leave the database as you found it and say what you removed. Probe rows use a
  `SYN-` tenant prefix and throwaway schemas are named `m7_*`; both are zero now,
  and manifest-less runs are 994.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes out of it.

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
