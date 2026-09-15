"""A minimal OpenTelemetry exporter for runs (FR-58, FR-59, FR-60, NFR-19, P2-D12).

`import agentsdk` never imports this module, and this module imports OpenTelemetry only
when an exporter is built, so the SDK runs without the `otel` extra (NFR-19).

The exporter turns a run's recorded events into spans after they happen: the run becomes
an `invoke_agent` span, each `ModelCalled` a `chat` span and each `ToolCalled` an
`execute_tool` span beneath it, timed from what the events recorded (FR-57). It adds
nothing to the path a run takes. `export` only puts events on a bounded queue; one worker
thread owned by the exporter takes them off, and every span is created, ended and handed
to the caller's span processors on that thread, so a synchronous processor or a blocking
exporter cannot stall the event loop (FR-60). A full queue drops the event and counts it
in `dropped`; an exception on the worker is counted in `errors`; neither is raised.

A run's spans are built when its terminal event arrives, because only that event records
when the run started and how long it took. A run whose terminal event never arrives --
dropped from a full queue, or still going at shutdown -- exports no spans.

A child run is linked to its parent's `invoke_agent` span when this exporter has already
started that span; either way it carries `agentsdk.parent_run_id`, read from its own
`RunStarted` event. A link, not a parent: the child's spans stay in a trace of their own.

Attributes follow OpenTelemetry semantic conventions v1.40.0 (P2-D12). No message content,
tool argument or tool result is exported: none is read from the events, and neither is a
run's failure reason, which can quote a tool or a model.
"""

from __future__ import annotations

import asyncio
import math
import queue
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from .events import EventType, RunEvent

SEMCONV_VERSION = "1.40.0"
SCHEMA_URL = f"https://opentelemetry.io/schemas/{SEMCONV_VERSION}"
DEFAULT_MAX_QUEUE_SIZE = 10_000
# Runs whose terminal event has not arrived, and invoke_agent spans remembered for the
# child runs that may link to them. Both bounded, oldest first out.
MAX_OPEN_RUNS = 10_000
MAX_REMEMBERED_RUNS = 10_000

