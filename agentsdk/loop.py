"""The agent loop (FR-1, FR-14, FR-26, FR-44, FR-50, LLD 3.10).

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

Since M12 the loop can be cancelled (FR-50). It checks before every model call
and every tool call whether its run has been asked to stop, lets a store write
already started finish, and pairs every tool call of a response it had begun to
execute with a result before the cancellation continues.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .context import ContextAssembler
from .errors import ModelError, describe_exception
from .events import EventSink, EventType
from .executor import PreparedCall, ToolExecutor
from .handle import RunControl
from .hooks import HookAction, RuntimeHook
from .identity import PrincipalContext
from .model import ModelClient, ModelRequest, ModelResponse, StopReason, Usage
from .outcomes import Completed, Failed, ToolExecutionOutcome
from .primitives import Message, Role, ToolCall, ToolResult
from .primitives import unstorable_reason
from .registry import add_costs
from .timings import elapsed_ms, now_ns, wall_clock
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
        tool_slot: Callable[[str], Any] | None = None,
        model_slot: Callable[[], Any] | None = None,
        control: RunControl | None = None,
        provider: str | None = None,
        provider_name: str | None = None,
        recorded_model: str | None = None,
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
        # The Runner's limits: a tool call's slots (FR-44) and a model call's
        # provider slot (FR-46). A loop built without them limits nothing.
        self._tool_slot = tool_slot
        self._model_slot = model_slot if model_slot is not None else nullcontext
        # What every ModelCalled names (FR-57): the model client's key, the provider name
        # the client declares, and the run's recorded model for a request naming none.
        self._provider = provider
        self._provider_name = provider_name
        self._recorded_model = recorded_model
        # The run's cancellation state and progress (FR-50). A loop built without
        # one is never cancelled.
        self._control = control if control is not None else RunControl()

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
        #
        # Since M12 every store call goes through control.store, which is that
        # same worker thread, shielded so a cancellation cannot drop a write
        # already started (FR-50).
        meter = self._meter if self._meter is not None else RunMeter()
        control = self._control
        store = control.store
        await store(self._sessions.append, run_id, Message(role=Role.USER, content=task))

        for turn in range(1, max_turns + 1):
            control.turns = turn
            control.checkpoint()
            history = await store(self._sessions.history, run_id)
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

            # FR-50: no model call starts once the run has been asked to stop, even
            # when the hook above is what asked.
            control.checkpoint()
            sending = False
            try:
                # FR-46: the wait for a provider slot is outside send, so it is
                # no part of the client's own timeout, and the slot is held
                # through the retries the client makes inside send (FR-15).
                # FR-57: the wait for the slot, then when send was entered and how long
                # it took, retries inside the client included.
                waiting = now_ns()
                async with self._model_slot():
                    queued_ms = elapsed_ms(waiting)
                    sending = True
                    control.in_flight = True
                    started_at, sent = wall_clock(), now_ns()
                    try:
                        response = await self._model.send(request)
                    finally:
                        control.in_flight = False
                    duration_ms = elapsed_ms(sent)
            except ModelError as exc:
                # The client's own retries are exhausted, or the error is not
                # transient. The run fails; it does not raise past Runner.
                return _outcome(meter, None, turn, error=describe_exception(exc))
            except asyncio.CancelledError:
                # P2-D7: a call cancelled after entering send may already be
                # billed and reports no usage, so the run's cost becomes unknown.
                # One cancelled while waiting for its provider slot was never sent.
                if sending:
                    control.cancelled_in_flight = True
                raise

            # The provider has billed this call, whatever happens next. So the
            # account is recorded first and the event second, both before any
            # caller code or store write that could fail (R2). A hook may
            # rewrite what the model said, but it cannot un-spend the tokens.
            spent = response.usage
            call_cost = self._price(request, spent)
            meter.record(spent, call_cost)
            await store(
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
                    "started_at": started_at,
                    "duration_ms": duration_ms,
                    "queued_ms": queued_ms,
                    "model": _recordable(sent_model(request, self._recorded_model)),
                    "provider": self._provider,
                    "provider_name": self._provider_name,
                },
            )

            after = self._hook.after_model(response)
            if after.action is HookAction.HALT:
                return _outcome(meter, None, turn, error=after.reason or "halted by hook")
            if after.action is HookAction.MODIFY and after.replacement is not None:
                response = after.replacement

            await store(self._sessions.append, run_id, response.message)

            unfinished = _UNFINISHED.get(response.stop_reason)
            if unfinished is not None:
                # Recorded above -- the message and its event -- so the trace
                # shows what the model produced. Nothing in it executes.
                return _outcome(meter, response.message.content, turn, error=unfinished)

            if not response.tool_calls:
                return _outcome(meter, response.message.content, turn)

            # FR-50: none of this response's calls starts once the run has been
            # asked to stop. They were not begun, so they get no results.
            control.checkpoint()
            results = await self._execute_tool_calls(run_id, response.tool_calls, principal_context)
            await store(
                self._sessions.append,
                run_id,
                Message(role=Role.TOOL, tool_results=tuple(results)),
            )

        return _outcome(meter, None, max_turns, exhausted_turns=True)

    async def _execute_tool_calls(
        self, run_id: str, tool_calls: Sequence[ToolCall], principal_context: PrincipalContext | None
    ) -> list[ToolResult]:
        """One response's calls, batch by batch, with results in issue order (FR-44).

        If the run is cancelled, or a call raises CancelledError, while these calls
        execute, every call of the response is still paired with a result before
        the cancellation continues (FR-50): an unfinished or unstarted call gets a
        ToolCancelled result and its ToolCalled event, and the tool message is
        appended, so the recorded conversation stays well formed.
        """
        outcomes: dict[int, ToolExecutionOutcome] = {}
        prepared: dict[int, PreparedCall] = {}
        try:
            for batch in self._batches(list(enumerate(tool_calls))):
                await self._run_batch(batch, principal_context, outcomes, prepared)
        except asyncio.CancelledError:
            try:
                for index, call in enumerate(tool_calls):
                    if index not in outcomes:
                        outcomes[index] = await self._executor.cancelled(prepared.get(index, call))
                await self._control.store(
                    self._sessions.append,
                    run_id,
                    Message(role=Role.TOOL, tool_results=tuple(_results(outcomes, len(tool_calls)))),
                )
            except Exception:  # noqa: BLE001 - the cancellation, not a store failure, decides how the run ends
                pass
            raise
        return _results(outcomes, len(tool_calls))

    def _batches(self, calls: Sequence[tuple[int, ToolCall]]) -> list[list[tuple[int, ToolCall]]]:
        """Consecutive batches, in the order the model issued the calls (FR-44).

        A call to a registered tool that declares concurrency_safe joins the
        current batch; every other call, including one naming no registered
        tool, is a batch of its own. So a response whose tools declare nothing
        runs exactly as it did before M11: one call at a time, in order.
        """
        batches: list[list[tuple[int, ToolCall]]] = []
        current: list[tuple[int, ToolCall]] = []
        for item in calls:
            if self._is_concurrency_safe(item[1]):
                current.append(item)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([item])
        if current:
            batches.append(current)
        return batches

    def _is_concurrency_safe(self, call: ToolCall) -> bool:
        # The registry is the caller's. One that raises here makes the call a
        # batch of its own, and step 1 then reports whatever is wrong with it.
        try:
            return call.name in self._registry and self._registry.get(call.name).spec.concurrency_safe is True
        except Exception:  # noqa: BLE001
            return False

    async def _run_batch(
        self,
        batch: list[tuple[int, ToolCall]],
        principal_context: PrincipalContext | None,
        outcomes: dict[int, ToolExecutionOutcome],
        prepared: dict[int, PreparedCall],
    ) -> None:
        """Every call's steps 1 to 5 in issue order on this thread, then steps 6 to
        9 concurrently under the run's slots (FR-44), filling `outcomes` by index.

        Each call yields its own outcome through the executor's total boundary,
        so one failing changes no other. A BaseException escaping a call, or the
        cancellation of this batch, cancels the other calls and waits for them:
        none is left running behind a run that has moved on. The outcomes of calls
        that finished are kept, so a cancelled batch pairs only the rest.
        """
        control = self._control
        for index, call in batch:
            control.checkpoint()
            item = await self._executor.prepare(call, principal_context)
            if isinstance(item, PreparedCall):
                prepared[index] = item
            else:
                outcomes[index] = item
        ready = [index for index, _ in batch if index in prepared]
        # A cancellation that arrived while a failure above was being recorded
        # stops the batch here, before any of its calls reaches step 6.
        control.checkpoint()
        if len(ready) == 1:
            index = ready[0]
            outcomes[index] = await self._executor.run_prepared(prepared[index], self._tool_slot)
            return
        tasks = {
            index: asyncio.ensure_future(self._executor.run_prepared(prepared[index], self._tool_slot))
            for index in ready
        }
        try:
            await _wait_all_or_cancel(list(tasks.values()))
        finally:
            for index, task in tasks.items():
                if task.done() and not task.cancelled() and task.exception() is None:
                    outcomes[index] = task.result()

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


def sent_model(request: ModelRequest, recorded: str | None) -> str | None:
    """The model a request was sent with (FR-57, FR-30): the text model_settings names, which a
    before_model hook may have changed, or else the run's recorded model (FR-32).

    One function decides it for ModelCalled.model and for the call's price alike, so the two
    cannot name different models. Before round 2 the event accepted only an exact str while
    the price accepted any, and a StrEnum member was recorded as the run's model but priced as
    the one it named (M14 review round 1, C2). A str subclass is the text it holds, as a client
    sends it on the wire; a value that is not non-empty text names no model.
    """
    settings = getattr(request, "model_settings", None)
    named = settings.get("model") if isinstance(settings, dict) else None
    if isinstance(named, str):
        text = named if type(named) is str else str.__getitem__(named, slice(None))
        if text:
            return text
    return recorded


def _recordable(model: str | None) -> str | None:
    """A model name as ModelCalled records it: one no column can hold is unknown, never
    replaced by another model's name."""
    return model if model is None or unstorable_reason(model) is None else None


