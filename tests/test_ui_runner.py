"""Offline tests for the Streamlit pump — no tenant, no streamlit import.

The live-execute path uses ``batch_size=1`` so Pause/Cancel land between
Puts. SKIP records are not Puts; ``drain_skips`` exists so a plan of 430
skips and 10 writes does not spend 430 Streamlit reruns walking objects
that never touch the destination.
"""

from types import SimpleNamespace

from wdmigrator.ui.runner import _is_write_attempt, pump, start_job


def _event(action: str, name: str = "n", status: str | None = None) -> SimpleNamespace:
    if status is None:
        status = "skipped" if action == "skip" else "success"
    return SimpleNamespace(
        node=SimpleNamespace(name=name),
        record=SimpleNamespace(
            action=SimpleNamespace(value=action),
            status=SimpleNamespace(value=status),
        ),
    )


def test_batch_size_one_without_drain_stops_on_a_skip():
    """Today's contract, unchanged: one item per pump when drain_skips is off."""
    job = start_job(iter([_event("skip", "a"), _event("create", "b")]))
    pump(job, time_budget=10.0, batch_size=1)
    assert len(job.events) == 1
    assert job.events[0].node.name == "a"
    assert not job.done


def test_drain_skips_walks_skips_then_stops_after_one_write():
    skips = [_event("skip", f"s{i}") for i in range(20)]
    writes = [_event("create", "c1"), _event("update", "u1")]
    job = start_job(iter(skips + writes))
    pump(job, time_budget=10.0, batch_size=1, drain_skips=True)
    assert [e.node.name for e in job.events] == [f"s{i}" for i in range(20)] + ["c1"]
    assert not job.done

    pump(job, time_budget=10.0, batch_size=1, drain_skips=True)
    assert job.events[-1].node.name == "u1"
    assert job.done or len(job.events) == 22


def test_drain_skips_finishes_a_skip_only_plan_in_one_pump():
    job = start_job(iter(_event("skip", f"s{i}") for i in range(50)))
    pump(job, time_budget=10.0, batch_size=1, drain_skips=True)
    assert job.done
    assert len(job.events) == 50


def test_drain_skips_drains_not_attempted_after_a_halt():
    """Remaining CREATE nodes after stop_on_failure are NOT_ATTEMPTED — no Put."""
    events = [
        _event("skip", "s"),
        _event("create", "halted", status="not_attempted"),
        _event("create", "also_halted", status="not_attempted"),
    ]
    job = start_job(iter(events))
    pump(job, time_budget=10.0, batch_size=1, drain_skips=True)
    assert [e.node.name for e in job.events] == ["s", "halted", "also_halted"]
    assert job.done


def test_events_without_a_record_action_still_count_as_one():
    """Index/probe progress has no write action — keep one-per-batch."""
    job = start_job(iter([SimpleNamespace(page=1), SimpleNamespace(page=2)]))
    pump(job, time_budget=10.0, batch_size=1, drain_skips=True)
    assert len(job.events) == 1
    assert not job.done


def test_is_write_attempt_uses_status_when_present():
    assert _is_write_attempt(_event("create", status="success")) is True
    assert _is_write_attempt(_event("update", status="failed")) is True
    assert _is_write_attempt(_event("skip", status="skipped")) is False
    assert _is_write_attempt(_event("create", status="not_attempted")) is False
    assert _is_write_attempt(SimpleNamespace()) is True
