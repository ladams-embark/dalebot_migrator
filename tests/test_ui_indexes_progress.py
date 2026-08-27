"""Progress-and-ETA math for the bulk index build bar.

Pure-function tests only — no Streamlit rerun, no tenant. The rendering path
that calls these (``bulk_build_indexes``) is exercised indirectly by
``test_ui_app_smoke.py``; this file is about the arithmetic being right, not
the widgets.
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from wdmigrator.ui.indexes import (
    BUILD_ESTIMATE_SECONDS,
    IndexSpec,
    _DEFAULT_ESTIMATE_SECONDS,
    _StageEvent,
    _estimate_remaining_seconds,
    _format_duration,
    _progress_detail,
)


def _spec(kind: str) -> IndexSpec:
    return IndexSpec(kind=kind, label=kind, iterator_fn=lambda c: iter(()),
                      connection=None, index_attr=f"{kind}_index")


class TestFormatDuration:
    def test_a_short_wait_reads_as_a_few_seconds_not_zero(self):
        assert _format_duration(0) == "a few seconds"
        assert _format_duration(2) == "a few seconds"

    def test_sub_minute_rounds_to_the_nearest_five_seconds(self):
        assert _format_duration(23) == "about 25s"
        assert _format_duration(57) == "about 55s"

    def test_never_rounds_up_into_the_next_units_territory(self):
        # 57-59s must not read "about 1 min" - that overstates the wait.
        assert _format_duration(59) == "about 1 min"
        assert _format_duration(58) != "about 60s"

    def test_minutes_round_to_the_nearest_whole_minute(self):
        assert _format_duration(150) == "about 3 min"
        assert _format_duration(65) == "about 1 min"

    def test_never_negative_even_if_the_estimate_overshoots(self):
        assert _format_duration(-5) == "a few seconds"


class TestEstimateRemainingSeconds:
    def test_trusts_live_progress_once_a_stage_is_meaningfully_underway(self):
        # 20% through in 5s of a report sweep implies ~20s left on this stage.
        event = _StageEvent(
            stage=1, total_stages=1, label="Report", kind="report",
            fraction=0.2, fetched=1000, total=5000, elapsed=5.0,
        )
        assert _estimate_remaining_seconds(event, []) == pytest.approx(20.0)

    def test_falls_back_to_the_flat_estimate_before_progress_is_meaningful(self):
        # fraction just above zero: dividing by it would be wild, so this
        # should use the flat per-kind estimate instead of a live projection.
        event = _StageEvent(
            stage=1, total_stages=1, label="Calculated field",
            kind="calculated_field", fraction=0.001, fetched=1, total=9650,
            elapsed=0.1,
        )
        assert _estimate_remaining_seconds(event, []) == BUILD_ESTIMATE_SECONDS["calculated_field"]

    def test_unknown_kinds_fall_back_to_the_default_estimate(self):
        event = _StageEvent(
            stage=1, total_stages=1, label="Gauge range", kind="gauge_range",
            fraction=0.0, fetched=0, total=0, elapsed=0.0,
        )
        assert _estimate_remaining_seconds(event, []) == _DEFAULT_ESTIMATE_SECONDS

    def test_adds_a_flat_estimate_for_every_stage_still_queued(self):
        event = _StageEvent(
            stage=1, total_stages=2, label="Calculated field",
            kind="calculated_field", fraction=0.2, fetched=2000, total=10000,
            elapsed=5.0,
        )
        # live remainder of stage 1 (20s) + flat estimate for the queued report stage.
        expected = 20.0 + BUILD_ESTIMATE_SECONDS["report"]
        assert _estimate_remaining_seconds(event, [_spec("report")]) == pytest.approx(expected)


class TestProgressDetail:
    def test_includes_item_counts_when_a_total_is_known(self):
        event = _StageEvent(
            stage=1, total_stages=1, label="Calculated field",
            kind="calculated_field", fraction=0.5, fetched=4825, total=9650,
            elapsed=10.0,
        )
        detail = _progress_detail(event, [])
        assert "4,825 / 9,650 fetched" in detail

    def test_always_names_a_remaining_time_even_with_no_item_counts(self):
        # A gated-skip event carries no IndexProgress at all (total=0), which
        # used to mean the bar showed nothing but the label repeated.
        event = _StageEvent(
            stage=1, total_stages=1, label="Dashboard", kind="dashboard",
            fraction=1.0, fetched=0, total=0, elapsed=0.0,
        )
        detail = _progress_detail(event, [])
        assert "remaining" in detail

    def test_does_not_count_the_current_stage_as_also_queued(self):
        specs = [_spec("calculated_field"), _spec("report")]
        event = _StageEvent(
            stage=1, total_stages=2, label="Calculated field",
            kind="calculated_field", fraction=0.5, fetched=4825, total=9650,
            elapsed=10.0,
        )
        detail = _progress_detail(event, specs)
        # Only the report stage's flat estimate should be added on top of the
        # live projection for stage 1 - not a second helping of stage 1 itself.
        expected_remaining = 10.0 + BUILD_ESTIMATE_SECONDS["report"]
        assert _format_duration(expected_remaining) in detail
