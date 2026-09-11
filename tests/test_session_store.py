"""A saved session restores inputs and never restores approvals.

Streamlit's session state dies with the websocket, so a browser reload used
to cost the user every tenant URL, every username, and a selection that may
have taken half a dozen searches to assemble. Saving it to disk fixes that.

The reason this module needs tests more than most is the other half: what
must *not* come back. This tool writes to a service with no delete operation,
and every guard in ``safety.py`` is downstream of a human having ticked a box
about a specific plan. A rehydrated tick is not consent, and a rehydrated
password is not a thing that should exist at all.
"""

from __future__ import annotations

import json

import pytest

from wdmigrator.api import ReferenceAction, ReferenceDecision, ReportSharing
from wdmigrator.config.targets import target_from_parts
from wdmigrator.ui import session_store
from wdmigrator.ui.state import WizardState


def _populated_state() -> WizardState:
    state = WizardState(step="plan", object_kinds=["reports", "dashboards"])
    state.source.target_raw = "https://h/ccx/service/src_tenant/x/v46.0"
    state.source.target = target_from_parts("impl-services1.wd12.myworkday.com", "src_tenant")
    state.source.username = "wd-implementer"
    state.source.password = "hunter2"
    state.source.connection = object()
    state.source.verified_fingerprint = "fp-source"

    state.dest.target_raw = "https://h/ccx/service/dst_tenant/x/v46.0"
    state.dest.target = target_from_parts("impl-services1.wd12.myworkday.com", "dst_tenant")
    state.dest.username = "wd-implementer"
    state.dest.password = "hunter2"
    state.dest.connection = object()

    state.selected_reports_added = {"W0": {"Tenanted_Report_Definition_Data": {"Name": "A"}}}
    state.selected_reports = dict(state.selected_reports_added)
    state.selected_dashboards_added = {"D0": {"Custom_Landing_Page_Data": {}}}
    state.selected_dashboards = dict(state.selected_dashboards_added)
    state.selected_field_wids = {"CF0", "CF1"}
    state.selected_time_calculation_wids = {"TC0"}
    state.reference_decisions = {
        "R0": ReferenceDecision(source_wid="R0", action=ReferenceAction.BLANK, note="gone")
    }
    state.report_sharing = ReportSharing.SHARED_WITH_ALL_AUTHORIZED_USERS

    # Everything below stands for a human having looked at something.
    state.dry_run_reviewed = True
    state.dry_run_plan_hash = "abc123"
    state.dry_run_records = [object()]
    state.confirmed_tenant_name = "dst_tenant"
    state.irreversible_ack = True
    state.warnings_acknowledged = {"w1", "w2"}
    state.execute_records = [object()]
    return state


class TestWhatIsNeverWritten:
    def test_no_password_reaches_the_file(self, tmp_path):
        path = session_store.save_session(_populated_state(), directory=tmp_path)
        assert "hunter2" not in path.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "dry_run_reviewed",
            "dry_run_plan_hash",
            "dry_run_records",
            "confirmed_tenant_name",
            "irreversible_ack",
            "warnings_acknowledged",
            "execute_records",
            "plan",
            "closure",
        ],
    )
    def test_no_approval_or_artefact_key_is_serialised(self, key):
        """The snapshot is an allow-list. A field added to ``WizardState``
        later defaults to absent, which is the safe direction: re-picking a
        report costs a minute, and a resurrected acknowledgement costs a write
        to a tenant that cannot be undone."""
        assert key not in json.dumps(session_store.snapshot(_populated_state()))

    def test_the_snapshot_is_json_serialisable_with_zeep_payloads(self, tmp_path):
        """Report payloads come out of zeep carrying datetimes and Decimals."""
        import datetime
        import decimal

        state = _populated_state()
        state.selected_reports_added["W1"] = {
            "Effective": datetime.date(2026, 1, 1),
            "Weight": decimal.Decimal("1.5"),
        }
        path = session_store.save_session(state, directory=tmp_path)
        assert json.loads(path.read_text(encoding="utf-8"))


