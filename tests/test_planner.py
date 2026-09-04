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
    CalculatedMeasureSummary,
    Index,
    LookupOutcome,
    calculated_field_match_index,
    calculated_field_shape,
    calculated_measure_match_index,
    calculated_measure_shape,
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
from wdmigrator.migrate.ordering import substitute_reference_ids
from wdmigrator.migrate.resolver import (
    Closure,
    Node,
    NodeKind,
    node_id_for,
    resolve_closure,
)

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
            Get_Calculated_Measures=self._get_measure,
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

    def _get_measure(self, **kwargs):
        return self._answer(
            kwargs, "BI_Calculated_Measure_ID", "Calculated_Measure"
        )

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

    def test_shell_dashboard_defaults_to_update_not_skip(self):
        """A FOUND-but-shell dashboard is the one FOUND case that must not
        SKIP. Skipping it silently leaves a broken dashboard in the
        destination that every subsequent run then also skips — HANDOFF flags
        this as the escape hatch that used to require `--force-update` in
        the scripts and nothing in the wizard."""
        shell = Existence("dashboard:D1", LookupOutcome.FOUND, "W1", is_shell=True)
        assert default_action(shell) is Action.UPDATE

    def test_prefer_update_defaults_to_update(self):
        """Workday-delivered dashboards already exist and cannot be created."""
        found = Existence(
            "dashboard:DDB1", LookupOutcome.FOUND, "W1", prefer_update=True
        )
        assert default_action(found) is Action.UPDATE


class TestShellDashboardDetection:
    """A dashboard that failed mid-write leaves admin config in place but no
    worklets, and would otherwise re-probe FOUND forever. `probe_node`
    detects the shell and routes to UPDATE; the writer already supports
    UPDATE on dashboards (verified live 2026-08-13, HANDOFF.md)."""

    def _dashboard_node(self, reference_id="Commit - HR Dashboard"):
        return Node(
            node_id=f"dashboard_tabbed:D1",
            kind=NodeKind.DASHBOARD_TABBED,  # type: ignore[attr-defined]
            source_wid="D1",
            reference_id=reference_id,
            name=reference_id,
            payload={},
        )

    def _dest(self, response_item):
        """A minimal FakeDestination that returns one dashboard on lookup."""
        dest = FakeDestination()

        def _get(**kwargs):
            return {
                "Response_Data": {
                    "Custom_Dashboard_with_Tabs": [response_item]
                }
            }

        dest.service = SimpleNamespace(Get_Custom_Dashboards_with_Tabs=_get)
        return dest

    def test_dashboard_with_worklets_is_a_normal_found_not_a_shell(self):
        node = self._dashboard_node()
        item = {
            "Custom_Dashboard_with_Tabs_Reference": {
                "ID": [
                    {"type": "WID", "_value_1": "DEST_D1"},
                    {"type": "Custom_Landing_Page_Group_ID", "_value_1": node.reference_id},
                ]
            },
            "Custom_Dashboard_with_Tabs_Data": {
                "Custom_Dashboard_Tab_Data": [
                    {"Worklets_Data": [{"Worklet_Reference": "..."}]}
                ]
            },
        }
        existence = probe_node(self._dest(item), node)
        assert existence.state is LookupOutcome.FOUND
        assert existence.is_shell is False

    def test_dashboard_with_empty_tabs_is_a_shell(self):
        node = self._dashboard_node()
        item = {
            "Custom_Dashboard_with_Tabs_Reference": {
                "ID": [
                    {"type": "WID", "_value_1": "DEST_D1"},
                    {"type": "Custom_Landing_Page_Group_ID", "_value_1": node.reference_id},
                ]
            },
            "Custom_Dashboard_with_Tabs_Data": {
                "Custom_Dashboard_Tab_Data": [{"Tab_Name": "Overview"}]
            },
        }
        existence = probe_node(self._dest(item), node)
        assert existence.state is LookupOutcome.FOUND
        assert existence.is_shell is True
        assert existence.dest_wid == "DEST_D1"

    def test_dashboard_with_no_data_block_at_all_is_a_shell(self):
        node = self._dashboard_node()
        item = {
            "Custom_Dashboard_with_Tabs_Reference": {
                "ID": [
                    {"type": "WID", "_value_1": "DEST_D1"},
                    {"type": "Custom_Landing_Page_Group_ID", "_value_1": node.reference_id},
                ]
            },
        }
        existence = probe_node(self._dest(item), node)
        assert existence.is_shell is True


