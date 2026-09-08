"""Canonical, provider-independent primitives (LLD 3.1, FR-2).

Every provider adapter translates to and from these types. Nothing in this
module knows what a provider is.
"""

from __future__ import annotations

import json
import dataclasses
import sys
import uuid
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
        # source_uri_or_hash is persisted with every tool result.
        _replace_unstorable_text(self)

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
        # `id` and `name` reach the same JSONB column `arguments` does. Round 5
        # found them unguarded because the check had been applied to the fields
        # someone thought of -- so this walks EVERY field instead (see
        # _replace_unstorable_text), and a field added later is covered without
        # anyone remembering.
        #
        # The check is here rather than in an adapter because every adapter
        # would otherwise have to remember it, and NFR-1's whole claim is that
        # a new provider is a configuration change.
        problems = _replace_unstorable_text(self)
        reason = unstorable_reason(self.arguments)
        if reason is not None:
            # Cleared, not merely flagged. Flagging alone still leaves the
            # unstorable value in `arguments`, and the assistant message
            # carrying this call is persisted whether or not the executor runs
            # it -- so the write fails anyway and the divergence survives. This
            # is the shape undecodable JSON already takes: empty arguments plus
            # the reason they are empty, which ToolExecutor step 2 rejects
            # before anything can execute {}.
            object.__setattr__(self, "arguments", {})
            problems.append(f"arguments cannot be stored: {reason}")
        if problems and self.arguments_error is None:
            object.__setattr__(self, "arguments_error", "; ".join(problems))
        # No second check on arguments_error. The walk above already covers it
        # -- it is a field like any other, and exempting it was the round-6
        # defect: a carve-out inside the mechanism whose purpose was to end
        # carve-outs. A backstop here would only be reachable if the reason
        # string this method just built were itself unstorable, which it cannot
        # be, and it would mask the carve-out coming back: with both guards in
        # place, reintroducing the skip left the whole suite green.


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
        # tool_call_id goes into the same JSONB column the content does.
        _replace_unstorable_text(self, skip=("content",))
        reason = unstorable_reason(self.content)
        if reason is not None:
            # A result whose content cannot be stored is a failed result: the
            # model is told why, rather than the run dying at the write. The
            # executor normally catches this first; this is the backstop for a
            # ToolResult built anywhere else.
            object.__setattr__(self, "content", f"tool result cannot be stored: {reason}")
            object.__setattr__(self, "is_error", True)


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
        _replace_unstorable_text(self, skip=("content",))
        reason = unstorable_reason(self.content)
        if reason is not None:
            raise ValueError(f"Message.content cannot be stored: {reason}")


# --- storability (M5 round 3) -----------------------------------------------

_NUL = "\x00"
# A PREFIX, not a value. Two tool calls with unstorable ids used to collapse to
# the same marker, which destroys the call-to-result correlation in the very
# record kept to explain what happened. Each replacement gets its own suffix so
# distinct values stay distinct.
UNSTORABLE = "<unstorable"


def _unstorable_marker() -> str:
    return f"{UNSTORABLE}:{uuid.uuid4().hex[:8]}>"
