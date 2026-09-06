"""Canonical, provider-independent primitives (LLD 3.1, FR-2).

Every provider adapter translates to and from these types. Nothing in this
module knows what a provider is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class Role(str, Enum):
    """Matches the `messages.role` column enum exactly (LLD 2.2)."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Origin(str, Enum):
    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    MODEL = "model"
    INTERNAL_TOOL = "internal_tool"
    EXTERNAL_TOOL = "external_tool"
    MCP_RESOURCE = "mcp_resource"


class InstructionAuthority(str, Enum):
    AUTHORITATIVE = "authoritative"
    ADVISORY = "advisory"
    DATA_ONLY = "data_only"


class TrustZone(str, Enum):
    TRUSTED_SOURCE = "trusted_source"
    VALIDATED = "validated"
    UNTRUSTED = "untrusted"


class TaintFlag(str, Enum):
    EXTERNAL_CONTENT = "external_content"
    USER_CONTROLLED = "user_controlled"
    EXECUTABLE_CONTENT = "executable_content"
    PROMPT_INJECTION_RISK = "prompt_injection_risk"
    SECRET_BEARING = "secret_bearing"


# Least-trusted wins when provenance is combined. Ordered most to least trusted.
_TRUST_ORDER = (TrustZone.TRUSTED_SOURCE, TrustZone.VALIDATED, TrustZone.UNTRUSTED)


@dataclass(frozen=True)
class ContentProvenance:
    """Where content came from and how far it may be trusted (ADR-17, ADR-26).

    Provenance *informs* policy; it does not enforce it. The enforcement chain is
    PolicyEngine -> ToolExecutor -> Approval/Credential/Sandbox/Network. Nothing
    here authorizes anything.
    """

    origin: Origin
    instruction_authority: InstructionAuthority
    trust_zone: TrustZone
    taint_flags: frozenset[TaintFlag] = frozenset()
    source_uri_or_hash: str | None = None

    def __post_init__(self) -> None:
        # Accept any iterable of flags but always store a frozenset, so equality
        # and union behave regardless of what the caller passed.
        if not isinstance(self.taint_flags, frozenset):
            object.__setattr__(self, "taint_flags", frozenset(self.taint_flags))

    @classmethod
    def internal_tool(cls, source_uri_or_hash: str | None = None) -> ContentProvenance:
        """The Phase 0 default for a local tool's result (LLD 3.1).

        No external or MCP tool exists yet, so nothing in Phase 0 exercises the
        untrusted or tainted path -- but the fields are populated correctly now
        so that path is not retrofitted later.
        """
        return cls(
            origin=Origin.INTERNAL_TOOL,
            instruction_authority=InstructionAuthority.DATA_ONLY,
            trust_zone=TrustZone.TRUSTED_SOURCE,
            taint_flags=frozenset(),
            source_uri_or_hash=source_uri_or_hash,
        )

    @classmethod
    def from_model(cls, *inputs: ContentProvenance) -> ContentProvenance:
        """Provenance for content a model generated from `inputs`.

        THE TAINT PROPAGATION RULE (ADR-26): model-generated content does not
        automatically clear taint inherited from its inputs. A model paraphrasing
        an untrusted source does not launder it. Taint is the union of every
        input's taint, and trust is that of the least-trusted input. Only an
        explicit validator or policy decision may downgrade or clear either --
        never the mere act of passing through the model.
        """
        taint: frozenset[TaintFlag] = frozenset()
        trust = TrustZone.TRUSTED_SOURCE
        for provenance in inputs:
            taint |= provenance.taint_flags
            if _TRUST_ORDER.index(provenance.trust_zone) > _TRUST_ORDER.index(trust):
                trust = provenance.trust_zone
        return cls(
            origin=Origin.MODEL,
            # Model output is never authoritative over the developer's instructions.
            instruction_authority=InstructionAuthority.ADVISORY,
            trust_zone=trust,
            taint_flags=taint,
        )

    def with_taint(self, *flags: TaintFlag) -> ContentProvenance:
        return replace(self, taint_flags=self.taint_flags | frozenset(flags))

    @property
    def is_tainted(self) -> bool:
        return bool(self.taint_flags)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Set when a provider sent arguments that could not be decoded. Empty
    # arguments are NOT self-evidently invalid: a tool whose schema has no
    # required properties would happily accept {} and execute, turning garbled
    # model output into a silently successful call. ToolExecutor checks this
    # before schema validation so the failure is explicit rather than delegated
    # to a schema the adapter does not control.
    arguments_error: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """The result of one tool call.

    INVARIANT (LLD 3.1): every ToolResult carries exactly one ContentProvenance.
    `provenance` has no default precisely so that omitting it is a TypeError at
    construction rather than a None discovered later in the session store.
    """

    tool_call_id: str
    content: str
    provenance: ContentProvenance
    is_error: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, ContentProvenance):
            raise TypeError(
                "ToolResult.provenance must be a ContentProvenance, got "
                f"{type(self.provenance).__name__}"
            )


@dataclass(frozen=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "tool_results", tuple(self.tool_results))
