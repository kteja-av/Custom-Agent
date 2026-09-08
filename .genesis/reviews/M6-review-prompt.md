You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** M5 took nine rounds and M6 is on its third. Every defect
but one was found in a region the previous reviewer had not examined, and
reviewer rotation — not reviewer effort — is what moved them. If you have
reviewed this project before, say so and ask for a different session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

**This milestone needs a real database AND the live gateway.** `DATABASE_URL`,
`BASE_URL` and `MODEL_API_KEY` are in `.env`, loaded by the test module.
**Never print credentials** — see the incident below, which is a reason to be
more careful here, not less.

## Task under review

**M6-golden-eval** — "The golden eval passes end to end against the live
gateway on two upstream providers, exercising the permission-denial and
validation-failure paths and leaking no credential into any row or event."

State: `retry`, `unit:pass`, `independent-review:pending`. This is the LAST
milestone in Phase 0.

### Requirements it claims to satisfy

- **AC-1**: the eval runs to `completed` — an agent with one registered `echo`
  tool is asked to call it three times with different inputs and summarise.
- **AC-2**: in that same run, a tool outside the allowlist produces
  `Failed(ToolPermissionDenied)`, is surfaced to the model as an error tool
  result, and the run still reaches `completed`.
- **AC-3**: in that same run, a malformed argument produces
  `Failed(ToolValidationError)` at the validate step, and the tool
  implementation is **provably never invoked**.
- **AC-4**: every persisted `ToolResult` carries a non-null, fully populated
  `ContentProvenance`.
- **AC-9**: the same eval passes unchanged against `openai.gpt-4o-mini` and
  `bedrock.anthropic.claude-haiku-4-5`, with no source change between runs.
- **AC-10**: no persisted row and no event payload anywhere in the database
  contains the value of `MODEL_API_KEY`.
- **NFR-3**: a completed run reconstructs from `runs` + `messages` +
  `run_events` together.
- **NFR-4**: no credential reaches model context, a persisted row, or a
  `RunEvent` payload.
- **NFR-7**: Phase 0 interfaces are chosen so Phases 2–6 add fields and
  implementations rather than replace contracts.

### Files in scope

- `tests/test_golden_eval.py` (879 lines, the milestone itself).
- `agentsdk/executor.py` and `agentsdk/primitives.py` — one round-2 defect was
  in the SDK, not the gate: see below.
- `tests/test_model_client.py` — one backstop test added in round 2.

M1–M5 are approved, so a defect you find elsewhere in `agentsdk/` is a
regression in an approved milestone and is still a defect.

## Two rounds have already been rejected. Read this before you start.

Reporting a defect that is already fixed costs you a round. Reporting the
**same shape** somewhere nobody has looked is exactly what has worked twice.

### Round 1 — rejected, both defects closed

1. **AC-3 was not exercised live.** The author argued it could not be, citing a
   measurement: asked to call `echo` with the number `42`, `gpt-4o-mini` sends
   `42` and trips validation while `claude-haiku-4-5` coerces it to `"42"`. The
   measurement was real; the conclusion generalised from one shape. The
   reviewer found a deterministic trigger — a `maxLength: 8` constraint against
   a 36-character input the task supplies — and it fails on both providers.
   AC-3 is now asserted live. Round 2 confirmed 60 live executions with no miss.
2. **AC-10's scan enumerated columns.** A canary in `run_events.tool_call_id`
   survived 421 green tests. The scan is now dynamic over
   `information_schema` base tables and whole-row (`{table}::text LIKE`). Round
   2 confirmed it by writing a canary into five previously invisible columns
   one at a time, including all of `model_registry`.

Round 1 also carried a synthesis worth more than either defect: **four
assertions each asserted one step removed from the claim they underwrote.**
All four were repaired.

### Round 2 — rejected, three defects, all now closed

1. **AC-2's "surfaced to the model" was still not asserted on the live run.**
   Mutating the adapter so every error tool result reached the wire as the
   neutral word `done` survived the full suite, 421 passed. Reproduced before
   being believed. Now: the live test asserts the exact JSON-encoded content of
   **every** error result the run stored appears in the recorded request bytes,
   and `tests/test_model_client.py` gained the backstop that never existed —
   nothing in the repository had ever constructed an `is_error` `ToolResult`
   and checked what the adapter did with it.
