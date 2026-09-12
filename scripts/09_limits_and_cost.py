"""09 - Output limits, honest failures, and what a run cost.

What it shows
  * max_output_tokens: a run cut off at the limit ends FAILED with the error
    "max_tokens" -- never "completed" with half an answer -- and the partial
    text is still on the result
  * cost_usd: every run reports what it cost, from prices YOU put in a
    ModelRegistry; a model with no price reports None, never 0
  * usage detail: cached and reasoning tokens, when the provider reports them
  * the model a run records is the one it used, even when no model was named
    and the client chose it

The prices below are made up and deliberately round, so nobody mistakes them
for a real price list. The SDK ships none: a bundled price goes stale, and a
stale price reports a wrong number rather than an unknown one.

Run it
  python scripts/09_limits_and_cost.py            # live: uses BASE_URL and MODEL_API_KEY from .env
  python scripts/09_limits_and_cost.py --offline  # a scripted model: no network, no credentials
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.registry import ModelCapabilities, ModelEntry, ModelPricing, ModelRegistry, provider_of

OFFLINE_MODEL = "example.small-model"

# Illustrative USD prices per token, NOT any provider's price list: a round $1 per
# million input tokens, $2 per million output tokens and $0.50 per million
# cache-read tokens. Put your provider's current prices here. There is no
# cache-write price, which is fine until a call writes to the cache -- then that
# call's cost is None rather than a guess.
EXAMPLE_PRICES = ModelPricing(
    input="0.000001",
    output="0.000002",
    cache_read="0.0000005",
)


class ScriptedModel:
    """Offline stand-in for a real model.

    It runs out of room the way a real model does when max_output_tokens is
    small, and otherwise answers from a prompt mostly served out of the
    provider's cache. Like the real client, it names its own default model.
    """

    default_model_id = OFFLINE_MODEL

    async def send(self, request):
        limit = request.model_settings.get("max_tokens")
        if limit is not None and limit < 50:
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, content="The sea covers most of the planet and"),
                stop_reason=StopReason.MAX_TOKENS,
                usage=Usage(28, limit, 28 + limit),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Paris."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(2620, 2, 2622, cache_read_tokens=2560),
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


def registry(model_id: str, prices: ModelPricing | None) -> ModelRegistry:
    """A registry holding one model, priced or not."""
    return ModelRegistry(
        [
            ModelEntry(
                provider=provider_of(model_id),
                model_id=model_id,
                model_version="example",
                adapter_version="openai-compatible/1",
                capabilities=ModelCapabilities(max_context_tokens=128_000, pricing=prices),
            )
        ]
    )


async def main(offline: bool) -> None:
    client = ScriptedModel() if offline else live_client()
    model_id = client.default_model_id  # no agent below names a model: the client's default is used
    config = RunConfig(tenant_id="example-tenant", project_id="examples", max_turns=3)
    answerer = AgentSpec(id="answerer", instructions="Answer in one word.")
    try:
        priced = Runner({"model": client}, model_registry=registry(model_id, EXAMPLE_PRICES))

        # 1. Cut off at the output limit.
        essayist = AgentSpec(id="essayist", instructions="Write at length.", max_output_tokens=16)
        cut = await priced.run(essayist, "Write 300 words about the sea.", config)
        print(f"truncated run: status={cut.status.value} error={cut.error}")
        print(f"  the partial text is kept: {cut.output!r}")

        # 2. A priced model.
        answer = await priced.run(answerer, "What is the capital of France?", config)
        used = answer.usage
        print(
            f"priced run: cost_usd={answer.cost_usd} (illustrative prices, not a price list) "
            f"status={answer.status.value} model={model_id}"
        )
        print(
            f"  prompt={used.prompt_tokens} (cache read {used.cache_read_tokens}) "
            f"completion={used.completion_tokens} (reasoning {used.reasoning_tokens})"
        )

        # 3. The same model, registered with no price.
        unpriced = Runner({"model": client}, model_registry=registry(model_id, None))
        bare = await unpriced.run(answerer, "What is the capital of France?", config)
        print(f"unpriced run: cost_usd={bare.cost_usd} status={bare.status.value}")
    finally:
        if not offline:
            await client.aclose()

    checks = (
        ("the cut-off run failed with max_tokens", cut.status is RunStatus.FAILED and cut.error == "max_tokens"),
        ("the priced run completed", answer.status is RunStatus.COMPLETED),
        ("the priced run reports a cost", answer.cost_usd is not None and answer.cost_usd > 0),
        ("the unpriced run reports no cost, not zero", bare.cost_usd is None),
    )
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    failed = [name for name, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use a scripted model instead of the gateway")
    asyncio.run(main(parser.parse_args().offline))
