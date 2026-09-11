"""Blockers that clear themselves must not look like blockers that need a human.

The wizard's Continue button is gated by ``Blocker`` lists, and every one of
them used to render as a red banner with a bold **Fix:**. During a Select
sweep that put two red banners in front of a user who had nothing to fix and
nothing to do but wait twenty-five seconds. ``Blocker.waiting`` is what lets
those two cases look different; these tests are what keep them different.
"""

from __future__ import annotations

import pathlib
import time

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.api import Blocker, Index
from wdmigrator.ui.runner import JobState
from wdmigrator.ui.state import WizardState
from wdmigrator.ui.steps import conflicts as conflicts_step
from wdmigrator.ui.steps import execute as execute_step
from wdmigrator.ui.steps import select as select_step

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _never_finishes():
    while True:
        yield None


def _running_job() -> JobState:
    """A job that has been started and is not done — what ``pump`` leaves
    behind between reruns of a sweep in flight."""
    return JobState(generator=_never_finishes())


def _index(kind: str) -> Index:
    return Index(kind=kind, tenant="stub_tenant", fetched_at=time.time())


def _selected_reports_state(**kwargs) -> WizardState:
    state = WizardState(step="select", object_kinds=["reports"], **kwargs)
    state.selected_reports_added = {"W0": {"Tenanted_Report_Definition_Data": {"Name": "A"}}}
    state.selected_reports = dict(state.selected_reports_added)
    return state


class TestSelectGate:
    def test_missing_indexes_are_waiting_while_the_sweep_runs(self):
        state = _selected_reports_state()
        state.source_index_job = _running_job()
        blockers = select_step.gate(state)
        assert blockers, "missing indexes must still gate Continue"
        assert all(b.waiting for b in blockers), (
            "a sweep in flight is not something the user can fix"
        )

    def test_missing_indexes_are_real_blockers_once_no_sweep_is_running(self):
        """A cancelled or failed job clears ``source_index_job`` and leaves the
        indexes absent. Nothing will build them now, so this is a real one."""
        state = _selected_reports_state()
        state.source_index_job = None
        blockers = select_step.gate(state)
        assert blockers
        assert not any(b.waiting for b in blockers)
        assert any("Rebuild" in b.remedy for b in blockers)

    def test_nothing_selected_is_never_waiting(self):
        """The user is the only thing that can resolve this one."""
        state = WizardState(step="select", object_kinds=["reports"])
        state.source_index_job = _running_job()
        nothing_selected = [
            b for b in select_step.gate(state) if b.title == "Nothing selected"
        ]
        assert nothing_selected
        assert not nothing_selected[0].waiting

    @pytest.mark.parametrize(
        "kinds, expected",
        [
            (["reports"], "reports"),
            (["dashboards"], "dashboards"),
            (["calculated_fields"], "calculated fields"),
        ],
    )
    def test_the_calculated_field_blocker_names_what_was_actually_chosen(self, kinds, expected):
        """It used to say "even if you only selected reports" regardless, which
        on the dashboard path reads as the app having lost the user's answer."""
        state = WizardState(step="select", object_kinds=kinds)
        state.selected_field_wids = {"CF0"}
        state.selected_reports_added = {"W0": {}}
        state.selected_reports = {"W0": {}}
        state.selected_dashboards_added = {"D0": {}}
        state.selected_dashboards = {"D0": {}}
        cf = [
            b for b in select_step.gate(state)
            if b.title == "Calculated field index not built"
        ]
        assert cf, "the calculated field index always gates resolution"
        assert expected in cf[0].detail


class TestConflictsGate:
    def _state(self) -> WizardState:
        from wdmigrator.api import Closure

        state = WizardState(step="plan")
        state.closure = Closure()
        return state

    def test_destination_sweep_is_waiting_while_it_runs(self):
        state = self._state()
        state.dest_index_job = _running_job()
        blocker = conflicts_step.gate(state)[0]
        assert blocker.waiting
        assert blocker.title == "Reading the destination catalog"

    def test_destination_sweep_is_a_real_blocker_when_nothing_is_running(self):
        state = self._state()
        blocker = conflicts_step.gate(state)[0]
        assert not blocker.waiting
        assert "Build destination indexes" in blocker.remedy

    def test_the_existence_probe_is_waiting_while_it_runs(self):
        state = self._state()
        state.dest_cf_index = _index("calculated_field")
        state.dest_measure_index = _index("calculated_measure")
        state.existence_job = _running_job()
        blocker = conflicts_step.gate(state)[0]
        assert blocker.waiting
        assert blocker.title == "Checking the destination"


def test_a_live_run_in_progress_is_waiting_not_broken():
    state = WizardState(step="run")
    state.execute_job = _running_job()
    blocker = execute_step.gate(state)[0]
    assert blocker.waiting


_RENDER_SCRIPT = """
from wdmigrator.api import Blocker
from wdmigrator.ui import components

components.render_blocker(
    Blocker(None, "Sweeping", "Not finished yet.", "Sit tight.", waiting=True)
)
components.render_blocker(
    Blocker(None, "No implementer", "This account cannot read dashboards.", "Ask IT.")
)
"""


class TestRendering:
    """The flag has to reach the page, not just the dataclass."""

    def _rendered(self) -> str:
        at = AppTest.from_string(_RENDER_SCRIPT)
        at.run(timeout=15)
        assert not at.exception
        return " ".join(str(w.value) for w in at.markdown)

    def test_a_waiting_blocker_renders_neutral_and_never_says_fix(self):
        rendered = self._rendered()
        assert "cmt-banner--neutral" in rendered
        assert "<strong>Next:</strong> Sit tight." in rendered

    def test_a_real_blocker_still_renders_danger_and_says_fix(self):
        rendered = self._rendered()
        assert "cmt-banner--danger" in rendered
        assert "<strong>Fix:</strong> Ask IT." in rendered


def test_the_nav_bar_promotes_an_actionable_blocker_over_a_waiting_one():
    """``app.py`` shows one blocker inline and hides the rest in an expander.
    The promoted one must be the one the user can act on, not whichever the
    gate happened to append first."""
    from wdmigrator.ui.app import prioritise_blockers

    waiting = Blocker(None, "Sweeping", "d", "r", waiting=True)
    actionable = Blocker(None, "Nothing selected", "d", "r")
    assert prioritise_blockers([waiting, actionable])[0] is actionable
    assert prioritise_blockers([actionable, waiting])[0] is actionable