def _results(outcomes: dict[int, ToolExecutionOutcome], count: int) -> list[ToolResult]:
    results = []
    for index in range(count):
        outcome = outcomes[index]
        if isinstance(outcome, (Completed, Failed)):
            results.append(outcome.result)
        else:  # pragma: no cover - unreachable until Phase 4
            raise AssertionError(
                f"Phase 0 ToolExecutor returned {type(outcome).__name__}; "
                "only Completed and Failed are reachable"
            )
    return results


async def _wait_all_or_cancel(tasks: list[asyncio.Future[Any]]) -> None:
    """Wait for every task. If one ends by raising or by being cancelled, or this
    wait is itself cancelled, cancel the rest and return only once every one has
    finished, then raise (FR-44).

    The executor's boundary is total over Exception, so only a BaseException,
    CancelledError included, ends a task this way. The wait wakes on every
    completion because asyncio.wait(FIRST_EXCEPTION) does not wake for a task
    that ends cancelled: M11 review round 1 (R1) found the siblings of a call that
    raised CancelledError running on to their own end, without bound for a tool
    with no timeout, and still running after the run had failed.
    """
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    raise asyncio.CancelledError()
                error = task.exception()
                if error is not None:
                    raise error
    except BaseException:
        await _cancel_and_wait(pending)
        raise


async def _cancel_and_wait(tasks: set[asyncio.Future[Any]]) -> None:
    """Cancel `tasks` and return only once every one of them has finished.

    A further cancellation of this wait is absorbed rather than obeyed. Obeying
    it ended the run while its cancelled calls were still cleaning up (R2); the
    exception already on its way out is raised as soon as they finish.
    """
    for task in tasks:
        task.cancel()
    remaining = {task for task in tasks if not task.done()}
    while remaining:
        try:
            await asyncio.wait(remaining)
        except asyncio.CancelledError:
            pass
        remaining = {task for task in remaining if not task.done()}


def _outcome(meter: RunMeter, output: str | None, turns: int, **fields: Any) -> LoopOutcome:
    """An outcome carrying the meter's totals, so no exit reports its own."""
    return LoopOutcome(output, meter.usage, turns, cost_usd=meter.cost_usd, **fields)