class TestDeliveredDashboardProbe:
    """Workday-owned dashboards cannot be created. FOUND must UPDATE;
    a miss is UNKNOWN rather than CREATE."""

    def _node(self):
        return Node(
            node_id="dashboard:DDB1",
            kind=NodeKind.DASHBOARD,
            source_wid="DDB1",
            reference_id="HOME",
            name="HOME",
            payload={
                "Workday_Delivered_Dashboard_without_Tabs_Reference": {
                    "ID": [
                        {"type": "WID", "_value_1": "DDB1"},
                        {"type": "Landing_Page_ID", "_value_1": "HOME"},
                    ]
                },
                "Workday_Delivered_Dashboard_without_Tabs_Data": {
                    "Worklets_Data": [{"Worklet_Reference": "..."}]
                },
            },
        )

    def test_found_prefers_update(self):
        dest = FakeDestination()

        def _get(**kwargs):
            return {
                "Response_Data": {
                    "Workday_Delivered_Dashboard_without_Tabs": [
                        {
                            "Workday_Delivered_Dashboard_without_Tabs_Reference": {
                                "ID": [
                                    {"type": "WID", "_value_1": "DEST_DDB1"},
                                    {"type": "Landing_Page_ID", "_value_1": "HOME"},
                                ]
                            },
                            "Workday_Delivered_Dashboard_without_Tabs_Data": {
                                "Worklets_Data": [{"Worklet_Reference": "..."}]
                            },
                        }
                    ]
                }
            }

        dest.service = SimpleNamespace(
            Get_Workday_Delivered_Dashboards_without_Tabs=_get
        )
        existence = probe_node(dest, self._node())
        assert existence.state is LookupOutcome.FOUND
        assert existence.prefer_update is True
        assert existence.dest_wid == "DEST_DDB1"
        assert default_action(existence) is Action.UPDATE

    def test_not_found_is_unknown_not_create(self):
        dest = FakeDestination()

        def _get(**kwargs):
            raise Exception(
                "Validation error occurred. Invalid ID value.  'HOME' is not a "
                "valid ID value for type = 'Landing_Page_ID'"
            )

        dest.service = SimpleNamespace(
            Get_Workday_Delivered_Dashboards_without_Tabs=_get
        )
        existence = probe_node(dest, self._node())
        assert existence.state is LookupOutcome.UNKNOWN
        assert "cannot be created" in (existence.fault or "")
        assert default_action(existence) is Action.SKIP


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


# ── Cross-tenant calculated-field matching ──────────────────────────────────
#
# Lives here rather than in test_discovery.py because these helpers exist to
# serve one decision — "does the destination already have this field?" — and
# that decision is the planner's. test_discovery.py is `live`-marked; this is
# pure logic and belongs in the default offline run.


def shaped_payload(
    wid,
    ref_id,
    *,
    name,
    business_object,
    class_name="Arithmetic Calculated Field",
    alias=None,
):
    """A calculated field carrying everything cross-tenant matching looks at."""
    data = {
        "Calculated_Field_Reference_ID": ref_id,
        "Name": name,
        "Class_Name": class_name,
        "External_Field_Reference": {
            "ID": [{"type": "WID", "_value_1": business_object}]
        },
    }
    if alias is not None:
        data["WQL_Alias"] = alias
    return {
        "Calculated_Field_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Calculated_Field_ID", "_value_1": ref_id},
            ]
        },
        "Calculated_Field_Data": data,
    }


def dest_match_index(*payloads):
    index = Index(kind="calculated_field", tenant="dest", fetched_at=time.time())
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
    return calculated_field_match_index(index)


def only_node(closure):
    return next(iter(closure.nodes.values()))


