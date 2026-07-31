"""Reading calculated fields and report definitions out of a tenant.

Three things shape this module.

**There is no server-side search worth using.** ``Calculated_Field_Request_Criteria``
is an empty type — no filtering at all — and ``Report_Name`` is *exact match*
(a substring returns zero rows). So discovery means building a local index and
searching that. Measured on the source tenant: the full calculated-field index
is ~9,652 records, ~34 MB of JSON, ~19s at ``Count=999``. That is small enough
to hold in memory and cache to disk, which is what makes the rest of the tool
responsive.

**``Count`` accepts 999, not 100.** The WSDL types it as a 3-digit decimal.
This is a 10x reduction in page count and is the single reason indexing is
cheap enough to just do.

**Not-found must be distinguishable from went-wrong.** A targeted lookup that
misses raises a SOAP fault, and so does a permissions problem. Collapsing those
together would be dangerous: downstream, "missing" means "create", and creating
something that already exists is how you get duplicates in a tenant with no
delete operation. Hence the three-valued :class:`LookupOutcome`.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator

from zeep.helpers import serialize_object

from wdmigrator.auth.client import Connection

#: Max rows per page. The WSDL types Count as a 3-digit decimal, and 999 is
#: confirmed working live. Do not raise this without re-testing.
PAGE_SIZE = 999

#: Fault fragments that mean "this object does not exist here".
#:
#: Matched conservatively and case-insensitively. Everything not matching is
#: treated as UNKNOWN rather than assumed missing — see the module docstring.
#: Observed live:
#:   by Calculated_Field_ID, no such ID:
#:     "Validation error occurred. Invalid ID value.  'X' is not a valid ID
#:      value for type = 'Calculated_Field_ID'"
#:   by WID, valid WID but not a calculated field:
#:     "Validation error occurred. Invalid instance 1435$104 for
#:      Calculated_Field_Request_References--IS (15$40527)"
_NOT_FOUND_FRAGMENTS = (
    "is not a valid id value for type",
    "invalid instance",
)


class LookupOutcome(str, Enum):
    """Three-valued on purpose. ``UNKNOWN`` must never be treated as missing."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LookupResult:
    outcome: LookupOutcome
    data: dict | None = None
    wid: str | None = None
    reference_id: str | None = None
    fault: str | None = None

    @property
    def found(self) -> bool:
        return self.outcome is LookupOutcome.FOUND


@dataclass(frozen=True)
class CalculatedFieldSummary:
    """The slim record used for pickers and WID classification."""

    wid: str
    reference_id: str | None
    name: str | None
    class_name: str | None
    do_not_use: bool = False
    intermediate: bool = False


@dataclass(frozen=True)
class ReportSummary:
    wid: str
    custom_report_id: str | None
    name: str | None
    data_source_wid: str | None = None
    owner: str | None = None


@dataclass
class Index:
    """An in-memory index of one object kind for one tenant.

    Holds both slim summaries (for pickers) and full payloads (for dependency
    walking), because at 34 MB there is no reason to make the resolver refetch.
    """

    kind: str
    tenant: str
    fetched_at: float
    summaries: dict[str, Any] = field(default_factory=dict)
    payloads: dict[str, dict] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.summaries)

    def __contains__(self, wid: str) -> bool:
        return wid in self.summaries

    def payload(self, wid: str) -> dict | None:
        return self.payloads.get(wid)

    def by_reference_id(self) -> dict[str, Any]:
        return {
            s.reference_id: s
            for s in self.summaries.values()
            if getattr(s, "reference_id", None) or getattr(s, "custom_report_id", None)
        }

    def age_seconds(self) -> float:
        return time.time() - self.fetched_at


