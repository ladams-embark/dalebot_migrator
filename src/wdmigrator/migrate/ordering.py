"""Dependency ordering and WID substitution. Pure logic, no tenant calls.

Two jobs, both of which have to be exactly right because the failure modes are
silent and unrecoverable:

**Ordering.** A calculated field that references another must be created after
the one it references, so the parent's payload can point at a WID that already
exists in the destination. Kahn's algorithm, emitting child-most first.

**Substitution.** A custom object gets a *brand new WID* when it is created in
the destination. Every downstream reference to it has to be rewritten from the
source WID to the destination WID. A WID that is not in the map is left alone —
that is how Workday-delivered objects, which share WIDs across all tenants,
pass through untouched.

The ordering is deterministic: ties break on ``node_id``. That matters because
the plan hash is computed over this order, and a hash that changes between two
identical runs would keep invalidating the user's dry-run approval.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping, Sequence


class CycleError(ValueError):
    """The dependency graph contains a cycle, so no safe PUT order exists.

    Should not happen in valid Workday configuration — a calculated field
    cannot really depend on itself transitively. If it does, something is
    wrong with the extraction, and guessing an order could create objects with
    dangling references.
    """

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        super().__init__(
            "Dependency cycle detected, cannot determine a safe PUT order: "
            + " -> ".join(self.cycle)
        )


def build_dag(nodes: Mapping[str, Any]) -> dict[str, set[str]]:
    """Adjacency of ``node_id -> {node_ids it depends on}``.

    Dependencies pointing outside ``nodes`` are dropped: they are objects not
    being migrated (delivered fields, or things already present in the
    destination), and they impose no ordering constraint on this run.
    """
    known = set(nodes)
    return {
        node_id: {dep for dep in getattr(node, "depends_on", ()) or () if dep in known}
        for node_id, node in nodes.items()
    }


def topological_sort(nodes: Mapping[str, Any]) -> list[Any]:
    """Order nodes child-most first, so every dependency precedes its dependents.

    Deterministic — ties are broken by ``node_id`` rather than by dict order,
    so the same input always produces the same order and therefore the same
    plan hash.
    """
    dag = build_dag(nodes)
    remaining = {node_id: set(deps) for node_id, deps in dag.items()}
    ordered: list[Any] = []

    while remaining:
        ready = sorted(
            node_id for node_id, deps in remaining.items() if not deps
        )
        if not ready:
            raise CycleError(_find_cycle(remaining))

        for node_id in ready:
            ordered.append(nodes[node_id])
            del remaining[node_id]

        for deps in remaining.values():
            deps.difference_update(ready)

    return ordered


def _find_cycle(remaining: Mapping[str, set[str]]) -> list[str]:
    """Walk the unresolved subgraph to produce a concrete cycle for the error.

    A cycle the user can actually see beats "a cycle exists somewhere".
    """
    start = min(remaining)
    seen: list[str] = []
    current = start

    while current not in seen:
        seen.append(current)
        candidates = sorted(d for d in remaining.get(current, ()) if d in remaining)
        if not candidates:
            break
        current = candidates[0]

    if current in seen:
        return seen[seen.index(current):] + [current]
    return seen


def substitute_wids(obj: Any, wid_map: Mapping[str, str]) -> Any:
    """Deep-copy ``obj``, rewriting any mapped source WID to its destination WID.

    Only entries whose ``type`` is exactly ``WID`` are rewritten. Business IDs
    such as ``Calculated_Field_ID`` are stable across tenants and must survive
    untouched — rewriting one would break the very identity the migration uses
    to match objects up.

    WIDs absent from ``wid_map`` are left alone. That is not an oversight: an
    unmapped WID is a Workday-delivered object, identical in every tenant.
    """
    if not wid_map:
        return copy.deepcopy(obj)
    return _substitute(copy.deepcopy(obj), wid_map)


def _substitute(obj: Any, wid_map: Mapping[str, str]) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "ID" and isinstance(value, list):
                for entry in value:
                    if (
                        isinstance(entry, dict)
                        and entry.get("type") == "WID"
                        and entry.get("_value_1") in wid_map
                    ):
                        entry["_value_1"] = wid_map[entry["_value_1"]]
            else:
                obj[key] = _substitute(value, wid_map)
        return obj

    if isinstance(obj, list):
        return [_substitute(item, wid_map) for item in obj]

    return obj


def extract_wid_refs(obj: Any, exclude: Iterable[str] = ()) -> set[str]:
    """Every WID referenced anywhere inside ``obj``.

    Walks the serialized structure looking for ``ID`` lists rather than trying
    to understand each of the ~34 polymorphic calculated-field sub-types. New
    sub-types therefore need no code change here.
    """
    found: set[str] = set()
    _collect(obj, found)
    return found - set(exclude)


def _collect(obj: Any, found: set[str]) -> None:
    if isinstance(obj, dict):
        entries = obj.get("ID")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("type") == "WID":
                    value = entry.get("_value_1")
                    if value:
                        found.add(value)
        for value in obj.values():
            _collect(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect(item, found)


def unmapped_wids(obj: Any, wid_map: Mapping[str, str], custom: Iterable[str]) -> set[str]:
    """Custom WIDs still present in ``obj`` that have no destination mapping.

    A non-empty result means the payload would be written referencing an object
    that does not exist in the destination — i.e. the ordering is wrong or a
    dependency was skipped. Callers should treat it as a blocker, not a warning.
    """
    return extract_wid_refs(obj) & set(custom) - set(wid_map)
