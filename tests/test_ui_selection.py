"""Dropping one pick must not mean dropping all of them.

Selections bank additively — that is deliberate, and it is what stopped picks
made under one search term vanishing when the filter changed. But for a long
time the only inverse was "Clear report selections", which dropped every
report. Noticing that the seventh of twelve was wrong meant redoing all
twelve, and nobody was going to be standing there to say so.
"""

from __future__ import annotations

import pathlib
import time

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.ui.state import STATE_KEY, WizardState

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _StubTarget:
    tenant = "stub_tenant"


class _StubConnection:
    """No ``.service`` — reaching for one would be a tenant call."""

    target = _StubTarget()


def _report_index(*names):
    from wdmigrator.api import Index, ReportSummary

    summaries, payloads = {}, {}
    for i, name in enumerate(names):
        wid = f"W{i}"
        summaries[wid] = ReportSummary(wid=wid, custom_report_id=name, name=name, owner="x")
        payloads[wid] = {"Tenanted_Report_Definition_Data": {"Name": name}}
    return Index(
        kind="report", tenant="stub_tenant", fetched_at=time.time(),
        summaries=summaries, payloads=payloads,
    )


def _dashboard_index(*names):
    from wdmigrator.api import DashboardSummary, Index

    summaries, payloads = {}, {}
    for i, name in enumerate(names):
        wid = f"D{i}"
        summaries[wid] = DashboardSummary(
            wid=wid, reference_id=name, name=name, tabbed=True, worklet_count=1
        )
        payloads[wid] = {"name": name}
    return Index(
        kind="dashboard", tenant="stub_tenant", fetched_at=time.time(),
        summaries=summaries, payloads=payloads,
    )


def _app(**state_kwargs):
    state_kwargs.setdefault("object_kinds", ["reports"])
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    state = WizardState(step="select", **state_kwargs)
    state.source.connection = _StubConnection()
    state.dest.connection = _StubConnection()
    at.session_state[STATE_KEY] = state
    at.run(timeout=20)
    return at


class TestReports:
    def _three(self):
        index = _report_index("Alpha", "Beta", "Gamma")
        return _app(
            report_index=index,
            selected_reports_added={w: index.payload(w) for w in ("W0", "W1", "W2")},
        )

    def test_every_pick_gets_its_own_remove_control(self):
        at = self._three()
        assert not at.exception
        keys = {b.key for b in at.button}
        assert {"report_rm_W0", "report_rm_W1", "report_rm_W2"} <= keys

    def test_removing_one_leaves_the_others_selected(self):
        at = self._three()
        [b for b in at.button if b.key == "report_rm_W1"][0].click().run(timeout=20)
        assert not at.exception
        state = at.session_state[STATE_KEY]
        assert set(state.selected_reports) == {"W0", "W2"}
        assert set(state.selected_reports_added) == {"W0", "W2"}

    def test_removing_one_does_not_re_add_it_from_the_table(self):
        """The picker banks highlighted rows on every rerun. If removal did not
        also drop the row from the banked store, the next rerun would put it
        straight back and Remove would look broken."""
        at = self._three()
        [b for b in at.button if b.key == "report_rm_W0"][0].click().run(timeout=20)
        at.run(timeout=20)
        state = at.session_state[STATE_KEY]
        assert "W0" not in state.selected_reports

    def test_clear_all_still_exists_for_starting_over(self):
        at = self._three()
        clear = [b for b in at.button if b.key == "report_clear"]
        assert clear
        clear[0].click().run(timeout=20)
        assert not at.session_state[STATE_KEY].selected_reports

    def test_removing_a_pick_invalidates_the_plan_built_from_it(self):
        """A closure resolved against three reports must not outlive dropping
        one of them."""
        from wdmigrator.api import Closure

        index = _report_index("Alpha", "Beta")
        at = _app(
            report_index=index,
            selected_reports_added={w: index.payload(w) for w in ("W0", "W1")},
            closure=Closure(),
            dry_run_reviewed=True,
        )
        [b for b in at.button if b.key == "report_rm_W0"][0].click().run(timeout=20)
        state = at.session_state[STATE_KEY]
        assert state.closure is None
        assert state.dry_run_reviewed is False


class TestDashboards:
    def test_removing_one_dashboard_leaves_the_rest(self):
        index = _dashboard_index("Alpha", "Beta")
        at = _app(
            object_kinds=["dashboards"],
            dashboard_index=index,
            selected_dashboards_added={w: index.payload(w) for w in ("D0", "D1")},
        )
        assert not at.exception
        [b for b in at.button if b.key == "dashboard_rm_D0"][0].click().run(timeout=20)
        state = at.session_state[STATE_KEY]
        assert set(state.selected_dashboards) == {"D1"}


class TestCalculatedFields:
    def test_picked_fields_are_listed_by_name_and_individually_removable(self):
        """The calculated-field picker used to show a bare count — you could
        not even see what you had picked, let alone drop one."""
        from wdmigrator.api import CalculatedFieldSummary, Index

        index = Index(
            kind="calculated_field",
            tenant="stub_tenant",
            fetched_at=time.time(),
            summaries={
                "C0": CalculatedFieldSummary(wid="C0", reference_id="a", name="Age Band", class_name="Date"),
                "C1": CalculatedFieldSummary(wid="C1", reference_id="b", name="Tenure", class_name="Date"),
            },
        )
        at = _app(
            object_kinds=["calculated_fields"],
            cf_index=index,
            selected_field_wids={"C0", "C1"},
        )
        assert not at.exception
        rendered = " ".join(str(w.value) for w in at.markdown)
        assert "Age Band" in rendered and "Tenure" in rendered
        [b for b in at.button if b.key == "cf_rm_C0"][0].click().run(timeout=20)
        assert at.session_state[STATE_KEY].selected_field_wids == {"C1"}
