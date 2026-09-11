"""02 - Custom tools: schemas, validation, async tools, timeouts and errors.

What it shows
  * the JSON Schema is enforced BEFORE your function runs: a call that does not
    match it is refused, and the function is never invoked
  * async tools are awaited exactly as sync tools are called
  * a tool that raises, or runs past its timeout, becomes an error result the
    model can read and react to -- the run itself keeps going

Every outcome is one of: ran, ToolValidationError, ToolPermissionDenied,
ToolExecutionError (the function raised) or ToolTimeout.

Run it
  python scripts/02_custom_tools.py            # live model, same tools
  python scripts/02_custom_tools.py --offline  # scripted model, every outcome asserted
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import Tool, ToolSpec

PRICES = {"ABC-123": 19.99, "XYZ-789": 5.25}
RATES = {"EUR": 0.92, "INR": 83.10}


def lookup_price(sku: str) -> str:
    if sku not in PRICES:
        raise KeyError(f"no product with sku {sku}")  # -> ToolExecutionError
    return f"{PRICES[sku]:.2f} USD"


async def convert_price(amount: float, currency: str) -> str:
    await asyncio.sleep(0)  # stands in for real async work, such as an HTTP call
    return f"{amount * RATES[currency]:.2f} {currency}"


async def weekly_report() -> str:
    await asyncio.sleep(2)  # longer than the tool's timeout below
    return "never returned: the timeout fires first"


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


TOOLS = [
    Tool(
        spec=ToolSpec(
            name="lookup_price",
            description="Price of a product by SKU, such as ABC-123.",
            input_schema=schema({"sku": {"type": "string", "pattern": "^[A-Z]{3}-[0-9]{3}$"}}),
        ),
        fn=lookup_price,
    ),
    Tool(
        spec=ToolSpec(
            name="convert_price",
            description="Convert a USD amount to EUR or INR.",
            input_schema=schema(
                {
                    "amount": {"type": "number", "minimum": 0},
                    "currency": {"type": "string", "enum": sorted(RATES)},
                }
            ),
        ),
        fn=convert_price,
    ),
    Tool(
        spec=ToolSpec(
            name="weekly_report",
            description="Build the weekly sales report.",
            input_schema=schema({}),
            timeout_seconds=0.2,  # per tool; None disables the timeout
        ),
        fn=weekly_report,
    ),
]

AGENT = AgentSpec(
    id="shop-assistant",
    instructions="Use the tools exactly as asked, one call per turn, then summarise.",
    tool_profile=("lookup_price", "convert_price", "weekly_report"),
)

TASK = (
    "Do these in order, one tool call per turn:\n"
    "1. lookup_price for sku ABC-123\n"
    "2. lookup_price for sku banana, exactly as written\n"
    "3. lookup_price for sku QQQ-000\n"
    "4. convert_price for 19.99 into EUR\n"
    "5. weekly_report\n"
    "Then summarise which calls worked and which failed, and why."
)

# The scripted model performs the same five calls, so every outcome is exercised.
SCRIPT = [
    ToolCall(id="c1", name="lookup_price", arguments={"sku": "ABC-123"}),
    ToolCall(id="c2", name="lookup_price", arguments={"sku": "banana"}),  # fails the pattern
    ToolCall(id="c3", name="lookup_price", arguments={"sku": "QQQ-000"}),  # valid, but raises
    ToolCall(id="c4", name="convert_price", arguments={"amount": 19.99, "currency": "EUR"}),
    ToolCall(id="c5", name="weekly_report", arguments={}),  # times out
]
EXPECTED = [None, "ToolValidationError", "ToolExecutionError", None, "ToolTimeout"]


class ScriptedModel:
    async def send(self, request):
        done = sum(1 for m in request.messages if m.role is Role.TOOL)
        if done < len(SCRIPT):
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(SCRIPT[done],)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(30, 10, 40),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Two calls worked and three failed."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(30, 10, 40),
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
    model = ScriptedModel() if offline else live_client()
    store = InMemorySessionStore()  # passed in so we can read what the model was told
    runner = Runner({"model": model}, tools=TOOLS, session_store=store)
    try:
        result = await runner.run(
            AGENT, TASK, RunConfig(tenant_id="example-tenant", project_id="examples", max_turns=12)
        )
    finally:
        if not offline:
            await model.aclose()

    told = {r.tool_call_id: r.content for m in store.history(result.run_id) for r in m.tool_results}
    outcomes = []
    print(f"{'tool':<14} {'outcome':<20} what the model was told")
    for event in result.events:
        if event.event_type.value != "ToolCalled":
            continue
        payload = event.payload
        outcome = payload.get("error_type") if payload["is_error"] else None
        outcomes.append(outcome)
        print(f"{payload['name']:<14} {outcome or 'ran':<20} {told.get(payload['tool_call_id'], '')[:70]}")
    print(f"\nstatus: {result.status.value}\nanswer: {result.output}")

    if result.status is not RunStatus.COMPLETED:
        raise SystemExit(f"the run did not complete: {result.error}")
    if offline and outcomes != EXPECTED:
        raise SystemExit(f"expected outcomes {EXPECTED}, got {outcomes}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use a scripted model instead of the gateway")
    asyncio.run(main(parser.parse_args().offline))
