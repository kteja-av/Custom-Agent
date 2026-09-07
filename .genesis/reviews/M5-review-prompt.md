You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did not write this code and you must not trust its author's claims about it.

**Use a fresh model.** M3 took five rounds: one reviewer found exactly one defect per round for three rounds, all in the same region; swapping models found one immediately in a region the first never examined; a third found none. M4 took two rounds with a fresh reviewer each time. Reviewer rotation, not reviewer effort, is what moves these. If you have reviewed M5 before, say so and ask for a different session.

## Rounds 1-7 -- what was rejected, and what changed

A previous reviewer rejected this milestone and was right to. Their finding, and the response:

**The defect.** `start_run` wrote the `runs` row and `write_manifest` wrote the manifest on two independent connections, with `RunStarted` emitted after both. An injected transient `psycopg.OperationalError` between them committed a `runs` row with **zero** manifests -- AC-6 requires exactly one -- and no `RunStarted` event to explain it.

**The fix is structural, not careful ordering.** The manifest is now a required *argument* to `start_run`, and both rows are written in one transaction. `RunRecorder` no longer declares `write_manifest`, so no backend can offer the Runner a way to start a run without a manifest. Either both rows commit or neither does. `write_manifest` survives as a standalone method, but it can only ever *add* a manifest, so it cannot reproduce the defect.

**Six surviving mutations, all gate gaps, now closed** -- plus two more the author found by extending the reviewer's own reasoning: the round-1 reviewer flagged `history()`'s unpinned `ORDER BY`, and the same hole existed in `PostgresTrace.reconstruct` for both messages and events, which is AC-7's own claim. Tests now write rows *out of order* before asserting a read is ordered.

Round 2 confirmed that fix held under `os._exit` mid-transaction and injected failures, and rejected on a different defect:

**Round 2's defect.** `_usage_from_events` did an unguarded `int()` and runs inside `Runner.run()`'s `except` block, so a ModelClient reporting `NaN` or `Infinity` took down the total boundary *on its error path* -- violating two invariants by name and regressing a property M4's approval had verified.

**The reviewer's diagnosis mattered more than the defect.** This was the third rejection for one bug class: M3 round 3 found `int(float('inf'))` raising `OverflowError` in the adapter, that fix added `OverflowError` to that one call site, and the pattern reappeared in a new function. So the coercion moved onto the type: `Usage.__post_init__` routes every field through `agentsdk.model.token_count`, and the adapter's `_as_int` is **deleted** rather than duplicated. A `Usage` cannot hold a non-int whoever built it. Recorded as `DECISION-a2c8f342` and `INVARIANT-3c123c38`.

Round 3 confirmed both earlier fixes dead and rejected on a third defect, in a region no prior round examined.

**Round 3's defect.** `json.loads` is RFC-8259-correct: `1e400` decodes to `inf` and a `U+0000` escape to a NUL. JSONB holds neither, so the same run **completed in memory and failed against Postgres** -- breaking the contract `postgres.py` states in its own header, that the loop cannot tell which store it is talking to.

**The fix is upstream of the symptom, for the third round running.** Not in the persistence layer: silently nulling an infinity or stripping a NUL would trade a loud failure for a corrupted audit trail. The primitives now refuse what no store can hold, via one total helper (`primitives.unstorable_reason`):
- unstorable tool **arguments** are cleared and routed onto `arguments_error` -- the channel undecodable JSON already uses. Flagging alone was not enough: the assistant message carrying the call is persisted whether or not the executor runs it, so the value had to go.
- unstorable **content** raises, because content has no error channel. Both surrounding boundaries are total, and `send()` converts it to a typed `ModelError`, so a caller can tell "the model emitted something unstorable" from "the database is down".
- an unstorable tool **result** becomes an ordinary tool error, so the run continues identically on both backends.

**Scope was wider than the report.** The reviewer tested tool arguments; message content and tool results diverge the same way, and fixing it inside the OpenAI adapter would have left every other provider broken -- NFR-1's whole claim is that a new provider is a configuration change. Convergence is now verified through a **custom** `ModelClient`, not the adapter the defect was found in.

**Both named blind spots are closed.** `arguments_error` now has a round-trip test (the Phase 6 replay hazard: a dropped flag turns an undecodable call back into a valid empty-args call that step 2 waves through). And message tenancy is taken from the **run row** inside the INSERT, with the caller's scope matched in the WHERE, so a mismatch errors instead of silently filing a message under the wrong tenant.

Round 4 confirmed rounds 1 and 3 dead, and rejected on a fourth defect inside the round-3 fix itself.

