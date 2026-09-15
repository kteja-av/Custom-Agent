You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** Across M5 to M12, nearly every defect was found in a region
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
requirements.txt`. M13 adds no dependency (NFR-18). **Windows Developer Mode must
be on** (D7).

**Only one test session at a time.** Every pytest session compares the store's
run ids, and from M13 its artifact ids, at its start and end (AC-44,
`tests/conftest.py`), and the development database is shared: two sessions at
once fail each other at teardown. A live run of `scripts/12_artifacts.py` writes
a run row, so it counts as a session too. Before running tests, gates or that
example, check that no other `pytest`, `genesis.mjs gate` or example process is
running. Run your own probes with `-p no:cacheprovider`: Genesis hashes
`.pytest_cache`, so a cache write can make a gate read stale with no code change.

## Task under review

**M13-artifacts**: artifacts stored per tenant and project, on an in-memory store
and on Postgres, with two new examples.

First review round. Requirements FR-53..FR-56, NFR-16, AC-42 and AC-44 (its M13
half) are in `SPEC.md` under "Phase 2, increment 1" (M13). Decisions that bear on
it: P2-D8, artifacts in Postgres, metadata and content in one table, capped at
10,485,760 bytes by default; P2-D10, migration `0006`; P2-D11, the examples; and
**DECISION-40ae2d24**, the owner's amendment of FR-56 and FR-60 on 2026-09-15:
`EXPECTED_EXAMPLES` gains 09 to 12 here, and 13 with its example in M14. The
pre-flight is KNOWLEDGE-b0e097e4. D13 applies (DECISION-2bad84bb): in-process
caller code is trusted.

**The M13 code is not committed.** HEAD is `e366ded`. Review it with:

```bash
git diff e366ded -- agentsdk tests scripts README.md SPEC.md
git status --short   # new: agentsdk/artifacts.py, agentsdk/migrations/0006_artifacts.sql,
                     #      scripts/11_run_handle.py, scripts/12_artifacts.py, tests/test_artifacts.py
```

## What changed

- `agentsdk/artifacts.py` (new):
  - `ArtifactRef`, the frozen description; `ArtifactStore`, the protocol;
    `DEFAULT_MAX_CONTENT_BYTES`.
  - `prepare_put`, the one validation both stores run before anything is written:
    content copied into exact `bytes` and capped; `mime_type`, `created_by_agent`
    and `classification` at most 255 characters and storable (`unstorable_reason`
    on the exact text); `expires_at` aware and later than the store's clock;
    provenance through `primitives.checked_provenance`; `source_run` through
    `canonical_id`. It issues the id and builds the ref.
  - `canonical_id`: an exact `uuid.UUID` as its text, or a string only in its
    canonical form. Every other form is no id, so a lookup by it is not found.
  - `InMemoryArtifactStore`: a dict of `(ArtifactRef, bytes)` shared by the stores
    `for_scope` returns; a source run admitted only by its `runs` callable
    returning exactly `True`.
- `agentsdk/postgres.py`: `PostgresArtifactStore`.
  - Every statement filtered by tenant and project, and every read and `delete`
    also by expiry against the store's clock.
  - The insert checks the source run's tenant and project in the same statement
    (`INSERT ... SELECT ... WHERE`), and a row count other than 1 is refused.
  - `get` reads through a binary cursor and verifies the SHA-256.
  - `put`, `delete` and `expire` run through `_write_then_honour_cancellation`: a
    write that has started is awaited to its end, through repeated cancels, and
    `CancelledError` is then raised. A cancelled `put` then removes its own row,
    shielded the same way.
  - `get` and `metadata` use plain `asyncio.to_thread`.
- `agentsdk/persistence.py`: `Persistence.artifact_store(tenant_id, project_id, *,
  max_content_bytes, clock)`.
- `agentsdk/primitives.py`: `checked_provenance`, `_is_member` and
  `MAX_SOURCE_CHARS`, moved from the executor, which now calls
  `checked_provenance`. The str-subclass copy is inlined; the executor keeps its
  own `_exact` for its other uses.
- `agentsdk/errors.py`: `ArtifactNotFound`, `ArtifactIntegrityError`.
- `agentsdk/migrations/0006_artifacts.sql`: the `artifacts` table, `source_run`
  referencing `runs` with no `ON DELETE`, and two indexes leading with
  `(tenant_id, project_id)`.
- `agentsdk/__init__.py`: exports `ArtifactRef`, `ArtifactStore`,
  `InMemoryArtifactStore`, `ArtifactNotFound`, `ArtifactIntegrityError`.
- `scripts/11_run_handle.py` and `scripts/12_artifacts.py` (new), listed in
  `README.md` and `scripts/README.md`.
- `SPEC.md`: FR-56 and FR-60 as amended (DECISION-40ae2d24). `README.md`: the two
  example rows and the test count.
- Tests:
  - `tests/test_artifacts.py` (new, 121 tests). 102 were written first; 19 were
    added after the first mutation run (A4).
  - Two existing test files were edited, both as the specification requires:
    `tests/conftest.py` tracks `artifacts.artifact_id` for AC-44, skipping the
    table on a database that does not have it yet; `tests/test_distribution.py`'s
    `EXPECTED_EXAMPLES` gains 09 to 12 (FR-56).

## What the author ran

1. **Pre-flight** (KNOWLEDGE-b0e097e4):
   - **Baseline:** 989 passed at `e366ded`, in 132 s.
   - **bytea:** psycopg returns `bytes`. A 10 MiB read took 142 ms through a text
     cursor and 99 ms through a binary one.
2. **Tests first.** Against the unchanged code, `tests/test_artifacts.py` plus the
   example-list test: 103 failed, 0 passed. The failures were a missing module, a
   missing `Persistence.artifact_store`, missing scripts, a missing
   `primitives.checked_provenance`, a missing migration, and missing README rows
   and exports.
3. **After the implementation:**
   - The M13 set: 103 passed.
   - Both examples offline: every check passed, exit 0.
   - Example 11 live against the gateway: every check passed, and the second run
     was cancelled with its model call in flight.
4. **Full suite:** 1 failed, 1090 passed in 227.45 s.
   - The failure was the live golden eval for `openai.gpt-4o-mini`. Its first
     model call failed at the gateway with `upstream connect error or
     disconnect/reset before headers`, and the test requires exactly one attempt.
   - Rerun alone: 2 passed in 21.10 s. Recorded as KNOWLEDGE-ea66a3c4.
5. **AC-44 red check.** A throwaway test copied into `tests/` left one artifact and
   no run. Its session failed at teardown with `the test session changed the
   store's runs: {'artifacts': {'added': 1, 'removed': 0}}`. The probe was then
   removed, and 0 probe artifacts or runs were left.
