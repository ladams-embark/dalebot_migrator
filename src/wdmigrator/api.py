"""The UI's only door into the engine.

Everything a Streamlit page (or a future CLI) needs — connecting, indexing,
resolving dependencies, planning, and writing — is reachable from this one
module. That is a boundary, not a convenience: it means there is exactly one
place to check "does anything here import streamlit or pandas", and exactly
one surface to review when reasoning about what the UI can and cannot do.

Two rules this module exists to enforce:

**Nothing below `wdmigrator/` except `ui/` may import streamlit or pandas.**
`tests/test_api.py` checks this module specifically; the engine subpackages it
wraps (`auth`, `discovery`, `migrate`, `config`, `safety`, `secrets`) have never
imported either.

**Every long-running operation is a generator yielding progress events, not a
blocking call with a callback.** That is what makes cancellable progress work
under Streamlit's rerun model — a caller drains the generator one step per
rerun — and it costs nothing for a CLI, which can just drain it in a loop.
:func:`resolve` is the one exception, and deliberately not a generator: it
makes zero tenant calls (see its docstring), so there is nothing to report
progress on.

Most of what follows is a thin, curated re-export of the engine modules under
their existing names — renaming a function that is already well-named would
just be a second name for one thing. The exceptions are :func:`connect`, which
hides the ``Secret`` wrapping of a password behind plain strings so the UI
layer never has to import ``wdmigrator.secrets`` to authenticate, and
:func:`resolve`, a thin alias for `resolve_closure` documenting why it is
synchronous.
"""

from __future__ import annotations

from typing import Mapping

# ── Targeting: which tenant, how dangerous ──────────────────────────────────
from wdmigrator.config import (
    Environment,
    TenantTarget,
    TenantURLError,
    classify_environment,
    derive_services_host,
    parse_tenant_url,
    target_from_parts,
)

# ── Authentication: building a pinned, authenticated client ─────────────────
from wdmigrator.auth import (
    DEFAULT_SERVICE_NAME,
    DEFAULT_VERSION,
    AuthError,
    Connection,
    ConnectionStatus,
    Credentials,
    Role,
    make_client,
    verify_connection,
)

# ── Endpoint discovery: finding a services host from just a tenant ID ───────
from wdmigrator.auth import (
    DataCenter,
    DiscoveryAttempt,
    DiscoveryResult,
    EndpointNotFoundError,
    KNOWN_IMPL_DATA_CENTERS,
    discover_services_host,
    iter_discover_services_host,
)

# ── Discovery: indexing and targeted lookups ────────────────────────────────
from wdmigrator.discovery import (
    DASHBOARD_FLAVOURS,
    IMPLEMENTER_REQUIRED_REMEDY,
    PAGE_SIZE,
    CalculatedFieldSummary,
    CalculatedMeasureSummary,
    DashboardSummary,
    Index,
    IndexProgress,
    LookupOutcome,
    LookupResult,
    GaugeRangeSummary,
    PromptFieldSummary,
    PromptSetSummary,
    ReportSummary,
    build_index,
    cache_path,
    CalculatedFieldMatchIndex,
    calculated_field_data,
    calculated_field_shape,
    calculated_measure_data,
    calculated_measure_match_index,
    calculated_measure_shape,
    calculated_field_match_index,
    classify_fault,
    find_report_by_exact_name,
    ids_of,
    iter_calculated_field_index,
    iter_calculated_measure_index,
    iter_dashboard_index,
    iter_gauge_range_index,
    iter_prompt_field_index,
    iter_prompt_set_index,
    iter_report_index,
    load_index,
    lookup_calculated_field,
    lookup_calculated_measure,
    lookup_dashboard,
    lookup_gauge_range,
    lookup_prompt_field,
    lookup_prompt_set,
    lookup_report,
    lookup_report_by_name,
    requires_implementer,
    save_index,
)

# ── Dependency resolution, ordering, planning, writing ──────────────────────
from wdmigrator.migrate import (
    DASHBOARD_KINDS,
    DASHBOARD_TABBED_BY_KIND,
    Action,
    Blocker,
    Closure,
    CycleError,
    ExceptionDetail,
    Existence,
    MigrationPlan,
    Node,
    NodeKind,
    PartialIndexError,
    ProbeProgress,
    BlockingReference,
    ReferenceAction,
    ReferenceDecision,
    ReferenceSite,
    WriteError,
    WriteProgress,
    WriteRecord,
    WriteStatus,
    build_calculated_field_payload,
    build_calculated_measure_payload,
    build_dashboard_payload,
    build_owner_reference,
    build_plan,
    build_gauge_range_payload,
    build_prompt_field_payload,
    build_prompt_set_payload,
    build_report_payload,
    default_action,
    extract_exceptions,
    find_reference_sites,
    parse_blocking_reference,
    extract_dashboard_refs,
    extract_measure_refs,
    extract_gauge_range_refs,
    extract_prompt_field_refs,
    extract_prompt_set_refs,
    extract_reference_id_refs,
    extract_report_refs,
    extract_wid_refs,
    is_dashboard_worklet,
    is_failure,
    iter_check_existence,
    iter_execute,
    node_id_for,
    operation_for,
    resolve_closure,
    serialize_envelope,
    substitute_wids,
    summarise,
    topological_sort,
    unmapped_wids,
    validate_plan,
    write_node,
)

