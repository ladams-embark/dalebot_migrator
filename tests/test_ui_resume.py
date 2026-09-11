"""Resuming a session, and the one case where resuming must throw work away.

A restored selection is a list of source WIDs. WIDs are tenant-local, so a
session saved against one source and then resumed against another is not a
partial match — every ID in it refers to nothing, or to something unrelated.
Carrying it forward would let a user migrate a selection they never made.
"""

from __future__ import annotations

import pathlib
import time

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.api import Capabilities, ConnectionStatus, Role
from wdmigrator.config.targets import target_from_parts
from wdmigrator.ui import session_store
from wdmigrator.ui.state import WizardState
from wdmigrator.ui.steps import connect as connect_step

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _StubConnection:
    pass


@pytest.fixture
def stub_connect(monkeypatch):
    """Make ``_attempt_connect`` succeed without touching a tenant."""
    monkeypatch.setattr(connect_step, "connect", lambda *a, **k: _StubConnection())
    monkeypatch.setattr(
        connect_step,
        "verify_connection",
        lambda _c: ConnectionStatus(
            ok=True, detail="ok", fingerprint="fp", checked_at=time.time()
        ),
    )
    monkeypatch.setattr(
        connect_step,
        "probe_capabilities",
        lambda _c: Capabilities(implementer=True, detail="ok"),
    )
    monkeypatch.setattr(connect_step, "install_redacting_log_filter", lambda *a, **k: None)


def _state_resumed_from(tenant: str) -> WizardState:
    state = WizardState()
    state.restored_source_tenant = tenant
    state.selected_reports_added = {"W0": {}}
    state.selected_reports = {"W0": {}}
    state.object_kinds = ["reports"]
    return state


def _connect_source(state: WizardState, tenant: str) -> None:
    state.source.target_raw = (
        f"https://impl-services1.wd12.myworkday.com/ccx/service/{tenant}/x/v46.0"
    )
    state.source.username = "wd-implementer"
    state.source.password = "x"
    connect_step._attempt_connect(state, state.source, Role.SOURCE, "Source")


def _run_in_app(fn):
    """``_attempt_connect`` renders banners, so it needs a script run."""
    at = AppTest.from_string(
        "import streamlit as st\n"
        "st.session_state['fn']()\n"
    )
    at.session_state["fn"] = fn
    at.run(timeout=15)
    assert not at.exception, at.exception
    return at


class TestSourceTenantMismatch:
    def test_reconnecting_to_the_same_source_keeps_the_selection(self, stub_connect):
        state = _state_resumed_from("src_tenant")
        _run_in_app(lambda: _connect_source(state, "src_tenant"))
        assert set(state.selected_reports_added) == {"W0"}
        assert state.restored_source_tenant == "", "the pin is spent either way"

    def test_connecting_to_a_different_source_discards_the_selection(self, stub_connect):
        state = _state_resumed_from("src_tenant")
        at = _run_in_app(lambda: _connect_source(state, "other_tenant"))
        assert state.selected_reports_added == {}
        assert state.selected_reports == {}
        rendered = " ".join(str(m.value) for m in at.markdown)
        assert "Resumed selection discarded" in rendered
        assert "src_tenant" in rendered and "other_tenant" in rendered

    def test_a_normal_first_connection_is_unaffected(self, stub_connect):
        """No resume happened, so nothing is pinned and nothing is discarded."""
        state = WizardState()
        state.selected_reports_added = {"W0": {}}
        _run_in_app(lambda: _connect_source(state, "src_tenant"))
        assert set(state.selected_reports_added) == {"W0"}


def _connect_app(session_dir, **state_kwargs):
    from wdmigrator.ui.state import STATE_KEY

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.session_state[STATE_KEY] = WizardState(step="connect", **state_kwargs)
    at.run(timeout=20)
    return at


class TestConnectStepUI:
    @pytest.fixture(autouse=True)
    def _sessions_in_tmp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_store, "SESSION_DIR", tmp_path)

    def test_no_resume_control_when_nothing_has_been_saved(self, tmp_path):
        at = _connect_app(tmp_path)
        assert not at.exception
        assert not [b for b in at.button if b.key == "session_resume"]

    def test_a_saved_session_is_offered(self, tmp_path):
        saved = WizardState()
        saved.source.target = target_from_parts("h.example.com", "src_tenant")
        saved.dest.target = target_from_parts("h.example.com", "dst_tenant")
        saved.selected_reports_added = {"W0": {}}
        session_store.save_session(saved, directory=tmp_path)

        at = _connect_app(tmp_path)
        assert not at.exception
        assert [b for b in at.button if b.key == "session_resume"]

    def test_resume_is_withheld_once_a_selection_exists(self, tmp_path):
        """Restoring over work in progress would be destructive, under a
        button that says "resume"."""
        saved = WizardState()
        saved.selected_reports_added = {"W0": {}}
        session_store.save_session(saved, directory=tmp_path)

        at = _connect_app(tmp_path, selected_reports_added={"W9": {}})
        assert not at.exception
        assert not [b for b in at.button if b.key == "session_resume"]

    def test_save_appears_only_once_something_is_picked(self, tmp_path):
        assert not [b for b in _connect_app(tmp_path).button if b.key == "session_save_connect"]
        at = _connect_app(tmp_path, selected_reports_added={"W0": {}})
        assert [b for b in at.button if b.key == "session_save_connect"]
