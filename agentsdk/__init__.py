"""Custom Agent SDK - Phase 0 (single-agent skeleton).

Application code should import from this package root only. Internal
collaborators (AgentLoop, ToolExecutor, ...) are deliberately not re-exported:
the public surface is AgentSpec / RunConfig / Runner / RunResult (NFR-5).
"""

from .api import AgentSpec, RunConfig, Runner, RunResult, RunStatus
from .artifacts import ArtifactRef, ArtifactStore, InMemoryArtifactStore
from .handle import RunHandle, RunState
from .model import ReasoningEffort
from .persistence import Persistence
from .scheduler import SchedulerLimits
from .errors import (
    AgentSDKError,
    ArtifactIntegrityError,
    ArtifactNotFound,
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
    ToolCancelled,
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
    # What Runner.start returns, and its snapshot (FR-48, FR-49): driving a run
    # that way needs them, so they belong beside Runner.
    "RunHandle",
    "RunState",
    # A field of AgentSpec and RunConfig (FR-28), so it belongs beside them.
    "ReasoningEffort",
    # An argument of Runner and a field of RunConfig (FR-43), for the same reason.
    "SchedulerLimits",
    # Artifacts (FR-53, FR-54): the in-memory store is built directly; the
    # Postgres one comes from Persistence.artifact_store.
    "ArtifactRef",
    "ArtifactStore",
    "InMemoryArtifactStore",
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
    "ToolCancelled",
    "ArtifactNotFound",
    "ArtifactIntegrityError",
    "WorkflowError",
    "BudgetExceeded",
    "MaxTurnsExceeded",
    "MaxDepthExceeded",
    "DependencyFailed",
    "ReplanLimitExceeded",
    "Cancelled",
    "__version__",
]
