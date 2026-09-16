"""M10 gate: safe built-in tools (FR-35..FR-42, NFR-13, NFR-14, AC-27..AC-33).

Written before the implementation, against the owner-approved specification of
2026-09-12. Each property is asserted over a class of cases: every escape form
against every file tool and every path-bearing argument, every non-global
address class the ipaddress tables define, every executor path that yields
content.

Everything here runs offline. The file-confinement corpus creates real
symlinks, junctions, hard links, alternate data streams and 8.3 short names on
this Windows host; symlink creation FAILS rather than skips without Developer
Mode (decision D7). The fetch tests talk only to servers on 127.0.0.1, reached
through the tool's test seams, which map a checked public address onto them.

Names M10 adds are reached through their modules rather than imported by name,
so that before the implementation each test fails on its own instead of the
whole file failing to import.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import ctypes
import dataclasses
import datetime
import functools
import hashlib
import importlib
import importlib.util
import inspect
import ipaddress
import itertools
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import sys
import textwrap
import threading
import time
import tracemalloc
import types
import uuid
import zlib

import pytest

# The file-confinement corpus builds real junctions through CPython's
# Windows-only `_winapi` extension. Importing it at module scope aborts
# collection of the WHOLE run on macOS and Linux, hiding the nine other test
# modules. Skip this module instead. `agentsdk.builtin_tools` itself imports
# cleanly everywhere, and the file tools refuse to construct off Windows by
# design, so there is nothing here to exercise elsewhere.
_winapi = pytest.importorskip("_winapi", reason="the file tools use Windows handle APIs")

import agentsdk.tools as tools_module
from agentsdk import AgentSpec, RunConfig, Runner, RunStatus
from agentsdk.errors import ToolError
from agentsdk.executor import ToolExecutor
from agentsdk.hooks import HookAction, HookOutcome, RuntimeHook
from agentsdk.model import ModelResponse, StopReason, Usage
from agentsdk.outcomes import Completed, Failed
from agentsdk.permissions import AllowlistPermissionChecker
from agentsdk.primitives import (
    ContentProvenance,
    InstructionAuthority,
    Message,
    Origin,
    Role,
    TaintFlag,
    ToolCall,
    ToolResult,
    TrustZone,
    unstorable_reason,
)
from agentsdk.session import InMemorySessionStore
from agentsdk.tools import ApprovalPolicy, RiskClass, Tool, ToolRegistry, ToolSpec

REPO = pathlib.Path(__file__).resolve().parents[1]


def builtin():
    return importlib.import_module("agentsdk.builtin_tools")


INTERNAL = dict(
    origin=Origin.INTERNAL_TOOL,
    trust_zone=TrustZone.TRUSTED_SOURCE,
    instruction_authority=InstructionAuthority.DATA_ONLY,
    taint_flags=frozenset(),
)
EXTERNAL = dict(
    origin=Origin.EXTERNAL_TOOL,
    trust_zone=TrustZone.UNTRUSTED,
    instruction_authority=InstructionAuthority.DATA_ONLY,
    taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT, TaintFlag.PROMPT_INJECTION_RISK}),
)
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "maxLength": 5}},
    "required": ["text"],
    "additionalProperties": False,
}


def labels(provenance):
    return {name: getattr(provenance, name) for name in INTERNAL}


def declaration(**fields):
    return tools_module.ResultProvenance(**fields)


async def execute(tool, arguments, *, allowed=None, hook=None, call_name=None):
    """Run one call through the real executor; return the outcome and its ToolCalled payloads."""
    registry = ToolRegistry()
    registry.register(tool)
    events = []
    executor = ToolExecutor(
        registry,
        AllowlistPermissionChecker({tool.name} if allowed is None else set(allowed)),
        hook=hook,
        emit=lambda event_type, payload: events.append((event_type, payload)),
    )
    outcome = await executor.execute(
        ToolCall(id="c1", name=call_name or tool.name, arguments=arguments)
    )
    assert isinstance(outcome, (Completed, Failed)), type(outcome)
    return outcome, [payload for event_type, payload in events if event_type == "ToolCalled"]


class Rejecting(RuntimeHook):
    def __init__(self, reason="blocked by policy"):
        self.reason = reason

    def before_tool(self, tool_call):
        return HookOutcome(action=HookAction.REJECT, reason=self.reason)


class Substituting(RuntimeHook):
    def __init__(self, content):
        self.content = content

    def after_tool(self, result):
        return HookOutcome(
            action=HookAction.MODIFY, replacement=dataclasses.replace(result, content=self.content)
        )


class Scripted:
    """Replays model responses."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return self.script.pop(0) if self.script else text("done")


def text(content):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, content=content),
        stop_reason=StopReason.END_TURN,
        usage=Usage(1, 1, 2),
    )


def calls(*tool_calls):
    return ModelResponse(
        message=Message(role=Role.ASSISTANT, tool_calls=tuple(tool_calls)),
        stop_reason=StopReason.TOOL_CALLS,
        usage=Usage(1, 1, 2),
    )


# =============================================================================
# FR-40, FR-41, AC-32, AC-33: declared provenance and the output cap
# =============================================================================

# Captured by scratchpad/golden_schema_hashes.py at commit cc2f0d6, with no
# change under agentsdk/, BEFORE M10 touched tools.py: 1944 specs, 81 distinct
# hashes (today's hash covers name, description, input_schema and risk_class).
GOLDEN_COMMIT = "cc2f0d6a2195d28167907baa4eba6a4a76e4430d"
GOLDEN_FIRST = "6ac9f03fbdce985abc861a335d4516fa6eb548e884d91c8195393199100d4f79"
GOLDEN_SHA256 = "962d0454187b7d7329cf66f9e2da362bcdb83fe2534b8d9c37724c59bf9de016"


def golden_specs():
    """Copied verbatim from the capture script; the order matters."""
    schemas = (
        {"type": "object", "properties": {}},
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}, "tags": {"type": "array", "items": {"type": "string"}}}},
    )
    for i, (name, description, schema, risk, read_only, idempotent, approval, timeout) in enumerate(
        itertools.product(
            ("echo", "lookup_order", "日本語"),
            ("", "Echo the text back.", "Line one\nline two é"),
            schemas,
            tuple(RiskClass),
            (True, False),
            (True, False),
            tuple(ApprovalPolicy),
            (30.0, None, 0.5),
        )
    ):
        yield ToolSpec(
            name=name,
            description=description,
            input_schema=schema,
            risk_class=risk,
            read_only=read_only,
            idempotent=idempotent,
            approval_policy=approval,
            timeout_seconds=timeout,
        )


def test_a_tool_that_declares_nothing_keeps_its_pre_m10_schema_hash():
    """AC-32. Every manifest ever written names tools by these hashes."""
    hashes = [spec.schema_hash() for spec in golden_specs()]
    assert len(hashes) == 1944
    assert hashes[0] == GOLDEN_FIRST
    assert hashlib.sha256("".join(hashes).encode()).hexdigest() == GOLDEN_SHA256, (
        f"a tool that declares nothing no longer hashes as it did at {GOLDEN_COMMIT[:7]}"
    )


def test_declaring_a_default_keeps_the_hash_and_any_other_declaration_changes_it():
    """FR-40, FR-41: declared provenance, the output cap and a tool's bound
    configuration are covered by schema_hash, and the defaults hash as nothing."""
    base = ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA)
    assert base.max_output_chars == 50_000
    same = [
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, result_provenance=declaration(**INTERNAL)),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, max_output_chars=50_000),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, configuration={}),
    ]
    for spec in same:
        assert spec.schema_hash() == base.schema_hash()

    different = [
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, result_provenance=declaration(**EXTERNAL)),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, result_provenance=declaration(**{**EXTERNAL, "taint_flags": frozenset({TaintFlag.EXTERNAL_CONTENT})})),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, result_provenance=declaration(**{**INTERNAL, "trust_zone": TrustZone.VALIDATED})),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, result_provenance=declaration(**{**INTERNAL, "origin": Origin.MCP_RESOURCE})),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, result_provenance=declaration(**{**INTERNAL, "instruction_authority": InstructionAuthority.ADVISORY})),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, max_output_chars=49_999),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, max_output_chars=50_001),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, configuration={"root": "a"}),
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, configuration={"root": "b"}),
    ]
    hashes = [spec.schema_hash() for spec in different]
    assert base.schema_hash() not in hashes
    assert len(set(hashes)) == len(hashes)


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "100", None])
def test_an_invalid_output_cap_is_refused_at_construction_by_name(bad):
    """FR-41: a positive int, with no uncapped setting (D5)."""
    # The control first: without the field, the refusal below would be the
    # TypeError for an unknown keyword, whose message also names the field.
    assert ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, max_output_chars=1).max_output_chars == 1
    with pytest.raises((ValueError, TypeError, ToolError), match="max_output_chars"):
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, max_output_chars=bad)


def test_an_unstorable_configuration_or_a_wrong_provenance_type_is_refused_at_construction():
    # Controls first, for the same reason as the cap test above.
    ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, configuration={"root": "a"})
    ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, result_provenance=declaration(**EXTERNAL))
    with pytest.raises((ValueError, TypeError, ToolError), match="configuration"):
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, configuration={"root": "a\x00b"})
    with pytest.raises((ValueError, TypeError, ToolError), match="result_provenance"):
        ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, result_provenance=ContentProvenance.internal_tool())


PATHS = (
    "success",
    "raised",
    "timeout",
    "unstorable",
    "not found",
    "validation",
    "permission denied",
    "before_tool rejection",
)
RAN = {"raised": "ToolExecutionError", "timeout": "ToolTimeout", "unstorable": "ToolExecutionError"}
NOT_RAN = {
    "not found": "ToolNotFound",
    "validation": "ToolValidationError",
    "permission denied": "ToolPermissionDenied",
    "before_tool rejection": "ToolPermissionDenied",
}


def provenance_scenario(path, declared):
    ran = []

    async def fn(text):
        ran.append(text)
        if path == "raised":
            raise RuntimeError("the tool failed")
        if path == "timeout":
            await asyncio.sleep(5)
        if path == "unstorable":
            return "bad\x00value"
        return "fine"

    kwargs = dict(name="probe", description="probe", input_schema=ECHO_SCHEMA)
    if path == "timeout":
        kwargs["timeout_seconds"] = 0.05
    if declared:
        kwargs["result_provenance"] = declaration(**EXTERNAL)
    tool = Tool(ToolSpec(**kwargs), fn)
    return dict(
        tool=tool,
        arguments={"text": 1} if path == "validation" else {"text": "hi"},
        allowed=set() if path == "permission denied" else None,
        hook=Rejecting() if path == "before_tool rejection" else None,
        call_name="ghost" if path == "not found" else None,
        ran=ran,
    )


@pytest.mark.parametrize("declared", [False, True], ids=["default", "external"])
@pytest.mark.parametrize("path", PATHS)
async def test_every_executor_path_carries_the_provenance_fr40_states(path, declared):
    """AC-32. A tool that ran and failed may have put external bytes in the
    error text, so the error keeps the declared labels; a call stopped before the
    tool ran carries the executor's own error provenance."""
    s = provenance_scenario(path, declared)
    outcome, _ = await execute(
        s["tool"], s["arguments"], allowed=s["allowed"], hook=s["hook"], call_name=s["call_name"]
    )
    provenance = outcome.result.provenance
    expected = EXTERNAL if declared else INTERNAL
    if path == "success":
        assert isinstance(outcome, Completed)
        assert labels(provenance) == expected
        assert provenance.source_uri_or_hash == s["tool"].spec.schema_hash()
    elif path in RAN:
        assert isinstance(outcome, Failed)
        assert s["ran"], f"the {path} scenario never ran the tool, so it tests nothing"
        assert labels(provenance) == expected
        assert provenance.source_uri_or_hash == f"urn:agentsdk:tool-error:{RAN[path]}"
    else:
        assert isinstance(outcome, Failed)
        assert not s["ran"]
        assert provenance == ContentProvenance.executor_error(NOT_RAN[path])