class TestRoundTrip:
    def _restored(self, tmp_path) -> tuple[WizardState, list[str]]:
        path = session_store.save_session(_populated_state(), directory=tmp_path)
        fresh = WizardState()
        notes = session_store.restore(fresh, session_store.load_session(path))
        return fresh, notes

    def test_the_selection_comes_back_whole(self, tmp_path):
        fresh, _ = self._restored(tmp_path)
        assert set(fresh.selected_reports_added) == {"W0"}
        assert set(fresh.selected_dashboards_added) == {"D0"}
        assert fresh.selected_field_wids == {"CF0", "CF1"}
        assert fresh.selected_time_calculation_wids == {"TC0"}
        assert fresh.object_kinds == ["reports", "dashboards"]

    def test_the_derived_selection_mirrors_are_rebuilt(self, tmp_path):
        """``selected_reports`` is a copy of ``selected_reports_added`` that
        the picker refreshes each render. Leaving it empty on restore would
        make the Select gate say "nothing selected" over a full selection."""
        fresh, _ = self._restored(tmp_path)
        assert fresh.selected_reports == fresh.selected_reports_added
        assert fresh.selected_dashboards == fresh.selected_dashboards_added

    def test_tenants_and_usernames_come_back(self, tmp_path):
        fresh, _ = self._restored(tmp_path)
        assert fresh.source.username == "wd-implementer"
        assert "src_tenant" in fresh.source.target_raw
        assert "dst_tenant" in fresh.dest.target_raw

    def test_reference_decisions_come_back_as_objects(self, tmp_path):
        fresh, _ = self._restored(tmp_path)
        decision = fresh.reference_decisions["R0"]
        assert decision.action is ReferenceAction.BLANK
        assert decision.note == "gone"

    def test_report_sharing_comes_back(self, tmp_path):
        fresh, _ = self._restored(tmp_path)
        assert fresh.report_sharing is ReportSharing.SHARED_WITH_ALL_AUTHORIZED_USERS

    def test_an_unrecognised_sharing_value_falls_back_to_unshared(self):
        fresh = WizardState()
        session_store.restore(fresh, {"report_sharing": "share_with_everyone_forever"})
        assert fresh.report_sharing is ReportSharing.UNSHARED

    def test_approvals_do_not_come_back(self, tmp_path):
        fresh, _ = self._restored(tmp_path)
        assert fresh.dry_run_reviewed is False
        assert fresh.dry_run_plan_hash == ""
        assert fresh.confirmed_tenant_name == ""
        assert fresh.irreversible_ack is False
        assert fresh.warnings_acknowledged == set()
        assert fresh.plan is None

    def test_no_connection_or_password_comes_back(self, tmp_path):
        fresh, _ = self._restored(tmp_path)
        assert fresh.source.password == ""
        assert fresh.source.connection is None
        assert fresh.source.verified_fingerprint == ""
        assert not fresh.source.verified

    def test_the_user_is_told_what_did_not_come_back(self, tmp_path):
        """Seeing thirty reports restored invites assuming everything was."""
        _, notes = self._restored(tmp_path)
        joined = " ".join(notes)
        assert "Passwords are never saved" in joined
        assert "acknowledgement" in joined


class TestSourceTenantPinning:
    def test_the_source_tenant_is_remembered_for_the_connect_check(self, tmp_path):
        path = session_store.save_session(_populated_state(), directory=tmp_path)
        fresh = WizardState()
        session_store.restore(fresh, session_store.load_session(path))
        assert fresh.restored_source_tenant == "src_tenant"

    def test_a_session_saved_before_the_source_connected_pins_nothing(self):
        fresh = WizardState()
        session_store.restore(fresh, {"source": {}})
        assert fresh.restored_source_tenant == ""


class TestListing:
    def test_newest_first(self, tmp_path):
        for name in ("20260101T000000Z-a.json", "20260201T000000Z-b.json"):
            (tmp_path / name).write_text(
                json.dumps({"$schema_version": session_store.SCHEMA_VERSION}), encoding="utf-8"
            )
        names = [s.path.name for s in session_store.list_sessions(tmp_path)]
        assert names == ["20260201T000000Z-b.json", "20260101T000000Z-a.json"]

    def test_a_bad_file_is_skipped_not_raised(self, tmp_path):
        (tmp_path / "good.json").write_text(
            json.dumps({"$schema_version": session_store.SCHEMA_VERSION}), encoding="utf-8"
        )
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "wrong-schema.json").write_text(
            json.dumps({"$schema_version": 99}), encoding="utf-8"
        )
        assert [s.path.name for s in session_store.list_sessions(tmp_path)] == ["good.json"]

    def test_a_missing_directory_lists_nothing(self, tmp_path):
        assert session_store.list_sessions(tmp_path / "nope") == []

    def test_the_label_names_both_tenants_and_the_counts(self, tmp_path):
        session_store.save_session(_populated_state(), directory=tmp_path)
        label = session_store.list_sessions(tmp_path)[0].label
        assert "src_tenant to dst_tenant" in label
        assert "1 reports" in label
        assert "2 calculated fields" in label

    def test_an_empty_session_says_so_rather_than_reading_as_broken(self, tmp_path):
        session_store.save_session(WizardState(), directory=tmp_path)
        assert "nothing selected" in session_store.list_sessions(tmp_path)[0].label


class TestRotation:
    def test_old_sessions_are_pruned(self, tmp_path):
        for i in range(session_store.KEEP_SESSIONS + 5):
            (tmp_path / f"2026010{i:02d}T000000Z-old.json").write_text(
                json.dumps({"$schema_version": session_store.SCHEMA_VERSION}), encoding="utf-8"
            )
        session_store.save_session(_populated_state(), directory=tmp_path)
        assert len(list(tmp_path.glob("*.json"))) == session_store.KEEP_SESSIONS

    def test_the_session_just_saved_survives_its_own_prune(self, tmp_path):
        for i in range(session_store.KEEP_SESSIONS + 5):
            (tmp_path / f"2026010{i:02d}T000000Z-old.json").write_text(
                json.dumps({"$schema_version": session_store.SCHEMA_VERSION}), encoding="utf-8"
            )
        path = session_store.save_session(_populated_state(), directory=tmp_path)
        assert path.exists()


class TestSchema:
    def test_an_unknown_schema_version_raises_rather_than_partially_loading(self, tmp_path):
        bad = tmp_path / "s.json"
        bad.write_text(json.dumps({"$schema_version": 99}), encoding="utf-8")
        with pytest.raises(session_store.SessionError, match="different version"):
            session_store.load_session(bad)

    def test_a_non_object_file_raises(self, tmp_path):
        bad = tmp_path / "s.json"
        bad.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(session_store.SessionError):
            session_store.load_session(bad)

    def test_a_missing_file_raises_session_error_not_oserror(self, tmp_path):
        with pytest.raises(session_store.SessionError):
            session_store.load_session(tmp_path / "absent.json")
