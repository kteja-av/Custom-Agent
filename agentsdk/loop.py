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
from .errors import ModelError, describe_exception
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
    ) -> None:
        self._model = model_client
        self._sessions = session_store
        self._executor = tool_executor
        self._registry = tool_registry
        self._events = event_sink
        self._assembler = assembler if assembler is not None else ContextAssembler()
        self._hook = hook if hook is not None else RuntimeHook()

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
        # Store calls go to a worker thread (FR-20). SessionStore and
        # EventSink are synchronous protocols and stay that way: making them
        # async would REPLACE a contract NFR-7 says to extend, and would
        # rewrite 94 call sites across the approved suites. Moving the
        # blocking off the loop needs neither.
        #
        # The connection pool alone was not enough, which is worth stating
        # because it very nearly looked like it was. Pooled, a store call
        # costs about a millisecond -- but it is still a millisecond ON the
        # loop, and with the pool and no thread the worst stall measured
        # 51-57 ms against NFR-8's 50 ms bound, failing 5 runs out of 5.
        # With the offload the same measurement is 13-16 ms.
        await asyncio.to_thread(
            self._sessions.append, run_id, Message(role=Role.USER, content=task)
        )
        usage = Usage()

        for turn in range(1, max_turns + 1):
            history = await asyncio.to_thread(self._sessions.history, run_id)
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
                response = await self._model.send(request)
            except ModelError as exc:
                # The client's own retries are exhausted, or the error is not
                # transient. The run fails; it does not raise past Runner.
                return LoopOutcome(None, usage, turn, error=describe_exception(exc))

            usage = usage + response.usage
            after = self._hook.after_model(response)
            if after.action is HookAction.HALT:
                return LoopOutcome(None, usage, turn, error=after.reason or "halted by hook")
            if after.action is HookAction.MODIFY and after.replacement is not None:
                response = after.replacement

            await asyncio.to_thread(self._sessions.append, run_id, response.message)
            await asyncio.to_thread(
                self._events.emit,
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
            await asyncio.to_thread(
                self._sessions.append,
                run_id,
                Message(role=Role.TOOL, tool_results=tuple(results)),
            )

        return LoopOutcome(None, usage, max_turns, exhausted_turns=True)

    # NOTE ON RETRY (FR-15): the loop does not retry.
    #
    # It used to, as a "backstop" for a client that does not retry -- but the
    # adapter retries too, and the two layers multiplied: 3 outer attempts times
    # 3 inner ones meant 9 HTTP calls where FR-15 permits 3. Neither layer knew
    # about the other and both were on by default.
    #
    # Retry belongs to the ModelClient, because that is where ModelTimeout and
    # ModelRateLimited are classified in the first place and where FR-15's
    # numbers live. A client that chooses not to retry is making a policy
    # decision the loop must not silently override.
