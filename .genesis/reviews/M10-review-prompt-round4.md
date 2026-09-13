You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh session.** Round 1 was Claude Fable 5.1, round 2 Claude Opus 5 and
round 3 Claude Sonnet 5. If you reviewed any of them, say so and ask for a
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
credentials or a connection string**: read `.env` values into variables and
print only whether they are set. Windows Developer Mode must stay on (D7).

## Task under review

**M10-safe-builtin-tools**: "A caller can opt into read-only file, fetch and web
search tools that cannot leave their root folder or allowlist, cannot reach
private addresses, send no credential, cap every tool output, and label external
results untrusted"

**Round 4, a narrow delta review of the same seam as round 3.** Round 3 (control
`4640a946`) **rejected**: step 8 of `ToolExecutor._execute` checked
`returned.is_error` on one read and built the result from a second, so a
`ToolResult` subclass built past its constructor answered `True` and then a
non-bool. The owner's own reproduction pushed a 5 MB string through the public
Runner on both stores: it persisted as `is_error` and passed a 40-character cap in
a field the cap does not measure. Round 3 also found `ContentProvenance` labels
unchecked (a plain-string `origin` completes in memory and raises on
`origin.value` at Postgres); it predates M10, and the owner folded it into this
repair. Read round 3's reason first:

```bash
.venv\Scripts\python.exe -c "import json; p=json.load(open('.genesis/project.json',encoding='utf-8')); print([c['reason'] for c in p['controls'] if c.get('id','').startswith('4640a946')][0])"
```

The task was moved back to active with `genesis control retry`. Earlier rounds are
described in `.genesis/reviews/M10-review-prompt*.md`. This round changed only
`agentsdk/executor.py`: step 8 of `_execute`, a new `_checked_provenance` and a
new `_MAX_SOURCE_CHARS`. **Nothing is committed.** HEAD is `cc2f0d6`.

## The repair

The rule, recorded as KNOWLEDGE-96063b68: **read each field of a value caller code
shapes exactly once, into a local; check that local for type and size; build only
from checked locals.**

- Step 8 reads `tool_call_id`, `content`, `provenance` and `is_error` from the
  returned result once each. The id must be a plain `str` equal to the call's id
  and `is_error` a `bool`, or the call is a tool error. The rebuilt `ToolResult`
  uses only those locals; its id is the call's own id.
- `_checked_provenance` reads each `ContentProvenance` field once and requires,
  by **exact type**: `Origin`, `InstructionAuthority` and `TrustZone` members; a
  `frozenset` of `TaintFlag` members; and a source that is `None` or a plain `str`
  of at most 8,192 characters. Anything else is a tool error. It returns a fresh
  `ContentProvenance` built from the checked locals.
- **New bound, visible to callers**: a result whose `source_uri_or_hash` is longer
  than 8,192 characters is a tool error. Before, it was stored whatever its size.
  That applies to a tool's own `ToolOutput` source too (the fetch tool's final
  URL), because every result passes the rebuild.
- **Stricter types, visible to callers**: a `ContentProvenance` whose labels are
  plain strings (for example `taint_flags={"external_content"}` rather than
  `TaintFlag` members) is now a tool error when it reaches step 8. The author did
  not search for callers that build one; the evidence is only that every test
  (819) and every example (run offline by AC-17 in the regression gate) passes.

New test: `test_every_field_of_a_returned_result_is_read_once_checked_and_used_as_checked`.
Its `Shifting` mixin answers a field honestly on the first read and with a lie
after it, which is the value round 3's tests could not express. Its cases:

- `is_error` shifting to a string, and to 5 MB;
- `tool_call_id` shifting to another call;
- `content` shifting to 5 MB with a NUL;
- `provenance` shifting to an entirely invalid one;
- a provenance whose `origin` shifts, and one whose source shifts to 5 MB;
- stable bad provenance: plain-string origin, plain-string trust zone, taint as a
  list, a non-flag in taint, a 5 MB source, a non-str source.

Each case must come back as a result with exact types, the call's id, a bool
`is_error` that agrees with `ToolCalled`, bounded content and source, a
serialisation under 20,000 characters through the real Postgres serialiser
(`postgres._provenance_to_json`), and a context the assembler can build. Or it
must come back as a tool error meeting all of those.

## What the author ran

