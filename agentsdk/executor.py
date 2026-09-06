"""The tool-call lifecycle (FR-5, LLD 3.5).

Nine steps, fixed order:

    1 resolve  2 validate  3 permission  4 approval (stub)  5 before_tool
    6 execute  7 assign provenance  8 after_tool  9 emit ToolCalled

INVARIANT: a call that fails validation never reaches the permission check, and
a denied call never reaches execution. That ordering is the difference between
"we checked" and "we checked in time", so it is asserted in the tests rather
than trusted to code reading.

A failed tool call is a normal turn outcome, not a run failure: every failure
below still produces a ToolResult with is_error=True so the model sees it and
can react on its next turn (LLD 4.2, 4.3).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import jsonschema

from .errors import (
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolPermissionDenied,
    ToolTimeout,
    ToolValidationError,
)
from .hooks import HookAction, RuntimeHook
from .identity import PrincipalContext
from .outcomes import Completed, Failed, ToolExecutionOutcome
from .permissions import PermissionChecker
from .primitives import ContentProvenance, ToolCall, ToolResult
from .tools import ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        permission_checker: PermissionChecker,
        hook: RuntimeHook | None = None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
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
        # --- 1. resolve -----------------------------------------------------
        try:
            tool = self._registry.get(tool_call.name)
        except ToolNotFound as exc:
            return self._failed(tool_call, exc)

        # --- 2. validate arguments ------------------------------------------
        # Must precede the permission check: an unparseable call is rejected on
        # its shape, before any policy question is even asked.
        #
        # Undecodable arguments fail here explicitly rather than arriving as {}
        # and being waved through by any schema without required properties.
        if tool_call.arguments_error is not None:
            return self._failed(
                tool_call,
                ToolValidationError(
                    f"could not decode tool arguments: {tool_call.arguments_error}"
                ),
                tool_reached=False,
            )
        try:
            jsonschema.validate(tool_call.arguments, tool.spec.input_schema)
        except jsonschema.ValidationError as exc:
            return self._failed(
                tool_call, ToolValidationError(exc.message), tool_reached=False
            )

        # --- 3. permission check --------------------------------------------
        result = self._permissions.check(tool_call, principal_context)
        if not result.allowed:
            return self._failed(
                tool_call, ToolPermissionDenied(result.reason), tool_reached=False
            )

        # --- 4. approval ------------------------------------------------------
        # Stubbed to auto-allow. The call site exists so Phase 4's
        # ApprovalManager slots in without reshaping this lifecycle.

        # --- 5. before_tool hook ----------------------------------------------
        outcome = self._hook.before_tool(tool_call)
        if outcome.action is HookAction.REJECT:
            return self._failed(
                tool_call, ToolPermissionDenied(outcome.reason or "rejected by hook")
            )
        if outcome.action is HookAction.MODIFY and outcome.replacement is not None:
            tool_call = outcome.replacement

        # --- 6. execute --------------------------------------------------------
        try:
            value = await self._invoke(tool, tool_call.arguments)
        except asyncio.TimeoutError:
            return self._failed(
                tool_call, ToolTimeout(f"tool {tool_call.name!r} exceeded its timeout")
            )
        except Exception as exc:  # noqa: BLE001 - any tool failure is a tool error
            return self._failed(tool_call, ToolExecutionError(str(exc)))

        # --- 7. assign provenance ----------------------------------------------
        provenance = ContentProvenance.internal_tool(
            source_uri_or_hash=tool.spec.schema_hash()
        )
        tool_result = ToolResult(
            tool_call_id=tool_call.id,
            content=value if isinstance(value, str) else repr(value),
            provenance=provenance,
        )

        # --- 8. after_tool hook -------------------------------------------------
        after = self._hook.after_tool(tool_result)
        if after.action is HookAction.MODIFY and after.replacement is not None:
            tool_result = after.replacement

        # --- 9. emit ------------------------------------------------------------
        self._emit(
            "ToolCalled",
            {"tool_call_id": tool_call.id, "name": tool_call.name, "is_error": False},
        )
        return Completed(result=tool_result)

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

    def _failed(
        self, tool_call: ToolCall, error: ToolError, tool_reached: bool = True
    ) -> Failed:
        """Every failure still yields a ToolResult, so the model sees the error."""
        result = ToolResult(
            tool_call_id=tool_call.id,
            content=f"{type(error).__name__}: {error}",
            provenance=ContentProvenance.internal_tool(),
            is_error=True,
        )
        self._emit(
            "ToolCalled",
            {
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                "is_error": True,
                "error_type": type(error).__name__,
            },
        )
        return Failed(error=error, result=result)
