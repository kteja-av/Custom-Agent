You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** M5 took nine rounds. Every defect but one was found in a
region the previous reviewer had not examined, and reviewer rotation — not
reviewer effort — is what moved them. If you have reviewed this project before,
say so and ask for a different session.

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

State: `active`, `unit:pass`, `independent-review:pending`. This is the LAST
milestone in Phase 0.

### Requirements it claims to satisfy

- **AC-1**: the eval runs to `completed` — an agent with one registered `echo`
  tool is asked to call it three times with different inputs and summarise.
- **AC-2**: in that same run, a tool outside the allowlist produces
  `Failed(ToolPermissionDenied)`, is surfaced to the model as an error tool
  result, and the run still reaches `completed`.
- **AC-3**: in that same run, an `echo` call with a malformed argument produces
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

New: `tests/test_golden_eval.py` (578 lines, the whole milestone).

Everything it exercises is already approved — M1–M4, and M5 after nine rounds —
so a defect you find in `agentsdk/` is a regression in an approved milestone and
is still a defect. The eval is the artefact under review; the SDK is what it
claims to prove.

## What to do

1. Read `SPEC.md`, `tests/test_golden_eval.py`, and the decisions, invariants,
   knowledge and limitations in `.genesis/project.json`.
2. Re-run the gate:
   ```bash
   .venv\Scripts\python.exe -m pytest -q                             # 421
   .venv\Scripts\python.exe -m pytest tests/test_golden_eval.py -q   # 14
   ```
   Per file, so a mismatch is locatable rather than merely alarming:
   `test_agent_loop 77` + `test_golden_eval 14` + `test_model_client 150` +
   `test_persistence 80` + `test_primitives 74` + `test_tool_executor 26`
   = **421**. A different number is itself a finding.

   Note: commit `8db7379`'s message says "434 tests". That is wrong — the
   author miscounted; 421 is correct, corrected here rather than left for you
   to trip over.
3. **Mutation-test.** The author ran 9 and killed 8. Re-run these, and invent
   better ones:
   ```
   permission check always allows      allowlist allows everything
   argument validation skipped         denied tool still executed
   provenance dropped on write         manifest never written
   tool results dropped on write       api key copied into a row
   output_schema populated by loop     summary never produced
   echo called with the same input     ToolCalled events not emitted
   ```
   Restore every mutated file and verify SHA-256, restoring in a `finally`.
   **A mutant that errors out did not run.** The author once counted a SQL
   error as a kill, and once made an entire matrix meaningless by passing an
   unrecognised `--timeout` flag to pytest, which exits 4 and looks like a kill
   every time. Confirm your baseline is green before trusting any matrix.

## Attack these first

- **The central risk, named in SPEC.md**: "a single golden eval carrying three
  assertions may pass for the wrong reason." Each AC has its own test, but
  check the assertions actually distinguish the path from the final status.
  Would AC-2's test pass if the denial happened for the wrong reason?
- **AC-3 is deliberately NOT asserted on the live path**, and this is the
  author's most contestable decision. The reason is measured, not assumed:
  asked to call `echo` with the number 42, `openai.gpt-4o-mini` sends `42` and
  trips validation, while `bedrock.anthropic.claude-haiku-4-5` coerces it to
  `"42"` and validates cleanly — twice per provider. So AC-3 is proven on a
  scripted run instead. **Judge whether that satisfies AC-3, which says "in
  that same run".** If you think it does not, say so; of everything here it is
  the finding most likely to be right.
- **Is the scripted half proving the SDK, or proving the script?** A scripted
  `ModelClient` emits exactly the calls the assertions expect. Find an
  assertion that would pass against a broken SDK because the script hands it
  the answer.
- **The live half is a network dependency inside a gate.** Is it flaky? Run it
  several times. What happens when the gateway is slow, rate-limits, or a model
  chooses differently? A gate that fails for reasons unrelated to the code is
  its own defect — and so is one that quietly passes when the network is down.
- **AC-10, hardest.** While mutation-testing the AC-10 assertion, the author
  copied `MODEL_API_KEY` into `runs.agent_spec_id` to check the test would
  notice. It did — but the five rows outlived the mutation, and a real
  credential sat in the database until the next gate run found it. It was
  redacted, then deleted with its child rows at the owner's instruction, and
  verified zero across all four tables. The mutation now writes
  `LEAK-CANARY-<first four chars>` instead, and the harness clears its own
  canary rows. **Verify that cleanup independently.** Then ask whether the
  AC-10 test covers the columns that actually carry provider data: it
  enumerates columns per table, and an enumeration is the shape that produced
  five of M5's nine rejections.
- **NFR-7 is the weakest assertion in the file**, and the author's own matrix
  caught part of it: the seam test asserted `output_schema` *defaults* to None
  on a hand-made request, which populating it in the assembler left green. It
  now also asserts on the requests the loop really sends. Is the rest of that
  test still the "name in a list" shape? `KNOWLEDGE-41611bbf`: coverage tests
  ask whether a check runs; only fitness tests can fail.
- Anything belonging to a later phase (subagents, MCP, sandbox, compaction,
  durable interruptions, replay) that should not exist yet.

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
- About 994 `runs` rows have no manifest: development data predating the M5
  fix, plus 15 from a `manifest never written` mutation run today.

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
