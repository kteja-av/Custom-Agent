"""07 - Delegate work to a child run and record the lineage.

What it shows
  * a parent run produces a subtask; your code starts a child run for it with
    RunConfig(parent_run_id=...)
  * with persistence, the child's row records its parent, and a parent in
    ANOTHER tenant is refused -- lineage never crosses tenants
  * parent_run_id is checked when the RunConfig is built, not first at the
    database

What it does not show, because it does not exist yet
  The SDK has no orchestrator: that is Phase 2. A tool running inside a run
  cannot see its own run id, so an agent cannot start a child run from inside
  a tool call. Delegation happens between runs, driven by your code, as here.

Run it
  python scripts/07_delegating_to_a_child_run.py            # live: gateway + PostgreSQL
  python scripts/07_delegating_to_a_child_run.py --offline  # in memory, no database, no network
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import os
import uuid

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus
from agentsdk.model import ModelResponse, StopReason, Usage

PLANNER = AgentSpec(
    id="planner",
    instructions="Reply with exactly one short subtask for a worker to do, and nothing else.",
)
WORKER = AgentSpec(id="worker", instructions="Do the subtask you are given, in one or two sentences.")
TENANT, PROJECT = "example-tenant", "examples"


class ScriptedModel:
    async def send(self, request):
        first = request.messages[0].content or ""
        content = (
            "Write a two-line status update about the certificate renewal."
            if "release" in first
            else "Certificate renewal: scheduled for Friday. No blockers."
        )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content=content),
            stop_reason=StopReason.END_TURN,
            usage=Usage(15, 10, 25),
        )


def live_client():
    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    settings = Settings.from_env(load_dotfile=False)
    return OpenAICompatibleModelClient(
        base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model
    )


def named(spec: AgentSpec, model_ref: str | None) -> AgentSpec:
    """The agent with its model named, so a persisted run records which model ran."""
    return spec if model_ref is None else dataclasses.replace(spec, preferred_model=model_ref)


async def delegate(runner: Runner, model_ref: str | None = None):
    parent = await runner.run(
        named(PLANNER, model_ref), "Plan the next step for the release.", RunConfig(tenant_id=TENANT, project_id=PROJECT)
    )
    subtask = parent.output or ""
    child = await runner.run(
        named(WORKER, model_ref), subtask, RunConfig(tenant_id=TENANT, project_id=PROJECT, parent_run_id=parent.run_id)
    )
    return parent, subtask, child


def report(parent, subtask, child) -> None:
    print(f"parent run {parent.run_id}  {parent.status.value}")
    print(f"  subtask: {subtask}")
    print(f"child run  {child.run_id}  {child.status.value}  (parent_run_id={parent.run_id})")
    print(f"  result:  {child.output}")
    if parent.status is not RunStatus.COMPLETED or child.status is not RunStatus.COMPLETED:
        raise SystemExit(f"a run did not complete: {parent.error or child.error}")


def show_config_refusal() -> None:
    try:
        RunConfig(tenant_id=TENANT, project_id=PROJECT, parent_run_id="not-a-run-id")
    except ValueError as exc:
        print(f"\nRunConfig refuses a malformed parent at construction: {exc}")
    else:
        raise SystemExit("RunConfig accepted a malformed parent_run_id")


def offline() -> None:
    parent, subtask, child = asyncio.run(delegate(Runner({"model": ScriptedModel()})))
    report(parent, subtask, child)
    show_config_refusal()
    print("\nIn memory, nothing stores the lineage. Without --offline, the child's row in")
    print("PostgreSQL records parent_run_id, and the example reads it back.")


def live() -> None:
    from dotenv import load_dotenv

    from agentsdk import Persistence
    from agentsdk.config import Settings, normalise_database_url
    from agentsdk.postgres import PostgresRunStore, RunScope, close_pools

    load_dotenv()
    dsn = normalise_database_url(os.environ["DATABASE_URL"])
    persistence = Persistence.postgres(dsn)

    async def both():
        model = live_client()
        try:
            runner = Runner({"model": model}, persistence=persistence)
            model_ref = f"model:{Settings.from_env(load_dotfile=False).default_model}"
            parent, subtask, child = await delegate(runner, model_ref)
            intruder = await runner.run(
                named(WORKER, model_ref),
                "Try to attach to someone else's parent.",
                RunConfig(tenant_id="another-tenant", project_id=PROJECT, parent_run_id=parent.run_id),
            )
            return parent, subtask, child, intruder
        finally:
            await model.aclose()

    try:
        parent, subtask, child, intruder = asyncio.run(both())
        report(parent, subtask, child)
        row = PostgresRunStore(dsn).get_run(RunScope(run_id=child.run_id, tenant_id=TENANT, project_id=PROJECT))
        print(f"\nthe child's stored row says parent_run_id = {row['parent_run_id']}")
        print(f"a run in another tenant naming that parent: {intruder.status.value} -- {intruder.error}")
        if str(row["parent_run_id"]) != parent.run_id:
            raise SystemExit("the child row does not record its parent")
        show_config_refusal()
    finally:
        close_pools()


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="run in memory with a scripted model")
    offline() if parser.parse_args().offline else live()
