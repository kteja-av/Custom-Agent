You are the independent L4 reviewer for a **delta** review. This is not a fresh
review of M5 — that was approved at round 9 after eight rejections, by a
reviewer whose report is summarised below. Your job is narrow: decide whether
the changes made *after* that approval are sound.

**Use a fresh model.** Nine rounds, and every defect but one was found in a
region the previous reviewer had not examined. If you reviewed M5 before, say
so and ask for a different session.

## Why this exists

M5 was approved. The round-9 reviewer listed four caveats and I closed all four
rather than carrying them, which changed the source and made Genesis mark the
`independent-review` gate **stale** — the approval was bound to the pre-fix
source hash. That is the harness working correctly, not a workaround to route
around. Hence this delta review.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

`DATABASE_URL` is in `.env` and is loaded by the test module. Never print
credentials — a round-7 probe echoed a DSN fragment into its own output.

## The exact delta

```bash
git diff f2ab64b HEAD -- agentsdk tests
```

Six files, +204/-17. `f2ab64b` is the approved state.

```
   .venv\Scripts\python.exe -m pytest -q     # 407   (was 397 at approval)
```

## What changed, and what to attack

**1. `max_turns=True`.** The round-9 reviewer isolated this as the last
instance of round 8's shape, and it is a real divergence: psycopg adapts a bool
to SQL `boolean`, so it completes in memory and fails the write with
`DatatypeMismatch`. My `not isinstance(value, bool)` exclusion was copied from
`token_count`, where it is right for a different reason. Now refused at
`column_rejection_reason` **and** `RunConfig`.
→ *Attack:* two guards for one property is the shape that has hidden defects
twice in this project (`KNOWLEDGE-aa97d748`). Each has its own unit test now;
check that deleting either one alone actually fails the suite. Check also that
refusing a bool does not reject something legitimate — `JSONB` still accepts one.

**2. Schema tests assert definitions, not existence.** `pg_get_constraintdef`
and `information_schema` column types, in the throwaway namespace.
→ *Attack:* is the string matching brittle or over-fitted? Would a legitimate
reformatting of `schema.sql` fail it? Are there constraints it still only
counts rather than reads? The round-9 reviewer could not confirm its
`INTEGER → BIGINT` mutation; I ran it — it survives the old test and dies on
the new one — but verify that independently.

**3. `_safe_finish` is pinned.** Error path swallows a persistence failure;
success path deliberately does not.
→ *Attack:* the tests reach past the constructor and set `runner._persistence`
to a `SimpleNamespace`. Is that fake faithful enough to prove anything, or does
it prove only that the fake behaves? Is the success-path asymmetry right?

**4. The non-finite backstop is extracted** into `_serialisation_reason` so it
can be pinned as its own layer.
→ *Attack:* extracting a private function to make it testable can change
behaviour. Confirm `unstorable_reason` still answers identically, and that the
named layer still wins for the diagnosis.

## Also worth your time

- **Regression:** M1–M4 and the rest of M5 must be untouched. `git diff` shows
  only the six files; confirm nothing else moved.
- The round-9 reviewer's one demonstrated divergence — JSONB normalising floats
  `>= 1e16` back to int — is **recorded as a limitation, not fixed**. Judge
  whether that is the right call.
- Declared limitations are listed in `M5-review-prompt.md` and on the task
  record. They are not findings unless you can show one is reachable as a
  Phase 0 *correctness* failure.

## Rules

- **Do not fix the code.** Report; the implementing session repairs.
- Paste real command output for anything you assert. Gates are computed.
- **Approve if the delta is sound.** This is a narrow review: the milestone is
  already approved on its merits. Latent or cosmetic findings are caveats.
- If a mutation "kills" via an error rather than a failure, it did not run —
  say so rather than counting it. That mistake has been made in this project by
  both the author and a reviewer.

## Recording your decision

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M5-postgres --gate independent-review \
  --human "<your name>" --reason "<what you verified in the delta, and any caveat>"

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M5-postgres --human "<your name>" --reason "<the defect>"
```
