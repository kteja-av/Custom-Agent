You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** Across M5 to M7, nearly every defect was found in a region
the previous reviewer had not examined. If you have reviewed this project
before, say so and ask for a different session.

## Repository

`<repo>`

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

The unit gate needs no credentials. The regression gate needs `DATABASE_URL`,
`BASE_URL` and `MODEL_API_KEY` from `.env`. **Never print credentials.**
Install first:
`.venv\Scripts\python.exe -m pip install -r requirements.txt -r scripts/requirements.txt`

## Task under review

**M8-distribution-and-examples**: "The SDK installs as a package, and eight
runnable examples in scripts/ show how to build agents, tools, hooks,
persistence, model switching, MCP bridging, delegation and offline tests --
each running live and offline, with no credential in any tracked file."

First review round. Requirements FR-22..FR-25, NFR-9, NFR-10 and AC-16..AC-19
are in `SPEC.md` under "Distribution and examples (M8)". M8's own non-goal: it
adds **no SDK capability**.

## What changed

- `pyproject.toml` (new): package metadata, runtime dependencies, `test` and
  `examples` extras, `schema.sql` and `migrations/*.sql` as package data.
- `scripts/` (new): eight examples, `scripts/README.md`, `scripts/requirements.txt`.
- `tests/test_distribution.py` (new, 8 tests): the gate.
- `README.md`: an Examples section, a package install option, the test count.
- `.env.example`: `DEFAULT_MODEL` emptied (NFR-10 says no values).
- `.gitignore`: packaging debris. `requirements.txt`: `wheel`, which the wheel test uses.

**`agentsdk/` is unchanged by M8.** Verify rather than trust:
`git diff --stat 0bf8fd9 -- agentsdk/` should print nothing.

## What the author ran

1. **Tests first.** The gate was added before any file it tests: 5 failed, 2
   errored in their fixture, 1 passed (the credential scan). After the files
   were copied in: 8 passed.
2. **Gates.**
   ```bash
   .venv\Scripts\python.exe -m pytest tests/test_distribution.py -q   # 8
   .venv\Scripts\python.exe -m pytest -q                              # 462
   ```
   Per file: `test_agent_loop 77` + `test_distribution 8` + `test_golden_eval 15`
   + `test_model_client 151` + `test_persistence 80` + `test_phase2_readiness 31`
   + `test_primitives 74` + `test_tool_executor 26` = **462**.
3. **Mutation matrix, 8 of 8 killed**, each by exactly one test, working tree
   byte-identical afterwards:
   ```
   A  AC-18  the hook stops relabelling MCP results      -> the MCP provenance test
   B  AC-16  migrations left out of the wheel            -> the wheel test
   C  FR-22  a dependency drifts from requirements.txt   -> the dependency test
   D  NFR-9  the SDK imports mcp                         -> the example-dependency test
   E  NFR-10 an example reads an undocumented variable   -> the README/.env test
   F  NFR-10 .env.example carries a value                -> the README/.env test
   G  AC-17  a script with no offline mode               -> the offline-run test
   H  AC-19  a committable file holds the gateway host   -> the credential scan
   ```
4. **Every example run live**, by hand, from the repository root after
   `pip install -e ".[examples]"`, against the real gateway and database, with
   the rows 04 and 07 wrote deleted afterwards. All eight exit 0 now. That is
   how A3 and A4 below were found; **the gate itself runs the examples only
   offline.**

## Issues the author found during M8

Labels `A` are the author's own and never share a reviewer's label.

| id | finding | disposition |
|---|---|---|
| A1 | mcp 2.2.0 renamed `FastMCP` to `MCPServer` and moved every field to snake_case; the first probe crashed | the example uses the 2.x API; KNOWLEDGE-74c556aa |
| A2 | the README checker piped code through `python -`, which breaks `load_dotenv` at module level | a checker artefact: blocks rerun as saved files, 4 of 4 pass |
| A3 | `04` crashed live: `PostgresTrace.reconstruct` returns the manifest as a positional tuple, unlike the rest of the trace | the example prints the row as it comes; SDK gap recorded, KNOWLEDGE-46ac0348 |
| A4 | a run whose model came from the client default stored `model_id` NULL and manifest `unspecified` | `04` and `07` name the model in live mode; SDK gap recorded, KNOWLEDGE-862c2e9e |
| A5 | a model reply with a character the Windows console cannot encode would crash an example at print time | every example sets `stdout` to replace unencodable characters |
| A6 | a tool cannot see its own run id, so an agent cannot start a child run from inside a tool | `07` delegates between runs and says so |

## Attack these first

- **FR-24 is gated only in half.** Every example must run live and offline;
  AC-17 runs them offline only, and A3 was a live-only crash the gate could not
  see. Is FR-24 satisfied by a manual live run? Owner decision pending on
  whether live checks belong inside regression gates at all.
- **AC-18 reads what the example prints.** The test checks lines the MCP example
  prints about its own run history. Confirm those lines come from
  `InMemorySessionStore.history`, and ask whether a broken example could print
  them anyway.
- **MCP errors keep trusted provenance.** `after_tool` never runs for a failed
  call, so an MCP tool error -- whose text came from the external server -- is
  stored with the executor's own provenance, not `mcp_resource`/`untrusted`.
  The example's docstring says so. Does FR-25 ("relabels their results") hold?
- **The wheel test proves less than a real install.** It builds with
  `--no-build-isolation` against the venv's setuptools 65.5 and installs with
  `--no-deps`. A user building normally gets a current setuptools, where
  `license = { text = "MIT" }` is deprecated in favour of an SPDX string.
  Build it with isolation, if you have network, and say what happens.
- **Two tests are enumerations.** `EXPECTED_EXAMPLES` names the eight files
  (the offline run itself globs every `.py`), and the README check looks for
  keywords. Coverage or fitness?
- **Examples teach the SDK.** Read them as a new user would: does any one teach
  a pattern the SDK documents against, such as building `Persistence` inside
  the event loop, or depend on something private?

## Declared limitations: known, recorded, NOT findings

- The gate runs examples offline only; live runs were manual (see above).
- MCP tool errors keep the executor's provenance (the hook cannot see failures).
- SDK gaps found through M8 and left for an SDK milestone, since M8 adds no
  capability: the positional manifest row (A3), the unrecorded default model
  (A4), a tool that cannot see its run id (A6).
- No native MCP, no orchestration, no streaming: stated in the README and the examples.
- Older `independent-review` gates compute `stale` because the repo hash moved.
- About 994 runs without a manifest are pre-M5 development data.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- Gates are computed, never narrated. Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- Distinguish a defect from a gate blind spot over correct code.
- Leave the database as you found it and say what you removed. Examples write
  runs under tenant `example-tenant`; there are none now, and 994 manifest-less
  runs.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M8-distribution-and-examples --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M8-distribution-and-examples --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found, and your verdict.
