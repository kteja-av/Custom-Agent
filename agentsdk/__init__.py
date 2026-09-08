"""Custom Agent SDK - Phase 0 (single-agent skeleton).

Application code should import from this package root only. Internal
collaborators (AgentLoop, ToolExecutor, ...) are deliberately not re-exported:
the public surface is AgentSpec / RunConfig / Runner / RunResult (NFR-5).
"""

from .api import AgentSpec, RunConfig, Runner, RunResult, RunStatus
from .persistence import Persistence
from .errors import (
    AgentSDKError,
    BudgetExceeded,
    Cancelled,
    DependencyFailed,
    InvalidStructuredOutput,
    MaxDepthExceeded,
    MaxTurnsExceeded,
    ModelError,
    ModelProviderUnavailable,
    ModelRateLimited,
    ModelTimeout,
    ReplanLimitExceeded,
    ToolApprovalRequired,
    ToolError,
    ToolExecutionError,
    ToolNotFound,
    ToolPermissionDenied,
    ToolTimeout,
    ToolValidationError,
    WorkflowError,
)
from .primitives import (
    ContentProvenance,
    InstructionAuthority,
    Message,
    Origin,
    Role,
    TaintFlag,
    ToolCall,
    ToolResult,
    TrustZone,
)

from .version import __version__

__all__ = [
    # The public API surface (NFR-5). Everything below it is exported for
    # authoring tools and specs, not for driving a run.
    "AgentSpec",
    "RunConfig",
    "Runner",
    "RunResult",
    # Opting into persistence is part of driving a run, so it belongs on the
    # public surface. Requiring `import agentsdk.persistence` for it made
    # NFR-5's "application code calls Runner.run() and nothing else" false in
    # the one place every real caller has to go.
    "Persistence",
    "RunStatus",
    "ContentProvenance",
    "InstructionAuthority",
    "Message",
    "Origin",
    "Role",
    "TaintFlag",
    "ToolCall",
    "ToolResult",
    "TrustZone",
    "AgentSDKError",
    "ModelError",
    "ModelTimeout",
    "ModelRateLimited",
    "ModelProviderUnavailable",
    "InvalidStructuredOutput",
    "ToolError",
    "ToolNotFound",
    "ToolValidationError",
    "ToolPermissionDenied",
    "ToolApprovalRequired",
    "ToolTimeout",
    "ToolExecutionError",
    "WorkflowError",
    "BudgetExceeded",
    "MaxTurnsExceeded",
    "MaxDepthExceeded",
    "DependencyFailed",
    "ReplanLimitExceeded",
    "Cancelled",
    "__version__",
]
