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
from typing import Any, Callable, Iterator, Mapping

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


#: The two custom-dashboard flavours. There is no unified operation: a tabbed
#: dashboard is a ``Custom_Landing_Page_Group`` and an untabbed one is a
#: ``Custom_Landing_Page``, with separate Get, Put, data block, response
#: reference and ID space. Nothing in a reference says which one you have, so
#: both are always swept and the flavour is carried on the summary from there.
DASHBOARD_FLAVOURS = {
    # tabbed -> operation/keys
    False: {
        "get": "Get_Custom_Dashboards_without_Tabs",
        "put": "Put_Custom_Dashboard_without_Tabs",
        "collection": "Custom_Dashboard_without_Tabs",
        "reference": "Custom_Dashboard_without_Tabs_Reference",
        "data": "Custom_Dashboard_without_Tabs_Data",
        "id_type": "Custom_Landing_Page_ID",
    },
    True: {
        "get": "Get_Custom_Dashboards_with_Tabs",
        "put": "Put_Custom_Dashboard_with_Tabs",
        "collection": "Custom_Dashboard_with_Tabs",
        "reference": "Custom_Dashboard_with_Tabs_Reference",
        "data": "Custom_Dashboard_with_Tabs_Data",
        "id_type": "Custom_Landing_Page_Group_ID",
    },
}

#: The fault a non-implementer account gets from every dashboard operation.
#: Distinct from the `Report_Metadata` entitlement failure ("the web service or
#: version is invalid for the requested operation") — this one means the
#: operation is valid and the *account* is not allowed to call it.
IMPLEMENTER_REQUIRED_FRAGMENT = "task submitted is not authorized"

#: What to tell a user who hits it. The dashboard, prompt-set, prompt-field and
#: worklet-configuration operations are all gated this way; calculated fields,
#: reports and calculated measures are not (confirmed live 2026-08-07 on
#: commitconsulting_dpt1: the same ISU reads 9,716 fields and 5,197 reports
#: fine, and fails all five dashboard-side operations).
IMPLEMENTER_REQUIRED_REMEDY = (
    "Custom dashboards require an implementer account. A normal Integration "
    "System User cannot read or write them no matter which domains it is "
    "granted — connect with an implementer account on both tenants, or migrate "
    "reports and calculated fields only."
)


def requires_implementer(fault: str | None) -> bool:
    """Whether a fault means "this account is not an implementer"."""
    return IMPLEMENTER_REQUIRED_FRAGMENT in (fault or "").lower()


@dataclass(frozen=True)
class DashboardSummary:
    wid: str
    reference_id: str | None
    name: str | None
    #: Which flavour this is, and therefore which Get/Put operation addresses
    #: it. Carried rather than re-derived: the two ID spaces are disjoint and
    #: guessing wrong sends a write to the wrong operation.
    tabbed: bool = False
    worklet_count: int = 0


@dataclass(frozen=True)
class CalculatedMeasureSummary:
    wid: str
    reference_id: str | None
    name: str | None
    business_object_wid: str | None = None


@dataclass(frozen=True)
class AnalyticIndicatorSummary:
    """A matrix measure display option — the marker beside a matrix value."""

    wid: str
    reference_id: str | None
    name: str | None


@dataclass(frozen=True)
class GaugeRangeSummary:
    """A custom analytic range — the colour banding on a report's gauge."""

    wid: str
    reference_id: str | None
    name: str | None


@dataclass(frozen=True)
class PromptFieldSummary:
    """A tenanted external parameter — what a prompt set's members point at."""

    wid: str
    reference_id: str | None
    name: str | None


@dataclass(frozen=True)
class PromptSetSummary:
    wid: str
    reference_id: str | None
    name: str | None
    member_count: int = 0


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


# ── Cross-tenant shape matching for calculated fields ────────────────────────


