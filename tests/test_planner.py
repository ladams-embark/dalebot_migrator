"""Tests for conflict classification — the gate before any write.

Offline, against a fake destination. The behaviours pinned here are the ones
whose failure is silent and unrecoverable: mistaking an error for "absent"
(duplicates), skipping a dependency that isn't there (dangling references), and
forgetting to seed destination WIDs (references to objects that don't exist).
"""

import time
from types import SimpleNamespace

import pytest

from wdmigrator.discovery.inventory import (
    CalculatedFieldSummary,
    Index,
    LookupOutcome,
)
from wdmigrator.migrate.planner import (
    Action,
    Existence,
    build_plan,
    default_action,
    iter_check_existence,
    probe_node,
    validate_plan,
)
from wdmigrator.migrate.resolver import Closure, NodeKind, node_id_for, resolve_closure

NOT_FOUND_FAULT = (
    "Validation error occurred. Invalid ID value.  'X' is not a valid ID "
    "value for type = 'Calculated_Field_ID'"
)


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


class FakeDestination:
    """A destination whose answer per reference_id is scripted.

    Values: ("found", dest_wid) | "missing" | ("fault", message)
    """

    def __init__(self, responses=None, report_names=None):
        self.responses = responses or {}
        self.report_names = report_names or {}
        self.probed = []
        self.limiter = SimpleNamespace(wait=lambda: 0.0)
        self.target = SimpleNamespace(tenant="dest_tenant")
        self.service = SimpleNamespace(
            Get_Calculated_Fields=self._get_cf,
            Get_Tenanted_Report_Definitions=self._get_report,
        )

    def _answer(self, kwargs, id_type, collection):
        refs = kwargs["Request_References"]
        ref = next(iter(refs.values()))[0]
        ref_id = next(e["_value_1"] for e in ref["ID"] if e["type"] == id_type)
        self.probed.append(ref_id)

        answer = self.responses.get(ref_id, "missing")
        if answer == "missing":
            raise Exception(NOT_FOUND_FAULT)
        if isinstance(answer, tuple) and answer[0] == "fault":
            raise Exception(answer[1])

        _, dest_wid = answer
        return {
            "Response_Data": {
                collection: [
                    {
                        f"{collection}_Reference": {
                            "ID": [
                                {"type": "WID", "_value_1": dest_wid},
                                {"type": id_type, "_value_1": ref_id},
                            ]
                        }
                    }
                ]
            }
        }

    def _get_cf(self, **kwargs):
        return self._answer(kwargs, "Calculated_Field_ID", "Calculated_Field")

    def _get_report(self, **kwargs):
        """Reports are matched by NAME — Custom_Report_ID is not a usable key.

        `report_names` maps a name to the number of destination reports with
        that name, mirroring Response_Results.Total_Results.
        """
        name = (kwargs.get("Request_Criteria") or {}).get("Report_Name")
        self.probed.append(name)
        count = self.report_names.get(name, 0)
        items = [
            {
                "Tenanted_Report_Definition_Reference": {
                    "ID": [
                        {"type": "WID", "_value_1": f"DEST_{name}_{i}"},
                        {"type": "Custom_Report_ID", "_value_1": name},
                    ]
                }
            }
            for i in range(min(count, 5))
        ]
        return {
            "Response_Results": {"Total_Results": count, "Total_Pages": 1, "Page": 1},
            "Response_Data": {"Tenanted_Report_Definition": items},
        }

    def redact(self, text):
        return text


def closure_of(*payloads, selected=None):
    index = cf_index(*payloads)
    selected = selected or [
        payloads[0]["Calculated_Field_Reference"]["ID"][0]["_value_1"]
    ]
    return resolve_closure(
        cf_index=index,
        selected_field_wids=selected,
        expected_index_size=len(index),
    )


def probe_all(connection, closure):
    return {p.node.node_id: p.existence for p in iter_check_existence(connection, closure)}


