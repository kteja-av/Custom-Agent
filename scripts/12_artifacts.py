"""12 - Artifacts: store what a run produced, read it back, and keep it in its scope.

What it shows
  * put() stores bytes with a checked description -- MIME type, SHA-256, the
    creating agent, the run it came from, provenance -- and returns an
    ArtifactRef whose uri names neither a file path nor a credential
  * get() takes the id and checks the content against its hash before returning
    it; metadata() returns the description without the content
  * a store is bound to one tenant and project: another tenant asking for the
    same id gets ArtifactNotFound, exactly as for an id that never existed, and
    cannot name this tenant's run as its source
  * an artifact past its expiry is not found at once, and expire() removes it
  * delete() removes an artifact; there is no update

Build Persistence ONCE, at process start and outside the event loop: it creates
or migrates the schema with blocking DDL under a database-wide lock.

Run it
  python scripts/12_artifacts.py            # live: gateway + PostgreSQL (DATABASE_URL in .env)
  python scripts/12_artifacts.py --offline  # in memory, a scripted model: no database, no network
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from agentsdk import (
    AgentSpec,
    ArtifactNotFound,
    ContentProvenance,
    InMemoryArtifactStore,
    InstructionAuthority,
    Message,
    Origin,
    Role,
    RunConfig,
    Runner,
    RunStatus,
    TrustZone,
)
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.session import InMemorySessionStore

TENANT, PROJECT = "example-tenant", "examples"
CONFIG = RunConfig(tenant_id=TENANT, project_id=PROJECT)
AGENT = AgentSpec(
    id="report-writer",
    instructions="Write a two-line status report for a software project. Reply with the report only.",
)
TASK = "Write today's status report."
# A model wrote the report: data to read, not instructions to follow.
WRITTEN_BY_A_MODEL = ContentProvenance(
    origin=Origin.MODEL, instruction_authority=InstructionAuthority.DATA_ONLY, trust_zone=TrustZone.UNTRUSTED
)


class ScriptedModel:
    """Offline stand-in for a real model."""

    async def send(self, request):
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Status: green.\nNext: ship the release on Friday."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(20, 10, 30),
        )


class Clock:
    """A clock the script moves forward, so expiry needs no waiting."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now


def live_client():
    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    settings = Settings.from_env(load_dotfile=False)
    return OpenAICompatibleModelClient(
        base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model
    )


async def not_found(call):
    """What a store call raised, if it was ArtifactNotFound; None if it returned."""
    try:
        await call
    except ArtifactNotFound as error:
        return f"{type(error).__name__}: {error}"
    return None


async def demonstrate(store, run, clock):
    other_tenant = store.for_scope("another-tenant", PROJECT)
    report = (run.output or "").encode("utf-8")

    ref = await store.put(
        report,
        mime_type="text/plain",
        provenance=WRITTEN_BY_A_MODEL,
        created_by_agent=AGENT.id,
        source_run=run.run_id,
        classification="internal",
    )
    print(f"put       {ref.uri}")
    print(f"          {ref.size} bytes, sha256 {ref.content_hash[:16]}..., from run {ref.source_run}")
    content = await store.get(ref.artifact_id)
    print(f"get       {content.decode('utf-8')[:80]!r}")
    described = await store.metadata(ref.artifact_id)
    print(f"metadata  mime_type={described.mime_type} created_by_agent={described.created_by_agent}"
          f" trust_zone={described.provenance.trust_zone.value}")

    from_other_tenant = await not_found(other_tenant.get(ref.artifact_id))
    never_existed = await not_found(store.get(str(uuid.uuid4())))
    print(f"\nanother tenant, the same id: {from_other_tenant}")
    print(f"an id that never existed:    {never_existed}")
    try:
        await other_tenant.put(b"x", mime_type="text/plain", provenance=WRITTEN_BY_A_MODEL,
                               created_by_agent="intruder", source_run=run.run_id)
        foreign_source = None
    except ValueError as error:
        foreign_source = str(error)
    print(f"another tenant naming this run as a source: refused ({foreign_source})")

    short_lived = await store.put(b"a draft", mime_type="text/plain", provenance=WRITTEN_BY_A_MODEL,
                                  created_by_agent=AGENT.id, expires_at=clock.now + timedelta(hours=1))
    clock.now += timedelta(hours=2)
    expired = await not_found(store.metadata(short_lived.artifact_id))
    removed = await store.expire()
    print(f"\ntwo hours later, an artifact that expired after one: {expired}")
    print(f"expire() removed {removed}")

    await store.delete(ref.artifact_id)
    deleted = await not_found(store.get(ref.artifact_id))
    print(f"after delete(): {deleted}\n")

    return [
        ("the run completed", run.status is RunStatus.COMPLETED),
        ("the uri names only the artifact id", ref.uri == f"urn:agentsdk:artifact:{ref.artifact_id}"),
        ("get() returned exactly the bytes put", content == report),
        ("metadata() described the artifact and its source run",
         described.content_hash == ref.content_hash and described.source_run == run.run_id),
        ("another tenant got ArtifactNotFound, as for an id that never existed",
         from_other_tenant is not None and never_existed is not None),
        ("another tenant could not name this run as its source", foreign_source is not None),
        ("an expired artifact was not found, and expire() removed it", expired is not None and removed == 1),
        ("a deleted artifact was not found", deleted is not None),
    ]


async def offline():
    runner = Runner({"scripted": ScriptedModel()}, session_store=InMemorySessionStore())
    run = await runner.run(AGENT, TASK, CONFIG)
    # The in-memory store knows which runs exist only through this callable.
    known_runs = {(TENANT, PROJECT, run.run_id)}
    clock = Clock()
    store = InMemoryArtifactStore(
        TENANT, PROJECT, runs=lambda tenant, project, run_id: (tenant, project, run_id) in known_runs, clock=clock
    )
    return await demonstrate(store, run, clock)


def live():
    from dotenv import load_dotenv

    from agentsdk import Persistence
    from agentsdk.config import normalise_database_url
    from agentsdk.postgres import close_pools

    load_dotenv()
    dsn = normalise_database_url(os.environ["DATABASE_URL"])
    persistence = Persistence.postgres(dsn)  # once, before the event loop starts

    async def go():
        client = live_client()
        try:
            run = await Runner({"model": client}, persistence=persistence).run(AGENT, TASK, CONFIG)
        finally:
            await client.aclose()
        clock = Clock()
        return await demonstrate(persistence.artifact_store(TENANT, PROJECT, clock=clock), run, clock)

    try:
        return asyncio.run(go())
    finally:
        close_pools()


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="run in memory with a scripted model")
    checks = asyncio.run(offline()) if parser.parse_args().offline else live()
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")
