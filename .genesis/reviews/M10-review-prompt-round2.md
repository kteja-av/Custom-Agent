You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model, different from round 1** (round 1 was reviewed by Claude
Fable 5.1). If you have reviewed this project before, say so and ask for a
different session.

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
credentials.** Windows Developer Mode is on and must stay on (decision D7).

## Task under review

**M10-safe-builtin-tools**: "A caller can opt into read-only file, fetch and web
search tools that cannot leave their root folder or allowlist, cannot reach
private addresses, send no credential, cap every tool output, and label external
results untrusted"

**Round 2, a delta review.** Round 1 (control `158234c3`, source hash
`9413932d…`) approved with six caveats. The owner judged the first a defect in
substance and asked for it, and two small caveats, to be repaired before M10
closes. That repair changed the source, so round 1's approval no longer covers
the tree. Read round 1's full reason first:

```bash
.venv\Scripts\python.exe -c "import json; p=json.load(open('.genesis/project.json',encoding='utf-8')); print([c['reason'] for c in p['controls'] if c.get('id','').startswith('158234c3')][0])"
```

The round 1 prompt, `.genesis/reviews/M10-review-prompt.md`, describes the whole
milestone. You need not repeat round 1's confinement and SSRF attacks. Do check
that the repairs break none of what round 1 verified.

**Nothing is committed.** HEAD is `cc2f0d6`, and `agentsdk/builtin_tools.py` and
`tests/test_builtin_tools.py` are untracked, so there is no git diff of the
repair alone; the functions it touched are named below.

## What the repair changed

The owner's decisions on the round 1 caveats, 2026-09-13:

| caveat | decision | where |
|---|---|---|
| (1) D1: a server-chosen charset can name a bytes-to-bytes codec | **repaired** | `builtin_tools._media` |
| (2) C2: file tools starve the shared thread pool | recorded; a bounded executor is a Phase 2 prerequisite (DECISION-ae7e223f, KNOWLEDGE-0d5f3a4e) | not changed |
| (3) C3: a synchronous search backend iterable blocks the loop | recorded (KNOWLEDGE-b4d99824) | not changed |
| (4) C4: an `after_tool` replacement that is not a `ToolResult`, or not text, escapes the cap | **repaired** | `executor.ToolExecutor._execute`, step 8 |
| (5) C5: the walk budget read per directory only | **repaired** | `builtin_tools._Walk.spent`, `_glob`, `_grep` |
| (6) C6: TLS re-verification across redirects is incidental | recorded (KNOWLEDGE-cc627410) | not changed |

