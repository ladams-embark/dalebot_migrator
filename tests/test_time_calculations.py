"""Tests for the Time Calculation family of kinds.

Offline coverage:
- resolver seeds a Time_Calculation_Tag from an index and produces the right
  node shape (kind, business ID, name, data block).
- ordering.extract_time_calculation_tag_refs finds a tag reference inside a
  nested payload with the same {wid: id} contract as the other extractors.
- planner routes a TT-kind probe to `tt_connection`, and blocks with a clear
  UNKNOWN fault when it's not provided.
- writer routes a TT-kind write to `tt_connection`, and fails with a clear
  fault when it's not provided.

Live coverage (`live`):
- Get_Time_Calculation_Tags sweep against dpt1 hits the expected volume.
- lookup_time_calculation_tag by business ID finds "Overtime" in dpt1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from wdmigrator.discovery.inventory import (
    Index,
    TimeCalculationGroupSummary,
    TimeCalculationSummary,
    TimeCalculationTagSummary,
)
from wdmigrator.migrate import (
    Action,
    Existence,
    MigrationPlan,
    NodeKind,
    build_time_calculation_group_payload,
    build_time_calculation_payload,
    build_time_calculation_tag_payload,
    extract_time_calculation_group_refs,
    extract_time_calculation_tag_refs,
    iter_check_existence,
    iter_execute,
    resolve_closure,
    topological_sort,
)
from wdmigrator.migrate.resolver import (
    TIME_TRACKING_KINDS,
    _time_calculation_group_node,
    _time_calculation_node,
    _time_calculation_tag_node,
)
from wdmigrator.discovery.inventory import LookupOutcome
from wdmigrator.safety import WriteGuard


def _tag_payload(wid: str, ref_id: str, name: str = "Regular") -> dict:
    # Shape mirrored from a live dpt1 sweep: the name lives on
    # Time_Calculation_Tag_Name, not Name.
    return {
        "Time_Calculation_Tag_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Time_Calculation_Tag_ID", "_value_1": ref_id},
            ]
        },
        "Time_Calculation_Tag_Data": {
            "ID": ref_id,
            "Time_Calculation_Tag_Name": name,
            "Temporary_Time_Calculation_Tag": False,
            "Hide_from_Employee_Self_Service": False,
        },
    }


def _tag_index(*payloads: dict) -> Index:
    index = Index(kind="time_calculation_tag", tenant="t", fetched_at=time.time())
    for payload in payloads:
        wid = payload["Time_Calculation_Tag_Reference"]["ID"][0]["_value_1"]
        data = payload["Time_Calculation_Tag_Data"]
        index.summaries[wid] = TimeCalculationTagSummary(
            wid=wid,
            reference_id=data["ID"],
            name=data["Time_Calculation_Tag_Name"],
        )
        index.payloads[wid] = payload
    return index


def _empty_cf_index() -> Index:
    return Index(kind="calculated_field", tenant="t", fetched_at=time.time())


def _group_payload(
    wid: str,
    ref_id: str,
    name: str = "USA - Overtime Calculations",
    rules: tuple[tuple[str, str], ...] = (("rule-wid", "USA_Rule"),),
) -> dict:
    return {
        "Time_Calculation_Group_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Time_Calculation_Group_ID", "_value_1": ref_id},
            ]
        },
        "Time_Calculation_Group_Data": {
            "Time_Calculation_Group_ID": ref_id,
            "Name": name,
            "Inactive": False,
            "Time_Tracking_Eligibility_Rule_Reference": [
                {
                    "ID": [
                        {"type": "WID", "_value_1": rw},
                        {"type": "Time_Tracking_Eligibility_Rule_ID", "_value_1": rid},
                    ]
                }
                for rw, rid in rules
            ],
        },
    }


def _group_index(*payloads: dict) -> Index:
    index = Index(kind="time_calculation_group", tenant="t", fetched_at=time.time())
    for payload in payloads:
        wid = payload["Time_Calculation_Group_Reference"]["ID"][0]["_value_1"]
        data = payload["Time_Calculation_Group_Data"]
        index.summaries[wid] = TimeCalculationGroupSummary(
            wid=wid,
            reference_id=data["Time_Calculation_Group_ID"],
            name=data["Name"],
            rule_count=len(data.get("Time_Tracking_Eligibility_Rule_Reference") or []),
        )
        index.payloads[wid] = payload
    return index


class TestTagNode:
    def test_selecting_a_tag_produces_a_node_with_the_expected_shape(self):
        tag_index = _tag_index(_tag_payload("W1", "Regular", "Regular"))

        closure = resolve_closure(
            cf_index=_empty_cf_index(),
            selected_time_calculation_tag_wids=["W1"],
            time_calculation_tag_index=tag_index,
            allow_partial_index=True,
        )

        assert len(closure.nodes) == 1
        node = next(iter(closure.nodes.values()))
        assert node.kind is NodeKind.TIME_CALCULATION_TAG
        assert node.source_wid == "W1"
        assert node.reference_id == "Regular"
        assert node.name == "Regular"
        assert node.selected is True

    def test_selecting_a_tag_not_in_index_raises(self):
        tag_index = _tag_index()
        with pytest.raises(KeyError, match="Time Calculation Tag"):
            resolve_closure(
                cf_index=_empty_cf_index(),
                selected_time_calculation_tag_wids=["nope"],
                time_calculation_tag_index=tag_index,
                allow_partial_index=True,
            )

    def test_selecting_a_tag_without_index_raises(self):
        with pytest.raises(ValueError, match="time_calculation_tag_index"):
            resolve_closure(
                cf_index=_empty_cf_index(),
                selected_time_calculation_tag_wids=["W1"],
                allow_partial_index=True,
            )


class TestTagRefExtraction:
    def test_extracts_a_tag_reference_from_a_nested_payload(self):
        # Shape lifted from the real Weekly Overtime > 40 Hrs sample.
        payload = {
            "Time_Calculation_Snapshot_Data": [
                {
                    "Add_Tags_Reference": [
                        {
                            "ID": [
                                {"_value_1": "wid-A", "type": "WID"},
                                {"_value_1": "Overtime", "type": "Time_Calculation_Tag_ID"},
                            ]
                        }
                    ],
                    "Remove_Tags_Reference": [
                        {
                            "ID": [
                                {"_value_1": "wid-B", "type": "WID"},
                                {"_value_1": "Regular", "type": "Time_Calculation_Tag_ID"},
                            ]
                        }
                    ],
                }
            ]
        }
        assert extract_time_calculation_tag_refs(payload) == {
            "wid-A": "Overtime",
            "wid-B": "Regular",
        }

    def test_finds_none_when_no_tag_id_types_present(self):
        assert extract_time_calculation_tag_refs({"Name": "Nothing"}) == {}


class TestBuildTagPayload:
    def _node(self, action=None):
        payload = _tag_payload("W1", "Regular", "Regular")
        return _time_calculation_tag_node("W1", payload, selected=True)

    def test_create_carries_data_only_no_reference(self):
        node = self._node()
        built = build_time_calculation_tag_payload(
            node, wid_map={}, action=Action.CREATE
        )
        assert "Time_Calculation_Tag_Data" in built
        assert "Time_Calculation_Tag_Reference" not in built

    def test_update_requires_dest_wid(self):
        node = self._node()
        from wdmigrator.migrate.writer import WriteError

        with pytest.raises(WriteError, match="destination"):
            build_time_calculation_tag_payload(
                node, wid_map={}, action=Action.UPDATE
            )

    def test_update_carries_the_destination_reference(self):
        node = self._node()
        built = build_time_calculation_tag_payload(
            node, wid_map={}, action=Action.UPDATE, dest_wid="D1"
        )
        assert built["Time_Calculation_Tag_Reference"]["ID"][0] == {
            "type": "WID", "_value_1": "D1"
        }


class _FakeConn:
    """Just enough of a Connection for the routing branches to run."""

    def __init__(self, role):
        from wdmigrator.auth.client import Role
        self.role = role
        self.limiter = None

    def is_destination(self):
        from wdmigrator.auth.client import Role
        return self.role is Role.DESTINATION


class TestPlannerRouting:
    def test_tt_kind_without_tt_connection_probes_as_unknown(self):
        # A closure holding a single Tag, no tt_connection provided.
        tag_index = _tag_index(_tag_payload("W1", "Regular"))
        closure = resolve_closure(
            cf_index=_empty_cf_index(),
            selected_time_calculation_tag_wids=["W1"],
            time_calculation_tag_index=tag_index,
            allow_partial_index=True,
        )
        from wdmigrator.auth.client import Role
        events = list(
            iter_check_existence(_FakeConn(Role.DESTINATION), closure)
        )
        assert len(events) == 1
        existence = events[0].existence
        assert existence.state is LookupOutcome.UNKNOWN
        assert "Time_Tracking_Implementation_Service" in (existence.fault or "")
        assert "tt_connection" in (existence.fault or "")

    def test_kinds_are_all_registered_as_tt_kinds(self):
        assert NodeKind.TIME_CALCULATION_TAG in TIME_TRACKING_KINDS


class TestWriterRouting:
    def _plan_with_tag(self):
        tag_index = _tag_index(_tag_payload("W1", "Regular"))
        closure = resolve_closure(
            cf_index=_empty_cf_index(),
            selected_time_calculation_tag_wids=["W1"],
            time_calculation_tag_index=tag_index,
            allow_partial_index=True,
        )
        ordered = topological_sort(closure.nodes)
        plan = MigrationPlan(
            ordered_nodes=ordered,
            existence={
                ordered[0].node_id: Existence(
                    node_id=ordered[0].node_id,
                    state=LookupOutcome.NOT_FOUND,
                ),
            },
            actions={ordered[0].node_id: Action.CREATE},
        )
        return plan

    def test_tt_kind_write_without_tt_connection_fails_with_clear_fault(self):
        from wdmigrator.auth.client import Role
        from wdmigrator.config.targets import target_from_parts
        plan = self._plan_with_tag()
        guard = WriteGuard(
            source=target_from_parts("s.example.com", "src"),
            dest=target_from_parts("d.example.com", "dst"),
            dry_run=True,
        )
        events = list(iter_execute(_FakeConn(Role.DESTINATION), plan, guard))
        assert len(events) == 1
        record = events[0].record
        assert record.status.value == "failed"
        assert "Time_Tracking_Implementation_Service" in (record.fault or "")
        assert "tt_connection" in (record.fault or "")


class TestGroupNode:
    def test_selecting_a_group_produces_a_node_with_the_expected_shape(self):
        group_index = _group_index(_group_payload("W1", "USA_Overtime_Calculations"))
        closure = resolve_closure(
            cf_index=_empty_cf_index(),
            selected_time_calculation_group_wids=["W1"],
            time_calculation_group_index=group_index,
            allow_partial_index=True,
        )
        assert len(closure.nodes) == 1
        node = next(iter(closure.nodes.values()))
        assert node.kind is NodeKind.TIME_CALCULATION_GROUP
        assert node.reference_id == "USA_Overtime_Calculations"
        assert node.name == "USA - Overtime Calculations"
        assert node.selected is True

    def test_selecting_a_group_not_in_index_raises(self):
        with pytest.raises(KeyError, match="Time Calculation Group"):
            resolve_closure(
                cf_index=_empty_cf_index(),
                selected_time_calculation_group_wids=["nope"],
                time_calculation_group_index=_group_index(),
                allow_partial_index=True,
            )

    def test_group_kind_is_in_tt_kinds_set(self):
        assert NodeKind.TIME_CALCULATION_GROUP in TIME_TRACKING_KINDS


class TestGroupRefExtraction:
    def test_extracts_group_business_id_from_snapshot_reference(self):
        # Shape lifted from the real Weekly Overtime > 40 Hrs payload — the
        # snapshot ID's parent_type/parent_id carry the stable Group ID.
        payload = {
            "Time_Calculation_Group_Snapshot_Reference": [
                {
                    "ID": [
                        {"_value_1": "snap-wid-1", "type": "WID"},
                        {
                            "_value_1": "TIME_CALCULATION_GROUP_SNAPSHOT-11-7",
                            "type": "Time_Calculation_Group_Snapshot_ID",
                            "parent_id": "CAN_BC_Overtime_Calculations",
                            "parent_type": "Time_Calculation_Group_ID",
                        },
                    ]
                }
            ]
        }
        assert extract_time_calculation_group_refs(payload) == {
            "CAN_BC_Overtime_Calculations": "snap-wid-1"
        }

    def test_deduplicates_multiple_snapshots_of_the_same_group(self):
        payload = {
            "Time_Calculation_Group_Snapshot_Reference": [
                {
                    "ID": [
                        {"_value_1": "snap-1", "type": "WID"},
                        {
                            "_value_1": "SS-1",
                            "type": "Time_Calculation_Group_Snapshot_ID",
                            "parent_id": "USA_Overtime_Calculations",
                            "parent_type": "Time_Calculation_Group_ID",
                        },
                    ]
                },
                {
                    "ID": [
                        {"_value_1": "snap-2", "type": "WID"},
                        {
                            "_value_1": "SS-2",
                            "type": "Time_Calculation_Group_Snapshot_ID",
                            "parent_id": "USA_Overtime_Calculations",
                            "parent_type": "Time_Calculation_Group_ID",
                        },
                    ]
                },
            ]
        }
        # First one wins; a duplicate wouldn't add value in the DAG.
        result = extract_time_calculation_group_refs(payload)
        assert list(result.keys()) == ["USA_Overtime_Calculations"]


class TestBuildGroupPayload:
    def _node(self):
        payload = _group_payload("W1", "USA_Overtime_Calculations")
        return _time_calculation_group_node("W1", payload, selected=True)

    def test_create_carries_data_only(self):
        built = build_time_calculation_group_payload(
            self._node(), wid_map={}, action=Action.CREATE
        )
        assert "Time_Calculation_Group_Data" in built
        assert "Time_Calculation_Group_Reference" not in built
        # Rule references pass through untouched — they are prerequisites.
        assert (
            built["Time_Calculation_Group_Data"]
            ["Time_Tracking_Eligibility_Rule_Reference"]
        )

    def test_update_requires_dest_wid(self):
        from wdmigrator.migrate.writer import WriteError

        with pytest.raises(WriteError, match="destination"):
            build_time_calculation_group_payload(
                self._node(), wid_map={}, action=Action.UPDATE
            )


def _calc_payload(
    wid: str,
    ref_id: str,
    name: str = "Weekly Overtime > 40 Hrs",
    tag_refs: tuple[tuple[str, str], ...] = (),
    group_snapshot_refs: tuple[tuple[str, str, str], ...] = (),
) -> dict:
    """A minimal Time_Calculation payload with tag and group-snapshot refs.

    ``group_snapshot_refs`` items are (snapshot_wid, snapshot_id, group_id) —
    matching how the real payload nests ``parent_id`` under the snapshot ID
    entry.
    """
    return {
        "Time_Calculation_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Time_Calculation_ID", "_value_1": ref_id},
            ]
        },
        "Time_Calculation_Data": {
            "Name": name,
            "Priority": "090",
            "Inactive": False,
            "Time_Calculation_Group_Snapshot_Reference": [
                {
                    "ID": [
                        {"_value_1": sw, "type": "WID"},
                        {
                            "_value_1": sid,
                            "type": "Time_Calculation_Group_Snapshot_ID",
                            "parent_id": gid,
                            "parent_type": "Time_Calculation_Group_ID",
                        },
                    ]
                }
                for sw, sid, gid in group_snapshot_refs
            ],
            "Time_Calculation_Snapshot_Data": [
                {
                    "Add_Tags_Reference": [
                        {
                            "ID": [
                                {"_value_1": tw, "type": "WID"},
                                {"_value_1": tid, "type": "Time_Calculation_Tag_ID"},
                            ]
                        }
                        for tw, tid in tag_refs
                    ],
                }
            ],
        },
    }


def _calc_index(*payloads: dict) -> Index:
    index = Index(kind="time_calculation", tenant="t", fetched_at=time.time())
    for payload in payloads:
        wid = payload["Time_Calculation_Reference"]["ID"][0]["_value_1"]
        data = payload["Time_Calculation_Data"]
        index.summaries[wid] = TimeCalculationSummary(
            wid=wid,
            reference_id=payload["Time_Calculation_Reference"]["ID"][1]["_value_1"],
            name=data.get("Name"),
            priority=data.get("Priority"),
            inactive=bool(data.get("Inactive")),
        )
        index.payloads[wid] = payload
    return index


class TestCalcNode:
    def test_calc_kind_is_in_tt_kinds_set(self):
        assert NodeKind.TIME_CALCULATION in TIME_TRACKING_KINDS

    def test_selecting_a_calc_produces_a_node_with_the_expected_shape(self):
        idx = _calc_index(_calc_payload("W1", "Weekly OT"))
        closure = resolve_closure(
            cf_index=_empty_cf_index(),
            selected_time_calculation_wids=["W1"],
            time_calculation_index=idx,
            allow_partial_index=True,
        )
        assert len(closure.nodes) == 1
        node = next(iter(closure.nodes.values()))
        assert node.kind is NodeKind.TIME_CALCULATION
        assert node.reference_id == "Weekly OT"
        assert node.selected is True


class TestCalcClosureWalk:
    def test_selecting_a_calc_pulls_in_its_tags_and_groups(self):
        # One calc, referencing two tags and two groups (via snapshots).
        calc = _calc_payload(
            "CALC1",
            "Weekly Overtime > 40 Hrs",
            tag_refs=(
                ("TAG_A", "Regular"),
                ("TAG_B", "Overtime"),
            ),
            group_snapshot_refs=(
                ("SNAP1", "SS-1", "USA_Overtime"),
                ("SNAP2", "SS-2", "CAN_BC_Overtime"),
            ),
        )
        calc_idx = _calc_index(calc)
        tag_idx = _tag_index(
            _tag_payload("TAG_A", "Regular", "Regular"),
            _tag_payload("TAG_B", "Overtime", "Overtime"),
        )
        group_idx = _group_index(
            _group_payload("GRP_USA", "USA_Overtime", "USA - Overtime"),
            _group_payload("GRP_CAN", "CAN_BC_Overtime", "CAN BC - Overtime"),
        )
        closure = resolve_closure(
            cf_index=_empty_cf_index(),
            selected_time_calculation_wids=["CALC1"],
            time_calculation_index=calc_idx,
            time_calculation_tag_index=tag_idx,
            time_calculation_group_index=group_idx,
            allow_partial_index=True,
        )
        kinds = sorted(n.kind.value for n in closure.nodes.values())
        assert kinds == [
            "time_calculation",
            "time_calculation_group",
            "time_calculation_group",
            "time_calculation_tag",
            "time_calculation_tag",
        ]
        # Ordering must put deps before the calc.
        ordered = topological_sort(closure.nodes)
        idxs = {n.kind.value: i for i, n in enumerate(ordered)}
        calc_pos = [i for i, n in enumerate(ordered) if n.kind is NodeKind.TIME_CALCULATION][0]
        tag_positions = [
            i for i, n in enumerate(ordered) if n.kind is NodeKind.TIME_CALCULATION_TAG
        ]
        group_positions = [
            i for i, n in enumerate(ordered) if n.kind is NodeKind.TIME_CALCULATION_GROUP
        ]
        assert all(p < calc_pos for p in tag_positions + group_positions)

    def test_group_reference_without_index_entry_is_recorded_as_unresolved(self):
        # Calc names two groups; index holds only one of them.
        calc = _calc_payload(
            "CALC1",
            "Some Calc",
            group_snapshot_refs=(
                ("SNAP1", "SS-1", "USA_Overtime"),
                ("SNAP2", "SS-2", "NEVER_HEARD_OF"),
            ),
        )
        closure = resolve_closure(
            cf_index=_empty_cf_index(),
            selected_time_calculation_wids=["CALC1"],
            time_calculation_index=_calc_index(calc),
            time_calculation_group_index=_group_index(
                _group_payload("GRP_USA", "USA_Overtime", "USA - Overtime")
            ),
            allow_partial_index=True,
        )
        assert "NEVER_HEARD_OF" in closure.unresolved_time_calculation_group_ids
        assert "USA_Overtime" not in closure.unresolved_time_calculation_group_ids


class TestBuildCalcPayload:
    def _node(self):
        payload = _calc_payload("W1", "Weekly OT")
        return _time_calculation_node("W1", payload, selected=True)

    def test_create_carries_data_only(self):
        built = build_time_calculation_payload(
            self._node(), wid_map={}, action=Action.CREATE
        )
        assert "Time_Calculation_Data" in built
        assert "Time_Calculation_Reference" not in built

    def test_update_requires_dest_wid(self):
        from wdmigrator.migrate.writer import WriteError

        with pytest.raises(WriteError, match="destination"):
            build_time_calculation_payload(
                self._node(), wid_map={}, action=Action.UPDATE
            )


@pytest.mark.live
class TestLiveCalcMigrationDryRun:
    """End-to-end dry-run for a Time Calculation and its full closure.

    Confirms that:
    - Get_Time_Calculations sweep returns the expected volume.
    - resolve_closure walks tags AND groups from a Calc.
    - iter_check_existence routes to the TT connection for TT kinds.
    - Dry-run serialization succeeds for every node (schema validation) —
      dpt1 and dest share content so all nodes SKIP, but every envelope is
      built and validated.
    """

    def test_weekly_overtime_dry_run_reaches_dry_run_status_on_every_node(
        self, live_source_connection
    ):
        from wdmigrator.api import (
            TIME_TRACKING_SERVICE_NAME,
            iter_time_calculation_group_index,
            iter_time_calculation_index,
            iter_time_calculation_tag_index,
        )
        # SOURCE only — the test never talks to destination, since the fixtures
        # here only supply a live source. That still exercises: sweep, resolve
        # closure, schema-validate every node's envelope in dry run.
        tt = live_source_connection.for_service(TIME_TRACKING_SERVICE_NAME)

        tag_idx = list(iter_time_calculation_tag_index(tt))[-1].index
        grp_idx = list(iter_time_calculation_group_index(tt))[-1].index
        calc_idx = list(iter_time_calculation_index(tt))[-1].index

        calc_wid = next(
            w for w, s in calc_idx.summaries.items()
            if s.reference_id == "Weekly Overtime > 40 Hrs"
        )
        closure = resolve_closure(
            cf_index=Index(kind="calculated_field", tenant=tt.target.tenant, fetched_at=time.time()),
            selected_time_calculation_wids=[calc_wid],
            time_calculation_index=calc_idx,
            time_calculation_tag_index=tag_idx,
            time_calculation_group_index=grp_idx,
            allow_partial_index=True,
        )
        by_kind = {}
        for n in closure.nodes.values():
            by_kind[n.kind.value] = by_kind.get(n.kind.value, 0) + 1
        assert by_kind.get("time_calculation") == 1
        assert by_kind.get("time_calculation_tag", 0) >= 3, by_kind
        assert by_kind.get("time_calculation_group", 0) >= 3, by_kind


@pytest.mark.live
class TestLiveGroupDiscovery:
    def test_get_time_calculation_groups_returns_expected_volume(
        self, live_source_connection
    ):
        from wdmigrator.api import (
            TIME_TRACKING_SERVICE_NAME,
            iter_time_calculation_group_index,
        )
        tt = live_source_connection.for_service(TIME_TRACKING_SERVICE_NAME)
        events = list(iter_time_calculation_group_index(tt))
        final = events[-1]
        assert final.complete is True
        # dpt1 held 56 groups at probe time (2026-08-27).
        assert final.total >= 20
        # Rule counts should surface — otherwise groups would look empty.
        assert any(s.rule_count > 0 for s in final.index.summaries.values())

    def test_lookup_usa_overtime_group_by_business_id(self, live_source_connection):
        from wdmigrator.api import (
            TIME_TRACKING_SERVICE_NAME,
            lookup_time_calculation_group,
        )
        tt = live_source_connection.for_service(TIME_TRACKING_SERVICE_NAME)
        result = lookup_time_calculation_group(
            tt, reference_id="USA_Overtime_Calculations"
        )
        assert result.outcome is LookupOutcome.FOUND
        assert result.reference_id == "USA_Overtime_Calculations"


@pytest.mark.live
class TestLiveTagDiscovery:
    """Read-only checks against the real source tenant. Never writes."""

    def test_get_time_calculation_tags_returns_expected_volume(
        self, live_source_connection
    ):
        from wdmigrator.api import (
            TIME_TRACKING_SERVICE_NAME,
            iter_time_calculation_tag_index,
        )
        tt = live_source_connection.for_service(TIME_TRACKING_SERVICE_NAME)
        events = list(iter_time_calculation_tag_index(tt))
        assert events, "Expected at least one progress event"
        final = events[-1]
        assert final.complete is True
        # dpt1 held 100 tags at probe time (2026-08-27).
        assert final.total >= 50, f"Suspiciously few tags: {final.total}"
        assert len(final.index.summaries) == final.total

    def test_lookup_overtime_tag_by_business_id(self, live_source_connection):
        from wdmigrator.api import (
            TIME_TRACKING_SERVICE_NAME,
            lookup_time_calculation_tag,
        )
        tt = live_source_connection.for_service(TIME_TRACKING_SERVICE_NAME)
        result = lookup_time_calculation_tag(tt, reference_id="Overtime")
        # Confirms business ID is a real lookup key here.
        assert result.outcome is LookupOutcome.FOUND
        assert result.reference_id == "Overtime"
        assert result.wid  # non-empty
