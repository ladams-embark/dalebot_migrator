"""The plan, in a sentence.

Everywhere else a plan is shown as counts by action plus a hash. That answers
"how many" and never "what is about to happen to my tenant" — which is the
question somebody deciding without a colleague beside them actually has.
"""

from __future__ import annotations

import pytest

from wdmigrator.api import (
    Action,
    MigrationPlan,
    Node,
    NodeKind,
    describe_plan,
    node_id_for,
)


def _node(kind: NodeKind, wid: str) -> Node:
    return Node(
        node_id=node_id_for(kind, wid),
        kind=kind,
        source_wid=wid,
        reference_id=wid,
        name=wid,
        payload={},
    )


def _plan(*pairs: tuple[NodeKind, Action]) -> MigrationPlan:
    plan = MigrationPlan()
    for i, (kind, action) in enumerate(pairs):
        node = _node(kind, f"W{i}")
        plan.ordered_nodes.append(node)
        plan.actions[node.node_id] = action
    return plan


def test_a_create_only_plan_names_the_kinds_and_the_tenant():
    plan = _plan(
        (NodeKind.CALCULATED_FIELD, Action.CREATE),
        (NodeKind.CALCULATED_FIELD, Action.CREATE),
        (NodeKind.REPORT, Action.CREATE),
    )
    sentence = describe_plan(plan, destination_tenant="dpt5")
    assert "create 2 calculated fields and 1 report" in sentence
    assert "`dpt5`" in sentence
    assert "Nothing is deleted" in sentence


def test_reused_objects_are_counted_separately_from_written_ones():
    plan = _plan(
        (NodeKind.REPORT, Action.CREATE),
        (NodeKind.CALCULATED_FIELD, Action.SKIP),
        (NodeKind.CALCULATED_FIELD, Action.SKIP),
    )
    sentence = describe_plan(plan, destination_tenant="dpt5")
    assert "create 1 report" in sentence
    assert "2 other object(s) already exist" in sentence


def test_updates_read_as_updates_not_creates():
    plan = _plan(
        (NodeKind.DASHBOARD_TABBED, Action.UPDATE),
        (NodeKind.REPORT, Action.CREATE),
    )
    sentence = describe_plan(plan, destination_tenant="dpt5")
    assert "create 1 report" in sentence
    assert "update 1 dashboard" in sentence


def test_the_two_dashboard_flavours_add_up_to_one_plural():
    """Tabbed and untabbed are a real distinction to the writer and a
    meaningless one to a reader — they must not read as "1 dashboard" twice."""
    plan = _plan(
        (NodeKind.DASHBOARD, Action.CREATE),
        (NodeKind.DASHBOARD_TABBED, Action.CREATE),
    )
    assert "create 2 dashboards" in describe_plan(plan, destination_tenant="dpt5")


def test_a_plan_that_writes_nothing_says_so_plainly():
    plan = _plan(
        (NodeKind.REPORT, Action.SKIP),
        (NodeKind.CALCULATED_FIELD, Action.SKIP),
    )
    sentence = describe_plan(plan, destination_tenant="dpt5")
    assert "Nothing will be written" in sentence
    assert "all 2 object(s) are already there" in sentence


def test_the_tenant_is_optional():
    plan = _plan((NodeKind.REPORT, Action.CREATE))
    assert "`" not in describe_plan(plan)


@pytest.mark.parametrize("kind", list(NodeKind))
def test_every_node_kind_has_a_word(kind):
    """A kind added to the enum without a label here would surface as a raw
    ``time_calculation_tag`` in the one sentence meant for a human."""
    from wdmigrator.migrate.planner import _KIND_WORDS

    assert kind in _KIND_WORDS