async def test_a_failure_after_the_tool_ran_keeps_the_declared_labels_outside_the_named_paths_too():
    """The executor's last-resort catch knows whether the tool had run: a hook
    failing after an external tool ran is still external content in the error."""

    class AfterExplodes(RuntimeHook):
        def after_tool(self, result):
            raise RuntimeError("hook failed after the tool ran")

    class BeforeExplodes(RuntimeHook):
        def before_tool(self, tool_call):
            raise RuntimeError("hook failed before the tool ran")

    ran = []
    tool = Tool(
        ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA, result_provenance=declaration(**EXTERNAL)),
        lambda text: ran.append(text) or "fetched bytes",
    )
    outcome, _ = await execute(tool, {"text": "hi"}, hook=AfterExplodes())
    assert isinstance(outcome, Failed) and ran
    assert labels(outcome.result.provenance) == EXTERNAL
    assert outcome.result.provenance.source_uri_or_hash == "urn:agentsdk:tool-error:ToolExecutionError"

    ran.clear()
    outcome, _ = await execute(tool, {"text": "hi"}, hook=BeforeExplodes())
    assert isinstance(outcome, Failed) and not ran
    assert outcome.result.provenance == ContentProvenance.executor_error("ToolExecutionError")


async def test_a_tool_output_names_its_own_source():
    """How fetch records its final URL (FR-38): a ToolOutput's source becomes
    source_uri_or_hash; without one the schema hash stands, as before M10."""
    tool = Tool(
        ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA, result_provenance=declaration(**EXTERNAL)),
        lambda text: tools_module.ToolOutput("body", source_uri="https://example.test/final"),
    )
    outcome, _ = await execute(tool, {"text": "hi"})
    assert isinstance(outcome, Completed)
    assert outcome.result.content == "body"
    assert outcome.result.provenance.source_uri_or_hash == "https://example.test/final"
    assert labels(outcome.result.provenance) == EXTERNAL


CAPPED_PATHS = (
    "success",
    "raised",
    "timeout",
    "unstorable",
    "validation",
    "permission denied",
    "before_tool rejection",
    "after_tool substitution",
)


async def content_for(path, cap, k=60):
    """One executor path with max_output_chars=cap; k sizes what the path controls."""

    async def fn(text):
        if path == "raised":
            raise RuntimeError("e" * k)
        if path == "timeout":
            await asyncio.sleep(5)
        if path == "unstorable":
            return "bad\x00" + "u" * k
        return "s" * k

    kwargs = dict(name="probe", description="probe", input_schema=ECHO_SCHEMA, max_output_chars=cap)
    arguments, allowed, hook = {"text": "hi"}, None, None
    if path == "timeout":
        kwargs["timeout_seconds"] = 0.02
    if path == "validation":
        arguments = {"text": "v" * k}  # too long: jsonschema's message echoes it
    if path == "permission denied":
        kwargs["name"] = "p" * k
        allowed = set()
    if path == "before_tool rejection":
        hook = Rejecting("h" * k)
    if path == "after_tool substitution":
        hook = Substituting("a" * k)
    return await execute(Tool(ToolSpec(**kwargs), fn), arguments, allowed=allowed, hook=hook)


def assert_capped(result, payload, reference, cap):
    assert payload["original_length"] == len(reference)
    if len(reference) <= cap:
        assert result.content == reference
        assert payload["truncated"] is False
        return
    assert payload["truncated"] is True
    assert result.content.startswith(reference[:cap])
    marker = result.content[cap:]
    assert marker and len(marker) < 200, f"marker {marker!r}"
    assert str(len(reference)) in marker and str(cap) in marker, (
        f"the marker must state the original and the kept length: {marker!r}"
    )
    assert unstorable_reason(result.content) is None


@pytest.mark.parametrize("path", CAPPED_PATHS)
async def test_every_path_that_yields_content_is_cut_exactly_at_the_cap(path):
    """AC-33, over caps above, at, one below and far below the content length."""
    reference_outcome, _ = await content_for(path, 10**9)
    reference = reference_outcome.result.content
    assert len(reference) >= 20, reference
    for cap in (len(reference) + 1, len(reference), len(reference) - 1, len(reference) // 3, 1):
        outcome, payloads = await content_for(path, cap)
        assert payloads, "no ToolCalled event was emitted"
        assert_capped(outcome.result, payloads[-1], reference, cap)
        assert outcome.result.is_error is reference_outcome.result.is_error


async def test_a_hook_replacement_that_is_not_a_tool_result_or_not_text_cannot_escape_the_cap():
    """C4, M10 review round 1: the cap applied only to a ToolResult whose content
    was a str, so a hook could return anything else and it went out uncapped."""
    tool = Tool(
        ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA, max_output_chars=40,
                 result_provenance=declaration(**EXTERNAL)),
        lambda text: "fine",
    )

    class NotAResult(RuntimeHook):
        def after_tool(self, result):
            return HookOutcome(action=HookAction.MODIFY, replacement="x" * 1000)

    class NotText(RuntimeHook):
        def after_tool(self, result):
            return HookOutcome(action=HookAction.MODIFY, replacement=dataclasses.replace(result, content=["y" * 1000]))

    class ShapedLikeAResult(RuntimeHook):
        # Short text content, so nothing about it fails by accident: without an
        # explicit check it went out as the result (mutant M32 survived without this).
        def after_tool(self, result):
            return HookOutcome(
                action=HookAction.MODIFY,
                replacement=types.SimpleNamespace(
                    tool_call_id=result.tool_call_id, content="short", provenance=result.provenance, is_error=False
                ),
            )

    outcome, payloads = await execute(tool, {"text": "hi"}, hook=NotAResult())
    assert isinstance(outcome, Failed) and isinstance(outcome.result, ToolResult), type(outcome.result)
    assert outcome.result.is_error and "x" * 41 not in outcome.result.content
    assert labels(outcome.result.provenance) == EXTERNAL, "the tool ran, so its labels apply"

    outcome, payloads = await execute(tool, {"text": "hi"}, hook=ShapedLikeAResult())
    assert isinstance(outcome, Failed) and isinstance(outcome.result, ToolResult), type(outcome.result)
    # The error itself, not the result text: this tool caps output at 40 characters.
    assert "not a ToolResult" in str(outcome.error), outcome.error

    outcome, payloads = await execute(tool, {"text": "hi"}, hook=NotText())
    assert isinstance(outcome.result, ToolResult) and isinstance(outcome.result.content, str)
    assert "y" * 41 not in outcome.result.content
    assert payloads[-1]["truncated"] is True


class LyingText(str):
    """A str that lies about its length and its slices (M10 round 2, caveat 1)."""

    def __len__(self):
        return 3

    def __getitem__(self, index):
        return self


async def test_a_str_subclass_cannot_lie_its_way_past_the_cap():
    """Caveat 1, M10 review round 2: the cap read len() from the content, so a str
    subclass reporting 3 put 200000 characters through a 40-character cap, and
    ToolCalled recorded original_length 3. Both ways in are covered: the tool's
    own return value and a hook's replacement."""
    lie = LyingText("z" * 200_000)
    returning = Tool(ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA, max_output_chars=40),
                     lambda text: lie)
    honest = Tool(ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA, max_output_chars=40),
                  lambda text: "fine")
    for tool, hook in ((returning, None), (honest, Substituting(lie))):
        outcome, payloads = await execute(tool, {"text": "hi"}, hook=hook)
        content = outcome.result.content
        assert type(content) is str, type(content)
        assert str.__len__(content) < 40 + 200, str.__len__(content)
        assert payloads[-1]["original_length"] == 200_000 and payloads[-1]["truncated"] is True, payloads[-1]


def bypassing(result, **fields):
    """A copy of `result` with fields set past the constructor, as hostile caller
    code can: the checks in ToolResult.__post_init__ never see these values."""
    copy = dataclasses.replace(result)
    for name, value in fields.items():
        object.__setattr__(copy, name, value)
    return copy


class UncheckedResult(ToolResult):
    def __post_init__(self):  # skips every check ToolResult makes
        pass


async def test_whatever_a_hook_returns_is_checked_as_if_it_were_built_there():
    """Caveat 2, M10 review round 2: step 7's storability rule was not applied
    after after_tool, so a replacement that bypassed the constructor carried a
    NUL, a lone surrogate or a non-str id, completed in memory, and failed the run
    against Postgres. Every returned result must be storable, and must answer the
    call it was returned for."""
    tool = Tool(ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA), lambda text: "fine")
    shapes = {
        "NUL content": lambda r: bypassing(r, content="a\x00b"),
        "lone surrogate content": lambda r: bypassing(r, content="a\ud800b"),
        "int tool_call_id": lambda r: bypassing(r, tool_call_id=12345),
        "NUL tool_call_id": lambda r: bypassing(r, tool_call_id="c\x001"),
        "another call's id": lambda r: dataclasses.replace(r, tool_call_id="someone-else"),
        "non-bool is_error": lambda r: bypassing(r, is_error="no"),
        "unchecked subclass with NUL": lambda r: UncheckedResult(r.tool_call_id, "x\x00y", r.provenance),
        "NUL in provenance source": lambda r: dataclasses.replace(r, provenance=bypassing(r.provenance, source_uri_or_hash="s\x00")),
    }
    class InPlace(RuntimeHook):
        """No replacement at all: the result the hook was handed, changed where it
        lies. Checking only replacements would let this through."""

        def __init__(self, **fields):
            self.fields = fields

        def after_tool(self, result):
            for name, value in self.fields.items():
                object.__setattr__(result, name, value)
            return HookOutcome()

    hooks = {
        label: type("Replacing", (RuntimeHook,), {"after_tool": lambda self, result, make=make: HookOutcome(
            action=HookAction.MODIFY, replacement=make(result))})()
        for label, make in shapes.items()
    }
    hooks["in place: NUL content, no replacement"] = InPlace(content="a\x00b")
    hooks["in place: another call's id, no replacement"] = InPlace(tool_call_id="someone-else")
    wrong = {}
    for label, hook in hooks.items():
        outcome, payloads = await execute(tool, {"text": "hi"}, hook=hook)
        result = outcome.result
        problems = []
        if type(result) is not ToolResult:
            problems.append(f"returned a {type(result).__name__}")
        if result.tool_call_id != "c1" or type(result.tool_call_id) is not str:
            problems.append(f"answers {result.tool_call_id!r}")
        if not isinstance(result.is_error, bool):
            problems.append(f"is_error={result.is_error!r}")
        reason = unstorable_reason([result.tool_call_id, result.content, result.provenance.source_uri_or_hash])
        if reason is not None:
            problems.append(f"unstorable: {reason}")
        if payloads[-1]["is_error"] is not result.is_error:
            problems.append("ToolCalled disagrees with the result")
        if problems:
            wrong[label] = problems
    assert not wrong, wrong


class Shifting:
    """Answers the listed fields honestly on the first read and with `lies` after it.

    Mixed into a ToolResult or ContentProvenance subclass built past its
    constructor: round 3 checked is_error on one read and used a second.
    """

    lies: dict = {}

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        lies = type(self).lies
        if name in lies:
            reads = object.__getattribute__(self, "__dict__").setdefault("_reads", {})
            reads[name] = reads.get(name, 0) + 1
            if reads[name] > 1:
                return lies[name]
        return value


def built_past_the_constructor(cls, **fields):
    instance = object.__new__(cls)
    for name, value in fields.items():
        object.__setattr__(instance, name, value)
    return instance


def shifting(base, lies, **fields):
    cls = type(f"Shifting{base.__name__}", (Shifting, base), {"lies": lies, "__post_init__": lambda self: None})
    return built_past_the_constructor(cls, **fields)


HUGE = "z" * 5_000_000


