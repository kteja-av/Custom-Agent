"""The agent loop (FR-1, FR-14, FR-26, LLD 3.10).

send -> check tool calls -> execute -> append -> repeat, until the model stops
asking for tools or max_turns is reached.

Two things this deliberately does NOT do:
  - It does not fail the run when a tool fails. A denied or invalid tool call is
    a normal turn outcome; the error goes back to the model as a tool result it
    can react to (LLD 4.2, 4.3).
  - It does not raise on max_turns. That is a defined terminal state, reported
    as status, never an exception reaching application code (FR-14, AC-8).

And one thing it does that it once did not: a response that hit the
output-token limit, or was stopped by a content filter, ends the run failed
(FR-26). Reporting it as completed passed half an answer off as a whole one,
and ran tool calls whose argument lists may have been cut off.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .context import ContextAssembler
from .errors import ModelError, describe_exception
from .events import EventSink, EventType
from .executor import ToolExecutor
from .hooks import HookAction, RuntimeHook
from .identity import PrincipalContext
from .model import ModelClient, ModelRequest, ModelResponse, StopReason, Usage
from .outcomes import Completed, Failed
from .primitives import Message, Role
from .registry import add_costs
from .tools import ToolRegistry

# FR-26. A response that stopped for one of these reasons is not the answer the
# model would have given, and the run fails with the reason as its error. Cut
# off at the output limit, its call list or arguments may be truncated -- and
# arguments that decode prove nothing: at the limit gpt-4o-mini returned
# undecodable ones and Claude Haiku ones that decoded (KNOWLEDGE-312441cb).
# Filtered, it is not the model's answer at all (decision D1). An unrecognised
# stop reason is not in this table and ends the run as it always did.
_UNFINISHED = {
    StopReason.MAX_TOKENS: "max_tokens",
    StopReason.CONTENT_FILTER: "content_filter",
}


@dataclass
class RunMeter:
    """What a run has consumed: the one account of its usage and cost (FR-30).

    Recorded the moment a model call returns, before any hook, store write or
    event can fail, and read by the Runner on every exit, the failure path
    included. M9 round 1 rebuilt a failed run's totals from its ModelCalled
    events instead, and those were written after the after_model hook and the
    session append: a failure in that window dropped billed calls, and a run the
    provider billed 0.036 reported 0.006 (rejected, R2). Events are telemetry;
    this is the account.
    """

    # What the run costs if it makes no model call at all: zero on a priced
    # model, None on an unpriced one. Used for that case only. Once a call is
    # made its own price decides, so a hook that moves a run onto a priced model
    # is costed by that model rather than by the one it left.
    no_call_cost: Decimal | None = None
    usage: Usage = field(default_factory=Usage)
    call_costs: list[Decimal | None] = field(default_factory=list)

    def record(self, usage: Usage, cost: Decimal | None) -> None:
        self.usage = self.usage + usage
        self.call_costs.append(cost)

    @property
    def cost_usd(self) -> Decimal | None:
        if not self.call_costs:
            return self.no_call_cost
        total: Decimal | None = Decimal(0)
        for cost in self.call_costs:
            total = add_costs(total, cost)
        return total


@dataclass(frozen=True)
class LoopOutcome:
    """What the loop produced, before Runner turns it into a RunResult."""

    output: str | None
    usage: Usage
    turns: int
    exhausted_turns: bool = False
    error: str | None = None
    # None when unknown: no way to price, or a call nothing could price (FR-30).
    cost_usd: Decimal | None = None


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
        cost_of: Callable[[ModelRequest, Usage], Decimal | None] | None = None,
        meter: RunMeter | None = None,
    ) -> None:
        self._model = model_client
        self._sessions = session_store
        self._executor = tool_executor
        self._registry = tool_registry
        self._events = event_sink
        self._assembler = assembler if assembler is not None else ContextAssembler()
        self._hook = hook if hook is not None else RuntimeHook()
        # How to price one call: the Runner knows the registry, the loop does not.
        self._cost_of = cost_of
        # The Runner's meter, so the account survives an exception that leaves
        # this loop before it can return an outcome.
        self._meter = meter

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
        # The connection pool alone was not enough. Pooled, a store call costs
        # about a millisecond, but it is still a millisecond ON the loop and it
        # scales with fan-out: pool only, 24 concurrent runs stalled the loop a
        # median 62 ms (3 of 6 samples over NFR-8's 50 ms); offloaded, 14 ms.
        # An earlier version of this comment said six runs failed 5 of 5
        # without the offload. That did not reproduce (median 16 ms), and the
        # tests that actually guard this are not timed -- see
        # test_no_store_call_runs_on_the_event_loop_thread_on_any_run_path.
        meter = self._meter if self._meter is not None else RunMeter()
        await asyncio.to_thread(
            self._sessions.append, run_id, Message(role=Role.USER, content=task)
        )

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
                return _outcome(meter, None, turn, error=before.reason or "halted by hook")
            if before.action is HookAction.MODIFY and before.replacement is not None:
                request = before.replacement

            try:
                response = await self._model.send(request)
            except ModelError as exc:
                # The client's own retries are exhausted, or the error is not
                # transient. The run fails; it does not raise past Runner.
                return _outcome(meter, None, turn, error=describe_exception(exc))

            # The provider has billed this call, whatever happens next. So the
            # account is recorded first and the event second, both before any
            # caller code or store write that could fail (R2). A hook may
            # rewrite what the model said, but it cannot un-spend the tokens.
            spent = response.usage
            call_cost = self._price(request, spent)
            meter.record(spent, call_cost)
            await asyncio.to_thread(
                self._events.emit,
                EventType.MODEL_CALLED,
                {
                    "turn": turn,
                    # What the provider returned. A hook may replace the response
                    # below: the history and the FR-26 decision use the
                    # replacement, and the audit trail keeps what the model said.
                    "stop_reason": response.stop_reason.value,
                    "tool_calls": [call.name for call in response.tool_calls],
                    # Every field of Usage, walked rather than listed (FR-29).
                    "usage": {f.name: getattr(spent, f.name) for f in dataclasses.fields(Usage)},
                    # A string: JSON has no decimal, and a float would drift.
                    "cost_usd": None if call_cost is None else str(call_cost),
                    "provider_response_id": response.provider_response_id,
                },
            )

            after = self._hook.after_model(response)
            if after.action is HookAction.HALT:
                return _outcome(meter, None, turn, error=after.reason or "halted by hook")
            if after.action is HookAction.MODIFY and after.replacement is not None:
                response = after.replacement

            await asyncio.to_thread(self._sessions.append, run_id, response.message)

            unfinished = _UNFINISHED.get(response.stop_reason)
            if unfinished is not None:
                # Recorded above -- the message and its event -- so the trace
                # shows what the model produced. Nothing in it executes.
                return _outcome(meter, response.message.content, turn, error=unfinished)

            if not response.tool_calls:
                return _outcome(meter, response.message.content, turn)

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

        return _outcome(meter, None, max_turns, exhausted_turns=True)

    def _price(self, request: ModelRequest, usage: Usage) -> Decimal | None:
        """One call's cost, or None. The pricer is the caller's, so it is
        guarded: accounting never fails a run (NFR-11)."""
        if self._cost_of is None:
            return None
        try:
            return self._cost_of(request, usage)
        except Exception:  # noqa: BLE001
            return None

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


def _outcome(meter: RunMeter, output: str | None, turns: int, **fields: Any) -> LoopOutcome:
    """An outcome carrying the meter's totals, so no exit reports its own."""
    return LoopOutcome(output, meter.usage, turns, cost_usd=meter.cost_usd, **fields)
