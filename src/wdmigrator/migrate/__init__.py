"""Dependency resolution, ordering, planning, and destination writes.

`ordering.py` and `resolver.py` are pure — no tenant calls — so the
highest-risk logic in the project is testable offline in the fast inner loop.

Every writer function defaults to dry_run=True.
"""

from wdmigrator.migrate.ordering import (
    CycleError,
    build_dag,
    extract_wid_refs,
    substitute_wids,
    topological_sort,
    unmapped_wids,
)
from wdmigrator.migrate.resolver import (
    Closure,
    Node,
    NodeKind,
    PartialIndexError,
    node_id_for,
    resolve_closure,
)

__all__ = [
    "Closure",
    "CycleError",
    "Node",
    "NodeKind",
    "PartialIndexError",
    "build_dag",
    "extract_wid_refs",
    "node_id_for",
    "resolve_closure",
    "substitute_wids",
    "topological_sort",
    "unmapped_wids",
]
