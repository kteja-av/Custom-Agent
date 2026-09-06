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

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .primitives import Message, ToolCall


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_CALLS = "tool_calls"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
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
