"""The tool-call lifecycle (FR-5, FR-44, LLD 3.5).

Nine steps, fixed order:

    1 resolve  2 validate  3 permission  4 approval (stub)  5 before_tool
    6 execute  7 assign provenance  8 after_tool  9 emit ToolCalled

INVARIANT: a call that fails validation never reaches the permission check, and
a denied call never reaches execution. That ordering is the difference between
"we checked" and "we checked in time", so it is asserted in the tests rather
than trusted to code reading.

Since M11 the steps are also reachable as two halves: `prepare` runs 1 to 5 and
`run_prepared` runs 6 to 9, because a parallel batch prepares every one of its
calls before any of them executes (FR-44). `execute` is the two halves in a row,
which is the lifecycle it always was. Each half is a total boundary of its own.

A failed tool call is a normal turn outcome, not a run failure: every failure
below still produces a ToolResult with is_error=True so the model sees it and
can react on its next turn (LLD 4.2, 4.3).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jsonschema

from .errors import (
    ToolCancelled,
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolPermissionDenied,
    ToolTimeout,
    ToolValidationError,
    describe_exception,
)
from .hooks import HookAction, RuntimeHook
from .identity import PrincipalContext
from .outcomes import Completed, Failed, ToolExecutionOutcome
from .permissions import PermissionChecker
from .primitives import (
    ContentProvenance,
    InstructionAuthority,
    Origin,
    TaintFlag,
    ToolCall,
    ToolResult,
    TrustZone,
    unstorable_reason,
)
from .tools import DEFAULT_MAX_OUTPUT_CHARS, Tool, ToolOutput, ToolRegistry


@dataclass
class _CallState:
    """How far one call got, for the paths that must know.

    `ran` is set the moment the implementation is invoked. A failure after that
    point -- the tool raised, timed out, returned something unstorable, or a hook
    broke afterwards -- may carry what the tool read in its error text, so the
    error keeps the tool's declared provenance (FR-40). Before it, the executor
    wrote every byte of the error itself. The last-resort handlers read this
    too, which is why it is state rather than an argument.
    """

    tool: Tool | None = None
    ran: bool = False


@dataclass
class PreparedCall:
    """A call that has passed steps 1 to 5 and waits for step 6 (FR-44).

    `issued` is the call the model issued, which a last-resort failure answers,
    as it always did; `tool_call` is the call that runs, which a before_tool hook
    may have replaced; `tool` is the tool step 1 resolved, whose name selects
    the call's slots.
    """

    issued: ToolCall
    tool_call: ToolCall
    tool: Tool
    state: _CallState


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        permission_checker: PermissionChecker,
        hook: RuntimeHook | None = None,
        emit: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self._registry = registry
        self._permissions = permission_checker
        self._hook = hook or RuntimeHook()
        # Event emission is injected: ToolExecutor must not know what a
        # RunEvent store is. Wired up for real in M5.
        self._emit = emit or (lambda event_type, payload: None)

    async def execute(
        self,
        tool_call: ToolCall,
        principal_context: PrincipalContext | None = None,
    ) -> ToolExecutionOutcome:
        """Total boundary: a tool call always yields an outcome, never a raise.

        The steps below guard the failures they can name, but naming them all is
        the approach this project already abandoned once (see the ModelClient
        boundary decision). Anything unforeseen -- a hook that raises, a
        malformed input_schema, a registry that misbehaves -- becomes
        Failed(ToolExecutionError) so the loop keeps its own promise of a
        terminal status. BaseException passes through as control flow.
        """
        prepared = await self.prepare(tool_call, principal_context)
        if not isinstance(prepared, PreparedCall):
            return prepared
        return await self.run_prepared(prepared)

    async def prepare(
        self,
        tool_call: ToolCall,
        principal_context: PrincipalContext | None = None,
    ) -> PreparedCall | ToolExecutionOutcome:
        """Steps 1 to 5: the call, ready for step 6, or the outcome of a call that
        stopped before it. Total, as execute is."""
        state = _CallState()
        try:
            return await self._prepare(tool_call, principal_context, state)
        except Exception as exc:  # noqa: BLE001
            return await self._failed(
                tool_call, ToolExecutionError(describe_exception(exc)), state
            )

    async def run_prepared(
        self,
        prepared: PreparedCall,
        slot: Callable[[str], Any] | None = None,
    ) -> ToolExecutionOutcome:
        """Steps 6 to 9, holding the call's slots (FR-44). Total, as execute is.

        `slot(tool_name)` gives an async context manager entered immediately
        before step 6 and left after step 9. Waiting for it is no part of the
        tool's timeout, and until it is granted the tool has not run, so an error
        before then is still wholly the executor's own (FR-40).
        """
        try:
            async with slot(prepared.tool.name) if slot is not None else contextlib.nullcontext():
                try:
                    return await self._run(prepared)
                except Exception as exc:  # noqa: BLE001
                    return await self._failed(
                        prepared.issued, ToolExecutionError(describe_exception(exc)), prepared.state
                    )
        except Exception as exc:  # noqa: BLE001 - a slot that could not be taken or left
            return await self._failed(
                prepared.issued, ToolExecutionError(describe_exception(exc)), prepared.state
            )

    async def cancelled(self, item: PreparedCall | ToolCall) -> Failed:
        """The result of a call its run's cancellation left unfinished (FR-50).

        Every call of a response the run had begun to execute is paired with a
        result, and its ToolCalled event is emitted, so the recorded conversation
        stays well formed. A call that reached step 6 carries its tool's declared
        provenance, as every error after a tool ran does (DECISION-ea6e1daf); one
        that did not -- still waiting for a slot, or never prepared -- carries the
        executor's own. Total, like _failed, which it is.
        """
        if isinstance(item, PreparedCall):
            return await self._failed(
                item.issued,
                ToolCancelled("the run was cancelled before this tool call finished"),
                item.state,
            )
        return await self._failed(
            item, ToolCancelled("the run was cancelled before this tool call started"), _CallState()
        )

    async def _prepare(
        self,
        tool_call: ToolCall,
        principal_context: PrincipalContext | None,
        state: _CallState,
    ) -> PreparedCall | ToolExecutionOutcome:
        issued = tool_call

        # --- 1. resolve -----------------------------------------------------
        try:
            tool = self._registry.get(tool_call.name)
        except ToolNotFound as exc:
            return await self._failed(tool_call, exc, state)
        state.tool = tool

        # --- 2. validate arguments ------------------------------------------
        # Must precede the permission check: an unparseable call is rejected on
        # its shape, before any policy question is even asked.
        #
        # Undecodable arguments fail here explicitly rather than arriving as {}
        # and being waved through by any schema without required properties.
        if tool_call.arguments_error is not None:
            return await self._failed(
                tool_call,
                ToolValidationError(
                    f"could not decode tool arguments: {tool_call.arguments_error}"
                ),
                state,
                tool_reached=False,
            )
        try:
            jsonschema.validate(tool_call.arguments, tool.spec.input_schema)
        except jsonschema.ValidationError as exc:
            return await self._failed(
                tool_call, ToolValidationError(exc.message), state, tool_reached=False
            )
        except jsonschema.SchemaError as exc:
            # The TOOL's schema is invalid, not the model's arguments -- a typo
            # in a ToolSpec, which is a developer error rather than a model one.
            # It still must not crash the run: the model sees an error result
            # and can try something else, and the operator sees the reason.
            return await self._failed(
                tool_call,
                ToolValidationError(
                    f"tool {tool_call.name!r} has an invalid input_schema: {exc.message}"
                ),
                state,
                tool_reached=False,
            )

        # --- 3. permission check --------------------------------------------
        result = self._permissions.check(tool_call, principal_context)
        if not result.allowed:
            return await self._failed(
                tool_call, ToolPermissionDenied(result.reason), state, tool_reached=False
            )

        # --- 4. approval ------------------------------------------------------
        # Stubbed to auto-allow. The call site exists so Phase 4's
        # ApprovalManager slots in without reshaping this lifecycle.

        # --- 5. before_tool hook ----------------------------------------------
        outcome = self._hook.before_tool(tool_call)
        if outcome.action is HookAction.REJECT:
            return await self._failed(
                tool_call, ToolPermissionDenied(outcome.reason or "rejected by hook"), state
            )
        if outcome.action is HookAction.MODIFY and outcome.replacement is not None:
            tool_call = outcome.replacement

        return PreparedCall(issued=issued, tool_call=tool_call, tool=tool, state=state)

    async def _run(self, prepared: PreparedCall) -> ToolExecutionOutcome:
        tool_call, tool, state = prepared.tool_call, prepared.tool, prepared.state

        # --- 6. execute --------------------------------------------------------
        state.ran = True
        try:
            value = await self._invoke(tool, tool_call.arguments)
        except asyncio.TimeoutError:
            return await self._failed(
                tool_call, ToolTimeout(f"tool {tool_call.name!r} exceeded its timeout"), state
            )
        except Exception as exc:  # noqa: BLE001 - any tool failure is a tool error
            return await self._failed(tool_call, ToolExecutionError(str(exc)), state)

        # --- 7. assign provenance ----------------------------------------------
        source = None
        if isinstance(value, ToolOutput):
            source, value = value.source_uri, value.content
        if isinstance(source, str):
            # The text it holds, read with str's own methods: a subclass decides
            # its own truthiness and length (FR-47, KNOWLEDGE-739aca22).
            source = _exact(source)
        content = value if isinstance(value, str) else repr(value)
        # A tool's own output reaches the same JSONB column the model's does, so
        # it can diverge the same way: a tool returning a NUL completed in
        # memory and failed the run against Postgres. It becomes an ordinary
        # tool error instead -- the mechanism this executor already has for
        # "the tool produced something unusable" -- so the run continues and
        # the model is told, identically on both backends.
        unstorable = unstorable_reason(content)
        if unstorable is not None:
            return await self._failed(
                tool_call,
                ToolExecutionError(
                    f"tool {tool_call.name!r} returned a result that cannot be stored: "
                    f"{unstorable}"
                ),
                state,
            )
        # The tool's declared labels (FR-40); undeclared, internal_tool as before.
        provenance = tool.spec.result_provenance.for_source(
            source if isinstance(source, str) and source else tool.spec.schema_hash()
        )
        tool_result = ToolResult(
            tool_call_id=tool_call.id,
            content=content,
            provenance=provenance,
        )

        # --- 8. after_tool hook -------------------------------------------------
        after = self._hook.after_tool(tool_result)
        modified = after.action is HookAction.MODIFY and after.replacement is not None
        returned = after.replacement if modified else tool_result
        # What the hook leaves to be returned is shaped by caller code -- a
        # replacement, or the result the hook was handed, which it can change in
        # place -- so it is checked as if it were built here. Round 1 (C4) found
        # anything that was not a ToolResult passing uncapped; round 2 found a
        # ToolResult passing with values set past its constructor
        # (object.__setattr__, or a subclass without __post_init__): a NUL that
        # failed the run on Postgres, and an id for another call. Only a
        # ToolResult answering this call is accepted, and it is rebuilt through
        # the constructors, which apply the storability rule step 7 applies.
        if not isinstance(returned, ToolResult):
            return await self._failed(
                tool_call,
                ToolExecutionError(
                    "the after_tool hook replaced the result with something that is not a ToolResult"
                ),
                state,
            )
        # Each field is read exactly once, into a local that is then checked and
        # is the only thing used. Round 3 checked is_error on one read and built
        # the result from a second, and a result built past its constructor can
        # answer every read differently (rejected, KNOWLEDGE-96063b68).
        answers = returned.tool_call_id
        content = returned.content
        provenance = _checked_provenance(returned.provenance)
        is_error = returned.is_error
        if type(answers) is not str or not str.__eq__(answers, tool_call.id) or type(is_error) is not bool:
            return await self._failed(
                tool_call,
                ToolExecutionError(
                    "the after_tool hook returned a result for another call, or one whose is_error is not a bool"
                ),
                state,
            )
        if isinstance(provenance, str):  # the reason it cannot be provenance
            return await self._failed(tool_call, ToolExecutionError(provenance), state)
        tool_result = ToolResult(
            tool_call_id=tool_call.id,
            content=_text(content),
            provenance=provenance,
            is_error=is_error,
        )

        # The cap applies to what is returned, so it comes after the hook: a
        # substitution is capped too, and a redacting hook sees the whole text
        # before any of it is cut (KNOWLEDGE-294f2901).
        content, original_length, truncated = _cap(tool_result.content, tool.spec.max_output_chars)
        if truncated:
            tool_result = dataclasses.replace(tool_result, content=content)

        # --- 9. emit ------------------------------------------------------------
        await self._safe_emit(
            {
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                # The state of what is returned: a replacement may be an error,
                # or be made one by its constructor (M10 review round 2, caveat 3).
                "is_error": tool_result.is_error,
                "original_length": original_length,
                "truncated": truncated,
            }
        )
        return Completed(result=tool_result)

    async def _safe_emit(self, payload: dict[str, Any]) -> None:
        """Telemetry must never be able to fail the thing it observes.

        `emit` is caller-supplied, and it is called from inside the failure path
        below -- an exception there would escape the total boundary through the
        one route the boundary cannot catch.

        It may return an awaitable, and the Runner's does. Persisting ToolCalled
        is a store write, and before M7 round 1 this was the one store write
        still made ON the event loop after every other had been moved off it:
        the executor called its sink synchronously, so the Runner could not
        offload it. The executor cannot know whether its sink does I/O, so it
        awaits whatever it is handed -- the rule _invoke already applies to
        tools, for the same reason.
        """
        try:
            outcome = self._emit("ToolCalled", payload)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:  # noqa: BLE001
            pass

    async def _invoke(self, tool: Any, arguments: dict[str, Any]) -> Any:
        async def _run() -> Any:
            # Await whatever is awaitable rather than inspecting the callable:
            # iscoroutinefunction() is False for a wrapper or a class with an
            # async __call__, which would silently return an un-awaited
            # coroutine as if it were the tool's result.
            value = tool.fn(**arguments)
            if inspect.isawaitable(value):
                value = await value
            return value

        # Sync tools run inline. Phase 5's sandbox is what moves genuinely
        # blocking work off the loop; a thread pool here would only hide it.
        if tool.spec.timeout_seconds is None:
            return await _run()
        return await asyncio.wait_for(_run(), timeout=tool.spec.timeout_seconds)

    async def _failed(
        self,
        tool_call: ToolCall,
        error: ToolError,
        state: _CallState,
        tool_reached: bool = True,
    ) -> Failed:
        """Every failure still yields a ToolResult, so the model sees the error.

        Reached from the last-resort handlers, so it must not raise: what it
        reads off the tool is read under a guard.
        """
        provenance = ContentProvenance.executor_error(type(error).__name__)
        cap = DEFAULT_MAX_OUTPUT_CHARS
        try:
            if state.tool is not None:
                cap = state.tool.spec.max_output_chars
                if state.ran:
                    # The tool ran, so this text may carry what it read (M8
                    # review caveat 3): the declared labels apply. The bytes are
                    # still the executor's rendering, so the source stays its
                    # error URN.
                    provenance = state.tool.spec.result_provenance.for_source(
                        provenance.source_uri_or_hash
                    )
        except Exception:  # noqa: BLE001 - a registry may hand back anything
            provenance = ContentProvenance.executor_error(type(error).__name__)
            cap = DEFAULT_MAX_OUTPUT_CHARS
        content, original_length, truncated = _cap(describe_exception(error), cap)
        result = ToolResult(
            tool_call_id=tool_call.id,
            content=content,
            provenance=provenance,
            is_error=True,
        )
        await self._safe_emit(
            {
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                "is_error": True,
                "error_type": type(error).__name__,
                "original_length": original_length,
                "truncated": truncated,
            }
        )
        return Failed(error=error, result=result)


# A source is a URI or a hash. Bounded, so a result cannot carry megabytes past
# the output cap in a field the cap does not measure (M10 review round 3).
_MAX_SOURCE_CHARS = 8192


def _is_member(value: Any, labels: Any) -> bool:
    """Identity with one of the enum's real members.

    An exact-type check is not membership: the labels are str-mixin enums, and
    str.__new__(Origin, "x") builds an instance of exactly Origin that is none of
    its members, with any _value_ or none, while every store reads .value (M10
    review round 4, rejected). Equality is only string equality, so a forged
    "system" valued "user" would pass it.
    """
    return any(value is member for member in labels)


def _checked_provenance(value: Any) -> ContentProvenance | str:
    """A fresh ContentProvenance built from `value`, or the reason it cannot be one.

    Each field is read once and checked before it is used. ContentProvenance does
    not check its own labels, and every store reads `.value` from them, so a
    plain-string origin completed in memory and failed on Postgres. The labels
    must be the enums' own members; the taint set an exact frozenset (a subclass
    can answer iteration differently from what it holds); the source bounded text.

    A source that is a str subclass is copied into the exact text it holds and
    bounded on the copy (FR-47): refusing it was a regression M10 introduced for
    ordinary caller code (KNOWLEDGE-739aca22), and a subclass reporting a false
    length is measured by what it holds.
    """
    if not isinstance(value, ContentProvenance):
        return "the result carries provenance that is not a ContentProvenance"
    origin = value.origin
    authority = value.instruction_authority
    zone = value.trust_zone
    taint = value.taint_flags
    source = value.source_uri_or_hash
    if not (_is_member(origin, Origin) and _is_member(authority, InstructionAuthority) and _is_member(zone, TrustZone)):
        return "the result carries provenance whose labels are not members of their enums"
    if type(taint) is not frozenset or not all(_is_member(flag, TaintFlag) for flag in taint):
        return "the result carries taint flags that are not members of TaintFlag"
    if source is not None:
        source = _exact(source) if isinstance(source, str) else None
        if source is None or len(source) > _MAX_SOURCE_CHARS:
            return f"the result carries a source that is not text of at most {_MAX_SOURCE_CHARS} characters"
    return ContentProvenance(
        origin=origin, instruction_authority=authority, trust_zone=zone, taint_flags=taint, source_uri_or_hash=source
    )


def _exact(text: str) -> str:
    """The characters `text` holds, as an exact str.

    A str subclass can report any length and return anything from a slice: one
    reporting 3 put 200000 characters through a 40-character cap (M10 review
    round 2, caveat 1). A full slice taken with str.__getitem__ copies the
    characters actually held into a plain str.
    """
    return text if type(text) is str else str.__getitem__(text, slice(None))


def _text(value: Any) -> str:
    """A result's content as exact text, read with str's own methods (see _exact)."""
    return _exact(value if isinstance(value, str) else repr(value))


def _cap(content: str, limit: Any) -> tuple[str, int, bool]:
    """FR-41: cut `content` to `limit` characters and say so where the model reads.

    Characters are code points, so the cut can never fall inside one: a non-BMP
    character is one code point in a str, and cutting on UTF-16 units instead
    would leave half a surrogate pair that no store can hold.
    """
    original = len(content)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = DEFAULT_MAX_OUTPUT_CHARS
    if original <= limit:
        return content, original, False
    marker = f"\n[truncated by the SDK: the result was {original} characters; the first {limit} are shown]"
    return content[:limit] + marker, original, True
