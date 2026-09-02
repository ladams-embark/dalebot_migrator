"""Deciding what to do with each object, before anything is written.

This is the gate the user reviews. For every object in the closure it asks the
destination "do you already have this?" and turns the answer into an action.

**Existence is three-valued.** A targeted lookup that misses raises a fault —
but so does a permissions problem, a timeout, or an entitlement gap. Only the
specific not-found faults mean *absent*; everything else is ``UNKNOWN`` and
blocks the run. Collapsing UNKNOWN into "missing" would mean "create", and
creating something that already exists produces duplicates in a tenant with no
delete operation.

**Existing objects default to SKIP, not UPDATE.** Overwriting destination
configuration is the destructive case, and whether ``Put_Calculated_Field``
with a reference replaces or merges is still unverified. UPDATE is available
but never automatic.

**Destination WIDs of existing objects are captured and seeded into the WID
map.** This is easy to miss and breaks silently: if a dependency already exists
and is skipped, anything referencing it must still be rewritten to point at the
*destination's* WID. Without seeding, the parent would be written pointing at a
source WID that means nothing there.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Mapping

from wdmigrator.auth.client import Connection
from wdmigrator.discovery.inventory import (
    CalculatedFieldMatchIndex,
    LookupOutcome,
    calculated_field_data,
    calculated_field_shape,
    calculated_measure_shape,
    dashboard_has_worklets,
    lookup_calculated_field,
    lookup_calculated_measure,
    lookup_dashboard,
    lookup_analytic_indicator,
    lookup_gauge_range,
    lookup_prompt_field,
    lookup_prompt_set,
    lookup_report_by_name,
    lookup_time_calculation,
    lookup_time_calculation_group,
    lookup_time_calculation_tag,
)
from wdmigrator.migrate.ordering import topological_sort
from wdmigrator.migrate.resolver import (
    DASHBOARD_TABBED_BY_KIND,
    Closure,
    Node,
    NodeKind,
    TIME_TRACKING_KINDS,
)


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    SKIP = "skip"


@dataclass(frozen=True)
class Existence:
    """What the destination said about one object."""

    node_id: str
    state: LookupOutcome
    dest_wid: str | None = None
    fault: str | None = None
    #: How the match was made, when it was not the object's own business ID.
    #: Recorded so the review step can show that a field was matched on shape
    #: rather than identity — a weaker claim the user should be able to see.
    matched_by: str | None = None
    #: The destination's OWN business id, when it differs from the source's.
    #: Nested calculated-field references are by business id, so a reused field
    #: needs them rewritten or they dangle. Only set on a cross-tenant match —
    #: an id match means the two agree already.
    dest_reference_id: str | None = None
    #: True when the destination holds this object but it's an empty shell — a
    #: dashboard that failed mid-write and left its admin config behind with no
    #: worklets. A shell probes as FOUND and would default to SKIP, so every
    #: subsequent run silently leaves it broken. Marking the shell here routes
    #: :func:`default_action` to UPDATE for the one case (dashboard completion)
    #: where UPDATE is empirically safe (confirmed live 2026-08-13 —
    #: HANDOFF.md).
    is_shell: bool = False

    @property
    def exists(self) -> bool:
        return self.state is LookupOutcome.FOUND

    @property
    def is_unknown(self) -> bool:
        return self.state is LookupOutcome.UNKNOWN


class ReferenceAction(str, Enum):
    """What to do with a reference the destination cannot resolve.

    ``KEEP`` is the "leave the source WID in the payload" option. It's the same
    thing that happens when no decision is recorded, but expressed explicitly
    so the preflight table can offer it as a chosen answer alongside BLANK /
    REPLACE — the case for that is delivered content (event classifications,
    business process types, currencies) whose WID is stable across every
    tenant, and where blanking a valid default would degrade the report.
    """

    BLANK = "blank"
    REPLACE = "replace"
    KEEP = "keep"


@dataclass(frozen=True)
class ReferenceDecision:
    """A human's answer to "the destination has no such object".

    Some references point at tenant *data* rather than configuration — a prompt
    defaulting to a particular Organization, a filter comparing against a
    specific Cost Center. Those cannot migrate: the instance simply is not there,
    and no amount of dependency resolution will conjure it. Confirmed live, an
    Organization reference failed by WID *and* by its ``Organization_Reference_ID``
    business id, because that organization does not exist in the destination at
    all.

    So the choice is genuinely the user's: drop the value, or point it at
    something that does exist. Keyed on the **source** WID, which is what
    appears in the payload before any remapping and what the fault names.
    """

    source_wid: str
    action: ReferenceAction
    replacement_type: str | None = None
    replacement_value: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.action is ReferenceAction.REPLACE and not (
            self.replacement_type and self.replacement_value
        ):
            raise ValueError(
                "A REPLACE decision needs both replacement_type and "
                "replacement_value — e.g. ('Organization_Reference_ID', 'TOP')."
            )


@dataclass(frozen=True)
class Blocker:
    """A reason the plan cannot be executed live, phrased for the UI."""

    node_id: str | None
    title: str
    detail: str
    remedy: str


@dataclass
class ProbeProgress:
    """Emitted per probed object so the UI can show progress and cancel."""

    checked: int
    total: int
    node: Node
    existence: Existence

    @property
    def fraction(self) -> float:
        return min(1.0, self.checked / self.total) if self.total else 0.0


@dataclass
class MigrationPlan:
    """The reviewed, ordered plan. Everything the writer needs."""

    ordered_nodes: list[Node] = field(default_factory=list)
    existence: dict[str, Existence] = field(default_factory=dict)
    actions: dict[str, Action] = field(default_factory=dict)
    #: source WID -> destination WID, pre-seeded with objects that already
    #: exist in the destination. Grows as the writer creates things.
    wid_map: dict[str, str] = field(default_factory=dict)
    #: Carried forward from the closure so `validate_plan` can refuse a plan
    #: with known-missing dependencies. Without this the check lived only in the
    #: Streamlit Resolve step, and any other caller — a CLI, a script — would
    #: happily write objects referencing things that were never resolved.
    unresolved_reference_ids: frozenset[str] = frozenset()
    unresolved_measure_ids: frozenset[str] = frozenset()
    unresolved_report_ids: frozenset[str] = frozenset()
    unresolved_prompt_set_ids: frozenset[str] = frozenset()
    unresolved_prompt_field_ids: frozenset[str] = frozenset()
    unresolved_gauge_range_ids: frozenset[str] = frozenset()
    #: Analytic indicators readable on neither tenant. Carried so the
    #: writer can drop the optional display option naming them rather
    #: than failing the write. Deliberately NOT a blocker.
    unmigratable_indicator_wids: frozenset[str] = frozenset()
    unresolved_dashboard_ids: frozenset[str] = frozenset()
    unresolved_time_calculation_tag_ids: frozenset[str] = frozenset()
    unresolved_time_calculation_group_ids: frozenset[str] = frozenset()
    #: source Calculated_Field_ID -> destination Calculated_Field_ID, for
    #: fields the destination already had under a different name. Seeded from
    #: the probe exactly as ``wid_map`` is, and applied to nested references.
    reference_id_map: dict[str, str] = field(default_factory=dict)
    #: source WID -> what to do with a reference the destination cannot resolve.
    #: Survives a retry, so the same question is never asked twice, and is
    #: included in the plan hash so a decision invalidates a prior dry run.
    reference_decisions: dict[str, ReferenceDecision] = field(default_factory=dict)

    def action_for(self, node: Node) -> Action:
        return self.actions.get(node.node_id, Action.SKIP)

    def counts(self) -> dict[str, int]:
        counts = {a.value: 0 for a in Action}
        for action in self.actions.values():
            counts[action.value] += 1
        return counts

    def unknown_nodes(self) -> list[str]:
        return [n for n, e in self.existence.items() if e.is_unknown]

    @property
    def writes_planned(self) -> int:
        return sum(1 for a in self.actions.values() if a is not Action.SKIP)

    def plan_hash(self) -> str:
        """Stable fingerprint of *what would be written*.

        Covers the ordered node list and the chosen actions, so editing either
        one invalidates a previous dry-run approval. Deliberately excludes
        probe faults and timings, which vary between runs without changing the
        outcome.
        """
        payload = json.dumps(
            {
                "order": [n.node_id for n in self.ordered_nodes],
                "actions": {k: v.value for k, v in sorted(self.actions.items())},
                # A reference decision changes the bytes on the wire, so it has
                # to invalidate a dry run reviewed before it was made.
                "reference_decisions": {
                    wid: [d.action.value, d.replacement_type, d.replacement_value]
                    for wid, d in sorted(self.reference_decisions.items())
                },
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def default_action(existence: Existence) -> Action:
    """Map a probe result to a starting action.

    EXISTS defaults to SKIP rather than UPDATE: overwriting destination config
    is the destructive direction, and replace-vs-merge semantics are unverified.

    The one exception is a shell dashboard — a dashboard that failed mid-write
    and left its admin config in place but every tab empty. It probes as FOUND
    and would otherwise SKIP forever, so every subsequent run silently leaves
    it broken. UPDATE has been verified live for exactly this case (HANDOFF.md,
    2026-08-13), so a shell routes to UPDATE.
    """
    if existence.state is LookupOutcome.NOT_FOUND:
        return Action.CREATE
    if existence.is_shell:
        return Action.UPDATE
    return Action.SKIP


def probe_node(
    connection: Connection,
    node: Node,
    *,
    match_index: CalculatedFieldMatchIndex | None = None,
    measure_match_index: Mapping[tuple[str, str], list[str]] | None = None,
) -> Existence:
    """Ask the destination whether one object already exists.

    The kinds are matched differently, and not by choice:

    - **Calculated fields** match on ``Calculated_Field_ID`` first. That was
      believed to be a stable cross-tenant identifier and is not — pass
      ``match_index`` (from
      :func:`~wdmigrator.discovery.inventory.calculated_field_match_index`) to
      re-check a miss against name + class + business object, and then WQL
      alias, before concluding the field is absent.
    - **Reports** match on their **name**, because ``Custom_Report_ID`` is
      returned by the API but rejected as a lookup key (see
      :func:`~wdmigrator.discovery.inventory.lookup_report_by_name`). Name is a
      weaker identity, and a duplicated name resolves to UNKNOWN rather than a
      guess.
    - **Dashboards and prompt sets** match on their business ID, which for both
      is the object's own name and — unlike ``Custom_Report_ID`` — genuinely
      works as a lookup key (confirmed live 2026-08-07). Dashboards additionally
      carry their flavour, since the two are addressed by different operations.
    """
    if node.kind in DASHBOARD_TABBED_BY_KIND:
        if not node.reference_id:
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    "Dashboard has no Custom_Landing_Page ID, so it cannot be "
                    "matched against the destination."
                ),
            )
        result = lookup_dashboard(
            connection,
            tabbed=DASHBOARD_TABBED_BY_KIND[node.kind],
            reference_id=node.reference_id,
        )
        if result.outcome is LookupOutcome.FOUND and not dashboard_has_worklets(result.data):
            # A dashboard that failed partway through a previous run leaves its
            # admin config in place but every tab empty. Without this it probes
            # FOUND and defaults to SKIP forever; with it, default_action routes
            # to UPDATE so the run can complete what a prior one couldn't.
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.FOUND,
                dest_wid=result.wid,
                is_shell=True,
                matched_by=(
                    "destination holds this dashboard but it has no worklets — "
                    "likely a shell from a mid-run failure of a prior migration"
                ),
            )
    elif node.kind is NodeKind.ANALYTIC_INDICATOR:
        # By WID, not business id: the WID is what is stable across tenants for
        # indicators, and the business id is what is not.
        result = lookup_analytic_indicator(connection, wid=node.source_wid)
    elif node.kind is NodeKind.GAUGE_RANGE:
        if not node.reference_id:
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    "Gauge range has no Custom_Analytic_Range_ID, so it cannot "
                    "be matched against the destination."
                ),
            )
        result = lookup_gauge_range(connection, reference_id=node.reference_id)
    elif node.kind is NodeKind.PROMPT_FIELD:
        if not node.reference_id:
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    "Prompt field has no TenantedExternalParameter ID, so it "
                    "cannot be matched against the destination."
                ),
            )
        result = lookup_prompt_field(connection, reference_id=node.reference_id)
    elif node.kind is NodeKind.PROMPT_SET:
        if not node.reference_id:
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    "Prompt set has no Prompt_Set_ID, so it cannot be matched "
                    "against the destination."
                ),
            )
        result = lookup_prompt_set(connection, reference_id=node.reference_id)
    elif node.kind is NodeKind.TIME_CALCULATION:
        if not node.reference_id:
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    "Time Calculation has no Time_Calculation_ID, so it "
                    "cannot be matched against the destination."
                ),
            )
        result = lookup_time_calculation(connection, reference_id=node.reference_id)
    elif node.kind is NodeKind.TIME_CALCULATION_GROUP:
        if not node.reference_id:
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    "Time Calculation Group has no Time_Calculation_Group_ID, "
                    "so it cannot be matched against the destination."
                ),
            )
        result = lookup_time_calculation_group(connection, reference_id=node.reference_id)
    elif node.kind is NodeKind.TIME_CALCULATION_TAG:
        if not node.reference_id:
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    "Time Calculation Tag has no Time_Calculation_Tag_ID, so it "
                    "cannot be matched against the destination."
                ),
            )
        result = lookup_time_calculation_tag(connection, reference_id=node.reference_id)
    elif node.kind is NodeKind.CALCULATED_MEASURE:
        if not node.reference_id:
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    "Calculated measure has no BI_Calculated_Measure_ID, so it "
                    "cannot be matched against the destination."
                ),
            )
        result = lookup_calculated_measure(connection, reference_id=node.reference_id)
        if (
            result.outcome is LookupOutcome.NOT_FOUND
            and measure_match_index is not None
        ):
            return _match_calculated_measure_across_tenants(node, measure_match_index)
    elif node.kind is NodeKind.REPORT:
        result = lookup_report_by_name(connection, node.name or "")
    else:
        if not node.reference_id:
            # No stable cross-tenant ID to match on. A source WID would be
            # meaningless in another tenant, so there is nothing safe to try.
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    "Calculated field has no Calculated_Field_ID, so it cannot "
                    "be matched against the destination."
                ),
            )
        result = lookup_calculated_field(connection, reference_id=node.reference_id)
        if result.outcome is LookupOutcome.NOT_FOUND and match_index is not None:
            return _match_calculated_field_across_tenants(node, match_index)

    return Existence(
        node_id=node.node_id,
        state=result.outcome,
        dest_wid=result.wid,
        fault=result.fault,
    )


def _match_calculated_field_across_tenants(
    node: Node, match_index: CalculatedFieldMatchIndex
) -> Existence:
    """Second opinion on a calculated field the ID probe said was absent.

    ``Calculated_Field_ID`` is only a cross-tenant identity when both tenants
    acquired the field the same way, which two independently-built tenants have
    no reason to have done — see
    :func:`~wdmigrator.discovery.inventory.calculated_field_shape`. Three tiers
    are tried, weakest last:

    1. **Shape** — name + class + business object. The strong signal: all three
       are comparable across tenants and together they describe what the field
       *is*.
    2. **Shape, tie-broken by ``WQL_Alias``** — when several destination fields
       share a shape, one carrying the source's exact alias is that field.
    3. **Alias alone** — for a field the destination holds under a different
       name (`CF LRV Benefit Group` against dpt3's `Benefit Group`). Weakest,
       because it asserts identity on a query nickname.

    Every tier returns UNKNOWN rather than guessing when it finds more than one
    candidate. Picking arbitrarily would wire dependents to whichever field was
    swept first, which is both wrong and invisible.
    """
    shape = calculated_field_shape(node.payload)
    source_alias = calculated_field_data(node.payload).get("WQL_Alias")
    source_alias = str(source_alias) if source_alias else None

    if shape is None and not source_alias:
        return Existence(
            node_id=node.node_id,
            state=LookupOutcome.NOT_FOUND,
            fault=(
                "No Calculated_Field_ID match, and the field has neither a "
                "complete shape nor a WQL alias to match on."
            ),
        )

    candidates = list(match_index.by_shape.get(shape) or []) if shape else []

    if len(candidates) == 1:
        name, class_name, business_object = shape
        return Existence(
            node_id=node.node_id,
            state=LookupOutcome.FOUND,
            dest_wid=candidates[0],
            dest_reference_id=match_index.reference_id_of.get(candidates[0]),
            matched_by=(
                f"name + class + business object ({name!r}, {class_name}, "
                f"{business_object}) — its Calculated_Field_ID differs between "
                "the tenants"
            ),
        )

    if len(candidates) > 1:
        if source_alias:
            by_alias = [
                wid for wid in candidates
                if match_index.alias_of.get(wid) == source_alias
            ]
            if len(by_alias) == 1:
                return Existence(
                    node_id=node.node_id,
                    state=LookupOutcome.FOUND,
                    dest_wid=by_alias[0],
                    dest_reference_id=match_index.reference_id_of.get(by_alias[0]),
                    matched_by=(
                        f"name + class + business object, tie-broken by WQL "
                        f"alias {source_alias!r} against {len(candidates)} "
                        "same-shape candidates"
                    ),
                )
        return Existence(
            node_id=node.node_id,
            state=LookupOutcome.UNKNOWN,
            fault=(
                f"{len(candidates)} destination calculated fields share this "
                f"name, class and business object ({shape[0]!r})"
                + (
                    f", and none uniquely carries the source's WQL alias "
                    f"{source_alias!r}"
                    if source_alias
                    else ", and the source has no WQL alias to break the tie"
                )
                + ". Choosing one would wire dependents to arbitrary data."
            ),
        )

    # No shape match at all. The destination may still hold this field under a
    # different name, in which case its alias is the only thread left.
    if source_alias:
        alias_candidates = match_index.by_alias.get(source_alias) or []
        if len(alias_candidates) == 1:
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.FOUND,
                dest_wid=alias_candidates[0],
                dest_reference_id=match_index.reference_id_of.get(alias_candidates[0]),
                matched_by=(
                    f"WQL alias {source_alias!r} only — the destination's field "
                    "has a different name, so nothing stronger matched"
                ),
            )
        if len(alias_candidates) > 1:
            # Several fields share the alias. The business object narrows them:
            # it is a Workday-delivered object, so its WID is identical in both
            # tenants, and a field on a different object is a different field
            # whatever it is called. Applied only here, never to relax a
            # single-candidate alias match, which is already unambiguous.
            if shape is not None:
                same_object = [
                    wid for wid in alias_candidates
                    if (match_index.shape_of.get(wid) or (None, None, None))[2]
                    == shape[2]
                ]
                if len(same_object) == 1:
                    return Existence(
                        node_id=node.node_id,
                        state=LookupOutcome.FOUND,
                        dest_wid=same_object[0],
                        dest_reference_id=match_index.reference_id_of.get(same_object[0]),
                        matched_by=(
                            f"WQL alias {source_alias!r} narrowed by business "
                            f"object {shape[2]} — {len(alias_candidates)} "
                            "destination fields share the alias, one shares the "
                            "object"
                        ),
                    )
            return Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault=(
                    f"No name/class/business-object match, and "
                    f"{len(alias_candidates)} destination fields share the WQL "
                    f"alias {source_alias!r} without one uniquely sharing the "
                    "business object. Creating it would be rejected as a "
                    "duplicate alias; choosing one would be a guess."
                ),
            )

    return Existence(node_id=node.node_id, state=LookupOutcome.NOT_FOUND)


def _match_calculated_measure_across_tenants(
    node: Node, measure_match_index: Mapping[tuple[str, str], list[str]]
) -> Existence:
    """Second opinion on a calculated measure the ID probe said was absent.

    ``BI_Calculated_Measure_ID`` cannot be a cross-tenant identity, and unlike
    the calculated-field case that is true *by construction* rather than by
    convention: the destination's IDs are Workday-generated with tenant-local
    sequence numbers (``ARITHMETIC_CALCULATED_MEASURE-11-210``), so no two
    tenants can agree on them.

    Matching is on (name, business object) — see
    :func:`~wdmigrator.discovery.inventory.calculated_measure_shape` for why
    that is thinner than the calculated-field equivalent. Several candidates
    means UNKNOWN, never a guess.

    Confirmed live 2026-08-12: creating a measure whose name is already taken
    fails with "Enter a unique name for the System-Wide Summarization
    Calculation", so an unmatched duplicate does not silently succeed — but it
    does halt a run partway, which is what this avoids.
    """
    shape = calculated_measure_shape(node.payload)
    if shape is None:
        return Existence(
            node_id=node.node_id,
            state=LookupOutcome.NOT_FOUND,
            fault=(
                "No BI_Calculated_Measure_ID match, and the measure is missing "
                "a name or business object, so it cannot be matched on shape."
            ),
        )

    candidates = measure_match_index.get(shape) or []
    if len(candidates) == 1:
        name, business_object = shape
        return Existence(
            node_id=node.node_id,
            state=LookupOutcome.FOUND,
            dest_wid=candidates[0],
            matched_by=(
                f"name + business object ({name!r}, {business_object}) — "
                "BI_Calculated_Measure_ID is tenant-generated and never matches"
            ),
        )
    if len(candidates) > 1:
        return Existence(
            node_id=node.node_id,
            state=LookupOutcome.UNKNOWN,
            fault=(
                f"{len(candidates)} destination calculated measures share this "
                f"name and business object ({shape[0]!r}). Choosing one would "
                "wire dependents to arbitrary data."
            ),
        )
    return Existence(node_id=node.node_id, state=LookupOutcome.NOT_FOUND)


def iter_check_existence(
    connection: Connection,
    closure: Closure,
    *,
    match_index: CalculatedFieldMatchIndex | None = None,
    measure_match_index: Mapping[tuple[str, str], list[str]] | None = None,
    tt_connection: Connection | None = None,
) -> Iterator[ProbeProgress]:
    """Probe every node against the destination, yielding progress per object.

    A generator so the UI can show a live count and cancel partway through a
    rate-limited sweep.

    ``match_index`` enables the cross-tenant calculated-field matching
    described on :func:`probe_node`. It costs a destination index sweep to
    build, so it is opt-in rather than automatic — but without it, two tenants
    with different ``Calculated_Field_ID`` conventions will duplicate every
    field they already share.

    ``tt_connection`` is required whenever the closure contains any node whose
    kind is in :data:`TIME_TRACKING_KINDS`. Without one, those nodes probe as
    UNKNOWN with an explanatory fault — the run does not silently skip them.
    """
    ordered = topological_sort(closure.nodes)
    total = len(ordered)

    for position, node in enumerate(ordered, start=1):
        if node.kind in TIME_TRACKING_KINDS:
            if tt_connection is None:
                existence = Existence(
                    node_id=node.node_id,
                    state=LookupOutcome.UNKNOWN,
                    fault=(
                        f"{node.kind.value} lives on "
                        "Time_Tracking_Implementation_Service, but no "
                        "time-tracking connection was provided. Open one with "
                        "Connection.for_service(TIME_TRACKING_SERVICE_NAME) and "
                        "pass it as `tt_connection`."
                    ),
                )
            else:
                existence = probe_node(
                    tt_connection,
                    node,
                    match_index=match_index,
                    measure_match_index=measure_match_index,
                )
        else:
            existence = probe_node(
                connection,
                node,
                match_index=match_index,
                measure_match_index=measure_match_index,
            )
        yield ProbeProgress(
            checked=position,
            total=total,
            node=node,
            existence=existence,
        )


def build_plan(
    closure: Closure,
    existence: Mapping[str, Existence],
    overrides: Mapping[str, Action] | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
) -> MigrationPlan:
    """Assemble the reviewable plan from a closure and its probe results.

    ``overrides`` are the user's per-object decisions from the conflict table
    and win over the defaults. ``reference_decisions`` are answers to
    "the destination has no such object", carried in so they survive a plan
    rebuild — otherwise re-probing would discard every answer already given.
    """
    overrides = overrides or {}
    ordered = topological_sort(closure.nodes)

    plan = MigrationPlan(
        ordered_nodes=ordered,
        existence=dict(existence),
        unresolved_reference_ids=frozenset(closure.unresolved_reference_ids),
        unresolved_measure_ids=frozenset(closure.unresolved_measure_ids),
        unresolved_report_ids=frozenset(closure.unresolved_report_ids),
        unresolved_prompt_set_ids=frozenset(closure.unresolved_prompt_set_ids),
        unresolved_prompt_field_ids=frozenset(closure.unresolved_prompt_field_ids),
        unresolved_gauge_range_ids=frozenset(closure.unresolved_gauge_range_ids),
        unmigratable_indicator_wids=frozenset(closure.unmigratable_indicator_wids),
        unresolved_dashboard_ids=frozenset(closure.unresolved_dashboard_ids),
        unresolved_time_calculation_tag_ids=frozenset(
            closure.unresolved_time_calculation_tag_ids
        ),
        unresolved_time_calculation_group_ids=frozenset(
            closure.unresolved_time_calculation_group_ids
        ),
        reference_decisions=dict(reference_decisions or {}),
    )

    for node in ordered:
        found = existence.get(node.node_id)
        if found is None:
            plan.existence[node.node_id] = Existence(
                node_id=node.node_id,
                state=LookupOutcome.UNKNOWN,
                fault="Not probed against the destination.",
            )
            plan.actions[node.node_id] = Action.SKIP
            continue

        plan.actions[node.node_id] = overrides.get(
            node.node_id, default_action(found)
        )

        # Seed the WID map for anything already present in the destination, so
        # dependents get rewritten to the destination's WID even when skipped.
        if found.exists and found.dest_wid:
            plan.wid_map[node.source_wid] = found.dest_wid
        # And the business-id map, for the same reason one step over: nested
        # calculated-field references name the field by business id, and a
        # reused field answers to the destination's id, not the source's.
        if found.exists and found.dest_reference_id and node.reference_id:
            if found.dest_reference_id != node.reference_id:
                plan.reference_id_map[node.reference_id] = found.dest_reference_id

    return plan


def validate_plan(plan: MigrationPlan) -> list[Blocker]:
    """Everything wrong with this plan, all at once.

    Returns a full list rather than the first problem, so the user can fix them
    in one pass instead of one refused click at a time.
    """
    blockers: list[Blocker] = []
    by_id = {node.node_id: node for node in plan.ordered_nodes}

    # Dependencies the closure could not account for. Unlike an unmatched WID —
    # usually a delivered object passing through — these were named outright as
    # calculated fields or measures, so a write referencing one will fail.
    for missing, kind, remedy in (
        (
            sorted(plan.unresolved_reference_ids),
            "calculated field",
            "Rebuild the calculated field index, or promote the field to global "
            "in Workday if it is report-scoped.",
        ),
        (
            sorted(plan.unresolved_measure_ids),
            "calculated measure",
            "Confirm the source ISU can call Get_Calculated_Measures. A "
            "report-scoped measure cannot be created by this tool and must be "
            "removed from the report.",
        ),
        (
            sorted(plan.unresolved_report_ids),
            "sub-report",
            "Confirm the source ISU can read the sub-report; a composite cannot "
            "render one the destination does not have.",
        ),
        (
            sorted(plan.unresolved_prompt_set_ids),
            "prompt set",
            "Rebuild the prompt set index. Reading prompt sets requires an "
            "implementer account, so check the source connection is one.",
        ),
        (
            sorted(plan.unresolved_gauge_range_ids),
            "gauge range",
            "Rebuild the gauge range index (Get_Gauge_Ranges, one page).",
        ),
        (
            sorted(plan.unresolved_prompt_field_ids),
            "prompt field",
            "Rebuild the prompt field index. Reading them requires an "
            "implementer account, so check the source connection is one.",
        ),
        (
            sorted(plan.unresolved_dashboard_ids),
            "nested dashboard",
            "Rebuild the dashboard index. Reading dashboards requires an "
            "implementer account, so check the source connection is one.",
        ),
    ):
        if missing:
            blockers.append(
                Blocker(
                    node_id=None,
                    title=f"{len(missing)} {kind}(s) could not be resolved",
                    detail=(
                        "Something being written names these as a "
                        f"{kind}, but they are not available on the source: "
                        + ", ".join(missing[:3])
                        + (f" (+{len(missing) - 3} more)" if len(missing) > 3 else "")
                    ),
                    remedy=remedy,
                )
            )

    for node in plan.ordered_nodes:
        found = plan.existence.get(node.node_id)
        action = plan.action_for(node)

        if found is not None and found.is_unknown and action is not Action.SKIP:
            blockers.append(
                Blocker(
                    node_id=node.node_id,
                    title=f"Cannot determine whether {node.name!r} exists",
                    detail=(
                        "The destination probe failed for a reason other than "
                        f"'not found': {found.fault}"
                    ),
                    remedy=(
                        "Resolve the destination error, then re-run the check. "
                        "Creating this object blind risks a duplicate."
                    ),
                )
            )

        if action is Action.SKIP:
            # Skipping is only safe if the object is already in the destination
            # or nothing being written depends on it.
            missing = found is None or not found.exists
            dependents = [
                dependent
                for dependent in node.required_by
                if dependent in by_id
                and plan.action_for(by_id[dependent]) is not Action.SKIP
            ]
            if missing and dependents:
                names = ", ".join(
                    repr(by_id[d].name) for d in sorted(dependents)[:3]
                )
                blockers.append(
                    Blocker(
                        node_id=node.node_id,
                        title=f"{node.name!r} is skipped but still needed",
                        detail=(
                            f"It is not in the destination, yet {len(dependents)} "
                            f"object(s) being written depend on it ({names}). "
                            "They would be created referencing something that "
                            "does not exist."
                        ),
                        remedy=(
                            f"Set {node.name!r} to CREATE, or remove the objects "
                            "that depend on it from this migration."
                        ),
                    )
                )

    if plan.writes_planned == 0:
        blockers.append(
            Blocker(
                node_id=None,
                title="Nothing to write",
                detail="Every object is set to SKIP.",
                remedy="Set at least one object to CREATE or UPDATE.",
            )
        )

    return blockers