class TestDefaultActions:
    def test_missing_defaults_to_create(self):
        assert default_action(Existence("n", LookupOutcome.NOT_FOUND)) is Action.CREATE

    def test_existing_defaults_to_skip_not_update(self):
        """Overwriting destination config is the destructive direction, and
        replace-vs-merge semantics for Put are still unverified."""
        assert default_action(Existence("n", LookupOutcome.FOUND, "W1")) is Action.SKIP

    def test_unknown_defaults_to_skip(self):
        assert default_action(Existence("n", LookupOutcome.UNKNOWN)) is Action.SKIP


class TestProbing:
    def test_missing_object_is_marked_create(self):
        closure = closure_of(cf_payload("W1", "CF_A"))
        plan = build_plan(closure, probe_all(FakeDestination(), closure))
        assert plan.counts()["create"] == 1

    def test_existing_object_is_detected_and_skipped(self):
        closure = closure_of(cf_payload("W1", "CF_A"))
        dest = FakeDestination({"CF_A": ("found", "DEST_W1")})
        plan = build_plan(closure, probe_all(dest, closure))
        assert plan.counts()["skip"] == 1

    def test_probe_uses_the_cross_tenant_business_id_not_the_wid(self):
        """A source WID is meaningless in another tenant."""
        closure = closure_of(cf_payload("W1", "CF_A"))
        dest = FakeDestination()
        probe_all(dest, closure)
        assert dest.probed == ["CF_A"]

    def test_an_unexpected_fault_is_unknown_not_missing(self):
        """The whole safety story: an entitlement error must not read as
        'safe to create'."""
        closure = closure_of(cf_payload("W1", "CF_A"))
        dest = FakeDestination(
            {"CF_A": ("fault", "The web service or version is invalid")}
        )
        plan = build_plan(closure, probe_all(dest, closure))
        assert plan.existence[node_id_for(NodeKind.CALCULATED_FIELD, "W1")].is_unknown
        assert plan.counts()["create"] == 0

    def test_node_without_a_reference_id_is_unknown(self):
        from wdmigrator.migrate.resolver import Node

        node = Node(
            node_id="calculated_field:W1",
            kind=NodeKind.CALCULATED_FIELD,
            source_wid="W1",
            reference_id=None,
            name="No ID",
            payload={},
        )
        assert probe_node(FakeDestination(), node).is_unknown

    def test_progress_is_emitted_per_object(self):
        closure = closure_of(
            cf_payload("W1", "CF_A", refs=["W2"]), cf_payload("W2", "CF_B")
        )
        events = list(iter_check_existence(FakeDestination(), closure))
        assert [e.checked for e in events] == [1, 2]
        assert events[-1].fraction == 1.0

    def test_probing_can_be_cancelled(self):
        closure = closure_of(
            cf_payload("W1", "CF_A", refs=["W2"]), cf_payload("W2", "CF_B")
        )
        dest = FakeDestination()
        sweep = iter_check_existence(dest, closure)
        next(sweep)
        sweep.close()
        assert len(dest.probed) == 1


