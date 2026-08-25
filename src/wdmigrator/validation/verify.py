"""Read-back verification for a completed migration.

Every migration this project has actually trusted was verified by a
hand-written script against the destination, not by trusting the run summary
— and for good reason. HANDOFF records the intermediate run that reported
"0 failed" while two of three dashboards were empty shells: the writer path
had returned SUCCESS, no exception was raised, and only a read-back against
both tenants surfaced the truth. This module formalises that read-back so
the answer is a programmatic verdict rather than a bespoke script per run.

**What is verified is structural, not byte-identical.** Payloads are
heavily rewritten between source and destination:

- WIDs are remapped
- Owners are set to the fixed destination account
- Report tags, tenanted security groups, filter instance values, and shell
  worklet configs are stripped
- Reference IDs may differ after a cross-tenant match

A byte-for-byte comparison would report every one of those as a difference
and drown out the ones that matter. So each kind gets a small set of
structural signals — the ones whose divergence is what an actual failed
migration looks like — and those are what's compared.

**The signals per kind are the same ones that caught the empty-shell
dashboards.** For dashboards, tab count, worklet count, and prompt-set
member reference count; for reports, columns and — where present — filter
condition count; for prompt sets, member count; for calculated fields, the
data block being non-empty. Everything else is left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator

from wdmigrator.auth.client import Connection
from wdmigrator.discovery.inventory import (
    DASHBOARD_FLAVOURS,
    LookupOutcome,
    dashboard_has_worklets,
    ids_of,
    lookup_analytic_indicator,
    lookup_calculated_field,
    lookup_calculated_measure,
    lookup_dashboard,
    lookup_gauge_range,
    lookup_prompt_field,
    lookup_prompt_set,
    lookup_report,
)
from wdmigrator.migrate.planner import Action
from wdmigrator.migrate.resolver import DASHBOARD_TABBED_BY_KIND, Node, NodeKind
from wdmigrator.migrate.writer import WriteRecord, WriteStatus


class VerifyStatus(str, Enum):
    """Verdict for one object's read-back.

    ``MISMATCH`` is a soft failure — the object exists but does not look the
    way it should. ``MISSING`` means the destination has no such object
    despite the write recording SUCCESS. ``ERROR`` means the read-back itself
    failed (auth, timeout, unexpected fault) — treat as unknown, not as
    absent. ``SKIPPED`` is a write that was itself skipped.
    """

    OK = "ok"
    MISMATCH = "mismatch"
    MISSING = "missing"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class VerifyFinding:
    """One structural difference between source and destination."""

    signal: str
    source: object
    destination: object

    def __str__(self) -> str:
        return f"{self.signal}: source={self.source!r}, destination={self.destination!r}"


@dataclass
class VerifyRecord:
    """Result for one migrated object."""

    node_id: str
    kind: str
    name: str | None
    status: VerifyStatus
    #: Populated when ``status`` is MISMATCH. Every entry is a structural
    #: signal that read different on the two tenants — an empty list means the
    #: two agree on every signal the verifier looks at.
    findings: list[VerifyFinding] = field(default_factory=list)
    fault: str | None = None
    dest_wid: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is VerifyStatus.OK


@dataclass
class VerifyProgress:
    """One tick of the sweep. ``record`` is the verdict for the object just
    read; the generator's return value is the full list."""

    position: int
    total: int
    record: VerifyRecord

    @property
    def fraction(self) -> float:
        return min(1.0, self.position / self.total) if self.total else 0.0


# ── Signal extractors ───────────────────────────────────────────────────────

