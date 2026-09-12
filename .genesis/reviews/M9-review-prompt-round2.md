You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** Across M5 to M9, nearly every defect was found in a region
the previous reviewer had not examined. If you reviewed M9 round 1, say so: a
second look by the same reviewer is worth less than a first look by another.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

The unit gate needs `DATABASE_URL` from `.env`; the regression gate also needs
`BASE_URL` and `MODEL_API_KEY`. **Never print credentials.** Install first:
`.venv\Scripts\python.exe -m pip install -r requirements.txt`

## Task under review

**M9-honest-results**, **round 2**. Requirements FR-26..FR-34, NFR-11, NFR-12 and
AC-20..AC-26 are in `SPEC.md` under "Honest results (M9)". Owner decisions D1..D4
and D9: DECISION-87bcd821, 3b47f58d, cc5837e4, c8734410, 59572370.

**Round 1 was rejected** (Genesis control `eb263e57`) on two defects:

- **R1, FR-33 class not closed.** Only `httpx.DecodingError` was guarded, so any
  other failure reading a body (a lying Content-Length, a truncated chunked body,
  a reset, a body trickling past the read timeout) still reclassified the status:
  a 429 came back `ModelProviderUnavailable` or `ModelTimeout`, never retried, and
  a slow 503 was retried three times. Reproduced over a real 127.0.0.1 socket.
- **R2, FR-30 "never a partial sum".** A run failing at the Runner boundary
  rebuilt its usage and cost from `ModelCalled` events written after the
  `after_model` hook and the session append, so a failure in that window dropped
  billed calls: billed 0.036, reported and stored 0.006. The same ordering left an
  `after_model` HALT with an event cost sum short of the run total, and a mutant
  dropping the halted call's cost survived all 694 tests.

**The code is not committed.** HEAD is `2f3a0b1` (M8 approved). The working tree
also carries uncommitted planning changes that are not M9 code (`SPEC.md`,
`agent_sdk/*.md`, `requirements.txt`, `.genesis/*`). Review with:

```bash
git diff 2f3a0b1 -- agentsdk tests scripts README.md
git status --short   # new: migration 0003, scripts/09, tests/test_honest_results.py
```

## Round 2 repair ledger

R1 and R2 are the round 1 reviewer's labels. C1..C8 are the author's labels for
that reviewer's caveats; they never share a reviewer's label.

| id | finding | repair | proven by |
|---|---|---|---|
| R1 | any body-read failure other than DecodingError reclassified an error status | an error status (>= 400) decides its class before the body is touched; the body is then read for its message only, bounded to 64 KB and `error_body_timeout` (default 5 s), and ANY exception reading it means "unreadable" | `test_no_way_an_error_body_can_fail_to_arrive_changes_what_its_status_means` (8 behaviours x 429/503/400 over a raw socket, a well-formed control included); `test_an_error_body_that_raises_any_exception_...` |
| R2 | failure-path totals rebuilt from events written after the hook and the append | `RunMeter`, created by the Runner and recorded the moment `send()` returns, before any hook, store write or event; the failure path reads the meter. `ModelCalled` is emitted next, also before `after_model` | `test_a_failure_at_any_point_after_the_model_answers_loses_no_billed_call` (a failure injected at every seam of a run, one run per point, memory and Postgres); `test_a_call_halted_after_the_model_answered_...` |
| C1 | a price NUMERIC cannot hold a cost of: RunResult a number, row NULL | `ModelPricing` refuses exponent < -16383 or adjusted > 131000 | refusal and at-the-limit persisted tests |
| C2 | accounting offered by signature: a pre-M9 recorder wrapped without `functools.wraps` took it, raised, and a completed run went FAILED with both terminal events | offered only to a recorder declaring `records_accounting = True`; `PostgresRunStore` declares it | wrapped-recorder test; flag test |
| C3 | example 09's "illustrative" prices were gpt-4o-mini's real list prices, unlabelled live | round fake prices ($1/$2/$0.50 per million) and a label on the printed line | FR-34 test asserts the label |
| C4 | a before_model hook moving a run onto a priced model reported None | the run model's zero-call cost applies only when no call was made | hook-move test |
| C5 | counts clamped before the no-price check, contrary to FR-30's "non-zero count" | the check reads each class's count as reported; clamping is for arithmetic only | negative-count test; grid |
| C6 | a price of -0 accepted and rendered "-0" | normalised to 0 | test |
| C7 | the cost oracle shared the implementation's precision: a 40-digit context passed | the oracle is exact `Fraction` arithmetic; the grid adds 60-significant-digit prices | grid; mutant C7 |
| C8 | `repr(Persistence)` shows the DSN password (an M8 finding, recorded nowhere) | recorded as KNOWLEDGE-cb2f13f5; not an M9 defect, not changed here | verified with a fake DSN |

