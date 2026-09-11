"""One rate budget per tenant, not per person.

Workday rate-limits at roughly 10 calls/sec *per tenant*. A limiter scoped to
a connection enforces that only while exactly one person is working. Hosted,
each consultant got their own 8 calls/sec, so two of them sweeping the same
tenant was already over the ceiling — and a 429 lands on whichever request
arrives next, which may well be somebody else's write rather than the sweep
that caused it.
"""

from __future__ import annotations

import threading
import time

import pytest

from wdmigrator.ratelimit import (
    DEFAULT_CALLS_PER_SECOND,
    RateLimiter,
    reset_shared_limiters,
    shared_limiter,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_shared_limiters()
    yield
    reset_shared_limiters()


class TestTheRegistry:
    def test_the_same_tenant_gets_the_same_limiter(self):
        assert shared_limiter("host|acme") is shared_limiter("host|acme")

    def test_different_tenants_do_not_share_a_budget(self):
        assert shared_limiter("host|acme") is not shared_limiter("host|other")

    def test_same_tenant_name_on_another_host_is_a_different_tenant(self):
        assert shared_limiter("a|acme") is not shared_limiter("b|acme")

    def test_a_later_caller_cannot_widen_the_budget(self):
        """Otherwise one session could raise the tenant's rate for everyone
        else by asking for a faster limiter."""
        first = shared_limiter("host|acme", calls_per_second=2.0)
        second = shared_limiter("host|acme", calls_per_second=1000.0)
        assert second is first
        assert first.calls_per_second == 2.0

    def test_the_default_is_under_workdays_ceiling(self):
        assert shared_limiter("host|acme").calls_per_second == DEFAULT_CALLS_PER_SECOND
        assert DEFAULT_CALLS_PER_SECOND < 10


class TestPacing:
    def test_a_single_caller_is_paced(self):
        limiter = RateLimiter(calls_per_second=50.0)
        limiter.wait()
        started = time.monotonic()
        limiter.wait()
        assert time.monotonic() - started >= 0.015

    def test_concurrent_callers_share_one_stream(self):
        """The real point. Eight threads against one limiter must come out at
        the limiter's rate, not eight times it.
        """
        limiter = RateLimiter(calls_per_second=50.0)
        threads = [threading.Thread(target=limiter.wait) for _ in range(8)]

        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - started

        # 8 calls at 50/sec is 7 intervals of 20ms once the first goes free.
        assert elapsed >= 7 * 0.02 * 0.8

    def test_unpaced_callers_would_not_have_waited(self):
        """Contrast, so the test above cannot pass by being slow for some
        unrelated reason: a limiter each means nobody waits."""
        limiters = [RateLimiter(calls_per_second=50.0) for _ in range(8)]
        threads = [threading.Thread(target=lim.wait) for lim in limiters]

        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert time.monotonic() - started < 7 * 0.02 * 0.8

    def test_wait_reports_how_long_it_slept(self):
        limiter = RateLimiter(calls_per_second=20.0)
        assert limiter.wait() == 0.0
        assert limiter.wait() > 0.0

    def test_an_unlimited_limiter_never_sleeps(self):
        assert RateLimiter(calls_per_second=0).wait() == 0.0