# Half the recursion limit: see the margin note in _named_unstorable_reason.
_MAX_NESTING = max(64, sys.getrecursionlimit() // 2)


def refuse_unstorable_fields(instance: Any) -> None:
    """For CONFIGURATION types: refuse and name the field, never degrade.

    Two differences from _replace_unstorable_text, both deliberate:

    * It RAISES. Model output has a run to keep alive, so an unstorable value
      there is replaced and flagged; configuration is supplied by the caller
      before anything starts, so there is nothing to preserve and a named
      error beats an opaque psycopg failure three frames later.
    * It covers fields of ANY shape, not just `str`. The replacement walk only
      handles string fields, and round 7 found the gap through
      `PrincipalContext.scopes` -- a `tuple[str, ...]` whose contents reached
      JSONB unchecked. `unstorable_reason` already recurses, so passing it the
      whole field value covers tuples, lists and nested dicts alike.
    """
    for f in dataclasses.fields(instance):
        reason = unstorable_reason(getattr(instance, f.name, None))
        if reason is not None:
            raise ValueError(
                f"{type(instance).__name__}.{f.name} cannot be stored: {reason}"
            )


def _replace_unstorable_text(instance: Any, *, skip: tuple[str, ...] = ()) -> list[str]:
    """Replace every unstorable STRING field with a marker; return the reasons.

    Walks `dataclasses.fields()` rather than a list of names, because the
    enumeration IS the defect this exists to fix. Five review rounds found the
    storability check correct but applied only to the fields someone had
    thought of -- Message.content, ToolCall.arguments, ToolResult.content --
    while id, name, tool_call_id and source_uri_or_hash reached the same
    columns unguarded. Walking the fields means one added tomorrow is covered
    without anyone remembering.

    Identifiers are replaced rather than refused: a NUL in a tool name should
    become an ordinary "no such tool" error result, which is what happens
    without persistence -- not a run that dies at the write with no record of
    what the model said (NFR-3). The marker is deliberately conspicuous.

    Total by intent: it is called from constructors that sit under boundaries.
    """
    problems: list[str] = []
    try:
        for f in dataclasses.fields(instance):
            if f.name in skip:
                continue
            value = getattr(instance, f.name, None)
            if not isinstance(value, str):
                continue
            reason = unstorable_reason(value)
            if reason is not None:
                object.__setattr__(instance, f.name, _unstorable_marker())
                problems.append(f"{f.name} cannot be stored: {reason}")
    except Exception:  # noqa: BLE001 - total by intent
        problems.append("a field could not be checked for storability")
    return problems



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
    return _serialisation_reason(value)


def _serialisation_reason(value: Any) -> str | None:
    """The backstop, as its own function so it can be tested as its own layer.

    It overlaps the named checks deliberately, and while it was inlined the
    overlap made both halves individually deletable with the suite still green
    -- the exact failure KNOWLEDGE-fd4720a4 records. A guard that cannot be
    called on its own cannot be pinned on its own.
    """
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - total by intent
        try:
            return f"cannot be serialised for storage: {type(exc).__name__}"
        except Exception:  # noqa: BLE001
            return "cannot be serialised for storage"
    return None


def _named_unstorable_reason(value: Any) -> str | None:
    """The diagnosable half: the failures worth naming precisely.

    Iterative, with a set of visited container ids, rather than recursive with
    a depth cap. The cap was a defect of its own: it declared anything nested
    past 60 unstorable, and Postgres stores 900-deep JSON without complaint, so
    a model returning deeply nested arguments had its tool call refused over a
    limit that does not exist. Over-rejection fails runs that should work.

    Raising the number would only have moved the wrong answer. Depth is not
    this function's call to make -- the serialiser and the database agree on
    where it ends (both give up around 1000, at the interpreter's recursion
    limit), so the backstop decides it. Walking iteratively also means a NUL
    nested 300 deep is still found by name, which deferring on depth would have
    quietly stopped doing.
    """
    try:
        stack = [(value, 0)]
        seen: set[int] = set()
        while stack:
            item, depth = stack.pop()
            if depth > _MAX_NESTING:
                # A MARGIN, not a storage limit -- and deliberately a false
                # positive in a narrow band. Postgres accepts about 969 levels,
                # but that number is the interpreter's recursion limit minus
                # whatever stack the caller already used, so it moves: this
                # helper and psycopg run at different depths and can disagree.
                # Disagreeing in the accepting direction means the write fails,
                # which costs the audit trail (NFR-3); disagreeing in the
                # refusing direction costs an explicit tool error on nesting no
                # model plausibly emits. Refusing is the cheaper mistake.
                return (
                    f"nested more than {_MAX_NESTING} deep, which is too close to "
                    "the serialiser's recursion limit to store reliably"
                )
            if isinstance(item, str):
                if _NUL in item:
                    return "text contains a NUL character"
                continue
            if isinstance(item, bool) or item is None or isinstance(item, int):
                continue
            if isinstance(item, float):
                if item != item:
                    return "NaN cannot be stored: JSON has no representation for it"
                if item in (float("inf"), float("-inf")):
                    return f"{item} cannot be stored: JSON has no representation for it"
                continue
            if isinstance(item, dict):
                if id(item) in seen:  # a cycle, or the same object twice
                    continue
                seen.add(id(item))
                for key, sub in item.items():
                    stack.append((key, depth + 1))
                    stack.append((sub, depth + 1))
                continue
            if isinstance(item, (list, tuple, set, frozenset)):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                stack.extend((sub, depth + 1) for sub in item)
                continue
            # Not reachable from decoded JSON. The backstop decides it.
        return None
    except Exception:  # noqa: BLE001 - total by intent, see unstorable_reason
        return "could not be checked for storability"
