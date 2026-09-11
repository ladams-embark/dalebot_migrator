"""Invalidating a reviewed dry run is correct. Doing it silently is not.

``reset_downstream`` is what keeps the wizard honest: a review is a review of
one exact plan, and ``safety.py`` will not unlock a live run against a plan
hash the review did not cover. But the user who went back to Select, dropped
one report of twelve, and returned to Run had no way to know their attestation
had been thrown away — the tick was simply unticked, on a page they were not
looking at.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.ui.state import STATE_KEY, WizardState, reset_downstream

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _reviewed_state(**kwargs) -> WizardState:
    state = WizardState(step="run", **kwargs)
    state.selected_reports_added = {"W0": {}, "W1": {}}
    state.selected_reports = dict(state.selected_reports_added)
    state.dry_run_records = [object()]
    state.dry_run_reviewed = True
    state.dry_run_plan_hash = "abc"
    state.confirmed_tenant_name = "dst_tenant"
    state.irreversible_ack = True
    return state


class TestWhatTriggersIt:
    def test_losing_a_review_is_announced(self):
        state = _reviewed_state()
        reset_downstream(state, from_step="plan")
        assert "your confirmation that you had reviewed it" in state.discarded_notice
        assert "the dry run" in state.discarded_notice
        assert "the Run step acknowledgements" in state.discarded_notice

    def test_losing_a_selection_is_announced(self):
        state = _reviewed_state()
        reset_downstream(state, from_step="select")
        assert "the selection of 2 object(s)" in state.discarded_notice

    def test_the_notice_explains_why_rather_than_only_what(self):
        """"Your approval is gone" invites the question this answers."""
        state = _reviewed_state()
        reset_downstream(state, from_step="plan")
        assert "review of one exact plan" in state.discarded_notice

    def test_overrides_and_reference_decisions_count(self):
        state = WizardState(step="plan")
        state.action_overrides = {"n1": object()}
        state.reference_decisions = {"R0": object()}
        reset_downstream(state, from_step="select")
        assert "1 CREATE/SKIP override(s)" in state.discarded_notice
        assert "1 reference decision(s)" in state.discarded_notice


class TestWhatDoesNotTriggerIt:
    def test_a_first_pick_says_nothing(self):
        """Select calls reset_downstream on every single pick. A banner there
        would appear before there was anything to lose and train the user to
        stop reading it."""
        state = WizardState(step="select")
        reset_downstream(state, from_step="plan")
        assert state.discarded_notice == ""

    def test_rebuilding_an_index_alone_says_nothing(self):
        """Indexes and closures re-derive themselves. Only work that has to be
        redone by hand is worth interrupting for."""
        from wdmigrator.api import Closure

        state = WizardState(step="plan")
        state.closure = Closure()
        reset_downstream(state, from_step="plan")
        assert state.discarded_notice == ""


class TestRendering:
    def _app(self, state: WizardState):
        at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        at.session_state[STATE_KEY] = state
        at.run(timeout=20)
        return at

    def test_the_notice_reaches_the_page(self):
        state = WizardState(step="connect")
        state.discarded_notice = "That change cleared the dry run."
        at = self._app(state)
        assert not at.exception
        rendered = " ".join(str(m.value) for m in at.markdown)
        assert "Downstream work was cleared" in rendered
        assert "That change cleared the dry run." in rendered

    def test_it_is_shown_once_and_then_cleared(self):
        """It describes an event, not a condition. Leaving it up would have it
        still on screen several steps later, describing nothing current."""
        state = WizardState(step="connect")
        state.discarded_notice = "That change cleared the dry run."
        at = self._app(state)
        assert at.session_state[STATE_KEY].discarded_notice == ""

        at.run(timeout=20)
        assert "Downstream work was cleared" not in " ".join(
            str(m.value) for m in at.markdown
        )