@dataclass(frozen=True)
class IndexProgress:
    """One page of progress.

    ``index`` is the *accumulating* index and is populated on every event, not
    just the last one. That is what makes a sweep cancellable: a caller that
    stops early still has everything fetched so far, which matters for the
    ~160s report sweep. Use ``complete`` to tell a finished sweep from a
    partial one — a partial index is real data, but it is not the whole tenant.
    """

    page: int
    total_pages: int
    fetched: int
    total: int
    elapsed: float
    index: Index
    complete: bool = False

    @property
    def result(self) -> Index | None:
        """The finished index, or None if the sweep is still running."""
        return self.index if self.complete else None

    @property
    def done(self) -> bool:
        return self.complete

    @property
    def fraction(self) -> float:
        return min(1.0, self.fetched / self.total) if self.total else 0.0


# ── Reference helpers ────────────────────────────────────────────────────────


def ids_of(reference: dict | None) -> dict[str, str]:
    """Flatten a Workday reference's ID list into ``{type: value}``."""
    if not reference:
        return {}
    return {
        entry.get("type"): entry.get("_value_1")
        for entry in reference.get("ID") or []
        if isinstance(entry, dict) and entry.get("type")
    }


def _reference(id_type: str, value: str) -> dict:
    return {"ID": [{"type": id_type, "_value_1": value}]}


def classify_fault(message: str) -> LookupOutcome:
    """Decide whether a fault means "absent" or "something else went wrong".

    Deliberately conservative: only the specific fragments observed for a
    genuine miss return ``NOT_FOUND``. An entitlement fault ("the web service
    or version is invalid"), an auth failure, or a timeout all fall through to
    ``UNKNOWN`` so the caller blocks instead of assuming it can create.
    """
    lowered = (message or "").lower()
    if any(fragment in lowered for fragment in _NOT_FOUND_FRAGMENTS):
        return LookupOutcome.NOT_FOUND
    return LookupOutcome.UNKNOWN


# ── Targeted lookups ─────────────────────────────────────────────────────────


def lookup_calculated_field(
    connection: Connection,
    *,
    reference_id: str | None = None,
    wid: str | None = None,
) -> LookupResult:
    """Fetch one calculated field by business ID or WID.

    This is the primitive behind both custom-vs-delivered discrimination and
    destination conflict detection: an object exists in a tenant iff this
    returns ``FOUND`` there.
    """
    if bool(reference_id) == bool(wid):
        raise ValueError("Pass exactly one of reference_id or wid.")

    ref = (
        _reference("Calculated_Field_ID", reference_id)
        if reference_id
        else _reference("WID", wid)
    )

    try:
        connection.limiter.wait()
        response = connection.service.Get_Calculated_Fields(
            Request_References={"Calculated_Field_Reference": [ref]},
            Response_Group={
                "Include_Reference": True,
                "Include_Calculated_Field_Data": True,
            },
        )
    except Exception as exc:  # noqa: BLE001 - classified, never blindly swallowed
        message = connection.redact(str(exc))
        return LookupResult(
            outcome=classify_fault(message),
            reference_id=reference_id,
            wid=wid,
            fault=message,
        )

    data = serialize_object(response)
    items = (data.get("Response_Data") or {}).get("Calculated_Field") or []
    if not items:
        # A successful call with no rows. Treated as absent rather than
        # unknown: the request was accepted, there is simply nothing there.
        return LookupResult(
            outcome=LookupOutcome.NOT_FOUND, reference_id=reference_id, wid=wid
        )

    item = items[0]
    ids = ids_of(item.get("Calculated_Field_Reference"))
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        data=item,
        wid=ids.get("WID"),
        reference_id=ids.get("Calculated_Field_ID") or reference_id,
    )