def _dashboard_signals(payload: object) -> dict[str, int]:
    """Tab count, worklet count, prompt-set reference count.

    Tabs and worklets are the shape of an empty-shell dashboard — HANDOFF's
    "0 failed" bug. Prompt-set references caught a related class of write:
    the dashboard writes clean, but its `Prompt_Set_Reference` list ends up
    partially populated. Both are integers so a comparison stays readable.
    """
    tabs = 0
    worklets = 0
    prompt_sets = 0

    stack: list[object] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "Custom_Dashboard_Tab_Data":
                    tabs += _list_len(value)
                if key in ("Worklets_Data", "Content_Data"):
                    worklets += _list_len(value)
                if key == "Prompt_Set_Reference":
                    prompt_sets += _list_len(value)
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)

    return {"tabs": tabs, "worklets": worklets, "prompt_set_refs": prompt_sets}


def _list_len(value: object) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return 1
    return 0


def _report_signals(payload: object) -> dict[str, int]:
    """Column count and filter-condition count.

    A report that lost its columns has been observed on prior migrations
    (see the Span-of-Control duplicate story in HANDOFF); columns are the
    highest-signal thing to check on a report. Filter conditions come next —
    stripping ``Filter_Instances_Reference`` from a condition is legal but
    dropping the whole condition is not.
    """
    data = _data_block(payload, "Tenanted_Report_Definition_Data")
    columns = data.get("Report_Column_Data") if isinstance(data, dict) else None

    conditions = 0
    stack: list[object] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "Condition_Item_Data":
                    conditions += _list_len(value)
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)

    return {
        "columns": _list_len(columns),
        "filter_conditions": conditions,
    }


def _prompt_set_signals(payload: object) -> dict[str, int]:
    data = _data_block(payload, "Prompt_Set_Data")
    members = data.get("Tenanted_Prompt_Set_Member_Data") if isinstance(data, dict) else None
    return {"members": _list_len(members)}


def _calculated_field_signals(payload: object) -> dict[str, int]:
    """Only whether the data block is populated. Nested operands and business
    IDs are remapped between tenants; comparing them would flag every field."""
    data = _data_block(payload, "Calculated_Field_Data")
    return {"has_data_block": 1 if data else 0}


def _calculated_measure_signals(payload: object) -> dict[str, int]:
    data = _data_block(payload, "Calculated_Measure_Data")
    return {"has_data_block": 1 if data else 0}


def _analytic_indicator_signals(payload: object) -> dict[str, int]:
    data = _data_block(payload, "Analytic_Indicator_Data")
    return {"has_data_block": 1 if data else 0}


def _gauge_range_signals(payload: object) -> dict[str, int]:
    data = _data_block(payload, "Gauge_Range_Data")
    return {"has_data_block": 1 if data else 0}


def _prompt_field_signals(payload: object) -> dict[str, int]:
    data = _data_block(payload, "Prompt_Field_Data")
    return {"has_data_block": 1 if data else 0}


def _data_block(payload: object, key: str) -> object:
    """A data block, whether zeep returned it singular or as a one-element list."""
    if not isinstance(payload, dict):
        return {}
    value = payload.get(key)
    if isinstance(value, list):
        return value[0] if value else {}
    return value or {}


_SIGNAL_EXTRACTORS = {
    NodeKind.CALCULATED_FIELD: _calculated_field_signals,
    NodeKind.CALCULATED_MEASURE: _calculated_measure_signals,
    NodeKind.REPORT: _report_signals,
    NodeKind.DASHBOARD_TABBED: _dashboard_signals,
    NodeKind.DASHBOARD: _dashboard_signals,
    NodeKind.PROMPT_SET: _prompt_set_signals,
    NodeKind.PROMPT_FIELD: _prompt_field_signals,
    NodeKind.GAUGE_RANGE: _gauge_range_signals,
    NodeKind.ANALYTIC_INDICATOR: _analytic_indicator_signals,
}


# ── Destination fetch, per kind ─────────────────────────────────────────────

