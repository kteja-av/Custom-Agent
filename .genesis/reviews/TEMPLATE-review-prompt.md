# L4 review prompt template

Copy this file to `<TASK-ID>-review-prompt.md`, fill every `<...>`, and verify
the test counts by running the suite immediately before handing it over. A stale
count misleads the reviewer and destroys the tripwire it exists to provide.

**Rotate the model between rounds.** M3 took five rounds: one reviewer found
exactly one defect per round for three rounds, all in the same region; swapping
models found one immediately in a region the first never examined; a third model
found none. Reviewer rotation, not reviewer effort, is what moved it.

---

You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did not write this code and you must not trust its author's claims about it.

**If you have reviewed `<TASK-ID>` before, stop and say so**, and ask the human for a different model or a clean session. Re-derive the requirements from `SPEC.md` rather than inheriting the framing below.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`; without the venv first on PATH that hits the Microsoft Store alias and crashes Node.

## Task under review

**`<TASK-ID>`** — `<outcome, copied verbatim from PLAN.md>`

State: `<state>`, `unit:<status>`, `independent-review:pending`. The gate passing is not sufficient and is not what you are judging.

### Requirements it claims to satisfy

`<paste each FR/NFR/AC verbatim from SPEC.md — not paraphrased>`

### Standing invariants that constrain every task

- Every `ToolResult` carries exactly one `ContentProvenance`.
- Every persisted row carries non-null `tenant_id` and `project_id`.
- `ToolExecutor` order is fixed: resolve → validate → permission → execute.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- Application code calls `Runner.run()` and nothing else.
- Only `AgentSDKError` subclasses may escape `ModelClient.send()`.
- A boundary's error path must not itself be able to raise.

### Files in scope

`<list>`

Also name any file owned by an already-approved milestone that these changes touched — breaking a completed milestone is still a defect.

## What to do

1. Read the files, `SPEC.md`, and the decisions/invariants/knowledge in `.genesis/project.json`.
2. Re-run the gate yourself:
   ```bash
   .venv\Scripts\python.exe -m pytest <gate test file> -q     # expect <N>
   .venv\Scripts\python.exe -m pytest -q                      # expect <M>
   ```
   A different number is itself a finding.
3. **Mutation-test the gate.** The author's claimed-caught list is `<paste>`. Re-run those, then invent your own — the useful question is which mutation still survives. Restore every mutated file and verify SHA-256 before finishing; restore in a `finally` so a crash cannot leave the tree mutated.
4. Attack the work on its own terms: `<task-specific angles>`.
5. Optionally verify against the live gateway. Credentials are in `.env` — never print them.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- Gates are computed, never narrated. Paste real command output for anything you assert.
- **Approve if it is sound.** Prior rejections do not mean another is owed. A defect must be reachable and must matter. If your only findings are latent, out-of-scope or cosmetic, approve and record them as caveats in your reason rather than blocking.
- Distinguish a defect from a gate blind spot where the code is correct — say which you found.

## Recording your decision

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . <TASK-ID> --gate independent-review \
  --human "<your name>" --reason "<what you verified, and any caveat>"

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . <TASK-ID> --human "<your name>" --reason "<the defect>"
```

Then report: what you checked, what you ran, what you found, and your verdict.
