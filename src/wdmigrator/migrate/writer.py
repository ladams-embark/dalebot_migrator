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

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Mapping

from zeep.exceptions import Fault
from zeep.helpers import serialize_object

from wdmigrator.auth.client import Connection, Role
from wdmigrator.discovery.inventory import ids_of
from wdmigrator.migrate.ordering import substitute_wids
from wdmigrator.migrate.planner import Action, MigrationPlan
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
) -> dict:
    """Arguments for ``Put_Calculated_Field``.

    On CREATE the reference is omitted entirely — including it with a source
    WID would either fail or, worse, address an unrelated destination object.
    """
    data = node.payload.get("Calculated_Field_Data")
    if not data:
        raise WriteError(f"{node.name!r} has no Calculated_Field_Data to write.")

    payload: dict = {"Calculated_Field_Data": substitute_wids(data, wid_map)}

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


def build_report_payload(
    node: Node,
    wid_map: Mapping[str, str],
    *,
    action: Action,
    owner_reference: dict | None = None,
    dest_wid: str | None = None,
) -> dict:
    """Arguments for ``Put_Tenanted_Report_Definition``.

    The owner is remapped because a source ``System_User`` reference is
    meaningless in the destination. If no owner is supplied the source's is
    stripped rather than passed through, so the destination assigns its own
    default instead of the write failing on an unresolvable user. Filter
    condition instance references are stripped for the same reason — see
    :func:`_strip_filter_instance_references`.
    """
    data = node.payload.get("Tenanted_Report_Definition_Data")
    if not data:
        raise WriteError(
            f"{node.name!r} has no Tenanted_Report_Definition_Data to write."
        )

    remapped = substitute_wids(data, wid_map)
    _strip_filter_instance_references(remapped)

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


def operation_for(node: Node) -> str:
    return (
        "Put_Tenanted_Report_Definition"
        if node.kind is NodeKind.REPORT
        else "Put_Calculated_Field"
    )


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


def _reference_wid(response: dict, node: Node) -> str | None:
    key = (
        "Tenanted_Report_Definition_Reference"
        if node.kind is NodeKind.REPORT
        else "Calculated_Field_Reference"
    )
    return ids_of(response.get(key)).get("WID")


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
            )
        else:
            payload = build_calculated_field_payload(
                node, plan.wid_map, action=action, dest_wid=dest_wid
            )
    except WriteError as exc:
        record.status = WriteStatus.FAILED
        record.fault = str(exc)
        return record

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
