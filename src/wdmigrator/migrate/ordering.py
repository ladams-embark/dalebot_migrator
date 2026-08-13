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


#: The key a calculated field uses to name *another* calculated field it
#: depends on. Confirmed live 2026-08-05 on `commitconsulting` (wd501).
_CF_REFERENCE_ID_KEY = "Calculated_Field_Reference_ID"


def extract_reference_id_refs(obj: Any) -> set[str]:
    """Every nested ``Calculated_Field_Reference_ID`` inside ``obj``.

    **This is the primary way one calculated field names another, and it is
    invisible to :func:`extract_wid_refs`.** The "Add or Reference" structures
    that carry a nested field — ``Business_Object_Field``, ``Condition_Field``,
    ``Related_Field``, ``Sort_Field``, ``Default_Value_Field`` and a dozen more
    — identify it by a bare ``Calculated_Field_Reference_ID`` string, sitting
    directly in the block rather than inside an ``ID`` list, with the WID slot
    (``Class_Report_Field_Reference``) left null::

        "Business_Object_Field": [{
            "Class_Report_Field_Reference": null,
            "Calculated_Field_Reference_ID": "LRV_Global_Top_Supervisory_...",
            "Calculated_Field_Name": "LRV Global Top Supervisory ...",
            "Business_Object_Reference": {"ID": [{"type": "WID", ...}]}
        }]

    The only WID in that block belongs to the *business object* the field lives
    on, not to the field, so a WID-only walk records a pass-through and drops
    the dependency silently. Measured on `commitconsulting`: 612 of 1,399
    calculated fields (43.7%) reference another one exclusively this way, 1,035
    references in total, all of them resolvable in the index.

    A top-level ``Calculated_Field_Reference_ID`` is the field's *own* id and is
    excluded — only nested occurrences are dependencies.

    Because these references are business IDs rather than WIDs, they are stable
    across tenants and need no remapping by :func:`substitute_wids`. What they
    do need is for the referenced field to already exist in the destination,
    which is exactly what including them in the closure and ordering child-most
    first achieves.
    """
    found: set[str] = set()
    _collect_reference_ids(obj, found, at_root=True)
    return found


def _collect_reference_ids(obj: Any, found: set[str], *, at_root: bool) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == _CF_REFERENCE_ID_KEY:
                if not at_root and isinstance(value, str) and value:
                    found.add(value)
                continue
            _collect_reference_ids(value, found, at_root=False)
    elif isinstance(obj, list):
        for item in obj:
            _collect_reference_ids(item, found, at_root=False)


#: The ID type that marks a reference as pointing at a calculated measure.
#: Matching on this rather than on element names is deliberate: the same object
#: is reached through at least three differently-named elements
#: (``Summary_Calculation_Reference``, ``Matrix_Measure__All__Reference``,
#: ``Calculated_Measure_Reference``), and a name-based list would silently miss
#: whichever one nobody thought of.
_MEASURE_ID_TYPE = "BI_Calculated_Measure_ID"


def extract_measure_refs(obj: Any) -> dict[str, str]:
    """Every calculated-measure reference inside ``obj``, as ``{wid: business_id}``.

    Both identifiers are returned because both are needed and they do different
    jobs: the **WID** is how the source payload names the measure and therefore
    what :func:`substitute_wids` has to rewrite, while the
    ``BI_Calculated_Measure_ID`` is the stable cross-tenant identity used to
    look the measure up and to decide whether the destination already has it.

    A reference carrying only a WID is not returned — without a business ID
    there is nothing to match on in the destination, so it cannot be resolved
    into a migratable dependency.
    """
    found: dict[str, str] = {}
    _collect_measures(obj, found)
    return found


