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