**Round 4's defect.** `unstorable_reason` -- the single source of truth for what a primitive may hold -- did not check for lone UTF-16 surrogates. A truncated surrogate escape is a legal RFC-8259 decode, models emit truncated pairs, and both TEXT and JSONB refuse the result. Round 3's bug shape, unchanged, in the helper written to close it.

**The fix generalises rather than adds a case.** The helper now has two layers, the same structure as the ModelClient boundary: named checks (NUL, non-finite, depth) for a precise diagnosis, backed by attempting the serialisation the store actually performs -- `json.dumps(allow_nan=False, ensure_ascii=False).encode("utf-8")`. That catches the CLASS rather than the instance, and closed oversized integers for free. Verified against the live database in **both** directions: astral emoji, 4-byte CJK and `int(1e308)` still store fine, so it does not over-reject.

**One round-4 finding was checked and is wrong.** The report said `INVARIANT-3c123c38` (`token_count` must not swallow `BaseException`) had no test. It did: mutating it turns the full suite red. The test was named `test_base_exception_from_a_hostile_int_still_propagates`, so no `-k usage` or `-k token_count` selection could find it -- which is a real problem of its own, and it has been renamed so all three selections now go red. Verify this yourself rather than taking either account on trust.

**Found after round 4, by the author, unprompted.** Probing the helper for FALSE positives (rather than misses) turned up a live defect no review round had looked for: it capped nesting at depth 60 and called anything deeper unstorable, while Postgres stores 900-deep JSON happily. A model returning deeply nested arguments had its tool call refused over a limit that does not exist. The walk is now iterative with a visited-id set and never guesses about depth -- the serialiser decides, and agrees with Postgres exactly (both stop at ~1000). Cycles are caught by the serialiser's circular-reference detection instead of a cap.

**Known equivalent mutant.** Flipping the backstop's `allow_nan=False` to `True` leaves the suite green, because the named layer catches non-finite floats first. It is kept deliberately as overlapping defence, and documented in the code. Do not report it as a fresh blind spot without saying why the overlap should go.

Round 5 rejected on the identifier and name fields -- `ToolCall.id`, `function.name`, `provider_response_id` -- which reach the same JSONB columns the guarded fields do. Its diagnosis is the part that matters: `unstorable_reason` is total over VALUES, but its APPLICATION was an enumeration of the fields someone had thought of. Five rounds, five defects, each fix one level more general than the last.

**So the application is no longer an enumeration.** `_replace_unstorable_text` walks `dataclasses.fields()` and is called by `ToolCall`, `ToolResult`, `ContentProvenance`, `Message` and `ModelResponse`. A string field added tomorrow is covered without anyone remembering. Identifiers are REPLACED with a conspicuous marker rather than refused, so a NUL in a tool name becomes an ordinary "no such tool" error -- what happens without persistence -- instead of a run dying at the write with no record of what the model said (NFR-3). There is a test asserting the property over `dataclasses.fields()` itself, not over today's field list.

**Defect 2 (the recursion window) is fixed by erring toward refusal.** The window moves with the caller's stack, so the two verdicts cannot be made to agree exactly. `_MAX_NESTING` (half the recursion limit) refuses early: accepting-then-failing costs the audit trail, refusing costs an explicit tool error on nesting no model emits. The band between the margin and the true limit IS a deliberate false positive, and a test says so.

**All four blind spots are closed**, including the two the reviewer verified personally: `runs.status` is now asserted for every loop-path outcome (hardcoding `finish_run` to `completed` used to leave the suite green), event ids are compared emitted-to-stored, and the runs row's model/spec/max_turns plus the RunStarted payload are asserted.

**`history()` is now tenant-scoped on read** (`DECISION-e692386f`), which the reviewer raised as "worth a decision, not a fix". Both directions were arguable; NFR-2's premise settled it.

Round 6 rejected on `ToolCall.__post_init__` exempting `arguments_error` from the very walk round 5 introduced to end exemptions -- and the test asserting "the guarantee is structural, not a list of names" contained `if name == "arguments_error": continue`. The fix and its guard were written together, and the guard was shaped around the gap. Both are gone.

Worth knowing how the repair went, because it repeated round 5's mistake: the first attempt added the walk coverage AND a follow-up check on the generated reason string. Reintroducing the carve-out then left all 380 tests green, because whichever guard was deleted the other still produced a storable value -- and the second guard was unreachable anyway. It was removed, and the mutation now dies. **Two guards for the SAME property do not add defence; they remove the suite's ability to notice either being deleted.**

