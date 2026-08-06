"""Dependency resolution, ordering, planning, and destination writes.

`ordering.py` and `resolver.py` are pure — no tenant calls — so the
highest-risk logic in the project is testable offline in the fast inner loop.

Every writer function defaults to dry_run=True.
"""

from wdmigrator.migrate.ordering import (
    CycleError,
    build_dag,
    extract_measure_refs,
    extract_reference_id_refs,
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
from wdmigrator.migrate.writer import (
    ExceptionDetail,
    WriteError,
    WriteProgress,
    WriteRecord,
    WriteStatus,
    build_calculated_field_payload,
    build_calculated_measure_payload,
    build_owner_reference,
    build_report_payload,
    extract_exceptions,
    is_failure,
    iter_execute,
    operation_for,
    serialize_envelope,
    summarise,
    write_node,
)

__all__ = [
    "Action",
    "Blocker",
    "Closure",
    "CycleError",
    "ExceptionDetail",
    "Existence",
    "MigrationPlan",
    "Node",
    "NodeKind",
    "PartialIndexError",
    "ProbeProgress",
    "WriteError",
    "WriteProgress",
    "WriteRecord",
    "WriteStatus",
    "build_calculated_field_payload",
    "build_calculated_measure_payload",
    "build_dag",
    "build_owner_reference",
    "build_plan",
    "build_report_payload",
    "default_action",
    "extract_exceptions",
    "extract_measure_refs",
    "extract_reference_id_refs",
    "extract_wid_refs",
    "is_failure",
    "iter_check_existence",
    "iter_execute",
    "node_id_for",
    "operation_for",
    "probe_node",
    "resolve_closure",
    "serialize_envelope",
    "substitute_wids",
    "summarise",
    "topological_sort",
    "unmapped_wids",
    "validate_plan",
    "write_node",
]
