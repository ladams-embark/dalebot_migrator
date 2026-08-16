"""Writing objects into the destination tenant. The only module that mutates.

Everything here is shaped by one fact: **``Core_Implementation_Service`` has no
delete operation.** Anything written must be removed by hand in the Workday UI,
object by object. There is no rollback, so the design refuses in every
ambiguous case rather than proceeding.

Four behaviours worth understanding before changing anything:

**A HTTP 200 with no SOAP fault does not mean success.**
``Put_Calculated_Field`` returns ``Exceptions_Response_Data`` — an array of
``Exception_Data{Classification, Message}`` — alongside the reference. Treating
"no fault" as success would silently record failures as successes and, worse,
register a destination WID that downstream payloads then reference.
``Put_Tenanted_Report_Definition`` has no such block; the two writers are
genuinely asymmetric and must not be unified into one code path that assumes
either shape.

**A timeout is not a failure.** A PUT that times out or drops the connection
may have committed server-side. Those are recorded ``INDETERMINATE`` and are
never retried automatically: a blind retry is how you get duplicates. They must
be re-probed against the destination before anything else is decided.

**The guard is re-checked before every single object**, not once per run.
Between two writes a session can change, a token can expire, a plan can be
swapped. Re-checking costs nothing and closes all of it.

**Dry run never touches the network.** It builds and serializes the real SOAP
envelope through zeep's binding without sending it, which catches type and
cardinality errors before the live run — the highest-value thing a dry run can
do here.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Mapping

from zeep.exceptions import Fault
from zeep.helpers import serialize_object

from wdmigrator.auth.client import Connection, Role
from wdmigrator.discovery.inventory import DASHBOARD_FLAVOURS, ids_of
from wdmigrator.migrate.ordering import substitute_reference_ids, substitute_wids
from wdmigrator.migrate.planner import (
    Action,
    MigrationPlan,
    ReferenceAction,
    ReferenceDecision,
)
from wdmigrator.migrate.resolver import (
    DASHBOARD_TABBED_BY_KIND,
    WORKLET_BACKREF_FIELDS,
    Node,
    NodeKind,
)
from wdmigrator.safety import WriteGuard, assert_write_allowed
from wdmigrator.secrets import redact_envelope

#: Exception classifications Workday returns that are not actually failures.
#: Empty for now — every classification seen so far indicates a real problem.
#: Kept explicit so a future "Warning" classification has an obvious home.
_BENIGN_CLASSIFICATIONS: frozenset[str] = frozenset()


class WriteStatus(str, Enum):
    """Outcome of attempting one object.

    ``INDETERMINATE`` is not a nicety. A transport failure on a PUT leaves the
    destination in an unknown state, and conflating it with ``FAILED`` invites
    a retry that duplicates a committed object.
    """

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_ATTEMPTED = "not_attempted"
    INDETERMINATE = "indeterminate"


class WriteError(RuntimeError):
    """A write could not be prepared or its result could not be trusted."""


@dataclass(frozen=True)
class BlockingReference:
    """The identifier a failed write named as unresolvable."""

    value: str
    id_type: str


@dataclass(frozen=True)
class ReferenceSite:
    """Where a blocking reference sits in a payload, and what else names it."""

    path: str
    element: str
    ids: dict


@dataclass(frozen=True)
class ExceptionDetail:
    classification: str | None
    message: str | None


@dataclass
class WriteRecord:
    """What happened to one object. The unit of the results report."""

    node_id: str
    kind: str
    name: str | None
    reference_id: str | None
    action: Action
    status: WriteStatus
    dest_wid: str | None = None
    exceptions: list[ExceptionDetail] = field(default_factory=list)
    #: Things that were changed or dropped to make the write succeed. Not
    #: failures — the object landed — but the user is entitled to know the
    #: destination copy is not byte-identical to the source.
    warnings: list[str] = field(default_factory=list)
    fault: str | None = None
    #: Set when the fault named a specific unresolvable identifier, so the UI
    #: can ask about exactly that reference instead of the whole payload.
    blocking_reference: "BlockingReference | None" = None
    envelope: str | None = None
    duration_ms: int = 0
    dry_run: bool = True

    @property
    def ok(self) -> bool:
        return self.status is WriteStatus.SUCCESS

    @property
    def needs_reprobe(self) -> bool:
        """Indeterminate objects must be re-checked before any retry."""
        return self.status is WriteStatus.INDETERMINATE


@dataclass
class WriteProgress:
    """Emitted per object so the UI can show progress and cancel between writes."""

    position: int
    total: int
    node: Node
    record: WriteRecord

    @property
    def fraction(self) -> float:
        return min(1.0, self.position / self.total) if self.total else 0.0


# ── Payload construction ─────────────────────────────────────────────────────


def build_owner_reference(
    *, workday_username: str | None = None, wid: str | None = None
) -> dict:
    """A ``System_UserObjectType`` reference for the destination report owner.

    ``WorkdayUserName`` is preferred: it is a plain string the user can type,
    whereas a WID would have to be hunted down in the destination first.
    """
    if bool(workday_username) == bool(wid):
        raise ValueError("Pass exactly one of workday_username or wid.")
    if workday_username:
        return {"ID": [{"type": "WorkdayUserName", "_value_1": workday_username}]}
    return {"ID": [{"type": "WID", "_value_1": wid}]}


def build_calculated_field_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    dest_wid: str | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
) -> dict:
    """Arguments for ``Put_Calculated_Field``.

    On CREATE the reference is omitted entirely — including it with a source
    WID would either fail or, worse, address an unrelated destination object.
    """
    data = node.payload.get("Calculated_Field_Data")
    if not data:
        raise WriteError(f"{node.name!r} has no Calculated_Field_Data to write.")

    remapped = substitute_wids(data, wid_map)
    if reference_decisions:
        _apply_reference_decisions(remapped, reference_decisions)
    payload: dict = {"Calculated_Field_Data": remapped}

    if action is Action.UPDATE:
        if not dest_wid:
            raise WriteError(
                f"Cannot UPDATE {node.name!r} without the destination's WID. "
                "A source WID does not address anything in the destination."
            )
        payload["Calculated_Field_Reference"] = {
            "ID": [{"type": "WID", "_value_1": dest_wid}]
        }

    return payload


def _strip_filter_instance_references(obj: object) -> None:
    """Remove ``Filter_Instances_Reference`` from every filter condition, in place.

    A filter condition's fixed comparison value is a reference to a specific
    business object instance in the *source* tenant — a particular Cost
    Center, Location, Worker, whatever the filtered field happens to be on.
    That is tenant-specific data, not something this tool creates or can
    verify ahead of time (no generic "does this WID exist" operation exists,
    same underlying limit as the ``External_Field`` case).

    Confirmed live 2026-08-03 on "Luke's Fancy Report": Workday rejects an
    unresolvable ``Filter_Instances_Reference`` outright
    (``Invalid ID value ... is not a valid ID value for type = 'WID'``), and
    ``Ignore_When_No_Target_Value`` — despite its name — does not suppress
    that validation; the same fault occurred with it set ``True``. Stripping
    the reference is safe in the direction that matters: worst case the
    migrated report's filter has no default value and someone sets one in
    Workday afterward, versus the whole report being blocked outright.
    ``Ignore_When_No_Target_Value`` is stripped alongside it since it has
    nothing left to apply to.
    """
    if isinstance(obj, dict):
        if "Filter_Instances_Reference" in obj:
            obj.pop("Filter_Instances_Reference", None)
            obj.pop("Ignore_When_No_Target_Value", None)
        for value in obj.values():
            _strip_filter_instance_references(value)
    elif isinstance(obj, list):
        for item in obj:
            _strip_filter_instance_references(item)


#: Who a migrated report is visible to, and where it appears. All optional in
#: the schema, all cleared on write — see :func:`_strip_sharing_and_placement`.
_SHARING_FIELDS = (
    "Restricted_to_Metadata_Security_Groups_Reference",
    "Restricted_to_Tenanted_Security_Groups_Reference",
    "Restricted_to_System_User_Reference",
)
#: Every field on ``Tenanted_Report_Definition_DataType`` that only means
#: something on a worklet. Enumerated from the WSDL rather than discovered one
#: failure at a time — several of these are not merely ignorable when
#: ``Enable_As_Worklet`` is False, they are conditionally *invalid*, and Workday
#: rejects the entire write:
#:
#:   "A Refresh Data Reference can only be specified for a Custom Report
#:    Definition that is enabled as a worklet"
#:   "Worklet Max Rows can only be specified for a Custom Report Definition
#:    that is enabled as a Worklet"
#:
#: Both confirmed live. Clearing the flag without clearing all of these is a
#: self-inflicted failure, so keep this list in step with the schema.
_PLACEMENT_FIELDS = (
    "Worklet_Landing_Page_Reference",
    "Worklet_Max_Rows",
    "Worklet_Help_Text",
    "Worklet_Icon_Reference",
    "Refresh_Data_Reference",
    "Maximized_Custom_Report_Definition_Reference",
    "Maximized_Worklet_Report_Definition_Reference",
    "Tenanted_Report_Worklet_Layout_Data",
)
#: Dropped even when a report is kept as a worklet for a dashboard. These point
#: from the report *back* at the landing page that shows it — the opposite of
#: the direction the dashboard's own worklet data expresses. Keeping them means
#: an unresolvable reference in the destination and a cycle in the closure.
#:
#: Defined in `resolver` and imported here rather than duplicated: the resolver
#: has to exclude exactly what this strips, or it invents a dependency edge over
#: bytes that never get written.
_WORKLET_BACKREF_FIELDS = WORKLET_BACKREF_FIELDS

#: References to objects this tool does not migrate and cannot create, which
#: therefore block a write on a destination that has never seen them.
#:
#: **Report tags.** Confirmed live 2026-08-07, first real migration into
#: `commitconsulting`: report 18 of 25 failed with ``Invalid ID value.
#: 'd07f2203d8fc1001b64bf07d1d130000' is not a valid ID value for type = 'WID'``,
#: and that WID is a ``Report_Tag_Reference`` carrying
#: ``Custom_Report_Tag_ID = 'Commit - Reporting Optimization Report-NDc3...'``.
#: Five of the ten reports in that closure share the same tag, so it would have
#: blocked five times over.
#:
#: A tag is an organisational label — it groups reports for discoverability and
#: nothing depends on it, so the reports land untagged and someone re-tags them
#: by hand if they care. That is the same trade already made for sharing and for
#: ``Filter_Instances_Reference``. The field is ``minOccurs=0``, so removing it
#: is valid.
#:
#: Note this is a *choice*, not a limitation: ``Get_Report_Tags`` and
#: ``Put_Report_Tag`` both exist on this service, so tags could be migrated as
#: their own dependency kind. That is more scope than "make dashboards work",
#: and it is a clean follow-up if tags turn out to matter.
_UNMIGRATABLE_REPORT_REFERENCES = ("Report_Tag_Reference",)


#: References to objects created *inside* another object's write, whose
#: destination WID is never reported back, but which carry a business ID that is
#: stable across tenants.
#:
#: ``{element: id_type}``. For these, a WID with no entry in ``wid_map`` is not
#: a delivered object passing through — it is a dead source WID, and the write
#: fails on it. The business ID beside it addresses the same object correctly,
#: so the WID is dropped and the business ID left to resolve.
#:
#: **Matrix measures.** Confirmed live 2026-08-07, second pass of the first real
#: migration: `Custom Report Exceptions by Owner` failed with ``Invalid ID value.
#: 'd07f2203d8fc1001b86ccee64da00000' is not a valid ID value for type = 'WID'``.
#: That WID is a ``Matrix_Measure__All__Reference`` naming
#: ``MATRIX_MEASURE-6-4022``, a measure defined inline on the sub-report through
#: ``Matrix_Measures_Data``. Read-back proved the measure was already in the
#: destination — written moments earlier as part of the sub-report, **with the
#: same business ID** — so nothing was missing. Only the WID was stale.
#:
#: This cannot be fixed by mapping the WID: ``Matrix_Measure_DataType`` has an
#: ``ID`` string and no reference element, so reading the sub-report back does
#: not reveal the measure's destination WID at all. Dropping the WID is the only
#: route, and it is sound because the business ID is genuinely stable — the same
#: reasoning that makes ``substitute_wids`` leave nested
#: ``Calculated_Field_Reference_ID`` values alone.
#: **Matrix dimensions.** Same failure, same report, one round trip later:
#: ``'d07f2203d8fc1001b86ccd16e57b0001' is not a valid ID value for type =
#: 'WID'``, a ``Matrix_Dimension_Reference`` naming
#: ``MATRIX_DIMENSION-6-50350-1618554797`` inside
#: ``Tenanted_Duplicate_BO_Mapping_Data``. Also inline on the sub-report.
#:
#: Deliberately **not** listed: ``Worklet_Icon_Reference``, which also carries a
#: ``*_Reference_ID`` (``DEFAULT_WORKLET_ICON``). That is a Workday-delivered
#: icon, not an inline child — its WID is almost certainly global, it has never
#: failed, and adding it would be acting on shape rather than evidence. Add
#: entries here when a write actually fails on one, not before.
#: ``Matrix_Display_Option_Reference`` was briefly listed here and **that was
#: wrong**. It is the exact inverse case: an analytic indicator keeps the same
#: WID in both tenants (all 318 of dpt5 appear in dpt1 under identical WIDs)
#: while its business id is tenant-scoped
#: (``CRTMNU01_Commit - HR Dashboard_09_Retention Risk Indicator`` against
#: ``Worker-Retention-Retention Risk Indicator`` for one and the same object).
#: Dropping its WID removed the half that transfers and kept the half that does
#: not, turning a WID rejection into a business-id rejection. See
#: :data:`_TENANT_SCOPED_BUSINESS_IDS`.
#: **Prompt set members.** Same shape again, confirmed live 2026-08-13. A
#: dashboard names an individual member through
#: ``Prompt_Set_Member__All__Reference`` (WID plus ``Prompt_Set_Member_ID``),
#: and the WID is the source's.
#:
#: These cannot be mapped, only dropped. ``Get_Prompt_Sets`` does not expose
#: member WIDs on *either* tenant — a member carries only
#: ``Reference_for_Webservices``, its ordinal — so there is nothing to read back
#: and nothing to build a map from. They are created inline with the prompt set,
#: which is precisely why they have no independent reference, and precisely why
#: they belong here.
_INLINE_CHILD_REFERENCES = {
    "Matrix_Measure__All__Reference": "Matrix_Measure_Reference_ID",
    "Matrix_Dimension_Reference": "Matrix_Dimension_Reference_ID",
    "Prompt_Set_Member__All__Reference": "Prompt_Set_Member_ID",
}


#: References whose WID is stable across tenants but whose business id is not —
#: the mirror image of :data:`_INLINE_CHILD_REFERENCES`. Sending both makes
#: Workday validate the business id and reject it, so the business id is
#: dropped and the WID left to resolve on its own.
_TENANT_SCOPED_BUSINESS_IDS = {
    "Matrix_Display_Option_Reference": "Analytic_Indicator_ID",
    "Analytic_Indicator_Reference": "Analytic_Indicator_ID",
}


def _drop_tenant_scoped_business_ids(obj: object) -> int:
    """Strip tenant-scoped business ids, in place. Returns the count.

    Only touches a reference that still carries a WID — the WID is what will
    resolve, and removing the only identifier would leave an empty reference.
    """
    dropped = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            id_type = _TENANT_SCOPED_BUSINESS_IDS.get(key)
            for reference in value if isinstance(value, list) else [value]:
                if id_type is None or not isinstance(reference, dict):
                    continue
                entries = reference.get("ID")
                if not isinstance(entries, list):
                    continue
                has_wid = any(
                    e.get("type") == "WID" for e in entries if isinstance(e, dict)
                )
                if not has_wid:
                    continue
                kept = [
                    e for e in entries
                    if not (isinstance(e, dict) and e.get("type") == id_type)
                ]
                if len(kept) != len(entries):
                    reference["ID"] = kept
                    dropped += 1
            dropped += _drop_tenant_scoped_business_ids(value)
    elif isinstance(obj, list):
        for item in obj:
            dropped += _drop_tenant_scoped_business_ids(item)
    return dropped


def _drop_stale_inline_wids(obj: object, wid_map: Mapping[str, str]) -> int:
    """Strip unmapped WIDs from inline-child references, in place.

    Only touches a reference that still carries its business ID, and only when
    the WID has no mapping — a mapped WID is correct and must be kept, since the
    destination resolves it directly without a second lookup.
    """
    dropped = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            id_type = _INLINE_CHILD_REFERENCES.get(key)
            for reference in value if isinstance(value, list) else [value]:
                if id_type is None or not isinstance(reference, dict):
                    continue
                entries = reference.get("ID")
                if not isinstance(entries, list):
                    continue
                ids = {
                    e.get("type"): e.get("_value_1")
                    for e in entries
                    if isinstance(e, dict)
                }
                if not ids.get(id_type):
                    continue
                wid = ids.get("WID")
                if wid and wid not in wid_map.values():
                    reference["ID"] = [
                        e for e in entries if e.get("type") != "WID"
                    ]
                    dropped += 1
            dropped += _drop_stale_inline_wids(value, wid_map)
    elif isinstance(obj, list):
        for item in obj:
            dropped += _drop_stale_inline_wids(item, wid_map)
    return dropped


#: The fault Workday raises for a reference the destination cannot resolve. It
#: names the offending value and its ID type, which is the whole basis of the
#: guided-resolution loop: the user is asked about exactly the reference that
#: broke, rather than triaging the 89 un-migrated references a real report
#: carries, almost all of which are delivered objects that pass through fine.
_INVALID_ID_PATTERN = re.compile(
    r"Invalid ID value\.\s*'(?P<value>[^']+)' is not a valid ID value "
    r"for type = '(?P<id_type>[^']+)'"
)


def _names_analytic_indicator(fault: str | None) -> bool:
    """Whether a fault blames an analytic indicator.

    Matches the business id type by name, and the WID case by the element the
    reference lives in — Workday names the value but not where it sat, so a
    bare WID fault is only attributable once the payload is searched. Callers
    do that by attempting the strip and checking it removed something.
    """
    return "analytic_indicator_id" in (fault or "").lower()


def parse_blocking_reference(fault: str | None) -> BlockingReference | None:
    """Pull the offending identifier out of an ``Invalid ID value`` fault.

    Returns None for any other fault — a schema error, an entitlement problem, a
    timeout — because those are not resolvable by substituting a reference and
    should not be presented as though they were.
    """
    if not fault:
        return None
    match = _INVALID_ID_PATTERN.search(fault)
    if match is None:
        return None
    return BlockingReference(
        value=match.group("value"), id_type=match.group("id_type")
    )


def find_reference_sites(node: Node, value: str) -> list[ReferenceSite]:
    """Every place ``value`` appears in a node's payload, with context.

    The fault says *what* broke but not *where*, and "where" is what makes the
    question answerable: a prompt's default value and a filter's comparison
    value need different answers, and the sibling business ids often name the
    object in a way a WID never will.
    """
    sites: list[ReferenceSite] = []

    def walk(obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            entries = obj.get("ID")
            if isinstance(entries, list):
                ids = {
                    e.get("type"): e.get("_value_1")
                    for e in entries
                    if isinstance(e, dict) and e.get("type")
                }
                if value in ids.values():
                    sites.append(
                        ReferenceSite(
                            path=path,
                            element=path.split(".")[-1].split("[")[0],
                            ids=ids,
                        )
                    )
            for key, item in obj.items():
                walk(item, f"{path}.{key}" if path else key)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(item, f"{path}[{i}]")

    walk(node.payload)
    return sites


def _decision_for(reference: object, decisions: Mapping[str, ReferenceDecision]):
    """The decision covering this reference dict, if any."""
    if not isinstance(reference, dict):
        return None
    entries = reference.get("ID")
    if not isinstance(entries, list):
        return None
    return next(
        (
            decisions[e["_value_1"]]
            for e in entries
            if isinstance(e, dict)
            and e.get("type") == "WID"
            and e.get("_value_1") in decisions
        ),
        None,
    )


def _apply_reference_decisions(
    obj: object, decisions: Mapping[str, ReferenceDecision]
) -> int:
    """Blank or replace decided references, in place. Returns the count applied.

    Handles both shapes a reference appears in, which is the whole subtlety
    here. Most are a single dict under a key::

        "Data_Source_Reference": {"ID": [...]}

    but several repeat, and are a *list* of reference dicts::

        "Instance_Reference": [{"ID": [...]}, {"ID": [...]}]

    A matcher written for only the first shape silently does nothing to the
    second — and ``Instance_Reference``, the very case this feature exists for,
    is the second. Blanking a list entry removes just that entry; the key is
    dropped only once nothing is left.
    """
    applied = 0
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            decision = _decision_for(value, decisions)
            if decision is not None:
                if decision.action is ReferenceAction.BLANK:
                    obj.pop(key, None)
                else:
                    value["ID"] = [{
                        "type": decision.replacement_type,
                        "_value_1": decision.replacement_value,
                    }]
                applied += 1
                continue

            if isinstance(value, list) and any(
                _decision_for(item, decisions) is not None for item in value
            ):
                kept = []
                for item in value:
                    hit = _decision_for(item, decisions)
                    if hit is None:
                        kept.append(item)
                        continue
                    applied += 1
                    if hit.action is ReferenceAction.REPLACE:
                        item["ID"] = [{
                            "type": hit.replacement_type,
                            "_value_1": hit.replacement_value,
                        }]
                        kept.append(item)
                if kept:
                    obj[key] = kept
                else:
                    obj.pop(key, None)
                continue

            applied += _apply_reference_decisions(value, decisions)
    elif isinstance(obj, list):
        for item in obj:
            applied += _apply_reference_decisions(item, decisions)
    return applied


def _strip_self_references(obj: object, source_wid: str) -> int:
    """Remove references to the object's own source WID, in place.

    A report can point at itself. Confirmed live on `commitconsulting`: the
    "Monthly Annualized Turnover Calendar YTD Sub-Report" carries five
    ``Matrix_Measures_Data[n].Matrix_Drilldown_Override_Data.Report_Definition_Reference``
    entries naming the report itself — clicking a measure drills back into the
    same report.

    On a CREATE that is unresolvable by construction: the destination object
    does not exist yet, so there is no WID to remap to and the source WID means
    nothing there. Workday rejects the whole write with ``Invalid ID value``.

    Keeping the accompanying ``Custom_Report_ID`` instead is not an option —
    it is returned by the API but rejected as a lookup key (verified on 18/18
    sampled reports; see CLAUDE.md). Dropping the reference is the only move
    that lets the report exist at all. The cost is the drill-down override
    reverting to its default, which is recoverable by hand; the alternative is
    no report.

    Returns the number of references removed, so the caller can report it.
    """
    removed = 0
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if isinstance(value, dict) and any(
                isinstance(entry, dict)
                and entry.get("type") == "WID"
                and entry.get("_value_1") == source_wid
                for entry in (value.get("ID") or [])
            ):
                obj.pop(key, None)
                removed += 1
                continue
            removed += _strip_self_references(value, source_wid)
    elif isinstance(obj, list):
        for item in obj:
            removed += _strip_self_references(item, source_wid)
    return removed


#: ID types on ``Worklet_Landing_Page_Reference`` that name a *custom* dashboard
#: and are stable across tenants. The delivered ones (``Landing_Page_ID``,
#: ``Landing_Page_Group_ID``) are left alone — they resolve on their own.
_LANDING_PAGE_STABLE_IDS = ("Custom_Landing_Page_Group_ID", "Custom_Landing_Page_ID")


def _rewrite_worklet_landing_pages(data: dict, wid_map: Mapping[str, str]) -> None:
    """Keep a worklet's dashboard association **only once the dashboard exists**.

    **This field is not merely a back-pointer, which is what it was first taken
    for.** It is the declaration that the report may be used as a worklet *on
    that dashboard*, and without it the dashboard write fails outright:

        The worklet "Commit - Last Run Date Greater than 18 Months or Never Run"
        is not valid for the assigned dashboard.

    Confirmed live 2026-08-07. Read-back showed the destination reports were
    otherwise correct — ``Enable_As_Worklet=True``, ``Worklet_Max_Rows=100`` —
    and differed from the source only in having no landing page reference.

    **The two objects are mutually dependent, and Workday validates both ends at
    write time.** Referencing the dashboard by its stable business ID before it
    exists does not work either — also confirmed live:

        'Commit - Optimize Reporting Dashboard' is not a valid ID value for
        type = 'Custom_Landing_Page_Group_ID'

    So the association can only be written after the dashboard is created, which
    means a report that a dashboard shows has to be written twice. This function
    handles the second pass and is deliberately conservative about the first:
    the reference survives only when ``wid_map`` already maps the source
    dashboard's WID, i.e. the dashboard has been created in this run. Otherwise
    it is dropped, exactly as before, and the report writes cleanly.

    Note this does *not* make the resolver treat the field as a dependency edge —
    see :data:`~wdmigrator.migrate.resolver.WORKLET_BACKREF_FIELDS`. The
    dashboard still depends on the report; the second write is a follow-up, not
    a reordering.
    """
    for key in _WORKLET_BACKREF_FIELDS:
        value = data.get(key)
        if value is None:
            continue
        references = value if isinstance(value, list) else [value]
        kept = []
        for reference in references:
            entries = reference.get("ID") if isinstance(reference, dict) else None
            if not isinstance(entries, list):
                continue
            wid = next(
                (e.get("_value_1") for e in entries if e.get("type") == "WID"), None
            )
            # Tested against the map's *values*: ``substitute_wids`` has already
            # run over this payload, so a dashboard created in this run appears
            # here as its destination WID, not its source one. A WID that is not
            # a mapped destination is still the source's, and writing it fails.
            if wid and wid in set(wid_map.values()):
                kept.append({"ID": [{"type": "WID", "_value_1": wid}]})
        if kept:
            data[key] = kept
        else:
            data.pop(key, None)


def _strip_sharing_and_placement(
    data: dict, *, keep_worklet: bool = False, wid_map: Mapping[str, str] = {}
) -> None:
    """Migrate a report unshared, and normally unplaced, in place.

    Two reasons for clearing sharing, and the second is the one that forces it.

    **Sharing does not survive a tenant hop meaningfully.** The security groups
    a report is restricted to are tenant-specific: the source's
    ``HR_Administrator`` is not the destination's, even when both exist. Copying
    the restriction would either fail or — worse — silently grant a different
    population than the source intended. Landing the report visible only to its
    owner is the safe default; whoever adopts it decides who else sees it.

    **These references are a large share of what makes a report unmigratable.**
    Confirmed live on `commitconsulting` -> `web`: the sub-report carried three
    ``Restricted_to_Tenanted_Security_Groups_Reference`` entries
    (``HR_Administrator``, ``HR_Auditor``, ``HR_Executive``) and a
    ``Worklet_Landing_Page_Reference`` to a custom landing page, none of which
    exist in the destination. Each is an ``Invalid ID value`` fault.

    ``keep_worklet`` is the dashboard case, and it exists because the default is
    actively wrong there. **A report reaches a dashboard only as a worklet** —
    the dashboard names it through ``Worklet__All__Reference`` — so clearing
    ``Enable_As_Worklet`` on a report a dashboard depends on would migrate the
    dashboard with a hole where that worklet should be. A worklet report is also
    written ``Shared=True``, which Workday requires; see below. Either way the
    ``Restricted_to_*`` security groups are stripped, which is the part that is
    tenant-specific.

    Note the asymmetry in what ``keep_worklet`` preserves. It keeps the flag and
    the presentation fields, but ``Worklet_Landing_Page_Reference`` is dropped
    regardless: it points back at the dashboard from the report, and the
    dashboard's own worklet data is the authoritative direction. Keeping it
    would be both an unresolvable source reference and a dependency cycle.

    ``Shared`` and ``Enable_As_Worklet`` are set explicitly rather than removed,
    so the destination cannot inherit a default more permissive than intended.
    """
    # The security-group restrictions go in every case. They are the part that
    # is tenant-specific and the part that actually failed live.
    for key in _SHARING_FIELDS:
        data.pop(key, None)

    if keep_worklet:
        # **A dashboard worklet must be a shared report.** Confirmed live
        # 2026-08-07 by elimination: with Shared=False every worklet was
        # rejected with "The worklet ... is not valid for the assigned
        # dashboard", including when written as the dashboard's only worklet;
        # re-writing the same report with Shared=True and retrying the same
        # dashboard payload succeeded immediately.
        #
        # This is narrower than it sounds, and does not undo the intent behind
        # landing reports unshared. ``Shared`` is a separate flag from the
        # ``Restricted_to_*`` references stripped above — those name specific
        # source-tenant security groups and are what actually made reports
        # unmigratable. A worklet report lands shared but with **no** inherited
        # restrictions, so who can see it is decided by the destination's own
        # defaults, not by the source's security model.
        data["Shared"] = True
        data["Enable_As_Worklet"] = True
        _rewrite_worklet_landing_pages(data, wid_map)
        return

    data["Shared"] = False

    data["Enable_As_Worklet"] = False
    for key in _PLACEMENT_FIELDS:
        data.pop(key, None)


def build_report_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    owner_reference: dict | None = None,
    dest_wid: str | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
    keep_worklet: bool = False,
) -> dict:
    """Arguments for ``Put_Tenanted_Report_Definition``.

    The owner is remapped because a source ``System_User`` reference is
    meaningless in the destination. If no owner is supplied the source's is
    stripped rather than passed through, so the destination assigns its own
    default instead of the write failing on an unresolvable user. Filter
    condition instance references are stripped for the same reason — see
    :func:`_strip_filter_instance_references` — and sharing and worklet
    placement are cleared by :func:`_strip_sharing_and_placement`. Report tags
    go too, on every report: see :data:`_UNMIGRATABLE_REPORT_REFERENCES`.
    """
    data = node.payload.get("Tenanted_Report_Definition_Data")
    if not data:
        raise WriteError(
            f"{node.name!r} has no Tenanted_Report_Definition_Data to write."
        )

    remapped = substitute_wids(data, wid_map)
    if reference_decisions:
        _apply_reference_decisions(remapped, reference_decisions)
    _strip_filter_instance_references(remapped)
    _strip_sharing_and_placement(remapped, keep_worklet=keep_worklet, wid_map=wid_map)
    # Unconditional, on every report — not just dashboard ones. A tag is
    # tenant-specific by construction, so it fails on any destination that has
    # not been tagged identically by hand first.
    for key in _UNMIGRATABLE_REPORT_REFERENCES:
        remapped.pop(key, None)
    # Runs AFTER substitute_wids, so "unmapped" means what it says.
    _drop_stale_inline_wids(remapped, wid_map)
    # The mirror case: keep the WID, drop the tenant-scoped business id.
    _drop_tenant_scoped_business_ids(remapped)
    if action is Action.CREATE:
        # Only on CREATE: an UPDATE addresses an object that already exists, so
        # a self-reference there is resolvable and must be left alone.
        _strip_self_references(remapped, node.source_wid)

    if owner_reference is not None:
        remapped["Tenanted_Report_Definition_System_User_Reference"] = owner_reference
    else:
        remapped.pop("Tenanted_Report_Definition_System_User_Reference", None)

    payload: dict = {"Tenanted_Report_Definition_Data": remapped}

    if action is Action.UPDATE:
        if not dest_wid:
            raise WriteError(
                f"Cannot UPDATE report {node.name!r} without the destination's WID."
            )
        payload["Tenanted_Report_Definition_Reference"] = {
            "ID": [{"type": "WID", "_value_1": dest_wid}]
        }

    return payload


#: Tenant-specific references stripped from every dashboard, in place.
#:
#: ``Security_Group_Reference`` is a ``Tenanted_Security_Group`` — the source's
#: ``Report_Administrator`` is not the destination's. This is the same call
#: already made for report sharing, but at a very different scale: measured live
#: on `commitconsulting_dpt1`, **23,707** of these across the tabbed dashboards,
#: on essentially every worklet configuration.
#:
#: ``Workday-Delivered_Security_Group_Reference`` was initially **kept**, on the
#: reasoning that a delivered ``Workday_Security_Group_ID`` resolves in any
#: tenant — the class-1/class-2 WID split the rest of this tool is built on.
#: **That reasoning is wrong, disproved live 2026-08-07.** The dashboard write
#: failed with:
#:
#:     Worklet "Custom Report Exceptions by Owner" references one or more
#:     invalid metadata security groups.
#:
#: The only such group on the dashboard is ``implementers_wkdyGroup``, and no
#: dashboard in the destination tenant references any metadata security group at
#: all. "Workday-delivered" evidently does not imply "referenceable from a
#: dashboard in every tenant" — so both kinds are stripped, and a migrated
#: dashboard lands with no per-worklet visibility configuration.
#:
#: The practical consequence: whoever adopts the dashboard sets worklet
#: visibility in the Workday UI. That is the same trade already made for report
#: sharing, and the alternative is a dashboard that cannot be written at all.
_DASHBOARD_TENANT_DATA_FIELDS = (
    "Security_Group_Reference",
    "Workday-Delivered_Security_Group_Reference",
)

#: Announcements are content, not configuration, and every reference in one
#: points at tenant data that a config migration cannot create: uploaded images
#: (``File_ID``/``Image_ID``), media, quicklinks, and the Worker an announcement
#: is "from". Confirmed present live on the test dashboard — two announcements,
#: each with an ``ANNOUNCEMENT_IMAGE-*`` reference that exists in one tenant only.
_DASHBOARD_CONTENT_FIELDS = ("Announcements_Data",)


def _strip_dashboard_tenant_data(obj: object) -> int:
    """Remove tenant-specific references from a dashboard payload, in place."""
    removed = 0
    if isinstance(obj, dict):
        for key in _DASHBOARD_TENANT_DATA_FIELDS:
            if obj.pop(key, None):
                removed += 1
        for value in obj.values():
            removed += _strip_dashboard_tenant_data(value)
    elif isinstance(obj, list):
        for item in obj:
            removed += _strip_dashboard_tenant_data(item)
    return removed


def build_dashboard_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    dest_wid: str | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
) -> dict:
    """Arguments for ``Put_Custom_Dashboard_with_Tabs`` / ``_without_Tabs``.

    Which of the two is decided by ``node.kind``; the flavours have separate
    data blocks, reference keys and ID spaces, and are never interchangeable.

    Three things are stripped, all for the same underlying reason — they name
    tenant *data* rather than configuration, and no amount of dependency
    resolution conjures it in the destination:

    - tenanted security groups on every worklet (see
      :data:`_DASHBOARD_TENANT_DATA_FIELDS`),
    - announcements and their uploaded images (see
      :data:`_DASHBOARD_CONTENT_FIELDS`),
    - the dashboard's own self-reference on CREATE.

    Delivered security groups are kept, since their business IDs resolve
    anywhere. Anything left that the destination cannot resolve surfaces through
    the normal ``Invalid ID value`` path into the reference-decision table.
    """
    tabbed = DASHBOARD_TABBED_BY_KIND[node.kind]
    spec = DASHBOARD_FLAVOURS[tabbed]

    data = node.payload.get(spec["data"])
    if not data:
        raise WriteError(f"{node.name!r} has no {spec['data']} to write.")

    remapped = substitute_wids(data, wid_map)
    if reference_decisions:
        _apply_reference_decisions(remapped, reference_decisions)
    # Prompt set members reach a dashboard as inline children with a source WID
    # that can never be mapped — see _INLINE_CHILD_REFERENCES. Runs after
    # substitute_wids so "unmapped" means what it says.
    _drop_stale_inline_wids(remapped, wid_map)
    _strip_dashboard_tenant_data(remapped)
    for key in _DASHBOARD_CONTENT_FIELDS:
        remapped.pop(key, None)
    if action is Action.CREATE:
        _strip_self_references(remapped, node.source_wid)

    payload: dict = {spec["data"]: remapped}

    if action is Action.UPDATE:
        if not dest_wid:
            raise WriteError(
                f"Cannot UPDATE dashboard {node.name!r} without the destination's WID."
            )
        payload[spec["reference"]] = {"ID": [{"type": "WID", "_value_1": dest_wid}]}
    else:
        # Create-only guard, unique to these two operations among every Put this
        # tool calls. Belt and braces on top of the planner's CREATE/SKIP
        # decision: if the probe was wrong and the dashboard does exist, this
        # makes the server refuse rather than overwrite something that cannot be
        # restored.
        payload["Add_Only"] = True

    return payload


#: Every element that names an analytic indicator, and can therefore be dropped
#: when that indicator exists on neither tenant. Both are ``minOccurs="0"``,
#: which is what makes dropping them legitimate rather than a corruption.
#:
#: Two of them because the same object is reached from two places: a matrix
#: measure names one as its display option, and a report *column* names one
#: directly. Found separately and the hard way — fixing only the matrix case
#: left `Skills Gaps (as of Today)` failing on the column case one object from
#: the end of the run.
_INDICATOR_ELEMENTS = (
    "Matrix_Display_Option_Reference",
    "Analytic_Indicator_Reference",
)


def _strip_display_options(obj: object, wids: "set[str] | None" = None) -> int:
    """Remove ``Matrix_Display_Option_Reference``, in place. Returns the count.

    With ``wids``, only references naming one of those are removed — the
    pre-emptive case, where resolution already established the indicator is
    readable on neither tenant. With ``None``, every display option goes: the
    retry case, after a write has failed naming one.
    """
    removed = 0
    if isinstance(obj, dict):
        for key in list(obj):
            value = obj[key]
            if key in _INDICATOR_ELEMENTS and isinstance(value, dict):
                if wids is None or any(
                    entry.get("_value_1") in wids
                    for entry in value.get("ID") or []
                    if isinstance(entry, dict)
                ):
                    del obj[key]
                    removed += 1
                    continue
            removed += _strip_display_options(value, wids)
    elif isinstance(obj, list):
        for item in obj:
            removed += _strip_display_options(item, wids)
    return removed


def build_analytic_indicator_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    dest_wid: str | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
) -> dict:
    """Arguments for ``Put_Analytic_Indicator``."""
    data = node.payload.get("Analytic_Indicator_Data")
    if not data:
        raise WriteError(f"{node.name!r} has no Analytic_Indicator_Data to write.")

    remapped = substitute_wids(data, wid_map)
    if reference_decisions:
        _apply_reference_decisions(remapped, reference_decisions)

    payload: dict = {"Analytic_Indicator_Data": remapped}

    if action is Action.UPDATE:
        if not dest_wid:
            raise WriteError(
                f"Cannot UPDATE {node.name!r} without the destination WID."
            )
        payload["Analytic_Indicator_Reference"] = {
            "ID": [{"type": "WID", "_value_1": dest_wid}]
        }

    return payload


def build_gauge_range_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    dest_wid: str | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
) -> dict:
    """Arguments for ``Put_Gauge_Range``.

    ``Gauge_Range_DataType`` carries its own business id in an ``ID`` string, so
    a created range keeps the source ``Custom_Analytic_Range_ID`` and stays
    findable by it afterwards. Its other references — decimal places, rounding
    option, and each zone meaning — are delivered objects with stable WIDs.
    """
    data = node.payload.get("Gauge_Range_Data")
    if not data:
        raise WriteError(f"{node.name!r} has no Gauge_Range_Data to write.")

    remapped = substitute_wids(data, wid_map)
    if reference_decisions:
        _apply_reference_decisions(remapped, reference_decisions)

    payload: dict = {"Gauge_Range_Data": remapped}

    if action is Action.UPDATE:
        if not dest_wid:
            raise WriteError(
                f"Cannot UPDATE {node.name!r} without the destination WID."
            )
        payload["Gauge_Range_Reference"] = {
            "ID": [{"type": "WID", "_value_1": dest_wid}]
        }

    return payload


def build_prompt_field_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    dest_wid: str | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
) -> dict:
    """Arguments for ``Put_Prompt_Field``.

    A prompt field is a *tenanted external parameter* — what a prompt set's
    members point at. ``Prompt_Field_DataType`` is small: ``Name``, a required
    ``Field_Type_Reference``, and optional ``Business_Object_Reference`` and
    ``Currency_Code_Reference``. All three references are delivered objects, so
    they pass through with the same WIDs; ``wid_map`` is applied anyway for
    consistency and costs nothing.

    Only *custom* prompt fields reach here. A delivered parameter (Effective
    Date, Supervisory Organization) is referenced by a bare WID with no
    ``TenantedExternalParameter`` id, is not returned by ``Get_Prompt_Fields``
    on either tenant, and is never resolved into a node — see
    :func:`~wdmigrator.migrate.ordering.extract_prompt_field_refs`.
    """
    data = node.payload.get("Prompt_Field_Data")
    if not data:
        raise WriteError(f"{node.name!r} has no Prompt_Field_Data to write.")

    remapped = substitute_wids(data, wid_map)
    if reference_decisions:
        _apply_reference_decisions(remapped, reference_decisions)

    payload: dict = {"Prompt_Field_Data": remapped}

    if action is Action.UPDATE:
        if not dest_wid:
            raise WriteError(
                f"Cannot UPDATE {node.name!r} without the destination's WID."
            )
        payload["Prompt_Field_Reference"] = {
            "ID": [{"type": "WID", "_value_1": dest_wid}]
        }

    return payload


def build_prompt_set_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    dest_wid: str | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
) -> dict:
    """Arguments for ``Put_Prompt_Set``.

    A prompt set's members carry ``Instance_Reference`` defaults pointing at
    specific tenant instances — Organizations, Ledger Accounts, Workers. Those
    are left in place rather than stripped: unlike a report filter's comparison
    value, a prompt default is often a delivered instance (currencies, ISO
    country codes — 244 of the 306 instance references on this tenant's prompt
    sets are exactly that) which resolves fine. The ones that do not surface
    through the reference-decision table, which is where that judgement belongs.
    """
    data = node.payload.get("Prompt_Set_Data")
    if not data:
        raise WriteError(f"{node.name!r} has no Prompt_Set_Data to write.")

    remapped = substitute_wids(data, wid_map)
    if reference_decisions:
        _apply_reference_decisions(remapped, reference_decisions)
    if action is Action.CREATE:
        _strip_self_references(remapped, node.source_wid)

    payload: dict = {"Prompt_Set_Data": remapped}

    if action is Action.UPDATE:
        if not dest_wid:
            raise WriteError(
                f"Cannot UPDATE prompt set {node.name!r} without the destination's WID."
            )
        payload["Prompt_Set_Reference"] = {"ID": [{"type": "WID", "_value_1": dest_wid}]}

    return payload


_OPERATIONS = {
    NodeKind.REPORT: "Put_Tenanted_Report_Definition",
    NodeKind.CALCULATED_FIELD: "Put_Calculated_Field",
    NodeKind.CALCULATED_MEASURE: "Put_Global_Calculated_Measure",
    NodeKind.PROMPT_SET: "Put_Prompt_Set",
    NodeKind.PROMPT_FIELD: "Put_Prompt_Field",
    NodeKind.GAUGE_RANGE: "Put_Gauge_Range",
    NodeKind.ANALYTIC_INDICATOR: "Put_Analytic_Indicator",
    NodeKind.DASHBOARD: DASHBOARD_FLAVOURS[False]["put"],
    NodeKind.DASHBOARD_TABBED: DASHBOARD_FLAVOURS[True]["put"],
}


def operation_for(node: Node) -> str:
    return _OPERATIONS[node.kind]


def build_calculated_measure_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    dest_wid: str | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
) -> dict:
    """Arguments for ``Put_Global_Calculated_Measure``.

    Same create/update contract as a calculated field: omit the reference to
    create, and refuse an UPDATE without the destination's own WID, since a
    source WID addresses nothing there.

    A measure's payload can reference calculated fields *and* other measures,
    so ``wid_map`` matters here exactly as much as it does for a field — the
    measures this one depends on were written first and their destination WIDs
    are already in the map.
    """
    data = node.payload.get("Calculated_Measure_Data")
    if not data:
        raise WriteError(f"{node.name!r} has no Calculated_Measure_Data to write.")

    remapped = substitute_wids(data, wid_map)
    if reference_decisions:
        _apply_reference_decisions(remapped, reference_decisions)
    payload: dict = {"Calculated_Measure_Data": remapped}

    if action is Action.UPDATE:
        if not dest_wid:
            raise WriteError(
                f"Cannot UPDATE {node.name!r} without the destination's WID. "
                "A source WID does not address anything in the destination."
            )
        payload["Calculated_Measure_Reference"] = {
            "ID": [{"type": "WID", "_value_1": dest_wid}]
        }

    return payload


def serialize_envelope(connection: Connection, operation: str, payload: dict) -> str:
    """Render the SOAP envelope zeep *would* send, without sending it.

    Runs the payload through the real binding, so a type or cardinality error
    surfaces during dry run rather than mid-migration. The credentials are
    stripped before the string is returned — the WS-Security header carries the
    password in cleartext.
    """
    from lxml import etree

    node = connection.client.create_message(
        connection.service, operation, **payload
    )
    return redact_envelope(
        etree.tostring(node, pretty_print=True).decode("utf-8")
    )


# ── Response interpretation ──────────────────────────────────────────────────


def extract_exceptions(response: dict) -> list[ExceptionDetail]:
    """Pull ``Exception_Data`` entries out of a Put response.

    Shape (confirmed from the WSDL)::

        Exceptions_Response_Data[]
          -> Exceptions_Data[]
            -> Exception_Data[] { Classification, Message }
    """
    found: list[ExceptionDetail] = []
    for related in response.get("Exceptions_Response_Data") or []:
        if not isinstance(related, dict):
            continue
        for group in related.get("Exceptions_Data") or []:
            if not isinstance(group, dict):
                continue
            for entry in group.get("Exception_Data") or []:
                if isinstance(entry, dict):
                    found.append(
                        ExceptionDetail(
                            classification=entry.get("Classification"),
                            message=entry.get("Message"),
                        )
                    )
    return found


def is_failure(exceptions: list[ExceptionDetail]) -> bool:
    """Whether returned exceptions should be treated as a failed write.

    Anything not explicitly known to be benign counts as a failure. Defaulting
    the other way would let real errors be recorded as successes.
    """
    return any(
        (e.classification or "") not in _BENIGN_CLASSIFICATIONS for e in exceptions
    )


_RESPONSE_REFERENCE_KEY = {
    NodeKind.REPORT: "Tenanted_Report_Definition_Reference",
    NodeKind.CALCULATED_FIELD: "Calculated_Field_Reference",
    NodeKind.CALCULATED_MEASURE: "Calculated_Measure_Reference",
    NodeKind.PROMPT_SET: "Prompt_Set_Reference",
    NodeKind.PROMPT_FIELD: "Prompt_Field_Reference",
    NodeKind.GAUGE_RANGE: "Gauge_Range_Reference",
    NodeKind.ANALYTIC_INDICATOR: "Analytic_Indicator_Reference",
    NodeKind.DASHBOARD: DASHBOARD_FLAVOURS[False]["reference"],
    NodeKind.DASHBOARD_TABBED: DASHBOARD_FLAVOURS[True]["reference"],
}


def _strip_summary_calculations(obj: object) -> int:
    """Remove every ``Summary_Calculation_Reference``, in place. Returns the count."""
    removed = 0
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if key == "Summary_Calculation_Reference" and value:
                obj.pop(key)
                removed += 1
                continue
            removed += _strip_summary_calculations(value)
    elif isinstance(obj, list):
        for item in obj:
            removed += _strip_summary_calculations(item)
    return removed


def _defer_summary_calculations(payload: dict) -> dict | None:
    """Hold a matrix report's summary calculations back for a second write.

    Workday refuses a ``Summary_Calculation_Reference`` on the write that
    *creates* the report:

        "The entered information does not meet the restrictions defined for
         this field. (Summary_Calculation_Reference)"

    The identical payload succeeds once the report exists. Confirmed live on
    `commitconsulting` -> `web`: creating without the references, then writing
    again with them, attached all four measures. So this is an ordering rule,
    not a validity one — the measures are perfectly valid, the report just has
    to exist first.

    Mutates ``payload`` to drop the references and returns the *original* for
    the follow-up write, or None if there were none to defer (the common case —
    only matrix reports carry them).

    Without this a matrix report migrates looking complete while silently
    losing every measure, which is worse than failing outright.
    """
    data = payload.get("Tenanted_Report_Definition_Data")
    if not isinstance(data, dict):
        return None
    full = copy.deepcopy(payload)
    if _strip_summary_calculations(data) == 0:
        return None
    return full


def _attach_summary_calculations(connection: Connection, full_payload: dict) -> str | None:
    """Second write, carrying the summary calculations. Returns a fault or None.

    Deliberately reference-less, exactly like the first write: a Put with no
    reference upserts on ``Custom_Report_ID`` rather than creating a duplicate
    (verified live — one row before, one row after, same WID).
    """
    try:
        connection.limiter.wait()
        raw = connection.service.Put_Tenanted_Report_Definition(**full_payload)
    except Exception as exc:  # noqa: BLE001 - surfaced on the record
        return (
            "Report was created but its summary calculations could not be "
            f"attached: {connection.redact(str(exc))}"
        )
    response = serialize_object(raw) or {}
    exceptions = extract_exceptions(response)
    if is_failure(exceptions):
        detail = "; ".join(f"{e.classification}: {e.message}" for e in exceptions)
        return f"Report was created but its summary calculations were rejected: {detail}"
    return None


#: Node-id prefixes that identify a dashboard, for the dependent scan below.
_DASHBOARD_NODE_PREFIXES = tuple(f"{kind.value}:" for kind in DASHBOARD_TABBED_BY_KIND)


def is_dashboard_worklet(node: Node) -> bool:
    """Whether a dashboard in this migration shows ``node`` as a worklet.

    Decided from the closure's reverse edges rather than from anything on the
    report itself, because the report does not know: the relationship is
    expressed entirely on the dashboard, through ``Worklet__All__Reference``.

    This is what stops :func:`_strip_sharing_and_placement` clearing
    ``Enable_As_Worklet`` on a report a dashboard needs. A report reached only
    as a sub-report or picked on its own still lands unplaced, as before.

    Non-report nodes are always False. A dashboard depends on plenty of things
    that are not worklets — calculated fields it uses in a runtime prompt, the
    prompt set itself — and "has a dashboard among its dependents" would answer
    True for every one of them. Only a report can be a worklet here.
    """
    if node.kind is not NodeKind.REPORT:
        return False
    return any(
        dependent.startswith(_DASHBOARD_NODE_PREFIXES) for dependent in node.required_by
    )


def _reference_wid(response: dict, node: Node) -> str | None:
    return ids_of(response.get(_RESPONSE_REFERENCE_KEY[node.kind])).get("WID")


def _is_transport_failure(exc: Exception) -> bool:
    """Whether an exception leaves the destination in an unknown state.

    A SOAP ``Fault`` means the server processed and rejected the request — the
    write did not happen. Anything else (timeout, connection reset, truncated
    read) may have committed server-side and must be treated as indeterminate.
    """
    return not isinstance(exc, Fault)


def _strip_dashboard_worklets(data: dict) -> int:
    """Remove every worklet configuration from a dashboard payload, in place."""
    removed = 0
    for tab in data.get("Content_Data") or []:            # tabbed
        tab_data = tab.get("Tab_Data") or {}
        if tab_data.pop("Dashboard_Admin_Configuration", None):
            removed += 1
    if data.pop("Worklets_Data", None):                    # untabbed
        removed += 1
    return removed


def _defer_dashboard_worklets(payload: dict, data_key: str) -> dict | None:
    """Hold a dashboard's worklets back for a second write.

    **The dashboard and its worklet reports are mutually dependent, and Workday
    validates both ends at write time.** Confirmed live 2026-08-07:

        The worklet "Commit - Last Run Date Greater than 18 Months or Never Run"
        is not valid for the assigned dashboard.

    A report is only a valid worklet for a dashboard once it carries a
    ``Worklet_Landing_Page_Reference`` naming that dashboard — and the report
    cannot be written with one before the dashboard exists, not even by the
    dashboard's stable business ID (also confirmed live: ``'Commit - Optimize
    Reporting Dashboard' is not a valid ID value for type =
    'Custom_Landing_Page_Group_ID'``).

    Neither object can go first, so the dashboard is written twice:

    1. a shell with no worklet configurations — valid, since ``Content_Data``
       and ``Dashboard_Admin_Configuration`` are both ``minOccurs=0``. Tabs,
       menus and prompt bindings all survive;
    2. the reports are re-written, now naming the real destination dashboard;
    3. the dashboard is written again, complete.

    Mutates ``payload`` to drop the worklets and returns the *original* for the
    follow-up write, or None if there were none to defer.

    Same shape as :func:`_defer_summary_calculations`, and the same reasoning:
    an ordering constraint, not a validity one.
    """
    data = payload.get(data_key)
    if not isinstance(data, dict):
        return None
    full = copy.deepcopy(payload)
    if _strip_dashboard_worklets(data) == 0:
        return None
    return full


def _worklet_reports_for(node: Node, plan: MigrationPlan) -> list[Node]:
    """The report nodes this dashboard shows as worklets."""
    return [
        candidate
        for candidate in plan.ordered_nodes
        if candidate.kind is NodeKind.REPORT
        and node.node_id in candidate.required_by
    ]


def _attach_dashboard_worklets(
    connection: Connection,
    node: Node,
    plan: MigrationPlan,
    full_payload: dict,
    operation: str,
    *,
    owner_reference: dict | None = None,
) -> str | None:
    """Phases two and three. Returns a fault string, or None on success.

    By the time this runs the dashboard exists and ``plan.wid_map`` maps its
    source WID to the destination's, so rebuilding each worklet report's payload
    now produces a real ``Worklet_Landing_Page_Reference`` — see
    :func:`_rewrite_worklet_landing_pages`, which is a no-op until exactly that
    mapping is present.

    Each worklet report is re-written as an **UPDATE when the destination
    already has it**, carrying the destination reference, and only as a create
    when it genuinely does not.

    This used to be an unconditional reference-less write, on the belief that
    such a Put upserts on ``Custom_Report_ID``. It does not, and cannot:
    ``Custom_Report_ID`` is returned by the API but rejected as a lookup key
    (see :func:`~wdmigrator.discovery.inventory.lookup_report_by_name`), so a
    reference-less Put has nothing to match on and simply creates. Confirmed the
    hard way on 2026-08-13 — dpt5 ended up with two reports named `Span of
    Control`, one sharing dpt1's WID and one freshly allocated, in a service
    with no delete operation.

    An ambiguous destination is refused rather than written. If several reports
    share the name the probe returns UNKNOWN, there is no reference to update
    against, and writing anyway is what produced the duplicate in the first
    place.

    The final dashboard write drops ``Add_Only``, which was set on the shell
    write and would now make the server refuse the very object it just created.
    """
    for report_node in _worklet_reports_for(node, plan):
        found = plan.existence.get(report_node.node_id)
        if found is not None and found.is_unknown:
            return (
                f"Cannot associate worklet {report_node.name!r} with the "
                f"dashboard: the destination could not identify it "
                f"({found.fault}). Writing it anyway would create a duplicate, "
                "and this service has no delete operation."
            )
        # Update in place when it is already there; create only when it is not.
        dest_wid = found.dest_wid if found is not None else None
        action = Action.UPDATE if dest_wid else Action.CREATE
        try:
            payload = build_report_payload(
                report_node,
                plan.wid_map,
                action=action,
                dest_wid=dest_wid,
                owner_reference=owner_reference,
                reference_decisions=plan.reference_decisions,
                keep_worklet=True,
            )
        except WriteError as exc:
            return f"Could not rebuild worklet report {report_node.name!r}: {exc}"

        payload, _ = _apply_plan_rewrites(payload, plan)

        try:
            connection.limiter.wait()
            raw = connection.service.Put_Tenanted_Report_Definition(**payload)
        except Exception as exc:  # noqa: BLE001 - surfaced on the record
            return (
                f"Dashboard shell was created, but associating worklet "
                f"{report_node.name!r} with it failed: "
                f"{connection.redact(str(exc))}"
            )
        exceptions = extract_exceptions(serialize_object(raw) or {})
        if is_failure(exceptions):
            detail = "; ".join(f"{e.classification}: {e.message}" for e in exceptions)
            return (
                f"Worklet {report_node.name!r} was rejected while being "
                f"associated with the dashboard: {detail}"
            )

    # Phase three: the dashboard, complete. Add_Only must go — it guarded the
    # create, and the object now exists.
    final = {k: v for k, v in full_payload.items() if k != "Add_Only"}
    try:
        connection.limiter.wait()
        raw = getattr(connection.service, operation)(**final)
    except Exception as exc:  # noqa: BLE001
        return (
            "Dashboard was created and its worklet reports updated, but writing "
            f"its worklets back failed: {connection.redact(str(exc))}"
        )
    exceptions = extract_exceptions(serialize_object(raw) or {})
    if is_failure(exceptions):
        detail = "; ".join(f"{e.classification}: {e.message}" for e in exceptions)
        return f"Dashboard worklets were rejected: {detail}"
    return None


# ── Execution ────────────────────────────────────────────────────────────────


def _apply_plan_rewrites(payload: dict, plan: "MigrationPlan") -> tuple[dict, list[str]]:
    """Rewrites every built payload needs, whoever built it.

    Two things that are *not* the builder's job, because they depend on what the
    destination turned out to contain rather than on the object being written:

    - Nested calculated-field references name their target by BUSINESS id, and a
      field the destination already had answers to the destination's id, not the
      source's. Confirmed live 2026-08-13: `Skills Gaps (as of Today)` was
      refused over a reused `Learning Points` that dpt5 calls
      `Worker - Learning Points`.
    - Analytic indicators readable on neither tenant, whose optional reference
      is dropped rather than allowed to fail the write.

    Extracted into a helper because it was inlined in :func:`write_node` and the
    dashboard worklet re-write builds its own payloads — so the last object of
    the whole migration failed on a reference the main path had been stripping
    correctly for hours. Anything that calls a ``build_*_payload`` must call
    this too.
    """
    notes: list[str] = []
    if plan.reference_id_map:
        payload = substitute_reference_ids(payload, plan.reference_id_map)
    if plan.unmigratable_indicator_wids:
        dropped = _strip_display_options(payload, plan.unmigratable_indicator_wids)
        if dropped:
            notes.append(
                f"Dropped {dropped} analytic indicator reference(s) naming an "
                "indicator that exists on neither tenant."
            )
    return payload, notes


def write_node(
    connection: Connection,
    node: Node,
    plan: MigrationPlan,
    guard: WriteGuard,
    *,
    owner_reference: dict | None = None,
) -> WriteRecord:
    """Write one object, or describe what would be written in dry run.

    Always returns a record rather than raising, so one bad object does not
    abandon a partially-completed migration with no account of what happened.
    """
    started = time.monotonic()
    action = plan.action_for(node)
    existence = plan.existence.get(node.node_id)
    dest_wid = existence.dest_wid if existence else None

    record = WriteRecord(
        node_id=node.node_id,
        kind=node.kind.value,
        name=node.name,
        reference_id=node.reference_id,
        action=action,
        status=WriteStatus.SKIPPED,
        dry_run=guard.dry_run,
    )

    if action is Action.SKIP:
        record.dest_wid = dest_wid
        return record

    operation = operation_for(node)

    try:
        if node.kind in DASHBOARD_TABBED_BY_KIND:
            payload = build_dashboard_payload(
                node, plan.wid_map, action=action, dest_wid=dest_wid,
                reference_decisions=plan.reference_decisions,
            )
        elif node.kind is NodeKind.ANALYTIC_INDICATOR:
            payload = build_analytic_indicator_payload(
                node, plan.wid_map, action=action, dest_wid=dest_wid,
                reference_decisions=plan.reference_decisions,
            )
        elif node.kind is NodeKind.GAUGE_RANGE:
            payload = build_gauge_range_payload(
                node, plan.wid_map, action=action, dest_wid=dest_wid,
                reference_decisions=plan.reference_decisions,
            )
        elif node.kind is NodeKind.PROMPT_FIELD:
            payload = build_prompt_field_payload(
                node, plan.wid_map, action=action, dest_wid=dest_wid,
                reference_decisions=plan.reference_decisions,
            )
        elif node.kind is NodeKind.PROMPT_SET:
            payload = build_prompt_set_payload(
                node, plan.wid_map, action=action, dest_wid=dest_wid,
                reference_decisions=plan.reference_decisions,
            )
        elif node.kind is NodeKind.REPORT:
            payload = build_report_payload(
                node,
                plan.wid_map,
                action=action,
                owner_reference=owner_reference,
                dest_wid=dest_wid,
                reference_decisions=plan.reference_decisions,
                keep_worklet=is_dashboard_worklet(node),
            )
        elif node.kind is NodeKind.CALCULATED_MEASURE:
            payload = build_calculated_measure_payload(
                node, plan.wid_map, action=action, dest_wid=dest_wid,
                reference_decisions=plan.reference_decisions,
            )
        else:
            payload = build_calculated_field_payload(
                node, plan.wid_map, action=action, dest_wid=dest_wid,
                reference_decisions=plan.reference_decisions,
            )
    except WriteError as exc:
        record.status = WriteStatus.FAILED
        record.fault = str(exc)
        return record

    payload, rewrite_notes = _apply_plan_rewrites(payload, plan)
    record.warnings.extend(rewrite_notes)

    # Two deferrals, same shape: an ordering constraint the schema does not
    # express, handled by writing the object twice. Both turn one node into
    # several SOAP calls while staying a single record.
    deferred = _defer_summary_calculations(payload) if node.kind is NodeKind.REPORT else None
    deferred_worklets = (
        _defer_dashboard_worklets(payload, DASHBOARD_FLAVOURS[
            DASHBOARD_TABBED_BY_KIND[node.kind]]["data"])
        if node.kind in DASHBOARD_TABBED_BY_KIND
        else None
    )

    if guard.dry_run:
        # Serialize through the real binding but never send. This is where
        # schema errors surface cheaply.
        try:
            record.envelope = serialize_envelope(connection, operation, payload)
            record.status = WriteStatus.NOT_ATTEMPTED
        except Exception as exc:  # noqa: BLE001 - a build error is a real finding
            record.status = WriteStatus.FAILED
            record.fault = f"Payload failed schema validation: {connection.redact(str(exc))}"
        record.duration_ms = int((time.monotonic() - started) * 1000)
        return record

    # Live write. Re-check the guard for THIS object, immediately before the
    # call — not once at the start of the run.
    assert_write_allowed(guard)

    if not connection.is_destination():
        raise WriteError(
            f"Refusing to write through a {connection.role.value} connection. "
            "Writes must go through a connection created with Role.DESTINATION."
        )

    try:
        connection.limiter.wait()
        raw = getattr(connection.service, operation)(**payload)
    except Exception as exc:  # noqa: BLE001 - classified below
        fault = connection.redact(str(exc))
        # Fallback, once: a write rejected over an analytic indicator is retried
        # without the display option. Fidelity first — the indicator is kept
        # whenever it resolves — but a marker beside a matrix value is never
        # worth failing a whole migration for, and the alternative is a halted
        # run that a human has to unpick.
        if _names_analytic_indicator(fault) and _strip_display_options(payload):
            record.warnings.append(
                "Write was rejected over an analytic indicator; retried without "
                f"the matrix display option. Original fault: {fault}"
            )
            try:
                connection.limiter.wait()
                raw = getattr(connection.service, operation)(**payload)
            except Exception as retry_exc:  # noqa: BLE001
                record.fault = connection.redact(str(retry_exc))
                record.blocking_reference = parse_blocking_reference(record.fault)
                record.status = (
                    WriteStatus.INDETERMINATE
                    if _is_transport_failure(retry_exc)
                    else WriteStatus.FAILED
                )
                record.duration_ms = int((time.monotonic() - started) * 1000)
                return record
        else:
            record.fault = fault
            record.blocking_reference = parse_blocking_reference(record.fault)
            record.status = (
                WriteStatus.INDETERMINATE
                if _is_transport_failure(exc)
                else WriteStatus.FAILED
            )
            record.duration_ms = int((time.monotonic() - started) * 1000)
            return record

    response = serialize_object(raw) or {}
    record.exceptions = extract_exceptions(response)
    returned_wid = _reference_wid(response, node)

    if is_failure(record.exceptions):
        # Put_Calculated_Field reports failure HERE rather than by raising —
        # the asymmetry described at the top of this module. Parsing the
        # exception messages too is what lets a calculated field reach the same
        # guided resolution a report gets; without it a field with an
        # unresolvable instance reference simply failed, unasked.
        record.blocking_reference = next(
            (
                found
                for found in (
                    parse_blocking_reference(e.message) for e in record.exceptions
                )
                if found is not None
            ),
            None,
        )
        # Deliberately do NOT record the WID: registering it would let
        # downstream payloads reference an object that may not be valid.
        record.status = WriteStatus.FAILED
        record.duration_ms = int((time.monotonic() - started) * 1000)
        return record

    if not returned_wid:
        # No fault, no exceptions, but nothing to reference either. Something
        # happened that we cannot describe, so refuse to call it success.
        record.status = WriteStatus.INDETERMINATE
        record.fault = (
            "Write returned no reference WID. The object may or may not have "
            "been created; re-probe the destination before retrying."
        )
        record.duration_ms = int((time.monotonic() - started) * 1000)
        return record

    # Phase two, for matrix reports only. See _defer_summary_calculations.
    if deferred is not None:
        attach_fault = _attach_summary_calculations(connection, deferred)
        if attach_fault is not None:
            record.status = WriteStatus.FAILED
            record.fault = attach_fault
            # The WID IS recorded here, unlike the exceptions case above: the
            # report demonstrably exists, it is just missing its measures, and
            # whoever cleans up needs to be able to find it.
            record.dest_wid = returned_wid
            record.duration_ms = int((time.monotonic() - started) * 1000)
            return record

    # Phases two and three for a dashboard that has worklets. The WID has to be
    # registered first: rebuilding the worklet reports' payloads is what makes
    # their landing-page reference resolve, and that reads plan.wid_map.
    if deferred_worklets is not None:
        plan.wid_map[node.source_wid] = returned_wid
        worklet_fault = _attach_dashboard_worklets(
            connection, node, plan, deferred_worklets, operation,
            owner_reference=owner_reference,
        )
        if worklet_fault is not None:
            record.status = WriteStatus.FAILED
            record.fault = worklet_fault
            # Recorded, unlike the exceptions case: the dashboard demonstrably
            # exists — as a shell — and whoever cleans up has to be able to find
            # it. This service has no delete operation.
            record.dest_wid = returned_wid
            record.duration_ms = int((time.monotonic() - started) * 1000)
            return record

    record.status = WriteStatus.SUCCESS
    record.dest_wid = returned_wid
    record.duration_ms = int((time.monotonic() - started) * 1000)
    return record


def iter_execute(
    connection: Connection,
    plan: MigrationPlan,
    guard: WriteGuard,
    *,
    owner_reference: dict | None = None,
    stop_on_failure: bool = True,
) -> Iterator[WriteProgress]:
    """Execute the plan in dependency order, yielding one event per object.

    Sequential by necessity, not by preference: each created object's
    destination WID is registered in ``plan.wid_map`` so the next payload can
    reference it. Parallelising would write parents before their children exist.

    ``stop_on_failure`` defaults to True. Continuing past a failed dependency
    would create dependents pointing at something that was never written — a
    worse end state than stopping with a partial, well-understood migration.
    Remaining objects are reported ``NOT_ATTEMPTED`` rather than silently
    dropped.
    """
    if not guard.dry_run and connection.role is not Role.DESTINATION:
        raise WriteError(
            "Live execution requires a Role.DESTINATION connection."
        )

    nodes = plan.ordered_nodes
    total = len(nodes)
    halted = False

    for position, node in enumerate(nodes, start=1):
        if halted:
            yield WriteProgress(
                position=position,
                total=total,
                node=node,
                record=WriteRecord(
                    node_id=node.node_id,
                    kind=node.kind.value,
                    name=node.name,
                    reference_id=node.reference_id,
                    action=plan.action_for(node),
                    status=WriteStatus.NOT_ATTEMPTED,
                    fault="Skipped because an earlier object failed.",
                    dry_run=guard.dry_run,
                ),
            )
            continue

        record = write_node(
            connection, node, plan, guard, owner_reference=owner_reference
        )

        # Register the new WID immediately so the next payload can use it.
        if record.status is WriteStatus.SUCCESS and record.dest_wid:
            plan.wid_map[node.source_wid] = record.dest_wid

        if stop_on_failure and record.status in (
            WriteStatus.FAILED,
            WriteStatus.INDETERMINATE,
        ):
            halted = True

        yield WriteProgress(
            position=position, total=total, node=node, record=record
        )


def summarise(records: list[WriteRecord]) -> dict[str, int]:
    counts = {status.value: 0 for status in WriteStatus}
    for record in records:
        counts[record.status.value] += 1
    return counts