# ── Safety: the gate between a plan and a write ─────────────────────────────
from wdmigrator.safety import (
    ALLOW_NON_IMPL_ENV_VAR,
    Guard,
    GuardViolation,
    Level,
    WriteGuard,
    assert_write_allowed,
    blocking_guards,
    evaluate_guards,
    non_impl_override_enabled,
)

# ── Secrets: keeping credentials out of logs, envelopes, and errors ─────────
from wdmigrator.secrets import Secret, install_redacting_log_filter, redact, redact_envelope

__all__ = [
    # targeting
    "Environment",
    "TenantTarget",
    "TenantURLError",
    "classify_environment",
    "derive_services_host",
    "parse_tenant_url",
    "target_from_parts",
    # auth
    "AuthError",
    "Connection",
    "ConnectionStatus",
    "Credentials",
    "Role",
    "DEFAULT_SERVICE_NAME",
    "DEFAULT_VERSION",
    "connect",
    "make_client",
    "verify_connection",
    # endpoint discovery
    "DataCenter",
    "DiscoveryAttempt",
    "DiscoveryResult",
    "EndpointNotFoundError",
    "KNOWN_IMPL_DATA_CENTERS",
    "discover_services_host",
    "iter_discover_services_host",
    # discovery
    "DASHBOARD_FLAVOURS",
    "IMPLEMENTER_REQUIRED_REMEDY",
    "PAGE_SIZE",
    "CalculatedFieldSummary",
    "CalculatedMeasureSummary",
    "DashboardSummary",
    "Index",
    "IndexProgress",
    "LookupOutcome",
    "LookupResult",
    "GaugeRangeSummary",
    "PromptFieldSummary",
    "PromptSetSummary",
    "ReportSummary",
    "build_index",
    "cache_path",
    "CalculatedFieldMatchIndex",
    "calculated_field_data",
    "calculated_field_shape",
    "calculated_measure_data",
    "calculated_measure_match_index",
    "calculated_measure_shape",
    "calculated_field_match_index",
    "classify_fault",
    "find_report_by_exact_name",
    "ids_of",
    "iter_calculated_field_index",
    "iter_calculated_measure_index",
    "iter_dashboard_index",
    "iter_gauge_range_index",
    "iter_prompt_field_index",
    "iter_prompt_set_index",
    "iter_report_index",
    "load_index",
    "lookup_calculated_field",
    "lookup_calculated_measure",
    "lookup_dashboard",
    "lookup_gauge_range",
    "lookup_prompt_field",
    "lookup_prompt_set",
    "measure_loader_for",
    "report_loader_for",
    "lookup_report",
    "lookup_report_by_name",
    "requires_implementer",
    "save_index",
    # resolution / ordering / planning / writing
    "DASHBOARD_KINDS",
    "DASHBOARD_TABBED_BY_KIND",
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
    "BlockingReference",
    "ReferenceAction",
    "ReferenceDecision",
    "ReferenceSite",
    "WriteError",
    "WriteProgress",
    "WriteRecord",
    "WriteStatus",
    "build_calculated_field_payload",
    "build_calculated_measure_payload",
    "build_dashboard_payload",
    "build_owner_reference",
    "build_plan",
    "build_gauge_range_payload",
    "build_prompt_field_payload",
    "build_prompt_set_payload",
    "build_report_payload",
    "default_action",
    "extract_exceptions",
    "find_reference_sites",
    "parse_blocking_reference",
    "extract_dashboard_refs",
    "extract_measure_refs",
    "extract_gauge_range_refs",
    "extract_prompt_field_refs",
    "extract_prompt_set_refs",
    "extract_reference_id_refs",
    "extract_report_refs",
    "extract_wid_refs",
    "is_dashboard_worklet",
    "is_failure",
    "iter_check_existence",
    "iter_execute",
    "node_id_for",
    "operation_for",
    "resolve",
    "resolve_closure",
    "serialize_envelope",
    "substitute_wids",
    "summarise",
    "topological_sort",
    "unmapped_wids",
    "validate_plan",
    "write_node",
    # safety
    "ALLOW_NON_IMPL_ENV_VAR",
    "Guard",
    "GuardViolation",
    "Level",
    "WriteGuard",
    "assert_write_allowed",
    "blocking_guards",
    "evaluate_guards",
    "non_impl_override_enabled",
    # secrets
    "Secret",
    "install_redacting_log_filter",
    "redact",
    "redact_envelope",
]