class TestReportProbing:
    """Reports are matched by name because Custom_Report_ID is not a usable
    lookup key — verified against 18 of 18 sampled reports on a live tenant."""

    def report_closure(self, name="My Report"):
        index = cf_index()
        payload = {
            "Tenanted_Report_Definition_Reference": {
                "ID": [
                    {"type": "WID", "_value_1": "r1"},
                    {"type": "Custom_Report_ID", "_value_1": name},
                ]
            },
            "Tenanted_Report_Definition_Data": {"Name": name},
        }
        return resolve_closure(
            cf_index=index,
            selected_reports={"r1": payload},
            expected_index_size=0,
        )

    def test_report_is_probed_by_name(self):
        closure = self.report_closure("Headcount Summary")
        dest = FakeDestination()
        probe_all(dest, closure)
        assert dest.probed == ["Headcount Summary"]

    def test_absent_report_is_create(self):
        closure = self.report_closure("Headcount Summary")
        plan = build_plan(closure, probe_all(FakeDestination(), closure))
        assert plan.counts()["create"] == 1

    def test_existing_report_is_found_and_seeds_its_dest_wid(self):
        closure = self.report_closure("Headcount Summary")
        dest = FakeDestination(report_names={"Headcount Summary": 1})
        plan = build_plan(closure, probe_all(dest, closure))
        assert plan.counts()["skip"] == 1
        assert plan.wid_map["r1"] == "DEST_Headcount Summary_0"

    def test_a_duplicated_report_name_is_unknown_not_a_guess(self):
        """7 of 999 sampled reports shared a name. Overwriting the wrong one
        cannot be undone, so ambiguity must never resolve to a match."""
        closure = self.report_closure("Consolidated Balance Sheet")
        dest = FakeDestination(report_names={"Consolidated Balance Sheet": 2})
        plan = build_plan(closure, probe_all(dest, closure))
        existence = plan.existence["report:r1"]
        assert existence.is_unknown
        assert "2 reports" in existence.fault

    def test_a_report_with_no_name_is_unknown(self):
        index = cf_index()
        payload = {
            "Tenanted_Report_Definition_Reference": {
                "ID": [{"type": "WID", "_value_1": "r1"}]
            },
            "Tenanted_Report_Definition_Data": {"Name": None},
        }
        closure = resolve_closure(
            cf_index=index, selected_reports={"r1": payload}, expected_index_size=0
        )
        plan = build_plan(closure, probe_all(FakeDestination(), closure))
        assert plan.existence["report:r1"].is_unknown

    def test_writing_an_ambiguously_named_report_is_blocked(self):
        closure = self.report_closure("Consolidated Balance Sheet")
        dest = FakeDestination(report_names={"Consolidated Balance Sheet": 2})
        plan = build_plan(
            closure, probe_all(dest, closure), overrides={"report:r1": Action.CREATE}
        )
        assert any("Cannot determine" in b.title for b in validate_plan(plan))


