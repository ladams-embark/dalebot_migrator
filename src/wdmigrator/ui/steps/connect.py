"""Step 1: Connect — source and destination tenant credentials.

Nothing downstream can start until both sides have a green connection test.
Same-tenant source/destination is *allowed* to pass this step — the engine
still hard-blocks a live write against it later (``safety.py``, no
override), and blocking it here too would make dry-running against a single
tenant impossible, which is the only way to exercise the whole flow before a
real destination tenant exists.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import (
    DEFAULT_SERVICE_NAME,
    DEFAULT_VERSION,
    AuthError,
    Blocker,
    Role,
    TenantURLError,
    connect,
    install_redacting_log_filter,
    iter_discover_services_host,
    parse_tenant_url,
    verify_connection,
)
from wdmigrator.ui import secrets as secrets_ui
from wdmigrator.ui import theme
from wdmigrator.ui.components import render_connection_status, render_target_card
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import ConnectionState, WizardState, reset_downstream

STEP_ID = "connect"

# Common-case quick fill. The tenant ID itself isn't a credential (it's
# already throughout this repo's docs and tests) — never extend this to
# username/password, which must always be typed, never prefilled.
_QUICK_FILL_TENANT = "commitconsulting_dpt1"
_QUICK_FILL_SERVICES_HOST = "impl-services1.wd12.myworkday.com"


def _attempt_connect(state: WizardState, side: ConnectionState, role: Role, label: str) -> None:
    # Installed before anything else touches the tenant, so even an auth
    # failure's error text — which can echo back parts of the request — gets
    # redacted rather than risking a cleartext password in a traceback.
    install_redacting_log_filter(state.source.password, state.dest.password)

    try:
        target = parse_tenant_url(side.target_raw)
    except TenantURLError as exc:
        theme.banner("danger", f"Could not parse the {label.lower()} tenant URL", str(exc))
        side.target = None
        return

    side.target = target

    if not side.username or not side.password:
        theme.banner("danger", f"{label}: credentials incomplete",
                     "Username and password are both required.")
        return

    try:
        connection = connect(target, side.username, side.password, role=role)
    except AuthError as exc:
        theme.banner("danger", f"{label}: could not build a connection", str(exc))
        return

    status = verify_connection(connection)

    if status.ok and side.verified_fingerprint and status.fingerprint != side.verified_fingerprint:
        # A previously-verified side just reconnected as a different
        # tenant/user. Everything built from it (indexes, closure, plan,
        # dry run, guard) may no longer be valid.
        reset_downstream(state, from_step="select")
        theme.banner(
            "info",
            f"{label} credentials changed",
            "This side now points at a different tenant or user, so the selections, "
            "closure, and plan built from the old one were cleared.",
        )

    side.status = status
    if status.ok:
        side.connection = connection
        side.verified_fingerprint = status.fingerprint
    else:
        side.connection = None


def _run_discovery(side: ConnectionState) -> None:
    tenant_id = side.discovery_tenant_id.strip()
    side.discovery_job = start_job(iter_discover_services_host(tenant_id))
    side.discovery_expanded = True


def _pump_discovery(side: ConnectionState, *, target_widget_key: str) -> None:
    job = side.discovery_job
    pump(job, time_budget=0.8)

    for event in job.events:
        outcome = "found" if event.ok else "no match"
        st.caption(f"`{event.data_center}` ({event.services_host}) — {outcome}: {event.detail}")

    if job.error is not None:
        theme.banner("danger", "Discovery failed", str(job.error))
        side.discovery_job = None
        return

    if not job.done:
        st.rerun()
        return

    found = next((e for e in job.events if e.ok), None)
    side.discovery_job = None
    if found is None:
        theme.banner(
            "danger",
            "Not found in any known data center",
            "This tenant's data center may not be one this tool knows about yet.",
            remedy="Add it to KNOWN_IMPL_DATA_CENTERS in auth/endpoint_discovery.py, "
                   "or paste the full services-host URL below instead.",
        )
        return

    tenant_id = side.discovery_tenant_id.strip()
    side.target_raw = (
        f"https://{found.services_host}/ccx/service/"
        f"{tenant_id}/{DEFAULT_SERVICE_NAME}/{DEFAULT_VERSION}"
    )
    # Once a widget's key exists in session_state, passing a different
    # value= on a later st.text_input(...) call is silently ignored —
    # session_state[key] is what actually drives the displayed value from
    # here on. Writing it directly is the only way to update the field
    # programmatically after the user has interacted with it once.
    st.session_state[target_widget_key] = side.target_raw
    # Cheap and offline — parse it immediately so the target card shows
    # something before the user has even entered credentials to test with.
    try:
        side.target = parse_tenant_url(side.target_raw)
    except TenantURLError:
        side.target = None
    theme.banner(
        "success",
        f"Found on {found.data_center}",
        f"{found.services_host} — filled in below.",
    )
    st.rerun()


def _quick_fill(side: ConnectionState, *, target_widget_key: str) -> None:
    side.target_raw = (
        f"https://{_QUICK_FILL_SERVICES_HOST}/ccx/service/"
        f"{_QUICK_FILL_TENANT}/{DEFAULT_SERVICE_NAME}/{DEFAULT_VERSION}"
    )
    # See the same fix in _pump_discovery — a widget's session_state entry
    # overrides value= once the widget has been rendered once.
    st.session_state[target_widget_key] = side.target_raw
    try:
        side.target = parse_tenant_url(side.target_raw)
    except TenantURLError:
        side.target = None


def _render_side(state: WizardState, side: ConnectionState, role: Role, label: str, key: str) -> None:
    theme.section(
        label,
        eyebrow="Reads from" if role is Role.SOURCE else "Writes to",
    )

    if st.button(f"Quick fill: {_QUICK_FILL_TENANT}", key=f"{key}_quick_fill"):
        _quick_fill(side, target_widget_key=f"{key}_target")
        st.rerun()

    with st.expander(
        "Don't know the services host? Find it from just the tenant ID",
        expanded=side.discovery_expanded,
    ):
        side.discovery_tenant_id = st.text_input(
            "Tenant ID",
            value=side.discovery_tenant_id,
            key=f"{key}_discover_tenant",
            help="Just the tenant ID, e.g. commitconsulting_dpt1 — not a URL.",
        )
        if side.discovery_job is None:
            if st.button("Find services host", key=f"{key}_discover_btn") and side.discovery_tenant_id.strip():
                _run_discovery(side)
                st.rerun()
        else:
            _pump_discovery(side, target_widget_key=f"{key}_target")

    side.target_raw = st.text_input(
        f"{label} tenant URL",
        value=side.target_raw,
        key=f"{key}_target",
        placeholder="https://impl.wd12.myworkday.com/commitconsulting_dpt1/...",
        help="A pasted browser URL, or a bare tenant name.",
    )
    side.username = secrets_ui.username_input(
        f"{label} ISU username",
        key=f"{key}_user",
        value=side.username,
        help=(
            "Just the ISU name — the tenant is appended for you. An email "
            "address works too. If it is already written as `name@tenant`, "
            "that is accepted as-is rather than suffixed twice."
        ),
    )
    side.password = secrets_ui.password_input(f"{label} ISU password", key=f"{key}_pass")

    if st.button(f"Test {label.lower()} connection", key=f"{key}_test"):
        _attempt_connect(state, side, role, label)

    render_target_card(label, side.target)
    render_connection_status(side.status)


def render(state: WizardState) -> None:
    st.header("Connect")
    theme.checklist(
        [
            "Both sides need a verified connection before you can pick anything to migrate.",
            "Both ISUs need Get and Put on the Configuration Set: Custom Reports and Fields "
            "domain. Special OX Web Services access is not required.",
            "After any permission change in Workday, run Activate Pending Security Policy "
            "Changes — until you do, the ISU returns empty data rather than an error.",
        ]
    )

    col1, col2 = st.columns(2)
    with col1:
        _render_side(state, state.source, Role.SOURCE, "Source", "src")
    with col2:
        _render_side(state, state.dest, Role.DESTINATION, "Destination", "dst")

    if state.source.target is not None and state.dest.target is not None:
        if state.source.target.identity() == state.dest.target.identity():
            theme.banner(
                "warning",
                "Source and destination are the same tenant",
                "Dry runs work fine against a single tenant — that's how the whole flow "
                "gets exercised without a second one.",
                remedy="A live migration will be blocked here with no override available.",
            )


def gate(state: WizardState) -> list[Blocker]:
    blockers = []
    if not state.source.verified:
        blockers.append(
            Blocker(
                node_id=None,
                title="Source not connected",
                detail="The source tenant connection has not been tested successfully.",
                remedy="Enter source credentials and click Test source connection.",
            )
        )
    if not state.dest.verified:
        blockers.append(
            Blocker(
                node_id=None,
                title="Destination not connected",
                detail="The destination tenant connection has not been tested successfully.",
                remedy="Enter destination credentials and click Test destination connection.",
            )
        )
    return blockers