## What the author ran

1. **Tests first, again.** The round 2 tests were added to `tests/test_honest_results.py`
   before any repair. Against the round 1 code: **30 failed, 241 passed**, and each
   failure was read for its reason: the fault sweep first failed at `after_model`
   (usage 0 of 1200 billed); the halt test saw 1 ModelCalled event of 2; a 429 with
   a lying Content-Length came back `ModelProviderUnavailable`; the wrapped recorder
   raised `TypeError: unexpected keyword argument 'usage'`; and so on. The 503 rows
   for framing faults passed before the repair, as round 1 said they would: a broken
   body happens to map to the right class for a 5xx.
2. **Gates**, on the final tree, after `control retry` moved the task from
   `rejected` back to `active`:
   ```
   M9-honest-results: passed executable gates
   unit        pass  2026-09-12T15:57:03Z  .venv\Scripts\python.exe -m pytest tests/test_honest_results.py -q
   regression  pass  2026-09-12T15:58:18Z  .venv\Scripts\python.exe -m pytest -q
   ```
   The evidence files record the command and exit code, not counts. The author's
   own run of the same code: full suite **733 passed**, the 462 pre-M9 tests
   unchanged plus 271 in `tests/test_honest_results.py`. No pre-M9 test was edited.
3. **Mutation matrix, 36 of 36 killed on the final tree**, each against the unit
   gate with `--maxfail=3` (so the tests listed are the first to fail, not every
   test that would), working tree byte-identical after every restore (SHA-256):
   ```
   R2a  the account is recorded after the after_model hook and the append -> halt test, fault sweep
   R2b  the failure path forgets the account                             -> boundary-usage test, terminal-path cost test
   R2c  ModelCalled is emitted after the hook and the append again       -> halt test, fault sweep
   R1a  an error body guards DecodingError alone again                   -> raw-socket body test
   R1b  an error body is read with no total deadline                     -> raw-socket body test
   R1c  an error body is read with no size cap                           -> raw-socket body test
   W    the body is read inside the request again                        -> misdeclared-encoding test
   V    a 2xx body that cannot be decoded is reported as unavailable     -> misdeclared-encoding test
   C1   prices whose cost NUMERIC cannot hold are accepted               -> price refusal test
   C2a  accounting is offered by signature again                         -> wrapped-recorder test
   C2b  the Postgres store stops declaring accounting                    -> terminal-path cost test
   C3   the example drops its illustrative-prices label                  -> FR-34 example test
   C4   the no-call cost sticks to a run that made calls                 -> hook-move test
   C5   a negative count passes the no-price check                       -> negative-count test, grid
   C6   a price of -0 stays signed                                       -> -0 test
   C7   cost arithmetic loses digits (40-digit context)                  -> grid
   A    FR-26 a cut-off response is not checked                          -> cut-off response test
   B    D1 content_filter is not treated as unfinished                   -> cut-off response test
   C    FR-27 the output limit never reaches the request                 -> settings test
   D    D2 reasoning effort without a limit is allowed                   -> settings test
   E    FR-28 the payload always carries reasoning_effort                -> settings test
   F    FR-29 a double-reported cache count is summed                    -> gateway usage shapes test
   H    FR-30 reasoning is priced a second time                          -> grid
   I    FR-30 no pricing costs zero                                      -> terminal-path cost test, grid
   J    FR-30 a missing price counts as free                             -> negative-count test, grid
   K    FR-31 cost_usd is never persisted                                -> terminal-path cost test
   L    FR-31 the Runner never passes accounting to the store            -> terminal-path cost test
   M    FR-32 the client default model is not recorded                   -> default-model persisted test, resolution test, example
   N    FR-33 the error-body message is no longer total                  -> 429 and 5xx body tests
   O    FR-33 decoded arguments are not scrubbed                         -> 2xx credential walk
   P    FR-33 dictionary keys are not scrubbed                           -> 2xx credential walk
   Q    NFR-12 an unset option still reaches the request                 -> pre-M9 payload test, settings test
   R    FR-31 the manifest pricing is not persisted                      -> persisted manifest test
   S    FR-29 Usage coerces only its first three fields                  -> settings test (first to fail)
   T    FR-32 an unstorable default model id is recorded                 -> unusable-default-model test
   U    NFR-11 a negative count lowers the cost                          -> grid
   ```
   Round 1's mutants G (failure-path usage from events) and the signature-based
   accounting rule are gone with the code they mutated; R2b and C2a replace them.
   Migration 0003 was not mutated: it is applied to the live database, and an
   edited applied migration is refused everywhere at once.