def lookup_report(
    connection: Connection,
    *,
    custom_report_id: str | None = None,
    wid: str | None = None,
) -> LookupResult:
    """Fetch one report definition by ``Custom_Report_ID`` or WID."""
    if bool(custom_report_id) == bool(wid):
        raise ValueError("Pass exactly one of custom_report_id or wid.")

    ref = (
        _reference("Custom_Report_ID", custom_report_id)
        if custom_report_id
        else _reference("WID", wid)
    )

    try:
        connection.limiter.wait()
        response = connection.service.Get_Tenanted_Report_Definitions(
            Request_References={"Tenanted_Report_Definition_Reference": [ref]},
            Response_Group={
                "Include_Reference": True,
                "Include_Tenanted_Report_Definition_Data": True,
            },
        )
    except Exception as exc:  # noqa: BLE001
        message = connection.redact(str(exc))
        return LookupResult(
            outcome=classify_fault(message),
            reference_id=custom_report_id,
            wid=wid,
            fault=message,
        )

    data = serialize_object(response)
    items = (data.get("Response_Data") or {}).get("Tenanted_Report_Definition") or []
    if not items:
        return LookupResult(
            outcome=LookupOutcome.NOT_FOUND, reference_id=custom_report_id, wid=wid
        )

    item = items[0]
    ids = ids_of(item.get("Tenanted_Report_Definition_Reference"))
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        data=item,
        wid=ids.get("WID"),
        reference_id=ids.get("Custom_Report_ID") or custom_report_id,
    )


def find_report_by_exact_name(
    connection: Connection, name: str
) -> ReportSummary | None:
    """Look a report up by name.

    ``Report_Name`` is **exact match** — a substring finds nothing. Use the
    index for anything resembling a search.
    """
    connection.limiter.wait()
    response = connection.service.Get_Tenanted_Report_Definitions(
        Request_Criteria={"Report_Name": name},
        Response_Filter={"Page": 1, "Count": 1},
        Response_Group={
            "Include_Reference": True,
            "Include_Tenanted_Report_Definition_Data": True,
        },
    )
    data = serialize_object(response)
    items = (data.get("Response_Data") or {}).get("Tenanted_Report_Definition") or []
    return _report_summary(items[0]) if items else None


# ── Index sweeps ─────────────────────────────────────────────────────────────


def _calculated_field_summary(item: dict) -> CalculatedFieldSummary | None:
    ids = ids_of(item.get("Calculated_Field_Reference"))
    wid = ids.get("WID")
    if not wid:
        return None
    data = item.get("Calculated_Field_Data") or {}
    return CalculatedFieldSummary(
        wid=wid,
        reference_id=data.get("Calculated_Field_Reference_ID")
        or ids.get("Calculated_Field_ID"),
        name=data.get("Name"),
        class_name=data.get("Class_Name"),
        do_not_use=bool(data.get("Do_Not_Use")),
        intermediate=bool(data.get("Intermediate_Calculation")),
    )


def _report_summary(item: dict) -> ReportSummary | None:
    ids = ids_of(item.get("Tenanted_Report_Definition_Reference"))
    wid = ids.get("WID")
    if not wid:
        return None
    data = item.get("Tenanted_Report_Definition_Data") or {}
    owner_ids = ids_of(data.get("Tenanted_Report_Definition_System_User_Reference"))
    return ReportSummary(
        wid=wid,
        custom_report_id=ids.get("Custom_Report_ID"),
        name=data.get("Name"),
        data_source_wid=ids_of(data.get("Data_Source_Reference")).get("WID"),
        owner=owner_ids.get("WorkdayUserName") or owner_ids.get("System_User_ID"),
    )


def _iter_index(
    connection: Connection,
    *,
    kind: str,
    operation_name: str,
    response_group: dict,
    collection_key: str,
    summarise: Callable[[dict], Any],
    page_size: int,
) -> Iterator[IndexProgress]:
    """Shared pagination driver for both object kinds."""
    started = time.monotonic()
    index = Index(kind=kind, tenant=connection.target.tenant, fetched_at=time.time())
    operation = getattr(connection.service, operation_name)

    page = 1
    total_pages = 1
    total = 0

    while True:
        connection.limiter.wait()
        response = operation(
            Response_Filter={"Page": page, "Count": page_size},
            Response_Group=response_group,
        )
        data = serialize_object(response)

        results = data.get("Response_Results") or {}
        total = int(results.get("Total_Results") or 0)
        total_pages = max(1, int(results.get("Total_Pages") or 1))

        for item in (data.get("Response_Data") or {}).get(collection_key) or []:
            summary = summarise(item)
            if summary is None:
                continue
            index.summaries[summary.wid] = summary
            index.payloads[summary.wid] = item

        elapsed = time.monotonic() - started
        final = page >= total_pages
        yield IndexProgress(
            page=page,
            total_pages=total_pages,
            fetched=len(index.summaries),
            total=total,
            elapsed=elapsed,
            index=index,
            complete=final,
        )
        if final:
            return
        page += 1


