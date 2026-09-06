"""Custom Agent SDK - Phase 0 (single-agent skeleton).

Application code should import from this package root only. Internal
collaborators (AgentLoop, ToolExecutor, ...) are deliberately not re-exported:
the public surface is AgentSpec / RunConfig / Runner / RunResult (NFR-5).
"""

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

__version__ = "0.1.0.dev0"

__all__ = [
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
