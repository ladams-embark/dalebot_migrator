"""Tests for dependency closure resolution. Pure logic, offline.

Resolution decides *what gets migrated*. Under-resolving is the dangerous
direction: a dependency mistaken for a delivered object is never created, and
the object that needs it lands in the destination pointing at nothing.
"""

import time

import pytest

from wdmigrator.discovery.inventory import CalculatedFieldSummary, Index
from wdmigrator.migrate import topological_sort
from wdmigrator.migrate.resolver import (
    NodeKind,
    PartialIndexError,
    node_id_for,
    resolve_closure,
)


def cf_payload(wid, ref_id, name="Field", refs=()):
    """A calculated field whose sub-type data references other WIDs."""
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
            "Arithmetic_Calculated_Field_Data": {
                "Operands": [
                    {"Field_Reference": {"ID": [{"type": "WID", "_value_1": r}]}}
                    for r in refs
                ]
            },
        },
    }


def report_payload(wid, report_id, name="Report", refs=()):
    return {
        "Tenanted_Report_Definition_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Custom_Report_ID", "_value_1": report_id},
            ]
        },
        "Tenanted_Report_Definition_Data": {
            "Name": name,
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


class TestFieldSelection:
    def test_selecting_a_leaf_field_yields_just_that_field(self):
        index = cf_index(cf_payload("W1", "CF_A"))
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert len(closure) == 1
        assert closure.nodes[node_id_for(NodeKind.CALCULATED_FIELD, "W1")].selected

    def test_nested_dependency_is_pulled_in(self):
        index = cf_index(cf_payload("W1", "CF_A", refs=["W2"]), cf_payload("W2", "CF_B"))
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert len(closure) == 2
        assert len(closure.selected_nodes) == 1
        assert len(closure.pulled_in_nodes) == 1

    def test_transitive_chain_is_fully_expanded(self):
        index = cf_index(
            cf_payload("W1", "A", refs=["W2"]),
            cf_payload("W2", "B", refs=["W3"]),
            cf_payload("W3", "C"),
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert len(closure) == 3

    def test_resolved_closure_sorts_child_most_first(self):
        """The user's explicit requirement, end to end."""
        index = cf_index(
            cf_payload("parent", "P", refs=["child"]),
            cf_payload("child", "C", refs=["grandchild"]),
            cf_payload("grandchild", "G"),
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["parent"])
        order = [n.source_wid for n in topological_sort(closure.nodes)]
        assert order == ["grandchild", "child", "parent"]

    def test_shared_dependency_appears_once(self):
        index = cf_index(
            cf_payload("W1", "A", refs=["shared"]),
            cf_payload("W2", "B", refs=["shared"]),
            cf_payload("shared", "S"),
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1", "W2"])
        assert len(closure) == 3

    def test_selecting_an_unknown_field_raises(self):
        with pytest.raises(KeyError):
            resolve_closure(cf_index=cf_index(), selected_field_wids=["nope"])


class TestPassthroughReferences:
    def test_references_outside_the_index_are_passthrough_not_dependencies(self):
        """Delivered objects share WIDs across tenants and need no migration."""
        index = cf_index(cf_payload("W1", "A", refs=["delivered_business_object"]))
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert len(closure) == 1
        assert "delivered_business_object" in closure.passthrough_wids

    def test_self_reference_is_not_a_dependency(self):
        index = cf_index(cf_payload("W1", "A", refs=["W1"]))
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        node = closure.nodes[node_id_for(NodeKind.CALCULATED_FIELD, "W1")]
        assert node.depends_on == frozenset()


class TestReportSelection:
    def test_report_pulls_in_its_calculated_fields(self):
        index = cf_index(cf_payload("cf1", "CF_A"))
        closure = resolve_closure(
            cf_index=index,
            selected_reports={"r1": report_payload("r1", "RPT_A", refs=["cf1"])},
        )
        assert len(closure) == 2
        assert closure.counts_by_kind() == {"report": 1, "calculated_field": 1}

    def test_report_fields_are_ordered_before_the_report(self):
        index = cf_index(cf_payload("cf1", "CF_A", refs=["cf2"]), cf_payload("cf2", "CF_B"))
        closure = resolve_closure(
            cf_index=index,
            selected_reports={"r1": report_payload("r1", "RPT_A", refs=["cf1"])},
        )
        order = [n.source_wid for n in topological_sort(closure.nodes)]
        assert order.index("cf2") < order.index("cf1") < order.index("r1")

    def test_report_data_source_reference_is_passthrough(self):
        """Data sources must pre-exist in the destination; we never write them."""
        index = cf_index()
        closure = resolve_closure(
            cf_index=index,
            selected_reports={"r1": report_payload("r1", "RPT_A", refs=["datasource1"])},
        )
        assert "datasource1" in closure.passthrough_wids
        assert len(closure) == 1

    def test_report_reference_id_is_the_custom_report_id(self):
        closure = resolve_closure(
            cf_index=cf_index(),
            selected_reports={"r1": report_payload("r1", "RPT_A")},
        )
        assert closure.nodes["report:r1"].reference_id == "RPT_A"


class TestProvenance:
    def test_required_by_explains_why_a_node_was_included(self):
        index = cf_index(cf_payload("W1", "A", refs=["W2"]), cf_payload("W2", "B"))
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        dep = closure.nodes[node_id_for(NodeKind.CALCULATED_FIELD, "W2")]
        assert node_id_for(NodeKind.CALCULATED_FIELD, "W1") in dep.required_by

    def test_selected_flag_separates_picked_from_pulled_in(self):
        index = cf_index(cf_payload("W1", "A", refs=["W2"]), cf_payload("W2", "B"))
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert [n.source_wid for n in closure.selected_nodes] == ["W1"]
        assert [n.source_wid for n in closure.pulled_in_nodes] == ["W2"]


class TestPartialIndexGuard:
    def test_partial_index_is_refused(self):
        """A partial index makes real dependencies look delivered — silent data loss."""
        index = cf_index(cf_payload("W1", "A"))
        with pytest.raises(PartialIndexError, match="partial"):
            resolve_closure(
                cf_index=index,
                selected_field_wids=["W1"],
                expected_index_size=9652,
            )

    def test_complete_index_passes_the_check(self):
        index = cf_index(cf_payload("W1", "A"))
        closure = resolve_closure(
            cf_index=index, selected_field_wids=["W1"], expected_index_size=1
        )
        assert len(closure) == 1

    def test_the_guard_can_be_bypassed_explicitly_for_tests(self):
        index = cf_index(cf_payload("W1", "A"))
        closure = resolve_closure(
            cf_index=index,
            selected_field_wids=["W1"],
            expected_index_size=9652,
            allow_partial_index=True,
        )
        assert len(closure) == 1


class TestNoNetwork:
    def test_resolution_takes_no_connection_argument(self):
        """Resolution is pure: the complete index makes probing unnecessary."""
        import inspect

        params = inspect.signature(resolve_closure).parameters
        assert not {"connection", "conn", "client"} & set(params)


class TestRealPayloadShape:
    """Runs against a fixture captured from a live tenant.

    Structure is real — all 34 polymorphic sub-type blocks, the actual nesting
    depth, the real reference shapes — while identifiers are synthetic so no
    tenant configuration is committed. Synthetic fixtures alone would not catch
    a mismatch between assumed and actual payload shape.
    """

    @pytest.fixture
    def real_index(self, nested_fields_fixture):
        return cf_index_from_items(nested_fields_fixture["Calculated_Field"])

    def test_fixture_has_genuinely_nested_dependencies(self, real_index):
        closure = resolve_closure(
            cf_index=real_index,
            selected_field_wids=[_seed_wid(real_index)],
            expected_index_size=len(real_index),
        )
        assert len(closure) > 1, "fixture should exercise real dependency resolution"

    def test_real_nesting_orders_child_most_first(self, real_index):
        closure = resolve_closure(
            cf_index=real_index,
            selected_field_wids=[_seed_wid(real_index)],
            expected_index_size=len(real_index),
        )
        order = topological_sort(closure.nodes)
        positions = {n.node_id: i for i, n in enumerate(order)}
        violations = [
            (n.node_id, dep)
            for n in order
            for dep in n.depends_on
            if positions[dep] > positions[n.node_id]
        ]
        assert violations == []
        assert order[-1].selected, "the user's pick should be written last"

    def test_delivered_references_are_passthrough(self, real_index):
        closure = resolve_closure(
            cf_index=real_index,
            selected_field_wids=[_seed_wid(real_index)],
            expected_index_size=len(real_index),
        )
        assert closure.passthrough_wids, "real fields reference delivered objects"


def cf_index_from_items(items):
    index = Index(kind="calculated_field", tenant="t", fetched_at=time.time())
    for item in items:
        ids = {e["type"]: e["_value_1"] for e in item["Calculated_Field_Reference"]["ID"]}
        wid = ids["WID"]
        data = item.get("Calculated_Field_Data") or {}
        index.summaries[wid] = CalculatedFieldSummary(
            wid=wid,
            reference_id=data.get("Calculated_Field_Reference_ID"),
            name=data.get("Name"),
            class_name=data.get("Class_Name"),
        )
        index.payloads[wid] = item
    return index


def _seed_wid(index):
    """The fixture's root field — the one nothing else depends on."""
    from wdmigrator.migrate.ordering import extract_wid_refs

    for wid, payload in index.payloads.items():
        deps = extract_wid_refs(
            payload.get("Calculated_Field_Data") or {}, exclude=[wid]
        ) & set(index.payloads)
        if deps:
            return wid
    raise AssertionError("fixture contains no nested dependencies")


def cf_payload_nested_by_reference_id(wid, ref_id, name="Field", nested_ref_ids=()):
    """A calculated field that names its dependencies the way Workday actually
    does: by `Calculated_Field_Reference_ID`, with no WID for the nested field
    anywhere. The only WID present belongs to the business object.

    Confirmed live on `commitconsulting` (wd501) 2026-08-05 — see
    `ordering.extract_reference_id_refs`.
    """
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
            "Class_Name": "Extract Single Instance Calculated Field",
            "External_Field_Reference": {
                "ID": [{"type": "WID", "_value_1": "business_object_wid"}]
            },
            "Extract_Single_Instance_Calculated_Field_Data": {
                "Business_Object_Field_Add_or_Reference_Data": {
                    "Business_Object_Field": [
                        {
                            "Class_Report_Field_Reference": None,
                            "Calculated_Field_Reference_ID": nested,
                            "Calculated_Field_Name": f"Nested {nested}",
                            "Business_Object_Reference": {
                                "ID": [{"type": "WID", "_value_1": "business_object_wid"}]
                            },
                        }
                        for nested in nested_ref_ids
                    ]
                }
            },
        },
    }


class TestNestedReferenceByReferenceId:
    """Regression: the multi-level dependency that carries no WID.

    Before this was handled, a field referencing another purely by
    `Calculated_Field_Reference_ID` resolved to a closure of one — the
    dependency was recorded as a pass-through and never migrated, so the live
    PUT landed in the destination pointing at a field that was never created.
    """

    def test_nested_field_is_pulled_into_the_closure(self):
        index = cf_index(
            cf_payload_nested_by_reference_id("W1", "CF_A", nested_ref_ids=["CF_B"]),
            cf_payload("W2", "CF_B"),
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert len(closure) == 2
        assert node_id_for(NodeKind.CALCULATED_FIELD, "W2") in closure.nodes

    def test_nested_field_is_ordered_before_its_dependent(self):
        index = cf_index(
            cf_payload_nested_by_reference_id("W1", "CF_A", nested_ref_ids=["CF_B"]),
            cf_payload("W2", "CF_B"),
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        order = [n.source_wid for n in topological_sort(closure.nodes)]
        assert order.index("W2") < order.index("W1")

    def test_chain_of_reference_id_links_expands_fully(self):
        index = cf_index(
            cf_payload_nested_by_reference_id("W1", "A", nested_ref_ids=["B"]),
            cf_payload_nested_by_reference_id("W2", "B", nested_ref_ids=["C"]),
            cf_payload("W3", "C"),
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert len(closure) == 3
        order = [n.source_wid for n in topological_sort(closure.nodes)]
        assert order == ["W3", "W2", "W1"]

    def test_business_object_wid_still_passes_through(self):
        index = cf_index(
            cf_payload_nested_by_reference_id("W1", "CF_A", nested_ref_ids=["CF_B"]),
            cf_payload("W2", "CF_B"),
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert "business_object_wid" in closure.passthrough_wids

    def test_a_nested_id_missing_from_the_index_is_recorded_not_ignored(self):
        """The payload states outright that the target is a calculated field, so
        not finding it is a real missing dependency, not a pass-through."""
        index = cf_index(
            cf_payload_nested_by_reference_id("W1", "CF_A", nested_ref_ids=["GONE"])
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert closure.unresolved_reference_ids == {"GONE"}
        assert len(closure) == 1

    def test_self_reference_by_reference_id_creates_no_cycle(self):
        index = cf_index(
            cf_payload_nested_by_reference_id("W1", "CF_A", nested_ref_ids=["CF_A"])
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert len(closure) == 1
        topological_sort(closure.nodes)  # must not raise CycleError

    def test_a_clean_closure_records_nothing_unresolved(self):
        index = cf_index(
            cf_payload_nested_by_reference_id("W1", "CF_A", nested_ref_ids=["CF_B"]),
            cf_payload("W2", "CF_B"),
        )
        closure = resolve_closure(cf_index=index, selected_field_wids=["W1"])
        assert closure.unresolved_reference_ids == set()


def measure_payload(wid, business_id, name="Measure", field_wids=(), measure_refs=()):
    """A calculated measure as the tenant actually returns one.

    Shape confirmed live on `commitconsulting` (wd501) 2026-08-05: measures
    reference calculated fields AND other measures, so they need the same
    transitive expansion and child-most-first ordering as fields.
    """
    related = []
    for fw in field_wids:
        related.append(
            {"External_Field": {"Class_Report_Field_Reference": {
                "ID": [{"type": "WID", "_value_1": fw}]}}}
        )
    for mw, mid in measure_refs:
        related.append(
            {"Calculated_Measure_Reference": {"ID": [
                {"type": "WID", "_value_1": mw},
                {"type": "BI_Calculated_Measure_ID", "_value_1": mid},
            ]}}
        )
    return {
        "Calculated_Measure_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "BI_Calculated_Measure_ID", "_value_1": business_id},
            ]
        },
        "Calculated_Measure_Data": {
            "Name": name,
            "ID": business_id,
            "Arithmetic_Calculated_Measure_Data": [
                {"BI_Calculated_Measure_Related_Content_Data": related}
            ],
        },
    }


def report_using_measure(wid, report_id, name, measure_wid, measure_id):
    """A report whose column summarises via a calculated measure."""
    return {
        "Tenanted_Report_Definition_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Custom_Report_ID", "_value_1": report_id},
            ]
        },
        "Tenanted_Report_Definition_Data": {
            "Name": name,
            "Tenanted_Report_Column_Data": [
                {"Summary_Calculation_Reference": {"ID": [
                    {"type": "WID", "_value_1": measure_wid},
                    {"type": "BI_Calculated_Measure_ID", "_value_1": measure_id},
                ]}}
            ],
        },
    }


class TestCalculatedMeasures:
    """Measures are dependency-only: never selected, never indexed, fetched on
    demand when a report being migrated uses one."""

    def test_report_pulls_in_the_measure_it_uses(self):
        loader = {"MW1": measure_payload("MW1", "ARITH-Turnover-1")}
        report = report_using_measure("R1", "RPT", "R", "MW1", "ARITH-Turnover-1")
        closure = resolve_closure(
            cf_index=cf_index(),
            selected_reports={"R1": report},
            measure_loader=loader.get,
        )
        assert len(closure) == 2
        node = closure.nodes[node_id_for(NodeKind.CALCULATED_MEASURE, "MW1")]
        assert node.kind is NodeKind.CALCULATED_MEASURE
        assert node.reference_id == "ARITH-Turnover-1"
        assert not node.selected

    def test_measure_is_ordered_before_the_report(self):
        loader = {"MW1": measure_payload("MW1", "ARITH-Turnover-1")}
        report = report_using_measure("R1", "RPT", "R", "MW1", "ARITH-Turnover-1")
        closure = resolve_closure(
            cf_index=cf_index(), selected_reports={"R1": report},
            measure_loader=loader.get,
        )
        order = [n.source_wid for n in topological_sort(closure.nodes)]
        assert order.index("MW1") < order.index("R1")

    def test_measure_depending_on_another_measure_expands_transitively(self):
        loader = {
            "MW1": measure_payload("MW1", "ARITH-Turnover-1",
                                   measure_refs=[("MW2", "ARITH-AvgHeadcount-2")]),
            "MW2": measure_payload("MW2", "ARITH-AvgHeadcount-2"),
        }
        report = report_using_measure("R1", "RPT", "R", "MW1", "ARITH-Turnover-1")
        closure = resolve_closure(
            cf_index=cf_index(), selected_reports={"R1": report},
            measure_loader=loader.get,
        )
        assert len(closure) == 3
        order = [n.source_wid for n in topological_sort(closure.nodes)]
        assert order.index("MW2") < order.index("MW1") < order.index("R1")

    def test_measure_pulls_in_the_calculated_fields_it_uses(self):
        index = cf_index(cf_payload("W2", "CF_B"))
        loader = {"MW1": measure_payload("MW1", "ARITH-1", field_wids=["W2"])}
        report = report_using_measure("R1", "RPT", "R", "MW1", "ARITH-1")
        closure = resolve_closure(
            cf_index=index, selected_reports={"R1": report},
            measure_loader=loader.get,
        )
        assert len(closure) == 3
        order = [n.source_wid for n in topological_sort(closure.nodes)]
        assert order.index("W2") < order.index("MW1") < order.index("R1")

    def test_a_measure_the_source_cannot_return_is_recorded(self):
        report = report_using_measure("R1", "RPT", "R", "MW1", "ARITH-Turnover-1")
        closure = resolve_closure(
            cf_index=cf_index(), selected_reports={"R1": report},
            measure_loader=lambda wid: None,
        )
        assert closure.unresolved_measure_ids == {"ARITH-Turnover-1"}
        assert len(closure) == 1

    def test_without_a_loader_measures_are_skipped_entirely(self):
        """No loader means no tenant calls — resolution stays pure."""
        report = report_using_measure("R1", "RPT", "R", "MW1", "ARITH-Turnover-1")
        closure = resolve_closure(cf_index=cf_index(), selected_reports={"R1": report})
        assert len(closure) == 1
        assert closure.unresolved_measure_ids == set()
        assert "MW1" in closure.passthrough_wids

    def test_a_measure_wid_is_not_also_recorded_as_a_passthrough(self):
        loader = {"MW1": measure_payload("MW1", "ARITH-Turnover-1")}
        report = report_using_measure("R1", "RPT", "R", "MW1", "ARITH-Turnover-1")
        closure = resolve_closure(
            cf_index=cf_index(), selected_reports={"R1": report},
            measure_loader=loader.get,
        )
        assert "MW1" not in closure.passthrough_wids

    def test_each_measure_is_fetched_once_even_when_referenced_repeatedly(self):
        calls = []

        def loader(wid):
            calls.append(wid)
            return measure_payload("MW1", "ARITH-Turnover-1")

        report = report_using_measure("R1", "RPT", "R", "MW1", "ARITH-Turnover-1")
        report["Tenanted_Report_Definition_Data"]["Tenanted_Report_Column_Data"].append(
            report["Tenanted_Report_Definition_Data"]["Tenanted_Report_Column_Data"][0]
        )
        resolve_closure(
            cf_index=cf_index(), selected_reports={"R1": report},
            measure_loader=loader,
        )
        assert calls == ["MW1"]
