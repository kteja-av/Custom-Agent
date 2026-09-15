"""When a call started and how long it took, as events record them (FR-57).

Start times are wall-clock UTC in ISO-8601, so a reader can place them. Durations and
waits are measured with the monotonic performance counter, so a clock step cannot make
one negative or wrong.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def wall_clock() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ns() -> int:
    return time.perf_counter_ns()


def elapsed_ms(since_ns: int) -> float:
    return (time.perf_counter_ns() - since_ns) / 1_000_000
