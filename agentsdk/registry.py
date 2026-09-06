"""Model registry (FR-12, LLD 2.5).

Phase 0 records capabilities and quirks; Phase 8 is what gates production
eligibility on eval status. The table exists now so "which model version
produced this run" is answerable from the start.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _version_key(version: str) -> tuple[object, ...]:
    """Natural sort key: digit runs compare numerically, everything else as text."""
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", version)
        if part
    )


@dataclass(frozen=True)
class ModelCapabilities:
    max_context_tokens: int
    supports_parallel_tool_calls: bool = True
    cost_per_token: float | None = None


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    model_id: str
    model_version: str
    adapter_version: str
    capabilities: ModelCapabilities
    known_quirks: str | None = None
    eval_status: str | None = None  # unpopulated until the eval suite exists
    production_eligibility: bool = True

    @property
    def key(self) -> tuple[str, str, str]:
        """Primary key of the `model_registry` table."""
        return (self.provider, self.model_id, self.model_version)


class ModelRegistry:
    def __init__(self, entries: list[ModelEntry] | None = None) -> None:
        self._entries: dict[tuple[str, str, str], ModelEntry] = {}
        for entry in entries or []:
            self.register(entry)

    def register(self, entry: ModelEntry) -> None:
        self._entries[entry.key] = entry

    def get(self, provider: str, model_id: str, model_version: str) -> ModelEntry | None:
        return self._entries.get((provider, model_id, model_version))

    def resolve(self, model_id: str) -> ModelEntry | None:
        """Latest registered entry for a model id, ignoring version.

        Ordered naturally, not lexicographically: a plain string sort puts "10"
        before "2" and would quietly return the wrong "latest".
        """
        matches = [e for e in self._entries.values() if e.model_id == model_id]
        return max(matches, key=lambda e: _version_key(e.model_version)) if matches else None

    def entries(self) -> tuple[ModelEntry, ...]:
        return tuple(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)


def provider_of(model_id: str) -> str:
    """The gateway namespaces model ids by upstream provider.

    'bedrock.anthropic.claude-haiku-4-5' -> 'bedrock'. Anything unprefixed is
    reported as 'unknown' rather than guessed.
    """
    head, _, rest = model_id.partition(".")
    return head if rest else "unknown"


def default_registry() -> ModelRegistry:
    """The two models the Phase 0 golden eval uses to prove NFR-1.

    Both were verified to return real tool_calls through the configured
    gateway before being listed here.
    """
    adapter = "openai-compatible/1"
    return ModelRegistry(
        [
            ModelEntry(
                provider="openai",
                model_id="openai.gpt-4o-mini",
                model_version="2024-07-18",
                adapter_version=adapter,
                capabilities=ModelCapabilities(max_context_tokens=128_000),
            ),
            ModelEntry(
                provider="bedrock",
                model_id="bedrock.anthropic.claude-haiku-4-5",
                model_version="4-5",
                adapter_version=adapter,
                capabilities=ModelCapabilities(max_context_tokens=200_000),
                known_quirks=(
                    "Tool call ids are prefixed 'tooluse_' rather than 'call_'; "
                    "argument JSON arrives with spaces after separators."
                ),
            ),
        ]
    )
