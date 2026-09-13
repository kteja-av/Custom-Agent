You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh session.** Round 1 was Claude Fable 5.1, round 2 Claude Opus 5,
round 3 Claude Sonnet 5 and round 4 a fresh Claude Fable 5.1 session. If you
reviewed any of them, say so and ask for a different session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.
Genesis stops a gate at 120 s by default and the full suite takes longer: pass
`--timeout 600000` to `genesis gate`.

The unit gate needs no credentials and no network. The regression gate needs
`DATABASE_URL`, `BASE_URL` and `MODEL_API_KEY` from `.env`. **Never print
credentials or a connection string**: read `.env` values into variables and print
only whether they are set. Delete any rows your probes write. Windows Developer
Mode must stay on (D7).

## Task under review

**M10-safe-builtin-tools**: "A caller can opt into read-only file, fetch and web
search tools that cannot leave their root folder or allowlist, cannot reach
private addresses, send no credential, cap every tool output, and label external
results untrusted"

**Round 5, a narrow delta review.** Round 4 (control `94bc5480`) **rejected**:
`_checked_provenance` checked `type(origin) is Origin`, but the labels are
str-mixin enums, so `str.__new__(Origin, "x")` builds an instance of exactly
`Origin` that is none of its members. With no `_value_`, the run failed on both
stores; with a 5 MB `_value_`, the value was stored past a 40-character cap and
could not be read back; with a NUL, it failed on Postgres. Equality is not enough
either: a forged `"system"` valued `"user"` passes string equality. Taint flags
behaved the same. Read round 4's reason first:

```bash
.venv\Scripts\python.exe -c "import json; p=json.load(open('.genesis/project.json',encoding='utf-8')); print([c['reason'] for c in p['controls'] if c.get('id','').startswith('94bc5480')][0])"
```

Earlier rounds are in `.genesis/reviews/M10-review-prompt*.md`. The task was
moved back to active with `genesis control retry`. **Nothing is committed.** HEAD
is `cc2f0d6`.

## The threat model is now decided -- read this before judging anything

Rounds 3 and 4 were both rejected on the same seam, each for a new way in-process
caller code can build an object past its constructor or the language machinery.
The owner has drawn the line, **DECISION-2bad84bb** (2026-09-13):

> Tools, hooks, permission checkers, registries and other in-process caller code
> are trusted code. A defect that requires such code to forge objects past their
> constructors or the language machinery (object.__setattr__ on a frozen
> dataclass, __new__ without __init__, str.__new__ on a str Enum, __class__
> assignment, attribute access overrides, ctypes) is outside the M10 threat model.
> The checks that exist stay as defence in depth, and such a finding is recorded
> as a caveat, not a blocker. In scope remain everything a model, a remote server
> or a file on disk can supply, and caller code that uses the public types as
> documented.

**Apply it.** A finding that needs forgery of that kind is a caveat. A finding
reachable through model output, a fetched page, a redirect, a search backend's
ordinary return values, a file's contents or names, or documented use of the
public types is in scope and can block.

## The repair

`agentsdk/executor.py` only:

- **`_is_member(value, labels)`** is identity against the enum's own members:
  `any(value is member for member in labels)`. `_checked_provenance` uses it for
  `origin`, `instruction_authority`, `trust_zone` and each taint flag. The taint set
  must still be an exact `frozenset`, and the source `None` or plain text of at
  most 8,192 characters.
