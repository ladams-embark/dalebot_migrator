"""Client-side pacing for tenant calls.

Workday rate-limits at roughly 10 calls/sec per tenant. The migration flow is
call-heavy — a full calculated-field index, a WID probe per unclassified
reference, an existence probe per planned object — so pacing has to be built
in rather than bolted on after the first 429.

A simple minimum-interval limiter, not a token bucket: bursts are exactly what
we want to avoid, and every caller here is a sequential loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: Under Workday's ~10/sec ceiling, with headroom for clock jitter and for
#: other integrations hitting the same tenant concurrently.
DEFAULT_CALLS_PER_SECOND = 8.0


@dataclass
class RateLimiter:
    """Blocks until at least ``1 / calls_per_second`` has passed since the last call."""

    calls_per_second: float = DEFAULT_CALLS_PER_SECOND
    _last_call: float = field(default=0.0, repr=False)

    @property
    def min_interval(self) -> float:
        if self.calls_per_second <= 0:
            return 0.0
        return 1.0 / self.calls_per_second

    def wait(self) -> float:
        """Sleep if needed. Returns how long we slept, for progress reporting."""
        interval = self.min_interval
        if interval <= 0:
            return 0.0

        elapsed = time.monotonic() - self._last_call
        slept = 0.0
        if self._last_call and elapsed < interval:
            slept = interval - elapsed
            time.sleep(slept)

        self._last_call = time.monotonic()
        return slept
