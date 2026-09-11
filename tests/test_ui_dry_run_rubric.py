"""The last gate before an irreversible write has to say what to look for.

"I have reviewed this dry run's output" is the checkbox standing between a
plan and a tenant this tool cannot un-write. It asked the user to certify
they had read something without ever saying what a problem looks like. With
somebody beside them that is the "check those two cross-tenant matches"
conversation; alone it is a box that gets ticked.
"""

from __future__ import annotations

import pytest

from wdmigrator.api import (
    Action,
    Existence,
    LookupOutcome,
    MigrationPlan,
    Node,
    NodeKind,
    ReportSharing,
    WriteRecord,
    WriteStatus,
    node_id_for,
    target_from_parts,
)
from wdmigrator.ui.state import WizardState
from wdmigrator.ui.steps.confirm import _dry_run_review_points


def _node(kind: NodeKind, wid: str, name: str) -> Node:
    return Node(
        node_id=node_id_for(kind, wid),
        kind=kind,
        source_wid=wid,
        reference_id=wid,
        name=name,
        payload={},
    )


def _state(*pairs, **kwargs) -> WizardState:
    plan = MigrationPlan()
    for i, (kind, action) in enumerate(pairs):
        node = _node(kind, f"W{i}", f"Object {i}")
        plan.ordered_nodes.append(node)
        plan.actions[node.node_id] = action
    state = WizardState(step="plan", plan=plan, **kwargs)
    state.dest.target = target_from_parts("impl-services1.wd12.myworkday.com", "dpt5")
    return state


def _points(state) -> str:
    return " ".join(_dry_run_review_points(state))


def test_creates_are_called_out_with_the_destination_named():
    state = _state(
        (NodeKind.CALCULATED_FIELD, Action.CREATE),
        (NodeKind.REPORT, Action.CREATE),
    )
    text = _points(state)
    assert "2 object(s) will be created in dpt5" in text
    assert "refreshed" in text, "the common cause of a surprise CREATE is worth naming"


def test_updates_are_described_as_overwriting_rather_than_adding():
    state = _state((NodeKind.DASHBOARD_TABBED, Action.UPDATE))
    assert "overwritten in place" in _points(state)


def test_cross_tenant_shape_matches_are_flagged_as_the_weaker_claim():
    """An ID match says "this is that object". A shape match says "this looks
    like that object", and acting on it reuses something that may not be."""
    state = _state((NodeKind.CALCULATED_FIELD, Action.SKIP))
    node_id = state.plan.ordered_nodes[0].node_id
    state.plan.existence[node_id] = Existence(
        node_id=node_id,
        state=LookupOutcome.FOUND,
        dest_wid="DEST1",
        matched_by="shape",
    )
    text = _points(state)
    assert "matched to dpt5 on shape rather than on business ID" in text
    assert "weaker claim" in text


def test_unknown_existence_is_named_even_though_it_already_blocks():
    state = _state((NodeKind.REPORT, Action.SKIP))
    node_id = state.plan.ordered_nodes[0].node_id
    state.plan.existence[node_id] = Existence(
        node_id=node_id, state=LookupOutcome.UNKNOWN, fault="boom"
    )
    assert "unknown destination state" in _points(state)


def test_objects_that_failed_to_serialize_are_named():
    state = _state((NodeKind.REPORT, Action.CREATE))
    state.dry_run_records = [
        WriteRecord(
            node_id="report:W0",
            kind="report",
            reference_id="W0",
            name="Headcount",
            action=Action.CREATE,
            status=WriteStatus.FAILED,
            dry_run=True,
        )
    ]
    text = _points(state)
    assert "could not even be serialized" in text
    assert "Headcount" in text


def test_report_ownership_and_sharing_are_stated_when_reports_are_created():
    """Both are decided for the user by this tool, so both belong in the
    things the user is certifying they understood."""
    state = _state((NodeKind.REPORT, Action.CREATE))
    state.report_sharing = ReportSharing.SHARED_WITH_ALL_AUTHORIZED_USERS
    text = _points(state)
    assert "owned by" in text
    assert "Share with all authorized users" in text


def test_the_envelope_spot_check_is_always_asked_for():
    state = _state((NodeKind.REPORT, Action.SKIP))
    assert "serialized envelope" in _points(state)


def test_a_skip_only_plan_still_produces_a_usable_list():
    """No creates, no updates, nothing matched — the list must not be empty,
    or the box goes back to certifying nothing."""
    state = _state((NodeKind.REPORT, Action.SKIP))
    assert _dry_run_review_points(state)
