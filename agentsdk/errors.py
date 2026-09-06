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
