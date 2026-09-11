"""Client-side pacing for tenant calls.

Workday rate-limits at roughly 10 calls/sec per tenant. The migration flow is
call-heavy — a full calculated-field index, a WID probe per unclassified
reference, an existence probe per planned object — so pacing has to be built
in rather than bolted on after the first 429.

A simple minimum-interval limiter, not a token bucket: bursts are exactly what
we want to avoid, and every caller here is a sequential loop.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: Under Workday's ~10/sec ceiling, with headroom for clock jitter and for
#: other integrations hitting the same tenant concurrently.
DEFAULT_CALLS_PER_SECOND = 8.0


@dataclass
class RateLimiter:
    """Blocks until at least ``1 / calls_per_second`` has passed since the last call.

    Thread-safe, because a hosted app shares one of these between every
    session pointed at the same tenant — see :func:`shared_limiter`. The lock
    is held across the sleep on purpose: that is what turns N callers into one
    paced stream rather than N independent ones that each think they are
    within budget.
    """

    calls_per_second: float = DEFAULT_CALLS_PER_SECOND
    _last_call: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

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

        with self._lock:
            elapsed = time.monotonic() - self._last_call
            slept = 0.0
            if self._last_call and elapsed < interval:
                slept = interval - elapsed
                time.sleep(slept)

            self._last_call = time.monotonic()
        return slept


#: One limiter per tenant for the whole process. Workday's ceiling is a
#: property of the tenant, not of whoever is calling it, so a limiter scoped
#: to a single connection only enforces the budget when exactly one person is
#: working. Hosted, several consultants each got their own 8 calls/sec against
#: a ~10/sec ceiling, so two of them on the same tenant was already over and
#: the resulting 429s hit whichever request happened to arrive next — quite
#: possibly the one in the middle of a write.
_shared: dict[str, RateLimiter] = {}
_registry_lock = threading.Lock()


def shared_limiter(key: str, *, calls_per_second: float | None = None) -> RateLimiter:
    """The process-wide limiter for ``key``, creating it on first use.

    ``key`` identifies a tenant. Two connections to the same tenant — a source
    and a destination pointed at it, a Core and a Time Tracking client, or two
    different people's sessions — must share one, because Workday counts them
    together.

    A ``calls_per_second`` on a later call does not re-pace an existing
    limiter: the first caller's budget wins, so one session cannot widen the
    tenant's rate for everybody else.
    """
    with _registry_lock:
        limiter = _shared.get(key)
        if limiter is None:
            limiter = RateLimiter(
                calls_per_second=(
                    DEFAULT_CALLS_PER_SECOND if calls_per_second is None else calls_per_second
                )
            )
            _shared[key] = limiter
        return limiter


def reset_shared_limiters() -> None:
    """Empty the registry. For tests; nothing in the app needs it."""
    with _registry_lock:
        _shared.clear()
