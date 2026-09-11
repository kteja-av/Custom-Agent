"""08 - Test an agent's behaviour, offline and then against a real model.

What it shows
  * the whole trick: a model client is anything with `async def send`, so a
    scripted one makes an agent's behaviour testable with no network
  * checks written against RunResult events and tool spies, not against wording
  * the same checks run against a real model, where they matter most

The agent under test is a support agent. It may look orders up, but refunds
need a human, so `refund` is registered and NOT in its profile.

To use these checks in pytest, put each in an `async def test_...` function
(pytest-asyncio, already in requirements.txt, runs them) and assert instead of
collecting.

Run it
  python scripts/08_testing_agents_offline.py            # checks against the live model
  python scripts/08_testing_agents_offline.py --offline  # checks against a scripted model
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.tools import Tool, ToolSpec

ORDERS = {"1042": "shipped on Monday, arriving Thursday"}


class Spy:
    """Records every call that actually reached a tool function."""

    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.fn(**kwargs)


def order_schema() -> dict:
    return {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
        "additionalProperties": False,
    }


def build():
    lookup = Spy(lambda order_id: ORDERS.get(order_id, "no such order"))
    refund = Spy(lambda order_id: f"refunded {order_id}")
    tools = [
        Tool(spec=ToolSpec(name="lookup_order", description="Look up an order's status.", input_schema=order_schema()), fn=lookup),
        Tool(spec=ToolSpec(name="refund", description="Refund an order.", input_schema=order_schema()), fn=refund),
    ]
    agent = AgentSpec(
        id="support",
        instructions=(
            "Look the order up before answering. If the customer asks for a refund, try the refund "
            "tool; if it is refused, say a human will follow up."
        ),
        tool_profile=("lookup_order",),  # refund needs a human
    )
    return agent, tools, lookup, refund


class ScriptedModel:
    SCRIPT = [
        ToolCall(id="t1", name="lookup_order", arguments={"order_id": "1042"}),
        ToolCall(id="t2", name="refund", arguments={"order_id": "1042"}),
    ]

    async def send(self, request):
        done = sum(1 for m in request.messages if m.role is Role.TOOL)
        if done < len(self.SCRIPT):
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(self.SCRIPT[done],)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(25, 10, 35),
            )
        return ModelResponse(
            message=Message(
                role=Role.ASSISTANT,
                content="Order 1042 shipped on Monday. I can't refund it myself; a human will follow up.",
            ),
            stop_reason=StopReason.END_TURN,
            usage=Usage(25, 10, 35),
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


def check(results: list, name: str, passed: bool, detail: str = "", required: bool = True) -> None:
    results.append((name, passed, detail, required))


async def main(offline: bool) -> None:
    agent, tools, lookup, refund = build()
    model = ScriptedModel() if offline else live_client()
    try:
        result = await Runner({"model": model}, tools=tools).run(
            agent,
            "Where is order 1042? It is late, please refund it.",
            RunConfig(tenant_id="example-tenant", project_id="examples", max_turns=8),
        )
    finally:
        if not offline:
            await model.aclose()

    calls = [e.payload for e in result.events if e.event_type.value == "ToolCalled"]
    results: list = []
    check(results, "the run completed", result.status is RunStatus.COMPLETED, result.error or "")
    check(results, "lookup_order ran for order 1042", {"order_id": "1042"} in lookup.calls, str(lookup.calls))
    check(results, "the refund function never ran", refund.calls == [], str(refund.calls))
    refused = [c for c in calls if c["name"] == "refund"]
    check(
        results,
        "any refund attempt was refused by permission",
        all(c.get("error_type") == "ToolPermissionDenied" for c in refused),
        str(refused),
    )
    answer = (result.output or "").lower()
    check(
        results,
        "the answer mentions the order status",
        "ship" in answer,
        result.output or "",
        required=offline,  # a real model may phrase it differently
    )

    for name, passed, detail, required in results:
        mark = "PASS" if passed else ("FAIL" if required else "note")
        print(f"[{mark}] {name}" + ("" if passed else f"  ({detail[:80]})"))
    failed = [name for name, passed, _, required in results if required and not passed]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if failed:
        raise SystemExit(f"failed: {failed}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use a scripted model instead of the gateway")
    asyncio.run(main(parser.parse_args().offline))
