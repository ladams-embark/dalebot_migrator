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
    LookupOutcome,
    lookup_calculated_field,
    lookup_calculated_measure,
    lookup_report_by_name,
)
from wdmigrator.migrate.ordering import topological_sort
from wdmigrator.migrate.resolver import Closure, Node, NodeKind


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

    @property
    def exists(self) -> bool:
        return self.state is LookupOutcome.FOUND

    @property
    def is_unknown(self) -> bool:
        return self.state is LookupOutcome.UNKNOWN


class ReferenceAction(str, Enum):
    """What to do with a reference the destination cannot resolve."""

    BLANK = "blank"
    REPLACE = "replace"


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
    """
    if existence.state is LookupOutcome.NOT_FOUND:
        return Action.CREATE
    return Action.SKIP


def probe_node(connection: Connection, node: Node) -> Existence:
    """Ask the destination whether one object already exists.

    The two kinds are matched differently, and not by choice:

    - **Calculated fields** match on ``Calculated_Field_ID``, a genuine stable
      cross-tenant identifier.
    - **Reports** match on their **name**, because ``Custom_Report_ID`` is
      returned by the API but rejected as a lookup key (see
      :func:`~wdmigrator.discovery.inventory.lookup_report_by_name`). Name is a
      weaker identity, and a duplicated name resolves to UNKNOWN rather than a
      guess.
    """
    if node.kind is NodeKind.CALCULATED_MEASURE:
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

    return Existence(
        node_id=node.node_id,
        state=result.outcome,
        dest_wid=result.wid,
        fault=result.fault,
    )


def iter_check_existence(
    connection: Connection, closure: Closure
) -> Iterator[ProbeProgress]:
    """Probe every node against the destination, yielding progress per object.

    A generator so the UI can show a live count and cancel partway through a
    rate-limited sweep.
    """
    ordered = topological_sort(closure.nodes)
    total = len(ordered)

    for position, node in enumerate(ordered, start=1):
        yield ProbeProgress(
            checked=position,
            total=total,
            node=node,
            existence=probe_node(connection, node),
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
