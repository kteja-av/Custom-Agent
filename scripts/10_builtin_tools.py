"""10 - Built-in tools: files confined to a folder, fetch confined to an allowlist.

What it shows
  * read_file_tool, list_directory_tool, glob_tool and grep_tool, bound to one
    root folder: a path that leaves it -- '..', an absolute path, a link that
    points out -- is refused, and the refusal names nothing outside
  * fetch_tool with an allowlist: a host not on it, a private or cloud-metadata
    address, and a non-http scheme are all refused before any connection
  * both kinds of result are ordinary tool results: refusals are errors the
    model reads, never exceptions in your code, and fetched pages are labelled
    untrusted external content
  * nothing is on by default: you pass the tools, and the agent's tool_profile
    decides which of them may run

The model's tool calls are scripted in the first part, in both modes, so every
refusal is shown every time. In live mode a real model then fetches one
allowlisted public page itself.

The file tools use Windows handle APIs in this release and refuse to construct
on other platforms. On macOS and Linux this example says so, shows the fetch
tool only, and still exits 0.

Run it
  python scripts/10_builtin_tools.py            # live: BASE_URL and MODEL_API_KEY from .env, plus internet access
  python scripts/10_builtin_tools.py --offline  # a scripted model: no network, no credentials
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.builtin_tools import fetch_tool, glob_tool, grep_tool, list_directory_tool, read_file_tool
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.session import InMemorySessionStore

PUBLIC_PAGE = "https://example.com/"


class ScriptedModel:
    """Offline stand-in for a real model: asks for the calls below, then answers."""

    def __init__(self, calls):
        self.calls = calls
        self.turn = 0

    async def send(self, request):
        self.turn += 1
        if self.turn == 1:
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=tuple(self.calls)),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(10, 10, 20),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Done: the plan is read; the rest was refused."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 10, 20),
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


def make_folders(base: Path) -> Path:
    root = base / "workspace"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "plan.txt").write_text("Step 1: read the plan.\nStep 2: ship it.\n", encoding="utf-8")
    (root / "notes" / "ideas.txt").write_text("Ship smaller releases.\n", encoding="utf-8")
    (base / "private.txt").write_text("a file outside the workspace\n", encoding="utf-8")
    return root


def results_of(sessions, run_id):
    return {result.tool_call_id: result for message in sessions.history(run_id) for result in message.tool_results}


async def main(offline: bool) -> None:
    config = RunConfig(tenant_id="example-tenant", project_id="examples", max_turns=3)
    windows = sys.platform == "win32"
    with tempfile.TemporaryDirectory() as temporary:
        root = make_folders(Path(temporary))
        # Localhost is allowlisted on purpose, to show that the address check
        # refuses it anyway: it resolves to loopback, which is not public.
        fetch = fetch_tool(["example.com", "localhost"], max_bytes=200_000, timeout_seconds=15)
        tools = [fetch]
        calls = [
            ToolCall(id="not-allowed", name="fetch_url", arguments={"url": "https://evil.example.net/"}),
            ToolCall(id="metadata", name="fetch_url", arguments={"url": "http://169.254.169.254/latest/meta-data/"}),
            ToolCall(id="loopback", name="fetch_url", arguments={"url": "http://localhost/"}),
            ToolCall(id="scheme", name="fetch_url", arguments={"url": "file:///C:/Windows/win.ini"}),
        ]
        if windows:
            # The file tools prepend to the fetch cases so both are shown in one run.
            tools = [read_file_tool(root), list_directory_tool(root), glob_tool(root), grep_tool(root), fetch]
            calls = [
                ToolCall(id="read", name="read_file", arguments={"path": "notes/plan.txt"}),
                ToolCall(id="glob", name="glob_files", arguments={"pattern": "**/*.txt"}),
                ToolCall(id="grep", name="grep_files", arguments={"text": "Ship"}),
                ToolCall(id="dotdot", name="read_file", arguments={"path": "../private.txt"}),
                ToolCall(id="absolute", name="read_file", arguments={"path": str(Path(temporary) / "private.txt")}),
            ] + calls
        else:
            print(
                "file tools require Windows in this release (they confine paths with Windows handle APIs);"
                " showing fetch_tool only."
            )
        names = tuple(tool.name for tool in tools)
        sessions = InMemorySessionStore()
        runner = Runner({"scripted": ScriptedModel(calls)}, tools=tools, session_store=sessions)
        agent = AgentSpec(id="researcher", instructions="Use the tools.", tool_profile=names)
        scripted = await runner.run(agent, "Read the plan and look around.", config)
        results = results_of(sessions, scripted.run_id)
        for call in calls:
            result = results[call.id]
            shown = result.content.replace("\n", " | ")[:110]
            print(f"{call.id:>11}: {'refused' if result.is_error else 'ok     '} {shown}")

        live_fetch = None
        if not offline:
            client = live_client()
            try:
                live_sessions = InMemorySessionStore()
                live_runner = Runner({"model": client}, tools=[fetch], session_store=live_sessions)
                reader = AgentSpec(
                    id="reader",
                    instructions=f"Call fetch_url exactly once with {PUBLIC_PAGE}, then give the page's title in one line.",
                    tool_profile=(fetch.name,),
                )
                live = await live_runner.run(reader, f"What is the title of {PUBLIC_PAGE}?", config)
                fetched = [r for r in results_of(live_sessions, live.run_id).values() if not r.is_error]
                live_fetch = fetched[0] if fetched else None
                print(f"live run: status={live.status.value} answer={(live.output or '')[:100]!r}")
                if live_fetch is not None:
                    p = live_fetch.provenance
                    print(f"  fetched {p.source_uri_or_hash}: origin={p.origin.value} trust_zone={p.trust_zone.value}")
            finally:
                await client.aclose()

    outside = "a file outside the workspace"
    checks = [
        ("the scripted run completed", scripted.status is RunStatus.COMPLETED),
        ("hosts off the allowlist, metadata, loopback and file: were refused",
         all(results[i].is_error for i in ("not-allowed", "metadata", "loopback", "scheme"))),
    ]
    if windows:
        # Matches the Windows-only cases prepended to `calls` above.
        checks = [
            ("the scripted run completed", scripted.status is RunStatus.COMPLETED),
            ("a file inside the root was read", results["read"].content.startswith("Step 1")),
            ("glob found both notes", "notes/plan.txt" in results["glob"].content and "notes/ideas.txt" in results["glob"].content),
            ("grep matched literal text", "notes/ideas.txt:1:" in results["grep"].content),
            ("'..' and an absolute path were refused, disclosing nothing",
             all(results[i].is_error and outside not in results[i].content for i in ("dotdot", "absolute"))),
        ] + checks
    if not offline:
        checks.append((
            f"a real model fetched {PUBLIC_PAGE}, labelled untrusted",
            live_fetch is not None and live_fetch.provenance.trust_zone.value == "untrusted",
        ))
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use a scripted model instead of the gateway")
    asyncio.run(main(parser.parse_args().offline))
