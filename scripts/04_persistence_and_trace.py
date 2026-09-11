"""04 - Persist a run to PostgreSQL and reconstruct it afterwards.

What it shows
  * Persistence.postgres(dsn) is the whole opt-in: runs, messages, events and
    an execution manifest are written as the run happens
  * PostgresTrace rebuilds the run, in order, from those rows
  * every row is scoped to a tenant and project: another tenant cannot read it
  * without --offline this needs DATABASE_URL (and the gateway) in .env

Build Persistence ONCE, at process start and outside the event loop: it creates
or migrates the schema with blocking DDL under a database-wide lock.

Run it
  python scripts/04_persistence_and_trace.py            # live: gateway + PostgreSQL
  python scripts/04_persistence_and_trace.py --offline  # in memory, no database, no network
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import os

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import Tool, ToolSpec

NOTES: list[str] = []


def save_note(text: str) -> str:
    NOTES.append(text)
    return f"saved note #{len(NOTES)}"


TOOLS = [
    Tool(
        spec=ToolSpec(
            name="save_note",
            description="Save a short note.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        ),
        fn=save_note,
    )
]
AGENT = AgentSpec(
    id="note-taker",
    instructions="Save the note you are given with save_note, then confirm in one sentence.",
    tool_profile=("save_note",),
)
TASK = "Save a note that says: renew the TLS certificate on Friday."
CONFIG = RunConfig(tenant_id="example-tenant", project_id="examples")


class ScriptedModel:
    async def send(self, request):
        if not any(m.role is Role.TOOL for m in request.messages):
            call = ToolCall(id="c1", name="save_note", arguments={"text": "renew the TLS certificate on Friday"})
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(call,)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(20, 10, 30),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Saved your note."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(20, 10, 30),
        )


def live_client():
    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    settings = Settings.from_env(load_dotfile=False)
    return OpenAICompatibleModelClient(
        base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model
    )


async def run(model, agent=AGENT, **runner_options):
    try:
        return await Runner({"model": model}, tools=TOOLS, **runner_options).run(agent, TASK, CONFIG)
    finally:
        if hasattr(model, "aclose"):
            await model.aclose()


def offline() -> None:
    store = InMemorySessionStore()
    result = asyncio.run(run(ScriptedModel(), session_store=store))
    print(f"status: {result.status.value}\n")
    print("history, in order:")
    for n, message in enumerate(store.history(result.run_id), 1):
        detail = message.content or ", ".join(c.name for c in message.tool_calls) or "; ".join(
            r.content for r in message.tool_results
        )
        print(f"  {n}. {message.role.value:<9} {detail}")
    print("\nevents:", [e.event_type.value for e in result.events])
    print("\nIn memory, this history lives only as long as the process. Without --offline, the")
    print("same run is written to PostgreSQL and rebuilt from the database afterwards.")
    if result.status is not RunStatus.COMPLETED:
        raise SystemExit(f"the run did not complete: {result.error}")


def live() -> None:
    from dotenv import load_dotenv

    from agentsdk import Persistence
    from agentsdk.config import normalise_database_url
    from agentsdk.postgres import PostgresRunStore, PostgresTrace, RunScope, close_pools

    load_dotenv()
    dsn = normalise_database_url(os.environ["DATABASE_URL"])
    persistence = Persistence.postgres(dsn)  # once, before the event loop starts
    try:
        from agentsdk.config import Settings

        # Name the model on the agent. Left to the client's default, the run row
        # and manifest cannot say which model produced the run.
        model_id = Settings.from_env(load_dotfile=False).default_model
        agent = dataclasses.replace(AGENT, preferred_model=f"model:{model_id}")
        result = asyncio.run(run(live_client(), agent, persistence=persistence))
        scope = RunScope(run_id=result.run_id, tenant_id=CONFIG.tenant_id, project_id=CONFIG.project_id)
        trace = PostgresTrace(dsn).reconstruct(scope)

        run_row = trace["run"]
        print(f"run      {run_row['run_id']}  status={run_row['status']}  model={run_row['model_id']}")
        print("messages")
        for m in trace["messages"]:
            print(f"  #{m['sequence_no']:<3} {m['role']:<9} {(m['content'] or '')[:60]}")
        print("events")
        for e in trace["events"]:
            print(f"  #{e['sequence_no']:<3} {e['event_type']}")
        # reconstruct returns the manifest as a positional row, unlike the dicts
        # above: (sdk_version, agent_spec_hash, instructions_hash, model_id,
        # model_version, model_adapter_version, tool_spec_hashes, policy_version).
        # A known gap in the SDK, printed here as it comes.
        manifest = trace["manifest"]
        print(f"manifest recorded: {manifest is not None}")
        print(f"manifest row: {manifest}")

        other = PostgresRunStore(dsn).get_run(RunScope(run_id=result.run_id, tenant_id="another-tenant", project_id="examples"))
        print(f"\nthe same run id, asked for by another tenant: {other}")
        if result.status is not RunStatus.COMPLETED:
            raise SystemExit(f"the run did not complete: {result.error}")
    finally:
        close_pools()


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="run in memory with a scripted model")
    offline() if parser.parse_args().offline else live()
