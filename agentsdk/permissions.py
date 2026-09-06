"""Permission decisions (FR-6, LLD 3.6).

This is the near end of the only enforcement chain that counts:

    PolicyEngine -> ToolExecutor -> Approval/Credential/Sandbox/Network

Authorization never depends on the model respecting a provenance label.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .identity import PrincipalContext
from .primitives import ToolCall


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionResult:
    decision: Decision
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@runtime_checkable
class PermissionChecker(Protocol):
    def check(
        self, tool_call: ToolCall, principal_context: PrincipalContext | None
    ) -> PermissionResult: ...


class AllowlistPermissionChecker:
    """Phase 0 default: name against a fixed set.

    `principal_context` is accepted and ignored. The parameter exists so Phase
    4's principal-aware checkers implement the same interface instead of forcing
    a signature change through every call site.
    """

    def __init__(self, allowed: set[str] | frozenset[str]) -> None:
        self._allowed = frozenset(allowed)

    def check(
        self, tool_call: ToolCall, principal_context: PrincipalContext | None = None
    ) -> PermissionResult:
        if tool_call.name in self._allowed:
            return PermissionResult(Decision.ALLOW, "in allowlist")
        return PermissionResult(
            Decision.DENY, f"tool {tool_call.name!r} is not in the allowlist"
        )


class DenyAllPermissionChecker:
    """Useful default for tests that must prove the deny path is real."""

    def check(
        self, tool_call: ToolCall, principal_context: PrincipalContext | None = None
    ) -> PermissionResult:
        return PermissionResult(Decision.DENY, "deny-all policy")
