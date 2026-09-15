"""Run handles: a run already under way, its events as they happen, and cancellation
(FR-48, FR-49, FR-50).

`Runner.start` returns a `RunHandle` for a run it has started without waiting for
it; `Runner.run` is a run started that way and awaited to its end. A run belongs to
the event loop that started it.

Cancellation is cooperative (DECISION-29e21dd0). Asking for it marks the run and,
when the run is waiting on something that can be interrupted -- a model call, a
provider slot, a tool, a slot for a tool -- cancels the run's task so that wait ends.
Store writes are never interrupted: one already started finishes, and the run then
stops at its next checkpoint, before any further model or tool call starts. A tool
that catches `CancelledError` and carries on delays the end of its run for as long
as it runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .events import EventSink, EventType, RunEvent
from .model import Usage

if TYPE_CHECKING:
    from .api import RunResult, RunStatus


class RunControl:
    """One run's cancellation state and progress, shared by the Runner, the loop and
    the executor. Internal: callers hold a RunHandle."""

    def __init__(self) -> None:
        self.task: asyncio.Task[Any] | None = None
        # The run's coroutine has begun. Before that, cancelling its task would end
        # it without a row or an event, so a request only marks the run and the run
        # records itself cancelled at its first checkpoint.
        self.started = False
        self.requested = False
        self.reason: object = None
        # Set immediately before a terminal event is written. From then on a request
        # has no effect: the run ends as it was already ending (FR-50).
        self.terminal = False
        # A model call is in flight from entering ModelClient.send until it returns
        # (P2-D7); waiting for a provider slot is not in flight.
        self.in_flight = False
        self.cancelled_in_flight = False
        self.turns = 0
        # The event loop the run belongs to (FR-48), set by Runner.start.
        self.loop: asyncio.AbstractEventLoop | None = None

    def request(self, reason: object = None) -> None:
        """Ask the run to stop. Idempotent; no effect once the run has ended or its
        terminal event is being written. Safe to call from any thread."""
        task = self.task
        if self.requested or self.terminal or (task is not None and task.done()):
            return
        self.requested = True
        self.reason = reason
        if task is None or not self.started:
            return
        loop = self.loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            # Called from another thread (M12 review round 1, C1). asyncio lets a
            # task be cancelled only on its own loop's thread: from elsewhere the
            # cancel took effect only when the loop next woke for some other
            # reason, and under asyncio debug mode Task.cancel raised RuntimeError
            # and left the run unable to end. The loop is handed the cancel instead,
            # which also wakes it, as PublishingSink hands it events.
            try:
                loop.call_soon_threadsafe(self._cancel, task)
            except RuntimeError:  # the loop has closed, and the run with it
                pass
            return
        # Asked from inside the run itself -- a hook -- there is nothing to interrupt:
        # the next checkpoint stops it, before the next model or tool call.
        if task is not asyncio.current_task():
            task.cancel()

    def _cancel(self, task: asyncio.Task[Any]) -> None:
        """A cancel handed over from another thread, run on the loop's own thread."""
        if not task.done() and not self.terminal:
            task.cancel()

    def checkpoint(self) -> None:
        """Stop here if the run has been asked to stop."""
        if self.requested:
            raise asyncio.CancelledError()

    async def store(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """A store call on a worker thread (FR-20) that, once started, completes even if
        the run is cancelled meanwhile (FR-50).

        Cancelling an asyncio.to_thread call does not stop a thread already running it
        and drops one still queued (KNOWLEDGE-e22f787f): a write could vanish or land
        after the run's terminal event. So the call is shielded, a cancellation that
        arrives meanwhile is absorbed until it finishes, and the run -- which the
        request has already marked -- stops at its next checkpoint instead.
        """
        future = asyncio.ensure_future(asyncio.to_thread(function, *args, **kwargs))
        absorbed = 0
        try:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    if future.cancelled():
                        raise
                    absorbed += 1
            return future.result()
        finally:
            task = asyncio.current_task()
            while absorbed and task is not None and task.cancelling():
                task.uncancel()
                absorbed -= 1


@dataclass(frozen=True)
class RunState:
    """A snapshot of a run (FR-49): whether it is still running or its terminal
    status, and the usage, cost and turns recorded so far. The cost is None while a
    model call is in flight, because that call may already be billed."""

    running: bool
    status: RunStatus | None
    usage: Usage
    cost_usd: Decimal | None
    turns: int


class PublishingSink:
    """An event sink that also hands every recorded event to its run's handle.

    Emits run on worker threads, so the handle is told on the event loop's thread,
    through call_soon_threadsafe, before the emit returns to the run.
    """

    def __init__(self, sink: EventSink, publish: Callable[[RunEvent], None], loop: asyncio.AbstractEventLoop) -> None:
        self._sink, self._publish, self._loop = sink, publish, loop

    def emit(self, event_type: EventType, payload: dict[str, Any] | None = None, **identifiers: Any) -> RunEvent:
        event = self._sink.emit(event_type, payload, **identifiers)
        try:
            self._loop.call_soon_threadsafe(self._publish, event)
        except RuntimeError:  # the loop has closed; nobody is left to stream to
            pass
        return event

    def events(self) -> tuple[RunEvent, ...]:
        return self._sink.events()


class RunHandle:
    """A run under way (FR-48, FR-49, FR-50). Returned by `Runner.start`."""

    def __init__(self, run_id: str, control: RunControl, meter: Any) -> None:
        self.run_id = run_id
        self._control = control
        self._meter = meter
        self._events: dict[int, RunEvent] = {}
        self._waiters: list[asyncio.Future[None]] = []
        self._ended = False

    def events(self) -> AsyncIterator[RunEvent]:
        """Every event of the run in sequence_no order, from its first, whenever this is
        called; then each new one as it is recorded. Ends after the run's terminal
        event, or after its last recorded event if the terminal event could not be
        recorded. Cancelling the iteration does not cancel the run."""
        return self._stream()

    async def _stream(self) -> AsyncIterator[RunEvent]:
        number = 1
        while True:
            event = self._events.get(number)
            if event is not None:
                number += 1
                yield event
            elif self._ended:
                return
            else:
                waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                self._waiters.append(waiter)
                try:
                    await waiter
                finally:
                    if waiter in self._waiters:
                        self._waiters.remove(waiter)

    async def result(self) -> RunResult:
        """The run's RunResult, the same object however often it is awaited.
        Cancelling the coroutine awaiting it does not cancel the run."""
        assert self._control.task is not None
        return await asyncio.shield(self._control.task)

    def state(self) -> RunState:
        task = self._control.task
        turns = self._control.turns
        if task is not None and task.done():
            if not task.cancelled() and task.exception() is None:
                result = task.result()
                return RunState(False, result.status, result.usage, result.cost_usd, turns)
            return RunState(False, None, self._meter.usage, None, turns)
        cost = None if self._control.in_flight else self._meter.cost_usd
        return RunState(True, None, self._meter.usage, cost, turns)

    def cancel(self, reason: object = None) -> None:
        """Ask the run to stop (FR-50). Idempotent; no effect on a run that has ended or
        whose terminal event is being written. Await result() for its outcome.

        May be called from any thread: off the run's own event loop thread, the
        cancellation is handed to that loop rather than applied from the calling
        thread (M12 review round 1, C1)."""
        self._control.request(reason)

    async def _settled(self) -> None:
        """Wait until the run has ended, whatever cancels this wait meanwhile."""
        task = self._control.task
        assert task is not None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: BLE001 - result() reports it
                return

    def _publish(self, event: RunEvent) -> None:
        number = getattr(event, "sequence_no", None)
        if isinstance(number, int) and number not in self._events:
            self._events[number] = event
        self._wake()

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._ended = True
        self._wake()

    def _wake(self) -> None:
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)
