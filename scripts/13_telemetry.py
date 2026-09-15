"""13 - Telemetry: a run's timings, and the same run as an OpenTelemetry span tree.

What it shows
  * every ModelCalled and ToolCalled event records when it started, how long it took,
    and how long it waited for a slot; the terminal event records the whole run's
  * agentsdk.telemetry.OpenTelemetryExporter turns a run into an invoke_agent span
    with a chat span per model call and an execute_tool span per tool call beneath it
  * it follows a RunHandle as the run goes, on a thread of its own, so a slow or
    broken collector cannot slow or change the run
  * spans carry the run, tenant, model and token counts, never the task, the answer,
    tool arguments or tool results
  * offline, spans go to an in-memory exporter and are printed as a tree; live, a real
    model's run is also sent over OTLP/HTTP to OTEL_EXPORTER_OTLP_ENDPOINT

Needs the otel extra (python -m pip install -r scripts/requirements.txt). For live
mode, run a collector first, for example Jaeger in Docker, and set
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 in .env; its UI is then at
http://localhost:16686, under the service agentsdk-example-13.

Run it
  python scripts/13_telemetry.py            # live: BASE_URL, MODEL_API_KEY and OTEL_EXPORTER_OTLP_ENDPOINT from .env
  python scripts/13_telemetry.py --offline  # a scripted model and an in-memory exporter: no network
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from agentsdk import AgentSpec, Message, Role, RunConfig, Runner, RunStatus, ToolCall
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import Tool, ToolSpec

SERVICE = "agentsdk-example-13"
TASK = "What day of the week is it today, and which day of the year?"
CONFIG = RunConfig(tenant_id="example-tenant", project_id="examples", max_turns=4)
NO_ARGUMENTS = {"type": "object", "properties": {}, "additionalProperties": False}


async def weekday() -> str:
    return datetime.now(timezone.utc).strftime("%A")


async def day_of_year() -> str:
    return str(datetime.now(timezone.utc).timetuple().tm_yday)


# Both declare concurrency_safe, so the model's two calls run as one parallel batch.
TOOLS = [
    Tool(spec=ToolSpec(name="weekday", description="Today's weekday in UTC.", input_schema=NO_ARGUMENTS,
                       concurrency_safe=True), fn=weekday),
    Tool(spec=ToolSpec(name="day_of_year", description="Today's day of the year in UTC, 1 to 366.",
                       input_schema=NO_ARGUMENTS, concurrency_safe=True), fn=day_of_year),
]
AGENT = AgentSpec(
    id="calendar",
    instructions="Call weekday and day_of_year in the same turn, then answer in one sentence.",
    tool_profile=("weekday", "day_of_year"),
)


class ScriptedModel:
    """Offline stand-in for a real model: asks for both tools, then answers."""

    async def send(self, request):
        await asyncio.sleep(0.02)  # so each model call has a visible length
        if not any(m.role is Role.TOOL for m in request.messages):
            return ModelResponse(
                message=Message(role=Role.ASSISTANT, tool_calls=(
                    ToolCall(id="w", name="weekday", arguments={}),
                    ToolCall(id="d", name="day_of_year", arguments={}),
                )),
                stop_reason=StopReason.TOOL_CALLS,
                usage=Usage(40, 12, 52),
            )
        return ModelResponse(
            message=Message(role=Role.ASSISTANT, content="Today is the day both tools reported."),
            stop_reason=StopReason.END_TURN,
            usage=Usage(60, 10, 70),
        )


def live_client():
    from agentsdk.config import Settings
    from agentsdk.providers import OpenAICompatibleModelClient

    settings = Settings.from_env(load_dotfile=False)
    return OpenAICompatibleModelClient(
        base_url=settings.base_url, api_key=settings.api_key, model=settings.default_model
    )


def span_tree(spans):
    children = {}
    for span in spans:
        children.setdefault(span.parent.span_id if span.parent else None, []).append(span)
    lines = []

    def walk(parent_id, depth):
        for span in sorted(children.get(parent_id, []), key=lambda s: s.start_time):
            length = (span.end_time - span.start_time) / 1_000_000
            lines.append(f"{'    ' * depth}{span.name}  ({length:.1f} ms)")
            walk(span.context.span_id, depth + 1)

    walk(None, 1)
    return lines


def show_events(result):
    for event in result.events:
        p, kind = event.payload, event.event_type.value
        if kind == "ModelCalled":
            detail = f"model={p['model']} duration_ms={p['duration_ms']:.1f} queued_ms={p['queued_ms']:.1f}"
        elif kind == "ToolCalled":
            detail = f"{p['name']} duration_ms={p['duration_ms']:.1f} queued_ms={p['queued_ms']:.1f}"
        elif "duration_ms" in p:
            detail = f"run duration_ms={p['duration_ms']:.1f}"
        else:
            detail = ""
        print(f"  #{event.sequence_no:<2} {kind:<13} {detail}")


async def main(offline: bool) -> None:
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    except ImportError:
        raise SystemExit("this example needs the otel extra: python -m pip install -r scripts/requirements.txt")
    from agentsdk.telemetry import OpenTelemetryExporter

    class Recorded(SpanExporter):
        """Wraps the OTLP exporter and keeps what each export returned."""

        def __init__(self, inner):
            self.inner, self.results = inner, []

        def export(self, spans):
            result = self.inner.export(spans)
            self.results.append(result)
            return result

        def shutdown(self):
            self.inner.shutdown()

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}), shutdown_on_exit=False)
    shown = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(shown))
    sent, endpoint = None, None
    if offline:
        client = ScriptedModel()
    else:
        from dotenv import load_dotenv
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        load_dotenv()
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            raise SystemExit("set OTEL_EXPORTER_OTLP_ENDPOINT in .env, for example http://localhost:4318")
        sent = Recorded(OTLPSpanExporter())  # reads OTEL_EXPORTER_OTLP_ENDPOINT and adds /v1/traces
        provider.add_span_processor(BatchSpanProcessor(sent))
        client = live_client()

    exporter = OpenTelemetryExporter(provider)
    try:
        runner = Runner({"model": client}, tools=TOOLS, session_store=InMemorySessionStore())
        handle = await runner.start(AGENT, TASK, CONFIG)
        exporter.export(handle)  # followed as the run goes; the run never waits for it
        result = await handle.result()
        flushed = await asyncio.to_thread(exporter.flush, 30)
    finally:
        await asyncio.to_thread(exporter.shutdown, 10)
        if hasattr(client, "aclose"):
            await client.aclose()
    delivered = await asyncio.to_thread(provider.force_flush, 30_000)

    print(f"run {result.run_id}: status={result.status.value} output={(result.output or '')[:80]!r}")
    show_events(result)
    spans = shown.get_finished_spans()
    print("\nthe same run as spans:")
    for line in span_tree(spans):
        print(line)

    operations = [span.attributes.get("gen_ai.operation.name") for span in spans]
    roots = [span for span in spans if span.parent is None]
    kinds = [event.event_type.value for event in result.events]
    timed = [e.payload for e in result.events if e.event_type.value in ("ModelCalled", "ToolCalled")]
    private = [TASK] + ([result.output] if result.output else [])
    leaked = any(text in str(value) for span in spans for value in span.attributes.values() for text in private)
    checks = [
        ("the run completed", result.status is RunStatus.COMPLETED),
        ("every model and tool event recorded its timings",
         bool(timed) and all({"started_at", "duration_ms", "queued_ms"} <= set(p) for p in timed)),
        ("the exporter turned every event into spans", flushed),
        ("one invoke_agent span, with a chat span per model call and an execute_tool span per tool call beneath it",
         len(roots) == 1 and operations.count("invoke_agent") == 1
         and operations.count("chat") == kinds.count("ModelCalled")
         and operations.count("execute_tool") == kinds.count("ToolCalled")
         and all(s.parent.span_id == roots[0].context.span_id for s in spans if s is not roots[0])),
        ("no span carries the task or the answer", not leaked),
    ]
    if sent is not None:
        checks.append((
            f"the spans reached {endpoint} over OTLP/HTTP",
            delivered and bool(sent.results) and all(r is SpanExportResult.SUCCESS for r in sent.results),
        ))
        if roots:
            print(f"\ntrace {roots[0].context.trace_id:032x}, service {SERVICE}: open your collector's UI "
                  "(for Jaeger, http://localhost:16686) to see it")
    for label, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
    failed = [label for label, passed in checks if not passed]
    if failed:
        raise SystemExit(f"failed: {failed}")


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # a model reply may hold characters this console cannot print
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="use a scripted model and an in-memory exporter")
    asyncio.run(main(parser.parse_args().offline))
