"""Keeping consultants out of each other's saved work.

The app is hosted, which means one container, one filesystem, and several
people using it at once. Every artifact directory the wizard writes to was a
single shared namespace: the resume picker listed everyone's saved sessions
(tenants, usernames, full report payloads), ``KEEP_SESSIONS`` pruned across all
of them so one person's saves deleted another's, and reference maps keyed only
by destination tenant overwrote each other.

The index cache is deliberately *not* scoped and there is a test here saying
so, because it would be an easy thing to "fix" later and lose: it is keyed by
tenant, holds nothing personal, and a report sweep costs two and a half
minutes that two consultants on the same tenant should not each pay.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.ui import reference_maps, session_store
from wdmigrator.ui.state import WizardState

from conftest import TEST_WORKSPACE, pin_workspace

ROOT = pathlib.Path(__file__).resolve().parents[1]

_APP = (
    "import streamlit as st\n"
    "from wdmigrator.ui import workspace\n"
    "st.session_state['seen'] = workspace.workspace_id()\n"
    "st.write(workspace.shared_link())\n"
)


def _run(query=None):
    at = AppTest.from_string(_APP)
    if query:
        at.query_params["w"] = query
    at.run(timeout=15)
    assert not at.exception, at.exception
    return at


class TestTheIdentifier:
    def test_one_is_minted_when_there_is_none(self):
        at = _run()
        assert at.session_state["seen"]

    def test_it_is_published_to_the_url(self):
        """It has to survive a browser reload or scoping breaks the very
        feature it protects — nobody could see their own saved session."""
        at = _run()
        # AppTest surfaces query params as lists; a browser sends a string.
        published = at.query_params["w"]
        if isinstance(published, list):
            published = published[0]
        assert published == at.session_state["seen"]

    def test_an_existing_url_is_honoured(self):
        at = _run(query="abc123")
        assert at.session_state["seen"] == "abc123"

    def test_it_survives_a_rerun(self):
        at = _run()
        first = at.session_state["seen"]
        at.run(timeout=15)
        assert at.session_state["seen"] == first

    def test_two_visitors_get_different_ids(self):
        assert _run().session_state["seen"] != _run().session_state["seen"]

    def test_the_link_is_a_usable_query_string(self):
        at = _run(query="abc123")
        assert at.markdown[0].value == "?w=abc123"


class TestTheIdIsUntrustedInput:
    """It arrives in a query string and becomes a directory name."""

    @pytest.mark.parametrize(
        "hostile",
        ["../../etc", "..", "/etc/passwd", "a/b", "\\..\\..", "%2e%2e"],
    )
    def test_traversal_attempts_never_reach_the_filesystem(self, hostile):
        at = _run(query=hostile)
        seen = at.session_state["seen"]
        assert "/" not in seen and "\\" not in seen and ".." not in seen
        assert seen.isalnum()

    def test_an_overlong_id_is_truncated(self):
        at = _run(query="f" * 500)
        assert len(at.session_state["seen"]) == 16


class TestSessionsAreNotShared:
    @pytest.fixture(autouse=True)
    def _tmp_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_store, "SESSION_DIR", tmp_path)
        self.root = tmp_path

    def _connect_app(self, workspace):
        from wdmigrator.ui.state import STATE_KEY

        at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        at.query_params["w"] = workspace
        at.session_state[STATE_KEY] = WizardState(step="connect")
        at.run(timeout=20)
        assert not at.exception, at.exception
        return at

    def test_another_consultants_session_is_not_offered(self):
        """The label on that picker names both tenants. Listing a colleague's
        saved session discloses where they are migrating from and to."""
        theirs = WizardState()
        theirs.selected_reports_added = {"W0": {}}
        session_store.save_session(theirs, directory=self.root / "someone-else")

        at = self._connect_app("mine")
        assert not [b for b in at.button if b.key == "session_resume"]

    def test_my_own_session_is_offered(self):
        mine = WizardState()
        mine.selected_reports_added = {"W0": {}}
        session_store.save_session(mine, directory=self.root / "mine")

        at = self._connect_app("mine")
        assert [b for b in at.button if b.key == "session_resume"]

    def test_pruning_cannot_reach_another_workspace(self):
        """``KEEP_SESSIONS`` counts files in a directory. Unscoped, an active
        consultant silently deleted a colleague's saved work."""
        theirs = self.root / "someone-else"
        theirs.mkdir(parents=True)
        (theirs / "20260101T000000Z-keep.json").write_text("{}", encoding="utf-8")

        mine = self.root / "mine"
        mine.mkdir(parents=True)
        for n in range(session_store.KEEP_SESSIONS + 5):
            (mine / f"2026010{n:02d}T000000Z-old.json").write_text("{}", encoding="utf-8")

        state = WizardState()
        state.selected_reports_added = {"W0": {}}
        session_store.save_session(state, directory=mine)

        assert (theirs / "20260101T000000Z-keep.json").exists()
        assert len(list(mine.glob("*.json"))) == session_store.KEEP_SESSIONS


class TestReferenceMapsAreNotShared:
    def test_a_map_for_the_same_tenant_does_not_leak_across_workspaces(
        self, tmp_path, monkeypatch
    ):
        """Two consultants migrating into the same destination is the normal
        case, and ``save_map`` replaces rather than merges."""
        monkeypatch.setattr(reference_maps, "MAP_DIR", tmp_path)
        from wdmigrator.api import ReferenceAction, ReferenceDecision

        decision = {
            "W1": ReferenceDecision(source_wid="W1", action=ReferenceAction.BLANK, note="")
        }
        reference_maps.save_map(decision, dest_tenant="dst", directory=tmp_path / "theirs")

        assert reference_maps.load_map("dst", directory=tmp_path / "mine") is None
        assert reference_maps.load_map("dst", directory=tmp_path / "theirs") is not None


class TestTheIndexCacheStaysShared:
    def test_cache_path_is_keyed_by_tenant_only(self):
        """Deliberate. It holds no personal state, is written atomically, and
        a report sweep is 2.5 minutes that two people on the same tenant
        should not each pay for."""
        from types import SimpleNamespace

        from wdmigrator.api import cache_path

        connection = SimpleNamespace(target=SimpleNamespace(tenant="acme"))
        path = cache_path(connection, "report")
        assert path.parent.name == "acme"
        assert TEST_WORKSPACE not in str(path)