6. **First mutation run**, 39 mutants against `tests/test_artifacts.py` (E1 also
   against `tests/test_builtin_tools.py`), with `-x`. Every file was restored and
   its SHA-256 verified, the tree was restored, and no mutant text was left.
   - Result: 32 of 39 killed.
   - Survived: A18 (a non-canonical id found), P2 (a frozenset subclass of taint
     flags), P4 (a str-subclass source measured by its own report), G8 (put not
     shielded), G9 (a text cursor), G12 (Postgres `for_scope` drops the clock),
     G13 (a second cancel abandons the write).
   - What followed is A2 to A4.
7. **After A2 to A4**, the M13 set: 122 passed (121 in `tests/test_artifacts.py`
   plus the example-list test).
8. **Second mutation run**, on the final tree:
   - The mutants: the same 39, with A18's and G6's snippets updated to the
     changed code, plus A21 and G14 for the fixes.
   - Checks: every file restored and its SHA-256 verified, `tree restored: True`,
     and a scan before and after found every snippet exactly once and no mutant
     text left.
   - Result: **40 of 41 killed**; G9 survives (A5).
   ```
   A1  a bool cap accepted                              -> size cap refused at construction [True]
   A2  the size cap not enforced                        -> invalid put [memory-content one byte over the cap]
   A3  content exactly at the cap refused               -> content at exactly the cap is accepted [memory]
   A4  a caller's buffer stored uncopied                -> round trip [memory-bytearray]
   A5  mime_type form unchecked                         -> invalid put [memory-mime_type without a slash]
   A6  text limit one over                              -> invalid put [memory-mime_type over 255 characters]
   A7  unstorable text accepted                         -> invalid put [memory-mime_type holding a NUL]
   A8  a naive expiry accepted                          -> invalid put [memory-expires_at naive]
   A9  an expiry at the clock accepted                  -> invalid put [memory-expires_at equal to the clock]
   A10 provenance unchecked                             -> invalid put [memory-provenance not a ContentProvenance]
   A11 a non-canonical source run accepted              -> invalid put [postgres-source_run not a uuid]
   A12 in-memory store admits a run with no callable    -> in-memory store refuses every source run without a callable
   A13 in-memory reads ignore scope                     -> not found indistinguishably [memory-get]
   A14 in-memory reads ignore expiry                    -> not found indistinguishably [memory-get]
   A15 expired only strictly before the clock           -> not found indistinguishably [memory-get]
   A16 in-memory get skips the hash check               -> altered content refused by get [memory]
   A17 in-memory expire ignores scope                   -> expire removes exactly the expired [memory]
   A18 a non-canonical id found                         -> only the canonical form finds its artifact [memory-get]   (A4)
   A19 in-memory for_scope does not share rows          -> expire removes exactly the expired [memory]
   A20 an empty scope accepted                          -> store refuses an empty or unstorable scope [an empty tenant]
   A21 a uuid.UUID id refused                           -> a source run or an id given as a uuid object [memory]    (A2)
   P1  labels checked by equality                       -> invalid put [memory-provenance label no member of its enum]
   P2  a frozenset subclass of taint flags accepted     -> invalid put [memory-taint flags in a frozenset subclass] (A4)
   P3  source length unbounded                          -> invalid put [memory-source over 8192 characters]
   P4  a str-subclass source measured by its own report -> invalid put [memory-source that reports a false length]  (A4)
   E1  the executor stops checking provenance           -> provenance check moved from the executor to primitives
   G1  a source run of another scope accepted           -> source run must belong to the store scope [postgres]
   G2  a refused insert reported as stored              -> source run must belong to the store scope [postgres]
   G3  Postgres reads ignore scope                      -> not found indistinguishably [postgres-get]
   G4  Postgres reads ignore expiry                     -> not found indistinguishably [postgres-get]
   G5  Postgres get skips the hash check                -> altered content refused by get [postgres]
   G6  Postgres delete ignores scope                    -> not found indistinguishably [postgres-delete]
   G7  Postgres expire ignores scope                    -> expire removes exactly the expired [postgres]
   G8  Postgres put not shielded                        -> a cancelled write finishes before it is reported [put]   (A3)
   G9  content read through a text cursor               -> SURVIVED, 121 passed                                     (A5)
   G10 a read on the event loop's thread                -> no artifact store I/O on the event loop thread
   G11 Postgres delete removes an expired artifact      -> not found indistinguishably [postgres-delete]
   G12 Postgres for_scope drops the clock               -> for_scope keeps the clock and the size cap [postgres]    (A4)
   G13 a second cancel abandons the write               -> a cancelled write finishes before it is reported [put]   (A3)
   G14 a cancelled put keeps its row                    -> a cancelled write finishes before it is reported [put]   (A3)
   R1  Persistence drops the cap                        -> invalid put [postgres-content one byte over the cap]
   ```
   Not mutated, and so not claimed:
   - migration `0006` (the live database records its checksum);
   - `refuse_bad_scope`'s unstorable branch;
   - the removal after a cancelled `put` failing;
   - the in-memory callable returning a truthy non-bool.