class TestWidMapSeeding:
    def test_existing_objects_seed_the_wid_map(self):
        """Without this, a skipped dependency leaves dependents pointing at a
        source WID that means nothing in the destination."""
        closure = closure_of(cf_payload("W1", "CF_A"))
        dest = FakeDestination({"CF_A": ("found", "DEST_W1")})
        plan = build_plan(closure, probe_all(dest, closure))
        assert plan.wid_map == {"W1": "DEST_W1"}

    def test_missing_objects_do_not_seed_the_map(self):
        closure = closure_of(cf_payload("W1", "CF_A"))
        plan = build_plan(closure, probe_all(FakeDestination(), closure))
        assert plan.wid_map == {}

    def test_a_skipped_existing_dependency_is_still_mapped(self):
        closure = closure_of(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        dest = FakeDestination({"CF_C": ("found", "DEST_CHILD")})
        plan = build_plan(closure, probe_all(dest, closure))
        assert plan.wid_map["child"] == "DEST_CHILD"
        assert plan.action_for(closure.nodes["calculated_field:child"]) is Action.SKIP


class TestOverrides:
    def test_user_override_wins_over_the_default(self):
        closure = closure_of(cf_payload("W1", "CF_A"))
        dest = FakeDestination({"CF_A": ("found", "DEST_W1")})
        node_id = node_id_for(NodeKind.CALCULATED_FIELD, "W1")
        plan = build_plan(
            closure, probe_all(dest, closure), overrides={node_id: Action.UPDATE}
        )
        assert plan.actions[node_id] is Action.UPDATE


class TestValidation:
    def test_valid_plan_has_no_blockers(self):
        closure = closure_of(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        plan = build_plan(closure, probe_all(FakeDestination(), closure))
        assert validate_plan(plan) == []

    def test_skipping_a_missing_dependency_blocks(self):
        """The dangling-reference case: parent created, child never written."""
        closure = closure_of(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        existence = probe_all(FakeDestination(), closure)
        plan = build_plan(
            closure,
            existence,
            overrides={node_id_for(NodeKind.CALCULATED_FIELD, "child"): Action.SKIP},
        )
        blockers = validate_plan(plan)
        assert any("still needed" in b.title for b in blockers)

    def test_skipping_a_dependency_that_already_exists_is_fine(self):
        closure = closure_of(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        dest = FakeDestination({"CF_C": ("found", "DEST_CHILD")})
        plan = build_plan(closure, probe_all(dest, closure))
        assert validate_plan(plan) == []

    def test_skipping_a_missing_dependency_nobody_needs_is_fine(self):
        closure = closure_of(cf_payload("W1", "CF_A"), cf_payload("W2", "CF_B"),
                             selected=["W1", "W2"])
        existence = probe_all(FakeDestination(), closure)
        plan = build_plan(
            closure,
            existence,
            overrides={node_id_for(NodeKind.CALCULATED_FIELD, "W2"): Action.SKIP},
        )
        assert not any("still needed" in b.title for b in validate_plan(plan))

    def test_writing_an_unknown_object_blocks(self):
        closure = closure_of(cf_payload("W1", "CF_A"))
        dest = FakeDestination({"CF_A": ("fault", "Permission denied")})
        node_id = node_id_for(NodeKind.CALCULATED_FIELD, "W1")
        plan = build_plan(
            closure, probe_all(dest, closure), overrides={node_id: Action.CREATE}
        )
        assert any("Cannot determine" in b.title for b in validate_plan(plan))

    def test_an_all_skip_plan_blocks_as_a_no_op(self):
        closure = closure_of(cf_payload("W1", "CF_A"))
        dest = FakeDestination({"CF_A": ("found", "DEST_W1")})
        plan = build_plan(closure, probe_all(dest, closure))
        assert any(b.title == "Nothing to write" for b in validate_plan(plan))

    def test_every_blocker_tells_the_user_how_to_fix_it(self):
        closure = closure_of(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        plan = build_plan(
            closure,
            probe_all(FakeDestination(), closure),
            overrides={node_id_for(NodeKind.CALCULATED_FIELD, "child"): Action.SKIP},
        )
        for blocker in validate_plan(plan):
            assert blocker.title and blocker.detail and blocker.remedy


class TestPlanHash:
    def _plan(self, overrides=None):
        closure = closure_of(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        return build_plan(closure, probe_all(FakeDestination(), closure), overrides)

    def test_identical_plans_hash_the_same(self):
        assert self._plan().plan_hash() == self._plan().plan_hash()

    def test_changing_an_action_changes_the_hash(self):
        """Editing decisions must invalidate a prior dry-run approval."""
        base = self._plan()
        edited = self._plan(
            overrides={node_id_for(NodeKind.CALCULATED_FIELD, "child"): Action.SKIP}
        )
        assert base.plan_hash() != edited.plan_hash()


class TestOrdering:
    def test_plan_preserves_child_most_first_order(self):
        closure = closure_of(
            cf_payload("parent", "CF_P", refs=["child"]), cf_payload("child", "CF_C")
        )
        plan = build_plan(closure, probe_all(FakeDestination(), closure))
        assert [n.source_wid for n in plan.ordered_nodes] == ["child", "parent"]


@pytest.mark.live
@pytest.mark.dest
class TestLiveProbing:
    """Read-only probing. Never writes.

    Uses the source connection: with source and destination pointed at the same
    tenant there is nothing extra to prove by using a second client, and this
    keeps the suite from holding a writable one.
    """

    def test_a_known_field_probes_as_existing(self, live_source_connection):
        from wdmigrator.migrate.resolver import Node

        node = Node(
            node_id="calculated_field:x",
            kind=NodeKind.CALCULATED_FIELD,
            source_wid="x",
            reference_id="PV_Global_Currency",
            name="PV Global Currency",
            payload={},
        )
        assert probe_node(live_source_connection, node).exists

    def test_an_absent_field_probes_as_not_found(self, live_source_connection):
        from wdmigrator.migrate.resolver import Node

        node = Node(
            node_id="calculated_field:x",
            kind=NodeKind.CALCULATED_FIELD,
            source_wid="x",
            reference_id="WDMIGRATOR_NO_SUCH_FIELD_XYZ",
            name="nope",
            payload={},
        )
        existence = probe_node(live_source_connection, node)
        assert existence.state is LookupOutcome.NOT_FOUND, existence.fault

    def test_custom_report_id_is_still_rejected_as_a_lookup_key(
        self, live_source_connection
    ):
        """Pins the quirk the report design works around.

        The API returns Custom_Report_ID on every report reference but refuses
        it in Request_References. If Workday ever fixes that, this test fails
        and we can switch reports back to stable-ID matching instead of names.
        """
        from wdmigrator.discovery import iter_report_index, ids_of, lookup_report

        sweep = iter_report_index(live_source_connection)
        first = next(sweep)
        sweep.close()

        wid, payload = next(iter(first.index.payloads.items()))
        report_id = ids_of(payload["Tenanted_Report_Definition_Reference"]).get(
            "Custom_Report_ID"
        )
        assert report_id, "reports should carry a Custom_Report_ID"

        by_id = lookup_report(live_source_connection, custom_report_id=report_id)
        by_wid = lookup_report(live_source_connection, wid=wid)

        assert by_wid.outcome is LookupOutcome.FOUND, "the report does exist"
        assert by_id.outcome is LookupOutcome.NOT_FOUND, (
            "Custom_Report_ID now works as a lookup key — reports could use "
            "stable-ID matching instead of name matching. See planner.probe_node."
        )

    def test_report_name_lookup_works_as_the_existence_check(
        self, live_source_connection
    ):
        from wdmigrator.discovery import iter_report_index, lookup_report_by_name

        sweep = iter_report_index(live_source_connection)
        first = next(sweep)
        sweep.close()

        payload = next(iter(first.index.payloads.values()))
        name = payload["Tenanted_Report_Definition_Data"]["Name"]

        assert lookup_report_by_name(live_source_connection, name).outcome is (
            LookupOutcome.FOUND
        )
        assert lookup_report_by_name(
            live_source_connection, "WDMIGRATOR No Such Report XYZ 123"
        ).outcome is LookupOutcome.NOT_FOUND


class TestUnresolvedDependenciesBlockThePlan:
    """The check used to live only in the Streamlit Resolve step, so a CLI or a
    script could build a plan with known-missing dependencies and write it."""

    def test_unresolved_calculated_field_blocks(self):
        closure = Closure(nodes={}, unresolved_reference_ids={"CF_GONE"})
        plan = build_plan(closure, {})
        titles = [b.title for b in validate_plan(plan)]
        assert any("calculated field(s) could not be resolved" in t for t in titles)

    def test_unresolved_measure_blocks(self):
        closure = Closure(nodes={}, unresolved_measure_ids={"ARITH-Gone-1"})
        plan = build_plan(closure, {})
        blockers = validate_plan(plan)
        assert any("calculated measure(s) could not be resolved" in b.title for b in blockers)
        assert any("ARITH-Gone-1" in b.detail for b in blockers)

    def test_a_clean_closure_adds_no_such_blocker(self):
        closure = Closure(nodes={})
        plan = build_plan(closure, {})
        titles = [b.title for b in validate_plan(plan)]
        assert not any("could not be resolved" in t for t in titles)

    def test_the_ids_are_carried_onto_the_plan(self):
        closure = Closure(
            nodes={}, unresolved_reference_ids={"A"}, unresolved_measure_ids={"B"}
        )
        plan = build_plan(closure, {})
        assert plan.unresolved_reference_ids == frozenset({"A"})
        assert plan.unresolved_measure_ids == frozenset({"B"})
