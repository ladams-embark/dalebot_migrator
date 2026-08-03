"""Tests for the write path — the only module that can damage a tenant.

Entirely offline. Every test asserts the destination service was called zero
times unless the test is specifically exercising a live-mode code path against
a fake, and even then nothing leaves the process.

There is no `live` test in this file and there must never be one. The service
has no delete operation, so a test that writes leaves permanent residue in a
tenant with no way to clean up.
"""

import time
from types import SimpleNamespace

import pytest
from zeep.exceptions import Fault

from wdmigrator.auth.client import Role
from wdmigrator.config.targets import target_from_parts
from wdmigrator.discovery.inventory import CalculatedFieldSummary, Index, LookupOutcome
from wdmigrator.migrate.planner import Action, Existence, build_plan
from wdmigrator.migrate.resolver import NodeKind, node_id_for, resolve_closure
from wdmigrator.migrate.writer import (
    ExceptionDetail,
    WriteError,
    WriteStatus,
    build_calculated_field_payload,
    build_owner_reference,
    build_report_payload,
    extract_exceptions,
    is_failure,
    iter_execute,
    operation_for,
    summarise,
    write_node,
)
from wdmigrator.safety import GuardViolation, WriteGuard

SOURCE = target_from_parts("impl-services1.wd12.myworkday.com", "source_tenant")
DEST = target_from_parts("impl-services1.wd12.myworkday.com", "dest_tenant")


def live_guard(**overrides):
    defaults = dict(
        source=SOURCE,
        dest=DEST,
        dry_run=False,
        plan_hash="abc123",
        dry_run_reviewed=True,
        source_verified=True,
        dest_verified=True,
    )
    return WriteGuard(**{**defaults, **overrides})


def dry_guard():
    return WriteGuard(source=SOURCE, dest=DEST, dry_run=True)


def cf_payload(wid, ref_id, name="Field", refs=()):
    return {
        "Calculated_Field_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Calculated_Field_ID", "_value_1": ref_id},
            ]
        },
        "Calculated_Field_Data": {
            "Calculated_Field_Reference_ID": ref_id,
            "Name": name,
            "Class_Name": "Arithmetic Calculated Field",
            "Operands": [
                {"Field_Reference": {"ID": [{"type": "WID", "_value_1": r}]}}
                for r in refs
            ],
        },
    }


def report_payload_with_filter_instance(wid, name="Report", instance_wid="SRC_INSTANCE"):
    """Shape matches what a live tenant actually returns — confirmed via
    scripts/find_wid_in_report.py: Filter_Instances_Reference is a list."""
    return {
        "Tenanted_Report_Definition_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Custom_Report_ID", "_value_1": name},
            ]
        },
        "Tenanted_Report_Definition_Data": {
            "Name": name,
            "Tenanted_Report_Definition_Top_Level_Filter_Data": {
                "Tenanted_Report_Filter_Data": {
                    "Condition_Item_Data": [
                        {
                            "Relational_Operator_Reference_Data": {
                                "ID": [{"type": "WID", "_value_1": "OP_EQUALS"}]
                            },
                            "Filter_Instances_Reference": [
                                {"ID": [{"type": "WID", "_value_1": instance_wid}]}
                            ],
                            "Ignore_When_No_Target_Value": True,
                        }
                    ]
                }
            },
        },
    }


def report_payload(wid, name="Report", refs=(), owner="source_user"):
    return {
        "Tenanted_Report_Definition_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Custom_Report_ID", "_value_1": name},
            ]
        },
        "Tenanted_Report_Definition_Data": {
            "Name": name,
            "Tenanted_Report_Definition_System_User_Reference": {
                "ID": [{"type": "WorkdayUserName", "_value_1": owner}]
            },
            "Tenanted_Report_Column_Data": [
                {"Field_Reference": {"ID": [{"type": "WID", "_value_1": r}]}}
                for r in refs
            ],
        },
    }


