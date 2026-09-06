> ARCHIVED. This is the round-4 prompt text, kept only as a record of how M3
> was reviewed. Its test counts and defect list are stale. M3 was APPROVED in
> round 5. Do not hand this to a reviewer.

You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did not write this code and you must not trust its author's claims about it.

**Start here — this must be a fresh reviewer.** Rounds 1, 2 and 3 were all graded by the same session, which flagged each time that it was grading repairs to its own findings. If you have reviewed M3 before, stop and say so; ask the human for a different model or a clean session. The defects least likely to be found are the ones the prior reviewer's mental model does not generate. Re-derive the requirements from `SPEC.md` rather than inheriting the framing below.

## What happened in the three prior rounds

Round 1 found seven defects. Round 2 confirmed those seven fixed, found three more. Round 3 confirmed those three fixed, found one new escape plus a structural diagnosis:

> Each fix enumerated one more exception type inside `_parse`. Three rounds, one escape each — `JSONDecodeError`/`AttributeError`, then `TypeError` (unhashable dict key), then `OverflowError` (`int(float('inf'))`). The invariant demands a total boundary, not a whitelist that grows by one member per review.

That diagnosis was accepted and acted on. `send()` now delegates to `_send()` and re-raises `AgentSDKError` untouched while wrapping **any** other `Exception` as `ModelError`, chaining the cause and redacting the message. `BaseException` — `CancelledError`, `KeyboardInterrupt`, `SystemExit` — passes through as control flow. Specific guards remain for precise diagnosis, but the invariant no longer depends on their completeness.

**The central question for this round: is that boundary genuinely total, and is the trade-off it makes correct?** A catch-all converts a real bug in the adapter into a `ModelError`. The author accepted that cost, keeping the original type name in the message and chaining `__cause__`. Judge whether that is right, or whether it now hides defects the earlier enumeration would have surfaced loudly.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`; without the venv first on PATH that hits the Microsoft Store alias and crashes Node.

## Task under review

**M3-model-client** — "ModelRequest/ModelResponse plus an OpenAI-compatible ModelClient that reaches the configured gateway, with ModelRegistry and the timeout/rate-limit retry policy." State: `active`, `unit:pass`, `independent-review:pending`.

### Requirements

- **FR-3**: `ModelClient` protocol with `send(ModelRequest) -> ModelResponse`, implemented by an OpenAI-compatible adapter that calls the configured `BASE_URL` and maps the response to `ModelResponse` (message, tool_calls, stop_reason, usage, provider_response_id, provider_metadata).
- **FR-12**: `ModelRegistry` records provider, model_id, model_version, adapter_version, capabilities.
- **FR-15**: `ModelError.Timeout` and `ModelError.RateLimited` retry up to 2 times with exponential backoff; every other `ModelError` propagates immediately.
- **NFR-1**: Provider-agnostic — switching between `openai.*`, `bedrock.*`, `azure.*`, `vertex_ai.*` is a configuration change only.
- **NFR-4**: No credential reaches model context, a persisted row, or a `RunEvent` payload.
- Recorded invariant: **only `AgentSDKError` subclasses may escape `ModelClient.send()`**.

### Files in scope

`agentsdk/model.py`, `agentsdk/providers/openai_compatible.py`, `agentsdk/providers/__init__.py`, `agentsdk/registry.py`, `agentsdk/config.py`, `tests/test_model_client.py`.

Also `agentsdk/primitives.py` and `agentsdk/executor.py` — owned by approved M1/M2, modified by these repairs. Breaking a completed milestone is still a defect.

## What to do

1. Read the files, `SPEC.md`, and the decisions/invariants/knowledge in `.genesis/project.json`.
2. Re-run the gate:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_model_client.py -q     # expect 115
   .venv\Scripts\python.exe -m pytest -q                                # expect 155
   ```
   A different number is itself a finding.
3. **Try to break the total boundary.** This is the highest-value thing you can do. The author asserts no `Exception` can escape `send()`, tested against 11 injected exception types plus `Infinity` token counts and 6000-deep nesting. Find a path that still escapes — during `build_payload`, inside the retry loop, in `aclose`, in the async context manager, from a custom transport, from `httpx` internals, during header construction, or anywhere the wrapper does not reach.
4. **Mutation-test.** The author claims all 16 of these are caught, including removal of the boundary itself. Re-run them and invent your own; the useful question is which mutation still survives.
   ```
   remove the total boundary          _as_int drops OverflowError
   totaliser swallows BaseException   decode drops RecursionError
   totaliser drops the cause          redact first occurrence only
   totaliser does not redact          Secret.__eq__ accepts raw str
   attempts 3 -> 7                    remove exponential growth
   drop StreamError                   unguard finish_reason
   truncate before redact             Secret.__str__ leaks
   lexicographic version sort         executor ignores arguments_error
   ```
   Restore every mutated file and verify SHA-256 before finishing.
5. Other angles worth suspicion:
   - Does the catch-all mask a real defect that should fail loudly? Construct one.
   - Is `_redact` reachable with an unset or empty key, and does it behave then?
   - Does `arguments_error` alter behaviour M1/M2's approved tests depended on?
   - Are the two accepted limitations still right — no retry attempt tracking (deferred to M5/M6 where `RunEvent.attempt_id` exists), and `_redact` blind to encoded renderings of the key?
   - Anything belonging to a later phase (subagents, MCP, sandbox, compaction, durable state) that should not exist yet?
6. Optionally verify against the live gateway. Credentials are in `.env` — never print them.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- Gates are computed, never narrated. Paste real command output for anything you assert.
- **Approve if it is sound.** Three rejections do not mean a fourth is owed. Do not manufacture a finding to justify another round — a defect must be reachable and must matter. Equally, a genuine escape from the boundary is still a rejection.
- If your only findings are latent, out-of-scope, or cosmetic, approve and record them as caveats in your reason rather than blocking.

## Recording your decision

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M3-model-client --gate independent-review \
  --human "<your name>" --reason "<what you verified, and any caveat>"

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M3-model-client --human "<your name>" --reason "<the defect>"
```

Then report: what you checked, what you ran, what you found, and your verdict.
