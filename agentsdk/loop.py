"""The agent loop (FR-1, FR-14, LLD 3.10).

send -> check tool calls -> execute -> append -> repeat, until the model stops
asking for tools or max_turns is reached.

Two things this deliberately does NOT do:
  - It does not fail the run when a tool fails. A denied or invalid tool call is
    a normal turn outcome; the error goes back to the model as a tool result it
    can react to (LLD 4.2, 4.3).
  - It does not raise on max_turns. That is a defined terminal state, reported
    as status, never an exception reaching application code (FR-14, AC-8).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .context import ContextAssembler
from .errors import ModelError, ModelRateLimited, ModelTimeout
from .events import EventSink, EventType
from .executor import ToolExecutor
from .hooks import HookAction, RuntimeHook
from .identity import PrincipalContext
from .model import ModelClient, ModelResponse, Usage
from .outcomes import Completed, Failed
from .primitives import Message, Role
from .tools import ToolRegistry


@dataclass(frozen=True)
class LoopOutcome:
    """What the loop produced, before Runner turns it into a RunResult."""

    output: str | None
    usage: Usage
    turns: int
    exhausted_turns: bool = False
    error: str | None = None


class AgentLoop:
    def __init__(
        self,
        *,
        model_client: ModelClient,
        session_store: Any,
        tool_executor: ToolExecutor,
        tool_registry: ToolRegistry,
        event_sink: EventSink,
        assembler: ContextAssembler | None = None,
        hook: RuntimeHook | None = None,
        max_model_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        self._model = model_client
        self._sessions = session_store
        self._executor = tool_executor
        self._registry = tool_registry
        self._events = event_sink
        self._assembler = assembler or ContextAssembler()
        self._hook = hook or RuntimeHook()
        self._max_model_retries = max_model_retries
        self._retry_backoff = retry_backoff_seconds

    async def run(
        self,
        run_id: str,
        task: str,
        *,
        max_turns: int,
        instructions: str | None = None,
        model_settings: dict[str, Any] | None = None,
        principal_context: PrincipalContext | None = None,
    ) -> LoopOutcome:
        self._sessions.append(run_id, Message(role=Role.USER, content=task))
        usage = Usage()

        for turn in range(1, max_turns + 1):
            history = self._sessions.history(run_id)
            request = self._assembler.build(
                history,
                self._registry.schemas(),
                instructions=instructions,
                model_settings=model_settings,
            )

            before = self._hook.before_model(request)
            if before.action is HookAction.HALT:
                return LoopOutcome(None, usage, turn, error=before.reason or "halted by hook")
            if before.action is HookAction.MODIFY and before.replacement is not None:
                request = before.replacement

            try:
                response = await self._send_with_retry(request)
            except ModelError as exc:
                # Retries are exhausted or the error is not transient. The run
                # fails; it does not raise past Runner.
                return LoopOutcome(
                    None, usage, turn, error=f"{type(exc).__name__}: {exc}"
                )

            usage = usage + response.usage
            after = self._hook.after_model(response)
            if after.action is HookAction.HALT:
                return LoopOutcome(None, usage, turn, error=after.reason or "halted by hook")
            if after.action is HookAction.MODIFY and after.replacement is not None:
                response = after.replacement

            self._sessions.append(run_id, response.message)
            self._events.emit(
                EventType.MODEL_CALLED,
                {
                    "turn": turn,
                    "stop_reason": response.stop_reason.value,
                    "tool_calls": [call.name for call in response.tool_calls],
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    },
                    "provider_response_id": response.provider_response_id,
                },
            )

            if not response.tool_calls:
                return LoopOutcome(response.message.content, usage, turn)

            results = []
            for call in response.tool_calls:
                outcome = await self._executor.execute(call, principal_context)
                if isinstance(outcome, (Completed, Failed)):
                    results.append(outcome.result)
                else:  # pragma: no cover - unreachable until Phase 4
                    raise AssertionError(
                        f"Phase 0 ToolExecutor returned {type(outcome).__name__}; "
                        "only Completed and Failed are reachable"
                    )
            self._sessions.append(run_id, Message(role=Role.TOOL, tool_results=tuple(results)))

        return LoopOutcome(None, usage, max_turns, exhausted_turns=True)

    async def _send_with_retry(self, request: Any) -> ModelResponse:
        """LLD 4.5: retry timeouts and rate limits only.

        The adapter has its own RetryPolicy; this is the loop-level backstop for
        a client that does not retry, and it deliberately retries the same two
        transient classes and nothing else. No side effect has occurred at this
        point, so replaying the request is safe -- which stops being true once
        Phase 6 tracks execution attempts near real side effects.
        """
        delay = self._retry_backoff
        for attempt in range(self._max_model_retries + 1):
            try:
                return await self._model.send(request)
            except (ModelTimeout, ModelRateLimited):
                if attempt == self._max_model_retries:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover
