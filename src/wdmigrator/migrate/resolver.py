"""Turning a user's selection into the full set of objects that must migrate.

The user picks a handful of reports. Those reports reference calculated fields,
which reference other calculated fields, and every one of them has to exist in
the destination before the thing that uses it. This module computes that
closure.

**It makes no tenant calls.** That is a consequence of holding the complete
source calculated-field index in memory: the index contains every calculated
field in the tenant, so a WID absent from it is definitively not a calculated
field and needs no probe to rule out. Measured on the source tenant, a report
carries ~95 WID references; probing each one would cost ~12 seconds per report
at the rate limit, versus a free set lookup. Existence probing still happens,
but against the *destination*, and that is `planner.py`'s job.

Passing a partial index would silently under-resolve — real dependencies would
look like delivered objects and never get migrated — so :func:`resolve_closure`
refuses one unless explicitly told the risk is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Mapping

from wdmigrator.discovery.inventory import Index, ids_of
from wdmigrator.migrate.ordering import (
    extract_measure_refs,
    extract_reference_id_refs,
    extract_report_refs,
    extract_wid_refs,
)


class NodeKind(str, Enum):
    CALCULATED_FIELD = "calculated_field"
    REPORT = "report"
    CALCULATED_MEASURE = "calculated_measure"


class PartialIndexError(ValueError):
    """Resolution was attempted against an index that is not the whole tenant."""


@dataclass(frozen=True)
class Node:
    """One object that may need to be written to the destination."""

    node_id: str
    kind: NodeKind
    source_wid: str
    reference_id: str | None
    name: str | None
    payload: dict
    depends_on: frozenset[str] = frozenset()
    class_name: str | None = None
    #: True when the user picked this explicitly, False when it was pulled in
    #: as a dependency. Drives the "why is this here?" column in review.
    selected: bool = False
    required_by: frozenset[str] = frozenset()

    @property
    def is_report(self) -> bool:
        return self.kind is NodeKind.REPORT


@dataclass
class Closure:
    """The resolved set of objects, plus anything we could not account for."""

    nodes: dict[str, Node] = field(default_factory=dict)
    #: WIDs referenced by a selected object that are not calculated fields in
    #: the source index. Overwhelmingly these are delivered objects (business
    #: objects, data sources, field categories) that pass through unchanged —
    #: they are recorded for transparency, not as errors.
    passthrough_wids: set[str] = field(default_factory=set)
    #: ``Calculated_Field_Reference_ID`` values naming a calculated field that
    #: is **not** in the source index. Unlike ``passthrough_wids`` these are
    #: unambiguous: the payload states outright that the target is a calculated
    #: field, so failing to find it means a genuinely missing dependency, not a
    #: delivered object passing through. A write referencing one will fail.
    unresolved_reference_ids: set[str] = field(default_factory=set)
    #: ``BI_Calculated_Measure_ID`` values that could not be fetched from the
    #: source. Same reasoning as ``unresolved_reference_ids``: the reference
    #: names the object as a calculated measure, so not finding it is a real
    #: gap rather than a pass-through.
    unresolved_measure_ids: set[str] = field(default_factory=set)
    #: ``Custom_Report_ID`` values for sub-reports that could not be fetched.
    #: A composite cannot render a sub-report the destination does not have.
    unresolved_report_ids: set[str] = field(default_factory=set)

    def __len__(self) -> int:
        return len(self.nodes)

    @property
    def selected_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.selected]

    @property
    def pulled_in_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if not n.selected]

    def counts_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes.values():
            counts[node.kind.value] = counts.get(node.kind.value, 0) + 1
        return counts


def node_id_for(kind: NodeKind, wid: str) -> str:
    return f"{kind.value}:{wid}"


def _calculated_field_node(wid: str, index: Index, *, selected: bool) -> Node | None:
    payload = index.payload(wid)
    if payload is None:
        return None
    summary = index.summaries.get(wid)
    data = payload.get("Calculated_Field_Data") or {}
    return Node(
        node_id=node_id_for(NodeKind.CALCULATED_FIELD, wid),
        kind=NodeKind.CALCULATED_FIELD,
        source_wid=wid,
        reference_id=(
            getattr(summary, "reference_id", None)
            or data.get("Calculated_Field_Reference_ID")
        ),
        name=getattr(summary, "name", None) or data.get("Name"),
        class_name=getattr(summary, "class_name", None) or data.get("Class_Name"),
        payload=payload,
        selected=selected,
    )


def _report_node(wid: str, payload: dict, *, selected: bool) -> Node:
    data = payload.get("Tenanted_Report_Definition_Data") or {}
    ids = ids_of(payload.get("Tenanted_Report_Definition_Reference"))
    return Node(
        node_id=node_id_for(NodeKind.REPORT, wid),
        kind=NodeKind.REPORT,
        source_wid=wid,
        reference_id=ids.get("Custom_Report_ID"),
        name=data.get("Name"),
        payload=payload,
        selected=selected,
    )


def _measure_node(wid: str, payload: dict) -> Node:
    data = payload.get("Calculated_Measure_Data") or {}
    ids = ids_of(payload.get("Calculated_Measure_Reference"))
    return Node(
        node_id=node_id_for(NodeKind.CALCULATED_MEASURE, wid),
        kind=NodeKind.CALCULATED_MEASURE,
        source_wid=wid,
        reference_id=ids.get("BI_Calculated_Measure_ID") or data.get("ID"),
        name=data.get("Name"),
        payload=payload,
        selected=False,
    )


_DATA_BLOCK = {
    NodeKind.REPORT: "Tenanted_Report_Definition_Data",
    NodeKind.CALCULATED_FIELD: "Calculated_Field_Data",
    NodeKind.CALCULATED_MEASURE: "Calculated_Measure_Data",
}


def _dependency_payload(node: Node) -> dict:
    """The part of a node worth scanning for references.

    Scoped to the data block so the object's own reference block does not
    register as a dependency on itself.
    """
    return node.payload.get(_DATA_BLOCK[node.kind]) or {}


def resolve_closure(
    *,
    cf_index: Index,
    selected_field_wids: Iterable[str] = (),
    selected_reports: Mapping[str, dict] | None = None,
    allow_partial_index: bool = False,
    expected_index_size: int | None = None,
    measure_loader: Callable[[str], dict | None] | None = None,
    report_loader: Callable[[str], dict | None] | None = None,
) -> Closure:
    """Expand a selection into every object that has to be written.

    Args:
        cf_index: The **complete** source calculated-field index. Completeness
            is what makes probing unnecessary; see the module docstring.
        selected_field_wids: Calculated fields the user picked directly.
        selected_reports: ``{report_wid: full payload}`` the user picked.
        allow_partial_index: Opt out of the completeness check. Only for tests.
        expected_index_size: Total the index claims the tenant has, when known,
            so a truncated sweep can be caught.
        report_loader: ``wid -> payload`` for sub-reports, or None to skip
            them. A composite report names its sub-reports inline, and each has
            to exist in the destination first. Same on-demand contract as
            ``measure_loader``.
        measure_loader: ``wid -> payload`` for calculated measures, or None to
            skip them entirely. **This is the one argument that can make this
            function touch the network** — measures are not indexed, so each
            one is fetched on demand (see below). Pass None, or a dict-backed
            stub, to keep resolution offline.

    Returns:
        A :class:`Closure` whose nodes carry ``depends_on`` edges, ready for
        :func:`~wdmigrator.migrate.ordering.topological_sort`.

    **On measures and the "no tenant calls" rule.** Calculated fields are
    resolved against a complete in-memory index, which is what lets this
    function stay pure. Measures deliberately are not indexed: a tenant holds a
    handful of them, they are only ever reached as a dependency of a report
    that uses one, and a sweep would be almost entirely wasted. So they are
    fetched one at a time through ``measure_loader``. The caller supplies it,
    which keeps the network dependency explicit and this function testable with
    a plain dict.
    """
    selected_reports = selected_reports or {}

    if not allow_partial_index and expected_index_size is not None:
        if len(cf_index) < expected_index_size:
            raise PartialIndexError(
                f"Calculated-field index holds {len(cf_index)} of "
                f"{expected_index_size} fields. Resolving against a partial "
                "index would silently miss dependencies and migrate a broken "
                "subset. Finish the index sweep first."
            )

    closure = Closure()

    # Seed nodes.
    for wid, payload in selected_reports.items():
        node = _report_node(wid, payload, selected=True)
        closure.nodes[node.node_id] = node

    for wid in selected_field_wids:
        node = _calculated_field_node(wid, cf_index, selected=True)
        if node is None:
            raise KeyError(
                f"Calculated field {wid} is not in the source index — it cannot "
                "be migrated."
            )
        closure.nodes[node.node_id] = node

    # Expand transitively. A calculated field can be named two different ways
    # and BOTH have to be followed:
    #
    #   * by WID, inside an ``ID`` list — what ``extract_wid_refs`` finds;
    #   * by ``Calculated_Field_Reference_ID``, a bare string with no WID
    #     anywhere near it — what ``extract_reference_id_refs`` finds.
    #
    # The second is how nested calculated fields are actually stored, and
    # following only the first silently drops 43.7% of this tenant's fields'
    # dependencies (measured live). See `ordering.extract_reference_id_refs`.
    by_reference_id = {
        summary.reference_id: summary
        for summary in cf_index.summaries.values()
        if getattr(summary, "reference_id", None)
    }

    pending = list(closure.nodes.values())
    edges: dict[str, set[str]] = {n.node_id: set() for n in pending}

    def _link(dep: Node | None, dep_id: str, from_node: Node) -> None:
        """Record an edge, adding the dependency to the closure if it's new."""
        if dep_id == from_node.node_id:
            return
        if dep_id not in closure.nodes:
            if dep is None:
                # Recording an edge to a node that will never exist would leave
                # a dangling dependency that `build_dag` silently drops.
                return
            closure.nodes[dep_id] = dep
            edges.setdefault(dep_id, set())
            pending.append(dep)
        edges.setdefault(from_node.node_id, set()).add(dep_id)

    def _link_field(dep_wid: str, from_node: Node) -> None:
        dep_id = node_id_for(NodeKind.CALCULATED_FIELD, dep_wid)
        existing = closure.nodes.get(dep_id)
        _link(
            existing or _calculated_field_node(dep_wid, cf_index, selected=False),
            dep_id,
            from_node,
        )

    while pending:
        node = pending.pop()
        payload = _dependency_payload(node)

        # Measures first, so their WIDs are known before the generic WID walk
        # below would otherwise record them as pass-throughs.
        measures: dict[str, str] = (
            extract_measure_refs(payload) if measure_loader is not None else {}
        )
        for measure_wid, business_id in measures.items():
            if measure_wid == node.source_wid:
                continue
            dep_id = node_id_for(NodeKind.CALCULATED_MEASURE, measure_wid)
            dep = closure.nodes.get(dep_id)
            if dep is None:
                fetched = measure_loader(measure_wid)
                if fetched is None:
                    closure.unresolved_measure_ids.add(business_id)
                    continue
                dep = _measure_node(measure_wid, fetched)
            _link(dep, dep_id, node)

        reports: dict[str, str] = (
            extract_report_refs(payload) if report_loader is not None else {}
        )
        for report_wid, report_id in reports.items():
            if report_wid == node.source_wid:
                continue  # a report can name itself; see writer._strip_self_references
            dep_id = node_id_for(NodeKind.REPORT, report_wid)
            dep = closure.nodes.get(dep_id)
            if dep is None:
                fetched = report_loader(report_wid)
                if fetched is None:
                    closure.unresolved_report_ids.add(report_id)
                    continue
                dep = _report_node(report_wid, fetched, selected=False)
            _link(dep, dep_id, node)

        for ref_wid in extract_wid_refs(payload, exclude=[node.source_wid]):
            if ref_wid in measures or ref_wid in reports:
                continue  # already handled as a measure or a sub-report
            if ref_wid not in cf_index:
                closure.passthrough_wids.add(ref_wid)
                continue
            _link_field(ref_wid, node)

        for ref_id in extract_reference_id_refs(payload):
            summary = by_reference_id.get(ref_id)
            if summary is None:
                # The payload says this is a calculated field, so unlike an
                # unmatched WID this is not a delivered object passing through.
                closure.unresolved_reference_ids.add(ref_id)
                continue
            _link_field(summary.wid, node)

    # Freeze edges onto the nodes, and record the reverse direction so review
    # can answer "why is this object in my migration?".
    required_by: dict[str, set[str]] = {node_id: set() for node_id in closure.nodes}
    for node_id, deps in edges.items():
        for dep_id in deps:
            required_by.setdefault(dep_id, set()).add(node_id)

    closure.nodes = {
        node_id: Node(
            **{
                **node.__dict__,
                "depends_on": frozenset(edges.get(node_id, set())),
                "required_by": frozenset(required_by.get(node_id, set())),
            }
        )
        for node_id, node in closure.nodes.items()
    }

    return closure
