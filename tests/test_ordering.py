"""Tests for dependency ordering and WID substitution.

Pure logic, no marker, no network — this is the fast inner loop, and it covers
the logic whose failures are silent and unrecoverable: a wrong order creates
objects with dangling references, and a wrong substitution points a destination
object at a WID that means nothing there.
"""

import pytest

from wdmigrator.migrate.ordering import (
    CycleError,
    build_dag,
    extract_reference_id_refs,
    extract_wid_refs,
    substitute_wids,
    topological_sort,
    unmapped_wids,
)
from wdmigrator.migrate.resolver import Node, NodeKind


def node(node_id, *deps):
    return Node(
        node_id=node_id,
        kind=NodeKind.CALCULATED_FIELD,
        source_wid=node_id,
        reference_id=f"REF_{node_id}",
        name=node_id,
        payload={},
        depends_on=frozenset(deps),
    )


def graph(*nodes):
    return {n.node_id: n for n in nodes}


def ref(id_type, value):
    return {"ID": [{"type": id_type, "_value_1": value}]}


class TestTopologicalSort:
    def test_dependency_comes_before_its_dependent(self):
        """The core guarantee: child-most first."""
        order = [n.node_id for n in topological_sort(graph(node("A", "B"), node("B")))]
        assert order.index("B") < order.index("A")

    def test_deep_chain_is_fully_ordered(self):
        # D -> C -> B -> A  (A is child-most)
        nodes = graph(node("D", "C"), node("C", "B"), node("B", "A"), node("A"))
        assert [n.node_id for n in topological_sort(nodes)] == ["A", "B", "C", "D"]

    def test_diamond_dependency(self):
        nodes = graph(node("top", "left", "right"), node("left", "base"),
                      node("right", "base"), node("base"))
        order = [n.node_id for n in topological_sort(nodes)]
        assert order[0] == "base"
        assert order[-1] == "top"
        assert order.index("left") < order.index("top")
        assert order.index("right") < order.index("top")

    def test_independent_nodes_all_appear(self):
        nodes = graph(node("A"), node("B"), node("C"))
        assert len(topological_sort(nodes)) == 3

    def test_empty_graph(self):
        assert topological_sort({}) == []

    def test_order_is_deterministic_across_runs(self):
        """The plan hash is computed over this order; instability would keep
        invalidating the user's dry-run approval."""
        nodes = graph(node("A"), node("B"), node("C"), node("D", "A", "B"))
        runs = {tuple(n.node_id for n in topological_sort(nodes)) for _ in range(5)}
        assert len(runs) == 1

    def test_order_does_not_depend_on_dict_insertion_order(self):
        forward = graph(node("A"), node("B"), node("C", "A", "B"))
        backward = graph(node("C", "A", "B"), node("B"), node("A"))
        assert [n.node_id for n in topological_sort(forward)] == [
            n.node_id for n in topological_sort(backward)
        ]


class TestCycles:
    def test_direct_cycle_raises(self):
        with pytest.raises(CycleError):
            topological_sort(graph(node("A", "B"), node("B", "A")))

    def test_longer_cycle_raises(self):
        with pytest.raises(CycleError):
            topological_sort(graph(node("A", "B"), node("B", "C"), node("C", "A")))

    def test_self_reference_raises(self):
        with pytest.raises(CycleError):
            topological_sort(graph(node("A", "A")))

    def test_error_names_the_nodes_involved(self):
        """'A cycle exists somewhere' is not actionable."""
        with pytest.raises(CycleError) as excinfo:
            topological_sort(graph(node("A", "B"), node("B", "A")))
        assert excinfo.value.cycle
        assert "A" in str(excinfo.value)

    def test_acyclic_part_does_not_mask_a_cycle(self):
        nodes = graph(node("ok"), node("A", "B"), node("B", "A"))
        with pytest.raises(CycleError):
            topological_sort(nodes)


class TestBuildDag:
    def test_edges_outside_the_graph_are_dropped(self):
        """Delivered objects aren't migrated, so they impose no ordering."""
        dag = build_dag(graph(node("A", "delivered_thing")))
        assert dag == {"A": set()}

    def test_internal_edges_are_kept(self):
        assert build_dag(graph(node("A", "B"), node("B"))) == {"A": {"B"}, "B": set()}


class TestSubstituteWids:
    def test_mapped_wid_is_rewritten(self):
        result = substitute_wids(ref("WID", "src1"), {"src1": "dst1"})
        assert result["ID"][0]["_value_1"] == "dst1"

    def test_unmapped_wid_is_left_alone(self):
        """Unmapped means Workday-delivered — identical in every tenant."""
        result = substitute_wids(ref("WID", "delivered"), {"src1": "dst1"})
        assert result["ID"][0]["_value_1"] == "delivered"

    def test_business_ids_are_never_rewritten(self):
        """Calculated_Field_ID is the cross-tenant identity; rewriting it would
        break the very matching the migration depends on."""
        payload = ref("Calculated_Field_ID", "src1")
        result = substitute_wids(payload, {"src1": "dst1"})
        assert result["ID"][0]["_value_1"] == "src1"

    def test_mixed_id_list_rewrites_only_the_wid(self):
        payload = {
            "ID": [
                {"type": "WID", "_value_1": "src1"},
                {"type": "Calculated_Field_ID", "_value_1": "src1"},
            ]
        }
        result = substitute_wids(payload, {"src1": "dst1"})
        assert [e["_value_1"] for e in result["ID"]] == ["dst1", "src1"]

    def test_does_not_mutate_the_input(self):
        """The source payload is reused across dry run and live run."""
        original = ref("WID", "src1")
        substitute_wids(original, {"src1": "dst1"})
        assert original["ID"][0]["_value_1"] == "src1"

    def test_substitutes_deep_inside_nested_structures(self):
        payload = {
            "Calculated_Field_Data": {
                "Arithmetic_Calculated_Field_Data": {
                    "Operands": [
                        {"Field_Reference": ref("WID", "src1")},
                        {"Field_Reference": ref("WID", "src2")},
                    ]
                }
            }
        }
        result = substitute_wids(payload, {"src1": "dst1", "src2": "dst2"})
        operands = result["Calculated_Field_Data"]["Arithmetic_Calculated_Field_Data"][
            "Operands"
        ]
        assert [o["Field_Reference"]["ID"][0]["_value_1"] for o in operands] == [
            "dst1",
            "dst2",
        ]

    def test_empty_map_returns_an_unchanged_copy(self):
        original = ref("WID", "src1")
        result = substitute_wids(original, {})
        assert result == original
        assert result is not original

    @pytest.mark.parametrize("payload", [None, {}, [], {"ID": None}, {"ID": []}])
    def test_tolerates_degenerate_payloads(self, payload):
        substitute_wids(payload, {"a": "b"})