2. **AC-4's "fully populated" was asserted over four of five fields — the four
   that were populated.** This one was a real SDK defect, not only a gate one:
   `ToolExecutor._failed` called `ContentProvenance.internal_tool()` with no
   `source_uri_or_hash`, so FR-2's fifth field was null on every error result
   ever written (5,951 rows in this database). Fixed in the SDK with
   `ContentProvenance.executor_error(error_type)`, and the test now reads the
   field names off `dataclasses.fields(ContentProvenance)`.
   **Judge the value chosen.** A failed call's content is the executor's
   rendering of the error, never anything the tool returned — on the validation
   and permission paths the tool is never reached, and on `ToolNotFound` no tool
   exists — so recording the tool's schema hash would claim the tool produced
   text it never produced. The value is `urn:agentsdk:tool-error:<ErrorType>`.
   If you think AC-4 demands the tool's identity instead, say so.
3. **AC-9's "unchanged" test could not fail.** It rebuilt its own specs from
   `GOLDEN_SPEC` and never observed the live half; making the live half
   secretly diverge per provider left it green. The live runs are now recorded
   in a `LiveRun` record and both tests observe the same runs.

Round 2's two caveats were also addressed, and both are new surface for you:

- **The bedrock flake (~3%).** `_execute_live` now retries **once**, and only
  when the run failed at the model boundary. Attack this: the retry could mask
  a real intermittent defect, and `assert run.attempts == 1` is the only thing
  standing between a sick gateway and a silent pass. The first version of the
  condition matched the string `"ModelError"` and — measured against a closed
  port — never fired at all, because an unreachable gateway reports
  `ModelProviderUnavailable`. It is now derived from `ModelError.__subclasses__()`
  transitively, and pinned by `test_the_live_retry_recognises_the_whole_model_error_family`.
  Is there a failure inside that family which should NOT be retried?

  **Known blind spot, stated rather than hidden**: the retry's EXECUTION is
  covered by no test, because exercising it needs a sick gateway the suite does
  not simulate. Only its condition is pinned. A mutant that makes the retry
  loop forever, or never fire, survives a healthy-gateway run and is equivalent
  under normal conditions.
- **AC-9 proved nothing about two upstreams.** `trace["run"]["model_id"]` is
  only what was asked for, and the gateway echoes the alias back verbatim
  (measured: both come back exactly as sent, so the response's own `model`
  field is worthless here). The test now compares the **response envelope
  shape**: OpenAI's carries `system_fingerprint`, `service_tier` and an
  OpenAI-format completion id; Bedrock's carries neither and a UUID. This is
  evidence, not proof, and SPEC.md's wire-format limitation stands. Judge
  whether it is worth having, and whether it will flake.

## What to do

1. Read `SPEC.md`, `tests/test_golden_eval.py`, and the decisions, invariants,
   knowledge and limitations in `.genesis/project.json`.
2. Re-run the gate:
   ```bash
   .venv\Scripts\python.exe -m pytest -q                             # 423
   .venv\Scripts\python.exe -m pytest tests/test_golden_eval.py -q   # 15
   ```
   Per file, so a mismatch is locatable rather than merely alarming:
   `test_agent_loop 77` + `test_golden_eval 15` + `test_model_client 151` +
   `test_persistence 80` + `test_primitives 74` + `test_tool_executor 26`
   = **423**. A different number is itself a finding. (It was 421 in round 2;
   the two added tests are the adapter backstop and the retry-condition test.)
3. **Mutation-test.** Round 2's matrix and the repairs' own:
   ```
   permission check always allows      allowlist allows everything
   argument validation skipped         denied tool still executed
   provenance dropped on write         manifest never written
   tool results dropped on write       api key copied into a row
   output_schema populated by loop     summary never produced
   echo called with the same input     ToolCalled events not emitted
   error results reach the model as "done"
   error results dropped from the wire
   failure provenance left null / naming the tool that never ran
   success provenance drops the schema hash
   the live half diverges per provider (spec, tools, or task)
   the retry condition reverts to matching the name "ModelError"
   ```
   The author ran the last six plus two variants and killed 8/8, each restored
   in a `finally` with SHA-256 verified. **A mutant that errors out did not
   run.** This project has produced three meaningless matrices: a SQL error
   counted as a kill, an unrecognised `--timeout` flag that made pytest exit 4
   every time, and canary debris from one mutation failing a later one. Confirm
   your baseline is green before trusting any matrix.

   **Note on file encoding**: source files use CRLF. A multi-line anchor
   written with `\n` will silently fail to match — the author's own matrix
   reported a mutant as NOT-APPLIED for this reason. Check that your mutation
   actually landed.

## Attack these first

- **The shape that has now recurred twice**: an assertion one step removed from
  its claim. Round 1 fixed four, round 2 found three more in places nobody had
  converted. Go looking for the ones still uncoverted rather than re-checking
  the seven.