async def test_every_field_of_a_returned_result_is_read_once_checked_and_used_as_checked():
    """M10 review round 3, rejected. Step 8 checked is_error on one read and built
    the result from a second, so a result answering False then 'NOT-A-BOOL' -- or
    5 MB of text -- completed, persisted and passed the 40-character cap through a
    field the cap does not measure. Provenance was read once per field and never
    type-checked, so a plain-string origin completed in memory and failed at
    Postgres on origin.value. Every case here must come back as a result every
    store can serialise, bounded in every field, or as a tool error."""
    from agentsdk.context import ContextAssembler
    from agentsdk.postgres import _provenance_to_json

    good = ContentProvenance.internal_tool("source")
    honest = dict(tool_call_id="c1", content="fine", provenance=good, is_error=False)
    bad_provenance = {
        "plain-string origin": built_past_the_constructor(ContentProvenance, **{**vars(good), "origin": "internal_tool"}),
        "plain-string trust zone": built_past_the_constructor(ContentProvenance, **{**vars(good), "trust_zone": "trusted_source"}),
        "taint as a list": built_past_the_constructor(ContentProvenance, **{**vars(good), "taint_flags": ["external_content"]}),
        "taint with a non-flag": built_past_the_constructor(ContentProvenance, **{**vars(good), "taint_flags": frozenset({"x"})}),
        "5 MB source": built_past_the_constructor(ContentProvenance, **{**vars(good), "source_uri_or_hash": HUGE}),
        "non-str source": built_past_the_constructor(ContentProvenance, **{**vars(good), "source_uri_or_hash": 12345}),
    }
    returned = {
        "is_error then a string": shifting(ToolResult, {"is_error": "NOT-A-BOOL"}, **honest),
        "is_error then 5 MB": shifting(ToolResult, {"is_error": HUGE}, **honest),
        "tool_call_id then another call": shifting(ToolResult, {"tool_call_id": "someone-else"}, **honest),
        "content then 5 MB with a NUL": shifting(ToolResult, {"content": HUGE + "\x00"}, **honest),
        # Every field of the second answer is bad: a per-field read of provenance
        # takes origin from the first, honest read and the rest from later reads.
        "provenance then entirely invalid": shifting(ToolResult, {"provenance": built_past_the_constructor(
            ContentProvenance, origin="internal_tool", instruction_authority="data_only", trust_zone="trusted_source",
            taint_flags=["external_content"], source_uri_or_hash=HUGE)}, **honest),
        "provenance whose origin shifts": built_past_the_constructor(
            ToolResult, **{**honest, "provenance": shifting(ContentProvenance, {"origin": "internal_tool"}, **vars(good))}),
        "provenance whose source shifts to 5 MB": built_past_the_constructor(
            ToolResult, **{**honest, "provenance": shifting(ContentProvenance, {"source_uri_or_hash": HUGE}, **vars(good))}),
        **{f"stable {label}": built_past_the_constructor(ToolResult, **{**honest, "provenance": p})
           for label, p in bad_provenance.items()},
    }
    tool = Tool(ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA, max_output_chars=40), lambda text: "fine")
    wrong = {}
    for label, value in returned.items():

        class Returning(RuntimeHook):
            def after_tool(self, result, value=value):
                return HookOutcome(action=HookAction.MODIFY, replacement=value)

        outcome, payloads = await execute(tool, {"text": "hi"}, hook=Returning())
        result, payload = outcome.result, payloads[-1]
        problems = []
        try:
            if type(result) is not ToolResult or type(result.provenance) is not ContentProvenance:
                problems.append(f"types {type(result).__name__}/{type(result.provenance).__name__}")
            if type(result.tool_call_id) is not str or result.tool_call_id != "c1":
                problems.append("tool_call_id")
            if type(result.is_error) is not bool or payload["is_error"] is not result.is_error:
                problems.append(f"is_error {type(result.is_error).__name__} / event {type(payload['is_error']).__name__}")
            if type(result.content) is not str or len(result.content) > 40 + 200:
                problems.append(f"content length {len(result.content)}")
            source = result.provenance.source_uri_or_hash
            if source is not None and (type(source) is not str or len(source) > 10_000):
                problems.append(f"source {type(source).__name__}")
            stored = json.dumps({"provenance": _provenance_to_json(result.provenance), "content": result.content,
                                 "is_error": result.is_error, "payload": payload})
            if len(stored) > 20_000:
                problems.append(f"serialised to {len(stored)} characters")
            ContextAssembler().build([Message(role=Role.TOOL, tool_results=(result,))])
            if unstorable_reason([result.tool_call_id, result.content, source]) is not None:
                problems.append("unstorable text")
        except Exception as exc:  # noqa: BLE001 - a store or assembler failing on it is the finding
            problems.append(f"{type(exc).__name__} while serialising: {str(exc)[:80]}")
        if problems:
            wrong[label] = problems
    assert not wrong, wrong


def forged_label(enum_cls, text, **attributes):
    """An instance whose type is exactly `enum_cls` but which is none of its members.

    The labels are str-mixin enums, so str.__new__ builds one; `type(x) is Origin`
    accepts it, and every store reads its `.value` (M10 review round 4, rejected).
    """
    instance = str.__new__(enum_cls, text)
    for name, value in attributes.items():
        setattr(instance, name, value)
    return instance


def is_member(value, enum_cls):
    return any(value is member for member in enum_cls)


async def test_provenance_labels_must_be_the_real_enum_members_not_lookalikes():
    """Round 4 was rejected: an exact-type check accepted a forged Origin with no
    _value_ (the run failed on both stores), with a 5 MB _value_ (stored past a
    40-character cap, then unreadable), with a NUL _value_ (failed on Postgres),
    and one spelled 'system' whose _value_ was 'user' (equal as a string, stored
    as the other label). Taint flags the same. Only identity with a real member
    is safe, and equality is not enough."""
    from agentsdk.context import ContextAssembler
    from agentsdk.postgres import _provenance_to_json

    good = ContentProvenance.internal_tool("source")
    forged = {
        "origin with no _value_": {"origin": forged_label(Origin, "internal_tool")},
        "origin with a 5 MB _value_": {"origin": forged_label(Origin, "internal_tool", _value_=HUGE)},
        "origin with a NUL _value_": {"origin": forged_label(Origin, "internal_tool", _value_="a\x00b")},
        "origin spelled system, valued user": {"origin": forged_label(Origin, "system", _value_="user")},
        "authority with a 5 MB _value_": {"instruction_authority": forged_label(InstructionAuthority, "data_only", _value_=HUGE)},
        "trust zone spelled trusted, valued untrusted": {"trust_zone": forged_label(TrustZone, "trusted_source", _value_="untrusted")},
        "taint flag with a 5 MB _value_": {"taint_flags": frozenset({forged_label(TaintFlag, "external_content", _value_=HUGE)})},
        "taint flag with no _value_": {"taint_flags": frozenset({forged_label(TaintFlag, "external_content")})},
    }
    tool = Tool(ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA, max_output_chars=40), lambda text: "fine")
    wrong = {}
    for label, fields in forged.items():
        provenance = built_past_the_constructor(ContentProvenance, **{**vars(good), **fields})

        class Returning(RuntimeHook):
            def after_tool(self, result, provenance=provenance):
                return HookOutcome(action=HookAction.MODIFY, replacement=dataclasses.replace(result, provenance=provenance))

        outcome, payloads = await execute(tool, {"text": "hi"}, hook=Returning())
        result, p = outcome.result, outcome.result.provenance
        problems = []
        try:
            if not (is_member(p.origin, Origin) and is_member(p.instruction_authority, InstructionAuthority)
                    and is_member(p.trust_zone, TrustZone)):
                problems.append("a label is not a real member")
            if type(p.taint_flags) is not frozenset or not all(is_member(flag, TaintFlag) for flag in p.taint_flags):
                problems.append("a taint flag is not a real member")
            stored = json.dumps({"provenance": _provenance_to_json(p), "content": result.content, "payload": payloads[-1]})
            if len(stored) > 20_000:
                problems.append(f"serialised to {len(stored)} characters")
            if unstorable_reason(stored) is not None:
                problems.append("unstorable")
            ContextAssembler().build([Message(role=Role.TOOL, tool_results=(result,))])
        except Exception as exc:  # noqa: BLE001 - a store or the assembler failing on it is the finding
            problems.append(f"{type(exc).__name__}: {str(exc)[:80]}")
        if problems:
            wrong[label] = problems
    assert not wrong, wrong


async def test_a_source_over_the_bound_is_refused_as_a_source():
    """Round 4 caveat: a redirect Location past the source bound failed with a
    message blaming label types, after the fetch had run. The refusal says which."""
    long_source = "https://e.test/" + "a" * 9000
    tool = Tool(
        ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA),
        lambda text: tools_module.ToolOutput("fine", source_uri=long_source),
    )
    outcome, _ = await execute(tool, {"text": "hi"})
    assert isinstance(outcome, Failed), outcome.result.content
    message = str(outcome.error)
    assert "source" in message and "label" not in message, message


async def test_tool_called_reports_the_error_state_of_the_result_actually_returned():
    """Caveat 3, M10 review round 2: step 9 emitted is_error False on every path
    that reached it, while a hook's replacement -- or the constructor sanitising
    one -- could return an error result."""
    tool = Tool(ToolSpec(name="probe", description="p", input_schema=ECHO_SCHEMA), lambda text: "fine")

    class MarksError(RuntimeHook):
        def after_tool(self, result):
            return HookOutcome(action=HookAction.MODIFY, replacement=dataclasses.replace(result, is_error=True))

    class Unstorable(RuntimeHook):
        def after_tool(self, result):
            return HookOutcome(action=HookAction.MODIFY, replacement=dataclasses.replace(result, content="bad\x00"))

    hooks = {"hook marks the result an error": MarksError(), "constructor sanitises the replacement": Unstorable()}
    for label, hook in hooks.items():
        outcome, payloads = await execute(tool, {"text": "hi"}, hook=hook)
        assert outcome.result.is_error is True, (label, outcome.result)
        assert payloads[-1]["is_error"] is True, (label, payloads[-1])
    outcome, payloads = await execute(tool, {"text": "hi"})
    assert outcome.result.is_error is False and payloads[-1]["is_error"] is False


async def test_a_not_found_result_is_cut_at_the_default_cap():
    """No tool resolved, so the default cap applies to the executor's error."""
    tool = Tool(ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA), lambda text: text)
    prefix = "ToolNotFound: no such tool: "
    for total in (49_999, 50_000, 50_001, 120_000):
        name = "g" * (total - len(prefix))
        outcome, payloads = await execute(tool, {"text": "hi"}, call_name=name)
        assert isinstance(outcome, Failed)
        assert_capped(outcome.result, payloads[-1], prefix + name, 50_000)


async def test_the_default_cap_is_50000_characters():
    tool = Tool(ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA), lambda text: "z" * 60_000)
    outcome, payloads = await execute(tool, {"text": "hi"})
    assert_capped(outcome.result, payloads[-1], "z" * 60_000, 50_000)


async def test_a_cut_through_non_bmp_text_keeps_whole_characters_and_stays_storable():
    for content in ("a" * 9 + "😀" + "b" * 5, "a" * 10 + "😀" * 5, "😀" * 20, "a" * 8 + "\U0010ffff" * 4):
        tool = Tool(
            ToolSpec(name="echo", description="d", input_schema=ECHO_SCHEMA, max_output_chars=10),
            lambda text, content=content: content,
        )
        outcome, payloads = await execute(tool, {"text": "hi"})
        assert isinstance(outcome, Completed), outcome.result.content
        assert_capped(outcome.result, payloads[-1], content, 10)
        outcome.result.content.encode("utf-8")


# =============================================================================
# File tree for AC-28 (real links on this host)
# =============================================================================


def short_name(path):
    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    assert length, f"GetShortPathNameW failed with {ctypes.GetLastError()}"
    alias = pathlib.PureWindowsPath(buffer.value).name
    assert alias.lower() != pathlib.PureWindowsPath(path).name.lower(), (
        f"this volume generated no 8.3 name for {pathlib.PureWindowsPath(path).name}"
    )
    return alias


CONTROL_FILES = ("name.with.dots.txt", "name with spaces.txt", "données-日本.txt")


@dataclasses.dataclass
class Tree:
    root: pathlib.Path
    outside: pathlib.Path
    token: str
    secret_name: str
    secret: str
    stream_secret: str
    short_file: str
    short_dir: str
    short_outside: str

    def leaks(self, content):
        """Outside content or an outside path, in any spelling, found in `content`."""
        folded = content.lower()
        needles = {
            self.secret,
            self.stream_secret,
            self.token,
            str(self.outside),
            self.outside.as_posix(),
            self.short_outside,
        }
        return sorted(needle for needle in needles if needle.lower() in folded)


def build_tree(base):
    token = uuid.uuid4().hex[:10]
    root = base / "root"
    outside = base / f"outside-{token}"
    secret_name = f"secret-{token}.txt"
    secret = f"OUTSIDE-SECRET-{token}"
    (outside / "nested").mkdir(parents=True)
    (outside / secret_name).write_text(secret, encoding="utf-8")
    (outside / "nested" / "deep.txt").write_text(secret, encoding="utf-8")

    # newline="\n": on Windows write_text would store "\r\n", and read returns the bytes on disk.
    (root / "sub" / "deeper").mkdir(parents=True)
    (root / "sub" / "deeper" / "file.txt").write_text("nested content\n", encoding="utf-8", newline="\n")
    for name in CONTROL_FILES:
        (root / name).write_text(f"control {name}\n", encoding="utf-8", newline="\n")
    (root / "averyveryverylongfilename.txt").write_text("long name\n", encoding="utf-8", newline="\n")
    (root / "a long directory name").mkdir()
    (root / "a long directory name" / "x.txt").write_text("in a long directory\n", encoding="utf-8", newline="\n")
    (root / "inside.txt").write_text("inside\n", encoding="utf-8", newline="\n")
    stream_secret = f"STREAM-SECRET-{token}"
    with open(str(root / "inside.txt") + ":hidden", "w", encoding="utf-8") as handle:
        handle.write(stream_secret)

    # Links out. Symlinks FAIL here without Developer Mode, by design (D7).
    os.symlink(outside / secret_name, root / "file_link")
    os.symlink(outside, root / "dir_link", target_is_directory=True)
    os.symlink(outside / "missing.txt", root / "dangling_link")
    _winapi.CreateJunction(str(outside), str(root / "junction_out"))
    os.link(outside / secret_name, root / "hard_link.txt")
    # Links in: controls that must keep working.
    _winapi.CreateJunction(str(root / "sub"), str(root / "junction_in"))
    os.symlink(root / "sub", root / "dir_link_in", target_is_directory=True)

    return Tree(
        root=root,
        outside=outside,
        token=token,
        secret_name=secret_name,
        secret=secret,
        stream_secret=stream_secret,
        short_file=short_name(root / "averyveryverylongfilename.txt"),
        short_dir=short_name(root / "a long directory name"),
        short_outside=short_name(outside),
    )


def remove_junctions(root):
    # shutil.rmtree on 3.11 descends into junctions; remove the links themselves.
    for name in ("junction_out", "junction_in"):
        with contextlib.suppress(OSError):
            os.rmdir(root / name)


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    built = build_tree(tmp_path_factory.mktemp("m10-confinement"))
    yield built
    remove_junctions(built.root)


def file_tools(root, **options):
    module = builtin()
    return {
        "read": module.read_file_tool(root, **options.get("read", {})),
        "list": module.list_directory_tool(root, **options.get("list", {})),
        "glob": module.glob_tool(root, **options.get("glob", {})),
        "grep": module.grep_tool(root, **options.get("grep", {})),
    }


def arguments_for(kind, value, tree=None):
    return {
        "read": {"path": value},
        "list": {"path": value},
        "glob": {"pattern": value},
        "grep": {"path": value, "text": tree.secret if tree else "needle"},
    }[kind]


def escape_forms(tree):
    """Every FR-36 escape form, as the string a model would send."""
    out, secret = tree.outside, tree.secret_name
    drive = tree.root.drive  # "C:"
    rel = str(out / secret)[len(drive):]  # "\Users\...\secret.txt"
    return {
        "dot-dot segments": [
            "..",
            "../",
            f"../{out.name}/{secret}",
            f"..\\{out.name}\\{secret}",
            f"sub/../../{out.name}/{secret}",
            f"sub\\..\\..\\{out.name}",
            "sub/../..",
            f"./../{out.name}",
            f"sub/deeper/../../../{out.name}/{secret}",
        ],
        "absolute paths": [
            str(out / secret),
            (out / secret).as_posix(),
            rel,
            rel.replace("\\", "/"),
            "/Windows/win.ini",
            "\\Windows\\win.ini",
            str(tree.root / "inside.txt"),
        ],
        "drive-letter paths": [
            f"{drive}\\",
            f"{drive}/Windows/win.ini",
            f"{drive}{rel}",
            f"{drive.lower()}{rel}",
        ],
        "drive-relative paths": [
            f"{drive}{secret}",
            f"{drive}..\\{out.name}\\{secret}",
            "C:x",
            "D:inside.txt",
        ],
        "UNC and device paths": [
            f"\\\\localhost\\{drive[0]}$\\{rel.lstrip(chr(92))}",
            f"\\\\127.0.0.1\\{drive[0].lower()}$\\{rel.lstrip(chr(92))}",
            f"\\\\?\\{out / secret}",
            f"\\\\.\\{out / secret}",
            f"//?/{(out / secret).as_posix()}",
            f"//./{(out / secret).as_posix()}",
            f"\\\\?\\UNC\\localhost\\{drive[0]}$\\{rel.lstrip(chr(92))}",
            "\\\\?\\GLOBALROOT\\Device\\Null",
            "\\\\.\\PhysicalDrive0",
            "\\??\\C:\\Windows\\win.ini",
        ],
        "reserved device names": [
            "CON", "con", "NUL", "nul.txt", "AUX", "PRN.log", "COM1", "com9.txt",
            "LPT1", "lpt3.dat", "CONIN$", "CONOUT$", "sub/NUL", "NUL ", "COM¹", "LPT².txt",
        ],
        "alternate data streams": [
            "inside.txt:hidden",
            "inside.txt::$DATA",
            "inside.txt:hidden:$DATA",
            "sub::$INDEX_ALLOCATION",
            "sub:$I30:$INDEX_ALLOCATION",
        ],
        "trailing dots and spaces": [
            "inside.txt.",
            "inside.txt ",
            "inside.txt. .",
            "sub./deeper/file.txt",
            "sub /deeper/file.txt",
            "sub\\deeper.\\file.txt",
            "...",
        ],
        "8.3 short names": [
            tree.short_file,
            f"{tree.short_dir}/x.txt",
            f"../{tree.short_outside}/{secret}",
        ],
        "links out": [
            "file_link",
            "dir_link",
            f"dir_link/{secret}",
            "junction_out",
            f"junction_out/{secret}",
            "junction_out/nested/deep.txt",
            "hard_link.txt",
            "dangling_link",
        ],
    }


def only_notes(content):
    """True when a listing holds no entries, only bracketed notes."""
    return all(line.startswith("[") for line in content.splitlines() if line.strip())


async def test_every_escape_form_is_refused_by_every_file_tool_without_disclosure(tree):
    """AC-28: every FR-36 escape form x every file tool x every path-bearing argument."""
    tools = file_tools(tree.root)
    forms = escape_forms(tree)
    assert sum(len(values) for values in forms.values()) >= 70
    failures = []
    for (category, values), kind in itertools.product(forms.items(), tools):
        for value in values:
            outcome, _ = await execute(tools[kind], arguments_for(kind, value, tree))
            content = outcome.result.content
            if kind == "glob" and category == "links out" and isinstance(outcome, Completed):
                refused = only_notes(content)  # a pattern naming a link out matches nothing
            else:
                refused = isinstance(outcome, Failed)
            leaked = tree.leaks(content)
            if not refused or leaked:
                failures.append(f"{kind} {category} {value!r}: refused={refused} leaked={leaked} -> {content[:160]!r}")
    assert not failures, f"{len(failures)} escapes:\n" + "\n".join(failures)


async def test_traversal_never_lists_matches_or_reads_through_a_link_out(tree):
    """AC-28: walking the whole root skips every link that resolves outside and
    every multiply linked file, and names nothing outside."""
    tools = file_tools(tree.root)
    out_names = {"file_link", "dir_link", "junction_out", "hard_link.txt", "dangling_link"}

    listed, _ = await execute(tools["list"], {"path": "."})
    assert isinstance(listed, Completed), listed.result.content
    entries = {line.rstrip("/") for line in listed.result.content.splitlines() if not line.startswith("[")}
    assert {"sub", "junction_in", "dir_link_in", "inside.txt", *CONTROL_FILES} <= entries
    assert not entries & out_names, entries & out_names

    for pattern in ("**/*", "**", "*", "**/secret*", "**/*.txt", "*/*"):
        globbed, _ = await execute(tools["glob"], {"pattern": pattern})
        assert isinstance(globbed, Completed), (pattern, globbed.result.content)
        lines = [line for line in globbed.result.content.splitlines() if not line.startswith("[")]
        assert not [line for line in lines if line.split("/")[0].rstrip("/") in out_names], (pattern, lines)
        assert not tree.leaks(globbed.result.content), pattern
    globbed, _ = await execute(tools["glob"], {"pattern": "**/*.txt"})
    assert {"sub/deeper/file.txt", "junction_in/deeper/file.txt", "dir_link_in/deeper/file.txt"} <= set(
        globbed.result.content.splitlines()
    )

    for needle in (tree.secret, "OUTSIDE-SECRET", tree.stream_secret):
        grepped, _ = await execute(tools["grep"], {"path": ".", "text": needle})
        assert isinstance(grepped, Completed), grepped.result.content
        assert only_notes(grepped.result.content), grepped.result.content
        assert not tree.leaks(grepped.result.content)
    grepped, _ = await execute(tools["grep"], {"path": ".", "text": "nested content"})
    assert "sub/deeper/file.txt:1:" in grepped.result.content
    assert "junction_in/deeper/file.txt:1:" in grepped.result.content


async def test_a_refusal_does_not_reveal_whether_an_outside_target_exists(tree):
    """NFR-14. The handle check alone refuses every existing outside target, but
    a missing one cannot be opened at all, so it would come back 'not found' --
    telling the model what exists outside. The spelling-resolved check refuses
    both alike. Found by mutation M6, which removed it with every other test green."""
    tools = file_tools(tree.root)
    pairs = [
        ("read", "file_link", "dangling_link"),
        ("read", f"junction_out/{tree.secret_name}", "junction_out/absent.txt"),
        ("read", f"dir_link/{tree.secret_name}", "dir_link/absent.txt"),
        ("list", "junction_out/nested", "junction_out/absent"),
        ("list", "dir_link", "dir_link/absent"),
        ("grep", f"junction_out/{tree.secret_name}", "junction_out/absent.txt"),
    ]
    for kind, existing, missing in pairs:
        shown = []
        for path in (existing, missing):
            outcome, _ = await execute(tools[kind], arguments_for(kind, path, tree))
            assert isinstance(outcome, Failed), (kind, path, outcome.result.content)
            shown.append(outcome.result.content)
        assert shown[0] == shown[1], f"{kind}: {existing!r} -> {shown[0]!r}, {missing!r} -> {shown[1]!r}"


async def test_the_control_set_succeeds(tree):
    """AC-28 controls: nested paths, dots, spaces, non-ASCII, and links that stay inside."""
    tools = file_tools(tree.root)
    reads = {
        "sub/deeper/file.txt": "nested content\n",
        "sub\\deeper\\file.txt": "nested content\n",
        "./sub/deeper/file.txt": "nested content\n",
        "junction_in/deeper/file.txt": "nested content\n",
        "dir_link_in/deeper/file.txt": "nested content\n",
        "averyveryverylongfilename.txt": "long name\n",
        "a long directory name/x.txt": "in a long directory\n",
        "inside.txt": "inside\n",
        **{name: f"control {name}\n" for name in CONTROL_FILES},
    }
    for path, expected in reads.items():
        outcome, _ = await execute(tools["read"], {"path": path})
        assert isinstance(outcome, Completed), (path, outcome.result.content)
        assert outcome.result.content == expected, path

    for path, expected in {"sub": "deeper/", "junction_in": "deeper/", "sub/deeper": "file.txt", "a long directory name": "x.txt"}.items():
        outcome, _ = await execute(tools["list"], {"path": path})
        assert isinstance(outcome, Completed), (path, outcome.result.content)
        assert expected in outcome.result.content.splitlines(), (path, outcome.result.content)

    for pattern, expected in {
        "sub/**/*.txt": "sub/deeper/file.txt",
        "*.txt": "inside.txt",
        "junction_in/*/*.txt": "junction_in/deeper/file.txt",
        "données-*.txt": "données-日本.txt",
        "NAME.WITH.*": "name.with.dots.txt",
    }.items():
        outcome, _ = await execute(tools["glob"], {"pattern": pattern})
        assert isinstance(outcome, Completed), (pattern, outcome.result.content)
        assert expected in outcome.result.content.splitlines(), (pattern, outcome.result.content)

    for path in ("sub", "junction_in/deeper/file.txt", "."):
        outcome, _ = await execute(tools["grep"], {"path": path, "text": "nested"})
        assert isinstance(outcome, Completed), (path, outcome.result.content)
        assert ":1: nested content" in outcome.result.content, (path, outcome.result.content)