class TestExtractWidRefs:
    def test_finds_nested_wids(self):
        payload = {"a": {"b": [{"Ref": ref("WID", "W1")}, {"Ref": ref("WID", "W2")}]}}
        assert extract_wid_refs(payload) == {"W1", "W2"}

    def test_ignores_non_wid_id_types(self):
        assert extract_wid_refs(ref("Calculated_Field_ID", "CF_A")) == set()

    def test_exclude_removes_self_reference(self):
        payload = {"a": ref("WID", "self"), "b": ref("WID", "other")}
        assert extract_wid_refs(payload, exclude=["self"]) == {"other"}

    def test_empty_payload_yields_nothing(self):
        assert extract_wid_refs({}) == set()


class TestUnmappedWids:
    def test_flags_a_custom_wid_with_no_destination_mapping(self):
        """This means we'd write a reference to something that isn't there."""
        payload = ref("WID", "custom1")
        assert unmapped_wids(payload, {}, custom={"custom1"}) == {"custom1"}

    def test_mapped_custom_wid_is_fine(self):
        payload = ref("WID", "custom1")
        assert unmapped_wids(payload, {"custom1": "dst"}, custom={"custom1"}) == set()

    def test_delivered_wids_are_not_flagged(self):
        payload = ref("WID", "delivered")
        assert unmapped_wids(payload, {}, custom={"custom1"}) == set()


class TestExtractReferenceIdRefs:
    """The nested-calculated-field reference that carries no WID at all.

    Shape confirmed live on `commitconsulting` (wd501) 2026-08-05: 612 of 1,399
    calculated fields reference another one exclusively this way, and
    `extract_wid_refs` finds none of them — the only WID in the block belongs to
    the business object the field lives on.
    """

    def real_shape(self, own_id, nested_id):
        return {
            "Calculated_Field_Reference_ID": own_id,
            "Class_Name": "Extract Single Instance Calculated Field",
            "External_Field_Reference": ref("WID", "business_object_wid"),
            "Extract_Single_Instance_Calculated_Field_Data": {
                "Business_Object_Field_Add_or_Reference_Data": {
                    "Business_Object_Field": [
                        {
                            "Class_Report_Field_Reference": None,
                            "Calculated_Field_Class_Name": "Lookup Single Instance",
                            "Calculated_Field_Reference_ID": nested_id,
                            "Calculated_Field_Name": "Nested field",
                            "Business_Object_Reference": ref("WID", "business_object_wid"),
                        }
                    ]
                }
            },
        }

    def test_finds_the_nested_reference(self):
        payload = self.real_shape("OWN", "NESTED")
        assert extract_reference_id_refs(payload) == {"NESTED"}

    def test_excludes_the_fields_own_top_level_id(self):
        payload = self.real_shape("OWN", "NESTED")
        assert "OWN" not in extract_reference_id_refs(payload)

    def test_wid_walk_cannot_see_it(self):
        """The regression this whole extractor exists for."""
        payload = self.real_shape("OWN", "NESTED")
        assert extract_wid_refs(payload) == {"business_object_wid"}

    def test_finds_several_across_different_containers(self):
        payload = {
            "Calculated_Field_Reference_ID": "OWN",
            "A": {"Condition_Field": [{"Calculated_Field_Reference_ID": "C1"}]},
            "B": {"Related_Field": {"Calculated_Field_Reference_ID": "C2"}},
            "C": [{"Sort_Field": [{"Calculated_Field_Reference_ID": "C3"}]}],
        }
        assert extract_reference_id_refs(payload) == {"C1", "C2", "C3"}

    def test_ignores_empty_and_non_string_values(self):
        payload = {"X": {"Calculated_Field_Reference_ID": ""},
                   "Y": {"Calculated_Field_Reference_ID": None}}
        assert extract_reference_id_refs(payload) == set()

    def test_empty_payload_yields_nothing(self):
        assert extract_reference_id_refs({}) == set()

    def test_reference_ids_survive_wid_substitution_untouched(self):
        """They are business IDs, stable across tenants — remapping one would
        break the identity the migration matches on."""
        payload = self.real_shape("OWN", "NESTED")
        out = substitute_wids(payload, {"business_object_wid": "dest_wid"})
        block = out["Extract_Single_Instance_Calculated_Field_Data"][
            "Business_Object_Field_Add_or_Reference_Data"
        ]["Business_Object_Field"][0]
        assert block["Calculated_Field_Reference_ID"] == "NESTED"
