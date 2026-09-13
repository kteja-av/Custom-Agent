You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** Across M5 to M9, nearly every defect was found in a region
the previous reviewer had not examined. If you have reviewed this project
before, say so and ask for a different session. Re-derive the requirements from
`SPEC.md` rather than inheriting the framing below.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

The unit gate needs no credentials and no network: every fetch test talks to
servers on 127.0.0.1. The regression gate needs `DATABASE_URL`, `BASE_URL` and
`MODEL_API_KEY` from `.env`. **Never print credentials.** Install first:
`.venv\Scripts\python.exe -m pip install -r requirements.txt` (M10 adds
`cryptography==50.0.1` to the test section, for a throwaway TLS certificate).

**Windows Developer Mode must be on** (decision D7): the confinement corpus
creates real symlinks and fails, rather than skips, when it cannot.

## Task under review

**M10-safe-builtin-tools**: "A caller can opt into read-only file, fetch and web
search tools that cannot leave their root folder or allowlist, cannot reach
private addresses, send no credential, cap every tool output, and label external
results untrusted"

First review round, risk **high** (D8). Requirements FR-35..FR-42, NFR-13,
NFR-14 and AC-27..AC-33 are in `SPEC.md` under "Safe built-in tools (M10)",
approved by the owner on 2026-09-12 with decisions D5..D9 (DECISION-afbe3542,
fc5d2760, 6c36d993, 5b1e3785, 59572370). Two owner decisions were added during
implementation on 2026-09-13:

- **DECISION-16fd5eb5**: the file tools are Windows-only in this release; on any
  other platform a file tool refuses to construct (`NotImplementedError`).
- **DECISION-ea6e1daf**: when a tool that declares provenance has run and then
  fails, the error result carries the declared origin too, not only the trust
  zone, authority and taint FR-40 names; its source stays the executor error URN.

Host facts the tests rely on: KNOWLEDGE-083e000b (link and stream behaviour),
KNOWLEDGE-c783cf3f and KNOWLEDGE-539d53d9 (Developer Mode on, verified).

**The M10 code is not committed.** HEAD is `cc2f0d6`. The working tree also holds
uncommitted `.genesis/*` changes (the two decisions above, and gate evidence).
Review the code with:

```bash
git diff cc2f0d6 -- agentsdk tests scripts README.md requirements.txt pyproject.toml
git status --short   # new: agentsdk/builtin_tools.py, scripts/10_builtin_tools.py, tests/test_builtin_tools.py
```

## What changed

