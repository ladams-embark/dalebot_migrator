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
from wdmigrator.migrate.planner import (
    Action,
    Blocker,
    Existence,
    MigrationPlan,
    ProbeProgress,
    build_plan,
    default_action,
    iter_check_existence,
    probe_node,
    validate_plan,
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
    "Action",
    "Blocker",
    "Closure",
    "CycleError",
    "Existence",
    "MigrationPlan",
    "Node",
    "NodeKind",
    "PartialIndexError",
    "ProbeProgress",
    "build_dag",
    "build_plan",
    "default_action",
    "extract_wid_refs",
    "iter_check_existence",
    "node_id_for",
    "probe_node",
    "resolve_closure",
    "substitute_wids",
    "topological_sort",
    "unmapped_wids",
    "validate_plan",
]