9. **Live example 12**, against the gateway and Postgres: every check passed,
   exit 0. Afterwards there were 0 artifacts under `example-tenant`, and its one
   run row was `completed` with no artifact naming it. Example 04 leaves its run
   the same way.
10. **Gates**, on the final tree, with `--timeout 600000` and no other test
    process running. There was no separate full-suite run: the regression gate is
    the full suite.
    - **First run:** both passed, unit 121 and regression 1110, but the unit gate
      read `stale`. Its source hash was `e76b4b2a`, the regression's `883c13df`.
      A listing of every file written since the gates began, outside the
      directories the hash skips, found only `.pytest_cache/v/cache/nodeids`,
      written as the regression run ended. So the cache moved, and no code did.
    - **Rerun**, with the SHA-256 of `nodeids` taken before and after
      (`847f07d9b838b449` both times):
    ```
    $ genesis gate . M13-artifacts --timeout 600000
    M13-artifacts: passed executable gates
    unit        exit 0  2026-09-15T11:56:37Z -> 11:56:50Z  121 passed in 12.60s
    regression  exit 0  2026-09-15T11:56:50Z -> 11:59:12Z  1110 passed in 141.38s
    ```
    Both gates carry source hash `883c13df`, and `KICKOFF.md` reads `unit:pass,
    regression:pass, independent-review:pending`. That is 989 tests before M13
    plus the 121 in `tests/test_artifacts.py`; the 989 are unchanged apart from
    `EXPECTED_EXAMPLES` and AC-44's tracked tables, and AC-44's session check
    passed with all of them.

## Issues the author found during M13

Labels `A` are the author's own and never share a reviewer's label.