def _fetch_destination(connection: Connection, node: Node, dest_wid: str | None):
    """Fetch one written object back from the destination.

    Prefer the destination WID the writer recorded — it is guaranteed to
    exist there and cannot be ambiguous. Fall back to the source's business
    id only when no dest WID is known (a SKIPPED-then-re-verified case).
    """
    kind = node.kind
    if kind is NodeKind.CALCULATED_FIELD:
        return lookup_calculated_field(connection, wid=dest_wid) if dest_wid else (
            lookup_calculated_field(connection, reference_id=node.reference_id)
            if node.reference_id else None
        )
    if kind is NodeKind.CALCULATED_MEASURE:
        return lookup_calculated_measure(connection, wid=dest_wid) if dest_wid else (
            lookup_calculated_measure(connection, reference_id=node.reference_id)
            if node.reference_id else None
        )
    if kind is NodeKind.REPORT:
        return lookup_report(connection, wid=dest_wid) if dest_wid else None
    if kind in DASHBOARD_TABBED_BY_KIND:
        tabbed = DASHBOARD_TABBED_BY_KIND[kind]
        if dest_wid:
            return lookup_dashboard(connection, tabbed=tabbed, wid=dest_wid)
        if node.reference_id:
            return lookup_dashboard(connection, tabbed=tabbed, reference_id=node.reference_id)
        return None
    if kind is NodeKind.PROMPT_SET:
        return lookup_prompt_set(connection, wid=dest_wid) if dest_wid else (
            lookup_prompt_set(connection, reference_id=node.reference_id)
            if node.reference_id else None
        )
    if kind is NodeKind.PROMPT_FIELD:
        return lookup_prompt_field(connection, wid=dest_wid) if dest_wid else (
            lookup_prompt_field(connection, reference_id=node.reference_id)
            if node.reference_id else None
        )
    if kind is NodeKind.GAUGE_RANGE:
        return lookup_gauge_range(connection, wid=dest_wid) if dest_wid else (
            lookup_gauge_range(connection, reference_id=node.reference_id)
            if node.reference_id else None
        )
    if kind is NodeKind.ANALYTIC_INDICATOR:
        return lookup_analytic_indicator(connection, wid=dest_wid) if dest_wid else None
    return None


# ── Public entry points ─────────────────────────────────────────────────────

def verify_record(
    connection: Connection,
    node: Node,
    record: WriteRecord,
) -> VerifyRecord:
    """Read one written object back and compare it to its source payload.

    Handles the three special cases up front:

    - A SKIPPED write is not a bug — the destination already had this object
      unchanged, so ``VerifyStatus.SKIPPED`` is returned without a network
      call.
    - A FAILED or INDETERMINATE write is not verified either; the
      write-side status already tells the whole story.
    - A CREATE/UPDATE that succeeded but comes back MISSING is the
      HANDOFF-flagged failure mode: the writer's own success bit lied. This
      is the case worth going to lengths to catch.
    """
    if record.status is WriteStatus.SKIPPED:
        return VerifyRecord(
            node_id=node.node_id,
            kind=node.kind.value,
            name=node.name,
            status=VerifyStatus.SKIPPED,
            dest_wid=record.dest_wid,
        )
    if record.status is not WriteStatus.SUCCESS:
        return VerifyRecord(
            node_id=node.node_id,
            kind=node.kind.value,
            name=node.name,
            status=VerifyStatus.ERROR,
            fault=f"Write recorded {record.status.value}; nothing to verify.",
            dest_wid=record.dest_wid,
        )

    try:
        result = _fetch_destination(connection, node, record.dest_wid)
    except Exception as exc:  # noqa: BLE001 - surfaced as ERROR, not silently dropped
        return VerifyRecord(
            node_id=node.node_id,
            kind=node.kind.value,
            name=node.name,
            status=VerifyStatus.ERROR,
            fault=connection.redact(str(exc)),
            dest_wid=record.dest_wid,
        )

    if result is None:
        return VerifyRecord(
            node_id=node.node_id,
            kind=node.kind.value,
            name=node.name,
            status=VerifyStatus.ERROR,
            fault="No lookup path for this kind — verifier does not know how to fetch it.",
            dest_wid=record.dest_wid,
        )

    if result.outcome is LookupOutcome.NOT_FOUND:
        return VerifyRecord(
            node_id=node.node_id,
            kind=node.kind.value,
            name=node.name,
            status=VerifyStatus.MISSING,
            fault=(
                "Write recorded SUCCESS, but the destination has no such object. "
                "This is exactly the empty-shell class of bug read-back exists to catch."
            ),
            dest_wid=record.dest_wid,
        )
    if result.outcome is not LookupOutcome.FOUND or result.data is None:
        return VerifyRecord(
            node_id=node.node_id,
            kind=node.kind.value,
            name=node.name,
            status=VerifyStatus.ERROR,
            fault=result.fault or f"Unexpected lookup outcome: {result.outcome.value}",
            dest_wid=record.dest_wid,
        )

    findings = _compare(node, result.data)

    # A dashboard that reads back with no worklets is a shell, whatever the
    # signal comparison says — the same detection used at probe time is worth
    # applying here so the mismatch surfaces even if the source somehow had
    # zero too.
    if node.kind in DASHBOARD_TABBED_BY_KIND and not dashboard_has_worklets(result.data):
        findings.append(
            VerifyFinding(
                signal="dashboard_is_shell",
                source="worklets present",
                destination="no worklets in the destination copy",
            )
        )

    return VerifyRecord(
        node_id=node.node_id,
        kind=node.kind.value,
        name=node.name,
        status=VerifyStatus.MISMATCH if findings else VerifyStatus.OK,
        findings=findings,
        dest_wid=record.dest_wid or ids_of(result.wid and {"ID": [{"type": "WID", "_value_1": result.wid}]}).get("WID"),
    )


