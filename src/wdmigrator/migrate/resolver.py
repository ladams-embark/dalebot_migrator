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
from typing import Iterable, Mapping

from wdmigrator.discovery.inventory import Index, ids_of
from wdmigrator.migrate.ordering import extract_wid_refs


class NodeKind(str, Enum):
    CALCULATED_FIELD = "calculated_field"
    REPORT = "report"


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


def _dependency_payload(node: Node) -> dict:
    """The part of a node worth scanning for references.

    Scoped to the data block so the object's own reference block does not
    register as a dependency on itself.
    """
    if node.kind is NodeKind.REPORT:
        return node.payload.get("Tenanted_Report_Definition_Data") or {}
    return node.payload.get("Calculated_Field_Data") or {}


def resolve_closure(
    *,
    cf_index: Index,
    selected_field_wids: Iterable[str] = (),
    selected_reports: Mapping[str, dict] | None = None,
    allow_partial_index: bool = False,
    expected_index_size: int | None = None,
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

    Returns:
        A :class:`Closure` whose nodes carry ``depends_on`` edges, ready for
        :func:`~wdmigrator.migrate.ordering.topological_sort`.
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

    # Expand transitively. Every reference that names a calculated field in the
    # index becomes an edge; everything else is a pass-through.
    pending = list(closure.nodes.values())
    edges: dict[str, set[str]] = {n.node_id: set() for n in pending}

    while pending:
        node = pending.pop()
        refs = extract_wid_refs(_dependency_payload(node), exclude=[node.source_wid])

        for ref_wid in refs:
            if ref_wid not in cf_index:
                closure.passthrough_wids.add(ref_wid)
                continue

            dep_id = node_id_for(NodeKind.CALCULATED_FIELD, ref_wid)
            edges.setdefault(node.node_id, set()).add(dep_id)

            if dep_id not in closure.nodes:
                dep = _calculated_field_node(ref_wid, cf_index, selected=False)
                if dep is None:  # pragma: no cover - guarded by the `in` check
                    continue
                closure.nodes[dep_id] = dep
                edges.setdefault(dep_id, set())
                pending.append(dep)

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
