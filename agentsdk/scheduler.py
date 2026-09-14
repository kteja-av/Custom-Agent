"""Concurrency limits (FR-43, FR-44, FR-46, ADR-30).

Budget answers "can we afford this"; `SchedulerLimits` answers "is it safe to
run this many things at once" (design v0.3), and a run can be affordable and
still unsafe to fan out unbounded. M11 adds the three limits Phase 2's first
increment needs. The fields the design names for the orchestrator
(max_concurrent_subagents, max_tasks_per_run, queue_policy) arrive with the
second increment and are deliberately not reserved here.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import weakref
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .primitives import unstorable_reason

# P2-D5: four tool calls of one run at once, and no per-tool or per-provider
# limit unless one is set.
DEFAULT_MAX_CONCURRENT_TOOLS = 4

# The manifest records every limit (migration 0004), and a limit is a count like
# max_turns: refused where an INTEGER would be, so a value cannot complete in
# memory and fail on Postgres (M5 round 8).
_CEILING = 2**31 - 1


def _count(value: Any, name: str) -> int:
    # Before the range check: a bool passes every range check (True >= 1).
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    if not 1 <= value <= _CEILING:
        raise ValueError(f"{name} must be between 1 and {_CEILING}, got {value}")
    return value


def _limits_by_name(value: Any, name: str) -> MappingProxyType:
    """A read-only copy of a name -> limit mapping, every key and value checked."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping of names to limits, got {type(value).__name__}")
    try:
        items = list(value.items())
    except Exception:  # noqa: BLE001 - a caller's mapping may raise anything
        raise ValueError(f"{name} could not be read as a mapping") from None
    copied: dict[str, int] = {}
    for key, limit in items:
        # Exact str: a subclass can compare, hash and render as something other
        # than what it holds, and these keys are matched against tool names and
        # client keys.
        if type(key) is not str:
            raise ValueError(f"{name} keys must be str, got a {type(key).__name__}")
        if not key:
            raise ValueError(f"{name} keys must not be empty")
        reason = unstorable_reason(key)
        if reason is not None:
            raise ValueError(f"{name} has a key that cannot be stored: {reason}")
        copied[key] = _count(limit, f"{name}[{key!r}]")
    return MappingProxyType(copied)


@dataclass(frozen=True)
class SchedulerLimits:
    """How many things may run at once (FR-43).

    `max_concurrent_tools` bounds one run's tool calls between steps 6 and 9;
    `tool_concurrency_limits` bounds the calls of one tool within a run; and
    `provider_concurrency_limits` bounds the model calls in flight for one model
    client key across every run of one Runner. Only a Runner may carry provider
    limits. The mappings are copied, so changing the dict passed in changes
    nothing, and cannot be changed through the instance.
    """

    max_concurrent_tools: int = DEFAULT_MAX_CONCURRENT_TOOLS
    tool_concurrency_limits: Mapping[str, int] = field(default_factory=dict)
    provider_concurrency_limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_concurrent_tools", _count(self.max_concurrent_tools, "max_concurrent_tools"))
        for name in ("tool_concurrency_limits", "provider_concurrency_limits"):
            object.__setattr__(self, name, _limits_by_name(getattr(self, name), name))

    # A read-only mapping cannot be hashed, so the generated hash would raise.
    def __hash__(self) -> int:
        return hash(
            (
                self.max_concurrent_tools,
                tuple(sorted(self.tool_concurrency_limits.items())),
                tuple(sorted(self.provider_concurrency_limits.items())),
            )
        )

    def to_json(self) -> dict[str, Any]:
        """As the manifest records it (migration 0004)."""
        return {
            "max_concurrent_tools": self.max_concurrent_tools,
            "tool_concurrency_limits": dict(sorted(self.tool_concurrency_limits.items())),
            "provider_concurrency_limits": dict(sorted(self.provider_concurrency_limits.items())),
        }


class RunSlots:
    """One run's tool-call slots (FR-44).

    A call takes its tool's slot first and then the run's. Waiting for the run's
    slot it holds only a slot that calls of its own tool would need, never one a
    call of another tool could use; and every call holding a tool slot either
    has the run slot or is next in line for it, so the order cannot deadlock.
    """

    def __init__(self, limits: SchedulerLimits) -> None:
        self._run = asyncio.Semaphore(limits.max_concurrent_tools)
        self._tool_limits = limits.tool_concurrency_limits
        self._tools: dict[str, asyncio.Semaphore] = {}

    @contextlib.asynccontextmanager
    async def slot(self, tool_name: str) -> AsyncIterator[None]:
        limit = self._tool_limits.get(tool_name) if isinstance(tool_name, str) else None
        tool = None
        if limit is not None:
            tool = self._tools.get(tool_name)
            if tool is None:
                tool = self._tools[tool_name] = asyncio.Semaphore(limit)
        async with tool if tool is not None else contextlib.nullcontext():
            async with self._run:
                yield


class ProviderSlots:
    """provider_concurrency_limits, shared by every run of one Runner (FR-46).

    asyncio primitives belong to one event loop, and a Runner can be driven from
    more than one over its life (a script that calls asyncio.run twice, a test
    suite), so the semaphores are kept per loop. Runs on different loops could
    not wait on each other anyway.
    """

    def __init__(self, limits: Mapping[str, int]) -> None:
        self._limits = dict(limits)
        self._by_loop: weakref.WeakKeyDictionary[Any, dict[str, asyncio.Semaphore]] = weakref.WeakKeyDictionary()
        self._lock = threading.Lock()

    def slot(self, client_key: str) -> Any:
        """An async context manager holding a slot for `client_key`, or none."""
        limit = self._limits.get(client_key)
        if limit is None:
            return contextlib.nullcontext()
        loop = asyncio.get_running_loop()
        with self._lock:
            semaphores = self._by_loop.get(loop)
            if semaphores is None:
                semaphores = self._by_loop[loop] = {}
            semaphore = semaphores.get(client_key)
            if semaphore is None:
                semaphore = semaphores[client_key] = asyncio.Semaphore(limit)
        return semaphore