def cf_index(*payloads):
    index = Index(kind="calculated_field", tenant="t", fetched_at=time.time())
    for payload in payloads:
        wid = payload["Calculated_Field_Reference"]["ID"][0]["_value_1"]
        data = payload["Calculated_Field_Data"]
        index.summaries[wid] = CalculatedFieldSummary(
            wid=wid,
            reference_id=data["Calculated_Field_Reference_ID"],
            name=data["Name"],
            class_name=data["Class_Name"],
        )
        index.payloads[wid] = payload
    return index


class FakeDestConnection:
    """A destination that records writes instead of performing them.

    `outcomes` maps operation-call-index to a scripted result:
      ("ok", wid) | ("exceptions", [(classification, message)]) |
      ("fault", msg) | ("timeout", msg) | ("no_wid", None)
    """

    def __init__(self, outcomes=None, role=Role.DESTINATION):
        self.outcomes = outcomes or {}
        self.writes = []
        self.role = role
        self.target = DEST
        self.limiter = SimpleNamespace(wait=lambda: 0.0)
        self.client = SimpleNamespace(
            create_message=lambda service, op, **kw: _FakeNode(op, kw)
        )
        self.service = SimpleNamespace(
            Put_Calculated_Field=lambda **kw: self._put("Put_Calculated_Field", kw),
            Put_Tenanted_Report_Definition=lambda **kw: self._put(
                "Put_Tenanted_Report_Definition", kw
            ),
        )

    def _put(self, operation, kwargs):
        index = len(self.writes)
        self.writes.append((operation, kwargs))
        outcome = self.outcomes.get(index, ("ok", f"DEST_WID_{index}"))
        kind, value = outcome

        if kind == "fault":
            raise Fault(value)
        if kind == "timeout":
            raise ConnectionError(value)

        ref_key = (
            "Tenanted_Report_Definition_Reference"
            if "Report" in operation
            else "Calculated_Field_Reference"
        )
        response = {}
        if kind != "no_wid":
            response[ref_key] = {"ID": [{"type": "WID", "_value_1": value}]}
        if kind == "exceptions":
            response[ref_key] = {"ID": [{"type": "WID", "_value_1": "PARTIAL"}]}
            response["Exceptions_Response_Data"] = [
                {
                    "Exceptions_Data": [
                        {
                            "Exception_Data": [
                                {"Classification": c, "Message": m} for c, m in value
                            ]
                        }
                    ]
                }
            ]
        return response

    def is_destination(self):
        return self.role is Role.DESTINATION

    def redact(self, text):
        return text


class _FakeNode:
    """Stands in for the lxml element zeep would build."""

    def __init__(self, operation, kwargs):
        self.operation = operation
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _fake_envelope(monkeypatch):
    """Dry-run serialization without a real 5.9 MB WSDL parse."""

    def fake(connection, operation, payload):
        return f"<Envelope op='{operation}' keys='{sorted(payload)}'/>"

    monkeypatch.setattr("wdmigrator.migrate.writer.serialize_envelope", fake)


def plan_for(*payloads, selected=None, existence=None, overrides=None, reports=None):
    index = cf_index(*payloads)
    selected = selected or (
        [payloads[0]["Calculated_Field_Reference"]["ID"][0]["_value_1"]]
        if payloads
        else []
    )
    closure = resolve_closure(
        cf_index=index,
        selected_field_wids=selected,
        selected_reports=reports or {},
        expected_index_size=len(index),
    )
    existence = existence or {
        node_id: Existence(node_id, LookupOutcome.NOT_FOUND)
        for node_id in closure.nodes
    }
    return closure, build_plan(closure, existence, overrides)