class TestCalculatedFieldShape:
    def test_shape_is_name_class_and_business_object(self):
        payload = shaped_payload("W1", "REF", name="Tenure", business_object="BO")
        assert calculated_field_shape(payload) == (
            "Tenure",
            "Arithmetic Calculated Field",
            "BO",
        )

    def test_a_field_with_no_business_object_has_no_shape(self):
        """An incomplete shape must never match: a match means "do not create",
        and matching on two thirds of an identity is a guess."""
        payload = shaped_payload("W1", "REF", name="Tenure", business_object="BO")
        del payload["Calculated_Field_Data"]["External_Field_Reference"]
        assert calculated_field_shape(payload) is None

    def test_duplicate_shapes_are_kept_as_a_list(self):
        """commitconsulting_dpt3 really does hold five fields named
        'Executive Group' on one business object."""
        index = dest_match_index(
            shaped_payload("D1", "a", name="Executive Group", business_object="BO"),
            shaped_payload("D2", "b", name="Executive Group", business_object="BO"),
        )
        shape = ("Executive Group", "Arithmetic Calculated Field", "BO")
        assert index.by_shape[shape] == ["D1", "D2"]


class TestCrossTenantMatching:
    """``Calculated_Field_ID`` is not a stable cross-tenant identity.

    Confirmed live 2026-08-11: `commitconsulting_dpt1` names a field
    ``CRTMNU01_Commit - HR Dashboard_03_Is Top Performer`` and `_dpt3` names the
    same field ``Custom Object Data - Is Top Performer``. Probing by ID alone
    called 62 present fields absent; creating them would have duplicated every
    one onto the same business object, with no delete operation to undo it.
    """

    def _probe(self, payload, match_index):
        # A destination that knows nothing by ID is the whole premise here.
        closure = closure_of(payload)
        return probe_node(
            FakeDestination(), only_node(closure), match_index=match_index
        )

    def test_without_a_match_index_an_id_miss_stays_missing(self):
        """The fallback is opt-in — callers that pass nothing are unaffected."""
        payload = shaped_payload("W1", "SRC_REF", name="Tenure", business_object="BO")
        found = probe_node(FakeDestination(), only_node(closure_of(payload)))
        assert found.state is LookupOutcome.NOT_FOUND
        assert found.matched_by is None

    def test_a_unique_shape_match_is_found_and_carries_the_destination_wid(self):
        payload = shaped_payload("W1", "SRC_REF", name="Tenure", business_object="BO")
        index = dest_match_index(
            shaped_payload("DEST1", "DIFFERENT_REF", name="Tenure", business_object="BO")
        )
        found = self._probe(payload, index)
        assert found.state is LookupOutcome.FOUND
        assert found.dest_wid == "DEST1"
        assert "name + class + business object" in found.matched_by

    def test_a_field_on_a_different_business_object_is_not_a_match(self):
        payload = shaped_payload("W1", "SRC_REF", name="Tenure", business_object="BO")
        index = dest_match_index(
            shaped_payload("DEST1", "x", name="Tenure", business_object="OTHER")
        )
        assert self._probe(payload, index).state is LookupOutcome.NOT_FOUND

    def test_two_same_shape_candidates_are_unknown_not_a_guess(self):
        payload = shaped_payload("W1", "SRC_REF", name="Tenure", business_object="BO")
        index = dest_match_index(
            shaped_payload("DEST1", "x", name="Tenure", business_object="BO"),
            shaped_payload("DEST2", "y", name="Tenure", business_object="BO"),
        )
        found = self._probe(payload, index)
        assert found.state is LookupOutcome.UNKNOWN
        assert "no WQL alias to break the tie" in found.fault

    def test_the_wql_alias_breaks_a_shape_tie(self):
        payload = shaped_payload(
            "W1", "SRC_REF", name="Tenure", business_object="BO", alias="cf_tenure"
        )
        index = dest_match_index(
            shaped_payload(
                "DEST1", "x", name="Tenure", business_object="BO", alias="cf_other"
            ),
            shaped_payload(
                "DEST2", "y", name="Tenure", business_object="BO", alias="cf_tenure"
            ),
        )
        found = self._probe(payload, index)
        assert found.state is LookupOutcome.FOUND
        assert found.dest_wid == "DEST2"
        assert "tie-broken by WQL alias" in found.matched_by

    def test_an_alias_matching_no_candidate_stays_unknown(self):
        payload = shaped_payload(
            "W1", "SRC_REF", name="Tenure", business_object="BO", alias="cf_tenure"
        )
        index = dest_match_index(
            shaped_payload(
                "DEST1", "x", name="Tenure", business_object="BO", alias="cf_a"
            ),
            shaped_payload(
                "DEST2", "y", name="Tenure", business_object="BO", alias="cf_b"
            ),
        )
        assert self._probe(payload, index).state is LookupOutcome.UNKNOWN

    def test_a_renamed_field_is_matched_on_alias_alone(self):
        """dpt1's 'CF LRV Benefit Group' is dpt3's 'Benefit Group'. Nothing but
        the alias connects them, which is why alias is tried last rather than
        not at all."""
        payload = shaped_payload(
            "W1",
            "SRC_REF",
            name="CF LRV Benefit Group",
            business_object="BO",
            alias="cf_benefitGroup",
        )
        index = dest_match_index(
            shaped_payload(
                "DEST1",
                "x",
                name="Benefit Group",
                business_object="BO",
                alias="cf_benefitGroup",
            )
        )
        found = self._probe(payload, index)
        assert found.state is LookupOutcome.FOUND
        assert found.dest_wid == "DEST1"
        assert "WQL alias" in found.matched_by

    def test_a_shared_alias_is_narrowed_by_business_object(self):
        """dpt3 holds three fields named 'Employee ID' sharing cf_EmployeeID on
        three different business objects. Only one of them is this field."""
        payload = shaped_payload(
            "W1",
            "SRC_REF",
            name="CF LRV Employee ID",
            business_object="BO",
            alias="cf_EmployeeID",
        )
        index = dest_match_index(
            shaped_payload(
                "DEST1", "x", name="Employee ID",
                business_object="OTHER1", alias="cf_EmployeeID",
            ),
            shaped_payload(
                "DEST2", "y", name="Employee ID",
                business_object="BO", alias="cf_EmployeeID",
            ),
            shaped_payload(
                "DEST3", "z", name="Employee ID",
                business_object="OTHER2", alias="cf_EmployeeID",
            ),
        )
        found = self._probe(payload, index)
        assert found.state is LookupOutcome.FOUND
        assert found.dest_wid == "DEST2"
        assert "narrowed by business object" in found.matched_by

    def test_a_shared_alias_on_one_business_object_stays_unknown(self):
        payload = shaped_payload(
            "W1", "SRC_REF", name="CF LRV Company",
            business_object="BO", alias="cf_Company",
        )
        index = dest_match_index(
            shaped_payload(
                "DEST1", "x", name="Company", business_object="BO", alias="cf_Company"
            ),
            shaped_payload(
                "DEST2", "y", name="Company", business_object="BO", alias="cf_Company"
            ),
        )
        found = self._probe(payload, index)
        assert found.state is LookupOutcome.UNKNOWN
        assert "duplicate alias" in found.fault

    def test_a_field_with_neither_shape_nor_alias_is_missing(self):
        payload = shaped_payload("W1", "SRC_REF", name="Tenure", business_object="BO")
        del payload["Calculated_Field_Data"]["External_Field_Reference"]
        assert self._probe(payload, dest_match_index()).state is LookupOutcome.NOT_FOUND

    def test_an_id_hit_never_consults_the_match_index(self):
        """The business ID stays the strongest signal on the runs where it does
        match — the fallback only ever fires on a miss."""
        payload = shaped_payload("W1", "SRC_REF", name="Tenure", business_object="BO")
        dest = FakeDestination({"SRC_REF": ("found", "BY_ID")})
        index = dest_match_index(
            shaped_payload("BY_SHAPE", "x", name="Tenure", business_object="BO")
        )
        found = probe_node(dest, only_node(closure_of(payload)), match_index=index)
        assert found.dest_wid == "BY_ID"
        assert found.matched_by is None

    def test_a_shape_matched_field_seeds_the_wid_map(self):
        """The point of matching rather than creating: dependents must be
        rewritten to the destination's WID, not left on a source one that means
        nothing there."""
        payload = shaped_payload("W1", "SRC_REF", name="Tenure", business_object="BO")
        closure = closure_of(payload)
        index = dest_match_index(
            shaped_payload("DEST1", "DIFFERENT_REF", name="Tenure", business_object="BO")
        )
        existence = {
            p.node.node_id: p.existence
            for p in iter_check_existence(
                FakeDestination(), closure, match_index=index
            )
        }
        plan = build_plan(closure, existence)
        assert plan.wid_map["W1"] == "DEST1"
        assert plan.action_for(only_node(closure)) is Action.SKIP