def iter_calculated_field_index(
    connection: Connection, *, page_size: int = PAGE_SIZE
) -> Iterator[IndexProgress]:
    """Sweep every calculated field. ~10 pages / ~20s / ~34 MB on the test tenant."""
    return _iter_index(
        connection,
        kind="calculated_field",
        operation_name="Get_Calculated_Fields",
        response_group={
            "Include_Reference": True,
            "Include_Calculated_Field_Data": True,
        },
        collection_key="Calculated_Field",
        summarise=_calculated_field_summary,
        page_size=page_size,
    )


def iter_report_index(
    connection: Connection, *, page_size: int = PAGE_SIZE
) -> Iterator[IndexProgress]:
    """Sweep every report definition.

    Full data is required, not just references: a reference-only sweep returns
    ``{WID, Custom_Report_ID}`` with **no name**, which is useless for a picker.
    That makes this the slower of the two sweeps (~6 pages / ~160s).
    """
    return _iter_index(
        connection,
        kind="report",
        operation_name="Get_Tenanted_Report_Definitions",
        response_group={
            "Include_Reference": True,
            "Include_Tenanted_Report_Definition_Data": True,
        },
        collection_key="Tenanted_Report_Definition",
        summarise=_report_summary,
        page_size=page_size,
    )


def build_index(
    iterator: Iterator[IndexProgress],
    on_progress: Callable[[IndexProgress], None] | None = None,
) -> Index:
    """Drain an index generator to completion. Convenience for CLI/tests."""
    result: Index | None = None
    for progress in iterator:
        if on_progress:
            on_progress(progress)
        if progress.result is not None:
            result = progress.result
    if result is None:
        raise RuntimeError("Index sweep produced no result.")
    return result


# ── Disk cache ───────────────────────────────────────────────────────────────

#: Already gitignored. Indexes are tenant configuration, not secrets, but they
#: are still tenant data — keep them out of the repo.
CACHE_ROOT = Path("out") / "cache"


def cache_path(connection: Connection, kind: str, root: Path | None = None) -> Path:
    """Per-tenant cache location. Keyed by tenant so source and dest never mix."""
    base = root or CACHE_ROOT
    return base / connection.target.tenant / f"{kind}.json"


def save_index(index: Index, path: Path) -> Path:
    """Persist an index. Contains tenant config only — never credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "kind": index.kind,
        "tenant": index.tenant,
        "fetched_at": index.fetched_at,
        "summaries": {w: asdict(s) for w, s in index.summaries.items()},
        "payloads": index.payloads,
    }
    path.write_text(json.dumps(document, default=str), encoding="utf-8")
    return path


def load_index(
    path: Path, *, tenant: str | None = None, max_age_seconds: float | None = None
) -> Index | None:
    """Load a cached index, or None if absent, stale, or for the wrong tenant.

    The tenant check matters: a cache keyed to the wrong tenant would silently
    classify the wrong objects as already-existing.
    """
    if not path.is_file():
        return None

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if tenant is not None and document.get("tenant") != tenant:
        return None

    fetched_at = float(document.get("fetched_at") or 0)
    if max_age_seconds is not None and (time.time() - fetched_at) > max_age_seconds:
        return None

    summary_type = (
        CalculatedFieldSummary
        if document.get("kind") == "calculated_field"
        else ReportSummary
    )
    return Index(
        kind=document.get("kind", ""),
        tenant=document.get("tenant", ""),
        fetched_at=fetched_at,
        summaries={
            wid: summary_type(**fields)
            for wid, fields in (document.get("summaries") or {}).items()
        },
        payloads=document.get("payloads") or {},
    )
