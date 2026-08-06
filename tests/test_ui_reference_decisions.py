"""The mapping table's logic, tested without rendering anything.

`_collect_blockers`, `_decision_rows` and `_apply_decisions` are where the
behaviour lives; the Streamlit widgets around them are a shell. Testing these
directly catches the class of bug that has bitten this feature twice already —
a decision that records but never reaches the payload.
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from wdmigrator.migrate.planner import Action, MigrationPlan, ReferenceAction
from wdmigrator.migrate.resolver import Node, NodeKind
from wdmigrator.migrate.writer import BlockingReference, WriteRecord, WriteStatus
from wdmigrator.ui.state import WizardState
from wdmigrator.ui.steps.execute import (
    _apply_decisions,
    _collect_blockers,
    _decision_rows,
)


def node_with_reference(wid="ORG_WID"):
    return Node(
        node_id="report:R1", kind=NodeKind.REPORT, source_wid="R1",
        reference_id="RPT", name="Jordan Demo",
        payload={"Tenanted_Report_Definition_Data": {
            "Tenanted_Report_Parameter_Options_Data": [
                {"Instance_Reference": [{"ID": [
                    {"type": "WID", "_value_1": wid},
                    {"type": "Organization_Reference_ID", "_value_1": "TOP"},
                ]}]}
            ],
        }},
    )


def failed_record(wid="ORG_WID", name="Jordan Demo"):
    return WriteRecord(
        node_id="report:R1", kind="report", name=name, reference_id="RPT",
        action=Action.CREATE, status=WriteStatus.FAILED, dry_run=False,
        blocking_reference=BlockingReference(value=wid, id_type="WID"),
    )


def state_with(records, nodes=None):
    state = WizardState()
    state.plan = MigrationPlan(ordered_nodes=nodes or [node_with_reference()])
    state.execute_records = records
    return state


class TestCollectBlockers:
    def test_a_blocking_reference_is_captured_with_context(self):
        state = state_with([failed_record()])
        _collect_blockers(state)
        info = state.blocking_references["ORG_WID"]
        assert info["node_name"] == "Jordan Demo"
        assert info["elements"] == ["Instance_Reference"]
        assert info["business"] == {"Organization_Reference_ID": "TOP"}

    def test_records_without_a_blocking_reference_are_ignored(self):
        record = failed_record()
        record.blocking_reference = None
        state = state_with([record])
        _collect_blockers(state)
        assert state.blocking_references == {}

    def test_blockers_accumulate_across_attempts(self):
        """Workday reports one per attempt; the table has to build up rather
        than replace itself, or the earlier rows vanish."""
        state = state_with([failed_record("W1")])
        _collect_blockers(state)
        state.execute_records = [failed_record("W2")]
        _collect_blockers(state)
        assert set(state.blocking_references) == {"W1", "W2"}

    def test_the_same_reference_is_not_captured_twice(self):
        state = state_with([failed_record(), failed_record()])
        _collect_blockers(state)
        assert len(state.blocking_references) == 1


class TestDecisionRows:
    def test_blank_is_the_default(self):
        state = state_with([failed_record()])
        _collect_blockers(state)
        row = _decision_rows(state)[0]
        assert row["Decision"] == ReferenceAction.BLANK.value

    def test_the_business_id_type_is_prefilled_for_replacing(self):
        state = state_with([failed_record()])
        _collect_blockers(state)
        assert _decision_rows(state)[0]["Replacement ID type"] == (
            "Organization_Reference_ID"
        )

    def test_an_existing_decision_is_shown_back(self):
        state = state_with([failed_record()])
        _collect_blockers(state)
        _apply_decisions(state, _decision_rows(state), _edited(state, decision="replace",
                                        id_type="Organization_Reference_ID",
                                        value="DEST"))
        row = _decision_rows(state)[0]
        assert row["Decision"] == "replace"
        assert row["Replacement value"] == "DEST"

    def test_the_wid_is_carried_but_hidden(self):
        state = state_with([failed_record()])
        _collect_blockers(state)
        assert _decision_rows(state)[0]["_wid"] == "ORG_WID"


def _edited(state, *, decision, id_type="", value=""):
    import pandas as pd

    rows = _decision_rows(state)
    for row in rows:
        row["Decision"] = decision
        row["Replacement ID type"] = id_type
        row["Replacement value"] = value
    return pd.DataFrame(rows)


class TestApplyDecisions:
    def test_blank_is_recorded(self):
        state = state_with([failed_record()])
        _collect_blockers(state)
        _apply_decisions(state, _decision_rows(state), _edited(state, decision="blank"))
        decision = state.reference_decisions["ORG_WID"]
        assert decision.action is ReferenceAction.BLANK

    def test_replace_is_recorded_with_both_parts(self):
        state = state_with([failed_record()])
        _collect_blockers(state)
        _apply_decisions(state, _decision_rows(state), _edited(state, decision="replace",
                                        id_type="Organization_Reference_ID",
                                        value="DEST_ORG"))
        decision = state.reference_decisions["ORG_WID"]
        assert decision.action is ReferenceAction.REPLACE
        assert decision.replacement_type == "Organization_Reference_ID"
        assert decision.replacement_value == "DEST_ORG"

    def test_an_incomplete_replace_is_not_recorded(self):
        """Constructing one would raise; the submit button is gated on this too,
        but skipping here means a half-filled row can never corrupt the plan."""
        state = state_with([failed_record()])
        _collect_blockers(state)
        _apply_decisions(state, _decision_rows(state), _edited(state, decision="replace"))
        assert "ORG_WID" not in state.reference_decisions

    def test_decisions_reach_the_payload(self):
        """The end that matters. Twice now a decision has been recorded and
        silently ignored by the payload builder."""
        from wdmigrator.migrate.writer import build_report_payload

        state = state_with([failed_record()])
        _collect_blockers(state)
        _apply_decisions(state, _decision_rows(state), _edited(state, decision="blank"))
        data = build_report_payload(
            node_with_reference(), {}, action=Action.CREATE,
            reference_decisions=state.reference_decisions,
        )["Tenanted_Report_Definition_Data"]
        assert "Instance_Reference" not in (
            data["Tenanted_Report_Parameter_Options_Data"][0]
        )
