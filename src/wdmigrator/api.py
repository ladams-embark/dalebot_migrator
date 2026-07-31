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

from typing import Iterator, Mapping

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
    AuthError,
    Connection,
    ConnectionStatus,
    Credentials,
    Role,
    make_client,
    verify_connection,
)

# ── Discovery: indexing and targeted lookups ────────────────────────────────
from wdmigrator.discovery import (
    PAGE_SIZE,
    CalculatedFieldSummary,
    Index,
    IndexProgress,
    LookupOutcome,
    LookupResult,
    ReportSummary,
    build_index,
    cache_path,
    classify_fault,
    find_report_by_exact_name,
    ids_of,
    iter_calculated_field_index,
    iter_report_index,
    load_index,
    lookup_calculated_field,
    lookup_report,
    lookup_report_by_name,
    save_index,
)

# ── Dependency resolution, ordering, planning, writing ──────────────────────
from wdmigrator.migrate import (
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
    WriteError,
    WriteProgress,
    WriteRecord,
    WriteStatus,
    build_calculated_field_payload,
    build_owner_reference,
    build_plan,
    build_report_payload,
    default_action,
    extract_exceptions,
    extract_wid_refs,
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
    "connect",
    "make_client",
    "verify_connection",
    # discovery
    "PAGE_SIZE",
    "CalculatedFieldSummary",
    "Index",
    "IndexProgress",
    "LookupOutcome",
    "LookupResult",
    "ReportSummary",
    "build_index",
    "cache_path",
    "classify_fault",
    "find_report_by_exact_name",
    "ids_of",
    "iter_calculated_field_index",
    "iter_report_index",
    "load_index",
    "lookup_calculated_field",
    "lookup_report",
    "lookup_report_by_name",
    "save_index",
    # resolution / ordering / planning / writing
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
    "WriteError",
    "WriteProgress",
    "WriteRecord",
    "WriteStatus",
    "build_calculated_field_payload",
    "build_owner_reference",
    "build_plan",
    "build_report_payload",
    "default_action",
    "extract_exceptions",
    "extract_wid_refs",
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


def resolve(
    cf_index: Index,
    *,
    selected_field_wids=(),
    selected_reports: Mapping[str, dict] | None = None,
    expected_index_size: int | None = None,
    allow_partial_index: bool = False,
) -> Closure:
    """Expand a selection into the full set of objects that must migrate.

    Not a generator, unlike almost everything else in this module — and that
    is deliberate rather than an inconsistency. ``cf_index`` already holds
    every calculated field in the source tenant, so classifying a reference as
    "is this a calculated field" is a free set lookup, not a tenant call. There
    is nothing here that takes long enough to need progress reporting. See
    :func:`~wdmigrator.migrate.resolver.resolve_closure` for the full story,
    including why a partial index is refused unless explicitly overridden.
    """
    return resolve_closure(
        cf_index=cf_index,
        selected_field_wids=selected_field_wids,
        selected_reports=selected_reports,
        expected_index_size=expected_index_size,
        allow_partial_index=allow_partial_index,
    )
