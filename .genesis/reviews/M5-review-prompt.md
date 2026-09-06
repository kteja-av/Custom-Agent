You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did not write this code and you must not trust its author's claims about it.

**Use a fresh model.** M3 took five rounds: one reviewer found exactly one defect per round for three rounds, all in the same region; swapping models found one immediately in a region the first never examined; a third found none. M4 took two rounds with a fresh reviewer each time. Reviewer rotation, not reviewer effort, is what moves these. If you have reviewed M5 before, say so and ask for a different session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`; without the venv first on PATH that hits the Microsoft Store alias and crashes Node.

**This milestone needs a real database.** `DATABASE_URL` is in `.env` (loaded automatically by the test module) and points at a local PostgreSQL 16 instance owned by `<db_user>`. Never print credentials.

## Task under review

**M5-postgres** — "Postgres schema and stores persist runs, messages, run_events and exactly one execution manifest per run, every row tenant-scoped and the trace reconstructable in order."

State: `active`, `unit:pass`, `independent-review:pending`.

### Requirements it claims to satisfy

- **FR-9**: A `SessionStore` protocol with Postgres implementation exposing `append(run_id, message)` and `history(run_id)`; `sequence_no` is assigned inside the same transaction as the insert.
- **FR-10**: `RunEvent` records are emitted and persisted with the full envelope (event_id, schema_version, sequence_no, event_type, tenant/project/run ids, nullable agent/task/tool_call/attempt ids, parent_event_id, correlation_id, timestamp, payload) for the Phase 0 event set.
- **FR-11**: Exactly one `ExecutionManifest` row per run, written at start, capturing sdk_version, agent_spec hash, instructions_hash, model id/version/adapter_version, tool_spec_hashes, policy_version.
- **NFR-2**: Multi-tenant by construction. Every row in every table carries non-null, indexed `tenant_id` and `project_id`.
- **AC-5**: Every row across `runs`, `messages`, `run_events`, `execution_manifests` has non-null tenancy.
- **AC-6**: `execution_manifests` holds exactly one row for the run, every field populated.
- **AC-7**: The run's trace reconstructs in order from `runs` + `messages` + `run_events` read back from Postgres.

### Standing invariants

- Every `ToolResult` carries exactly one `ContentProvenance` — including after a database round trip.
- Every persisted row carries non-null `tenant_id` and `project_id`.
- `ToolExecutor` order is fixed: resolve → validate → permission → execute.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- Application code calls `Runner.run()` and nothing else.
- Only `AgentSDKError` subclasses may escape `ModelClient.send()`.
- `Runner.run()` and `ToolExecutor.execute()` are total boundaries.
- A boundary's error path must not itself be able to raise.

### Files in scope

New: `agentsdk/schema.sql`, `agentsdk/postgres.py`, `agentsdk/persistence.py`, `agentsdk/manifest.py`, `agentsdk/version.py`, `tests/test_persistence.py`.
Modified: `agentsdk/api.py` (persistence wiring, manifest, model-version lookup, usage reconstruction), `agentsdk/__init__.py`.

`api.py` belongs to approved M4 — a change that breaks a completed milestone is still a defect.

## What to do

1. Read the files, `SPEC.md`, and the decisions/invariants/knowledge in `.genesis/project.json`.
2. Re-run the gate:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_persistence.py -q   # expect 30
   .venv\Scripts\python.exe -m pytest -q                             # expect 259
   ```
   A different number is itself a finding.
3. **Mutation-test.** The author ran 21 mutations; six survived and were fixed. Five shared one cause worth understanding: `schema.sql` uses `CREATE TABLE IF NOT EXISTS`, so mutating the file has **no effect on an already-created database** — the schema tests were proving the live database correct, not the file. A test now applies `schema.sql` into a throwaway namespace and asserts there. Re-run these and invent your own:
   ```
   sequence_no constant not computed   append without scope allowed
   bound store accepts any run         history ignores ordering
   provenance dropped on write         taint flags dropped
   source uri dropped                  tool call arguments dropped
   event sequence constant             events not persisted
   manifest never written              manifest hash ignores instructions
   manifest hash ignores tool profile  model version left null
   messages tenant nullable            run_events tenant nullable
   messages sequence not unique        manifest primary key relaxed
   tenancy index removed               run never marked finished
   usage not reconstructed
   ```
   Restore every mutated file and verify SHA-256, restoring in a `finally`.
4. Attack the work on its own terms. Worth suspicion:
   - **Concurrency.** The author measured this before submitting: 12 barrier-synchronised writers on one run committed 8 unique contiguous rows with 0 duplicates, and 4 writers raised `UniqueViolation`. So safety holds and availability does not, which is recorded as a known limitation for Phase 2 and pinned by a test. Verify that measurement independently, and judge whether deferring the availability half is the right call or whether FR-9 demands more.
   - **Connection per operation.** Every store method opens its own `psycopg.connect`. Correct but wasteful; does it create a correctness problem (no shared transaction across the run's writes — a run row can exist with no manifest if the process dies between them)?
   - **Is the vacuous-pass guard sound?** `test_the_database_is_actually_configured` is deliberately un-skippable so a database-less run fails rather than skipping to green. Does the fixture exempt exactly that one test and no other?
   - **Credential leakage into rows.** NFR-4 says no credential reaches a persisted row. `RunEvent` payloads are written to JSONB and `_json_safe` stringifies unknown objects — can anything carrying a key reach a payload?
   - **Provenance fidelity.** Does every field survive the round trip, or only the ones the test happens to check?
   - **`_json_safe` on hostile input** — its `str(value)` fallback runs on arbitrary objects. Same class of bug as the round-4 M3 finding.
   - Anything belonging to a later phase (subagents, MCP, sandbox, compaction, durable interruptions, replay) that should not exist yet.
5. Optionally verify against the live gateway plus the database together.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- Gates are computed, never narrated. Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. If your only findings are latent, out-of-scope or cosmetic, approve and record them as caveats rather than blocking.
- Distinguish a defect from a gate blind spot where the code is correct — say which you found.

## Recording your decision

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M5-postgres --gate independent-review \
  --human "<your name>" --reason "<what you verified, and any caveat>"

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M5-postgres --human "<your name>" --reason "<the defect>"
```

Then report: what you checked, what you ran, what you found, and your verdict.