- **D1.** `_media` refuses any charset whose codec is not a text encoding: the
  codec's `_is_text_encoding` flag must be true and its incremental decoder must
  turn `b""` into `str`. A charset name no codec knows still reads as UTF-8. The
  refusal is the tool's own message, `fetch refused: the response names a charset
  that is not a text encoding`.
- **C4.** A replacement that is not a `ToolResult` becomes a tool error carrying
  the tool's declared labels, because the tool ran. A `ToolResult` whose content
  is not a `str` has its content rendered with `repr`, then capped.
- **C5.** `_Walk.spent()` reads the clock and the entry count; `entries()` calls
  it, and so do the per-entry loops of `_glob` and `_grep`.

New tests, all in `tests/test_builtin_tools.py` (now 81):

- `test_a_charset_that_is_not_a_text_encoding_is_refused_as_a_charset`: every
  non-text codec name, enumerated from the `encodings` package (19 names here),
  refused as a charset. Text charsets still decode.
- `test_each_charset_check_refuses_a_codec_the_other_would_admit`: two
  registered codecs, each caught by only one of the two checks.
- `test_a_non_text_charset_is_refused_with_assertions_stripped`: the same corpus
  in a separate `python -O` interpreter, asserting `sys.flags.optimize == 1`.
- `test_a_hook_replacement_that_is_not_a_tool_result_or_not_text_cannot_escape_the_cap`.
- `test_the_walk_budget_is_read_between_entries_not_only_between_directories`:
  a fake clock that advances a second per reading, and a count of files opened
  after the budget is spent.

## What the author ran

1. **Reproduced D1 first**, with a local server and an 8 KB zlib body:
   - default interpreter: `Failed`, `fetch failed: AssertionError`;
   - `python -O`: `Completed`, `original_length=8388622` against a
     1,000,000-byte cap, the content a bytes repr.
   - `codecs.lookup` resolves 7 non-text codecs under 19 names: base64, bz2,
     hex, quopri, rot-13, uu and zlib.
2. **Red first.** The four repair tests, run against the unrepaired code, all
   failed for their stated reason:
   - C4: `AssertionError: <class 'str'>`, the replacement came back as a bare str;
   - C5: `grep_files opened 202 of 200 files with a 5-tick budget`;
   - D1, default interpreter: `base64` refused only as `fetch failed: AssertionError`;
   - D1 under `-O`: `bz2` `Completed`, with decoded content.

   The two-codec test was written after the repair, so its red evidence is
   mutants M31b and M31c below.
3. **Gates**, on the final tree:
   ```
   $ genesis gate . M10-safe-builtin-tools --timeout 600000
   M10-safe-builtin-tools: passed executable gates
   unit        exit 0  2026-09-13T07:49:10Z -> 07:49:25Z  81 passed in 13.62s
   regression  exit 0  2026-09-13T07:49:25Z -> 07:50:56Z  815 passed in 90.86s
   source hash f89820257340...
   ```
   Full suite 815 = round 1's 810 + the 5 repair tests. The repair only added
   tests (and the `json`, `textwrap` and `types` imports); no round 1 test and no
   test that existed before M10 was changed.
4. **Mutation matrix, 38 mutants, 37 killed, 1 equivalent.** Every file was
   restored and SHA-256 verified, the whole `agentsdk` tree was hashed before and
   after, and the process tree was killed on timeout. The full run went over the
   final source with the tests as they stood then: 35 killed. Two more were then
   run on their own:
   - **M3** was skipped in that run: the C4 repair re-indented its lines, and the
     harness skipped a snippet it could not find without saying so. Its snippet
     was corrected, a skipped mutant is now printed, and M3 was killed.
   - **M32 survived the full run.** Deleting the explicit not-a-`ToolResult`
     check left a plain `str` replacement failing anyway, through the last-resort
     catch, but an object shaped like a result with short text content went out
     as the result. A case for that shape was added to the C4 test; M32 was
     re-run and killed. The first re-run of M32 counted as a kill while the
     updated test was failing on unmutated code (its assertion read a message
     the 40-character cap had cut off); that run is discarded, the assertion
     now reads the error object, and the second re-run, after `81 passed`, is
     the one reported.
   ```
   repair mutants
   M25  C4   a hook substitution not capped (cap line)          -> cap [after_tool substitution]
   M31  D1   any codec accepted as a charset                     -> charset refused as a charset
   M31b D1   the codec flag ignored (decoder probe only)         -> each charset check refuses a codec the other would admit
   M31c D1   the decoder output not probed (flag only)           -> each charset check refuses a codec the other would admit
   M32  C4   a replacement that is not a ToolResult passes       -> hook replacement cannot escape the cap (re-run)
   M33  C4   non-text content not rendered as text               -> hook replacement cannot escape the cap
   M34  C5   glob reads the budget per directory only            -> walk budget read between entries
   M35  C5   grep reads the budget per directory only            -> walk budget read between entries
   round 1 mutants, re-run on the repaired tree
   M1 M2 M3 M4 M5 M6 M7 M8 M8b M9 M10 M11 M12 M13 M15 M16 M17 M18 M19 M20
   M21b M22 M23 M24 M26 M27 M28 M29 M30                       -> all killed (M3 re-run; M23 by the 300 s timeout)
   M9b  '..' allowed by name                                   -> SURVIVED, equivalent
   ```

## Attack these first

- **D1 is a class claim.** Look for another way the server chooses a decoding
  stage: charset parameters that are quoted, repeated, mixed case, padded,
  percent-encoded or carried on `Content-Type` twice; a codec name that resolves
  only after normalisation (`ZLIB-CODEC`, `zlib codec`); a text codec that still
  expands (`utf-7`, `idna`, `punycode`, `unicode_escape`, `raw_unicode_escape`);
  a charset that decodes NULs or surrogates the executor then refuses.
- **C4.** Is every value an `after_tool` hook can return now either a capped,
  stored `ToolResult` or a tool error? Try a `ToolResult` subclass, content whose
  `__repr__` raises or returns something huge, and provenance that is not a
  `ContentProvenance`.
- **C5.** The budget is read per entry in the walks. `list_directory` has no
  walk: is a flat folder at its entry cap still bounded? Does `spent()` stop a
  `grep` inside one very large file? (That one is bounded by `max_file_bytes`.)
- **Regressions.** Round 1's confinement races and SSRF corpus are expensive.
  Re-run whatever you judge the repair could have affected.

## Declared limitations: known, recorded, NOT findings

Round 1's list stands (file tools Windows-only, DECISION-16fd5eb5; an
`after_tool` hook can relabel provenance; tools offered regardless of profile;
non-UTF-8 text reported binary; uninterruptible worker threads; uncounted billed
calls, ASSUMPTION-b7463ca2). Added in round 2, decided by the owner:
- C2, the shared thread pool: a Phase 2 prerequisite (DECISION-ae7e223f).
- C3, a synchronous search backend: caller code, recorded.
- C6, TLS pooling across redirects: recorded, needs a two-host test before any
  change there.
- M9b (`..` by name) and the walk's own directory check are equivalent mutants.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Distinguish a defect from a gate blind spot over correct code.
- Restore every file you mutate, verify SHA-256 in a `finally`, and kill the
  whole process tree on a timeout (`taskkill /T /F`). Unregister any codec you
  register. Leave the database and the temp folders as you found them.

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
