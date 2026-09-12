You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** Across M5 to M8, nearly every defect was found in a region
the previous reviewer had not examined. If you have reviewed this project
before, say so and ask for a different session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

The unit gate needs `DATABASE_URL` from `.env` (its persisted tests fail rather
than skip without it). The regression gate also needs `BASE_URL` and
`MODEL_API_KEY`. **Never print credentials.** Install first:
`.venv\Scripts\python.exe -m pip install -r requirements.txt`

## Task under review

**M9-honest-results**: "A run cut off at the output-token limit or stopped by
the content filter ends failed and runs none of its tool calls, output limits
and reasoning effort are configurable and recorded, and every run reports and
persists its token usage and its cost in USD, or None when the model has no
price."

First review round. Requirements FR-26..FR-34, NFR-11, NFR-12 and AC-20..AC-26
are in `SPEC.md` under "Honest results (M9)", approved by the owner on
2026-09-12 with decisions D1..D4 and D9 (DECISION-87bcd821, 3b47f58d, cc5837e4,
c8734410, 59572370). Gateway facts the spec quotes: KNOWLEDGE-312441cb and
KNOWLEDGE-d625552b. M9 closes ASSUMPTION-75110765.

**The M9 code is not committed.** HEAD is `2f3a0b1` (M8 approved). The working
tree also carries uncommitted planning changes that are NOT part of M9's code:
`SPEC.md`, `agent_sdk/*.md`, `requirements.txt`, `.genesis/*`. Review the code with:

```bash
git diff 2f3a0b1 -- agentsdk tests scripts README.md
git status --short   # three new files: migration 0003, scripts/09, tests/test_honest_results.py
```

## What changed

- `agentsdk/model.py`: `ReasoningEffort`; `Usage` gains `cache_read_tokens`,
  `cache_write_tokens`, `reasoning_tokens`, coerced by walking its fields.
- `agentsdk/registry.py`: `ModelPricing` (Decimal prices, refused by name when
  invalid) replaces `cost_per_token`; `call_cost` and `add_costs`, both total.
- `agentsdk/loop.py`: `MAX_TOKENS` and `CONTENT_FILTER` end the run failed after
  the message and its `ModelCalled` event are recorded; per-call cost on the
  event; the event records usage as reported, before `after_model`.
- `agentsdk/api.py`: `max_output_tokens` and `reasoning_effort` on `AgentSpec` and
  `RunConfig`; D2 refused at the call site; the client default model id recorded;
  `RunResult.cost_usd`; cost rebuilt from events on the boundary path; accounting
  passed to a recorder only when its `finish_run` signature accepts it.
- `agentsdk/providers/openai_compatible.py`: `default_model_id`; `reasoning_effort`
  in the payload only when set; usage detail from both gateway shapes; a total
  `_error_text`; `_scrub` of every decoded string and key; a streamed send so a
  body that cannot be decoded cannot change the status's classification.
- `agentsdk/postgres.py`: `finish_run` writes six token totals and `cost_usd`
  (NULL when a column cannot hold the value); the manifest insert writes
  `max_output_tokens`, `reasoning_effort`, `pricing`; `get_run` returns them.
- `agentsdk/migrations/0003_usage_and_cost.sql` (new); `persistence.py`
  (protocol docstring and signature); `__init__.py` exports `ReasoningEffort`.
- `scripts/09_limits_and_cost.py` (new), `scripts/README.md`, `README.md`.
- `tests/test_honest_results.py` (new, 232 tests). **No existing test was edited.**

## What the author ran

1. **Tests first.** Against the pre-M9 code: 179 failed, 37 passed. The 37 were
   the two NFR-12 pre-M9 payload tests (written to pass there), the three
   stop-reason controls, the database guard, and error bodies the old adapter
   already classified correctly. The 5xx case with JSON nested past the
   recursion limit failed, reproducing ASSUMPTION-75110765.
2. **Gates**, on the final tree:
   ```
   M9-honest-results: passed executable gates
   unit        pass  2026-09-12T14:29:45Z  .venv\Scripts\python.exe -m pytest tests/test_honest_results.py -q
   regression  pass  2026-09-12T14:30:52Z  .venv\Scripts\python.exe -m pytest -q
   ```
   The evidence files record the command and exit code (0), not counts. The
   author's own runs of the same code: `tests/test_honest_results.py` 232 passed;
   full suite **694 passed**. Per file: `test_agent_loop 77` + `test_distribution 8`
   + `test_golden_eval 15` + `test_honest_results 232` + `test_model_client 151`
   + `test_persistence 80` + `test_phase2_readiness 31` + `test_primitives 74`
   + `test_tool_executor 26` = 694. The existing 462 passed unchanged before the
   new file was added.
3. **Mutation matrix, 23 of 23 killed on the final tree**, each against the unit
   gate, working tree byte-identical after every restore (SHA-256 verified):
   ```
   A  FR-26  a cut-off response is not checked                -> 69 failed
   B  D1     content_filter is not treated as unfinished       -> 34 failed
   C  FR-27  the output limit never reaches the request        -> 13 failed
   D  D2     reasoning effort without a limit is allowed       ->  3 failed
   E  FR-28  the payload always carries reasoning_effort       ->  6 failed
   F  FR-29  a double-reported cache count is summed           ->  1 failed
   G  FR-29  failure-path usage rebuilds only three fields     ->  1 failed
   H  FR-30  reasoning is priced a second time                 ->  1 failed
   I  FR-30  no pricing costs zero                             -> 23 failed
   J  FR-30  a missing price counts as free                    ->  1 failed
   K  FR-31  cost_usd is never persisted                       ->  8 failed
   L  FR-31  the Runner never passes accounting to the store   -> 15 failed
   M  FR-32  the client default model is not recorded          ->  3 failed
   N  FR-33  the pre-M9 error-body reader is restored          -> 10 failed
   O  FR-33  decoded arguments are not scrubbed                ->  2 failed
   P  FR-33  dictionary keys are not scrubbed                  ->  2 failed
   Q  NFR-12 an unset option still reaches the request         ->  3 failed
   R  FR-31  the manifest pricing is not persisted             ->  1 failed
   S  FR-29  Usage coerces only its first three fields         -> 27 failed
   T  FR-32  an unstorable default model id is recorded        ->  1 failed
   U  NFR-11 a negative completion count lowers the cost       ->  1 failed
   V  FR-33  the undecodable-body guard is removed             ->  4 failed
   W  FR-33  the pre-fix plain post() is restored              ->  4 failed
   ```
   Migration 0003 was not mutated: it is applied to the live database, and an
   edited applied migration is refused everywhere at once.
