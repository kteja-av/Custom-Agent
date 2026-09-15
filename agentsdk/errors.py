"""Stable exception hierarchy (FR-13, master design 4.11).

Three families under one root, so callers can catch at whatever altitude they
mean: `AgentSDKError` for anything this SDK raised, `ToolError` for anything a
tool call did, or one concrete class.

Names are prefixed (`ModelTimeout`, `ToolTimeout`) because the design lists a
`Timeout` under both `ModelError` and `ToolError`; the prefix keeps the two
distinguishable at an import site without nesting classes.
"""

from __future__ import annotations


class AgentSDKError(Exception):
    """Root of every error this SDK raises deliberately."""


def describe_exception(exc: BaseException) -> str:
    """Render any exception for an error message, unconditionally.

    Shared by every total boundary in the SDK. `str(exc)` runs inside an except
    block at each of those call sites, and a hostile `__str__` that raises (or
    returns a non-string) would escape the very mechanism built to contain it.
    A boundary whose error path can raise is not a boundary.
    """
    try:
        try:
            detail = str(exc)
            if not isinstance(detail, str):
                detail = ""
        except Exception:  # noqa: BLE001
            detail = "<unrenderable>"
        # type(exc).__name__ can raise too (a metaclass may define it as a
        # property); the outer guard below covers that without a dead inner one.
        name = type(exc).__name__
        return f"{name}: {detail}" if detail else name
    except Exception:  # noqa: BLE001 - the last line of defence
        return "unrenderable exception"


# --- Model layer ------------------------------------------------------------


class ModelError(AgentSDKError):
    """A model provider failed to produce a usable response."""


class ModelTimeout(ModelError):
    """Transient. Retried with backoff (FR-15)."""


class ModelRateLimited(ModelError):
    """Transient. Retried with backoff (FR-15)."""


class ModelProviderUnavailable(ModelError):
    """Not retried -- no side effects have occurred, but retrying will not help."""


class InvalidStructuredOutput(ModelError):
    """Raised once Phase 2 populates ModelRequest.output_schema. Unused in Phase 0."""


# --- Tool layer -------------------------------------------------------------


class ToolError(AgentSDKError):
    """A tool call failed. Surfaced to the model as an error tool result rather
    than failing the run (LLD 4.2, 4.3)."""


class ToolNotFound(ToolError):
    """Resolve step: no such tool in the registry."""


class ToolValidationError(ToolError):
    """Validate step: arguments do not satisfy the tool's input schema.

    Reaching this means the tool implementation was never invoked.
    """


class ToolPermissionDenied(ToolError):
    """Permission step: the checker returned DENY."""


class ToolApprovalRequired(ToolError):
    """Defined for Phase 4. Phase 0 stubs approval to auto-allow."""


class ToolTimeout(ToolError):
    """Execute step: the tool implementation exceeded its deadline."""


class ToolExecutionError(ToolError):
    """Execute step: the tool implementation raised."""


class ToolCancelled(ToolError):
    """The run was cancelled before this tool call finished (FR-50).

    Every tool call of a response the run had begun to execute is paired with a
    result, so the conversation stays well formed. A call that had reached step 6
    carries its tool's declared provenance, as every error after a tool ran does
    (DECISION-ea6e1daf); one that had not carries the executor's own.
    """


# --- Workflow layer ---------------------------------------------------------


class WorkflowError(AgentSDKError):
    """The run itself cannot continue."""


class BudgetExceeded(WorkflowError):
    """Defined for Phase 2 (ADR-06)."""


class MaxTurnsExceeded(WorkflowError):
    """A defined terminal state, not an exception that escapes to the caller.

    `Runner` converts this into RunStatus.MAX_TURNS_EXCEEDED (FR-14, LLD 4.4).
    """


class MaxDepthExceeded(WorkflowError):
    """Defined for Phase 2 (subagent recursion)."""


class DependencyFailed(WorkflowError):
    """Defined for Phase 2 (DAG node dependencies)."""


class ReplanLimitExceeded(WorkflowError):
    """Defined for Phase 2 (ADR-03 replanning policy)."""


class Cancelled(WorkflowError):
    """Defined for Phase 2 (RunHandle.cancel)."""