# ── The two functions that are not straight re-exports ──────────────────────


def connect(
    target: TenantTarget,
    username: str,
    password: str,
    *,
    role: Role = Role.SOURCE,
    **kwargs,
) -> Connection:
    """Build an authenticated, endpoint-pinned client from plain strings.

    A UI text input and a password input hand you two strings, not a
    :class:`~wdmigrator.secrets.Secret`. Wrapping happens here so the UI layer
    never needs to import ``wdmigrator.secrets`` just to log in — it only needs
    ``Secret`` if it wants to redact something itself.

    ``**kwargs`` passes through to :func:`~wdmigrator.auth.make_client`
    (``service_name``, ``version``, ``wsdl_source``, ``timeout``,
    ``calls_per_second``).
    """
    credentials = Credentials(username=username, password=Secret(password))
    return make_client(target, credentials, role=role, **kwargs)


def measure_loader_for(connection: Connection):
    """A ``wid -> payload`` loader for calculated measures, backed by a tenant.

    Pass to :func:`resolve` to make reports pull in the calculated measures they
    use. Results are memoised for the life of the loader, including misses — a
    measure referenced by four columns is fetched once, and a measure that is
    not there is not re-requested on every reference.

    Measures are fetched rather than indexed on purpose: a tenant holds a
    handful, and they are only ever reached as a dependency of a report that
    uses one. See :func:`~wdmigrator.migrate.resolver.resolve_closure`.
    """
    cache: dict[str, dict | None] = {}

    def load(wid: str) -> dict | None:
        if wid not in cache:
            result = lookup_calculated_measure(connection, wid=wid)
            cache[wid] = result.data if result.outcome is LookupOutcome.FOUND else None
        return cache[wid]

    return load


def report_loader_for(connection: Connection):
    """A ``wid -> payload`` loader for sub-reports, backed by a tenant.

    Pass to :func:`resolve` so a composite report pulls in the sub-reports it
    renders. Memoised for the life of the loader, misses included.

    Fetched on demand rather than read from the report index for two reasons:
    the index is optional (it costs ~158s to build and a user may never have
    asked for one), and a sub-report reached this way needs its *full* payload,
    which a reference-only sweep does not carry.
    """
    cache: dict[str, dict | None] = {}

    def load(wid: str) -> dict | None:
        if wid not in cache:
            result = lookup_report(connection, wid=wid)
            cache[wid] = result.data if result.outcome is LookupOutcome.FOUND else None
        return cache[wid]

    return load


def resolve(
    cf_index: Index,
    *,
    selected_field_wids=(),
    selected_reports: Mapping[str, dict] | None = None,
    selected_dashboards: Mapping[str, dict] | None = None,
    expected_index_size: int | None = None,
    allow_partial_index: bool = False,
    measure_loader=None,
    report_loader=None,
    prompt_set_index: Index | None = None,
    prompt_field_index: Index | None = None,
    gauge_range_index: Index | None = None,
    dashboard_index: Index | None = None,
) -> Closure:
    """Expand a selection into the full set of objects that must migrate.

    Not a generator, unlike almost everything else in this module. ``cf_index``
    already holds every calculated field in the source tenant, so classifying a
    reference as "is this a calculated field" is a free set lookup rather than a
    tenant call.

    The one exception is ``measure_loader``. Calculated measures are not
    indexed, so if you pass a loader — see :func:`measure_loader_for` — this
    function will make one targeted call per distinct measure referenced. That
    is a handful of calls for a typical report, not a sweep, which is why it
    stays synchronous. Pass nothing and measures are skipped entirely, and
    resolution makes no tenant calls at all.

    Dashboards and prompt sets are resolved against indexes rather than loaders
    (their request criteria do not work — see :func:`iter_prompt_set_index`), so
    passing those keeps this function offline too.

    See :func:`~wdmigrator.migrate.resolver.resolve_closure` for the rest,
    including why a partial index is refused unless explicitly overridden.
    """
    return resolve_closure(
        cf_index=cf_index,
        selected_field_wids=selected_field_wids,
        selected_reports=selected_reports,
        selected_dashboards=selected_dashboards,
        expected_index_size=expected_index_size,
        allow_partial_index=allow_partial_index,
        measure_loader=measure_loader,
        report_loader=report_loader,
        prompt_set_index=prompt_set_index,
        prompt_field_index=prompt_field_index,
        gauge_range_index=gauge_range_index,
        dashboard_index=dashboard_index,
    )
