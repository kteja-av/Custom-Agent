"""Runtime intervention points (FR-8, LLD 3.7).

`RunEvent` records what happened; `RuntimeHook` is what may influence what
happens. Keeping observation and intervention apart is the whole point.

Phase 0 default: every hook point returns CONTINUE. The interface is wired into
AgentLoop and ToolExecutor now so a real hook later needs no call-site change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class HookAction(str, Enum):
    CONTINUE = "continue"
    MODIFY = "modify"
    REJECT = "reject"
    REQUIRE_APPROVAL = "require_approval"
    HALT = "halt"


@dataclass(frozen=True)
class HookOutcome:
    action: HookAction = HookAction.CONTINUE
    replacement: Any = None
    reason: str = ""

    @property
    def is_continue(self) -> bool:
        return self.action is HookAction.CONTINUE


CONTINUE = HookOutcome()


class RuntimeHook:
    """Subclass and override only what you need."""

    def before_model(self, request: Any) -> HookOutcome:
        return CONTINUE

    def after_model(self, response: Any) -> HookOutcome:
        return CONTINUE

    def before_tool(self, tool_call: Any) -> HookOutcome:
        return CONTINUE

    def after_tool(self, result: Any) -> HookOutcome:
        return CONTINUE