def calculated_field_data(payload: Mapping | None) -> dict:
    """The ``Calculated_Field_Data`` block, whether it came back singular or in
    a one-element list."""
    data = (payload or {}).get("Calculated_Field_Data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def calculated_field_shape(payload: Mapping | None) -> tuple[str, str, str] | None:
    """A calculated field's cross-tenant *shape*: name, class, business object.

    ``Calculated_Field_Reference_ID`` was assumed to be a stable cross-tenant
    identifier, and it is not — it is stable only when both tenants acquired the
    field the same way. Confirmed live 2026-08-11 between `commitconsulting_dpt1`
    and `_dpt3`, whose conventions diverge completely: the same field is
    ``CRTMNU01_Commit - HR Dashboard_03_Is Top Performer`` on one and
    ``Custom Object Data - Is Top Performer`` on the other. Probing by ID alone
    reported 48 fields as absent that were plainly present, and creating them
    would have duplicated them onto the same business objects.

    The three parts are chosen because each is comparable across tenants:
    ``Name`` and ``Class_Name`` are plain strings, and the business object is a
    Workday-delivered object whose WID is identical in every tenant.

    Returns ``None`` when any part is missing — an incomplete shape must never
    match, since a match means "do not create".
    """
    data = calculated_field_data(payload)
    name = data.get("Name")
    class_name = data.get("Class_Name")
    business_object = ids_of(data.get("External_Field_Reference")).get("WID")
    if not (name and class_name and business_object):
        return None
    return (str(name), str(class_name), str(business_object))


@dataclass(frozen=True)
class CalculatedFieldMatchIndex:
    """Everything needed to recognise a source field in the destination.

    Three maps rather than one because matching happens in tiers, weakest last:
    shape identifies a field, ``WQL_Alias`` disambiguates between fields that
    share a shape, and alias alone is the last resort for a field the
    destination renamed. See
    :func:`~wdmigrator.migrate.planner.probe_node` for the order they are tried.
    """

    #: (name, class, business object) -> WIDs carrying it.
    by_shape: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    #: WQL_Alias -> WIDs carrying it. Not unique: `commitconsulting_dpt3` has
    #: five fields sharing ``cf_ExecutiveGroup``.
    by_alias: dict[str, list[str]] = field(default_factory=dict)
    #: WID -> its own alias, for breaking a tie within ``by_shape``.
    alias_of: dict[str, str] = field(default_factory=dict)
    #: WID -> its own shape, for narrowing an ambiguous ``by_alias`` hit down to
    #: the candidate sharing the source's business object.
    shape_of: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    #: WID -> its own ``Calculated_Field_ID``. A cross-tenant match learns the
    #: destination's WID *and* its business id, and nested references use the
    #: business id — see
    #: :func:`~wdmigrator.migrate.ordering.substitute_reference_ids`.
    reference_id_of: dict[str, str] = field(default_factory=dict)


def calculated_field_match_index(index: "Index") -> CalculatedFieldMatchIndex:
    """Index a destination tenant's calculated fields for cross-tenant matching.

    Every map holds a *list* of WIDs on purpose. A tenant can hold several
    fields with one name on one business object — `commitconsulting_dpt3` has
    five called ``Executive Group`` — and those have to surface as ambiguous
    rather than silently resolving to whichever was seen first.
    """
    by_shape: dict[tuple[str, str, str], list[str]] = {}
    by_alias: dict[str, list[str]] = {}
    alias_of: dict[str, str] = {}
    shape_of: dict[str, tuple[str, str, str]] = {}
    reference_id_of: dict[str, str] = {}
    for wid in index.summaries:
        summary_reference_id = getattr(index.summaries[wid], "reference_id", None)
        if summary_reference_id:
            reference_id_of[wid] = str(summary_reference_id)
        payload = index.payload(wid)
        shape = calculated_field_shape(payload)
        if shape is not None:
            by_shape.setdefault(shape, []).append(wid)
            shape_of[wid] = shape
        alias = calculated_field_data(payload).get("WQL_Alias")
        if alias:
            by_alias.setdefault(str(alias), []).append(wid)
            alias_of[wid] = str(alias)
    return CalculatedFieldMatchIndex(
        by_shape=by_shape,
        by_alias=by_alias,
        alias_of=alias_of,
        shape_of=shape_of,
        reference_id_of=reference_id_of,
    )


def calculated_measure_data(payload: Mapping | None) -> dict:
    """The ``Calculated_Measure_Data`` block, singular or one-element list."""
    data = (payload or {}).get("Calculated_Measure_Data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def calculated_measure_shape(payload: Mapping | None) -> tuple[str, str] | None:
    """A calculated measure's cross-tenant shape: name and business object.

    Thinner than :func:`calculated_field_shape`, which also has ``Class_Name``.
    ``Calculated_Measure_DataType`` has no equivalent — the measure's type shows
    only in which sub-type block is populated, and that is not a comparable
    scalar. So this asserts identity on two parts rather than three, and is
    correspondingly weaker.

    It holds on the tenants seen so far: `commitconsulting_dpt5` has 47 measures
    and **zero** duplicate names (confirmed live 2026-08-12), so name alone is
    already unique there and the business object is a second, free check.
    :func:`~wdmigrator.migrate.planner.probe_node` still refuses to guess when
    more than one candidate shares a shape.

    Returns ``None`` if either part is missing — an incomplete shape must never
    match, because a match means "do not create".
    """
    data = calculated_measure_data(payload)
    name = data.get("Name")
    business_object = ids_of(data.get("Business_Object_Reference")).get("WID")
    if not (name and business_object):
        return None
    return (str(name), str(business_object))


def calculated_measure_match_index(index: "Index") -> dict[tuple[str, str], list[str]]:
    """Map each measure shape in ``index`` to the WIDs carrying it.

    A list, not a single WID, for the same reason as calculated fields: a
    duplicate has to surface as ambiguous rather than silently resolve to
    whichever the sweep saw first.
    """
    shapes: dict[tuple[str, str], list[str]] = {}
    for wid in index.summaries:
        shape = calculated_measure_shape(index.payload(wid))
        if shape is not None:
            shapes.setdefault(shape, []).append(wid)
    return shapes


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


def lookup_calculated_measure(
    connection: Connection,
    *,
    reference_id: str | None = None,
    wid: str | None = None,
) -> LookupResult:
    """Fetch one calculated measure by ``BI_Calculated_Measure_ID`` or WID.

    Measures are never swept into an index the way calculated fields are. A
    tenant holds a handful of them (9 on the source tenant, 18 on the
    destination) and they are only ever reached as a dependency of a report
    that uses one, so a targeted lookup per reference is cheaper than a sweep
    and cannot go stale mid-run.

    ``BI_Calculated_Measure_ID`` is the cross-tenant identity — the WID is
    tenant-specific, and measures created by a report PUT get a fresh one.
    """
    if bool(reference_id) == bool(wid):
        raise ValueError("Pass exactly one of reference_id or wid.")

    ref = (
        _reference("BI_Calculated_Measure_ID", reference_id)
        if reference_id
        else _reference("WID", wid)
    )

    try:
        connection.limiter.wait()
        response = connection.service.Get_Calculated_Measures(
            Request_References={"Calculated_Measure_Reference": [ref]},
            Response_Group={
                "Include_Reference": True,
                "Include_Calculated_Measure_Data": True,
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
    items = (data.get("Response_Data") or {}).get("Calculated_Measure") or []
    if not items:
        return LookupResult(
            outcome=LookupOutcome.NOT_FOUND, reference_id=reference_id, wid=wid
        )

    item = items[0]
    ids = ids_of(item.get("Calculated_Measure_Reference"))
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        data=item,
        wid=ids.get("WID"),
        reference_id=ids.get("BI_Calculated_Measure_ID") or reference_id,
    )


def lookup_dashboard(
    connection: Connection,
    *,
    tabbed: bool,
    reference_id: str | None = None,
    wid: str | None = None,
) -> LookupResult:
    """Fetch one custom dashboard by its business ID or WID.

    ``tabbed`` picks the operation, and it is required rather than inferred:
    ``Custom_Landing_Page_ID`` and ``Custom_Landing_Page_Group_ID`` are separate
    ID spaces addressed by separate operations, and asking the wrong one returns
    a clean "not found" that would be read as "safe to create".

    Unlike reports, the business ID **works as a lookup key** — confirmed live
    2026-08-07 on `commitconsulting_dpt1`, fetching
    ``Custom_Landing_Page_Group_ID = 'Commit - Optimize Reporting Dashboard'``
    resolved the same dashboard as its WID. So dashboards get real cross-tenant
    identity, and none of the name-matching compromise
    :func:`lookup_report_by_name` is forced into.

    Requires an implementer account; see :data:`IMPLEMENTER_REQUIRED_REMEDY`.
    """
    if bool(reference_id) == bool(wid):
        raise ValueError("Pass exactly one of reference_id or wid.")

    spec = DASHBOARD_FLAVOURS[bool(tabbed)]
    ref = (
        _reference(spec["id_type"], reference_id)
        if reference_id
        else _reference("WID", wid)
    )

    try:
        connection.limiter.wait()
        response = getattr(connection.service, spec["get"])(
            Request_References={spec["reference"]: [ref]},
            Response_Group={"Include_Reference": True},
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
    items = (data.get("Response_Data") or {}).get(spec["collection"]) or []
    if not items:
        return LookupResult(
            outcome=LookupOutcome.NOT_FOUND, reference_id=reference_id, wid=wid
        )

    item = items[0]
    ids = ids_of(item.get(spec["reference"]))
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        data=item,
        wid=ids.get("WID"),
        reference_id=ids.get(spec["id_type"]) or reference_id,
    )


def lookup_analytic_indicator(
    connection: Connection,
    *,
    reference_id: str | None = None,
    wid: str | None = None,
) -> LookupResult:
    """Fetch one analytic indicator by WID or ``Analytic_Indicator_ID``.

    Prefer the WID: it is the stable identity across tenants here, unusually,
    while the business id is tenant-specific.
    """
    if bool(reference_id) == bool(wid):
        raise ValueError("Pass exactly one of reference_id or wid.")

    ref = (
        _reference("Analytic_Indicator_ID", reference_id)
        if reference_id
        else _reference("WID", wid)
    )

    try:
        connection.limiter.wait()
        response = connection.service.Get_Analytic_Indicators(
            Request_References={"Analytic_Indicator_Reference": [ref]},
            Response_Group={"Include_Reference": True},
        )
    except Exception as exc:  # noqa: BLE001
        message = connection.redact(str(exc))
        return LookupResult(
            outcome=classify_fault(message),
            reference_id=reference_id,
            wid=wid,
            fault=message,
        )

    data = serialize_object(response)
    items = (data.get("Response_Data") or {}).get("Analytic_Indicator") or []
    if not items:
        return LookupResult(
            outcome=LookupOutcome.NOT_FOUND, reference_id=reference_id, wid=wid
        )

    item = items[0]
    ids = ids_of(item.get("Analytic_Indicator_Reference"))
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        data=item,
        wid=ids.get("WID"),
        reference_id=ids.get("Analytic_Indicator_ID") or reference_id,
    )


def lookup_gauge_range(
    connection: Connection,
    *,
    reference_id: str | None = None,
    wid: str | None = None,
) -> LookupResult:
    """Fetch one gauge range by ``Custom_Analytic_Range_ID`` or WID.

    The business ID lives inside ``Gauge_Range_Data.ID`` as well as on the
    reference, so a created range carries the same ID into the destination and
    stays findable by it afterwards.
    """
    if bool(reference_id) == bool(wid):
        raise ValueError("Pass exactly one of reference_id or wid.")

    ref = (
        _reference("Custom_Analytic_Range_ID", reference_id)
        if reference_id
        else _reference("WID", wid)
    )

    try:
        connection.limiter.wait()
        response = connection.service.Get_Gauge_Ranges(
            Request_References={"Gauge_Range_Reference": [ref]},
        )
    except Exception as exc:  # noqa: BLE001
        message = connection.redact(str(exc))
        return LookupResult(
            outcome=classify_fault(message),
            reference_id=reference_id,
            wid=wid,
            fault=message,
        )

    data = serialize_object(response)
    items = (data.get("Response_Data") or {}).get("Gauge_Range") or []
    if not items:
        return LookupResult(
            outcome=LookupOutcome.NOT_FOUND, reference_id=reference_id, wid=wid
        )

    item = items[0]
    ids = ids_of(item.get("Gauge_Range_Reference"))
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        data=item,
        wid=ids.get("WID"),
        reference_id=ids.get("Custom_Analytic_Range_ID") or reference_id,
    )


def lookup_prompt_field(
    connection: Connection,
    *,
    reference_id: str | None = None,
    wid: str | None = None,
) -> LookupResult:
    """Fetch one prompt field by ``TenantedExternalParameter`` or WID.

    Both work as cross-tenant identities here, unusually. A prompt field shared
    by two tenants keeps the *same WID* in both — all 13 of dpt5's appear in
    dpt1 under identical WIDs (confirmed live 2026-08-12) — so there is none of
    the shape-matching the calculated-field and measure probes need.

    Requires an implementer account.
    """
    if bool(reference_id) == bool(wid):
        raise ValueError("Pass exactly one of reference_id or wid.")

    ref = (
        _reference("TenantedExternalParameter", reference_id)
        if reference_id
        else _reference("WID", wid)
    )

    try:
        connection.limiter.wait()
        response = connection.service.Get_Prompt_Fields(
            Request_References={"Prompt_Field_Reference": [ref]},
            Response_Group={"Include_Reference": True},
        )
    except Exception as exc:  # noqa: BLE001
        message = connection.redact(str(exc))
        return LookupResult(
            outcome=classify_fault(message),
            reference_id=reference_id,
            wid=wid,
            fault=message,
        )

    data = serialize_object(response)
    items = (data.get("Response_Data") or {}).get("Prompt_Field") or []
    if not items:
        return LookupResult(
            outcome=LookupOutcome.NOT_FOUND, reference_id=reference_id, wid=wid
        )

    item = items[0]
    ids = ids_of(item.get("Prompt_Field_Reference"))
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        data=item,
        wid=ids.get("WID"),
        reference_id=ids.get("TenantedExternalParameter") or reference_id,
    )