# ── Cross-tenant calculated-measure matching ────────────────────────────────


def measure_payload(wid, ref_id, *, name, business_object):
    return {
        "Calculated_Measure_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "BI_Calculated_Measure_ID", "_value_1": ref_id},
            ]
        },
        "Calculated_Measure_Data": {
            "Name": name,
            "Business_Object_Reference": {
                "ID": [{"type": "WID", "_value_1": business_object}]
            },
        },
    }


def measure_match_index_of(*payloads):
    index = Index(kind="calculated_measure", tenant="dest", fetched_at=time.time())
    for payload in payloads:
        wid = payload["Calculated_Measure_Reference"]["ID"][0]["_value_1"]
        index.summaries[wid] = CalculatedMeasureSummary(
            wid=wid, reference_id=None, name=None
        )
        index.payloads[wid] = payload
    return calculated_measure_match_index(index)


def measure_node(payload, ref_id):
    """A bare closure node for a calculated measure."""
    wid = payload["Calculated_Measure_Reference"]["ID"][0]["_value_1"]
    return Node(
        node_id=node_id_for(NodeKind.CALCULATED_MEASURE, wid),
        kind=NodeKind.CALCULATED_MEASURE,
        source_wid=wid,
        reference_id=ref_id,
        name=payload["Calculated_Measure_Data"]["Name"],
        payload=payload,
    )


