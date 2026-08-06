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
from wdmigrator.discovery.inventory import ids_of
from wdmigrator.migrate.ordering import substitute_wids
from wdmigrator.migrate.planner import (
    Action,
    MigrationPlan,
    ReferenceAction,
    ReferenceDecision,
)
from wdmigrator.migrate.resolver import Node, NodeKind
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


#: The fault Workday raises for a reference the destination cannot resolve. It
#: names the offending value and its ID type, which is the whole basis of the
#: guided-resolution loop: the user is asked about exactly the reference that
#: broke, rather than triaging the 89 un-migrated references a real report
#: carries, almost all of which are delivered objects that pass through fine.
_INVALID_ID_PATTERN = re.compile(
    r"Invalid ID value\.\s*'(?P<value>[^']+)' is not a valid ID value "
    r"for type = '(?P<id_type>[^']+)'"
)


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


def _strip_sharing_and_placement(data: dict) -> None:
    """Migrate every report unshared and unplaced, in place.

    Two reasons, and the second is the one that actually forces it.

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

    Worklet placement is cleared for the same reason sharing is: it is about
    where the report is exposed, not what it computes, and it points at
    destination objects (landing pages, icons) that a config migration has no
    business inventing. ``Shared`` and ``Enable_As_Worklet`` are set to False
    explicitly rather than removed, so the destination cannot inherit a default
    that is more permissive than intended.
    """
    data["Shared"] = False
    data["Enable_As_Worklet"] = False
    for key in _SHARING_FIELDS + _PLACEMENT_FIELDS:
        data.pop(key, None)


def build_report_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    owner_reference: dict | None = None,
    dest_wid: str | None = None,
    reference_decisions: Mapping[str, ReferenceDecision] | None = None,
) -> dict:
    """Arguments for ``Put_Tenanted_Report_Definition``.

    The owner is remapped because a source ``System_User`` reference is
    meaningless in the destination. If no owner is supplied the source's is
    stripped rather than passed through, so the destination assigns its own
    default instead of the write failing on an unresolvable user. Filter
    condition instance references are stripped for the same reason — see
    :func:`_strip_filter_instance_references` — and sharing and worklet
    placement are cleared by :func:`_strip_sharing_and_placement`.
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
    _strip_sharing_and_placement(remapped)
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


_OPERATIONS = {
    NodeKind.REPORT: "Put_Tenanted_Report_Definition",
    NodeKind.CALCULATED_FIELD: "Put_Calculated_Field",
    NodeKind.CALCULATED_MEASURE: "Put_Global_Calculated_Measure",
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


def _reference_wid(response: dict, node: Node) -> str | None:
    return ids_of(response.get(_RESPONSE_REFERENCE_KEY[node.kind])).get("WID")


def _is_transport_failure(exc: Exception) -> bool:
    """Whether an exception leaves the destination in an unknown state.

    A SOAP ``Fault`` means the server processed and rejected the request — the
    write did not happen. Anything else (timeout, connection reset, truncated
    read) may have committed server-side and must be treated as indeterminate.
    """
    return not isinstance(exc, Fault)


# ── Execution ────────────────────────────────────────────────────────────────


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
        if node.kind is NodeKind.REPORT:
            payload = build_report_payload(
                node,
                plan.wid_map,
                action=action,
                owner_reference=owner_reference,
                dest_wid=dest_wid,
                reference_decisions=plan.reference_decisions,
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

    # A matrix report cannot carry its summary calculations on the write that
    # creates it — see _defer_summary_calculations. Holding them back turns one
    # node into two SOAP calls, still one record.
    deferred = _defer_summary_calculations(payload) if node.kind is NodeKind.REPORT else None

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
        record.fault = connection.redact(str(exc))
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