- `agentsdk/tools.py`: `ResultProvenance` (a tool's declared labels, defaulting
  to today's `internal_tool`); `ToolOutput` (a result naming its own source);
  `ToolSpec` gains `result_provenance`, `max_output_chars` (default 50,000, a
  positive int, D5) and `configuration` (what a tool is bound to: a root, an
  allowlist). All three enter `schema_hash` only when they differ from their
  defaults, so every existing tool keeps its hash.
- `agentsdk/executor.py`: step 7 applies the declared provenance; `_CallState`
  records whether the implementation was invoked, and an error after that point
  (raised, timeout, unstorable, or a later hook failure through the last-resort
  catch) keeps the declared labels; the cap is applied after `after_tool`, to
  every result returned, errors included; `ToolCalled` gains `original_length`
  and `truncated`.
- `agentsdk/builtin_tools.py` (new):
  - `_FileSystem`: the only place file I/O happens (ctypes: `CreateFileW` with
    backup semantics and no delete sharing, `GetFinalPathNameByHandleW`,
    `GetFileInformationByHandleEx` for link counts and handle-based directory
    enumeration, `ReadFile`).
  - Confinement: a spelling check (`_components`), a pre-open `realpath` check,
    then the final path of the handle actually opened; files with more than one
    hard link refused, or withheld when met while walking; 8.3 aliases refused.
  - `read_file_tool`, `list_directory_tool`, `glob_tool`, `grep_tool` (literal,
    D6), each running in a worker thread with caps and a walk budget.
  - `fetch_tool`: allowlist normalised through httpx's own host parser; a
    numeric host is an address, never a DNS name; every answer of every hop must
    be public (`_is_public`, embedded IPv4 forms included); the connection is
    pinned to the checked address with `Host` and SNI naming the host;
    `trust_env=False`; a request built by hand (no cookies, no auth); raw body
    decoded with a per-step output cap; one `asyncio.timeout` over everything;
    text content types only; the final URL recorded as the source.
  - `web_search_tool` over a caller's `SearchBackend`; `SearchResult`.
- `scripts/10_builtin_tools.py` (new), `scripts/README.md`, `README.md`.
- `requirements.txt` and `pyproject.toml`: `cryptography==50.0.1`, test only.
- `tests/test_builtin_tools.py` (new, 76 tests). **No existing test was edited.**

## What the author ran

1. **Golden hashes first.** `schema_hash` for a grid of 1944 specs was captured at
   `cc2f0d6` with no change under `agentsdk/`, before M10 touched `tools.py`;
   the test compares against those literals.
2. **Tests first.** Against the pre-M10 code: 65 failed, 10 passed. The 10 were
   the golden-hash test, the eight default-provenance executor paths (today's
   behaviour, which M10 must keep) and the empty-registry check. Every failure
   was a missing module, field, attribute, payload key or file.
3. **Gates**, on the final tree:
   ```
   $ genesis gate . M10-safe-builtin-tools --timeout 600000
   M10-safe-builtin-tools: passed executable gates
   unit        exit 0  2026-09-13T06:14:28Z -> 06:14:53Z  76 passed in 21.84s
   regression  exit 0  2026-09-13T06:14:53Z -> 06:16:46Z  810 passed in 111.91s
   ```
   Run with `--timeout 600000` as well: a separate timed run of the full suite
   took 177.6 s.
   Full suite **810 tests**: `test_agent_loop 77` + `test_builtin_tools 76` +
   `test_distribution 8` + `test_golden_eval 15` + `test_honest_results 271` +
   `test_model_client 152` + `test_persistence 80` + `test_phase2_readiness 31` +
   `test_primitives 74` + `test_tool_executor 26` = 810. The 734 pre-M10 tests are
   unchanged; `test_distribution` runs example 10 offline under AC-17.
   Genesis runs a gate with a 120 s default limit (`genesis.mjs` line 893), and
   the full suite can take longer than that (111.9 s in the gate above, 177.6 s
   in a separate timed run; the live evaluation's latency varies): the first
   `genesis gate` killed the regression gate at 120 s with no test failed. The gates were recomputed with
   `--timeout`; the gate commands themselves are unchanged.
4. **Mutation matrix, 31 mutants on the final tree, 30 killed**, each against the
   unit gate (`-x`), every file restored and SHA-256 verified, the whole
   `agentsdk` tree hashed before and after (`tree restored: True`):
   ```
   M1   FR-40/AC-32 hash includes the default provenance     -> golden schema_hash test
   M2   FR-40 ran-failures lose the declared labels          -> executor path [raised-external]
   M3   FR-41 success content never cut                      -> cap [success]
   M4   FR-41 cut one character short                        -> cap [success]
   M25  FR-41 a hook substitution not capped                 -> cap [after_tool substitution]
   M5   FR-36 no final-path check on the opened handle       -> link swapped between check and open
   M6   NFR-14 no pre-open realpath check                    -> refusal does not reveal existence
   M7   FR-36 read ignores hard links                        -> escape corpus
   M8   FR-36 reserved device names allowed                  -> escape corpus
   M8b  FR-36 a colon allowed in names (drives, streams)     -> escape corpus
   M9   FR-36 trailing dots and spaces allowed               -> escape corpus
   M9b  FR-36 '..' allowed by name                           -> SURVIVED, equivalent (A11)
   M10  FR-36 8.3 short names allowed                        -> escape corpus
   M22  FR-36 listings keep links that resolve outside       -> escape corpus
   M23  FR-37 grep as a regular expression                   -> killed by the 300 s timeout (A11)
   M24  FR-37 read does its I/O on the event loop            -> thread-identity spy
   M30  FR-36 grep of a hard link named directly             -> escape corpus
   M11  FR-38 every address public                           -> non-global address classes
   M12  FR-38 embedded IPv4 not checked                      -> non-global address classes
   M13  FR-38 environment proxies honoured                   -> proxies receive nothing
   M15  FR-38 a whole chunk decompressed at once             -> compression bomb [gzip]
   M16  FR-38 only the first hop checked                     -> host spellings
   M17  FR-38 no total deadline                              -> trickling or silent server
   M18  FR-39 search fields not capped                       -> search caps
   M19  FR-38 any content type                               -> non-text content type
   M20  FR-38 userinfo allowed                               -> host spellings
   M21b FR-38 redirect loop bound off by one                 -> redirect cap
   M26  FR-38 numeric hosts sent to DNS                      -> numeric and embedded host spellings
   M27  FR-38 allowlist not enforced                         -> host spellings
   M28  FR-38 a wildcard admits a suffix without its dot     -> host spellings
   M29  FR-38 port spellings accepted in the allowlist       -> allowlist validation
   ```
   Not mutated, and so not claimed: the executor's `_CallState` handling in the
   last-resort catch beyond M2, `_bounded_body`'s identity branch, `_media`'s
   charset parsing, `web_search_tool`'s skipping of malformed items, and the
   8.3-name rule inside `glob`.
5. **Example 10 live**, by hand (the gate runs it offline): every scripted
   refusal as offline, then a real model (the client default `openai.gpt-4o-mini`)
   called `fetch_url` on `https://example.com/`, answered `The title of the page
   is "Example Domain".`, and the result carried `origin=external_tool
   trust_zone=untrusted`. 7 of 7 checks passed.
6. **Host probes** before writing the file layer: the final path of an opened
   file or directory handle follows symlinks and junctions; enumeration through
   a directory handle works; with such a handle open, neither that directory nor
   its parent can be renamed (winerror 32 and 5).

## Issues the author found during M10

Labels `A` are the author's own and never share a reviewer's label.

| id | finding | disposition |
|---|---|---|
| A1 | Two tests passed against pre-M10 code: the `TypeError` for an unknown keyword names the field, and matched `match="max_output_chars"` | a valid construction first, as a control |
| A2 | The address-sample test asserted `127.0.0.1` was sampled; the generator takes each network's edges and middle | well-known addresses added by hand |
| A3 | `grep` given a hard-linked file directly returned "no matches" instead of refusing as `read` does | refused when named; withheld when walked |
| A4 | The control fixture wrote `"\n"` with `write_text`, which Windows stores as `"\r\n"`, and `read` rightly returned the bytes on disk | `newline="\n"` in the fixture |
| A5 | Every fetch built a client and loaded the certificate store (about 0.4 s) before checking the URL: the address corpus ran for over half an hour | checks first, client only after a hop passes, certificate store loaded once with `trust_env=False` |
| A6 | Allowlist entries `allowed.test:80`, `allowed.test:` and `[::1]:80` were accepted: httpx drops a default or empty port | refused by spelling; added to the test |
| A7 | httpx will not parse some numeric spellings (`0177.0.0.1`) even as allowlist entries, which the numeric-host test assumed | the test falls back to another allowlist; the URL must still be refused unresolved and undialled |
| A8 | Mutant M6 survived: without the pre-open check, a link to a missing outside file answers "not found" and one to an existing file "outside", which tells the model what exists outside | new test: both refusals identical |
| A9 | Mutant M21 survived: the in-loop redirect cap duplicated the raise after the loop | one guard, the loop bound; mutant M21b |
| A10 | The mutation harness's timeout killed only the venv's launcher `python.exe`; M11 ran about 40 minutes, and stopping that run left mutant M12 in the untracked `builtin_tools.py` | found by searching for mutation markers, restored, unit gate green; the harness now kills the process tree. **Check that no mutant remains** |
| A11 | M9b (`..` allowed by name) cannot be killed: every `..` segment also ends in a dot, which the trailing-dot rule refuses. M23 (grep as a regex) is killed only by a 300 s timeout: the runaway regex holds a worker thread | recorded as equivalent, and as slow |

## Attack these first

- **The handle is the claim.** The swap test swaps a link between the pre-open
  check and the open. Look for a swap the tests do not make: retargeting an
  existing junction in place (a reparse-point change, no rename), a parent
  directory replaced between a walk's listing and a later open by path (glob and
  grep close the directory handle before opening children by path, and rely on
  each child's own final-path check), hard links created mid-walk.
- **Spelling forms Windows accepts that `_components` does not name**: NTFS
  metafiles (`$MFT`, `$Extend`), `\\?\` semantics of names the rules admit,
  names ending in U+3000 or other Unicode spaces, device names under Windows 11's
  relaxed rules, case-sensitive directories (the prefix check compares exact case).
- **`_is_public` rests on Python 3.11's `ipaddress` tables.** `192.0.0.9` is
  global there; 6to4 needed an explicit embedded check. Is any special-purpose
  range global in 3.11 that should not be? What of DNS answers carrying scope ids?
- **Pinning and TLS.** The URL is rewritten to the address; `Host` and
  `sni_hostname` name the host. Check an IP-literal https host, an IDNA host, a
  redirect that changes scheme, and whether httpx could reuse a pooled
  connection across hops to a different host.
- **The body.** Stacked encodings (`gzip, gzip`), a `deflate` body in raw
  (headerless) form, a charset that decodes NULs, a `Content-Length` that lies.
- **Search.** A backend returning an endless generator of malformed items is
  bounded only by the executor timeout, not by `max_results`.
- **The cap.** Applied after `after_tool`, so a redacting hook sees the whole
  text first (KNOWLEDGE-294f2901). Is every result the executor returns capped,
  including one a hook returns that is not a `ToolResult`?
- **Provenance.** Does any path yield external content under `internal_tool`
  labels? The last-resort catch derives labels from `_CallState`.

## Declared limitations: known, recorded, NOT findings

- File tools are Windows-only (DECISION-16fd5eb5).
- An `after_tool` hook can overwrite declared provenance with a more trusted one
  (SPEC risk, not closed in M10).
- Registered built-in tools are offered to every agent; the profile gates
  execution only (DECISION-ca1ad3e0; SPEC non-goal).
- A text file that is not valid UTF-8 (Latin-1, UTF-16) is reported as binary.
- A worker thread cannot be interrupted: after the executor's timeout a file
  tool's thread runs on until its own 20 s walk budget.
- A billed model call with no usage reported is not counted (ASSUMPTION-b7463ca2,
  planned with the native Anthropic adapter, DECISION-0e3ed41b). Not M10.
- The gate runs example 10 offline; the live run was by hand.
- Older `independent-review` gates compute `stale` because the repository hash moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Distinguish a defect from a gate blind spot over correct code.
- Restore every file you mutate and verify SHA-256, restoring in a `finally`, and
  kill the whole process tree on a timeout (`taskkill /T`): A10 is what happens
  otherwise. Remove any links you create outside pytest's temporary folders.

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
