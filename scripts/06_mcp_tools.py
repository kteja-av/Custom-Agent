"""06 - Use tools from an MCP (Model Context Protocol) server.

The SDK has no native MCP support -- that arrives in Phase 4. This example
shows what you can do today: connect to an MCP server yourself, turn each tool
it offers into an ordinary SDK Tool, and mark every result as untrusted.

That last step matters. The SDK's executor records every tool result as a
trusted internal tool, which is wrong for text that came from an external
server: an MCP tool can return anything, including instructions aimed at the
model. A RuntimeHook replaces the provenance of those results with
origin=mcp_resource, trust_zone=untrusted and instruction_authority=data_only,
so the run's history says truthfully where the content came from.

The MCP server here is a tiny one in this same file, started over stdio.
Point StdioServerParameters at any other MCP server to use its tools instead.

Requires the MCP library (2.x API):
  python -m pip install -r scripts/requirements.txt

Run it
  python scripts/06_mcp_tools.py            # live model, real local MCP server
  python scripts/06_mcp_tools.py --offline  # scripted model, real local MCP server
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SERVER_NAME = "glossary"


def serve() -> None:
    """The demo MCP server. Runs in a child process; never imports the SDK."""
    from mcp.server.mcpserver import MCPServer

    app = MCPServer(SERVER_NAME, log_level="WARNING")
    glossary = {
        "agent": "a program that uses a model to decide which tools to call",
        "tool": "a function an agent may call, described by a JSON Schema",
    }

    @app.tool()
    def define(word: str) -> str:
        """Define a word from the glossary."""
        return glossary.get(word.lower(), f"no definition for {word!r}")

    @app.tool()
    def word_count(text: str) -> str:
        """Count the words in a piece of text."""
        return str(len(text.split()))

    app.run()  # stdio


if "--serve" in sys.argv:  # before any SDK import: the server process needs none
    serve()
    raise SystemExit(0)


from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from agentsdk import (  # noqa: E402
    AgentSpec,
    ContentProvenance,
    InstructionAuthority,
    Message,
    Origin,
    Role,
    RunConfig,
    Runner,
    RunStatus,
    TaintFlag,
    ToolCall,
    ToolResult,
    TrustZone,
)
from agentsdk.hooks import CONTINUE, HookAction, HookOutcome, RuntimeHook  # noqa: E402
from agentsdk.model import ModelResponse, StopReason, Usage  # noqa: E402
from agentsdk.session import InMemorySessionStore  # noqa: E402
from agentsdk.tools import Tool, ToolSpec  # noqa: E402


def bridge(session: ClientSession, name: str):
    """An SDK tool function that forwards the call to the MCP server."""

    async def call(**arguments):
        result = await session.call_tool(name, arguments)
        text = "\n".join(c.text for c in result.content if getattr(c, "text", None) is not None)
        if result.is_error:
            raise RuntimeError(text or f"MCP tool {name} failed")  # -> ToolExecutionError
        return text

    return call


async def tools_from(session: ClientSession) -> list[Tool]:
    listed = await session.list_tools()
    return [
        Tool(
            spec=ToolSpec(
                name=remote.name,
                description=remote.description or remote.name,
                input_schema=remote.input_schema,  # MCP tools are described by JSON Schema too
            ),
            fn=bridge(session, remote.name),
        )
        for remote in listed.tools
    ]


class MarkMCPResultsUntrusted(RuntimeHook):
    """Relabels the provenance of every successful result from a bridged tool.

    after_tool only receives the result, so before_tool remembers which call ids
    belong to MCP tools. (A call that fails never reaches after_tool; its error
    result keeps the executor's own provenance.)
    """

    def __init__(self, tool_names: set[str], server: str):
        self._names, self._server, self._calls = tool_names, server, {}

    def before_tool(self, tool_call):
        if tool_call.name in self._names:
            self._calls[tool_call.id] = tool_call.name
        return CONTINUE

    def after_tool(self, result):
        name = self._calls.pop(result.tool_call_id, None)
        if name is None:
            return CONTINUE
        untrusted = ContentProvenance(
            origin=Origin.MCP_RESOURCE,
            instruction_authority=InstructionAuthority.DATA_ONLY,
            trust_zone=TrustZone.UNTRUSTED,
            taint_flags={TaintFlag.EXTERNAL_CONTENT, TaintFlag.PROMPT_INJECTION_RISK},
            source_uri_or_hash=f"mcp://{self._server}/{name}",
        )
        return HookOutcome(
            action=HookAction.MODIFY,
            replacement=ToolResult(
                tool_call_id=result.tool_call_id,
                content=result.content,
                provenance=untrusted,
                is_error=result.is_error,
            ),
        )


class ScriptedModel:
    SCRIPT = [
        ToolCall(id="mcp-1", name="define", arguments={"word": "agent"}),
        ToolCall(id="mcp-2", name="word_count", arguments={"text": "tools extend what an agent can do"}),
    ]

    async def send(self, request):
        done = sum(1 for m in request.messages if m.role is Role.TOOL)
        if done < len(self.SCRIPT):
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(self.SCRIPT[done],)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(20, 10, 30),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="An agent picks tools with a model; that sentence has 6 words."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(20, 10, 30),
        )


def live_client():
    from dotenv import load_dotenv

    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    load_dotenv()
    settings = Settings.from_env(load_dotfile=False)
    return OpenAICompatibleModelClient(
        base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model
    )


async def main(offline: bool) -> None:
    server = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve()), "--serve"])
    model = ScriptedModel() if offline else live_client()
    try:
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await tools_from(session)
                names = {t.spec.name for t in tools}
                print(f"MCP server offered: {', '.join(sorted(names))}")

                store = InMemorySessionStore()
                runner = Runner(
                    {"model": model},
                    tools=tools,
                    hook=MarkMCPResultsUntrusted(names, SERVER_NAME),
                    session_store=store,
                )
                agent = AgentSpec(
                    id="glossary-reader",
                    instructions="Use the glossary tools, one call per turn, then answer in one sentence.",
                    tool_profile=tuple(sorted(names)),
                )
                result = await runner.run(
                    agent,
                    "Define 'agent', then count the words in 'tools extend what an agent can do'.",
                    RunConfig(tenant_id="example-tenant", project_id="examples", max_turns=8),
                )
    finally:
        if not offline:
            await model.aclose()

    history = store.history(result.run_id)
    names_by_call = {c.id: c.name for m in history for c in m.tool_calls}
    for message in history:
        for tool_result in message.tool_results:
            p = tool_result.provenance
            print(f"result from {names_by_call.get(tool_result.tool_call_id)}: {tool_result.content!r}")
            print(
                f"provenance: tool={names_by_call.get(tool_result.tool_call_id)} origin={p.origin.value} "
                f"trust_zone={p.trust_zone.value} instruction_authority={p.instruction_authority.value}"
            )
    print(f"\nstatus: {result.status.value}\nanswer: {result.output}")
    if result.status is not RunStatus.COMPLETED:
        raise SystemExit(f"the run did not complete: {result.error}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use a scripted model instead of the gateway")
    asyncio.run(main(parser.parse_args().offline))
