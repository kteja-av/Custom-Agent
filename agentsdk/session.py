"""Conversation history (FR-9, LLD 3.9).

Phase 0 is insert-only: append and read back in order, no update, no delete.
The Postgres implementation lands in M5 behind this same protocol; the
in-memory one exists so the loop is testable without a database and so M5 is a
new implementation rather than a new interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .primitives import Message


@runtime_checkable
class SessionStore(Protocol):
    def append(self, run_id: str, message: Message) -> None: ...

    def history(self, run_id: str) -> list[Message]: ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._runs: dict[str, list[Message]] = {}

    def append(self, run_id: str, message: Message) -> None:
        self._runs.setdefault(run_id, []).append(message)

    def history(self, run_id: str) -> list[Message]:
        # A copy: callers must not be able to mutate stored history by holding
        # the list, which would make the append-only invariant a lie.
        return list(self._runs.get(run_id, ()))