4. **Example 09 live** against the gateway: 16-token limit -> `failed` / `max_tokens`
   with the partial text kept; priced run `cost_usd=0.000027 (illustrative prices,
   not a price list)` on the client default `openai.gpt-4o-mini` (23 x 0.000001 +
   2 x 0.000002); unpriced run `None`.
5. **Database.** No `SYN-m9` rows, no `m9_` schemas, 994 manifest-less runs,
   unchanged.

## Issues the author found in round 2

Labels continue from round 1's A1..A9.

| id | finding | disposition |
|---|---|---|
| A10 | **R2's class has a window the meter cannot close: inside `send()`.** A 2xx whose body arrives but cannot be read or parsed, or an attempt that times out after the provider processed it and is retried, may be billed with no usage ever reported. The run's cost then excludes it, with no signal. | Not repaired: only the adapter knows an attempt got a response, and reporting that is a contract change. Declared below and raised to the owner as a question. |
| A11 | Moving `ModelCalled` before `after_model` means the event records the provider's response, not a hook's replacement: a hook that rewrites a `MAX_TOKENS` response to `END_TURN` leaves an event saying `max_tokens` on a run that completed. | Intended: the audit trail keeps what the model said; the history and the FR-26 decision use the replacement. |
| A12 | FR-30's reported-count rule takes the input class as `prompt - cache_read - cache_write` before clamping, so a negative cache count enlarges the input count. | Within NFR-11; the grid oracle states the same rule independently. |
| A13 | C1 was repaired for prices only, as round 1 suggested. A token count of 2**63 or more still stores NULL beside a numeric `RunResult.cost_usd`. | Declared. Making such a count yield None would add a None condition FR-30 does not list, contrary to AC-23's "None exactly when FR-30 says so"; that is a specification change. |
| A14 | The error message now reads at most 64 KB of an error body, so a JSON error whose message sits beyond that is reported as raw text. | The class is unaffected; only the message detail changes. |
| A15 | Four of the repair's file writes were first refused by the editor as "modified since read": the mutation matrix had rewritten those files (byte-identical) after the last read. | Process only. Re-read and rewritten; the tree was verified identical after every mutant. |

## Attack these first

- **The meter is now the account.** Is there any path where the model answered and
  `meter.record` is not reached, other than A10? Any path where it is reached twice?
- **R1's class.** Round 1 found a class the author's own tests missed. Look for the
  next: HTTP/2, a proxy, a redirect to an error, a status line with no headers
  terminator, a `BaseException` from a transport. And the cost of the bound: a slow
  429 body now costs up to `error_body_timeout` per attempt.
- **Emit before the hook.** A failure writing `ModelCalled` now fails the run before
  `after_model` sees the response. Is that the right order of evidence?
- **`records_accounting`.** A recorder that declares it but keeps the old signature
  raises on the success path and the run fails. Caller bug, or trap?
- **The fault sweep.** It injects `RuntimeError` at every seam once. Is anything a
  run passes through after `send()` NOT a seam it counts?
- **A13 and A10.** Do you agree they are declared limitations and not defects under
  the approved text?

## Declared limitations: known, recorded, NOT findings

- A10: calls billed inside `send()` with no usage reported are not counted.
- A13: token counts at or above 2**63 store NULL beside a numeric result.
- C8: `repr(Persistence)` shows the DSN password (KNOWLEDGE-cb2f13f5), an M8 finding.
- Cache-token semantics for Anthropic over the gateway are unverified (SPEC risk).
- Thinking blocks are not returned between turns (Phase 0/1 native adapter).
- Budgets are not enforced; cost is measured and recorded only (ADR-06, Phase 2).
- An unrecognised stop reason ends a run as before M9 (D1 covers content_filter only).
- The gate runs example 09 offline; the live run was by hand.
- About 994 runs without a manifest are pre-M5 development data.
- Older `independent-review` gates compute `stale` because the repository hash moved.

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
