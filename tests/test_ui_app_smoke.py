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

    def test_adding_is_an_explicit_button_not_a_read_of_the_live_table(self):
        at = _select_step_app(report_index=_report_index("Alpha", "Beta"))
        assert not at.exception
        assert [b for b in at.button if b.key == "report_add"], (
            "no 'Add selected reports' button — selections would again be derived "
            "from the table's transient row positions"
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

    def test_adding_is_an_explicit_button_not_a_read_of_the_live_table(self):
        at = self._app(dashboard_index=_dashboard_index("Alpha", "Beta"))
        assert not at.exception
        assert [b for b in at.button if b.key == "dashboard_add"], (
            "no 'Add selected dashboards' button — selections would again be "
            "derived from the table's transient row positions"
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