class TestCalculatedMeasureMatching:
    """``BI_Calculated_Measure_ID`` cannot be a cross-tenant identity.

    Unlike calculated fields — where it depends on how each tenant acquired the
    field — measure IDs are Workday-generated with tenant-local sequence
    numbers (`ARITHMETIC_CALCULATED_MEASURE-11-210`), so two tenants can never
    agree. Confirmed live 2026-08-12: creating a measure whose name is taken
    fails with "Enter a unique name for the System-Wide Summarization
    Calculation", which halted a migration 67 objects in.
    """

    def _probe(self, payload, ref_id, index):
        return probe_node(
            FakeDestination(), measure_node(payload, ref_id), measure_match_index=index
        )

    def test_shape_is_name_and_business_object(self):
        payload = measure_payload("W1", "x", name="Turnover", business_object="BO")
        assert calculated_measure_shape(payload) == ("Turnover", "BO")

    def test_a_measure_with_no_business_object_has_no_shape(self):
        payload = measure_payload("W1", "x", name="Turnover", business_object="BO")
        del payload["Calculated_Measure_Data"]["Business_Object_Reference"]
        assert calculated_measure_shape(payload) is None

    def test_without_a_match_index_an_id_miss_stays_missing(self):
        payload = measure_payload("W1", "SRC", name="Turnover", business_object="BO")
        found = probe_node(FakeDestination(), measure_node(payload, "SRC"))
        assert found.state is LookupOutcome.NOT_FOUND
        assert found.matched_by is None

    def test_a_unique_shape_match_is_found(self):
        payload = measure_payload("W1", "SRC", name="Turnover", business_object="BO")
        index = measure_match_index_of(
            measure_payload(
                "DEST1", "ARITHMETIC_CALCULATED_MEASURE-11-210",
                name="Turnover", business_object="BO",
            )
        )
        found = self._probe(payload, "SRC", index)
        assert found.state is LookupOutcome.FOUND
        assert found.dest_wid == "DEST1"
        assert "name + business object" in found.matched_by

    def test_a_measure_on_a_different_business_object_is_not_a_match(self):
        payload = measure_payload("W1", "SRC", name="Turnover", business_object="BO")
        index = measure_match_index_of(
            measure_payload("DEST1", "y", name="Turnover", business_object="OTHER")
        )
        assert self._probe(payload, "SRC", index).state is LookupOutcome.NOT_FOUND

    def test_two_candidates_are_unknown_not_a_guess(self):
        payload = measure_payload("W1", "SRC", name="Turnover", business_object="BO")
        index = measure_match_index_of(
            measure_payload("DEST1", "y", name="Turnover", business_object="BO"),
            measure_payload("DEST2", "z", name="Turnover", business_object="BO"),
        )
        found = self._probe(payload, "SRC", index)
        assert found.state is LookupOutcome.UNKNOWN
        assert "share this name and business object" in found.fault

    def test_a_genuinely_absent_measure_is_still_created(self):
        """Five of the seven measures in the live run really were new — the
        fallback must not turn 'absent' into 'assume it exists'."""
        payload = measure_payload("W1", "SRC", name="Brand New", business_object="BO")
        index = measure_match_index_of(
            measure_payload("DEST1", "y", name="Something Else", business_object="BO")
        )
        assert self._probe(payload, "SRC", index).state is LookupOutcome.NOT_FOUND