def lookup_prompt_set(
    connection: Connection,
    *,
    reference_id: str | None = None,
    wid: str | None = None,
) -> LookupResult:
    """Fetch one prompt set by ``Prompt_Set_ID`` or WID.

    ``Prompt_Set_ID`` is the prompt set's own name (`'Company'`, `'Start and End
    Dates'`), which makes it a usable cross-tenant identity.
    """
    if bool(reference_id) == bool(wid):
        raise ValueError("Pass exactly one of reference_id or wid.")

    ref = (
        _reference("Prompt_Set_ID", reference_id)
        if reference_id
        else _reference("WID", wid)
    )

    try:
        connection.limiter.wait()
        response = connection.service.Get_Prompt_Sets(
            Request_References={"Prompt_Set_Reference": [ref]},
            Response_Group={"Include_Reference": True},
        )
    except Exception as exc:  # noqa: BLE001
        message = connection.redact(str(exc))
        return LookupResult(
            outcome=classify_fault(message),
            reference_id=reference_id,
            wid=wid,
            fault=message,
        )

    data = serialize_object(response)
    items = (data.get("Response_Data") or {}).get("Prompt_Set") or []
    if not items:
        return LookupResult(
            outcome=LookupOutcome.NOT_FOUND, reference_id=reference_id, wid=wid
        )

    item = items[0]
    ids = ids_of(item.get("Prompt_Set_Reference"))
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        data=item,
        wid=ids.get("WID"),
        reference_id=ids.get("Prompt_Set_ID") or reference_id,
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
    """Look a report up by name, for selection.

    ``Report_Name`` is **exact match** — a substring finds nothing. Use the
    index for anything resembling a search. Returns the first match; use
    :func:`lookup_report_by_name` when ambiguity matters.
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


def lookup_report_by_name(connection: Connection, name: str) -> LookupResult:
    """Existence check for a report, matched on its exact name.

    Name is used because **``Custom_Report_ID`` does not work as a lookup key**
    on this tenant. The API returns it on every report reference, but feeding it
    back to ``Request_References`` fails with "is not a valid ID value for type
    = 'Custom_Report_ID'" — verified against 18 of 18 sampled reports, including
    ones whose names contain no special characters. WID is not an option either,
    since a source WID means nothing in another tenant. That leaves the name.

    Name is a weaker identity than a stable ID, and it is not unique: 7 of 999
    sampled reports shared a name with another. A duplicate therefore returns
    ``UNKNOWN`` rather than a guess — picking the wrong one would overwrite an
    unrelated report, and there is no delete operation to recover from that.
    """
    if not name:
        return LookupResult(
            outcome=LookupOutcome.UNKNOWN,
            fault="Report has no name, so it cannot be matched by name.",
        )

    try:
        connection.limiter.wait()
        response = connection.service.Get_Tenanted_Report_Definitions(
            Request_Criteria={"Report_Name": name},
            Response_Filter={"Page": 1, "Count": 5},
            Response_Group={
                "Include_Reference": True,
                "Include_Tenanted_Report_Definition_Data": False,
            },
        )
    except Exception as exc:  # noqa: BLE001
        message = connection.redact(str(exc))
        return LookupResult(outcome=classify_fault(message), fault=message)

    data = serialize_object(response)
    results = data.get("Response_Results") or {}
    total = int(results.get("Total_Results") or 0)
    items = (data.get("Response_Data") or {}).get("Tenanted_Report_Definition") or []

    if total == 0:
        return LookupResult(outcome=LookupOutcome.NOT_FOUND)

    if total > 1:
        return LookupResult(
            outcome=LookupOutcome.UNKNOWN,
            fault=(
                f"{total} reports in the destination are named {name!r}. Cannot "
                "tell which one this is, and overwriting the wrong report "
                "cannot be undone."
            ),
        )

    ids = ids_of(items[0].get("Tenanted_Report_Definition_Reference")) if items else {}
    return LookupResult(
        outcome=LookupOutcome.FOUND,
        data=items[0] if items else None,
        wid=ids.get("WID"),
        reference_id=ids.get("Custom_Report_ID"),
    )


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
    collection_key: str,
    summarise: Callable[[dict], Any],
    page_size: int,
    response_group: dict | None = None,
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
        # Get_Gauge_Ranges has no Response_Group at all in the WSDL, and zeep
        # rejects the keyword outright rather than ignoring it.
        kwargs = {"Response_Filter": {"Page": page, "Count": page_size}}
        if response_group is not None:
            kwargs["Response_Group"] = response_group
        response = operation(**kwargs)
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


def _dashboard_summary(item: dict, *, tabbed: bool) -> DashboardSummary | None:
    spec = DASHBOARD_FLAVOURS[tabbed]
    ids = ids_of(item.get(spec["reference"]))
    wid = ids.get("WID")
    if not wid:
        return None
    data = item.get(spec["data"]) or {}
    worklets = data.get("Worklets_Data") or data.get("Content_Data") or []
    if isinstance(worklets, dict):
        worklets = [worklets]
    return DashboardSummary(
        wid=wid,
        reference_id=ids.get(spec["id_type"]),
        name=data.get("Name"),
        tabbed=tabbed,
        worklet_count=len(worklets),
    )


def _calculated_measure_summary(item: dict) -> CalculatedMeasureSummary | None:
    ids = ids_of(item.get("Calculated_Measure_Reference"))
    wid = ids.get("WID")
    if not wid:
        return None
    data = calculated_measure_data(item)
    return CalculatedMeasureSummary(
        wid=wid,
        reference_id=ids.get("BI_Calculated_Measure_ID"),
        name=data.get("Name"),
        business_object_wid=ids_of(data.get("Business_Object_Reference")).get("WID"),
    )


def _analytic_indicator_summary(item: dict) -> AnalyticIndicatorSummary | None:
    ids = ids_of(item.get("Analytic_Indicator_Reference"))
    wid = ids.get("WID")
    if not wid:
        return None
    data = item.get("Analytic_Indicator_Data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return AnalyticIndicatorSummary(
        wid=wid,
        reference_id=ids.get("Analytic_Indicator_ID"),
        name=(data or {}).get("Name"),
    )


def _gauge_range_summary(item: dict) -> GaugeRangeSummary | None:
    ids = ids_of(item.get("Gauge_Range_Reference"))
    wid = ids.get("WID")
    if not wid:
        return None
    data = item.get("Gauge_Range_Data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return GaugeRangeSummary(
        wid=wid,
        reference_id=ids.get("Custom_Analytic_Range_ID"),
        name=(data or {}).get("Name"),
    )


def _prompt_field_summary(item: dict) -> PromptFieldSummary | None:
    ids = ids_of(item.get("Prompt_Field_Reference"))
    wid = ids.get("WID")
    if not wid:
        return None
    data = item.get("Prompt_Field_Data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return PromptFieldSummary(
        wid=wid,
        reference_id=ids.get("TenantedExternalParameter"),
        name=(data or {}).get("Name"),
    )


def _prompt_set_summary(item: dict) -> PromptSetSummary | None:
    ids = ids_of(item.get("Prompt_Set_Reference"))
    wid = ids.get("WID")
    if not wid:
        return None
    data = item.get("Prompt_Set_Data") or {}
    members = data.get("Tenanted_Prompt_Set_Member_Data") or []
    if isinstance(members, dict):
        members = [members]
    return PromptSetSummary(
        wid=wid,
        reference_id=ids.get("Prompt_Set_ID"),
        name=data.get("Name"),
        member_count=len(members),
    )


def iter_dashboard_index(
    connection: Connection, *, page_size: int = PAGE_SIZE
) -> Iterator[IndexProgress]:
    """Sweep every custom dashboard, both flavours, into one index.

    Both operations are swept because nothing identifies a dashboard's flavour
    ahead of time, and the two ID spaces are disjoint. Cheap enough that this is
    not a compromise: measured live on `commitconsulting_dpt1`, 52 untabbed and
    127 tabbed dashboards, **one page each**, and the sweep carries full data
    with only ``Include_Reference`` set — there is no ``Include_..._Data`` flag
    on either Response_Group, and none is needed.

    Requires an implementer account. A non-implementer gets "The task submitted
    is not authorized" from both operations; see :data:`IMPLEMENTER_REQUIRED_REMEDY`.
    """
    started = time.monotonic()
    index = Index(kind="dashboard", tenant=connection.target.tenant, fetched_at=time.time())

    # Totals are only known after the first call of each flavour, so the
    # progress fraction is against the running sum rather than a figure known
    # up front. Both are single-page in practice.
    grand_total = 0
    for position, tabbed in enumerate(sorted(DASHBOARD_FLAVOURS)):
        spec = DASHBOARD_FLAVOURS[tabbed]
        page = 1
        while True:
            connection.limiter.wait()
            response = getattr(connection.service, spec["get"])(
                Response_Filter={"Page": page, "Count": page_size},
                Response_Group={"Include_Reference": True},
            )
            data = serialize_object(response)
            results = data.get("Response_Results") or {}
            total_pages = max(1, int(results.get("Total_Pages") or 1))
            if page == 1:
                grand_total += int(results.get("Total_Results") or 0)

            for item in (data.get("Response_Data") or {}).get(spec["collection"]) or []:
                summary = _dashboard_summary(item, tabbed=tabbed)
                if summary is None:
                    continue
                index.summaries[summary.wid] = summary
                index.payloads[summary.wid] = item

            last_flavour = position == len(DASHBOARD_FLAVOURS) - 1
            final = page >= total_pages and last_flavour
            yield IndexProgress(
                page=page,
                total_pages=total_pages,
                fetched=len(index.summaries),
                total=grand_total,
                elapsed=time.monotonic() - started,
                index=index,
                complete=final,
            )
            if page >= total_pages:
                break
            page += 1


def iter_calculated_measure_index(
    connection: Connection, *, page_size: int = PAGE_SIZE
) -> Iterator[IndexProgress]:
    """Sweep every calculated measure. One page, 47-55 items on these tenants.

    Measures are deliberately *not* indexed for dependency resolution — see
    :func:`lookup_calculated_measure`, which fetches them one at a time because
    a report reaches only a handful and an on-demand lookup cannot go stale
    mid-run. That reasoning still holds.

    This sweep exists for the opposite job: recognising a measure in the
    **destination** when its ``BI_Calculated_Measure_ID`` does not match. It
    cannot match, in fact — confirmed live 2026-08-12, dpt5's IDs are Workday-
    generated with tenant-local sequence numbers
    (``ARITHMETIC_CALCULATED_MEASURE-11-210``) while dpt1's come from an earlier
    migration (``CRTMNU01_Commit - HR Dashboard_08_Annual - Turnover %``). Two
    tenants can never agree on those, so matching needs the whole destination
    set. See :func:`calculated_measure_match_index`.
    """
    return _iter_index(
        connection,
        kind="calculated_measure",
        operation_name="Get_Calculated_Measures",
        response_group={
            "Include_Reference": True,
            "Include_Calculated_Measure_Data": True,
        },
        collection_key="Calculated_Measure",
        summarise=_calculated_measure_summary,
        page_size=page_size,
    )


def iter_analytic_indicator_index(
    connection: Connection, *, page_size: int = PAGE_SIZE
) -> Iterator[IndexProgress]:
    """Sweep every analytic indicator. One page: 339 on dpt1, 318 on dpt5.

    **Their WIDs are stable across tenants** — all 318 of dpt5 appear in dpt1
    under identical WIDs (confirmed live 2026-08-12) — while their business ids
    are not: the same indicator is ``CRTMNU01_Commit - HR Dashboard_09_Retention
    Risk Indicator`` on one and ``Worker-Retention-Retention Risk Indicator`` on
    the other. So a shared indicator resolves by WID with no help at all, and
    this sweep exists for the 21 that dpt1 has and dpt5 does not.

    A reference naming an indicator that is in *neither* sweep is a pointer
    dangling in the source itself. Those cannot be migrated and must not block
    the run — see ``Closure.unmigratable_indicator_wids``.
    """
    return _iter_index(
        connection,
        kind="analytic_indicator",
        operation_name="Get_Analytic_Indicators",
        response_group={"Include_Reference": True},
        collection_key="Analytic_Indicator",
        summarise=_analytic_indicator_summary,
        page_size=page_size,
    )


def iter_gauge_range_index(
    connection: Connection, *, page_size: int = PAGE_SIZE
) -> Iterator[IndexProgress]:
    """Sweep every gauge range (custom analytic range). One page: 14 on dpt1,
    10 on dpt5.

    A report's gauge layout points at one through ``Analytic_Range_Reference``,
    and the report cannot be written until it exists — confirmed live
    2026-08-12, ``Put_Tenanted_Report_Definition`` failed on
    ``'80605873bea110011df7b34ea3060000' is not a valid ID value for type =
    'WID'`` for `Benefits - OE Submission %`.

    ``Get_Gauge_Ranges`` has **no Response_Group** in the WSDL — unusually — and
    returns ``Gauge_Range_Data`` regardless.
    """
    return _iter_index(
        connection,
        kind="gauge_range",
        operation_name="Get_Gauge_Ranges",
        collection_key="Gauge_Range",
        summarise=_gauge_range_summary,
        page_size=page_size,
    )


def iter_prompt_field_index(
    connection: Connection, *, page_size: int = PAGE_SIZE
) -> Iterator[IndexProgress]:
    """Sweep every prompt field (tenanted external parameter). One page.

    A prompt set's members point at these through
    ``Abstract_External_Parameter_Reference``, and a prompt set cannot be
    written until they exist — confirmed live 2026-08-12, ``Put_Prompt_Set``
    fails with ``Invalid ID value ... for type = 'WID'`` naming one.

    **Unlike calculated fields and measures, these do not need cross-tenant
    shape matching.** A prompt field shared by two tenants has the *same WID* in
    both: all 13 of dpt5's appear in dpt1 under identical WIDs. So the ordinary
    delivered-vs-custom split applies — a WID that resolves in the destination
    passes through, one that does not has to be created.

    Requires an implementer account, like the dashboard operations.
    """
    return _iter_index(
        connection,
        kind="prompt_field",
        operation_name="Get_Prompt_Fields",
        response_group={"Include_Reference": True},
        collection_key="Prompt_Field",
        summarise=_prompt_field_summary,
        page_size=page_size,
    )


def iter_prompt_set_index(
    connection: Connection, *, page_size: int = PAGE_SIZE
) -> Iterator[IndexProgress]:
    """Sweep every prompt set. One page, ~57 items on the test tenant.

    Swept rather than fetched on demand because **the request criteria do not
    work**. ``Prompt_Set_Request_Criteria`` offers
    ``Tenanted_Report_Definition_Reference`` and ``Custom_Dashboard_Reference``,
    and neither is usable (both confirmed live 2026-08-07):

    - The report-scoped criteria is *accepted and ignored*. Scoping to three
      different reports returned all 57 prompt sets each time, with
      ``Total_Results=57`` — the tenant-wide figure.
    - The dashboard-scoped criteria is typed ``Custom_Landing_PageObjectType``,
      i.e. untabbed dashboards only, and rejects a tabbed dashboard's WID
      outright: "Invalid instance ... for Custom_Landing_Page_Request_References".

    So the dependency edge comes from the dashboard payload's own
    ``Prompt_Set_Reference``, resolved against this index.
    """
    return _iter_index(
        connection,
        kind="prompt_set",
        operation_name="Get_Prompt_Sets",
        response_group={"Include_Reference": True},
        collection_key="Prompt_Set",
        summarise=_prompt_set_summary,
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


#: Which summary dataclass a cached index rehydrates into, by ``kind``.
_SUMMARY_TYPES = {
    "calculated_field": CalculatedFieldSummary,
    "calculated_measure": CalculatedMeasureSummary,
    "prompt_field": PromptFieldSummary,
    "gauge_range": GaugeRangeSummary,
    "analytic_indicator": AnalyticIndicatorSummary,
    "report": ReportSummary,
    "dashboard": DashboardSummary,
    "prompt_set": PromptSetSummary,
}


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

    summary_type = _SUMMARY_TYPES.get(document.get("kind"), ReportSummary)
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
