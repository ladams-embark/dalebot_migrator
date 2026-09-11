"""Select has to open with the thing the user came here to do.

It used to open with up to thirteen index status rows across three sweep
sections — "Prompt field index: not built — takes a few seconds" and so on —
before a single control the user could touch. Those rows are diagnostics; they
belong under the pickers, not in front of them. What replaces them above the
fold is one line saying whether the catalogs are usable yet.
"""

from __future__ import annotations

import pathlib
import time

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.api import Index
from wdmigrator.ui.indexes import IndexSpec, pending_specs, preload_cached_indexes
from wdmigrator.ui.state import WizardState

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _StubTarget:
    tenant = "stub_tenant"


class _StubConnection:
    """No ``service`` attribute: reaching for one would be a tenant call."""

    target = _StubTarget()

    def for_service(self, _name):
        return self


def _select_app(**state_kwargs):
    from wdmigrator.ui.state import STATE_KEY

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    state_kwargs.setdefault("object_kinds", ["reports"])
    state = WizardState(step="select", **state_kwargs)
    state.source.connection = _StubConnection()
    state.dest.connection = _StubConnection()
    at.session_state[STATE_KEY] = state
    at.run(timeout=20)
    return at


def _spec(index_attr: str, *, gated: bool = False) -> IndexSpec:
    return IndexSpec(
        kind="calculated_field",
        label=index_attr,
        iterator_fn=lambda _c: iter(()),
        connection=_StubConnection(),
        index_attr=index_attr,
        implementer_gated=gated,
    )


def _index() -> Index:
    return Index(kind="calculated_field", tenant="stub_tenant", fetched_at=time.time())


class TestPickerComesFirst:
    def test_the_picker_renders_above_the_index_bookkeeping(self):
        """Position, not mere presence. Both were always on the page; the
        complaint was the order."""
        at = _select_app()
        captions = [str(c.value) for c in at.caption]
        assert any("Reports" in c or "Migrating" in c for c in captions)
        assert [e for e in at.expander if "Catalog details" in str(e.label)], (
            "the status rows must still exist, just not up front"
        )

    def test_the_status_rows_are_no_longer_loose_on_the_page(self):
        at = _select_app()
        # ``_summarise`` writes "**<label> index**: ..." rows. They are all
        # inside the expander now; none should be a direct child of the step.
        details = [e for e in at.expander if "Catalog details" in str(e.label)]
        assert details
        inside = " ".join(str(m.value) for m in details[0].markdown)
        assert "index**:" in inside


class TestHeadline:
    def _headline(self, state: WizardState, specs, *, running: bool) -> str:
        script = "\n".join(
            [
                "import streamlit as st",
                "from wdmigrator.ui.steps import select",
                "state, specs, running = st.session_state['args']",
                "select._catalog_headline(state, specs, running=running)",
            ]
        )
        at = AppTest.from_string(script)
        at.session_state["args"] = (state, specs, running)
        at.run(timeout=15)
        assert not at.exception
        return " ".join(str(c.value) for c in at.caption)

    def test_a_sweep_in_flight_says_so_and_invites_picking_anyway(self):
        state = WizardState(step="select")
        state.cf_index = _index()
        specs = [_spec("cf_index"), _spec("report_index")]
        line = self._headline(state, specs, running=True)
        assert "1 of 2 ready" in line
        assert "Start picking now" in line

    def test_everything_built_says_ready(self):
        state = WizardState(step="select")
        state.cf_index = _index()
        state.report_index = _index()
        specs = [_spec("cf_index"), _spec("report_index")]
        assert "All 2 catalogs ready" in self._headline(state, specs, running=False)

    def test_a_stalled_sweep_points_at_the_expander(self):
        """Nothing running and something missing means a click is needed, and
        the click is inside a collapsed expander. Say where."""
        state = WizardState(step="select")
        specs = [_spec("cf_index"), _spec("report_index")]
        line = self._headline(state, specs, running=False)
        assert "2 of 2 catalogs still to build" in line
        assert "Catalog details" in line

    def test_implementer_skipped_indexes_are_not_counted_as_outstanding(self):
        """A standard ISU can never build the dashboard index. Counting it as
        a missing catalog would leave the headline permanently incomplete on a
        run that is in fact finished."""
        state = WizardState(step="select", implementer_required=True)
        state.cf_index = _index()
        specs = [_spec("cf_index"), _spec("dashboard_index", gated=True)]
        assert "All 1 catalogs ready" in self._headline(state, specs, running=False)


class TestCachePreload:
    def test_caches_load_before_the_pickers_lay_themselves_out(self, tmp_path, monkeypatch):
        """The regression this ordering exists to prevent: a picker rendering
        above the sweep controls, before anything had read the disk cache,
        reporting "index not built" about an index already on disk."""
        specs = [_spec("cf_index")]
        state = WizardState(step="select")
        monkeypatch.setattr(
            "wdmigrator.ui.indexes.load_index", lambda *a, **k: _index()
        )
        monkeypatch.setattr("wdmigrator.ui.indexes.cache_path", lambda *a, **k: tmp_path)
        preload_cached_indexes(state, specs)
        assert state.cf_index is not None
        assert pending_specs(state, specs) == []

    def test_a_gated_spec_is_not_pending_once_the_account_is_known_to_lack_it(self):
        state = WizardState(step="select", implementer_required=True)
        assert pending_specs(state, [_spec("dashboard_index", gated=True)]) == []