# ── Nested business-id remapping ─────────────────────────────────────────────


class TestSubstituteReferenceIds:
    """A reused field answers to the DESTINATION's business id, not the source's.

    ``extract_reference_id_refs`` documented the opposite — that a business id
    is stable across tenants and needs no remapping — and that held only while
    every field was created rather than matched. Confirmed live 2026-08-13:
    `Skills Gaps (as of Today)` was refused because it named
    ``CRTMNU01_Commit - HR Dashboard_03_Learning Points``, which dpt5 calls
    ``Worker - Learning Points``.
    """

    def nested(self, value):
        return {
            "Calculated_Field_Data": {
                "Calculated_Field_Reference_ID": "the objects own id",
                "Business_Object_Field": [
                    {
                        "Class_Report_Field_Reference": None,
                        "Calculated_Field_Reference_ID": value,
                        "Calculated_Field_Name": "Learning Points",
                    }
                ],
            }
        }

    def test_a_nested_id_is_rewritten_to_the_destination_id(self):
        out = substitute_reference_ids(
            self.nested("SRC_ID"), {"SRC_ID": "Worker - Learning Points"}
        )
        nested = out["Calculated_Field_Data"]["Business_Object_Field"][0]
        assert nested["Calculated_Field_Reference_ID"] == "Worker - Learning Points"

    def test_an_unmapped_id_is_left_alone(self):
        """Most fields are created rather than matched, and carry the source id
        into the destination — rewriting those would break them."""
        out = substitute_reference_ids(self.nested("SRC_ID"), {"OTHER": "X"})
        nested = out["Calculated_Field_Data"]["Business_Object_Field"][0]
        assert nested["Calculated_Field_Reference_ID"] == "SRC_ID"

    def test_the_input_is_not_mutated(self):
        payload = self.nested("SRC_ID")
        substitute_reference_ids(payload, {"SRC_ID": "DEST_ID"})
        nested = payload["Calculated_Field_Data"]["Business_Object_Field"][0]
        assert nested["Calculated_Field_Reference_ID"] == "SRC_ID"

    def test_an_empty_map_is_a_plain_copy(self):
        payload = self.nested("SRC_ID")
        out = substitute_reference_ids(payload, {})
        assert out == payload and out is not payload

    def test_ids_nested_arbitrarily_deep_are_reached(self):
        payload = {"a": [{"b": {"c": [{"Calculated_Field_Reference_ID": "SRC"}]}}]}
        out = substitute_reference_ids(payload, {"SRC": "DEST"})
        assert out["a"][0]["b"]["c"][0]["Calculated_Field_Reference_ID"] == "DEST"


class TestReferenceIdMapSeeding:
    def existence(self, **kwargs):
        return Existence(node_id="n", state=LookupOutcome.FOUND, **kwargs)

    def test_a_cross_tenant_match_seeds_the_map(self):
        payload = cf_payload("W1", "SRC_ID")
        closure = closure_of(payload)
        node = only_node(closure)
        plan = build_plan(
            closure,
            {
                node.node_id: Existence(
                    node_id=node.node_id,
                    state=LookupOutcome.FOUND,
                    dest_wid="DEST_WID",
                    dest_reference_id="DEST_ID",
                    matched_by="name + class + business object",
                )
            },
        )
        assert plan.reference_id_map == {"SRC_ID": "DEST_ID"}

    def test_matching_ids_are_not_mapped(self):
        """An id match means the tenants already agree — a no-op entry would
        just be noise in the map."""
        payload = cf_payload("W1", "SAME_ID")
        closure = closure_of(payload)
        node = only_node(closure)
        plan = build_plan(
            closure,
            {
                node.node_id: Existence(
                    node_id=node.node_id,
                    state=LookupOutcome.FOUND,
                    dest_wid="DEST_WID",
                    dest_reference_id="SAME_ID",
                )
            },
        )
        assert plan.reference_id_map == {}

    def test_a_missing_object_seeds_nothing(self):
        payload = cf_payload("W1", "SRC_ID")
        closure = closure_of(payload)
        node = only_node(closure)
        plan = build_plan(
            closure,
            {node.node_id: Existence(node_id=node.node_id, state=LookupOutcome.NOT_FOUND)},
        )
        assert plan.reference_id_map == {}
