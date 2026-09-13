You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh session.** Round 1 was reviewed by Claude Fable 5.1 and round 2 by
Claude Opus 5. If you reviewed either round, say so and ask for a different
session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.
Genesis stops a gate at 120 s by default and the full suite can take longer:
pass `--timeout 600000` to `genesis gate`.

The unit gate needs no credentials and no network. The regression gate needs
`DATABASE_URL`, `BASE_URL` and `MODEL_API_KEY` from `.env`. **Never print
credentials, and never print a connection string**: in round 2 a verification
probe printed the database URL with its password, which then had to be rotated.
Read `.env` values into variables and print only whether they are set. Windows
Developer Mode must stay on (decision D7).

## Task under review

**M10-safe-builtin-tools**: "A caller can opt into read-only file, fetch and web
search tools that cannot leave their root folder or allowlist, cannot reach
private addresses, send no credential, cap every tool output, and label external
results untrusted"

**Round 3, a narrow delta review of one seam.** Round 2 (control `df53b27e`,
source hash `f89820257340…`) approved with five caveats. The owner chose to repair
caveats 1 to 3 as one class before closing M10; caveats 4 and 5 are recorded
(KNOWLEDGE-39bcb4e5, KNOWLEDGE-45a11a6c). The repair changed the source, so round
2's approval no longer covers it. Read round 2's reason first:

```bash
.venv\Scripts\python.exe -c "import json; p=json.load(open('.genesis/project.json',encoding='utf-8')); print([c['reason'] for c in p['controls'] if c.get('id','').startswith('df53b27e')][0])"
```

Rounds 1 and 2 are described in `.genesis/reviews/M10-review-prompt.md` and
`M10-review-prompt-round2.md`. Round 3 changed only `agentsdk/executor.py`, and in
it only steps 8 and 9 of `ToolExecutor._execute`, plus a new helper `_text`.
Confinement, SSRF, charset handling, the walks and search are untouched.

**Nothing is committed.** HEAD is `cc2f0d6`.

## What round 2 found, and what the repair does

| round 2 caveat | the class | the repair |
|---|---|---|
| (1) a `str` subclass whose `__len__` lies put 200000 characters through a 40-character cap, and `ToolCalled` said `original_length` 3 | content read through methods the value itself controls | `_text` copies content into an exact `str` with `str.__getitem__(text, slice(None))`, which copies the characters actually held |
| (2) a replacement built past the `ToolResult` constructor (`object.__setattr__`, a subclass without `__post_init__`) carried a NUL, a lone surrogate or a non-str id, completed in memory and failed the run on Postgres | what an `after_tool` hook leaves to be returned was trusted | whatever is returned after the hook -- a replacement, **or the original result changed in place with no replacement** -- must be a `ToolResult` whose `tool_call_id` is exactly this call's id (a plain `str`, compared with `str.__eq__`) and whose `is_error` is a `bool`, or the call becomes a tool error; it is then rebuilt through the `ToolResult` and `ContentProvenance` constructors, walking `ContentProvenance`'s fields, so step 7's storability rule applies again |
| (3) a replacement the constructor turned into an error returned `Completed` while `ToolCalled` said `is_error: False` | the event hard-coded the error state | `ToolCalled` reports `is_error` of the result actually returned |

**Behaviour changes a caller could notice.** A hook that returns a result for a
different `tool_call_id`, or with a non-`bool` `is_error`, now produces a tool
error instead of passing through. A hook that returns a `ToolResult` subclass gets
a plain `ToolResult` back. No test or example did either (the author searched
every `after_tool` in `tests/` and `scripts/`).

**Found by the author during the repair:** the first version checked only
replacements. A hook that changes the result it was handed in place and returns
`CONTINUE` escaped it. A case was added to the test and seen red before the
executor was restructured to check whatever is returned.

New tests in `tests/test_builtin_tools.py` (now 84):

- `test_a_str_subclass_cannot_lie_its_way_past_the_cap`: the tool's return value
  and a hook's replacement.
