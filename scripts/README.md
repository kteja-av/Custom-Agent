# Examples

Runnable examples, one activity each. Every one runs two ways:

```bash
python scripts/01_minimal_agent.py            # live: uses BASE_URL and MODEL_API_KEY from .env
python scripts/01_minimal_agent.py --offline  # a scripted model: no network, no credentials
```

Offline mode is not a toy: it runs the real SDK code path with a scripted model
in place of a real one, and several examples assert on every outcome. The test
suite runs every one of them offline, from outside the repository, with no
credentials.

| script | what it shows |
|---|---|
| [`01_minimal_agent.py`](01_minimal_agent.py) | a tool, an agent, a run, a result |
| [`02_custom_tools.py`](02_custom_tools.py) | JSON Schema validation, async tools, timeouts, a tool that raises |
| [`03_permissions_and_hooks.py`](03_permissions_and_hooks.py) | a custom permission checker, hooks that rewrite calls and redact results |
| [`04_persistence_and_trace.py`](04_persistence_and_trace.py) | PostgreSQL persistence, trace reconstruction, tenant isolation |
| [`05_switching_models.py`](05_switching_models.py) | several model clients, a preferred model, a per-run override |
| [`06_mcp_tools.py`](06_mcp_tools.py) | bridging tools from an MCP server, with results marked untrusted |
| [`07_delegating_to_a_child_run.py`](07_delegating_to_a_child_run.py) | a child run that records its parent |
| [`08_testing_agents_offline.py`](08_testing_agents_offline.py) | behavioural checks against a scripted model, then a real one |
| [`09_limits_and_cost.py`](09_limits_and_cost.py) | an output limit that fails a cut-off run honestly, and what a run cost from prices you supply |
| [`10_builtin_tools.py`](10_builtin_tools.py) | built-in file tools confined to a folder, and a fetch tool confined to an allowlist and the public internet |
| [`11_run_handle.py`](11_run_handle.py) | a run's events streamed through its handle as they happen, and a second run cancelled mid-flight |
| [`12_artifacts.py`](12_artifacts.py) | an artifact put, read back and checked against its hash, kept within its tenant, expired and deleted |

## Setup

From the repository root:

```bash
python -m pip install -e ".[examples]"        # the SDK, plus the MCP library for 06
# or: python -m pip install -r requirements.txt -r scripts/requirements.txt
cp .env.example .env                           # then fill it in, for live mode
```

Examples 04, 07 and 12 need `DATABASE_URL` in live mode; the rest need only the
model endpoint.

## Two things the examples do by hand, and say so

- **MCP (06).** The SDK has no native MCP support yet (Phase 4). The example
  connects to the server itself and relabels every result's provenance as
  untrusted, because the SDK would otherwise record it as a trusted internal
  tool.
- **Delegation (07).** There is no orchestrator yet (Phase 2), and a tool cannot
  see its own run id, so a child run is started by your code between runs, not
  by an agent from inside a tool.

Example 10's live mode also needs internet access, to fetch one public page. Its
file tools use Windows handle APIs in this release and refuse to construct on
other platforms.

The prices in 09 are illustrative. The SDK ships no price list: a bundled price
goes stale, and a stale price reports a wrong number rather than an unknown one.