async def test_a_link_swapped_between_the_check_and_the_open_cannot_escape(tmp_path):
    """AC-28, FR-36: the check is made on what was actually opened."""
    root, outside = tmp_path / "root", tmp_path / "outside-swap"
    (root / "swapdir").mkdir(parents=True)
    outside.mkdir()
    secret = f"SWAPPED-SECRET-{uuid.uuid4().hex}"
    (outside / "secret.txt").write_text(secret, encoding="utf-8")
    (root / "swap.txt").write_text("inside\n", encoding="utf-8")
    (root / "grepme.txt").write_text("inside\n", encoding="utf-8")
    (root / "swapdir" / "inner.txt").write_text("inside\n", encoding="utf-8")
    swapped = []

    def swap(path):
        name = pathlib.PureWindowsPath(path).name
        if name in ("swap.txt", "grepme.txt") and name not in swapped:
            os.remove(path)
            os.symlink(outside / "secret.txt", path)
            swapped.append(name)
        elif name == "swapdir" and name not in swapped:
            shutil.rmtree(path)
            _winapi.CreateJunction(str(outside), path)
            swapped.append(name)

    module = builtin()
    try:
        read = module.read_file_tool(root, _between_check_and_open=swap)
        outcome, _ = await execute(read, {"path": "swap.txt"})
        assert "swap.txt" in swapped, "the seam was never called, so nothing was swapped"
        assert isinstance(outcome, Failed) and secret not in outcome.result.content
        assert "outside-swap" not in outcome.result.content

        grep = module.grep_tool(root, _between_check_and_open=swap)
        outcome, _ = await execute(grep, {"path": ".", "text": "SWAPPED-SECRET"})
        assert "grepme.txt" in swapped
        assert secret not in outcome.result.content and "outside-swap" not in outcome.result.content

        listing = module.list_directory_tool(root, _between_check_and_open=swap)
        outcome, _ = await execute(listing, {"path": "swapdir"})
        assert "swapdir" in swapped
        assert isinstance(outcome, Failed)
        assert "secret.txt" not in outcome.result.content and "outside-swap" not in outcome.result.content
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(root / "swapdir")


async def test_file_tool_roots_are_checked_resolved_once_and_hashed(tmp_path):
    """FR-36: the root must exist and be a directory, and is covered by the schema hash."""
    module = builtin()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "file.txt").write_text("x")
    for factory in (module.read_file_tool, module.list_directory_tool, module.glob_tool, module.grep_tool):
        with pytest.raises((ValueError, OSError)):
            factory(tmp_path / "missing")
        with pytest.raises((ValueError, OSError)):
            factory(tmp_path / "file.txt")
        assert factory(tmp_path / "a").spec.schema_hash() != factory(tmp_path / "b").spec.schema_hash()
        assert factory(str(tmp_path / "a")).spec.schema_hash() == factory(tmp_path / "a" / "." ).spec.schema_hash()


# =============================================================================
# AC-29: bounds, binary files, literal grep, and no I/O on the event loop
# =============================================================================


async def test_reads_listings_globs_and_greps_are_capped(tmp_path):
    module = builtin()
    root = tmp_path
    (root / "big.txt").write_bytes(b"a" * 10_000)
    (root / "exact.txt").write_bytes(b"b" * 1000)
    (root / "boundary.txt").write_bytes(b"a" * 999 + "é".encode() + b"c" * 10)
    many = root / "many"
    many.mkdir()
    for i in range(12):
        (many / f"f{i:02}.txt").write_text("needle\n")
    (root / "matches.txt").write_text("needle\n" * 50)
    (root / "large-with-needle.txt").write_bytes(b"x" * 1500 + b"\nneedle-in-large\n")

    read = module.read_file_tool(root, max_bytes=1000)
    outcome, _ = await execute(read, {"path": "big.txt"})
    assert isinstance(outcome, Completed)
    assert outcome.result.content.startswith("a" * 1000) and "a" * 1001 not in outcome.result.content
    outcome, _ = await execute(read, {"path": "exact.txt"})
    assert outcome.result.content == "b" * 1000
    outcome, _ = await execute(read, {"path": "boundary.txt"})
    assert outcome.result.content.startswith("a" * 999)
    assert "é" not in outcome.result.content and "\ufffd" not in outcome.result.content

    listing = module.list_directory_tool(root, max_entries=5)
    outcome, _ = await execute(listing, {"path": "many"})
    entries = [line for line in outcome.result.content.splitlines() if not line.startswith("[")]
    assert len(entries) == 5, outcome.result.content
    assert any(line.startswith("[") for line in outcome.result.content.splitlines()), "no note that entries were cut"

    glob = module.glob_tool(root, max_results=5)
    outcome, _ = await execute(glob, {"pattern": "many/*.txt"})
    entries = [line for line in outcome.result.content.splitlines() if not line.startswith("[")]
    assert len(entries) == 5, outcome.result.content

    grep = module.grep_tool(root, max_matches=7, max_file_bytes=1000)
    outcome, _ = await execute(grep, {"path": "matches.txt", "text": "needle"})
    entries = [line for line in outcome.result.content.splitlines() if not line.startswith("[")]
    assert len(entries) == 7, outcome.result.content
    outcome, _ = await execute(grep, {"path": ".", "text": "needle-in-large"})
    assert only_notes(outcome.result.content), "grep searched a file larger than its cap"


async def test_binary_files_are_reported_and_never_returned_as_text(tmp_path):
    module = builtin()
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00needle\x00IHDR")
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9 needle \xff\xfe")
    read = module.read_file_tool(tmp_path)
    grep = module.grep_tool(tmp_path)
    for name in ("image.png", "latin1.txt"):
        outcome, _ = await execute(read, {"path": name})
        assert "needle" not in outcome.result.content and "PNG" not in outcome.result.content
        assert "binary" in outcome.result.content.lower(), outcome.result.content
    outcome, _ = await execute(grep, {"path": ".", "text": "needle"})
    assert only_notes(outcome.result.content), outcome.result.content


async def test_grep_is_literal_so_a_catastrophic_regex_is_just_text(tmp_path):
    module = builtin()
    (tmp_path / "evil.txt").write_text("a" * 50_000 + "!\n" + "(a+)+$ appears literally\n" + "x" * 5000 + "\n")
    grep = module.grep_tool(tmp_path)
    for needle, expected in (("(a+)+$", "evil.txt:2:"), ("(x+x+)+y", None), (".*", None)):
        started = time.monotonic()
        outcome, _ = await execute(grep, {"path": ".", "text": needle})
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"grep for {needle!r} took {elapsed:.2f} s"
        assert isinstance(outcome, Completed)
        if expected:
            assert expected in outcome.result.content
        else:
            assert only_notes(outcome.result.content), (needle, outcome.result.content[:200])


def spy_on_file_io(monkeypatch):
    """AC-14's method: record the thread of every call into the file layer."""
    fs = builtin()._fs
    log = []
    members = [name for name, member in inspect.getmembers(fs, callable) if not name.startswith("__")]
    assert members, "the file layer exposes no operations, so the spy sees nothing"
    for name in members:
        real = getattr(fs, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            log.append((_name, threading.get_ident()))
            return _real(*args, **kwargs)

        monkeypatch.setattr(fs, name, spy)
    return log


async def test_the_walk_budget_is_read_between_entries_not_only_between_directories(tmp_path, monkeypatch):
    """C5, M10 review round 1: one flat directory of 24000 files took 20.8 s per
    grep against a 20 s budget, because the clock was read only when a directory
    was opened. A fake clock that advances a second per reading shows how many
    files are still opened after the budget is spent -- a count, not a stopwatch."""
    module = builtin()
    for i in range(200):
        (tmp_path / f"f{i:03}.txt").write_text("needle\n", encoding="utf-8")
    tools = [
        (module.grep_tool(tmp_path), {"path": ".", "text": "needle"}),
        (module.glob_tool(tmp_path), {"pattern": "*.txt"}),
    ]
    ticks = itertools.count()
    monkeypatch.setattr(module, "time", types.SimpleNamespace(monotonic=lambda: float(next(ticks))))
    monkeypatch.setattr(module, "_WALK_SECONDS", 5.0)
    log = spy_on_file_io(monkeypatch)
    for tool, arguments in tools:
        before = len(log)
        outcome, _ = await execute(tool, arguments)
        opened = sum(1 for name, _ in log[before:] if name == "open_handle")
        assert opened < 50, f"{tool.name} opened {opened} of 200 files with a 5-tick budget"
        assert "stopped early" in outcome.result.content, outcome.result.content[-300:]


async def test_no_file_io_runs_on_the_event_loop_thread(tree, monkeypatch):
    tools = file_tools(tree.root)
    log = spy_on_file_io(monkeypatch)
    loop_thread = threading.get_ident()
    for kind, arguments in (
        ("read", {"path": "sub/deeper/file.txt"}),
        ("read", {"path": "file_link"}),
        ("list", {"path": "."}),
        ("glob", {"pattern": "**/*"}),
        ("grep", {"path": ".", "text": "nested"}),
    ):
        before = len(log)
        outcome, _ = await execute(tools[kind], arguments)
        mine = log[before:]
        assert mine, f"{kind} made no call the spy could see"
        on_loop = [name for name, ident in mine if ident == loop_thread]
        assert not on_loop, f"{kind} ran {on_loop} on the event loop's thread"


def test_the_builtin_module_does_its_file_io_only_through_the_spied_layer():
    """The spy above sees only what goes through _fs. Any direct call to an I/O
    primitive elsewhere in the module is invisible to it, so it is refused here."""
    module_path = REPO / "agentsdk" / "builtin_tools.py"
    source = module_path.read_text(encoding="utf-8")
    parsed = ast.parse(source)
    layer = [node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == "_FileSystem"]
    assert layer, "no _FileSystem class"
    inside = {id(node) for node in ast.walk(layer[0])}
    io_roots = {"os", "msvcrt", "ctypes", "_winapi", "pathlib", "io", "shutil", "glob", "fnmatch_io", "kernel32", "_kernel32"}
    io_names = {"open", "stat", "lstat", "fstat", "scandir", "listdir", "walk", "readlink", "realpath",
                "exists", "isdir", "isfile", "islink", "read", "get_osfhandle", "resolve", "iterdir",
                "read_text", "read_bytes", "glob", "rglob", "CreateFileW", "GetFinalPathNameByHandleW"}

    def root_name(node):
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    offenders = []
    for node in ast.walk(parsed):
        if id(node) in inside or not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            offenders.append((node.lineno, "open"))
        elif isinstance(func, ast.Attribute) and func.attr in io_names and root_name(func) in io_roots:
            offenders.append((node.lineno, ast.unparse(func)))
    assert not offenders, f"file I/O outside _FileSystem: {offenders}"


async def test_no_file_tool_input_raises_past_the_executor(tree):
    """NFR-14 over hostile strings, within each schema's length limit."""
    tools = file_tools(tree.root)
    hostile = ["", "\x00", "a\x00b", "\ud800", "?", "*", "<>", "|", '"', "\t", "\u202e", "a" * 1024,
               "/" * 1000, "\\" * 1000, ".." + "/.." * 300, "\U0001f4a5", "\r\n", "%2e%2e/", "~", "$MFT", "sub/*/..",
               "**/../../**", "[", "[!]", "{a,b}"]
    for kind, value in itertools.product(tools, hostile):
        outcome, _ = await execute(tools[kind], arguments_for(kind, value))
        assert not tree.leaks(outcome.result.content), (kind, value)


# =============================================================================
# FR-35, AC-27: opt-in only
# =============================================================================


def test_importing_agentsdk_does_not_import_the_builtin_tools():
    assert importlib.util.find_spec("agentsdk.builtin_tools") is not None, "the module does not exist"
    probe = "import sys, agentsdk\nprint('agentsdk.builtin_tools' in sys.modules)\n"
    ran = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO, env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True, text=True, timeout=120,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "False"


def test_a_runner_given_no_tools_holds_none():
    assert len(Runner({"m": Scripted()})._registry) == 0


class FakeBackend:
    def __init__(self, results=(), raises=None):
        self.results, self.raises, self.calls = results, raises, []

    async def search(self, query, max_results):
        self.calls.append((query, max_results))
        if self.raises is not None:
            raise self.raises
        return self.results


async def test_every_builtin_is_read_only_and_an_empty_profile_denies_it_untouched(tmp_path, monkeypatch):
    """AC-27, with a spy on each implementation's only way to do anything."""
    module = builtin()
    (tmp_path / "f.txt").write_text("x")
    net = Net()
    backend = FakeBackend([module.SearchResult("t", "https://e.test/", "s")])
    built = [
        (module.read_file_tool(tmp_path), {"path": "f.txt"}),
        (module.list_directory_tool(tmp_path), {"path": "."}),
        (module.glob_tool(tmp_path), {"pattern": "*"}),
        (module.grep_tool(tmp_path), {"path": ".", "text": "x"}),
        (net.tool(["allowed.test"]), {"url": "http://allowed.test/"}),
        (module.web_search_tool(backend), {"query": "q"}),
    ]
    for tool, _ in built:
        assert tool.spec.risk_class is RiskClass.READ_ONLY and tool.spec.read_only is True, tool.name
    file_log = spy_on_file_io(monkeypatch)  # after construction: resolving the root is not a call

    model = Scripted(
        calls(*[ToolCall(id=f"c{i}", name=tool.name, arguments=args) for i, (tool, args) in enumerate(built)]),
        text("done"),
    )
    runner = Runner({"m": model}, tools=[tool for tool, _ in built])
    result = await runner.run(AgentSpec(id="a", instructions="x"), "go", RunConfig(tenant_id="t", project_id="p"))
    assert result.status is RunStatus.COMPLETED, result.error
    denied = [e.payload for e in result.events if e.event_type.value == "ToolCalled"]
    assert len(denied) == len(built)
    assert all(p["is_error"] and p["error_type"] == "ToolPermissionDenied" for p in denied), denied
    assert file_log == [] and net.resolved == [] and net.dialed == [] and backend.calls == []


# =============================================================================
# FR-38, AC-30: fetch
# =============================================================================


class Server:
    """A raw HTTP/1.1 server on 127.0.0.1 that records every connection and request head."""

    def __init__(self, respond, *, ssl_context=None):
        self.respond, self.ssl_context = respond, ssl_context
        self.connections, self.heads, self.closed = 0, [], False

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self.ssl_context)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        self.server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.server.wait_closed(), 2)

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)).decode("latin-1")
            self.heads.append(head)
            await self.respond(self, head, writer)
        except Exception:  # noqa: BLE001 - a client that hangs up is not a test failure
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()


