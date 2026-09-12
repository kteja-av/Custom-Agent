# Custom Agent SDK

A Python SDK for building tool-using AI agents on your own terms: no vendor agent
framework underneath, every tool call validated and permission-checked before it
runs, every result tagged with where it came from, and every run optionally
persisted so you can reconstruct exactly what happened afterwards.

> **Status: `0.1.0.dev0`, pre-release.** Phase 0 (the single-agent foundation) and
> a store-hardening milestone for Phase 2 are complete, each approved by an
> independent review; the suite has 734 tests. It is not on PyPI yet, and a lot is
> deliberately not built -- see [What it does not do yet](#what-it-does-not-do-yet).

## What it does

- **Runs one agent through a tool-calling loop** until it answers, fails, or
  runs out of turns. Runtime failures (a tool that raises, a model that returns
  nonsense, a hook that breaks) end as a status on the result, never as an
  exception in your code. Configuration mistakes still raise, at the call site.
- **Guards every tool call in a fixed order**: validate the arguments against
  the tool's JSON Schema, then check permission, then run it. A call that fails
  either check never reaches your function, and the model is told why.
- **Lets you intercept** model calls and tool calls with runtime hooks that can
  continue, modify or reject.
- **Tags every tool result with provenance**: its origin, trust zone and
  instruction authority, carried as metadata beside the prompt rather than as
  text the model can be talked out of.
- **Talks to any OpenAI-compatible chat completions API** over plain HTTP, with
  retries for timeouts and rate limits and credentials that refuse to appear in
  logs, reprs, stored rows or model context. Verified against OpenAI and
  Anthropic (via Bedrock) models through a LiteLLM gateway.
- **Is honest about how a run ended and what it cost.** A response cut off at
  the output-token limit, or stopped by a content filter, ends the run failed
  with that reason instead of passing half an answer off as complete, and none
  of its tool calls run. Every run reports its token usage, cached and reasoning
  tokens included, and its cost in USD from prices you supply; a model with no
  price reports `None`, never 0. The output limit and reasoning effort can be set
  per agent or per run.
- **Persists runs to PostgreSQL, if you want it**: runs, messages, events and a
  per-run manifest of the exact configuration, every row scoped to a tenant and
  project, reconstructable in order, with each run's usage and cost. Schema
  changes ship as versioned, checksummed migrations. Connections are pooled and
  validated, store calls stay off the event loop, concurrent writers to one run
  all commit, and a run can record the run that spawned it.
- **Works entirely in memory** when you don't pass a database.

## What it does not do yet

Stated plainly, because an SDK that overstates itself costs you a week:

| not built | where it lands |
|---|---|
| Multi-agent orchestration, planning, DAGs, replanning | Phase 2. `RunConfig.parent_run_id` records lineage today; nothing orchestrates |
| Streaming of tokens or run progress | Phase 2 |
| Native MCP (Model Context Protocol) support | Phase 4 |
| Human approval workflows | Phase 4. The approval step exists and auto-allows |
| Sandboxed tool execution | Phase 5 |
| Pause, resume, cancel; durable interruptions | Phase 6 |
| Context compaction (the full history is sent every turn) | Phase 7 |
| Budget enforcement (cost is measured and recorded, never limited); retention and partitioning of stored rows | Phase 2 / Phase 8 |
| A second wire format (for example Anthropic's native Messages API) | Deferred by choice; the contract was checked against it |
| Enforcing structured output | Phase 2. The slot exists and is unused |

## Requirements

- Python 3.11 (the version everything was tested on)
- An OpenAI-compatible API endpoint and key
- PostgreSQL 16, only for persistence and the full test suite

## Install

```bash
git clone https://github.com/siva-rgb/Custom-Agent.git
cd Custom-Agent
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt     # macOS / Linux
```

Or install it as a package, together with what the examples need:

```bash
.venv/Scripts/python -m pip install -e ".[examples]"
```

The runtime dependencies are `httpx`, `jsonschema`, `psycopg[binary]`,
`psycopg_pool` and `python-dotenv`. No vendor agent SDK is required, and none is
allowed.

## Configure

```bash
cp .env.example .env
```

| variable | what it is |
|---|---|
| `BASE_URL` | root of the API, **without** `/v1` -- the client calls `{BASE_URL}/v1/chat/completions` |
| `MODEL_API_KEY` | bearer token for that API |
| `DATABASE_URL` | `postgresql://user:password@host:5432/dbname`, only for persistence |
| `DEFAULT_MODEL` | model id used when an agent names none (default `openai.gpt-4o-mini`) |

## Quickstart: an agent with a tool

Save as `quickstart.py` in the repository root and run it.

```python
import asyncio

from agentsdk import AgentSpec, RunConfig, Runner
from agentsdk.config import Settings
from agentsdk.providers import OpenAICompatibleModelClient
from agentsdk.tools import Tool, ToolSpec


def add(a: int, b: int) -> str:
    return str(a + b)


add_tool = Tool(
    spec=ToolSpec(
        name="add",
        description="Add two whole numbers.",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    ),
    fn=add,
)


async def main() -> None:
    settings = Settings.from_env()  # BASE_URL, MODEL_API_KEY, DEFAULT_MODEL from .env
    client = OpenAICompatibleModelClient(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.default_model,
    )
    runner = Runner({"gw": client}, tools=[add_tool])
    agent = AgentSpec(
        id="calculator",
        instructions="Use the add tool for arithmetic.",
        preferred_model=f"gw:{settings.default_model}",
        tool_profile=("add",),  # the tools this agent is allowed to call
    )
    try:
        result = await runner.run(
            agent, "What is 17 + 25?", RunConfig(tenant_id="demo-tenant", project_id="demo")
        )
    finally:
        await client.aclose()

    print(result.status.value, "|", result.output)
    print("tokens used:", result.usage.total_tokens)


asyncio.run(main())
```

`tool_profile` is an allowlist, and an empty one allows nothing. A tool that is
registered but not in the profile is refused, and the model receives the refusal
as an error result it can respond to.

## Test an agent without a network

Anything with `async def send(request) -> ModelResponse` is a model client, so a
scripted one makes agent logic testable offline and deterministically.

```python
import asyncio

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.tools import Tool, ToolSpec


class ScriptedModel:
    async def send(self, request):
        tool_results = [r for m in request.messages for r in m.tool_results]
        if not tool_results:
            call = ToolCall(id="call-1", name="shout", arguments={"text": "hello"})
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(10, 5, 15),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content=f"The tool said {tool_results[-1].content}"),
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5, 15),
        )


shout = Tool(
    spec=ToolSpec(
        name="shout",
        description="Upper-case some text.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    fn=lambda text: text.upper(),
)


async def main() -> None:
    runner = Runner({"scripted": ScriptedModel()}, tools=[shout])
    agent = AgentSpec(id="shouter", instructions="Shout.", tool_profile=("shout",))
    result = await runner.run(
        agent, "Say hello loudly", RunConfig(tenant_id="demo-tenant", project_id="demo")
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "The tool said HELLO"
    print([event.event_type.value for event in result.events])


asyncio.run(main())
```

## Intercept tool calls with a hook

Permission decides whether an agent *may* call a tool; a hook can still refuse a
particular call. Here `delete_file` is in the profile, so the allowlist passes
it, and the hook rejects it.

```python
import asyncio

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, ToolCall
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.tools import Tool, ToolSpec


class FrozenDeletes(RuntimeHook):
    def before_tool(self, tool_call):
        if tool_call.name == "delete_file":
            return HookOutcome(action=HookAction.REJECT, reason="deletes are frozen today")
        return super().before_tool(tool_call)


class TriesToDelete:
    async def send(self, request):
        if not any(m.role is Role.TOOL for m in request.messages):
            call = ToolCall(id="call-1", name="delete_file", arguments={"path": "report.txt"})
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(1, 1, 2),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Understood, I left the file alone."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(1, 1, 2),
        )


deleted = []
delete_file = Tool(
    spec=ToolSpec(
        name="delete_file",
        description="Delete a file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    fn=lambda path: deleted.append(path) or "deleted",
)


async def main() -> None:
    runner = Runner({"m": TriesToDelete()}, tools=[delete_file], hook=FrozenDeletes())
    agent = AgentSpec(id="cleaner", instructions="Tidy up.", tool_profile=("delete_file",))
    result = await runner.run(agent, "Delete report.txt", RunConfig(tenant_id="demo-tenant", project_id="demo"))

    tool_events = [e.payload for e in result.events if e.event_type.value == "ToolCalled"]
    print(tool_events)  # is_error True, error_type ToolPermissionDenied
    assert deleted == [], "the tool must never have run"
    print(result.output)


asyncio.run(main())
```

## Persist and reconstruct a run

Build `Persistence` once, at process start and outside the event loop: it
creates or migrates the schema with blocking DDL.

```python
import asyncio
import os

from dotenv import load_dotenv

from agentsdk import AgentSpec, Message, Persistence, Role, RunConfig, Runner
from agentsdk.config import normalise_database_url
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.postgres import PostgresTrace, RunScope


class OneLiner:
    async def send(self, request):
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="recorded"),
            stop_reason=StopReason.END_TURN,
            usage=Usage(1, 1, 2),
        )


load_dotenv()
dsn = normalise_database_url(os.environ["DATABASE_URL"])
persistence = Persistence.postgres(dsn)  # once per process
config = RunConfig(tenant_id="demo-tenant", project_id="demo")


async def main():
    runner = Runner({"m": OneLiner()}, persistence=persistence)
    return await runner.run(AgentSpec(id="recorder", instructions="Reply."), "hi", config)


result = asyncio.run(main())
trace = PostgresTrace(dsn).reconstruct(
    RunScope(run_id=result.run_id, tenant_id=config.tenant_id, project_id=config.project_id)
)
print(trace["run"]["status"], "|", len(trace["messages"]), "messages |", len(trace["events"]), "events")
print("configuration manifest recorded:", trace["manifest"] is not None)
```

## Examples

[`scripts/`](scripts/) holds runnable examples, one activity each. Every one runs
live, or with `--offline` using a scripted model with no network and no
credentials -- which is also how the test suite checks them.

| script | what it shows |
|---|---|
| [`01_minimal_agent.py`](scripts/01_minimal_agent.py) | a tool, an agent, a run, a result |
| [`02_custom_tools.py`](scripts/02_custom_tools.py) | JSON Schema validation, async tools, timeouts, a tool that raises |
| [`03_permissions_and_hooks.py`](scripts/03_permissions_and_hooks.py) | a custom permission checker; hooks that rewrite calls and redact results |
| [`04_persistence_and_trace.py`](scripts/04_persistence_and_trace.py) | PostgreSQL persistence, trace reconstruction, tenant isolation |
| [`05_switching_models.py`](scripts/05_switching_models.py) | several model clients, a preferred model, a per-run override |
| [`06_mcp_tools.py`](scripts/06_mcp_tools.py) | tools from an MCP server, bridged by hand, results marked untrusted |
| [`07_delegating_to_a_child_run.py`](scripts/07_delegating_to_a_child_run.py) | a child run that records its parent |
| [`08_testing_agents_offline.py`](scripts/08_testing_agents_offline.py) | behavioural checks against a scripted model, then a real one |
| [`09_limits_and_cost.py`](scripts/09_limits_and_cost.py) | an output limit that fails a cut-off run honestly, and what a run cost from prices you supply |

```bash
python scripts/02_custom_tools.py --offline
```

MCP and delegation are bridged or recorded by hand in those examples, because
the SDK does not do either natively yet; each example says so. See
[`scripts/README.md`](scripts/README.md).

## The public API

Driving a run needs only the package root: `Runner`, `AgentSpec`, `RunConfig`,
`RunResult`, `RunStatus`, `ReasoningEffort` and `Persistence`, plus the
primitives (`Message`, `Role`, `ToolCall`, `ToolResult`, `ContentProvenance` and
its enums) and the error types.

Defining tools, model clients and prices currently means importing from
submodules: `agentsdk.tools` (`Tool`, `ToolSpec`), `agentsdk.providers`
(`OpenAICompatibleModelClient`), `agentsdk.registry` (`ModelRegistry`,
`ModelPricing`), `agentsdk.hooks`, `agentsdk.model` and `agentsdk.postgres`.
Narrowing that is a known, recorded gap.

## Run the tests

```bash
.venv/Scripts/python -m pytest -q
```

The full suite (734 tests) runs against a **real database and the live gateway**,
including an evaluation that calls two real models and spends tokens. Tests that
need configuration fail rather than skip when it is missing, on purpose: a test
suite that skips to green proves nothing.

## How it was built

Specification first, with every change gated. [`SPEC.md`](SPEC.md) holds the
requirements and acceptance criteria. `.genesis/` holds the decisions, recorded
knowledge, test evidence and the verdicts of independent reviews, each run in a
separate session that did not write the code. Milestones were approved only
after those reviews passed: the persistence milestone took nine rounds, the live
evaluation three, and the store hardening three.

## Project layout

```
agentsdk/
  api.py            Runner, AgentSpec, RunConfig, RunResult
  loop.py           the agent loop
  executor.py       validate -> permission -> hook -> execute -> provenance
  tools.py          Tool, ToolSpec, ToolRegistry
  permissions.py    permission checkers
  hooks.py          runtime hooks
  model.py          ModelRequest, ModelResponse, Usage, the ModelClient protocol
  registry.py       ModelRegistry, ModelPricing and cost
  providers/        the OpenAI-compatible HTTP client
  primitives.py     Message, ToolCall, ToolResult, ContentProvenance
  persistence.py    Persistence
  postgres.py       Postgres stores, pool and trace
  migrate.py        versioned migrations
  schema.sql        the baseline schema
  migrations/       numbered schema changes
scripts/            runnable examples, live or --offline
tests/              the test suite
SPEC.md             the specification
.genesis/           decisions, knowledge, evidence and review verdicts
```

## License

[MIT](LICENSE)