1. **Red first**, against round 3's executor, per case:
   ```
   FAIL  is_error then a string                   ['is_error str / event str']
   FAIL  is_error then 5 MB                       ['is_error str / event str', 'serialised to 10000309 characters']
   ok    tool_call_id then another call           (the rebuild already used the call's id)
   ok    content then 5 MB with a NUL             (content was already read once)
   FAIL  provenance then entirely invalid         ['source str', "AttributeError while serialising: 'str' object has no attribute 'value'"]
   ok    provenance whose origin shifts           (each provenance field was already read once)
   ok    provenance whose source shifts to 5 MB   (the same)
   FAIL  stable plain-string origin               ["AttributeError while serialising: ..."]
   FAIL  stable plain-string trust zone           ["AttributeError while serialising: ..."]
   FAIL  stable taint as a list                   ["AttributeError while serialising: ..."]
   FAIL  stable taint with a non-flag             ["AttributeError while serialising: ..."]
   FAIL  stable 5 MB source                       ['source str', 'serialised to 5000309 characters']
   FAIL  stable non-str source                    ['source int']
   ```
   The four `ok` cases stay as guards against a second read coming back. The first
   version of the provenance case shifted only `origin`, which a per-field read
   takes from the honest first answer, so it passed for the wrong reason; it was
   strengthened and seen red before the repair.
2. **Suites after the repair**: `test_builtin_tools` + `test_tool_executor` +
   `test_agent_loop` + `test_primitives`: 262 passed. Examples 03 and 06 offline:
   the same output as before (redaction applied; MCP results `mcp_resource` and
   `untrusted`). Their exit codes were not captured in that run (a `tail` pipe hid
   them); the regression gate's AC-17 test runs every example and checks its exit
   code.
3. **Gates**, on the final tree:
   ```
   $ genesis gate . M10-safe-builtin-tools --timeout 600000
   M10-safe-builtin-tools: passed executable gates
   unit        exit 0  2026-09-13T12:42:11Z -> 12:42:31Z  85 passed in 19.44s
   regression  exit 0  2026-09-13T12:42:31Z -> 12:44:52Z  819 passed in 140.54s
   source hash bffc7add276c...
   ```
   819 = round 3's 818 + the one new test. The regression gate took 140 s, past
   Genesis's 120 s default. No earlier test was changed.
4. **Executor mutants on the final tree**, files restored and SHA-256 verified:
   ```
   16 of 16 killed, tree restored (15 in one run, M38 re-run on its own)
   M42  is_error read a second time                     -> read once, checked and used as checked
   M44  label types not checked                         -> read once, checked and used as checked
   M45  taint not checked                               -> read once, checked and used as checked
   M46  source not bounded                              -> read once, checked and used as checked
   M47  provenance fields read a second time            -> read once, checked and used as checked
   M36  the returned result not rebuilt                 -> hook replacement cannot escape the cap
   M37  another call's id or a non-bool is_error        -> checked as if built there
   M38  provenance not rebuilt                          -> checked as if built there
   M39  ToolCalled says is_error False again            -> checked as if built there
   M32  a non-ToolResult replacement passes             -> hook replacement cannot escape the cap
   M33  _text keeps a str subclass                      -> str subclass cannot lie past the cap
   M41  a result changed in place is not rebuilt        -> str subclass cannot lie past the cap
   M2   ran-failures lose declared labels               -> executor path [raised-external]
   M3   success content never cut                        -> cap [success]
   M4   cut one character short                          -> cap [success]
   M25  a hook substitution not capped                   -> cap [after_tool substitution]
   ```
   M38's snippet (`provenance=provenance,`) matched three places in the first
   run; the harness reported it rather than skipping, and it was narrowed to the
   rebuild and re-run. `pytest -x` names only the first test to fail.
   Not mutated, because no test can distinguish them, and so not claimed:
   - reading `content` a second time: whichever read is used still passes
     `_text`, the constructor and the cap;
   - removing the `provenance is None` guard: a `None` provenance then fails the
     `ToolResult` constructor, and the last-resort catch turns that into the
     same tool error.

## Attack these first

- **Is the class closed now?** Find any value caller code shapes that reaches the
  returned result, the `ToolCalled` payload, the session store or the event store
  without being read once and checked. Candidates:
  - step 7's `ToolOutput`: `value.source_uri` and `value.content` are read before
    the hook;
  - the enum members themselves;
  - a `TaintFlag` set whose members hash oddly;
  - `_failed`'s use of `state.tool.spec`;
  - a hook that raises after changing the result in place.
- **The new 8,192-character source bound.** Is it the right place and size? Does
  a legitimate redirect chain in the fetch tool now fail?
- **The stricter label types.** Does any legitimate path build a
  `ContentProvenance` with string labels?

## Declared limitations: known, recorded, NOT findings

The earlier rounds' lists stand:

- The file tools are Windows-only (DECISION-16fd5eb5).
- An `after_tool` hook can still relabel provenance to a more trusted one (a SPEC
  risk).
- `before_tool` replacements are trusted (round 3 lead a; it predates M10).
- The shared thread pool must be bounded before Phase 2 (DECISION-ae7e223f).
- Round 2's caveats 4 and 5 are recorded (KNOWLEDGE-39bcb4e5, KNOWLEDGE-45a11a6c).

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Restore every file you mutate and verify SHA-256 in a `finally`; kill the whole
  process tree on a timeout (`taskkill /T /F`). Delete any rows your probes write.

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