def request_path(head):
    return head.split(" ", 2)[1]


def header_of(head, name):
    for line in head.split("\r\n")[1:]:
        key, _, value = line.partition(":")
        if key.strip().lower() == name:
            return value.strip()
    return None


def ok(body=b"hello from the server", content_type="text/plain; charset=utf-8", extra=""):
    async def respond(server, head, writer):
        kind = "" if content_type is None else f"Content-Type: {content_type}\r\n"
        writer.write(
            f"HTTP/1.1 200 OK\r\n{kind}Content-Length: {len(body)}\r\n{extra}Connection: close\r\n\r\n".encode() + body
        )
        await writer.drain()

    return respond


def redirect_bytes(location, extra=""):
    return f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n{extra}Connection: close\r\n\r\n".encode()


def routes(table):
    """Respond by request path: a bytes response, or a responder."""

    async def respond(server, head, writer):
        answer = table[request_path(head)]
        if callable(answer):
            await answer(server, head, writer)
        else:
            writer.write(answer)
            await writer.drain()

    return respond


PUBLIC_V4, OTHER_V4, PUBLIC_V6 = "1.1.1.1", "8.8.4.4", "2606:4700:4700::1111"


class Net:
    """The fetch tool's test seams. Resolution answers from a table (a public
    address by default); dialling maps a checked public address onto a local
    server. An unmapped public address goes to a closed local port, never out
    to the internet; a loopback address passes through unchanged, so a tool
    that dials one is caught by the server listening there."""

    def __init__(self, answers=None, routes=None):
        self.answers, self.routes = answers or {}, routes or {}
        self.resolved, self.dialed = [], []

    async def resolve(self, host, port):
        self.resolved.append(host)
        answer = self.answers.get(host, [PUBLIC_V4])
        return answer() if callable(answer) else list(answer)

    def connect(self, ip, port):
        self.dialed.append((ip, port))
        if (ip, port) in self.routes:
            return self.routes[(ip, port)]
        if ipaddress.ip_address(ip.split("%")[0]).is_loopback:
            return (ip, port)
        return ("127.0.0.1", 9)

    def tool(self, allowlist, **options):
        return builtin().fetch_tool(allowlist, _resolve=self.resolve, _connect=self.connect, **options)


ALLOWLIST = ["allowed.test", "*.wild.test", "bücher.test", "Mixed.Case.TEST.", "internal.test", "other-allowed.test"]
HOST_CASES = [
    ("http://allowed.test/", "allowed.test"),
    ("http://ALLOWED.test/", "allowed.test"),
    ("HTTP://allowed.test/", "allowed.test"),
    ("http://allowed.test./", "allowed.test"),
    ("http://mixed.case.test/", "mixed.case.test"),
    ("http://xn--bcher-kva.test/", "xn--bcher-kva.test"),
    ("http://BÜCHER.test/", "xn--bcher-kva.test"),
    ("http://allowed.test:8080/", "allowed.test:8080"),
    ("http://a.wild.test/", "a.wild.test"),
    ("http://deep.er.wild.test/", "deep.er.wild.test"),
    ("http://wild.test/", None),
    ("http://evilwild.test/", None),
    ("http://wild.test.evil.net/", None),
    ("http://allowed.test.evil.net/", None),
    ("http://evilallowed.test/", None),
    ("http://evil.net/allowed.test", None),
    ("http://user:pw@allowed.test/", None),
    ("http://user@allowed.test/", None),
    ("http://:@allowed.test/", None),
    ("http://allowed.test@evil.net/", None),
    ("http://allowed.test\\@evil.net/", None),
    ("http:\\\\allowed.test\\", None),
    ("http://allowed.test\\.evil.net/", None),
    ("http://%61llowed.test/", None),
    ("http://allowed%2etest/", None),
    ("http://allowed.test:99999/", None),
    ("http://allowed.test /", None),
    ("http://allowed.test\t/", None),
    ("http://allowed.test\n.evil.net/", None),
    ("ftp://allowed.test/", None),
    ("file:///C:/Windows/win.ini", None),
    ("gopher://allowed.test/", None),
    ("//allowed.test/", None),
    ("allowed.test", None),
    ("javascript:alert(1)", None),
    ("http://[::1]/", None),
]


async def test_host_spellings_decide_exactly_as_fr38_states():
    async with Server(ok()) as server:
        net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port), (PUBLIC_V4, 8080): ("127.0.0.1", server.port)})
        tool = net.tool(ALLOWLIST)
        wrong = []
        for url, host_header in HOST_CASES:
            before_connections, before_dials = server.connections, len(net.dialed)
            outcome, _ = await execute(tool, {"url": url})
            reached = server.connections > before_connections
            if host_header is None:
                if isinstance(outcome, Completed) or reached or len(net.dialed) > before_dials:
                    wrong.append(f"{url!r} should be refused: {outcome.result.content[:120]!r}")
            else:
                if not isinstance(outcome, Completed) or not reached:
                    wrong.append(f"{url!r} should be fetched: {outcome.result.content[:120]!r}")
                elif header_of(server.heads[-1], "host") != host_header:
                    wrong.append(f"{url!r} sent Host {header_of(server.heads[-1], 'host')!r}")
        assert not wrong, "\n".join(wrong)

        empty = net.tool([])
        before = server.connections
        outcome, _ = await execute(empty, {"url": "http://allowed.test/"})
        assert isinstance(outcome, Failed) and server.connections == before


def non_global_samples():
    """Every special-purpose network the ipaddress module defines, both families,
    sampled at its first, middle and last address, plus embedded IPv4 forms."""
    networks = []
    for constants in (ipaddress._IPv4Constants, ipaddress._IPv6Constants):
        for value in vars(constants).values():
            if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                networks.append(value)
            elif isinstance(value, list):
                networks.extend(v for v in value if isinstance(v, (ipaddress.IPv4Network, ipaddress.IPv6Network)))
    networks.append(ipaddress.ip_network("100.64.0.0/10"))
    samples = set()
    for network in networks:
        for address in (network.network_address, network.broadcast_address, network.network_address + network.num_addresses // 2):
            samples.add(address)
    v4 = [a for a in samples if a.version == 4]
    for address in v4:
        packed = int(address)
        samples.add(ipaddress.IPv6Address(f"::ffff:{address}"))
        samples.add(ipaddress.IPv6Address((0x2002 << 112) | (packed << 80)))
        samples.add(ipaddress.IPv6Address(f"64:ff9b::{address}"))
        samples.add(ipaddress.IPv6Address(f"::{address}"))
        samples.add(ipaddress.IPv6Address((0x2001 << 112) | (packed ^ 0xFFFFFFFF)))  # Teredo client
    return sorted(samples, key=lambda a: (a.version, int(a)))


async def test_every_non_global_address_class_is_refused_with_no_connection():
    samples = non_global_samples()
    # The generator samples each network at its edges and middle, which misses
    # the addresses an attacker actually names; those are added by hand.
    for spelled in ("127.0.0.1", "::1", "169.254.169.254", "10.0.0.1", "192.168.1.1", "::ffff:127.0.0.1",
                    "fe80::1", "100.64.0.1", "224.0.0.251", "fd00::1", "0.0.0.0", "::"):
        address = ipaddress.ip_address(spelled)
        if address not in samples:
            samples.append(address)
    assert len(samples) > 200, f"only {len(samples)} samples"
    async with Server(ok()) as server:
        current = {}
        net = Net(
            answers={"allowed.test": lambda: current["answer"]},
            routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)},
        )
        tool = net.tool(["allowed.test"])
        for sample in samples:
            for answer in ([str(sample)], [PUBLIC_V4, str(sample)], [str(sample), PUBLIC_V6]):
                current["answer"] = answer
                outcome, _ = await execute(tool, {"url": "http://allowed.test/"})
                # Checked per answer, not collected: an escaped address dials, and
                # thousands of dials make a broken check take many minutes to fail.
                assert isinstance(outcome, Failed), f"fetched through non-global answer {answer}"
                assert server.connections == 0 and net.dialed == [], f"a connection was made for {answer}"

        current["answer"] = [PUBLIC_V4]
        outcome, _ = await execute(tool, {"url": "http://allowed.test/"})
        assert isinstance(outcome, Completed), "the control failed, so the refusals prove nothing"


NUMERIC_HOSTS = [
    "127.1", "127.0.1", "2130706433", "0x7f000001", "0177.0.0.1", "0x7f.0.0.1", "0177.1", "017700000001",
    "127.000.000.001", "127.0.0.1", "0", "0.0.0.0", "10.0.0.1", "192.168.1.1", "100.64.0.1", "224.0.0.1",
    "169.254.169.254", "2852039166", "0xa9fea9fe", "0251.0376.0251.0376", "255.255.255.255",
    "[::1]", "[0:0:0:0:0:0:0:1]", "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "[::127.0.0.1]",
    "[64:ff9b::127.0.0.1]", "[2002:7f00:1::]", "[fe80::1]", "[::]", "[fc00::1]", "[ff02::1]",
]


async def test_numeric_and_embedded_host_spellings_are_refused_without_being_resolved_as_names():
    """A numeric host is an address, not a name to look up: resolving it would
    let the resolver's public answer stand in for the loopback it spells."""
    async with Server(ok()) as server:
        net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
        for host in NUMERIC_HOSTS:
            try:
                tool = net.tool([host])
            except ValueError:
                # httpx will not parse some numeric spellings as a host at all
                # (leading-zero octets), so they cannot be allowlisted; the URL
                # must still be refused without a lookup or a connection.
                tool = net.tool(["allowed.test"])
            outcome, _ = await execute(tool, {"url": f"http://{host}/"})
            assert isinstance(outcome, Failed), (host, outcome.result.content)
        assert server.connections == 0 and net.dialed == [], net.dialed

        control = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
        outcome, _ = await execute(control.tool([PUBLIC_V4]), {"url": f"http://{PUBLIC_V4}/"})
        assert isinstance(outcome, Completed), outcome.result.content
        assert control.resolved == [], "an IP literal was sent to the resolver"


