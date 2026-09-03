"""Smoke test: the wizard boots and renders Connect with no exceptions.

Entirely offline — with no credentials entered, nothing in the Connect step
makes a network call, so this is safe to run in the default `pytest`
collection alongside everything else.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_app_boots_to_connect_step_with_no_exceptions():
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.run(timeout=15)
    assert not at.exception


def test_connect_step_is_the_initial_step():
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.run(timeout=15)
    headers = [h.value for h in at.header]
    assert "Connect" in headers


def test_the_step_rail_is_five_visible_steps():
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.run(timeout=15)
    rendered = " ".join(str(w.value) for w in at.markdown)
    for title in ("Connect", "Select", "Plan", "Run", "Results"):
        assert title in rendered


def test_connect_does_not_prefill_credentials_but_keeps_quick_fill():
    """Users always type credentials. Quick fill still exists for dpt1."""
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.run(timeout=15)
    assert not at.exception
    user_fields = [w for w in at.text_input if getattr(w, "key", None) in {"src_user", "dst_user"}]
    assert user_fields, "username fields are missing"
    for field in user_fields:
        assert not field.value
    quick = [b for b in at.button if b.key in {"src_quick_fill", "dst_quick_fill"}]
    assert len(quick) == 2


class _StubTarget:
    tenant = "stub_tenant"


class _StubConnection:
    """Enough of a Connection for the Select step's index bookkeeping.

    No service attribute on purpose: touching one would be a tenant call, and
    reaching for it in a test is exactly the mistake worth failing loudly on.
    """

    target = _StubTarget()


def _select_step_app(**state_kwargs):
    from wdmigrator.ui.state import STATE_KEY, WizardState

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    state = WizardState(step="select", **state_kwargs)
    state.source.connection = _StubConnection()
    at.session_state[STATE_KEY] = state
    at.run(timeout=20)
    return at


def test_select_step_renders_with_the_default_object_kind():
    at = _select_step_app()
    assert not at.exception
    assert "Select" in [h.value for h in at.header]


def test_choosing_dashboards_renders_the_dashboard_section():
    """The dashboard picker and its two extra indexes only appear when asked
    for, so a user without an implementer account never sees a section they
    cannot use."""
    at = _select_step_app()
    kinds = [w for w in at.multiselect if w.key == "object_kinds"]
    assert kinds, "object kind chooser is missing"
    kinds[0].set_value(["reports", "dashboards"]).run(timeout=20)
    assert not at.exception
    assert any("Dashboard index" in str(w.value) for w in at.markdown)


def test_the_implementer_requirement_is_explained_rather_than_shown_as_an_error():
    at = _select_step_app(implementer_required=True)
    kinds = [w for w in at.multiselect if w.key == "object_kinds"]
    kinds[0].set_value(["dashboards"]).run(timeout=20)
    assert not at.exception
    rendered = " ".join(str(w.value) for w in at.markdown)
    assert "implementer account" in rendered


class TestConflictsRefusesToProbeUnmatched:
    """The Conflicts step has to sweep the destination before it probes it.

    A probe with no destination sweep answers "does the destination hold an
    object created the same way this one was", which is not the question — the
    same calculated field carries a different `Calculated_Field_ID` on each
    tenant. Acting on that answer plans a CREATE for something already present,
    and the write is rejected with "Enter a unique WQL alias for the business
    object". Renders offline: with nothing cached for the stub tenant, no
    button is pressed and no sweep starts.
    """

    def _app(self, ready: bool):
        import time

        from wdmigrator.api import Closure, Index
        from wdmigrator.ui.state import STATE_KEY, WizardState

        at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        state = WizardState(step="plan")
        state.source.connection = _StubConnection()
        state.dest.connection = _StubConnection()
        state.closure = Closure()
        if ready:
            for attr, kind in (
                ("dest_cf_index", "calculated_field"),
                ("dest_measure_index", "calculated_measure"),
            ):
                setattr(state, attr, Index(kind=kind, tenant="stub_tenant",
                                           fetched_at=time.time()))
        at.session_state[STATE_KEY] = state
        at.run(timeout=20)
        return at

    def _probe_button(self, at):
        found = [b for b in at.button if b.key == "conflicts_start"]
        assert found, "the existence-check button is missing"
        return found[0]

    def test_the_destination_sweeps_are_offered_before_the_probe(self):
        at = self._app(ready=False)
        assert not at.exception
        rendered = " ".join(str(w.value) for w in at.markdown)
        assert "Destination calculated field index" in rendered
        assert "Destination calculated measure index" in rendered

    def test_the_probe_button_is_disabled_until_both_sweeps_exist(self):
        at = self._app(ready=False)
        assert self._probe_button(at).disabled

    def test_the_probe_button_unlocks_once_both_sweeps_exist(self):
        at = self._app(ready=True)
        assert not at.exception
        assert not self._probe_button(at).disabled


def _report_index(*names):
    """A tiny in-memory report index, enough for the picker to render a table."""
    import time

    from wdmigrator.api import Index, ReportSummary

    summaries, payloads = {}, {}
    for i, name in enumerate(names):
        wid = f"W{i}"
        summaries[wid] = ReportSummary(
            wid=wid, custom_report_id=name, name=name, owner="someone"
        )
        payloads[wid] = {"Tenanted_Report_Definition_Data": {"Name": name}}
    return Index(
        kind="report",
        tenant="stub_tenant",
        fetched_at=time.time(),
        summaries=summaries,
        payloads=payloads,
    )


class TestReportSelectionSurvivesRefiltering:
    """Selecting reports across more than one search term has to accumulate.

    The picker used to rebuild ``selected_reports`` on every rerun from the
    index table's live selection. ``st.dataframe`` reports that selection as
    row *positions* into the frame it was handed, so retyping the filter
    pointed those positions at different reports — everything picked under the
    previous search silently vanished, and selecting two reports that no single
    search term matches was impossible.
    """

    def test_adding_banks_highlighted_rows_without_an_add_button(self):
        at = _select_step_app(report_index=_report_index("Alpha", "Beta"))
        assert not at.exception
        assert not [b for b in at.button if b.key == "report_add"], (
            "Add selected reports is gone — highlighting the table should bank "
            "the row, and Clear is how a pick is dropped"
        )

    def test_already_added_reports_survive_a_filter_that_matches_nothing(self):
        index = _report_index("Alpha", "Beta")
        at = _select_step_app(
            report_index=index,
            selected_reports_added={"W0": index.payload("W0")},
        )
        filters = [w for w in at.text_input if w.key == "report_filter"]
        assert filters, "report filter box is missing"
        filters[0].set_value("zzz-matches-nothing").run(timeout=20)

        assert not at.exception
        from wdmigrator.ui.state import STATE_KEY

        state = at.session_state[STATE_KEY]
        assert "W0" in state.selected_reports, (
            "a report added under an earlier search was dropped when the filter changed"
        )

    def test_reports_added_under_different_searches_accumulate(self):
        index = _report_index("Alpha", "Beta")
        at = _select_step_app(
            report_index=index,
            selected_reports_added={
                "W0": index.payload("W0"),
                "W1": index.payload("W1"),
            },
        )
        # "Alpha" and "Beta" share no substring, so no single filter can show
        # both — the whole point of accumulating rather than reading the table.
        filters = [w for w in at.text_input if w.key == "report_filter"]
        filters[0].set_value("Alpha").run(timeout=20)

        assert not at.exception
        from wdmigrator.ui.state import STATE_KEY

        state = at.session_state[STATE_KEY]
        assert set(state.selected_reports) == {"W0", "W1"}


def _dashboard_index(*names):
    import time

    from wdmigrator.api import DashboardSummary, Index

    summaries, payloads = {}, {}
    for i, name in enumerate(names):
        wid = f"D{i}"
        summaries[wid] = DashboardSummary(
            wid=wid, reference_id=name, name=name, tabbed=True, worklet_count=1
        )
        payloads[wid] = {"name": name}
    return Index(
        kind="dashboard",
        tenant="stub_tenant",
        fetched_at=time.time(),
        summaries=summaries,
        payloads=payloads,
    )


class TestDashboardSelectionSurvivesRefiltering:
    """The dashboard picker had the identical defect to the report picker —
    its selection was rebuilt each rerun from the filtered table's row
    positions, so filtering discarded whatever had been picked before."""

    def _app(self, **kwargs):
        at = _select_step_app(**kwargs)
        kinds = [w for w in at.multiselect if w.key == "object_kinds"]
        kinds[0].set_value(["dashboards"]).run(timeout=20)
        return at

    def test_adding_banks_highlighted_rows_without_an_add_button(self):
        at = self._app(dashboard_index=_dashboard_index("Alpha", "Beta"))
        assert not at.exception
        assert not [b for b in at.button if b.key == "dashboard_add"], (
            "Add selected dashboards is gone — highlighting the table should "
            "bank the row, and Clear is how a pick is dropped"
        )

    def test_dashboards_added_under_different_searches_accumulate(self):
        index = _dashboard_index("Alpha", "Beta")
        at = self._app(
            dashboard_index=index,
            selected_dashboards_added={
                "D0": index.payload("D0"),
                "D1": index.payload("D1"),
            },
        )
        filters = [w for w in at.text_input if w.key == "dashboard_filter"]
        assert filters, "dashboard filter box is missing"
        filters[0].set_value("Alpha").run(timeout=20)

        assert not at.exception
        from wdmigrator.ui.state import STATE_KEY

        state = at.session_state[STATE_KEY]
        assert set(state.selected_dashboards) == {"D0", "D1"}


def test_next_button_is_disabled_with_no_credentials_entered():
    # Keyed on "nav_next" rather than the button's label: the label is a
    # presentation choice that the Commit rebrand already changed once, but
    # the key is the wizard's actual gating contract.
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.run(timeout=15)
    next_buttons = [b for b in at.button if b.key == "nav_next"]
    assert next_buttons
    assert next_buttons[0].disabled


def test_plan_step_renders_the_probe_fallback_for_stubs():
    """AppTest stubs omit ``.service``, so Plan must not start a tenant call.
    The Check existence button stays as the offline fallback."""
    import time

    from wdmigrator.api import Closure, Index
    from wdmigrator.ui.state import STATE_KEY, WizardState

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    state = WizardState(step="plan")
    state.source.connection = _StubConnection()
    state.dest.connection = _StubConnection()
    state.closure = Closure()
    for attr, kind in (
        ("dest_cf_index", "calculated_field"),
        ("dest_measure_index", "calculated_measure"),
    ):
        setattr(state, attr, Index(kind=kind, tenant="stub_tenant", fetched_at=time.time()))
    at.session_state[STATE_KEY] = state
    at.run(timeout=20)
    assert not at.exception
    assert "Plan" in [h.value for h in at.header]
    found = [b for b in at.button if b.key == "conflicts_start"]
    assert found
    assert not found[0].disabled


def test_run_step_does_not_auto_start_live_execution():
    from wdmigrator.api import MigrationPlan, target_from_parts
    from wdmigrator.ui.state import STATE_KEY, WizardState

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    state = WizardState(step="run")
    state.plan = MigrationPlan()
    state.source.target = target_from_parts(
        "impl-services1.wd12.myworkday.com", "source_tenant"
    )
    state.dest.target = target_from_parts(
        "impl-services1.wd12.myworkday.com", "dest_tenant"
    )
    state.source.connection = _StubConnection()
    state.dest.connection = _StubConnection()
    at.session_state[STATE_KEY] = state
    at.run(timeout=20)
    assert not at.exception
    rendered = " ".join(str(w.value) for w in at.markdown)
    assert "Unexpected error" not in rendered
    assert "Run" in [h.value for h in at.header]
    start = [b for b in at.button if b.key == "execute_start"]
    assert start, "Start live execution is missing"
    assert start[0].disabled, "live Start must stay a click after the gate"