**All three caveats were taken, not carried.** `PostgresTrace.reconstruct`, `get_run` and `finish_run` take a `RunScope` and filter on tenancy (`DECISION-56558d52`), so AC-7's vehicle no longer answers the question the opposite way to `history()` one function over. The `<unstorable>` marker is a prefix with a per-replacement suffix, so two unstorable ids stay distinct and call-to-result correlation survives. `RunScope` refuses an unstorable `run_id`/`tenant_id`/`project_id`, and `start_run` refuses an unstorable `agent_spec_id`/`model_id` -- refusing rather than degrading, because these are configuration, not model output.

Round 7 rejected on `principal_context`, the one caller-supplied value still reaching `Jsonb()` unchecked -- and it is round 6's caveat (c) verbatim. That caveat named four fields; the repair implemented those four names, and the fifth was the defect. **A review names examples; the finding is the class.** (`KNOWLEDGE-4c167492`.)

The consequence was worse than a failed run: `start_run` raised before `RunStarted` was emitted, so the follow-up `RunFailed` hit a foreign key against a run row that never existed and was swallowed by `_safe_emit`. Round 1's defect at least left a discoverable orphan; this left nothing.

**The fix is one guard for configuration types.** `refuse_unstorable_fields` walks `dataclasses.fields()` and RAISES with the field named; `RunScope` and `PrincipalContext` use it. It also covers fields of **any shape**, not just `str` -- which is the deeper half: the replacement walk only handles string fields, and the reviewer found the gap through `PrincipalContext.scopes`, a `tuple[str, ...]` whose contents reached JSONB unchecked. `start_run` now checks every value it writes, with a test derived from its signature so a new parameter fails until it is covered (`DECISION-cc1f8297`).

**The blind spot the round-7 reviewer self-corrected on is closed.** `ToolResult`'s `skip=("content",)` was unpinned -- removing it let the field walk mark content storable before the compensating check ran, so `is_error` silently stayed False. A direct test now kills that mutation.

**One thing the review prompted that no round asked for.** The hygiene note about a DSN in a traceback led to testing NFR-4 on the connection-failure path for the first time: a wrong-password connection does not render the password into an exception message, on either `psycopg.connect` or `Persistence.postgres`. Now a test. The repo, its full git history and the scratchpad were checked for the real password: no occurrences, and `.env` is gitignored.

**Attack these first.** They are where the last two defects were, and where a fix is most likely to have introduced a new one:
- `unstorable_reason` decides what every primitive will accept, and has now been wrong twice. Find a value Postgres refuses that it passes, or one it rejects that would have stored fine. Probe the DATABASE for the answer rather than reasoning about it -- both previous holes were found that way, and the helper's verdicts are only worth what a live comparison says.
- The backstop calls `json.dumps` on every tool-call argument dict. Measured at 0.1-8ms for 10k keys, a 1MB string and a 50k-element list, with no false positives -- confirm or refute, and find a shape that is pathological.
- **Look for the next unchecked value, not the next unchecked FIELD.** Rounds 5, 6 and 7 were all the same shape: a guard applied to what someone enumerated. Ask what reaches a column, then check the guard covers all of it -- including containers, and including values that never pass through a primitive at all.
- **Look for the next exemption.** Round 6's defect was a `skip=` argument; round 5's was a field nobody listed. Grep for `skip=` and for any conditional inside a guard or its test, then check each is load-bearing by deleting it and running the suite.
- **The field walk is the new single point of failure.** It covers `str` fields on dataclasses. What about a string inside a tuple field, a nested dataclass, or a field added as `list[str]`? Find a persisted string it does not reach.
- `history()` now refuses an unbound store. Does anything legitimate need an unscoped read -- support tooling, a future replay -- and is raising the right answer there?
- **Hunt false positives, not just misses.** Four rounds hunted values the helper wrongly accepts; the depth-cap defect was one it wrongly rejected, and only a live-database comparison exposed it. Ask Postgres what it accepts and diff the two verdicts in both directions.
- **Raising in `Message.__post_init__` is the riskiest thing here.** Find a path where that raise is not contained by a total boundary, or where it fires on an error path and takes down a handler.
- `ToolCall.__post_init__` silently empties `arguments`. Is the reason always preserved, and can a caller be confused by arguments that vanish?
- The message INSERT is now a `LEFT JOIN ... GROUP BY`. Re-measure concurrency: the author saw safety unchanged but availability *improved* (0-1 of 12 losers, previously 4 of 12). Confirm or refute.
- `token_count` catches bare `Exception` and returns 0. Find a value it mishandles, or a place a provider number still reaches `int()` directly.
- Is `Usage.__post_init__` reachable on every construction path -- including `__add__`, `dataclasses.replace`, and unpickling?
- Coercing to 0 is silent. Is there a case where silently zeroing a token count is worse than failing? Judge whether the trade is right, not just whether it is implemented.
- **Does `BaseException` still pass through?** `token_count` must not swallow `KeyboardInterrupt`.
- Is the transaction genuinely atomic, or does `psycopg`'s `with conn.transaction()` inside `with psycopg.connect()` leave a window?
- 301 tests now, up from 259. Did any new test weaken an old one -- the `started` fixture now writes a manifest, which changed what two AC-6 tests assert against, and the e2e test now asserts manifest *contents*.

