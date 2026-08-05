"""Chunked draining of engine generators under Streamlit's rerun model.

Streamlit has no background threads in the normal case: a script reruns
top-to-bottom on every interaction, and the only thing that survives between
reruns is ``st.session_state``. Every long-running engine call in
``wdmigrator.api`` is a generator for exactly this reason — it can be stored
in session state *mid-iteration* and advanced a little further on each
rerun, which is what makes a progress bar (and a working Cancel button)
possible without threads.

A live generator object is fine to keep in ``st.session_state``: Streamlit
keeps session state in memory in the same process for the life of the
session, it does not pickle it, so there's no serialization concern.

Do not build a ``JobState`` before an interaction that should not start
network calls yet — construction is cheap, but calling :func:`pump` for the
first time is what starts draining the generator, which is what actually
issues SOAP calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class JobState:
    """Progress and control state for one chunked generator drain.

    ``events`` accumulates every item the generator has yielded so far — the
    step UI reads ``events[-1]`` for "what's happening right now" and the
    full list for a running log. ``result`` is the generator's return value
    (via ``StopIteration.value``), if it has one.
    """

    generator: Iterator[Any] | None = None
    events: list[Any] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    error: BaseException | None = None
    result: Any = None

    @property
    def running(self) -> bool:
        return self.generator is not None and not self.done and not self.cancelled

    @property
    def last_event(self) -> Any | None:
        return self.events[-1] if self.events else None

    def cancel(self) -> None:
        self.cancelled = True


def start_job(generator: Iterator[Any]) -> JobState:
    """Wrap a freshly-built generator. Does not advance it."""
    return JobState(generator=generator)


def pump(
    job: JobState,
    *,
    time_budget: float = 0.8,
    batch_size: int | None = None,
) -> None:
    """Advance ``job`` for up to ``time_budget`` seconds or ``batch_size`` items.

    Call this once per Streamlit rerun, then render from ``job.events``/
    ``job.last_event`` and — if ``job.running`` is still true — call
    ``st.rerun()`` to schedule the next chunk. Pause/Cancel only need to flip
    ``job.cancelled``; because this function returns control to the caller
    between every single item, a cancel always lands *between* objects, never
    mid-write.

    ``batch_size=1`` is the writer's contract for live execution: a refresh
    or cancel between reruns can never leave an object half-written, because
    at most one item is pulled from the generator per rerun regardless of how
    much of the time budget remains.
    """
    if not job.running:
        return

    start = time.monotonic()
    pulled = 0
    while True:
        if batch_size is not None and pulled >= batch_size:
            return
        if time.monotonic() - start >= time_budget:
            return
        try:
            event = next(job.generator)  # type: ignore[arg-type]
        except StopIteration as stop:
            job.done = True
            job.result = stop.value
            return
        except BaseException as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
            job.error = exc
            job.done = True
            return
        job.events.append(event)
        pulled += 1
