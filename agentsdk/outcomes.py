"""Future-seam types (FR-16).

These exist now so later phases add implementations rather than replace central
contracts. Phase 0 constructs only `Completed` and `Failed`; the rest are types
with no producer yet, and `reachable_in_phase0` says so explicitly rather than
leaving it to a comment nobody greps.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .errors import ToolError
from .primitives import ToolResult


class InterruptionKind(str, Enum):
    TOOL_APPROVAL = "tool_approval"
    ADDITIONAL_USER_INPUT = "additional_user_input"
    EXTERNAL_TOOL_INPUT = "external_tool_input"
    CREDENTIAL_REQUIRED = "credential_required"
    EXTERNAL_TASK_WAIT = "external_task_wait"


@dataclass(frozen=True)
class RunInterruption:
    """One generic pause/resume abstraction (ADR-29).

    Approval is one *kind* of interruption, not its own mechanism. Phase 0
    defines the type and never persists or raises one; Phase 4 handles it in
    process; Phase 6 makes it durable across restarts.
    """

    kind: InterruptionKind
    run_id: str
    request_payload: dict[str, Any] = field(default_factory=dict)
    interruption_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None
    tool_call_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resume_token: str | None = None


@dataclass(frozen=True)
class ExternalTaskRef:
    """A long-running external task. Phase 4+."""

    task_ref: str
    poll_after_seconds: int | None = None


class ToolExecutionOutcome:
    """Base of the outcome union returned by ToolExecutor (FR-5, FR-16)."""

    reachable_in_phase0: bool = False


@dataclass(frozen=True)
class Completed(ToolExecutionOutcome):
    result: ToolResult
    reachable_in_phase0 = True


@dataclass(frozen=True)
class Failed(ToolExecutionOutcome):
    error: ToolError
    result: ToolResult
    reachable_in_phase0 = True


@dataclass(frozen=True)
class InputRequired(ToolExecutionOutcome):
    interruption: RunInterruption


@dataclass(frozen=True)
class ApprovalRequired(ToolExecutionOutcome):
    interruption: RunInterruption


@dataclass(frozen=True)
class Pending(ToolExecutionOutcome):
    external_task: ExternalTaskRef


@dataclass(frozen=True)
class Cancelled(ToolExecutionOutcome):
    reason: str | None = None
