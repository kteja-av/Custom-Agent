"""Canonical, provider-independent primitives (LLD 3.1, FR-2).

Every provider adapter translates to and from these types. Nothing in this
module knows what a provider is.
"""

from __future__ import annotations

import json
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

    def __post_init__(self) -> None:
        # Arguments that no durable store can hold travel on the SAME channel as
        # undecodable ones, and the check is here rather than in an adapter
        # because every adapter would otherwise have to remember it -- and
        # NFR-1's whole claim is that a new provider is a configuration change.
        # A ToolCall whose arguments cannot be stored therefore cannot exist
        # without saying so.
        if self.arguments_error is None:
            reason = unstorable_reason(self.arguments)
            if reason is not None:
                object.__setattr__(
                    self, "arguments_error", f"arguments cannot be stored: {reason}"
                )
                # Cleared, not merely flagged. Flagging alone still leaves the
                # unstorable value in `arguments`, and the assistant message
                # carrying this call is persisted whether or not the executor
                # runs it -- so the write fails anyway and the divergence
                # survives. This is the same shape undecodable JSON already
                # takes: empty arguments plus the reason they are empty, which
                # ToolExecutor step 2 rejects before anything can execute {}.
                object.__setattr__(self, "arguments", {})


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
        # Unlike tool arguments, content has no error channel to travel on, so
        # an unstorable one is refused outright. This raises where ToolCall
        # merely flags because there is no honest alternative: dropping the byte
        # would edit the record NFR-3 calls authoritative, and accepting it
        # would make the run's outcome depend on which store was configured.
        #
        # Raising here is contained by design. Both surrounding boundaries are
        # total, so this surfaces as a terminal RunResult rather than a crash,
        # and ModelClient.send() converts it to a typed ModelError on the way
        # out -- which is what lets a caller tell "the model emitted something
        # unstorable" from "the database is down".
        reason = unstorable_reason(self.content)
        if reason is not None:
            raise ValueError(f"Message.content cannot be stored: {reason}")


# --- storability (M5 round 3) -----------------------------------------------

_NUL = "\x00"
_MAX_DEPTH = 60


def unstorable_reason(value: Any) -> str | None:
    """Why `value` could not survive a round trip through a durable store.

    Returns None when it can. Total by intent: it must never raise, because it
    runs on paths that are themselves boundaries.

    Two layers, for the same reason the ModelClient boundary has two: a named
    diagnosis is worth more than a generic one, but an enumeration of known bad
    values cannot be complete. Round 3 named NUL and non-finite floats; round 4
    found a lone UTF-16 surrogate walking straight past. A truncated surrogate
    escape is a legal RFC-8259 decode, models emit truncated pairs, and both
    TEXT and JSONB refuse the result. So the named checks are backed by
    attempting the serialisation the store performs, which catches the CLASS
    rather than the instance.

    The check lives here, above every store, rather than in postgres.py. That
    module promises "the loop cannot tell whether it is talking to memory or
    Postgres", and deciding this at write time is what made the promise false:
    the same run completed in memory and failed against the database.
    """
    named = _named_unstorable_reason(value)
    if named is not None:
        return named
    # The backstop. json.dumps with allow_nan=False and ensure_ascii=False is
    # the closest thing to what psycopg then hands Postgres, so a value that
    # cannot get through here cannot get into a row: lone surrogates fail the
    # encode, integers past the interpreter digit limit fail the dump, and so
    # does any object with no JSON representation.
    #
    # allow_nan=False is redundant today -- the named layer already catches
    # non-finite floats, so mutating it to True changes nothing the suite can
    # see. It stays because the two layers are meant to overlap: the named one
    # exists for better messages, not as the only defence, and a future edit
    # there should not silently reopen this.
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - total by intent
        try:
            return f"cannot be serialised for storage: {type(exc).__name__}"
        except Exception:  # noqa: BLE001
            return "cannot be serialised for storage"
    return None


def _named_unstorable_reason(value: Any, _depth: int = 0) -> str | None:
    """The diagnosable half: the failures worth naming precisely."""
    try:
        if _depth > _MAX_DEPTH:
            return "nested more deeply than the store can accept"
        if isinstance(value, str):
            return "text contains a NUL character" if _NUL in value else None
        if isinstance(value, bool) or value is None or isinstance(value, int):
            return None
        if isinstance(value, float):
            if value != value:
                return "NaN cannot be stored: JSON has no representation for it"
            if value in (float("inf"), float("-inf")):
                return f"{value} cannot be stored: JSON has no representation for it"
            return None
        if isinstance(value, dict):
            for key, item in value.items():
                reason = _named_unstorable_reason(key, _depth + 1)
                if reason is None:
                    reason = _named_unstorable_reason(item, _depth + 1)
                if reason is not None:
                    return reason
            return None
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                reason = _named_unstorable_reason(item, _depth + 1)
                if reason is not None:
                    return reason
            return None
        # Not reachable from decoded JSON. The backstop above decides it.
        return None
    except Exception:  # noqa: BLE001 - total by intent, see unstorable_reason
        return "could not be checked for storability"
