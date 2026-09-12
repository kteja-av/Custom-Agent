"""Model registry (FR-12, FR-30, LLD 2.5).

Phase 0 records capabilities and quirks; Phase 8 is what gates production
eligibility on eval status. The table exists now so "which model version
produced this run" is answerable from the start.

M9 adds prices, and the SDK ships none. The gateway does not publish them
(`/model/info` answers 403), and a bundled price that goes stale reports a wrong
number rather than an unknown one. The caller supplies them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from decimal import Context, Decimal, InvalidOperation, localcontext

from .model import Usage


def _version_key(version: str) -> tuple[object, ...]:
    """Natural sort key: digit runs compare numerically, everything else as text."""
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", version)
        if part
    )


# What a NUMERIC column can hold a cost of. M9 round 1 accepted a price like
# 1E-16384 USD per token, whose cost RunResult reported as a number while the
# row stored NULL -- "unstorable" dressed as "unknown". Refused where the price
# is chosen instead: NUMERIC keeps at most 16383 digits after the point and
# 131072 before it, and the upper bound leaves room for a BIGINT count (19
# digits) and the carry of summing four token classes.
_PRICE_MIN_EXPONENT = -16383
_PRICE_MAX_ADJUSTED = 131000


def _price(value: object, name: str) -> Decimal | None:
    """A USD-per-token price, refused by name at construction if it is not one.

    Configuration, so it RAISES rather than degrading -- the rule
    `refuse_unstorable_fields` states for configuration types. A float is taken
    at its written value: 0.1 becomes Decimal("0.1"), not the binary
    approximation Decimal(0.1) would give.
    """
    if value is None:
        return None
    price: Decimal | None = None
    if not isinstance(value, bool):
        try:
            if isinstance(value, float):
                price = Decimal(repr(value))
            elif isinstance(value, (Decimal, int, str)):
                price = Decimal(value)
        except (InvalidOperation, ValueError, TypeError):
            price = None
    if price is None or not price.is_finite() or price < 0:
        raise ValueError(
            f"ModelPricing.{name} must be a finite, non-negative USD price per token "
            f"or None, got {value!r}"
        )
    if price.as_tuple().exponent < _PRICE_MIN_EXPONENT or price.adjusted() > _PRICE_MAX_ADJUSTED:
        raise ValueError(
            f"ModelPricing.{name} {value!r} is a price no NUMERIC column could hold a cost of: "
            f"at most {-_PRICE_MIN_EXPONENT} digits after the point, and below "
            f"1E+{_PRICE_MAX_ADJUSTED + 1}"
        )
    # -0 is a legal Decimal and a legal zero, but it would reach a manifest as "-0".
    return Decimal(0) if price == 0 else price


@dataclass(frozen=True)
class ModelPricing:
    """USD per token, by token class (FR-30).

    None means "no price for this class", which is not the same as free: a call
    that used a class with no price costs None, never a guess. Decimals, never
    floats, because money summed across thousands of calls must not drift.
    """

    input: Decimal | None = None
    output: Decimal | None = None
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None

    def __post_init__(self) -> None:
        for f in fields(self):
            object.__setattr__(self, f.name, _price(getattr(self, f.name), f.name))

    def to_json(self) -> dict[str, str | None]:
        """Strings, because the manifest column is JSONB and JSON has no decimal."""
        return {
            f.name: None if getattr(self, f.name) is None else str(getattr(self, f.name))
            for f in fields(self)
        }


@dataclass(frozen=True)
class ModelCapabilities:
    max_context_tokens: int
    supports_parallel_tool_calls: bool = True
    # FR-30, decision D3 (FR-12 amended): replaces the single `cost_per_token`,
    # which nothing read and which could not say which tokens it priced.
    pricing: ModelPricing | None = None


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


# --- cost (FR-30, NFR-11) --------------------------------------------------------

# Fixed rather than inherited: the caller's thread-local decimal context could
# otherwise change what a run is said to have cost. A hundred digits keeps every
# realistic sum exact; past that, Decimal rounds, and an overflow is trapped
# below and reported as an unknown cost rather than raised.
_COST_CONTEXT = Context(prec=100)


def call_cost(usage: Usage, pricing: ModelPricing | None) -> Decimal | None:
    """What one model call cost in USD, or None when that cannot be known.

    Each token class's count is taken as the provider reported it. Uncached
    input is `prompt - cache_read - cache_write`, because Usage's prompt_tokens
    includes both; reasoning is inside completion and is never priced a second
    time. None when there is no pricing, or when a class with a non-zero count
    has no price -- never 0 for an unknown cost.

    "Non-zero" is read on the count as reported, BEFORE anything is clamped. A
    provider can send a negative count and Usage keeps it; round 1 clamped first
    and so priced a negative count in an unpriced class as if it were absent.
    Counts are clamped at zero only for the arithmetic, so a cost is never
    negative. Total by intent: accounting never fails a run.
    """
    if pricing is None:
        return None
    try:
        classes = (
            (usage.prompt_tokens - usage.cache_read_tokens - usage.cache_write_tokens, pricing.input),
            (usage.cache_read_tokens, pricing.cache_read),
            (usage.cache_write_tokens, pricing.cache_write),
            (usage.completion_tokens, pricing.output),
        )
        if any(count != 0 and price is None for count, price in classes):
            return None
        with localcontext(_COST_CONTEXT):
            total = sum(
                (Decimal(count) * price for count, price in classes if price is not None and count > 0),
                Decimal(0),
            )
        return total if total.is_finite() and total >= 0 else None
    except Exception:  # noqa: BLE001 - total by intent, see the docstring
        return None


def add_costs(total: Decimal | None, call: Decimal | None) -> Decimal | None:
    """A run's cost so far plus one call's: unknown if either is (FR-30). Total."""
    if total is None or call is None:
        return None
    try:
        with localcontext(_COST_CONTEXT):
            summed = total + call
        return summed if summed.is_finite() else None
    except Exception:  # noqa: BLE001 - total by intent
        return None


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
    gateway before being listed here. Neither carries a price (see the module
    docstring).
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
