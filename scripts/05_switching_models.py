"""05 - Switch models without changing the agent.

What it shows
  * a Runner can hold several model clients, each under a key
  * AgentSpec.preferred_model picks one as "<client key>:<model id>"
  * RunConfig.model_override picks another for a single run
  * the model actually used is recorded on the RunStarted event

Change FAST and CAREFUL below to model ids your endpoint serves.

Run it
  python scripts/05_switching_models.py            # live: two models through BASE_URL
  python scripts/05_switching_models.py --offline  # two scripted models
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.registry import default_registry

FAST = "openai.gpt-4o-mini"
CAREFUL = "bedrock.anthropic.claude-haiku-4-5"

AGENT = AgentSpec(
    id="explainer",
    instructions="Answer in one sentence.",
    preferred_model=f"fast:{FAST}",
)
TASK = "In one sentence, what is a tool-using agent?"


class ScriptedModel:
    def __init__(self, name: str):
        self.name = name

    async def send(self, request):
        model_id = request.model_settings.get("model")
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content=f"answered by the {self.name} client as {model_id}"),
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 10, 20),
        )


def live_clients():
    from dotenv import load_dotenv

    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    load_dotenv()
    settings = Settings.from_env(load_dotfile=False)

    def client(model: str):
        return OpenAICompatibleModelClient(base_url=settings.base_url, api_key=settings.api_key, model=model)

    return {"fast": client(FAST), "careful": client(CAREFUL)}


def describe(model_id: str) -> str:
    entry = default_registry().resolve(model_id)
    if entry is None:
        return "not in the model registry"
    return f"{entry.provider}, {entry.capabilities.max_context_tokens} context tokens"


async def main(offline: bool) -> None:
    clients = {"fast": ScriptedModel("fast"), "careful": ScriptedModel("careful")} if offline else live_clients()
    runner = Runner(clients)
    runs = {
        "preferred model": RunConfig(tenant_id="example-tenant", project_id="examples"),
        "overridden for one run": RunConfig(
            tenant_id="example-tenant", project_id="examples", model_override=f"careful:{CAREFUL}"
        ),
    }
    used = {}
    try:
        for label, config in runs.items():
            result = await runner.run(AGENT, TASK, config)
            started = next(e.payload for e in result.events if e.event_type.value == "RunStarted")
            used[label] = (started["provider"], started["model"])
            print(f"{label}")
            print(f"  client {started['provider']!r}, model {started['model']} ({describe(started['model'])})")
            print(f"  -> {result.output}\n")
            if result.status is not RunStatus.COMPLETED:
                raise SystemExit(f"{label}: the run did not complete: {result.error}")
    finally:
        if not offline:
            for client in clients.values():
                await client.aclose()

    if used["preferred model"] != ("fast", FAST) or used["overridden for one run"] != ("careful", CAREFUL):
        raise SystemExit(f"the wrong model was used: {used}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use scripted models instead of the gateway")
    asyncio.run(main(parser.parse_args().offline))