**Two round-2 caveats were also addressed.** The manifest-content blind spot (four mutations to what the Runner puts in the manifest all survived because the e2e test only asserted `is not None`) is closed -- all four now die. The 979 manifest-less `runs` rows were confirmed to be pre-fix development data plus rows my own mutation runs created deliberately: three clean suite runs produced 69 runs and **0** orphans. They are left in place pending the owner's decision; deleting their data to tidy a metric is not mine to make. Round 3's reviewer independently confirmed the count is static at 979.

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
Modified by the review rounds, and in scope for that reason: `agentsdk/api.py`
(persistence wiring, manifest, usage reconstruction), `agentsdk/primitives.py`
(storability), `agentsdk/model.py` (`token_count`, `Usage`, `ModelResponse`),
`agentsdk/executor.py` (unstorable tool results),
`agentsdk/providers/openai_compatible.py`, `agentsdk/__init__.py`, and
`tests/test_primitives.py`, `tests/test_model_client.py`,
`tests/test_agent_loop.py`, `tests/test_tool_executor.py`.

`api.py` belongs to approved M4 — a change that breaks a completed milestone is still a defect.

## What to do

1. Read the files, `SPEC.md`, and the decisions/invariants/knowledge in `.genesis/project.json`.
2. Re-run the gate:
   ```bash
   .venv\Scripts\python.exe -m pytest -q                             # 389
   .venv\Scripts\python.exe -m pytest tests/test_persistence.py -q   # 70
   ```
   Per file, so a mismatch is locatable rather than merely alarming:
   `test_agent_loop 75` + `test_model_client 150` + `test_persistence 70` +
   `test_primitives 70` + `test_tool_executor 24` = **389**.
   A different number is itself a finding. Round 6 lost time on a stale count
   in this document; these were regenerated after the round-6 fixes and are
   the only counts in this file.
3. **Mutation-test.** Round 1: 21 mutations, six survivors. Round 2's reviewer ran 56 and found 12 blind spots. Round 3: 12 targeted, 12 killed. Round 4: 12 targeted at the storability work, 12 killed -- one initially survived (removing the cycle-depth guard) because the outer catch absorbed the RecursionError, so the test now pins the *diagnosis* rather than mere survival. Every round's reviewer has found blind spots the author's own matrix did not; assume more exist. Invent your own -- the survivors were found by a reviewer, not by the author. Five shared one cause worth understanding: `schema.sql` uses `CREATE TABLE IF NOT EXISTS`, so mutating the file has **no effect on an already-created database** — the schema tests were proving the live database correct, not the file. A test now applies `schema.sql` into a throwaway namespace and asserts there. Re-run these and invent your own:
   ```
   sequence_no constant not computed   append without scope allowed
   bound store accepts any run         history ignores ordering
   provenance dropped on write         taint flags dropped
   source uri dropped                  tool call arguments dropped
   event sequence constant             events not persisted
   manifest never written              manifest hash ignores instructions
   manifest hash ignores tool profile  model version left null
   manifest write not atomic with run  trace ordering removed
   is_error dropped on write           principal_context not persisted
   Usage coercion removed              token_count re-narrowed to (TypeError, ValueError)
   token_count swallows BaseException  manifest tool hashes emptied
   manifest instructions replaced      manifest sdk_version faked
   NUL not counted as unstorable       non-finite not counted as unstorable
   ToolCall flags but does not clear   Message accepts unstorable content
   arguments_error dropped on write    arguments_error dropped on read
   message tenancy from the caller     missing run no longer detected
   serialisation backstop removed      backstop keeps ensure_ascii
   backstop drops the encode step      backstop is not total
   walk stops descending into lists    cycle guard removed (hangs the suite)
   depth cap reintroduced              named NUL check removed
   messages tenant nullable            run_events tenant nullable
   messages sequence not unique        manifest primary key relaxed
   tenancy index removed               run never marked finished
   usage not reconstructed
   ```
   Restore every mutated file and verify SHA-256, restoring in a `finally`.
4. Attack the work on its own terms. Worth suspicion:
   - **AC-6 atomicity, the round-1 defect.** Reproduce it against the current code: inject a failure into the manifest write and confirm no `runs` row survives. Then look for the same shape elsewhere.
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
