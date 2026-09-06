You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did not write this code and you must not trust its author's claims about it.

**Use a fresh model.** M3 took five rounds: one reviewer found exactly one defect per round for three rounds, all in the same region; swapping models found one immediately in a region the first never examined; a third model found none. Reviewer rotation, not reviewer effort, is what moved it. If you have reviewed M4 before, say so and ask for a different session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`; without the venv first on PATH that hits the Microsoft Store alias and crashes Node.

## Task under review

**M4-loop-and-api** — "ContextAssembler, AgentLoop and the Runner/AgentSpec/RunConfig/RunResult public surface drive a full tool-using turn against an in-memory session store."

State: `active`, `unit:pass`, `independent-review:pending`. The gate passing is not sufficient and is not what you are judging.

### Requirements it claims to satisfy

- **FR-1**: `Runner.run(spec, task, config)` drives one agent to a terminal status of `completed`, `failed`, or `max_turns_exceeded`, and returns a `RunResult` carrying status, output, events, and usage.
- **FR-7**: A minimal `ContextAssembler` builds `ModelRequest` from message history and tool schemas, carrying provenance as request metadata rather than as model-readable instruction text.
- **FR-14**: `AgentLoop` terminates at `max_turns` with status `max_turns_exceeded` as a defined terminal state, never as an exception escaping to application code.
- **NFR-5**: The public API surface is `AgentSpec`, `RunConfig`, `Runner`, `RunResult` only. No internal collaborator is imported by application code or by the golden eval.
- **NFR-6**: No runtime dependency on LangGraph, Claude Agent SDK, or OpenAI Agents SDK. The SDK must import and run with none of them installed.
- **AC-8**: A run configured with a deliberately low `max_turns` terminates with status `max_turns_exceeded` and raises nothing to the caller.

### Standing invariants that constrain every task

- Every `ToolResult` carries exactly one `ContentProvenance`.
- Every persisted row carries non-null `tenant_id` and `project_id`.
- `ToolExecutor` order is fixed: resolve → validate → permission → execute.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- Application code calls `Runner.run()` and nothing else.
- Only `AgentSDKError` subclasses may escape `ModelClient.send()`.
- A boundary's error path must not itself be able to raise.

### Files in scope

New: `agentsdk/loop.py`, `agentsdk/api.py`, `agentsdk/context.py`, `agentsdk/session.py`, `agentsdk/events.py`, `tests/test_agent_loop.py`.
Modified: `agentsdk/__init__.py` (public exports).

`agentsdk/executor.py`, `primitives.py`, `model.py`, `providers/` belong to approved M1–M3 and are unchanged here, but M4 depends on them — breaking a completed milestone is still a defect.

## What to do

1. Read the files, `SPEC.md`, and the decisions/invariants/knowledge in `.genesis/project.json`.
2. Re-run the gate yourself:
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_agent_loop.py -q    # expect 39
   .venv\Scripts\python.exe -m pytest -q                             # expect 200
   ```
   A different number is itself a finding.
3. **Mutation-test the gate.** The author ran 21 mutations before submitting; one survived (nothing pinned the empty-tool-profile deny default) and a test was added for it. All 21 are now killed:
   ```
   off-by-one on max_turns            max_turns never terminates run
   max_turns raises instead of status max_turns floor not enforced
   usage not accumulated              model failure reported as completed
   loop stops before running tools    tool results never fed back
   retries non-transient errors       never retries transient errors
   provenance dropped from metadata   provenance leaked into prompt text
   instructions dropped               tool schemas dropped
   tenant/project not enforced        events lose tenant scoping
   event sequence not monotonic       completion event never emitted
   empty tool profile allows all      history returns the live list
   runs share one history
   ```
   Re-run those and invent your own. Restore every mutated file and verify SHA-256, restoring in a `finally` so a crash cannot leave the tree mutated.
4. Attack the work on its own terms. Worth suspicion:
   - **The loop's error path.** `AgentLoop` catches `ModelError` and returns a failed `LoopOutcome`. Can any other exception escape `Runner.run()` — from a tool, a hook, the assembler, the session store, model resolution? FR-1 promises a terminal status; is that promise as total as M3's boundary, or is it another enumeration?
   - **Retry duplication.** Both `AgentLoop` and the adapter retry transient errors, so a timeout can be retried 3 × 3 = 9 times. Is that intended, correct, and consistent with FR-15?
   - **Provenance metadata.** FR-7 says provenance must not be model-readable. Verify it truly is not — check what the adapter puts on the wire, not just what `ModelRequest.metadata` holds.
   - **`RunResult.error`** is a fifth field beyond the design's four. Justified, or scope creep?
   - Does `RunStatus.FAILED` ever get reported as `COMPLETED`, or a partial output returned on failure?
   - Is `max_turns` counted per model call, per tool call, or per loop iteration — and does the answer match FR-14?
   - Anything belonging to a later phase (subagents, MCP, sandbox, compaction, durable state, Postgres) that should not exist yet? Note `events.py` defines the `RunEvent` envelope that FR-10 (M5) will persist; judge whether defining it here is correct or premature.
5. Optionally verify against the live gateway. Credentials are in `.env` — never print them. A real multi-step run using only the public API is the strongest evidence available.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- Gates are computed, never narrated. Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. If your only findings are latent, out-of-scope or cosmetic, approve and record them as caveats in your reason rather than blocking.
- Distinguish a defect from a gate blind spot where the code is correct — say which you found.

## Recording your decision

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M4-loop-and-api --gate independent-review \
  --human "<your name>" --reason "<what you verified, and any caveat>"

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M4-loop-and-api --human "<your name>" --reason "<the defect>"
```

Then report: what you checked, what you ran, what you found, and your verdict.
