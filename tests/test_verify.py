"""Read-back verifier tests.

Every test that fires against a live tenant here would defeat the point — a
fake destination is exactly enough to pin the structural comparisons and the
"SUCCESS bit lied" case (HANDOFF: empty-shell dashboards written and
reported clean). The signal set per kind is deliberately small; changing it
should require touching a test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wdmigrator.discovery.inventory import LookupOutcome, LookupResult
from wdmigrator.migrate.planner import Action
from wdmigrator.migrate.resolver import Node, NodeKind
from wdmigrator.migrate.writer import WriteRecord, WriteStatus
from wdmigrator.validation.verify import (
    VerifyStatus,
    _calculated_field_signals,
    _dashboard_signals,
    _prompt_set_signals,
    _report_signals,
    iter_verify,
    summarise,
    verify_record,
)


class _StubConnection:
    def __init__(self, result: LookupResult | Exception):
        self._result = result
        self.calls = []
        self.limiter = SimpleNamespace(wait=lambda: None)
        self.target = SimpleNamespace(tenant="dest")

    def redact(self, text: str) -> str:
        return text


def _write_record(node, status=WriteStatus.SUCCESS, dest_wid="DEST_W"):
    return WriteRecord(
        node_id=node.node_id,
        kind=node.kind.value,
        name=node.name,
        reference_id=node.reference_id,
        action=Action.CREATE,
        status=status,
        dest_wid=dest_wid,
        dry_run=False,
    )


class TestSignalExtractors:
    def test_dashboard_signals_count_tabs_worklets_and_prompt_sets(self):
        payload = {
            "Custom_Dashboard_with_Tabs_Data": {
                "Custom_Dashboard_Tab_Data": [
                    {"Worklets_Data": [{}, {}]},
                    {"Worklets_Data": [{}]},
                ],
                "Prompt_Set_Reference": [{}, {}, {}],
            }
        }
        assert _dashboard_signals(payload) == {"tabs": 2, "worklets": 3, "prompt_set_refs": 3}

    def test_dashboard_shell_reports_zero_worklets(self):
        payload = {
            "Custom_Dashboard_with_Tabs_Data": {
                "Custom_Dashboard_Tab_Data": [{"Tab_Name": "Empty"}]
            }
        }
        assert _dashboard_signals(payload)["worklets"] == 0

    def test_report_signals_count_columns_and_conditions(self):
        payload = {
            "Tenanted_Report_Definition_Data": {
                "Report_Column_Data": [{}, {}, {}],
                "Report_Filter_Data": {
                    "Filter_Condition_Data": {
                        "Condition_Item_Data": [{}, {}]
                    }
                },
            }
        }
        signals = _report_signals(payload)
        assert signals["columns"] == 3
        assert signals["filter_conditions"] == 2

    def test_prompt_set_signals_count_members(self):
        payload = {
            "Prompt_Set_Data": {
                "Tenanted_Prompt_Set_Member_Data": [{}, {}, {}, {}]
            }
        }
        assert _prompt_set_signals(payload)["members"] == 4

    def test_calculated_field_signals_reports_data_block_presence(self):
        assert _calculated_field_signals({"Calculated_Field_Data": {"Name": "x"}})["has_data_block"] == 1
        assert _calculated_field_signals({})["has_data_block"] == 0


class TestVerifyRecord:
    def _node(self, kind=NodeKind.REPORT, name="R", payload=None):
        return Node(
            node_id=f"{kind.value}:W1",
            kind=kind,
            source_wid="W1",
            reference_id="R",
            name=name,
            payload=payload
            or {"Tenanted_Report_Definition_Data": {"Report_Column_Data": [{}, {}]}},
        )

    def test_skipped_writes_are_reported_skipped_with_no_network_call(self, monkeypatch):
        node = self._node()
        record = _write_record(node, status=WriteStatus.SKIPPED)
        # Any lookup call would blow up — but none should happen.
        monkeypatch.setattr(
            "wdmigrator.validation.verify.lookup_report",
            lambda *a, **kw: pytest.fail("should not be called for a SKIPPED write"),
        )
        result = verify_record(_StubConnection(None), node, record)
        assert result.status is VerifyStatus.SKIPPED

    def test_failed_writes_are_not_re_examined(self, monkeypatch):
        node = self._node()
        record = _write_record(node, status=WriteStatus.FAILED)
        monkeypatch.setattr(
            "wdmigrator.validation.verify.lookup_report",
            lambda *a, **kw: pytest.fail("should not be called for a FAILED write"),
        )
        result = verify_record(_StubConnection(None), node, record)
        assert result.status is VerifyStatus.ERROR
        assert "failed" in (result.fault or "").lower()

    def test_missing_object_after_success_is_the_shell_bug(self, monkeypatch):
        """The failure mode the read-back exists to catch: writer reports
        SUCCESS, destination has no such object."""
        node = self._node()
        record = _write_record(node)
        monkeypatch.setattr(
            "wdmigrator.validation.verify.lookup_report",
            lambda *a, **kw: LookupResult(outcome=LookupOutcome.NOT_FOUND, wid="DEST_W"),
        )
        result = verify_record(_StubConnection(None), node, record)
        assert result.status is VerifyStatus.MISSING

    def test_matching_signals_produce_ok(self, monkeypatch):
        node = self._node()
        record = _write_record(node)
        dest_payload = {"Tenanted_Report_Definition_Data": {"Report_Column_Data": [{}, {}]}}
        monkeypatch.setattr(
            "wdmigrator.validation.verify.lookup_report",
            lambda *a, **kw: LookupResult(outcome=LookupOutcome.FOUND, data=dest_payload, wid="DEST_W"),
        )
        result = verify_record(_StubConnection(None), node, record)
        assert result.status is VerifyStatus.OK
        assert result.findings == []

    def test_column_count_mismatch_surfaces_as_mismatch(self, monkeypatch):
        node = self._node(
            payload={"Tenanted_Report_Definition_Data": {"Report_Column_Data": [{}, {}, {}, {}]}}
        )
        record = _write_record(node)
        dest_payload = {"Tenanted_Report_Definition_Data": {"Report_Column_Data": [{}]}}
        monkeypatch.setattr(
            "wdmigrator.validation.verify.lookup_report",
            lambda *a, **kw: LookupResult(outcome=LookupOutcome.FOUND, data=dest_payload, wid="DEST_W"),
        )
        result = verify_record(_StubConnection(None), node, record)
        assert result.status is VerifyStatus.MISMATCH
        assert any(f.signal == "columns" for f in result.findings)

    def test_dashboard_shell_read_back_is_flagged_even_if_signals_match(self, monkeypatch):
        """A dashboard whose source ALSO reads back as a shell (both zero) would
        report OK on structural signals alone. Read-back adds the shell flag so
        the "still broken" case surfaces regardless."""
        node = Node(
            node_id="dashboard_tabbed:D1",
            kind=NodeKind.DASHBOARD_TABBED,
            source_wid="D1",
            reference_id="Dash",
            name="Dash",
            payload={"Custom_Dashboard_with_Tabs_Data": {}},
        )
        record = _write_record(node)
        dest_payload = {"Custom_Dashboard_with_Tabs_Data": {}}  # also empty
        monkeypatch.setattr(
            "wdmigrator.validation.verify.lookup_dashboard",
            lambda *a, **kw: LookupResult(outcome=LookupOutcome.FOUND, data=dest_payload, wid="DEST_W"),
        )
        result = verify_record(_StubConnection(None), node, record)
        assert result.status is VerifyStatus.MISMATCH
        assert any(f.signal == "dashboard_is_shell" for f in result.findings)


class TestIterVerify:
    def test_summarise_counts_by_status(self):
        records = [
            SimpleNamespace(status=VerifyStatus.OK),
            SimpleNamespace(status=VerifyStatus.OK),
            SimpleNamespace(status=VerifyStatus.MISMATCH),
            SimpleNamespace(status=VerifyStatus.SKIPPED),
        ]
        counts = summarise(records)
        assert counts["ok"] == 2
        assert counts["mismatch"] == 1
        assert counts["skipped"] == 1
        assert counts["missing"] == 0

    def test_iter_verify_yields_progress_per_record(self, monkeypatch):
        node = Node(
            node_id="calculated_field:W1",
            kind=NodeKind.CALCULATED_FIELD,
            source_wid="W1",
            reference_id="CF_A",
            name="Field",
            payload={"Calculated_Field_Data": {"Name": "Field"}},
        )
        records = [_write_record(node), _write_record(node, status=WriteStatus.SKIPPED)]
        monkeypatch.setattr(
            "wdmigrator.validation.verify.lookup_calculated_field",
            lambda *a, **kw: LookupResult(
                outcome=LookupOutcome.FOUND,
                data={"Calculated_Field_Data": {"Name": "Field"}},
                wid="DEST_W",
            ),
        )
        events = list(iter_verify(_StubConnection(None), [node], records))
        assert len(events) == 2
        assert [e.position for e in events] == [1, 2]
        assert events[0].record.status is VerifyStatus.OK
        assert events[1].record.status is VerifyStatus.SKIPPED