- `test_whatever_a_hook_returns_is_checked_as_if_it_were_built_there`: NUL and
  lone-surrogate content, an int id, a NUL id, another call's id, a non-bool
  `is_error`, an unchecked subclass, a NUL in the provenance source, and two
  in-place changes with no replacement. Each is checked for type, id, `is_error`,
  storability of every text field, and agreement with `ToolCalled`.
- `test_tool_called_reports_the_error_state_of_the_result_actually_returned`.

## What the author ran

1. **Red first.** Against round 2's executor: caveat 1 returned a
   `LyingText`; caveat 2 returned unstorable content, id `12345` and another call's
   id; caveat 3's event said `is_error: False` for an error result. After the
   replacement-only repair, the two in-place cases failed alone, as predicted.
2. **Suites**, before the gates: `test_builtin_tools` + `test_tool_executor` +
   `test_agent_loop`: 187 passed.
3. **Gates**, on the final tree:
   ```
   $ genesis gate . M10-safe-builtin-tools --timeout 600000
   M10-safe-builtin-tools: passed executable gates
   unit        exit 0  2026-09-13T11:29:43Z -> 11:30:00Z  84 passed in 15.67s
   regression  exit 0  2026-09-13T11:30:00Z -> 11:32:05Z  818 passed in 124.10s
   source hash bd63683886c5...
   ```
   818 = round 2's 815 + the 3 caveat tests. The repair only added tests; no
   earlier test was changed. The regression gate took 124 s this time, past
   Genesis's 120 s default, which is why `--timeout` is needed.
4. **Executor mutants on the final tree**, each against the unit gate, files
   restored and SHA-256 verified:
   ```
   11 of 11 killed; tree restored: True
   M2   ran-failures lose declared labels               -> executor path [raised-external]
   M3   success content never cut                        -> cap [success]
   M4   cut one character short                          -> cap [success]
   M25  a hook substitution not capped                   -> cap [after_tool substitution]
   M32  a non-ToolResult replacement passes              -> hook replacement cannot escape the cap
   M33  _text keeps a str subclass                       -> str subclass cannot lie past the cap
   M36  the returned result not rebuilt                  -> hook replacement cannot escape the cap
   M37  another call's id or a non-bool is_error allowed -> checked as if built there
   M38  provenance not rebuilt                           -> checked as if built there
   M39  ToolCalled says is_error False again             -> checked as if built there
   M41  a result changed in place is not rebuilt         -> str subclass cannot lie past the cap
   ```
   The harness runs `pytest -x`, so each line names the first test to fail.
   M41's snippet first matched two places (the result built at step 7 has the
   same opening lines); the harness reported that instead of skipping, and the
   snippet was narrowed.
   A mutant that re-applied `_text` at step 7 as well survived: every result now
   goes through the rebuild, so a second conversion at step 7 guards the same
   property twice. Step 7 was restored to its round 2 line and that mutant dropped.

## Attack these first

- **Is the class closed?** Look for any other way a value the hook or tool
  controls reaches the returned result, the event or the store without passing
  the rebuild: `ContentProvenance` fields that are themselves hostile (a
  `frozenset` subclass for `taint_flags`, an `origin` that is not an `Origin`, a
  `str` subclass as `source_uri_or_hash`), the `Failed` path's content, a hook
  that raises after changing the result in place.
- **Did the stricter rule break a legitimate hook?** Redaction (example 03), MCP
  relabelling (example 06), the golden eval's hooks.
- **The cap and the event agree** for every returned result: `original_length`,
  `truncated`, `is_error`.

## Declared limitations: known, recorded, NOT findings

Rounds 1 and 2 declared limitations stand, including: file tools are Windows-only
(DECISION-16fd5eb5), an `after_tool` hook may still relabel provenance to a more
trusted one (a SPEC risk), the shared thread pool is a Phase 2 prerequisite
(DECISION-ae7e223f), and caveats 4 and 5 of round 2 (walks above 100000 entries;
glob opening matches past its cap) are recorded, not fixed. Round 2's
near-equivalent mutants R3, R7 and R10 stand as recorded.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Restore every file you mutate and verify SHA-256 in a `finally`, and kill the
  whole process tree on a timeout (`taskkill /T /F`).

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M10-safe-builtin-tools --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M10-safe-builtin-tools --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found, and your verdict.