async def test_a_redirect_is_refused_at_the_first_hop_that_leaves_the_allowlist_or_the_public_internet():
    async with Server(ok(b"should never be reached")) as elsewhere:
        table = {
            "/to-other": redirect_bytes("http://other.test/"),
            "/to-internal": redirect_bytes("http://internal.test/"),
            "/to-loopback": redirect_bytes(f"http://127.0.0.1:{elsewhere.port}/"),
            "/to-userinfo": redirect_bytes("http://user:pw@allowed.test/final"),
            "/to-file": redirect_bytes("file:///C:/Windows/win.ini"),
            "/to-backslash": redirect_bytes("http://allowed.test\\@other.test/"),
            "/to-final": redirect_bytes("/final"),
            "/final": ok(b"the final body"),
        }
        async with Server(routes(table)) as server:
            net = Net(
                answers={"other.test": [OTHER_V4], "internal.test": ["10.0.0.7"]},
                routes={
                    (PUBLIC_V4, 80): ("127.0.0.1", server.port),
                    (OTHER_V4, 80): ("127.0.0.1", elsewhere.port),
                    ("10.0.0.7", 80): ("127.0.0.1", elsewhere.port),
                },
            )
            tool = net.tool(["allowed.test", "internal.test"])
            for path in ("/to-other", "/to-internal", "/to-loopback", "/to-userinfo", "/to-file", "/to-backslash"):
                before = len(server.heads)
                outcome, _ = await execute(tool, {"url": f"http://allowed.test{path}"})
                assert len(server.heads) == before + 1, f"{path}: the first hop was not made"
                assert isinstance(outcome, Failed), (path, outcome.result.content)
            assert elsewhere.connections == 0

            outcome, _ = await execute(tool, {"url": "http://allowed.test/to-final"})
            assert isinstance(outcome, Completed), outcome.result.content
            assert "the final body" in outcome.result.content
            assert outcome.result.provenance.source_uri_or_hash == "http://allowed.test/final"


async def test_the_redirect_cap_holds():
    async def hops(server, head, writer):
        number = int(request_path(head).rsplit("/", 1)[1])
        if number == 3:
            await ok(b"arrived")(server, head, writer)
        else:
            writer.write(redirect_bytes(f"/hop/{number + 1}"))
            await writer.drain()

    async with Server(hops) as server:
        net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
        outcome, _ = await execute(net.tool(["allowed.test"], max_redirects=3), {"url": "http://allowed.test/hop/0"})
        assert isinstance(outcome, Completed) and "arrived" in outcome.result.content, outcome.result.content
        before = len(server.heads)
        outcome, _ = await execute(net.tool(["allowed.test"], max_redirects=2), {"url": "http://allowed.test/hop/0"})
        assert isinstance(outcome, Failed), outcome.result.content
        assert len(server.heads) - before == 3, "the tool followed more redirects than its cap"


async def test_a_resolver_that_answers_public_then_loopback_cannot_produce_a_loopback_connection():
    async with Server(ok(b"public")) as public, Server(ok(b"LOOPBACK")) as loopback:
        answers = iter([[PUBLIC_V4]])
        net = Net(
            answers={"allowed.test": lambda: next(answers, ["127.0.0.1"])},
            routes={(PUBLIC_V4, loopback.port): ("127.0.0.1", public.port)},
        )
        outcome, _ = await execute(net.tool(["allowed.test"]), {"url": f"http://allowed.test:{loopback.port}/"})
        assert loopback.connections == 0, "a second DNS answer reached loopback"
        assert isinstance(outcome, Completed) and "public" in outcome.result.content, outcome.result.content


async def test_environment_proxies_receive_nothing(monkeypatch):
    async with Server(ok()) as proxy, Server(ok(b"direct")) as target:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", target.port)})
        outcome, _ = await execute(net.tool(["allowed.test"]), {"url": "http://allowed.test/"})
        assert isinstance(outcome, Completed), outcome.result.content
        assert proxy.connections == 0 and target.connections == 1


async def test_no_credential_or_cookie_is_sent_on_any_hop(monkeypatch):
    key, password, cookie = "m10-model-key-value-1234", "m10-database-password-5678", "m10-cookie-value-9012"
    monkeypatch.setenv("MODEL_API_KEY", key)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://user:{password}@localhost:5432/db")
    table = {
        "/start": redirect_bytes("/next", extra=f"Set-Cookie: session={cookie}; Path=/\r\n"),
        "/next": ok(b"done"),
    }
    async with Server(routes(table)) as server:
        net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
        outcome, _ = await execute(net.tool(["allowed.test"]), {"url": "http://allowed.test/start"})
        assert isinstance(outcome, Completed), outcome.result.content
        assert len(server.heads) == 2
        for head in server.heads:
            names = {line.partition(":")[0].strip().lower() for line in head.split("\r\n")[1:] if line}
            assert not names & {"authorization", "cookie", "proxy-authorization"}, head
            for value in (key, password, cookie):
                assert value not in head, head


async def test_an_endless_body_stops_at_the_byte_cap():
    async def endless(server, head, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n")
        while not server.closed:
            writer.write(b"x" * 65536)
            await writer.drain()

    async with Server(endless) as server:
        net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
        started = time.monotonic()
        outcome, _ = await execute(net.tool(["allowed.test"], max_bytes=100_000, timeout_seconds=10), {"url": "http://allowed.test/"})
        assert time.monotonic() - started < 5
        assert isinstance(outcome, Completed), outcome.result.content[:200]
        assert 0 < outcome.result.content.count("x") <= 100_000


@functools.lru_cache(maxsize=None)
def bomb(encoding):
    compressor = zlib.compressobj(9, zlib.DEFLATED, {"gzip": 31, "deflate": 15}[encoding])
    chunk = b"A" * (1 << 20)
    return b"".join([compressor.compress(chunk) for _ in range(128)] + [compressor.flush()])


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_a_compression_bomb_is_bounded_while_it_decodes(encoding):
    """128 MiB compressed into about 125 KB: decoding a whole network chunk before
    counting would allocate tens of megabytes, so the peak is what is asserted."""
    body = bomb(encoding)

    async def respond(server, head, writer):
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Encoding: {encoding}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )
        await writer.drain()

    async with Server(respond) as server:
        net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
        tool = net.tool(["allowed.test"], max_bytes=1_000_000)
        tracemalloc.start()
        try:
            outcome, _ = await execute(tool, {"url": "http://allowed.test/"})
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert isinstance(outcome, Completed), outcome.result.content[:200]
        assert 0 < outcome.result.content.count("A") <= 1_000_000
        assert peak < 32 * 2**20, f"decoding peaked at {peak / 2**20:.1f} MiB"


async def test_a_trickling_or_silent_server_stops_at_the_total_deadline():
    async def trickle(server, head, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n")
        while not server.closed:
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.1)

    async def silent(server, head, writer):
        while not server.closed:
            await asyncio.sleep(0.1)

    for respond in (trickle, silent):
        async with Server(respond) as server:
            net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
            started = time.monotonic()
            outcome, _ = await execute(net.tool(["allowed.test"], timeout_seconds=1.0), {"url": "http://allowed.test/"})
            elapsed = time.monotonic() - started
            assert isinstance(outcome, Failed), (respond.__name__, outcome.result.content[:200])
            assert elapsed < 3.0, f"{respond.__name__}: {elapsed:.2f} s against a 1 s deadline"


async def test_a_non_text_content_type_is_refused():
    for content_type, allowed in (
        ("image/png", False), (None, False), ("application/octet-stream", False), ("application/zip", False),
        ("text/html; charset=utf-8", True), ("text/plain", True), ("application/json", True), ("application/xml", True),
    ):
        async with Server(ok(b"body-bytes", content_type=content_type)) as server:
            net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
            outcome, _ = await execute(net.tool(["allowed.test"]), {"url": "http://allowed.test/"})
            assert isinstance(outcome, Completed) is allowed, (content_type, outcome.result.content)
            assert ("body-bytes" in outcome.result.content) is allowed


def non_text_codec_names():
    """Every codec name codecs.lookup resolves that is not a text encoding.

    Enumerated from the encodings package rather than listed, so a codec added
    to a later Python is covered without anyone remembering it.
    """
    import codecs
    import encodings
    import encodings.aliases

    names = set(encodings.aliases.aliases) | set(encodings.aliases.aliases.values())
    names |= {path.stem for path in pathlib.Path(encodings.__file__).parent.glob("*.py") if path.stem != "__init__"}
    found = set()
    for name in names:
        try:
            info = codecs.lookup(name)
        except LookupError:
            continue
        if not getattr(info, "_is_text_encoding", True):
            found.add(name)
    return sorted(found)


def charset_payload(name):
    """A body the named codec turns into far more than it is, where it can."""
    import codecs

    plain = b"A" * (4 << 20)
    try:
        return codecs.encode(plain, name)
    except Exception:  # noqa: BLE001 - rot13 takes str; the name alone must be refused
        return plain[:1000]


async def test_a_charset_that_is_not_a_text_encoding_is_refused_as_a_charset():
    """D1, M10 review round 1. The server names the charset, and codecs.lookup also
    resolves bytes-to-bytes codecs: charset=zlib_codec decompressed an 8 KB body
    far past the decoded-byte bound under python -O. By default it failed closed
    only because a standard-library assert rejects errors='replace', so the
    refusal must be the tool's own, and say it is about the charset."""
    names = non_text_codec_names()
    assert {"zlib_codec", "bz2_codec", "base64_codec", "hex_codec", "uu_codec", "quopri_codec", "rot_13"} <= set(names)
    for name in names:
        async with Server(ok(charset_payload(name), content_type=f"text/plain; charset={name}")) as server:
            net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
            outcome, _ = await execute(net.tool(["allowed.test"], max_bytes=1_000_000), {"url": "http://allowed.test/"})
            assert isinstance(outcome, Failed), (name, outcome.result.content[:120])
            assert "charset" in outcome.result.content, (name, outcome.result.content[:120])
            assert "AAAA" not in outcome.result.content
    for name, body in (("utf-8", "café".encode()), ("latin-1", "café".encode("latin-1")), ("utf-16", "café".encode("utf-16"))):
        async with Server(ok(body, content_type=f"text/plain; charset={name}")) as server:
            net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
            outcome, _ = await execute(net.tool(["allowed.test"]), {"url": "http://allowed.test/"})
            assert isinstance(outcome, Completed) and "café" in outcome.result.content, (name, outcome.result.content)


async def test_each_charset_check_refuses_a_codec_the_other_would_admit():
    """D1's two checks overlap on every standard-library codec, so each is pinned
    by a registered codec only it catches: one that yields bytes without
    declaring itself non-text, and one that declares itself non-text but yields
    str. Without these, either check could be deleted with the suite green."""
    import codecs

    class BytesOut(codecs.IncrementalDecoder):
        def decode(self, data, final=False):
            return bytes(data) * 1000

    class TextOut(codecs.IncrementalDecoder):
        def decode(self, data, final=False):
            return bytes(data).decode("latin-1") * 1000

    registered = {
        "m10_undeclared_bytes": codecs.CodecInfo(None, None, incrementaldecoder=BytesOut, name="m10_undeclared_bytes"),
        "m10_declared_non_text": codecs.CodecInfo(
            None, None, incrementaldecoder=TextOut, name="m10_declared_non_text", _is_text_encoding=False
        ),
    }

    def search(name):
        return registered.get(name)

    codecs.register(search)
    try:
        for name in registered:
            async with Server(ok(b"AAAA", content_type=f"text/plain; charset={name}")) as server:
                net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
                outcome, _ = await execute(net.tool(["allowed.test"]), {"url": "http://allowed.test/"})
                assert isinstance(outcome, Failed) and "charset" in outcome.result.content, (name, outcome.result.content[:120])
                assert "AAAAAAAA" not in outcome.result.content
    finally:
        codecs.unregister(search)