| id | finding | disposition |
|---|---|---|
| A1 | FR-54 names `column_rejection_reason(..., "TEXT")`; `artifacts.py` calls `unstorable_reason` | the same check: `column_rejection_reason` returns `unstorable_reason(value)` and has no TEXT branch (`postgres.py`, just after its UUID branch). Called directly, because `postgres.py` imports `artifacts.py` |
| A2 | A `source_run` given as a `uuid.UUID` was refused. `get_run` and `PostgresTrace` return run ids in that type, and `column_rejection_reason` accepts one for a UUID column: M7 round 2's shape | a test on both stores, which failed first (2 tests). `canonical_id` accepts an exact `uuid.UUID`, for `source_run` and for artifact ids |
| A3 | A cancelled Postgres `put` finished its write and raised `CancelledError`, so a row existed whose id its caller never received, and there is no list method | a test holding the checkout on its worker thread, cancelling twice, for `put`, `delete` and `expire` (3 tests; the `put` case failed first). A cancelled `put` now removes its row, shielded the same way |
| A4 | Six survivors of the first mutation run had no test: A18, P2, P4, G8, G12, G13 | 14 tests added, all passing on the code as it was, since they close gaps rather than defects: non-canonical id forms on `get`, `metadata` and `delete` (6); a frozenset-subclass taint set and a source reporting a false length as invalid puts (4); a str-subclass source stored as exact text (2); `for_scope` keeping the clock and the cap (2). G8 and G13 are covered by A3's test, whose `delete` and `expire` cases also passed first. The red run of all 19 additions: 3 failed (A2's 2, A3's `put`), 16 passed |
| A5 | G9, a text cursor in place of the binary one, survives and will keep surviving: the bytes are identical and only the read time differs | declared, not claimed |
| A6 | The regression run's one failure was the gateway, not M13 | KNOWLEDGE-ea66a3c4; rerun alone, 2 passed |
| A7 | Non-canonical string ids (uppercase, braced, `urn:uuid:`, unhyphenated) are not found on either store, although a Postgres uuid column parses them | deliberate, so neither store finds what the other would not; tested |
| A8 | FR-54 does not say whose clock sets `created_at` | the store's clock, injected or wall, never the database's `now()`, so both stores agree under an injected clock |
| A9 | `delete` of an expired artifact | raises `ArtifactNotFound` and leaves the row for `expire()`, since an expired artifact is not found by any method |

## Attack these first

- **Scope.** Every statement on both stores and every path through `for_scope`.
  Try ids of every form and type, a store for another project of the same
  tenant, and the not-found message across all five cases.
- **The source-run check.** A run of the same tenant in another project; a run
  removed between the check and the insert; the in-memory callable returning a
  truthy non-bool.
- **Cancellation.** `put`, `delete` and `expire` cancelled before the thread
  starts, while it runs, during the removal a cancelled `put` makes, and under
  `asyncio.timeout`. Also a cancel landing while the insert raises `ValueError`.
- **Validation parity.** Every field FR-54 names on both stores: a `memoryview`
  whose format is not bytes, a `bytes` subclass, an `expires_at` with a non-UTC
  offset, and an injected clock at the boundary.
- **Integrity.** Content, `content_hash` or `size` altered in the row.
- **Migration `0006`**, on a database at `0005` holding runs: idempotence, and
  the upgraded shape against a fresh one.
- **The provenance move.** No change to tool results: M10's provenance tests are
  in the regression suite unchanged.
- **The examples**, offline and live, from outside the repository, and what a
  live `12_artifacts.py` leaves behind.

## Declared limitations: known, recorded, NOT findings

- Content is read whole into memory; the per-artifact cap bounds it (`SPEC.md`
  Risks).
- `source_task` is None until plan nodes exist (FR-53). There is no update and no
  list method (FR-54).
- The cap is enforced by the store, not the table.
- `get` verifies content against `content_hash` only. Metadata altered in the row
  by other means is reported as stored.
- An injected clock must return an aware datetime; the store does not check it
  (D13).
- A `uuid.UUID` subclass is not accepted as an id: exact type only.
- If a cancelled `put`'s removal also fails, its row stays; this is not tested.
- A cancelled `get` or `metadata` leaves its worker thread to finish, and the
  result is discarded.
- G9 (A5).
- `scripts/13_telemetry.py` and `EXPECTED_EXAMPLES` gaining 13 belong to M14
  (DECISION-40ae2d24).
- KNOWLEDGE-ea66a3c4: one transient gateway failure in the live eval.
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
- Remove every run, artifact, temporary folder and git worktree you create,
  artifacts before the runs they name.
- Include every probe you ran in your report, including ones that showed nothing.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$`
out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M13-artifacts --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M13-artifacts --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found, and your verdict.
