"""Tool specification and registry (FR-4, LLD 3.4).

`ToolRegistry` does lookup and nothing else. The lifecycle lives in
`ToolExecutor`, the allow/deny decision lives in `PermissionChecker` -- keeping
those three apart is what lets Phase 4 swap in an MCP-backed catalog without
touching either of the others.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import ToolError, ToolNotFound
from .primitives import unstorable_reason


class RiskClass(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class ApprovalPolicy(str, Enum):
    AUTO = "auto"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk_class: RiskClass = RiskClass.READ_ONLY
    read_only: bool = True
    idempotent: bool = True
    # Stubbed to AUTO until ApprovalManager exists (Phase 4).
    approval_policy: ApprovalPolicy = ApprovalPolicy.AUTO
    # ponytail: one flat timeout per tool; per-call deadlines land with
    # cancellation in Phase 2 if a tool ever needs its own budget.
    timeout_seconds: float | None = 30.0

    def __post_init__(self) -> None:
        """A schema that cannot be serialised is refused at REGISTRATION.

        Round 8: a schema containing a `set` -- a natural mistake when writing
        an enum -- broke every persisted run, while the same code completed
        happily in memory. `schema_hash()` is only reached when persistence is
        configured, because that is the only caller of `build_manifest`, so a
        developer error surfaced as a database-dependent runtime failure of an
        unrelated run. It did not even need the tool to be called: the manifest
        hashes every REGISTERED tool.

        Failing here makes the behaviour identical with and without a database,
        and puts the error where the mistake is.
        """
        reason = unstorable_reason(self.input_schema)
        if reason is not None:
            raise ToolError(
                f"tool {self.name!r} has an input_schema that cannot be stored: {reason}"
            )

    def schema_hash(self) -> str:
        """Feeds ExecutionManifest.tool_spec_hashes (FR-11)."""
        payload = json.dumps(
            {
                "name": self.name,
                "description": self.description,
                "input_schema": self.input_schema,
                "risk_class": self.risk_class.value,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Tool:
    """A spec bound to an implementation. The callable may be sync or async."""

    spec: ToolSpec
    fn: Callable[..., Any]

    @property
    def name(self) -> str:
        return self.spec.name


class ToolRegistry:
    """Name/schema lookup only."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """A duplicate name is a startup-time configuration bug, so it raises
        here rather than at call time (LLD 3.4)."""
        if tool.name in self._tools:
            raise ToolError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFound(f"no such tool: {name}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec for tool in self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-compatible tool schemas, as the gateway expects them (FR-3)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            }
            for spec in self.specs()
        ]