- **Refusals name their cause** (round 4 caveat c, a long redirect blamed "label
  types"). `_checked_provenance` returns the checked provenance or the reason:
  - not a `ContentProvenance`;
  - labels that are not members of their enums;
  - taint flags that are not members of `TaintFlag`;
  - a source that is not text of at most 8,192 characters.

  Step 8 turns the reason into the tool error.

New tests, in `tests/test_builtin_tools.py` (now 87):

- `test_provenance_labels_must_be_the_real_enum_members_not_lookalikes`: forged
  labels with no `_value_`, a 5 MB `_value_`, a NUL `_value_`, `"system"` valued
  `"user"`, a forged authority and trust zone, and forged taint flags. Each is
  checked by identity against the real members, through the Postgres serialiser
  and the context assembler.
- `test_a_source_over_the_bound_is_refused_as_a_source`.

These close round 4's finding even though, under DECISION-2bad84bb, it would now
be a caveat: the fix is one small function on the persisted path.

## What the author ran

1. **Red first**, against round 4's executor:
   ```
   FAIL  origin with no _value_                         ['a label is not a real member', "AttributeError: 'Origin' object has no attribute '_value_'"]
   FAIL  origin with a 5 MB _value_                     ['a label is not a real member', 'serialised to 5000283 characters']
   FAIL  origin with a NUL _value_                      ['a label is not a real member']
   FAIL  origin spelled system, valued user             ['a label is not a real member']
   FAIL  authority with a 5 MB _value_                  ['a label is not a real member', 'serialised to 5000287 characters']
   FAIL  trust zone spelled trusted, valued untrusted   ['a label is not a real member']
   FAIL  taint flag with a 5 MB _value_                 ['a taint flag is not a real member', 'serialised to 5000298 characters']
   FAIL  taint flag with no _value_                     ['a taint flag is not a real member', "AttributeError: 'TaintFlag' object has no attribute '_value_'"]
   source message test FAIL: the result carries provenance with a label of the wrong type, or a source that is not bounded text
   ```
2. **Suites after the repair**: `test_builtin_tools` + `test_tool_executor` +
   `test_agent_loop` + `test_primitives`: 264 passed.
3. **Gates**, on the final tree:
   ```
   $ genesis gate . M10-safe-builtin-tools --timeout 600000
   M10-safe-builtin-tools: passed executable gates
   unit        exit 0  2026-09-13T13:12:36Z -> 13:12:52Z  87 passed in 15.41s
   regression  exit 0  2026-09-13T13:12:52Z -> 13:14:44Z  821 passed in 110.94s
   source hash b7951f39862e...
   ```
   821 = round 4's 819 + the two new tests. No earlier test was changed. The
   regression gate includes AC-17, which runs every example offline and checks its
   exit code.
4. **Executor mutants on the final tree**, files restored and SHA-256 verified:
   ```
   18 of 18 killed; tree restored: True
   M48  membership by equality                 -> read once, checked and used as checked (a plain string equals its member)
   M49  membership by exact type (round 4)     -> labels must be the real enum members
   M44  labels not checked                     -> read once, checked and used as checked
   M45  taint not checked                      -> read once, checked and used as checked
   M46  source not bounded                     -> read once, checked and used as checked
   M47  provenance fields read a second time   -> read once, checked and used as checked
   M42  is_error read a second time            -> read once, checked and used as checked
   M36  the returned result not rebuilt        -> hook replacement cannot escape the cap
   M37  another call's id or non-bool is_error -> checked as if built there
   M38  provenance not rebuilt                 -> checked as if built there
   M39  ToolCalled says is_error False         -> checked as if built there
   M32  a non-ToolResult replacement passes    -> hook replacement cannot escape the cap
   M33  _text keeps a str subclass             -> str subclass cannot lie past the cap
   M41  a result changed in place not rebuilt  -> str subclass cannot lie past the cap
   M2 M3 M4 M25 (round 1 executor mutants)     -> provenance paths and output cap
   ```
   `pytest -x` names only the first failing test, so M48 appears under the
   read-once test: a stable plain-string origin already equals `Origin.INTERNAL_TOOL`
   as a string, before the forged-label test runs.

## Attack these first

- **In scope, per the decision.** Can anything a model, a fetched page, a redirect,
  a search backend's return value, or a file on disk supplies still reach a
  returned result, a `ToolCalled` payload or a stored row without being checked?
  Or reach it in a form that completes in memory and fails on Postgres?
- **Documented use of the public types.** Does the stricter step 8 refuse
  anything a caller writing ordinary code would build? For example a
  `ContentProvenance` constructed with `taint_flags={"external_content"}`, which
  its constructor accepts. Is that refusal honest, or a regression for
  documented use?
- **The source bound** of 8,192 characters against real redirect chains.

## Declared limitations: known, recorded, NOT findings

- Anything DECISION-2bad84bb places outside the threat model: record it as a
  caveat, not a blocker.
- The file tools are Windows-only (DECISION-16fd5eb5).
- An `after_tool` hook can relabel provenance to a more trusted one (a SPEC risk).
- `before_tool` replacements are trusted (it predates M10).
- A tool's declared `ResultProvenance` is checked when the tool is built;
  changing it afterwards is caller configuration (round 4 caveat b).
- The shared thread pool must be bounded before Phase 2 (DECISION-ae7e223f).
- Round 2's caveats 4 and 5 are recorded (KNOWLEDGE-39bcb4e5, KNOWLEDGE-45a11a6c).

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable within the decided threat
  model and must matter; record anything else as a caveat.
- Restore every file you mutate and verify SHA-256 in a `finally`; kill the whole
  process tree on a timeout (`taskkill /T /F`).

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