_TERMINAL = frozenset({EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED})
_USAGE = (
    ("gen_ai.usage.input_tokens", "prompt_tokens"),
    ("gen_ai.usage.output_tokens", "completion_tokens"),
    ("gen_ai.usage.cache_read.input_tokens", "cache_read_tokens"),
    ("gen_ai.usage.cache_creation.input_tokens", "cache_write_tokens"),
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_STOP = object()


def _load_opentelemetry() -> SimpleNamespace:
    try:
        import opentelemetry.sdk.trace  # noqa: F401 - the SDK a caller's provider comes from
        from opentelemetry import trace
        from opentelemetry.context import Context
    except ImportError as error:
        raise ImportError(
            "agentsdk.telemetry needs OpenTelemetry, which is not installed: install the SDK "
            "with its otel extra, which pins opentelemetry-api, opentelemetry-sdk and "
            "opentelemetry-exporter-otlp-proto-http (P2-D12)"
        ) from error
    return SimpleNamespace(trace=trace, Context=Context)


class OpenTelemetryExporter:
    """Exports runs as spans through a TracerProvider the caller owns (FR-58).

    `export(run)` takes a finished RunResult or a RunHandle and returns at once.
    `flush(timeout)` blocks until every event handed over has been turned into spans,
    so call it off the event loop. `shutdown(timeout)` stops the worker.
    """

    def __init__(self, tracer_provider: Any, *, max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE) -> None:
        self._otel = _load_opentelemetry()
        if isinstance(max_queue_size, bool) or not isinstance(max_queue_size, int) or max_queue_size < 1:
            raise ValueError(f"max_queue_size must be a positive int, got {max_queue_size!r}")
        self._provider = tracer_provider
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue_size)
        self._state = threading.Condition()
        self._pending = 0  # events accepted and not yet handled by the worker
        self._following = 0  # RunHandles whose events are still being followed
        self._feeds: set[asyncio.Task[None]] = set()
        self.dropped = 0
        self.errors = 0
        # Touched only on the worker thread.
        self._tracer: Any = None
        self._open: OrderedDict[str, list[RunEvent]] = OrderedDict()
        self._started: OrderedDict[str, Any] = OrderedDict()
        self._worker = threading.Thread(target=self._work, name="agentsdk-telemetry", daemon=True)
        self._worker.start()

    # --- the caller's side ------------------------------------------------------------------

    def export(self, run: Any) -> None:
        """Hand a run to the exporter and return at once.

        A finished RunResult's events are queued now. A RunHandle's are followed as the run
        records them, by a task on the run's own event loop that only queues them, whichever
        thread or loop export is called from.
        """
        from .api import RunResult
        from .handle import RunHandle

        if isinstance(run, RunHandle):
            self._follow_on_run_loop(run)
            return
        if isinstance(run, RunResult):
            for event in run.events:
                self._offer(event)
            return
        raise TypeError(f"export takes a RunResult or a RunHandle, not {type(run).__name__}")

    def flush(self, timeout: float | None = None) -> bool:
        """True once every event handed over has been handled; False if `timeout` passes first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._state:
            while self._pending or self._following:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._state.wait(remaining)
        return True

    def shutdown(self, timeout: float | None = None) -> bool:
        """Flush, then stop the worker. True if both finished within `timeout`."""
        deadline = None if timeout is None else time.monotonic() + timeout
        flushed = self.flush(timeout)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            self._queue.put(_STOP, timeout=remaining)
        except queue.Full:
            return False
        self._worker.join(remaining)
        return flushed and not self._worker.is_alive()

    def _follow_on_run_loop(self, handle: Any) -> None:
        """Follow a handle's events on the loop its run belongs to (FR-48).

        Before round 2 the follow task ran on the caller's loop. A waiter of another loop could
        then be stranded, and a loop that ended first took the task, and flush, with it (M14
        review round 1, C1). A run whose loop is no longer running has ended, or cannot
        progress: the events it recorded are queued at once.
        """
        run_loop = handle._control.loop
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        with self._state:
            self._following += 1
        if run_loop is not None and run_loop is here:
            task = here.create_task(self._follow(handle))
            self._feeds.add(task)
            task.add_done_callback(self._feeds.discard)
            task.add_done_callback(self._followed)
            return
        if run_loop is not None and run_loop.is_running() and not run_loop.is_closed():
            follow = self._follow(handle)
            try:
                asyncio.run_coroutine_threadsafe(follow, run_loop).add_done_callback(self._followed)
                return
            except RuntimeError:  # the loop closed in between
                follow.close()
        try:
            for _, event in sorted(handle._events.items()):
                self._offer(event)
        finally:
            self._followed(None)

    async def _follow(self, handle: Any) -> None:
        try:
            async for event in handle.events():
                self._offer(event)
        except Exception:  # noqa: BLE001 - FR-60: counted, never raised into the run's loop
            self._count_error()

    def _followed(self, _: Any) -> None:
        """A follow has ended, however it ended: finished, cancelled, or never started. Counted
        here rather than in _follow, whose finally a follow cancelled before its first step
        never reaches."""
        with self._state:
            self._following -= 1
            self._state.notify_all()

    def _offer(self, event: Any) -> None:
        with self._state:
            self._pending += 1
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._state:
                self._pending -= 1
                self.dropped += 1
                self._state.notify_all()

    def _count_error(self) -> None:
        with self._state:
            self.errors += 1

    # --- the worker thread ------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            try:
                self._take(item)
            except Exception:  # noqa: BLE001 - FR-60: counted, never raised
                self._count_error()
            finally:
                with self._state:
                    self._pending -= 1
                    self._state.notify_all()

    def _take(self, event: RunEvent) -> None:
        run_id = event.run_id
        events = self._open.get(run_id)
        if events is None:
            events = self._open[run_id] = []
            while len(self._open) > MAX_OPEN_RUNS:
                _, abandoned = self._open.popitem(last=False)
                with self._state:
                    self.dropped += len(abandoned)
        events.append(event)
        if event.event_type in _TERMINAL:
            del self._open[run_id]
            self._build(events)

    def _build(self, events: list[RunEvent]) -> None:
        trace = self._otel.trace
        if self._tracer is None:
            from .version import __version__

            self._tracer = self._provider.get_tracer("agentsdk", __version__, schema_url=SCHEMA_URL)
        tracer = self._tracer

        first, terminal = events[0], events[-1]
        started = first.payload if first.event_type is EventType.RUN_STARTED else {}
        parent_run_id = _text(started.get("parent_run_id"))
        common: dict[str, Any] = {
            "agentsdk.semconv_version": SEMCONV_VERSION,
            "agentsdk.run_id": terminal.run_id,
            "agentsdk.tenant_id": terminal.tenant_id,
            "agentsdk.project_id": terminal.project_id,
        }
        _put(common, "agentsdk.parent_run_id", parent_run_id)

        agent_id = _text(started.get("agent_spec_id"))
        attributes = {**common, "gen_ai.operation.name": "invoke_agent"}
        _put(attributes, "gen_ai.agent.id", agent_id)
        _put(attributes, "gen_ai.request.model", _text(started.get("model")))
        _put(attributes, "agentsdk.model_client_key", _text(started.get("provider")))
        parent_span = self._started.get(parent_run_id) if parent_run_id is not None else None
        links = [trace.Link(parent_span)] if parent_span is not None else []

        start, end = _window(terminal)
        run_span = tracer.start_span(
            _name("invoke_agent", agent_id),
            context=self._otel.Context(),
            kind=trace.SpanKind.INTERNAL,
            attributes=attributes,
            links=links,
            start_time=start,
        )
        self._started[terminal.run_id] = run_span.get_span_context()
        while len(self._started) > MAX_REMEMBERED_RUNS:
            self._started.popitem(last=False)
        try:
            inside = trace.set_span_in_context(run_span)
            for event in events[1:-1]:
                if event.event_type is EventType.MODEL_CALLED:
                    self._chat(tracer, inside, common, event)
                elif event.event_type is EventType.TOOL_CALLED:
                    self._tool(tracer, inside, common, event)
            if terminal.event_type is EventType.RUN_FAILED:
                run_span.set_status(trace.Status(trace.StatusCode.ERROR))
        finally:
            run_span.end(end_time=end)

    def _chat(self, tracer: Any, inside: Any, common: dict[str, Any], event: RunEvent) -> None:
        trace = self._otel.trace
        payload = event.payload
        model = _text(payload.get("model"))
        attributes = {**common, "gen_ai.operation.name": "chat"}
        _put(attributes, "gen_ai.request.model", model)
        _put(attributes, "agentsdk.model_client_key", _text(payload.get("provider")))
        _put(attributes, "gen_ai.provider.name", _text(payload.get("provider_name")))
        usage = payload.get("usage")
        for attribute, field_name in _USAGE:
            _put(attributes, attribute, _count(usage.get(field_name)) if isinstance(usage, dict) else None)
        stop = _text(payload.get("stop_reason"))
        if stop is not None:
            attributes["gen_ai.response.finish_reasons"] = (stop,)
        # Only a known cost: an unknown one is absent, never 0 (NFR-11).
        _put(attributes, "agentsdk.cost_usd", _text(payload.get("cost_usd")))
        start, end = _window(event)
        span = tracer.start_span(
            _name("chat", model), context=inside, kind=trace.SpanKind.CLIENT, attributes=attributes, start_time=start
        )
        span.end(end_time=end)

    def _tool(self, tracer: Any, inside: Any, common: dict[str, Any], event: RunEvent) -> None:
        trace = self._otel.trace
        payload = event.payload
        name = _text(payload.get("name"))
        is_error = payload.get("is_error") is True
        attributes = {**common, "gen_ai.operation.name": "execute_tool"}
        _put(attributes, "gen_ai.tool.name", name)
        _put(attributes, "gen_ai.tool.call.id", _text(payload.get("tool_call_id")))
        attributes["agentsdk.tool.is_error"] = is_error
        attributes["agentsdk.tool.truncated"] = payload.get("truncated") is True
        if is_error:
            _put(attributes, "error.type", _text(payload.get("error_type")))
        start, end = _window(event)
        span = tracer.start_span(
            _name("execute_tool", name), context=inside, kind=trace.SpanKind.INTERNAL, attributes=attributes, start_time=start
        )
        if is_error:
            span.set_status(trace.Status(trace.StatusCode.ERROR))
        span.end(end_time=end)


def _text(value: Any) -> str | None:
    return value if type(value) is str and value else None


def _count(value: Any) -> int | None:
    return value if type(value) is int else None


def _put(attributes: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        attributes[key] = value


def _name(operation: str, detail: str | None) -> str:
    return f"{operation} {detail}" if detail else operation


def _epoch_ns(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000


def _window(event: RunEvent) -> tuple[int, int]:
    """A span's start and end in nanoseconds since the epoch, from what its event recorded.

    An event with no start time -- a tool call that never reached step 6 -- is a point at
    the moment it was recorded.
    """
    payload = event.payload
    started_at = payload.get("started_at")
    try:
        start = _epoch_ns(datetime.fromisoformat(started_at)) if isinstance(started_at, str) else None
    except ValueError:
        start = None
    if start is None:
        start = _epoch_ns(event.timestamp)
        return start, start
    duration = payload.get("duration_ms")
    valid = isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration)
    length = round(duration * 1_000_000) if valid and duration > 0 else 0
    return start, start + length
