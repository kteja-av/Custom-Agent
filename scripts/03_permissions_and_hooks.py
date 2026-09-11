"""03 - Permissions and runtime hooks.

What it shows
  * a custom PermissionChecker: the allowlist, plus a rule about the ARGUMENTS
    (writes only inside sandbox/)
  * a RuntimeHook that rewrites a call before it runs (MODIFY) and redacts a
    result before the model sees it
  * the order every tool call goes through, which decides what a hook can do:

        validate arguments -> permission check -> before_tool hook
          -> execute -> after_tool hook

    A hook runs AFTER the permission check, so it can refuse or rewrite a call
    that was allowed, but it can never turn a denied call into an allowed one.

Run it
  python scripts/03_permissions_and_hooks.py            # live model
  python scripts/03_permissions_and_hooks.py --offline  # scripted model, every outcome asserted
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import dataclasses
import re

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.hooks import CONTINUE, HookAction, HookOutcome, RuntimeHook
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.permissions import AllowlistPermissionChecker, Decision, PermissionResult
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import Tool, ToolSpec

FILES = {"sandbox/notes.txt": "Call the supplier. Their number is 555-0142."}
PHONE = re.compile(r"\b\d{3}-\d{4}\b")


def read_file(path: str) -> str:
    return FILES[path]


def write_file(path: str, text: str) -> str:
    FILES[path] = text
    return f"wrote {len(text)} characters to {path}"


def delete_file(path: str) -> str:
    FILES.pop(path, None)
    return f"deleted {path}"


def path_schema(**extra: dict) -> dict:
    properties = {"path": {"type": "string"}, **extra}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TOOLS = [
    Tool(spec=ToolSpec(name="read_file", description="Read a file.", input_schema=path_schema()), fn=read_file),
    Tool(
        spec=ToolSpec(name="write_file", description="Write a file.", input_schema=path_schema(text={"type": "string"})),
        fn=write_file,
    ),
    Tool(spec=ToolSpec(name="delete_file", description="Delete a file.", input_schema=path_schema()), fn=delete_file),
]


class SandboxOnly:
    """A PermissionChecker is anything with this `check` method."""

    def __init__(self, allowed: set[str]):
        self._allowlist = AllowlistPermissionChecker(allowed)

    def check(self, tool_call, principal_context=None):
        decision = self._allowlist.check(tool_call, principal_context)
        if not decision.allowed:
            return decision
        path = str(tool_call.arguments.get("path", ""))
        if tool_call.name == "write_file" and not path.startswith("sandbox/"):
            return PermissionResult(Decision.DENY, f"writes are only allowed inside sandbox/, not {path!r}")
        return decision


class TidyAndRedact(RuntimeHook):
    """Subclass RuntimeHook and override only the points you need."""

    def __init__(self):
        self.model_calls = 0

    def before_model(self, request):
        self.model_calls += 1
        return CONTINUE

    def before_tool(self, tool_call):
        path = tool_call.arguments.get("path")
        if isinstance(path, str) and path.startswith("./"):
            tidied = dataclasses.replace(tool_call, arguments={**tool_call.arguments, "path": path[2:]})
            return HookOutcome(action=HookAction.MODIFY, replacement=tidied)
        return CONTINUE

    def after_tool(self, result):
        redacted = PHONE.sub("[phone redacted]", result.content)
        if redacted != result.content:
            return HookOutcome(action=HookAction.MODIFY, replacement=dataclasses.replace(result, content=redacted))
        return CONTINUE


AGENT = AgentSpec(
    id="file-clerk",
    instructions="Use the file tools exactly as asked, one call per turn, then report what happened.",
    tool_profile=("read_file", "write_file"),
    permission_policy=SandboxOnly({"read_file", "write_file"}),  # replaces the default allowlist
)

TASK = (
    "One tool call per turn:\n"
    "1. read_file ./sandbox/notes.txt\n"
    "2. write_file sandbox/summary.txt with the text 'call the supplier'\n"
    "3. write_file etc/passwd with the text 'x'\n"
    "4. delete_file sandbox/notes.txt\n"
    "Then report which calls were allowed."
)

SCRIPT = [
    ToolCall(id="c1", name="read_file", arguments={"path": "./sandbox/notes.txt"}),  # hook tidies the path
    ToolCall(id="c2", name="write_file", arguments={"path": "sandbox/summary.txt", "text": "call the supplier"}),
    ToolCall(id="c3", name="write_file", arguments={"path": "etc/passwd", "text": "x"}),  # SandboxOnly refuses
    ToolCall(id="c4", name="delete_file", arguments={"path": "sandbox/notes.txt"}),  # not in the allowlist
]
EXPECTED = [None, None, "ToolPermissionDenied", "ToolPermissionDenied"]


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
            message=Message(role=Role.ASSISTANT, content="The read and the sandbox write were allowed."),
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
    hook = TidyAndRedact()
    store = InMemorySessionStore()
    runner = Runner({"model": model}, tools=TOOLS, hook=hook, session_store=store)
    try:
        result = await runner.run(
            AGENT, TASK, RunConfig(tenant_id="example-tenant", project_id="examples", max_turns=10)
        )
    finally:
        if not offline:
            await model.aclose()

    told = {r.tool_call_id: r.content for m in store.history(result.run_id) for r in m.tool_results}
    outcomes = []
    for event in result.events:
        if event.event_type.value == "ToolCalled":
            payload = event.payload
            outcome = payload.get("error_type") if payload["is_error"] else None
            outcomes.append(outcome)
            print(f"{payload['name']:<12} {outcome or 'ran':<22} model saw: {told.get(payload['tool_call_id'], '')[:60]}")
    print(f"\nmodel calls seen by the hook: {hook.model_calls}")
    print(f"files now: {sorted(FILES)}")
    print(f"answer: {result.output}")

    if result.status is not RunStatus.COMPLETED:
        raise SystemExit(f"the run did not complete: {result.error}")
    if offline:
        if outcomes != EXPECTED:
            raise SystemExit(f"expected outcomes {EXPECTED}, got {outcomes}")
        if "etc/passwd" in FILES or "sandbox/notes.txt" not in FILES:
            raise SystemExit("a denied call changed the files")
        if "555-0142" in told.get("c1", "") or "[phone redacted]" not in told.get("c1", ""):
            raise SystemExit("the after_tool hook did not redact what the model saw")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use a scripted model instead of the gateway")
    asyncio.run(main(parser.parse_args().offline))