def _compare(node: Node, destination_payload: object) -> list[VerifyFinding]:
    """Structural comparison per kind.

    Signals are hand-picked to be **stable across the write-side rewrites**:
    tab count, worklet count, member count, and column count survive WID
    remaps, owner substitution, and tag stripping. That is the whole point
    of comparing signals rather than payloads.
    """
    extractor = _SIGNAL_EXTRACTORS.get(node.kind)
    if extractor is None:
        return []

    source_signals = extractor(node.payload)
    dest_signals = extractor(destination_payload)

    findings: list[VerifyFinding] = []
    for signal, expected in source_signals.items():
        actual = dest_signals.get(signal)
        if actual != expected:
            findings.append(
                VerifyFinding(signal=signal, source=expected, destination=actual)
            )
    return findings


def iter_verify(
    connection: Connection,
    plan_nodes: list[Node],
    records: list[WriteRecord],
) -> Iterator[VerifyProgress]:
    """Verify every written object in order.

    ``plan_nodes`` is used to look up each node by ``node_id`` because a
    ``WriteRecord`` carries only enough for a report row — the payload lives
    on the closure's ``Node``. Ordering matches ``records``; the caller
    should pass the plan's ``ordered_nodes`` alongside the writer's records.
    """
    by_id = {node.node_id: node for node in plan_nodes}
    total = len(records)

    for position, record in enumerate(records, start=1):
        node = by_id.get(record.node_id)
        if node is None:
            yield VerifyProgress(
                position=position,
                total=total,
                record=VerifyRecord(
                    node_id=record.node_id,
                    kind=record.kind,
                    name=record.name,
                    status=VerifyStatus.ERROR,
                    fault="Node not present in the plan.",
                ),
            )
            continue

        yield VerifyProgress(
            position=position,
            total=total,
            record=verify_record(connection, node, record),
        )


def summarise(records: list[VerifyRecord]) -> dict[str, int]:
    """Counts per :class:`VerifyStatus`, for a headline row."""
    counts = {status.value: 0 for status in VerifyStatus}
    for record in records:
        counts[record.status.value] += 1
    return counts