def _collect_measures(obj: Any, found: dict[str, str]) -> None:
    if isinstance(obj, dict):
        entries = obj.get("ID")
        if isinstance(entries, list):
            ids = {
                e.get("type"): e.get("_value_1")
                for e in entries
                if isinstance(e, dict) and e.get("type")
            }
            business = ids.get(_MEASURE_ID_TYPE)
            wid = ids.get("WID")
            if business and wid:
                found[wid] = business
        for value in obj.values():
            _collect_measures(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect_measures(item, found)


#: The ID type marking a reference to another report. Same reasoning as
#: :data:`_MEASURE_ID_TYPE` — sub-reports are reached through
#: ``Report_Definition__All__Reference``, ``Report_Definition_Reference`` and
#: ``Maximized_*_Reference``, so match on the ID type rather than the name.
_REPORT_ID_TYPE = "Custom_Report_ID"


def extract_report_refs(obj: Any) -> dict[str, str]:
    """Every report-to-report reference inside ``obj``, as ``{wid: custom_report_id}``.

    A composite report names its sub-reports this way, and a sub-report has to
    exist in the destination before the composite that renders it. Measured on
    `commitconsulting`: without following these, a composite resolves to a
    closure of exactly one object — itself — and lands in the destination
    referencing a report that was never created.

    Both identifiers are returned, but note they are *not* symmetric. The WID is
    what :func:`substitute_wids` must rewrite. The ``Custom_Report_ID`` is
    returned for identification and display only — it is **rejected as a lookup
    key** by this API (verified on 18/18 sampled reports), which is why reports
    are matched across tenants by exact name instead.
    """
    found: dict[str, str] = {}
    _collect_reports(obj, found)
    return found


def _collect_reports(obj: Any, found: dict[str, str]) -> None:
    if isinstance(obj, dict):
        entries = obj.get("ID")
        if isinstance(entries, list):
            ids = {
                e.get("type"): e.get("_value_1")
                for e in entries
                if isinstance(e, dict) and e.get("type")
            }
            report_id = ids.get(_REPORT_ID_TYPE)
            wid = ids.get("WID")
            if report_id and wid:
                found[wid] = report_id
        for value in obj.values():
            _collect_reports(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect_reports(item, found)


def _collect_by_id_type(obj: Any, id_types: Iterable[str], found: dict) -> None:
    """Every reference carrying a WID *and* one of ``id_types``.

    Records ``{wid: (id_type, business_id)}``. Both halves are needed and do
    different jobs — the WID is what :func:`substitute_wids` rewrites, the
    business ID is the cross-tenant identity used to look the object up — and a
    reference carrying only one of them cannot be resolved into a migratable
    dependency, so it is skipped.
    """
    wanted = tuple(id_types)
    if isinstance(obj, dict):
        entries = obj.get("ID")
        if isinstance(entries, list):
            ids = {
                e.get("type"): e.get("_value_1")
                for e in entries
                if isinstance(e, dict) and e.get("type")
            }
            wid = ids.get("WID")
            if wid:
                for id_type in wanted:
                    if ids.get(id_type):
                        found[wid] = (id_type, ids[id_type])
                        break
        for value in obj.values():
            _collect_by_id_type(value, wanted, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect_by_id_type(item, wanted, found)


_PROMPT_SET_ID_TYPE = "Prompt_Set_ID"


_ANALYTIC_INDICATOR_ID_TYPE = "Analytic_Indicator_ID"


def extract_analytic_indicator_refs(obj: Any) -> dict[str, str]:
    """Analytic-indicator references inside ``obj``, as ``{wid: id}``.

    A matrix measure names one through ``Matrix_Display_Option_Reference``.
    Unlike prompt fields and gauge ranges, most of these need no migration at
    all — indicator WIDs are shared across tenants — but the ones the
    destination lacks have to be created first, and the ones that exist nowhere
    have to be dropped rather than block the run.
    """
    collected: dict[str, tuple[str, str]] = {}
    _collect_by_id_type(obj, (_ANALYTIC_INDICATOR_ID_TYPE,), collected)
    return {wid: business for wid, (_, business) in collected.items()}


#: A report gauge colour banding. Carries a business id when custom; a
#: Workday-delivered range would carry none and pass through.
_GAUGE_RANGE_ID_TYPE = "Custom_Analytic_Range_ID"


def extract_gauge_range_refs(obj: Any) -> dict[str, str]:
    """Custom gauge-range references inside ``obj``, as ``{wid: id}``.

    A report gauge layout points at one through ``Analytic_Range_Reference``,
    and the report cannot be written before it exists — confirmed live
    2026-08-12, ``Put_Tenanted_Report_Definition`` failed for
    `Benefits - OE Submission %` on a WID that resolves as no migratable kind.

    Same delivered-vs-custom split as prompt fields: only references carrying
    ``Custom_Analytic_Range_ID`` become dependencies.
    """
    collected: dict[str, tuple[str, str]] = {}
    _collect_by_id_type(obj, (_GAUGE_RANGE_ID_TYPE,), collected)
    return {wid: business for wid, (_, business) in collected.items()}


#: A prompt set member parameter. Only the *custom* ones carry this business
#: id — Workday-delivered prompt parameters (Effective Date, Supervisory
#: Organization) come back as a bare WID with no id at all, and pass through
#: unchanged because their WID is the same in every tenant.
_PROMPT_FIELD_ID_TYPE = "TenantedExternalParameter"


def extract_prompt_field_refs(obj: Any) -> dict[str, str]:
    """Custom prompt-field references inside ``obj``, as ``{wid: id}``.

    A prompt set cannot be written before its parameters exist: confirmed live
    2026-08-12, ``Put_Prompt_Set`` fails with ``Invalid ID value ... for type =
    'WID'`` naming an ``Abstract_External_Parameter_Reference``.

    Only references carrying a ``TenantedExternalParameter`` id are returned.
    That is the delivered-vs-custom split, and it is load-bearing: the
    `Commit - HR Dashboard` prompt set's five members are all WID-only and
    appear in *neither* tenant's ``Get_Prompt_Fields``, so they are delivered
    parameters that need no migration at all. Returning them would invent a
    dependency that cannot be satisfied.
    """
    collected: dict[str, tuple[str, str]] = {}
    _collect_by_id_type(obj, (_PROMPT_FIELD_ID_TYPE,), collected)
    return {wid: business for wid, (_, business) in collected.items()}


def extract_prompt_set_refs(obj: Any) -> dict[str, str]:
    """Every prompt-set reference inside ``obj``, as ``{wid: Prompt_Set_ID}``.

    A dashboard binds its runtime prompts to a prompt set, which has to exist in
    the destination first. Measured live on `commitconsulting_dpt1`: 32 of 179
    dashboards reference one, so this is a real but minority dependency.

    ``Prompt_Set_ID`` is the prompt set's own name (`'Company'`, `'Start and End
    Dates'`) and works as a lookup key, unlike ``Custom_Report_ID``.
    """
    collected: dict[str, tuple[str, str]] = {}
    _collect_by_id_type(obj, (_PROMPT_SET_ID_TYPE,), collected)
    return {wid: business for wid, (_, business) in collected.items()}


#: The two dashboard ID spaces. Disjoint, and which one appears tells you the
#: flavour — and therefore which Get/Put operation addresses the dashboard.
_DASHBOARD_ID_TYPES = {
    "Custom_Landing_Page_Group_ID": True,   # tabbed
    "Custom_Landing_Page_ID": False,        # untabbed
}


def extract_dashboard_refs(obj: Any) -> dict[str, tuple[str, bool]]:
    """Every dashboard reference inside ``obj``, as ``{wid: (reference_id, tabbed)}``.

    Dashboards nest: a dashboard's worklet can be another dashboard, reached
    through ``Landing_Page__All__Reference``. Measured live on
    `commitconsulting_dpt1`, 226 such references across the untabbed dashboards
    alone, so a dashboard closure that ignored them would write a dashboard
    pointing at one that was never created.

    ``tabbed`` is derived from which ID space the reference uses, and is carried
    forward because the two flavours are addressed by different operations.
    ``Landing_Page__All__Reference`` can also name a Workday-delivered landing
    page (``Landing_Page_ID`` / ``Landing_Page_Group_ID``); those are not custom
    dashboards, carry no custom ID, and are correctly skipped here — they pass
    through as delivered objects.
    """
    collected: dict[str, tuple[str, str]] = {}
    _collect_by_id_type(obj, tuple(_DASHBOARD_ID_TYPES), collected)
    return {
        wid: (business, _DASHBOARD_ID_TYPES[id_type])
        for wid, (id_type, business) in collected.items()
    }


def unmapped_wids(obj: Any, wid_map: Mapping[str, str], custom: Iterable[str]) -> set[str]:
    """Custom WIDs still present in ``obj`` that have no destination mapping.

    A non-empty result means the payload would be written referencing an object
    that does not exist in the destination — i.e. the ordering is wrong or a
    dependency was skipped. Callers should treat it as a blocker, not a warning.
    """
    return extract_wid_refs(obj) & set(custom) - set(wid_map)