4. **Example 09 live**, against the real gateway: a 16-token limit ended
   `failed` / `max_tokens` with the partial text kept; the priced run reported
   `cost_usd=0.00000405` (23 prompt x 0.00000015 + 1 completion x 0.0000006) on
   the client default model `openai.gpt-4o-mini`; the unpriced run reported
   `None`. The gate runs examples offline only.
5. **Database.** Migration 0003 is applied to the live database with its checksum
   recorded. After the gates: no `SYN-m9` rows, no `m9_` schemas; 994
   manifest-less runs, unchanged.

## Issues the author found during M9

Labels `A` are the author's own and never share a reviewer's label.

| id | finding | disposition |
|---|---|---|
| A1 | The first red run could not collect: module-level `Usage(..., cache_read_tokens=...)` | built inside the function; the red run then failed per test |
| A2 | The cost oracle in the grid test rounded: the default 28-digit context loses a small count beside `10**30` | the oracle computes at 100 digits, the same precision as `_COST_CONTEXT`; judge whether that shared precision weakens it |
| A3 | The hostile-details test sent `inf` through `httpx.Response(json=...)`, which refuses it, so the adapter was never reached | raw bytes with an `Infinity` literal |
| A4 | The example test matched `"priced run: cost_usd=None"` inside the unpriced line | matched at line start |
| A5 | **A real FR-33 gap, found by the author's probe after the first green run.** A 429 whose `Content-Encoding` did not match its bytes came back `ModelProviderUnavailable` after one attempt: `post()` decodes inside the request, before any status is read | streamed send, guarded body read; an undecodable 2xx is a `ModelError`. Mutants V and W |
| A6 | The first test for A5 used `httpx.Response(content=...)`, which decodes in its constructor, so it was red before AND after the fix | a lazily streamed body; mutant W (the pre-fix `post()`) is the honest red evidence |
| A7 | `ModelCalled` recorded the post-`after_model` usage while the run accumulated the pre-hook usage | the event now records what the provider reported; no existing test pinned the old behaviour |
| A8 | Two test doubles in `test_agent_loop.py` implement `finish_run(scope, status)` exactly, and one asserts its own error reaches the result | accounting is passed only when the signature accepts it; those doubles record none and behave exactly as before |
| A9 | `test_persistence.py` unpacks the reconstructed manifest positionally and asserts every field non-null (KNOWLEDGE-46ac0348) | `PostgresTrace.reconstruct` is unchanged and does not return the three new manifest columns; `get_run` does return the new run columns |

## Attack these first

- **FR-26 is evaluated after `after_model`.** A hook can turn a `MAX_TOKENS`
  response into `END_TURN`, and the run completes. The hook is the intervention
  point by design. Is that honest, or a hole?
- **`after_model` HALT.** The halted call's usage and cost count toward the run,
  but no `ModelCalled` event is emitted. AC-23's per-path test covers a
  `before_model` halt only. Does the event sum falling short of the run's total
  on an `after_model` halt break AC-23?
- **Which model is priced.** Cost uses the model in `request.model_settings` after
  `before_model`; `runs.model_id` records the resolved model. A hook that swaps the
  model makes the row name one model and the cost come from another.
- **`_accepts_accounting`.** A recorder with `**kwargs` receives accounting; one
  with positional-only parameters does not. Is signature inspection the right
  compatibility rule?
- **FR-33 is a class claim.** The author found one misclassification after
  writing the tests (A5). Look for another: a body that fails mid-stream, a
  truncated chunked body, a lying `Content-Length`, a 429 from a transport
  wrapper. Separately, `_scrub` walks only dicts, lists and strings.
- **The oracle.** `expected_cost` restates FR-30's rule; the grid proves the
  implementation matches the restatement. Is anything in FR-30 left untested by
  both?
- **Example 09 teaches cost.** Read it as a new user: are the illustrative prices
  presented so nobody mistakes them for real ones?

## Declared limitations: known, recorded, NOT findings

- Cache-token semantics for Anthropic over the gateway are unverified: caching
  could not be triggered without cache markers (SPEC risk; Phase 0/1 adapter).
- Thinking blocks are not returned between turns (Phase 0/1 native adapter).
- Budgets are not enforced; cost is measured and recorded only (ADR-06, Phase 2).
- An unrecognised stop reason ends a run as before M9 (D1 covers content_filter only).
- The gate runs example 09 offline; the live run was by hand.
- Older `independent-review` gates compute `stale` because the repository hash moved.
- About 994 runs without a manifest are pre-M5 development data.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Distinguish a defect from a gate blind spot over correct code.
- Leave the database as you found it and say what you removed. The M9 tests write
  under tenant `SYN-m9` and remove it; there are none now.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M9-honest-results --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M9-honest-results --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found, and your verdict.