def test_a_non_text_charset_is_refused_with_assertions_stripped():
    """D1 under python -O, where the standard library's assert is gone: the case
    the reviewer reproduced, run as a separate interpreter because -O cannot be
    switched on inside this one."""
    probe = textwrap.dedent(
        """
        import asyncio, json, sys, tracemalloc
        sys.path.insert(0, sys.argv[1])
        import test_builtin_tools as t

        async def main():
            report = {"optimize": sys.flags.optimize, "results": {}}
            for name in t.non_text_codec_names():
                async with t.Server(t.ok(t.charset_payload(name), content_type="text/plain; charset=" + name)) as server:
                    net = t.Net(routes={(t.PUBLIC_V4, 80): ("127.0.0.1", server.port)})
                    tool = net.tool(["allowed.test"], max_bytes=1_000_000)
                    tracemalloc.start()
                    outcome, _ = await t.execute(tool, {"url": "http://allowed.test/"})
                    peak = tracemalloc.get_traced_memory()[1]
                    tracemalloc.stop()
                    content = outcome.result.content
                    report["results"][name] = [type(outcome).__name__, "AAAA" in content, "charset" in content, peak]
            print(json.dumps(report))

        asyncio.run(main())
        """
    )
    ran = subprocess.run(
        [sys.executable, "-O", "-c", probe, str(REPO / "tests")],
        cwd=REPO, env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )
    assert ran.returncode == 0, ran.stderr[-2000:]
    report = json.loads(ran.stdout.strip().splitlines()[-1])
    assert report["optimize"] == 1, "the probe did not run with assertions stripped"
    wrong = {
        name: result for name, result in report["results"].items()
        if result[0] != "Failed" or result[1] or not result[2] or result[3] > 16 * 2**20
    }
    assert report["results"] and not wrong, wrong


def certificate_authority(tmp_path, host):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "m10 test authority")])
    ca = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / f"{host}.pem", tmp_path / f"{host}.key"
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cadata=ca.public_bytes(serialization.Encoding.PEM).decode())
    return server_context, client_context


async def test_the_success_path_runs_over_tls_with_the_certificate_checked_against_the_host_name(tmp_path):
    good_server, good_client = certificate_authority(tmp_path, "allowed.test")
    other_server, _ = certificate_authority(tmp_path, "other.test")
    async with Server(ok(b"over tls"), ssl_context=good_server) as good, Server(ok(b"wrong name"), ssl_context=other_server) as wrong:
        net = Net(
            answers={"mismatch.test": [OTHER_V4]},
            routes={(PUBLIC_V4, 443): ("127.0.0.1", good.port), (OTHER_V4, 443): ("127.0.0.1", wrong.port)},
        )
        tool = net.tool(["allowed.test", "mismatch.test"], _ssl_context=good_client)
        outcome, _ = await execute(tool, {"url": "https://allowed.test/"})
        assert isinstance(outcome, Completed), outcome.result.content
        assert "over tls" in outcome.result.content
        assert header_of(good.heads[-1], "host") == "allowed.test"
        assert outcome.result.provenance.source_uri_or_hash == "https://allowed.test/"

        outcome, _ = await execute(tool, {"url": "https://mismatch.test/"})
        assert isinstance(outcome, Failed), "a certificate for another host name was accepted"
        assert "wrong name" not in outcome.result.content


async def test_the_test_seams_cannot_be_reached_through_tool_arguments(tmp_path):
    module = builtin()
    (tmp_path / "f.txt").write_text("x")
    net = Net()
    cases = [
        (module.read_file_tool(tmp_path), {"path": "f.txt", "_between_check_and_open": "x"}),
        (module.list_directory_tool(tmp_path), {"path": ".", "_between_check_and_open": "x"}),
        (module.glob_tool(tmp_path), {"pattern": "*", "_between_check_and_open": "x"}),
        (module.grep_tool(tmp_path), {"path": ".", "text": "x", "_between_check_and_open": "x"}),
        (net.tool(["allowed.test"]), {"url": "http://allowed.test/", "_resolve": "x"}),
        (net.tool(["allowed.test"]), {"url": "http://allowed.test/", "_connect": "x"}),
        (net.tool(["allowed.test"]), {"url": "http://allowed.test/", "_ssl_context": "x"}),
        (module.web_search_tool(FakeBackend()), {"query": "q", "backend": "x"}),
    ]
    for tool, arguments in cases:
        assert tool.spec.input_schema.get("additionalProperties") is False, tool.name
        assert not [name for name in inspect.signature(tool.fn).parameters if name.startswith("_")], tool.name
        outcome, _ = await execute(tool, arguments)
        assert isinstance(outcome, Failed) and outcome.error.__class__.__name__ == "ToolValidationError", tool.name
    assert net.resolved == [] and net.dialed == []


def test_the_allowlist_is_validated_normalised_and_covered_by_the_schema_hash():
    module = builtin()
    with pytest.raises((TypeError, ValueError)):
        module.fetch_tool("allowed.test")
    with pytest.raises((TypeError, ValueError)):
        module.fetch_tool(None)
    # ':80' and ':' included: httpx drops a default or empty port when parsing,
    # which let both through the first implementation.
    for bad in ("http://allowed.test", "allowed.test/path", "allowed.test:80", "allowed.test:443", "allowed.test:",
                "[::1]:80", "[::1", "user@allowed.test", "", " ", "*.", "*",
                "a*b.test", "*.*.test", "allowed.test\\x", "allowed test", 5, None):
        with pytest.raises((TypeError, ValueError)):
            module.fetch_tool([bad])
    same = module.fetch_tool(["Allowed.Test.", "*.Wild.test"]).spec.schema_hash()
    assert same == module.fetch_tool(["*.wild.test", "allowed.test"]).spec.schema_hash()
    assert same != module.fetch_tool(["allowed.test"]).spec.schema_hash()
    assert module.fetch_tool([]).spec.schema_hash() != same


async def test_no_fetch_input_raises_past_the_executor():
    net = Net()
    tool = net.tool(["allowed.test"])
    for url in ("", " ", "\x00", "http://", "http://[", "http://[::1", "http://allowed.test:abc/", "http://" + "a" * 2000,
                "💥://x", "http://allowed.test/%zz", "http://allowed.test/\x00", "http://allowed.test/\u2028",
                "http://allowed.test:0/", "http://.allowed.test/", "http://allowed..test/", "http://-allowed.test/",
                "http://" + "a." * 900 + "test/", "https://[fe80::1%25eth0]/"):
        outcome, _ = await execute(tool, {"url": url})
        assert outcome.result.content is not None


# =============================================================================
# FR-39, AC-31: web search
# =============================================================================


async def test_search_results_are_capped_in_number_and_size():
    module = builtin()
    many = [module.SearchResult("T" * 5000, "https://e.test/" + "u" * 5000, "S" * 50_000) for _ in range(50)]
    backend = FakeBackend(many)
    tool = module.web_search_tool(backend, max_results=4, max_result_chars=300)
    for arguments in ({"query": "q"}, {"query": "q", "max_results": 4}, {"query": "q", "max_results": 2}):
        outcome, _ = await execute(tool, arguments)
        assert isinstance(outcome, Completed), outcome.result.content[:200]
        numbered = re.findall(r"^\d+\. ", outcome.result.content, flags=re.M)
        assert len(numbered) == arguments.get("max_results", 4), outcome.result.content[:400]
        for letter in "TuS":
            assert letter * 301 not in outcome.result.content, f"a {letter} field exceeded 300 characters"
        assert backend.calls[-1][1] <= 4
    outcome, _ = await execute(tool, {"query": "q", "max_results": 5})
    assert isinstance(outcome, Failed), "a model asked for more results than the cap"


async def test_a_raising_or_malformed_backend_is_a_tool_error_with_external_labels():
    module = builtin()
    for backend in (FakeBackend(raises=RuntimeError("vendor down")), FakeBackend(results=None), FakeBackend(results=5)):
        outcome, _ = await execute(module.web_search_tool(backend), {"query": "q"})
        assert isinstance(outcome, Failed), outcome.result.content
        assert labels(outcome.result.provenance) == EXTERNAL
    with pytest.raises((TypeError, ValueError)):
        module.web_search_tool(None)


def test_no_module_imports_a_search_vendor_or_any_dependency_beyond_the_declared_ones():
    """AC-31 and NFR-13, read off the code: every import in the package is the
    standard library or a declared runtime dependency, and builtin_tools adds
    nothing beyond httpx."""
    allowed_everywhere = set(sys.stdlib_module_names) | {"httpx", "jsonschema", "psycopg", "psycopg_pool", "dotenv", "agentsdk"}
    offenders = {}
    for path in (REPO / "agentsdk").rglob("*.py"):
        imported = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        if path.name == "builtin_tools.py":
            limit = set(sys.stdlib_module_names) | {"httpx", "agentsdk"}
        elif path.name == "telemetry.py":
            # M14, P2-D12, NFR-19: the optional otel extra, imported by this module
            # alone and only when an exporter is built.
            limit = allowed_everywhere | {"opentelemetry"}
        else:
            limit = allowed_everywhere
        if imported - limit:
            offenders[path.name] = sorted(imported - limit)
    assert (REPO / "agentsdk" / "builtin_tools.py").is_file()
    assert not offenders, offenders


# =============================================================================
# AC-32 end to end, and FR-42
# =============================================================================


async def test_fetch_and_search_results_carry_exactly_fr40_labels_into_the_assembler():
    module = builtin()
    async with Server(ok(b"external page")) as server:
        net = Net(routes={(PUBLIC_V4, 80): ("127.0.0.1", server.port)})
        fetch = net.tool(["allowed.test"])
        search = module.web_search_tool(FakeBackend([module.SearchResult("t", "https://e.test/", "s")]))
        for tool in (fetch, search):
            assert tool.spec.result_provenance == declaration(**EXTERNAL)
        model = Scripted(
            calls(
                ToolCall(id="f1", name=fetch.name, arguments={"url": "http://allowed.test/page"}),
                ToolCall(id="s1", name=search.name, arguments={"query": "q"}),
            ),
            text("done"),
        )
        sessions = InMemorySessionStore()
        runner = Runner({"m": model}, tools=[fetch, search], session_store=sessions)
        spec = AgentSpec(id="a", instructions="x", tool_profile=(fetch.name, search.name))
        result = await runner.run(spec, "go", RunConfig(tenant_id="t", project_id="p"))
        assert result.status is RunStatus.COMPLETED, result.error

    results = {r.tool_call_id: r for m in sessions.history(result.run_id) for r in m.tool_results}
    assert not results["f1"].is_error and not results["s1"].is_error, results
    for call_id in ("f1", "s1"):
        assert labels(results[call_id].provenance) == EXTERNAL
    assert results["f1"].provenance.source_uri_or_hash == "http://allowed.test/page"

    metadata = {entry["tool_call_id"]: entry for entry in model.requests[1].metadata["provenance"]}
    for call_id in ("f1", "s1"):
        assert metadata[call_id]["origin"] == "external_tool"
        assert metadata[call_id]["trust_zone"] == "untrusted"
        assert metadata[call_id]["instruction_authority"] == "data_only"
        assert metadata[call_id]["taint_flags"] == ["external_content", "prompt_injection_risk"]


def test_example_10_runs_offline_and_is_listed():
    """FR-42 and D9. AC-17 runs every example too; this keeps the unit gate self-contained."""
    script = REPO / "scripts" / "10_builtin_tools.py"
    assert script.is_file()
    for doc in (REPO / "README.md", REPO / "scripts" / "README.md"):
        assert "10_builtin_tools.py" in doc.read_text(encoding="utf-8"), doc
    env = {k: v for k, v in os.environ.items() if k not in ("BASE_URL", "MODEL_API_KEY", "DATABASE_URL", "DEFAULT_MODEL")}
    env.update(PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    ran = subprocess.run(
        [sys.executable, str(script), "--offline"], cwd=REPO.parent, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=240,
    )
    assert ran.returncode == 0, (ran.stdout + ran.stderr)[-2500:]
    assert "[FAIL]" not in ran.stdout and "[PASS]" in ran.stdout, ran.stdout[-2000:]