- **`test_the_later_phase_seams_exist_and_are_inert` is the weakest thing left
  in the file** and the author has not repaired it. `assert ApprovalRequired
  and InputRequired and Pending` is a tautology, and the rest asserts field
  names exist. `KNOWLEDGE-41611bbf`: coverage tests ask whether a check runs;
  only fitness tests can fail. Round 2 also showed `M14` — an allowlist checker
  that reads `principal_context.scopes` — survives the whole suite.
- **The scripted half**: does any assertion pass because the script hands it the
  answer? Round 2's `M12` showed the scripted half cannot tell whether tool
  results reach the model at all.
- **The live half is a network dependency inside a gate.** Run it several
  times. With the retry now in place, does a slow or rate-limiting gateway
  produce a silent pass? Does a genuinely broken SDK get retried into green?
- **AC-10.** While mutation-testing this assertion in round 1, the author
  copied `MODEL_API_KEY` into `runs.agent_spec_id` to check the test would
  notice. It did — but the five rows outlived the mutation, and a real
  credential sat in the database until the next gate run found it. It was
  redacted, then deleted at the owner's instruction. The mutation now writes
  `LEAK-CANARY-<first four chars>`. **Verify that cleanup independently.**
- Anything belonging to a later phase (subagents, MCP, sandbox, compaction,
  durable interruptions, replay) that should not exist yet.

## The database will look worse than it is — read this before reporting it

Queried across all five tables at the final commit. Every count below grows
when you run the suite; they are the shape to expect, not constants:

- **5,951 persisted error results carry `provenance.source_uri_or_hash` NULL.**
  These are historical, written before round 2's fix. Every error result
  written since carries `urn:agentsdk:tool-error:<ErrorType>`.
- **261 persisted SUCCESS results carry it NULL.** These are mutation debris,
  from the round-2 reviewer's "provenance dropped on write" mutants and the
  author's "success provenance drops the schema hash" mutant, plus a handful of
  store-level unit-test fixtures that construct a `ToolResult` by hand.
- To judge the current code rather than its history, run the suite and query
  only rows whose `runs.started_at` is after the moment you started it. The
  author did exactly that: **60 runs, 42 success results and 36 error results
  all five fields populated**, and one null — a `test_persistence` fixture that
  writes a hand-made `ToolResult` to the store to test the `is_error` round
  trip, which is not a result persisted *by a run*. Decide for yourself whether
  that satisfies AC-4.
- **994 `runs` rows have no manifest**: development data predating the M5 fix.
  Unchanged from round 2. **26 failed `p-live` runs** exist, from probes that
  point `BASE_URL` at a closed port to prove the gate goes red rather than
  green; they are not orphans.
- Whole-row scan for the key, its 8-character prefix, `LEAK-CANARY-` and
  `REVIEW-CANARY` across every column of every table: **0 hits.**

## Declared limitations — known, recorded, NOT findings

On the task record and accepted for the Phase 0 prototype profile. Report one
only if you can show it is reachable as a Phase 0 **correctness** failure:

- No connection pool; store calls are synchronous psycopg on the async loop.
- Event `sequence_no` comes from an in-process counter — breaks on resume.
- `schema.sql` has no migration path; `messages` and `run_events` grow unbounded.
- Concurrent appends to one run are safe but not available.
- JSONB normalises floats >= 1e16 back to int; at 1e308 the value read back
  compares unequal to the original.
- **Provider neutrality is only half-proven** — both AC-9 providers are reached
  through one gateway speaking one wire format, so this proves model
  agnosticism, not wire-format agnosticism. Stated in SPEC.md's risks; the
  second wire format is Phase 0/1's real exit criterion.
- SPEC.md NFR-5 says the golden eval imports no internal collaborator, and it
  imports several. NFR-5 is M4's requirement; the tension was recorded during
  M5's review and is not new here.

**Known equivalent mutant**: mutating `DenyAllPermissionChecker` leaves the eval
green, because the eval uses `AllowlistPermissionChecker`. The wider suite kills
it. Do not report it without saying why this eval should cover it.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real command output for anything
  you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. If
  your only findings are latent, out-of-scope or cosmetic, approve and record
  them as caveats rather than blocking.
- Distinguish a defect from a gate blind spot where the code is correct — say
  which you found.
- Leave the database as you found it, and say what you removed.

## Recording your decision

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M6-golden-eval --gate independent-review \
  --human "<your name>" --reason "<what you verified, and any caveat>"

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M6-golden-eval --human "<your name>" --reason "<the defect>"
```

Then report: what you checked, what you ran, what you found, and your verdict.
