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
from .primitives import (
    ContentProvenance,
    InstructionAuthority,
    Origin,
    TaintFlag,
    TrustZone,
    unstorable_reason,
)

# FR-41, decision D5: the longest content a tool result may carry, in characters.
DEFAULT_MAX_OUTPUT_CHARS = 50_000


class RiskClass(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class ApprovalPolicy(str, Enum):
    AUTO = "auto"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class ResultProvenance:
    """The provenance a tool declares for its results (FR-40).

    Everything ContentProvenance holds except the source, which differs per
    result, so the executor supplies it. The defaults are what every tool's
    results carried before M10, so a tool that declares nothing is unchanged.
    Values are coerced through their enums, so a declaration that names a
    label which does not exist is refused where it is written.
    """

    origin: Origin = Origin.INTERNAL_TOOL
    trust_zone: TrustZone = TrustZone.TRUSTED_SOURCE
    instruction_authority: InstructionAuthority = InstructionAuthority.DATA_ONLY
    taint_flags: frozenset[TaintFlag] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", Origin(self.origin))
        object.__setattr__(self, "trust_zone", TrustZone(self.trust_zone))
        object.__setattr__(
            self, "instruction_authority", InstructionAuthority(self.instruction_authority)
        )
        object.__setattr__(
            self, "taint_flags", frozenset(TaintFlag(flag) for flag in self.taint_flags)
        )

    @classmethod
    def external(cls) -> ResultProvenance:
        """Content from outside the deployment: a fetched page, a search result."""
        return cls(
            origin=Origin.EXTERNAL_TOOL,
            trust_zone=TrustZone.UNTRUSTED,
            instruction_authority=InstructionAuthority.DATA_ONLY,
            taint_flags=frozenset({TaintFlag.EXTERNAL_CONTENT, TaintFlag.PROMPT_INJECTION_RISK}),
        )

    def for_source(self, source_uri_or_hash: str | None) -> ContentProvenance:
        return ContentProvenance(
            origin=self.origin,
            instruction_authority=self.instruction_authority,
            trust_zone=self.trust_zone,
            taint_flags=self.taint_flags,
            source_uri_or_hash=source_uri_or_hash,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "origin": self.origin.value,
            "trust_zone": self.trust_zone.value,
            "instruction_authority": self.instruction_authority.value,
            "taint_flags": sorted(flag.value for flag in self.taint_flags),
        }


_UNDECLARED = ResultProvenance()


@dataclass(frozen=True)
class ToolOutput:
    """What a tool returns when its result has a source of its own.

    The fetch tool records the URL it finally read this way (FR-38). The source
    becomes the result's `source_uri_or_hash`; without one the tool's schema
    hash stands, as it always did. Returning a plain value is unchanged.
    """

    content: Any
    source_uri: str | None = None


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
    # FR-40: what the executor records as the provenance of this tool's results.
    result_provenance: ResultProvenance = _UNDECLARED
    # FR-41, decision D5. Applied to every result the executor returns for this
    # tool, errors and hook substitutions included. There is no uncapped setting.
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    # What the implementation is bound to -- a file tool's root folder, a fetch
    # tool's allowlist -- so a manifest's tool hash names it. Never sent to the
    # model: schemas() does not read it.
    configuration: dict[str, Any] = field(default_factory=dict)

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
        if not isinstance(self.result_provenance, ResultProvenance):
            raise ToolError(
                f"tool {self.name!r} has a result_provenance that is not a ResultProvenance: "
                f"{type(self.result_provenance).__name__}"
            )
        cap = self.max_output_chars
        # A bool is an int, and True would cap every result at one character.
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise ToolError(
                f"tool {self.name!r} has max_output_chars={cap!r}: it must be a positive int, "
                "and there is no uncapped setting"
            )
        if not isinstance(self.configuration, dict):
            raise ToolError(
                f"tool {self.name!r} has a configuration that is not a dict: "
                f"{type(self.configuration).__name__}"
            )
        reason = unstorable_reason(self.configuration)
        if reason is not None:
            raise ToolError(f"tool {self.name!r} has a configuration that cannot be stored: {reason}")

    def schema_hash(self) -> str:
        """Feeds ExecutionManifest.tool_spec_hashes (FR-11).

        The M10 fields enter the hash only when they differ from their defaults,
        so every tool that declares none of them keeps the hash every manifest
        before M10 recorded for it (AC-32).
        """
        fields: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk_class": self.risk_class.value,
        }
        if self.result_provenance != _UNDECLARED:
            fields["result_provenance"] = self.result_provenance.to_json()
        if self.max_output_chars != DEFAULT_MAX_OUTPUT_CHARS:
            fields["max_output_chars"] = self.max_output_chars
        if self.configuration:
            fields["configuration"] = self.configuration
        payload = json.dumps(fields, sort_keys=True)
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