class TestDryRunNeverWrites:
    """The single most important property in this module."""

    def test_dry_run_makes_zero_destination_calls(self):
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        dest = FakeDestConnection()
        list(iter_execute(dest, plan, dry_guard()))
        assert dest.writes == []

    def test_dry_run_of_a_multi_object_plan_makes_zero_calls(self):
        _, plan = plan_for(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        dest = FakeDestConnection()
        records = [p.record for p in iter_execute(dest, plan, dry_guard())]
        assert dest.writes == []
        assert len(records) == 2
        assert all(r.status is WriteStatus.NOT_ATTEMPTED for r in records)

    def test_dry_run_still_produces_the_envelope_it_would_send(self):
        """A dry run that says 'would PUT X' without the payload is worthless."""
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        records = [p.record for p in iter_execute(FakeDestConnection(), plan, dry_guard())]
        assert records[0].envelope
        assert "Put_Calculated_Field" in records[0].envelope

    def test_dry_run_records_are_marked_as_such(self):
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        records = [p.record for p in iter_execute(FakeDestConnection(), plan, dry_guard())]
        assert all(r.dry_run for r in records)

    def test_dry_run_works_even_against_a_source_role_connection(self):
        """Dry run must be usable before a destination is even configured."""
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        source_conn = FakeDestConnection(role=Role.SOURCE)
        list(iter_execute(source_conn, plan, dry_guard()))
        assert source_conn.writes == []


class TestGuardEnforcement:
    def test_live_execution_is_blocked_when_the_guard_fails(self):
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        dest = FakeDestConnection()
        guard = live_guard(dry_run_reviewed=False)  # no dry run performed
        with pytest.raises(GuardViolation):
            list(iter_execute(dest, plan, guard))
        assert dest.writes == []

    def test_same_tenant_blocks_live_execution(self):
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        dest = FakeDestConnection()
        with pytest.raises(GuardViolation):
            list(iter_execute(dest, plan, live_guard(dest=SOURCE)))
        assert dest.writes == []

    def test_the_guard_is_rechecked_for_every_object_not_just_once(self, monkeypatch):
        """A session can change between two writes; one check at the start is
        not enforcement."""
        calls = []
        real = __import__(
            "wdmigrator.safety", fromlist=["assert_write_allowed"]
        ).assert_write_allowed

        def counting(guard):
            calls.append(guard)
            return real(guard)

        monkeypatch.setattr("wdmigrator.migrate.writer.assert_write_allowed", counting)
        _, plan = plan_for(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        list(iter_execute(FakeDestConnection(), plan, live_guard()))
        assert len(calls) == 2, "guard must be checked once per written object"

    def test_live_write_through_a_source_connection_is_refused(self):
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        source_conn = FakeDestConnection(role=Role.SOURCE)
        with pytest.raises(WriteError, match="DESTINATION"):
            list(iter_execute(source_conn, plan, live_guard()))
        assert source_conn.writes == []


class TestExceptionsResponseData:
    """A 200 with no SOAP fault can still be a failed write."""

    def test_exceptions_are_extracted_from_the_nested_shape(self):
        response = {
            "Exceptions_Response_Data": [
                {
                    "Exceptions_Data": [
                        {
                            "Exception_Data": [
                                {"Classification": "Error", "Message": "Bad field"}
                            ]
                        }
                    ]
                }
            ]
        }
        found = extract_exceptions(response)
        assert found == [ExceptionDetail("Error", "Bad field")]

    def test_missing_or_empty_blocks_yield_nothing(self):
        for response in ({}, {"Exceptions_Response_Data": None},
                         {"Exceptions_Response_Data": []}):
            assert extract_exceptions(response) == []

    def test_any_unrecognised_classification_counts_as_failure(self):
        """Defaulting the other way would record real errors as successes."""
        assert is_failure([ExceptionDetail("Error", "x")])
        assert is_failure([ExceptionDetail(None, "x")])
        assert not is_failure([])

    def test_a_put_returning_exceptions_is_recorded_as_failed(self):
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        dest = FakeDestConnection(
            outcomes={0: ("exceptions", [("Error", "Invalid operand")])}
        )
        record = [p.record for p in iter_execute(dest, plan, live_guard())][0]
        assert record.status is WriteStatus.FAILED
        assert record.exceptions[0].message == "Invalid operand"

    def test_a_failed_write_does_not_register_a_destination_wid(self):
        """Registering it would let downstream payloads reference a bad object."""
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        dest = FakeDestConnection(outcomes={0: ("exceptions", [("Error", "nope")])})
        record = [p.record for p in iter_execute(dest, plan, live_guard())][0]
        assert record.dest_wid is None
        assert plan.wid_map == {}


class TestIndeterminateOutcomes:
    def test_a_transport_failure_is_indeterminate_not_failed(self):
        """A timeout may have committed server-side."""
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        dest = FakeDestConnection(outcomes={0: ("timeout", "read timed out")})
        record = [p.record for p in iter_execute(dest, plan, live_guard())][0]
        assert record.status is WriteStatus.INDETERMINATE
        assert record.needs_reprobe

    def test_a_soap_fault_is_a_definite_failure(self):
        """The server processed and rejected it — the write did not happen."""
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        dest = FakeDestConnection(outcomes={0: ("fault", "Validation error")})
        record = [p.record for p in iter_execute(dest, plan, live_guard())][0]
        assert record.status is WriteStatus.FAILED
        assert not record.needs_reprobe

    def test_a_success_with_no_returned_wid_is_indeterminate(self):
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        dest = FakeDestConnection(outcomes={0: ("no_wid", None)})
        record = [p.record for p in iter_execute(dest, plan, live_guard())][0]
        assert record.status is WriteStatus.INDETERMINATE
        assert "re-probe" in record.fault


class TestWidMapPropagation:
    def test_a_created_objects_wid_feeds_the_next_payload(self):
        """The reason writes are sequential rather than parallel."""
        _, plan = plan_for(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        dest = FakeDestConnection(
            outcomes={0: ("ok", "DEST_CHILD"), 1: ("ok", "DEST_PARENT")}
        )
        list(iter_execute(dest, plan, live_guard()))

        # child written first, then parent referencing the child's NEW wid
        assert [op for op, _ in dest.writes] == ["Put_Calculated_Field"] * 2
        parent_kwargs = dest.writes[1][1]
        operands = parent_kwargs["Calculated_Field_Data"]["Operands"]
        assert operands[0]["Field_Reference"]["ID"][0]["_value_1"] == "DEST_CHILD"

    def test_wid_map_is_seeded_from_already_existing_objects(self):
        closure, _ = plan_for(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        existence = {
            node_id_for(NodeKind.CALCULATED_FIELD, "child"): Existence(
                node_id_for(NodeKind.CALCULATED_FIELD, "child"),
                LookupOutcome.FOUND,
                dest_wid="ALREADY_THERE",
            ),
            node_id_for(NodeKind.CALCULATED_FIELD, "parent"): Existence(
                node_id_for(NodeKind.CALCULATED_FIELD, "parent"),
                LookupOutcome.NOT_FOUND,
            ),
        }
        plan = build_plan(closure, existence)
        dest = FakeDestConnection()
        list(iter_execute(dest, plan, live_guard()))

        assert len(dest.writes) == 1, "the existing child must not be rewritten"
        operands = dest.writes[0][1]["Calculated_Field_Data"]["Operands"]
        assert operands[0]["Field_Reference"]["ID"][0]["_value_1"] == "ALREADY_THERE"


class TestFailureHalting:
    def test_a_failure_stops_subsequent_writes(self):
        """Continuing would create dependents referencing a missing object."""
        _, plan = plan_for(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        dest = FakeDestConnection(outcomes={0: ("fault", "child rejected")})
        records = [p.record for p in iter_execute(dest, plan, live_guard())]
        assert len(dest.writes) == 1
        assert records[0].status is WriteStatus.FAILED
        assert records[1].status is WriteStatus.NOT_ATTEMPTED

    def test_every_object_is_still_reported_after_a_halt(self):
        """A partial migration must be fully accounted for, not silently truncated."""
        _, plan = plan_for(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        dest = FakeDestConnection(outcomes={0: ("fault", "x")})
        records = [p.record for p in iter_execute(dest, plan, live_guard())]
        assert len(records) == len(plan.ordered_nodes)

    def test_an_indeterminate_result_also_halts(self):
        _, plan = plan_for(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        dest = FakeDestConnection(outcomes={0: ("timeout", "x")})
        records = [p.record for p in iter_execute(dest, plan, live_guard())]
        assert records[1].status is WriteStatus.NOT_ATTEMPTED

    def test_stop_on_failure_can_be_disabled(self):
        _, plan = plan_for(cf_payload("W1", "CF_A"), cf_payload("W2", "CF_B"),
                           selected=["W1", "W2"])
        dest = FakeDestConnection(outcomes={0: ("fault", "x")})
        records = [
            p.record
            for p in iter_execute(dest, plan, live_guard(), stop_on_failure=False)
        ]
        assert sum(1 for r in records if r.status is WriteStatus.NOT_ATTEMPTED) == 0


class TestPayloadConstruction:
    def test_create_omits_the_reference_entirely(self):
        """Including a source WID would address the wrong destination object."""
        closure, plan = plan_for(cf_payload("W1", "CF_A"))
        node = closure.nodes[node_id_for(NodeKind.CALCULATED_FIELD, "W1")]
        payload = build_calculated_field_payload(node, {}, action=Action.CREATE)
        assert "Calculated_Field_Reference" not in payload

    def test_update_uses_the_destination_wid(self):
        closure, plan = plan_for(cf_payload("W1", "CF_A"))
        node = closure.nodes[node_id_for(NodeKind.CALCULATED_FIELD, "W1")]
        payload = build_calculated_field_payload(
            node, {}, action=Action.UPDATE, dest_wid="DEST_W1"
        )
        assert payload["Calculated_Field_Reference"]["ID"][0]["_value_1"] == "DEST_W1"

    def test_update_without_a_destination_wid_is_refused(self):
        closure, plan = plan_for(cf_payload("W1", "CF_A"))
        node = closure.nodes[node_id_for(NodeKind.CALCULATED_FIELD, "W1")]
        with pytest.raises(WriteError, match="destination's WID"):
            build_calculated_field_payload(node, {}, action=Action.UPDATE)

    def test_operation_is_chosen_by_node_kind(self):
        closure, _ = plan_for(
            cf_payload("W1", "CF_A"),
            reports={"r1": report_payload("r1")},
        )
        assert operation_for(closure.nodes["calculated_field:W1"]) == (
            "Put_Calculated_Field"
        )
        assert operation_for(closure.nodes["report:r1"]) == (
            "Put_Tenanted_Report_Definition"
        )


class TestOwnerRemapping:
    def _report_node(self):
        closure, _ = plan_for(reports={"r1": report_payload("r1", owner="src_user")})
        return closure.nodes["report:r1"]

    def test_owner_is_replaced_with_the_supplied_reference(self):
        payload = build_report_payload(
            self._report_node(),
            {},
            action=Action.CREATE,
            owner_reference=build_owner_reference(workday_username="dest_user"),
        )
        owner = payload["Tenanted_Report_Definition_Data"][
            "Tenanted_Report_Definition_System_User_Reference"
        ]
        assert owner["ID"][0]["_value_1"] == "dest_user"

    def test_owner_is_stripped_when_none_is_supplied(self):
        """A source user reference is meaningless in the destination; leaving it
        would fail the write on an unresolvable user."""
        payload = build_report_payload(self._report_node(), {}, action=Action.CREATE)
        assert (
            "Tenanted_Report_Definition_System_User_Reference"
            not in payload["Tenanted_Report_Definition_Data"]
        )

    def test_source_payload_is_not_mutated_by_owner_remapping(self):
        node = self._report_node()
        build_report_payload(
            node, {}, action=Action.CREATE,
            owner_reference=build_owner_reference(workday_username="dest_user"),
        )
        original = node.payload["Tenanted_Report_Definition_Data"][
            "Tenanted_Report_Definition_System_User_Reference"
        ]
        assert original["ID"][0]["_value_1"] == "src_user"

    def test_owner_reference_accepts_username_or_wid_but_not_both(self):
        assert build_owner_reference(workday_username="u")["ID"][0]["type"] == (
            "WorkdayUserName"
        )
        assert build_owner_reference(wid="W")["ID"][0]["type"] == "WID"
        with pytest.raises(ValueError):
            build_owner_reference(workday_username="u", wid="W")


class TestFilterInstanceStripping:
    """Filter_Instances_Reference points at a specific business-object
    instance in the source tenant — tenant-specific data this tool has no
    way to verify or create. Confirmed live 2026-08-03 that Workday rejects
    an unresolvable one outright and that Ignore_When_No_Target_Value does
    not suppress that validation, so both are stripped unconditionally."""

    def _report_node(self, instance_wid="SRC_INSTANCE"):
        closure, _ = plan_for(
            reports={"r1": report_payload_with_filter_instance("r1", instance_wid=instance_wid)}
        )
        return closure.nodes["report:r1"]

    def test_filter_instance_reference_is_stripped(self):
        payload = build_report_payload(self._report_node(), {}, action=Action.CREATE)
        condition = payload["Tenanted_Report_Definition_Data"][
            "Tenanted_Report_Definition_Top_Level_Filter_Data"
        ]["Tenanted_Report_Filter_Data"]["Condition_Item_Data"][0]
        assert "Filter_Instances_Reference" not in condition
        assert "Ignore_When_No_Target_Value" not in condition

    def test_other_condition_fields_survive_the_strip(self):
        """Only the instance reference and its now-meaningless flag go —
        everything else on the condition (the operator, in this case) stays."""
        payload = build_report_payload(self._report_node(), {}, action=Action.CREATE)
        condition = payload["Tenanted_Report_Definition_Data"][
            "Tenanted_Report_Definition_Top_Level_Filter_Data"
        ]["Tenanted_Report_Filter_Data"]["Condition_Item_Data"][0]
        assert condition["Relational_Operator_Reference_Data"]["ID"][0]["_value_1"] == "OP_EQUALS"

    def test_source_payload_is_not_mutated_by_stripping(self):
        node = self._report_node()
        build_report_payload(node, {}, action=Action.CREATE)
        condition = node.payload["Tenanted_Report_Definition_Data"][
            "Tenanted_Report_Definition_Top_Level_Filter_Data"
        ]["Tenanted_Report_Filter_Data"]["Condition_Item_Data"][0]
        assert "Filter_Instances_Reference" in condition

    def test_a_report_with_no_filters_is_unaffected(self):
        payload = build_report_payload(
            plan_for(reports={"r1": report_payload("r1")})[0].nodes["report:r1"],
            {},
            action=Action.CREATE,
        )
        assert "Tenanted_Report_Definition_Top_Level_Filter_Data" not in payload[
            "Tenanted_Report_Definition_Data"
        ]


class TestSkippedObjects:
    def test_skipped_objects_are_not_written(self):
        closure, _ = plan_for(cf_payload("W1", "CF_A"))
        node_id = node_id_for(NodeKind.CALCULATED_FIELD, "W1")
        existence = {node_id: Existence(node_id, LookupOutcome.FOUND, dest_wid="D1")}
        plan = build_plan(closure, existence)
        dest = FakeDestConnection()
        records = [p.record for p in iter_execute(dest, plan, live_guard())]
        assert dest.writes == []
        assert records[0].status is WriteStatus.SKIPPED
        assert records[0].dest_wid == "D1"


class TestProgressAndSummary:
    def test_progress_is_emitted_per_object(self):
        _, plan = plan_for(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        events = list(iter_execute(FakeDestConnection(), plan, dry_guard()))
        assert [e.position for e in events] == [1, 2]
        assert events[-1].fraction == 1.0

    def test_execution_can_be_cancelled_between_objects(self):
        _, plan = plan_for(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        dest = FakeDestConnection()
        run = iter_execute(dest, plan, live_guard())
        next(run)
        run.close()
        assert len(dest.writes) == 1, "cancel must stop further writes"

    def test_summarise_counts_by_status(self):
        _, plan = plan_for(cf_payload("W1", "CF_A"))
        records = [p.record for p in iter_execute(FakeDestConnection(), plan, dry_guard())]
        assert summarise(records)["not_attempted"] == 1


class TestNoLiveTestsExist:
    def test_no_test_in_this_module_is_marked_live_or_dest(self, request):
        """Writing in a test would leave permanent residue — there is no delete.

        Checks the markers pytest actually collected rather than grepping the
        source, so it cannot be fooled by the marker name appearing in a string
        or a comment.
        """
        offenders = [
            item.nodeid
            for item in request.session.items
            if item.module.__name__ == __name__
            and ({"live", "dest"} & set(item.keywords))
        ]
        assert offenders == [], (
            "the write path must never be exercised against a real tenant: "
            f"{offenders}"
        )
