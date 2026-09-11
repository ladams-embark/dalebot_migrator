"""A session the hosted app cannot lose.

Server-side saves survive a browser reload. They do not survive the container
restarting, which a hosted Streamlit app does on redeploy, on waking from
idle, and on running out of memory — and it takes the whole ``out/`` tree with
it. So the durable copy is the one on the consultant's own machine: downloaded
from Select, handed back through the uploader on Connect.

An uploaded file is the least trustworthy input in the wizard, so it gets the
same schema check as one read off disk, and the same rule about what comes
back: inputs, never approvals.
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.config.targets import target_from_parts
from wdmigrator.ui import session_store
from wdmigrator.ui.state import STATE_KEY, WizardState

from conftest import pin_workspace

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _picked_state():
    state = WizardState()
    state.source.target = target_from_parts("h.example.com", "src_tenant")
    state.source.target_raw = "h.example.com"
    state.source.username = "isu_source"
    state.dest.target = target_from_parts("h.example.com", "dst_tenant")
    state.object_kinds = ["reports"]
    state.selected_reports_added = {"W0": {"Report_Name": "Headcount"}}
    state.selected_field_wids = {"F1", "F2"}
    return state


class TestTheFileItself:
    def test_it_round_trips_through_text(self):
        original = _picked_state()
        parsed = session_store.parse_session(session_store.serialise(original))

        restored = WizardState()
        session_store.restore(restored, parsed)
        assert restored.selected_reports_added == {"W0": {"Report_Name": "Headcount"}}
        assert restored.selected_field_wids == {"F1", "F2"}
        assert restored.source.username == "isu_source"

    def test_it_still_refuses_to_restore_approvals(self):
        """The whole safety argument for session persistence. A file someone
        can edit by hand must not be able to hand back a reviewed dry run."""
        text = session_store.serialise(_picked_state())
        document = json.loads(text)
        document["dry_run_reviewed"] = True
        document["confirmed_tenant_name"] = "dst_tenant"

        restored = WizardState()
        notes = session_store.restore(restored, session_store.parse_session(
            json.dumps(document)
        ))
        assert not getattr(restored, "dry_run_reviewed", False)
        assert not getattr(restored, "confirmed_tenant_name", "")
        assert any("acknowledgement" in n.lower() for n in notes)

    def test_no_password_is_ever_written(self):
        state = _picked_state()
        state.source.password = "hunter2-should-never-appear"
        assert "hunter2" not in session_store.serialise(state)

    def test_the_filename_names_both_tenants(self):
        name = session_store.download_name(_picked_state())
        assert "src_tenant" in name and "dst_tenant" in name
        assert name.endswith(".json")


class TestRejectingBadUploads:
    @pytest.mark.parametrize(
        "payload",
        [b"\xff\xfe not utf-8 \xff", b"{not json", b'"a string, not an object"', b"[]"],
    )
    def test_unusable_files_raise_rather_than_half_restore(self, payload):
        with pytest.raises(session_store.SessionError):
            session_store.parse_session(payload)

    def test_a_file_from_another_schema_version_is_refused(self):
        document = json.loads(session_store.serialise(_picked_state()))
        document["$schema_version"] = session_store.SCHEMA_VERSION + 1
        with pytest.raises(session_store.SessionError) as excinfo:
            session_store.parse_session(json.dumps(document), label="theirs.json")
        assert "theirs.json" in str(excinfo.value)

    def test_the_error_names_the_file_the_user_chose(self):
        with pytest.raises(session_store.SessionError) as excinfo:
            session_store.parse_session(b"{not json", label="monday.json")
        assert "monday.json" in str(excinfo.value)


class _StubTarget:
    tenant = "stub_tenant"


class _StubConnection:
    """No ``service`` attribute: reaching for one would be a tenant call."""

    target = _StubTarget()

    def for_service(self, _name):
        return self


class TestTheControlsAreThere:
    def _app(self, step, state):
        at = pin_workspace(AppTest.from_file(str(ROOT / "streamlit_app.py")))
        state.step = step
        state.source.connection = _StubConnection()
        state.dest.connection = _StubConnection()
        at.session_state[STATE_KEY] = state
        at.run(timeout=20)
        assert not at.exception, at.exception
        return at

    def test_select_offers_a_download_once_something_is_picked(self):
        at = self._app("select", _picked_state())
        assert [d for d in at.get("download_button") if d.key == "session_download"]

    def test_select_offers_no_download_with_an_empty_selection(self):
        at = self._app("select", WizardState())
        assert not [d for d in at.get("download_button") if d.key == "session_download"]

    def test_connect_offers_an_uploader_even_with_nothing_saved_on_the_server(self):
        """The case that matters after a restart: the server has nothing, and
        the only copy left is the one on the user's machine."""
        at = self._app("connect", WizardState())
        rendered = " ".join(str(m.value) for m in at.markdown)
        assert "Resume a saved session" in rendered

    def test_connect_withholds_resume_over_work_in_progress(self):
        at = self._app("connect", _picked_state())
        rendered = " ".join(str(m.value) for m in at.markdown)
        assert "Resume a saved session" not in rendered
