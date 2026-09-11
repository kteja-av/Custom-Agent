"""01 - A minimal agent: one tool, one question, one answer.

What it shows
  * a Tool is a JSON Schema plus a plain Python function
  * an AgentSpec lists the tools the agent may call (tool_profile)
  * Runner.run returns a RunResult; a runtime failure comes back as a status,
    never as an exception in your code

Run it
  python scripts/01_minimal_agent.py            # live: BASE_URL and MODEL_API_KEY from .env
  python scripts/01_minimal_agent.py --offline  # a scripted model, no network
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.tools import Tool, ToolSpec


def add(a: int, b: int) -> str:
    return str(a + b)


ADD = Tool(
    spec=ToolSpec(
        name="add",
        description="Add two whole numbers and return the sum.",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    ),
    fn=add,
)

AGENT = AgentSpec(
    id="calculator",
    instructions="Use the add tool for any arithmetic, then answer in one short sentence.",
    tool_profile=("add",),
)


class ScriptedModel:
    """Stands in for a real model: asks for the tool once, then answers with its result.

    Anything with `async def send(request) -> ModelResponse` is a model client.
    """

    async def send(self, request):
        results = [r for m in request.messages for r in m.tool_results]
        if not results:
            call = ToolCall(id="call-1", name="add", arguments={"a": 17, "b": 25})
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(20, 10, 30),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content=f"17 + 25 is {results[-1].content}."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(20, 10, 30),
        )


def live_client():
    from dotenv import load_dotenv

    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    load_dotenv()  # searches upward from this file, so the repository's .env is found
    settings = Settings.from_env(load_dotfile=False)
    return OpenAICompatibleModelClient(
        base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model
    )


async def main(offline: bool) -> None:
    model = ScriptedModel() if offline else live_client()
    runner = Runner({"model": model}, tools=[ADD])
    try:
        result = await runner.run(
            AGENT,
            "What is 17 + 25?",
            RunConfig(tenant_id="example-tenant", project_id="examples"),
        )
    finally:
        if not offline:
            await model.aclose()

    print(f"status : {result.status.value}")
    print(f"answer : {result.output}")
    for event in result.events:
        if event.event_type.value == "ToolCalled":
            print(f"tool   : {event.payload['name']} (error={event.payload['is_error']})")
    print(f"tokens : {result.usage.total_tokens}")
    if result.status is not RunStatus.COMPLETED:
        raise SystemExit(f"the run did not complete: {result.error}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use a scripted model instead of the gateway")
    asyncio.run(main(parser.parse_args().offline))
