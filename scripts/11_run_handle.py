"""11 - Run handles: watch a run's events as they happen, and cancel a run mid-flight.

What it shows
  * Runner.start() returns a RunHandle at once, and the run goes on without you
  * handle.events() streams every event in sequence_no order as it is recorded,
    and ends after the run's terminal event
  * handle.state() is a snapshot you can take at any time: running, turns, usage
  * handle.cancel() stops a run while its model call is in flight: the run ends
    `cancelled`, records a RunCancelled event, and result() returns it like any
    other outcome
  * events() called after a run has ended replays it from its first event

Run it
  python scripts/11_run_handle.py            # live: BASE_URL and MODEL_API_KEY from .env
  python scripts/11_run_handle.py --offline  # a scripted model: no network, no credentials
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import Tool, ToolSpec


def current_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


TOOLS = [
    Tool(
        spec=ToolSpec(
            name="current_date",
            description="Today's date in UTC, as YYYY-MM-DD.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        fn=current_date,
    )
]
PLANNER = AgentSpec(
    id="planner",
    instructions="Call current_date once, then say what day it is in one sentence.",
    tool_profile=("current_date",),
)
ESSAYIST = AgentSpec(id="essayist", instructions="Answer at length, in several paragraphs.")
CONFIG = RunConfig(tenant_id="example-tenant", project_id="examples", max_turns=4)


class ScriptedModel:
    """Offline stand-in for a real model: asks for the date, then answers."""

    async def send(self, request):
        if not any(m.role is Role.TOOL for m in request.messages):
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(ToolCall(id="d1", name="current_date", arguments={}),)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(20, 10, 30),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Today is the day the tool reported."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(20, 10, 30),
        )


class StalledModel:
    """Offline stand-in for a slow model call: it does not answer by itself."""

    async def send(self, request):
        await asyncio.sleep(3600)
        raise AssertionError("the run was not cancelled")


class Watched:
    """Wraps a model client and says when a model call has started."""

    def __init__(self, client):
        self.client = client
        self.entered = asyncio.Event()

    async def send(self, request):
        self.entered.set()
        return await self.client.send(request)

    def __getattr__(self, name):
        return getattr(self.client, name)


def live_client():
    from dotenv import load_dotenv

    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    load_dotenv()
    settings = Settings.from_env(load_dotfile=False)
    return OpenAICompatibleModelClient(
        base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model
    )


async def stream_a_run(model):
    runner = Runner({"model": model}, tools=TOOLS, session_store=InMemorySessionStore())
    handle = await runner.start(PLANNER, "What is the date today?", CONFIG)
    print(f"first run started ({handle.run_id}); its events, as they are recorded:")
    streamed = []
    async for event in handle.events():
        streamed.append(event)
        state = handle.state()
        print(f"  #{event.sequence_no:<2} {event.event_type.value:<13} running={state.running} turns={state.turns}")
    result = await handle.result()
    print(f"first run: status={result.status.value} output={(result.output or '')[:80]!r}\n")
    return streamed, result


async def cancel_a_run(model):
    watched = Watched(model)
    runner = Runner({"model": watched}, session_store=InMemorySessionStore())
    handle = await runner.start(ESSAYIST, "Write a long essay on the history of timekeeping.", CONFIG)
    print(f"second run started ({handle.run_id})")

    # Wait until the model call is in flight, or the run has ended without one.
    entered = asyncio.ensure_future(watched.entered.wait())
    finished = asyncio.ensure_future(handle.result())
    await asyncio.wait({entered, finished}, return_when=asyncio.FIRST_COMPLETED)
    entered.cancel()
    in_flight = watched.entered.is_set() and not finished.done()
    if in_flight:
        state = handle.state()
        print(f"  its model call is in flight (running={state.running}); cancelling")
        handle.cancel("the user pressed stop")

    result = await finished
    state = handle.state()
    print(f"second run: status={result.status.value} running={state.running} turns={state.turns}")
    replayed = [event async for event in handle.events()]
    print("  its events, replayed after it ended:", [event.event_type.value for event in replayed])
    return in_flight, replayed, result, state


async def main(offline: bool) -> None:
    if offline:
        streamed, first = await stream_a_run(ScriptedModel())
        in_flight, replayed, second, state = await cancel_a_run(StalledModel())
    else:
        client = live_client()
        try:
            streamed, first = await stream_a_run(client)
            in_flight, replayed, second, state = await cancel_a_run(client)
        finally:
            await client.aclose()

    kinds = [event.event_type.value for event in streamed]
    checks = [
        ("the first run completed", first.status is RunStatus.COMPLETED),
        ("its stream was numbered 1..n, in order",
         [event.sequence_no for event in streamed] == list(range(1, len(streamed) + 1))),
        ("its stream began with RunStarted and ended with RunCompleted",
         bool(kinds) and kinds[0] == "RunStarted" and kinds[-1] == "RunCompleted"),
        ("the stream held exactly the run's recorded events",
         [event.event_id for event in streamed] == [event.event_id for event in first.events]),
        ("the second run was cancelled while its model call was in flight",
         in_flight and second.status is RunStatus.CANCELLED),
        ("its events end with RunCancelled",
         bool(replayed) and replayed[0].event_type.value == "RunStarted" and replayed[-1].event_type.value == "RunCancelled"),
        ("its state says it is no longer running, and cancelled",
         not state.running and state.status is RunStatus.CANCELLED),
    ]
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use scripted models instead of the gateway")
    asyncio.run(main(parser.parse_args().offline))
