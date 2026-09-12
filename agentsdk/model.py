"""Provider-neutral model contract (FR-3, LLD 3.2).

`send(ModelRequest) -> ModelResponse` is the whole interface. Everything a
provider knows about itself lives behind it, which is what makes NFR-1 a
configuration change rather than a code change.

Note on system instructions: `Role` has no `system` member because it mirrors
the `messages.role` column enum exactly (user/assistant/tool). Instructions
therefore ride on `ModelRequest.instructions`, and each adapter renders them the
way its provider expects. Keeping the persisted enum honest is worth more than
the convenience of a fourth role.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .primitives import Message, ToolCall, _replace_unstorable_text


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_CALLS = "tool_calls"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


class ReasoningEffort(str, Enum):
    """How hard a model should reason before answering (FR-28).

    Provider-neutral: each adapter maps it to its own wire format, and the
    OpenAI-compatible one sends `reasoning_effort`. It reaches a payload only
    when a run sets it, so a model that rejects the parameter is unaffected by
    runs that never ask for it. Later providers add members, not a new type
    (NFR-7).
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def token_count(value: Any) -> int:
    """The one place a provider-supplied number becomes an int.

    Three rejections across three milestones were the same defect wearing a
    different call site: M3 round 3 found `int(float('inf'))` raising
    OverflowError in the adapter, and M5 round 2 found it again in usage
    reconstruction -- on the total boundary's error path, where raising is
    worst. Each was fixed where it was found, which guaranteed the next
    unguarded `int()` would be a fresh defect rather than a caught regression.

    So the coercion lives on the type instead of at the call sites: a `Usage`
    cannot hold a value that is not an int, whoever constructs it. Nonsense
    degrades to 0 -- token accounting is never worth failing a run over --
    and everything a provider can express is treated as possible input: NaN,
    Infinity (both are legal `json.loads` output), a string, None, or an
    object whose `__int__` raises.

    BaseException still propagates: that is control flow, not a token count.
    """
    if isinstance(value, bool):
        return 0  # bool is an int in Python; a flag is not a token count
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except Exception:  # noqa: BLE001 - total by intent, see the docstring
        return 0


@dataclass(frozen=True)
class Usage:
    """Tokens consumed by one model call, or summed over a run (FR-29).

    What each field means is fixed here, not left to each provider:

      * `prompt_tokens` INCLUDES `cache_read_tokens` and `cache_write_tokens`;
      * `completion_tokens` INCLUDES `reasoning_tokens`.

    That is the gateway's own accounting, measured rather than assumed
    (KNOWLEDGE-312441cb: 2560 cached of 2620 prompt tokens; 25 reasoning of 45
    completion tokens), and it is what lets cost subtract cached tokens once
    instead of charging them twice. An adapter whose provider reports them the
    other way normalises before it constructs a Usage.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        # Enforced here rather than trusted from the caller: this dataclass is
        # built from provider JSON and from replayed event payloads, neither of
        # which is under our control. Every field is walked rather than named,
        # so a count added later is coerced without anyone remembering to.
        for f in fields(self):
            object.__setattr__(self, f.name, token_count(getattr(self, f.name)))

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            **{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)}
        )


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[Message, ...]
    tools: tuple[dict[str, Any], ...] = ()
    instructions: str | None = None
    # Always None in Phase 0. The slot exists so AgentLoop and every adapter
    # keep their signature when Phase 2 starts populating it (FR-16).
    output_schema: dict[str, Any] | None = None
    model_settings: dict[str, Any] = field(default_factory=dict)
    provider_state: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))


@dataclass(frozen=True)
class ModelResponse:
    message: Message
    stop_reason: StopReason
    usage: Usage = Usage()
    structured_output: dict[str, Any] | None = None
    provider_response_id: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # provider_response_id is copied into the ModelCalled event payload and
        # written to run_events.payload, so it reaches a column exactly as the
        # message does -- and round 5 found it unguarded for that reason: the
        # check had been applied to the message and not to what travels beside
        # it. Same walk as the primitives, for the same reason.
        _replace_unstorable_text(self)

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """Mirrors the design's separate `tool_calls[]` field.

        Derived rather than stored: two copies of the same list are two things
        that can disagree.
        """
        return self.message.tool_calls


@runtime_checkable
class ModelClient(Protocol):
    async def send(self, request: ModelRequest) -> ModelResponse: ...
