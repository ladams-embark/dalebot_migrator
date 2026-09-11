"""Step 1: Connect — source and destination tenant credentials.

Nothing downstream can start until both sides have a green connection test.
Same-tenant source/destination is *allowed* to pass this step — the engine
still hard-blocks a live write against it later (``safety.py``, no
override), and blocking it here too would make dry-running against a single
tenant impossible, which is the only way to exercise the whole flow before a
real destination tenant exists.
"""

from __future__ import annotations

import os

import streamlit as st

from wdmigrator.api import (
    DEFAULT_SERVICE_NAME,
    DEFAULT_VERSION,
    AuthError,
    Blocker,
    PackageError,
    Role,
    TenantURLError,
    connect,
    default_packages_dir,
    install_redacting_log_filter,
    iter_discover_services_host,
    list_packages,
    load_package,
    parse_tenant_url,
    probe_capabilities,
    verify_connection,
)
from wdmigrator.ui import secrets as secrets_ui
from wdmigrator.ui import session_store, theme, workspace
from wdmigrator.ui.components import (
    render_capabilities,
    render_connection_status,
    render_target_card,
)
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import ConnectionState, WizardState, reset_downstream

STEP_ID = "connect"

#: Quick fill reads whatever tenant this operator put in ``.env`` for the side
#: being filled. It used to name one specific tenant as a module constant,
#: which shipped a prominent one-click button pointing the *write target* at
#: this project's own implementation tenant for anybody who was not this
#: project. Reading it per-side means the button can only ever offer the
#: tenant the operator already configured, and it disappears entirely when
#: they have not configured one.
_ENV_PREFIX = {Role.SOURCE: "WD_SOURCE", Role.DESTINATION: "WD_DEST"}


def _env_target(role: Role) -> tuple[str, str] | None:
    """``(tenant, services_host)`` from ``.env`` for this side, if both are set."""
    prefix = _ENV_PREFIX[role]
    tenant = os.environ.get(f"{prefix}_TENANT", "").strip()
    host = os.environ.get(f"{prefix}_SERVICES_HOST", "").strip()
    if tenant and host:
        return tenant, host
    return None


def _attempt_connect(state: WizardState, side: ConnectionState, role: Role, label: str) -> None:
    # Installed before anything else touches the tenant, so even an auth
    # failure's error text — which can echo back parts of the request — gets
    # redacted rather than risking a cleartext password in a traceback.
    install_redacting_log_filter(state.source.password, state.dest.password)

    side.quick_filled_pending_test = False

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
        # A source-side change also invalidates any stored package that was
        # loaded — the user is switching to a live source. A destination
        # change leaves the package alone, because the package is what the
        # source is.
        if role is Role.SOURCE:
            state.package = None
            state.closure = None
        theme.banner(
            "info",
            f"{label} credentials changed",
            "This side now points at a different tenant or user, so the selections, "
            "closure, and plan built from the old one were cleared.",
        )

    if status.ok and role is Role.SOURCE and state.restored_source_tenant:
        # A resumed session brought a selection of source WIDs with it. They
        # only mean anything in the tenant they were picked from.
        if target.tenant != state.restored_source_tenant:
            was = state.restored_source_tenant
            reset_downstream(state, from_step="select")
            theme.banner(
                "warning",
                "Resumed selection discarded",
                f"That session was saved against `{was}` and this source is "
                f"`{target.tenant}`. The object IDs in it do not refer to "
                "anything here, so the selection was cleared.",
                remedy="Pick objects again from this tenant's catalog on Select.",
            )
        state.restored_source_tenant = ""

    side.status = status
    if status.ok:
        side.connection = connection
        side.verified_fingerprint = status.fingerprint
        # One extra Get, ~0.2s, read-only. Answers the question that would
        # otherwise surface as a failed sweep on Select.
        side.capabilities = probe_capabilities(connection)
    else:
        side.connection = None
        side.capabilities = None


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


def _quick_fill(
    side: ConnectionState,
    role: Role,
    *,
    target_widget_key: str,
    user_widget_key: str,
    pass_widget_key: str,
) -> bool:
    """Fill this side's target URL, and any credentials found in .env for its
    role. Returns True if both username and password ended up populated —
    that's the caller's cue to auto-run the connection test.
    """
    env_target = _env_target(role)
    if env_target is None:
        return False
    tenant, host = env_target
    side.target_raw = (
        f"https://{host}/ccx/service/{tenant}/{DEFAULT_SERVICE_NAME}/{DEFAULT_VERSION}"
    )
    # See the same fix in _pump_discovery — a widget's session_state entry
    # overrides value= once the widget has been rendered once.
    st.session_state[target_widget_key] = side.target_raw
    try:
        side.target = parse_tenant_url(side.target_raw)
    except TenantURLError:
        side.target = None

    env_prefix = _ENV_PREFIX[role]
    env_user = os.environ.get(f"{env_prefix}_ISU_USERNAME", "")
    env_pass = os.environ.get(f"{env_prefix}_ISU_PASSWORD", "")
    if env_user:
        side.username = env_user
        st.session_state[user_widget_key] = env_user
    if env_pass:
        side.password = env_pass
        st.session_state[pass_widget_key] = env_pass
    return bool(env_user and env_pass)


def _render_quick_fill(state: WizardState, side: ConnectionState, role: Role,
                       label: str, key: str) -> None:
    """The .env shortcut, if this side has one configured.

    The source side fills and tests in one click — it is a read-only
    connection, and getting to a verified source fast is the whole point.
    The destination side fills but never tests itself: that is the tenant
    this tool writes to, and pointing it somewhere should always be
    something the user did on purpose and then confirmed with a second
    click on Test.
    """
    env_target = _env_target(role)
    if env_target is None:
        return
    tenant, _host = env_target
    if st.button(f"Quick fill from .env: {tenant}", key=f"{key}_quick_fill"):
        creds_filled = _quick_fill(
            side,
            role,
            target_widget_key=f"{key}_target",
            user_widget_key=f"{key}_user",
            pass_widget_key=f"{key}_pass",
        )
        if role is Role.SOURCE and creds_filled:
            _attempt_connect(state, side, role, label)
        else:
            side.quick_filled_pending_test = True
        st.rerun()

    if role is Role.DESTINATION and side.quick_filled_pending_test:
        theme.banner(
            "warning",
            f"Destination filled from .env: {tenant}",
            "This is the tenant this tool writes to, and nothing has been "
            "tested against it yet. Check it is the one you mean.",
            remedy="Click Test destination connection when you are ready.",
        )


def _render_side(state: WizardState, side: ConnectionState, role: Role, label: str, key: str) -> None:
    theme.section(
        label,
        eyebrow="Reads from" if role is Role.SOURCE else "Writes to",
    )

    _render_quick_fill(state, side, role, label, key)

    with st.expander(
        "Find services host from tenant ID",
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
        f"{label} Username",
        key=f"{key}_user",
        value=side.username,
        help="Username only — the tenant is appended. name@tenant is kept as-is.",
    )
    side.password = secrets_ui.password_input(f"{label} Password", key=f"{key}_pass")

    if st.button(f"Test {label.lower()} connection", key=f"{key}_test"):
        _attempt_connect(state, side, role, label)

    render_target_card(label, side.target)
    render_connection_status(side.status)
    render_capabilities(side.capabilities)


def _render_package_loader(state: WizardState) -> None:
    """Load a stored source package in place of a live source tenant.

    A package is a pre-resolved closure captured from a source tenant and
    serialised to disk. Loading one replaces the "connect to source" half of
    the wizard entirely: the destination side still needs credentials, but
    Select / Resolve become read-only summaries of what the package holds.
    Useful when the same bundle of reports gets shipped to many destinations,
    or when the source tenant is no longer reachable.
    """
    metas = list_packages(default_packages_dir())
    if not metas and state.package is None:
        return  # Nothing to load, and nothing loaded — skip the section entirely.

    theme.section(
        "Or load a stored package",
        "Replaces the source connection. Destination still needs credentials.",
        eyebrow="No live source",
    )

    if state.package is not None:
        theme.banner(
            "success",
            f"Package loaded: {state.package.name}",
            f"{state.package.node_count} objects from "
            f"{state.package.source_tenant} — captured {state.package.captured_at}.",
        )
        st.caption(state.package.description or "(no description)")
        if st.button("Clear loaded package", key="pkg_clear"):
            # Explicit — reset_downstream no longer touches state.package
            # (see its docstring for why).
            state.package = None
            state.closure = None
            state.closure_error = None
            reset_downstream(state, from_step="select")
            st.rerun()
        return

    options = {m.path.name: m for m in metas}
    choice = st.selectbox(
        "Pick a stored package",
        options=list(options),
        format_func=lambda n: (
            f"{options[n].name}  —  {options[n].node_count} objects "
            f"from {options[n].source_tenant}"
        ),
        key="pkg_choice",
    )
    picked = options[choice]
    st.caption(picked.description or "(no description)")
    if st.button(f"Load {picked.name!r}", key="pkg_load", type="secondary"):
        try:
            package = load_package(picked.path)
        except PackageError as exc:
            theme.banner("danger", "Could not load package", str(exc))
            return
        # Wipe every downstream artefact (selections, indexes, closure, plan,
        # dry run, blocking refs) so a package load starts from a clean slate.
        # reset_downstream no longer touches ``state.package`` (it's source-
        # side state that a destination change shouldn't wipe), so the order
        # of these two lines is no longer load-bearing.
        reset_downstream(state, from_step="select")
        state.package = package
        # The package IS the closure. Wire it straight into state so Resolve
        # is a no-op summary rather than a live resolve_closure call.
        state.closure = package.closure
        state.closure_error = None
        st.rerun()


def _render_resume(state: WizardState) -> None:
    """Pick up a saved session.

    Only offered on Connect, and only when nothing has been picked yet:
    restoring over a selection someone is in the middle of making would be a
    destructive act behind a button labelled "resume".
    """
    if state.selected_reports_added or state.selected_dashboards_added:
        return

    theme.section(
        "Resume a saved session",
        "Brings back the tenants, usernames and object selection from a "
        "previous session. Passwords and approvals are never saved.",
        eyebrow="Optional",
    )

    summaries = session_store.list_sessions(
        workspace.user_dir(session_store.SESSION_DIR)
    )
    if summaries:
        st.caption(
            "Saved on the server. Only sessions from this browser's workspace "
            "are listed — other people using this app have their own. These "
            "are lost if the app restarts."
        )
        options = {s.path.name: s for s in summaries}
        choice = st.selectbox(
            "Saved sessions",
            options=list(options),
            format_func=lambda n: options[n].label,
            key="session_choice",
        )
        if st.button("Resume this session", key="session_resume"):
            try:
                data = session_store.load_session(options[choice].path)
            except session_store.SessionError as exc:
                theme.banner("danger", "Could not resume", str(exc))
                return
            _apply_resumed(state, data)

    # The durable half. A hosted app's filesystem does not survive the
    # container restarting — which happens on redeploy, on waking from idle,
    # and on running out of memory — so the copy that actually keeps is the
    # one the user downloaded to their own machine.
    uploaded = st.file_uploader(
        "Or upload a session file you downloaded earlier",
        type=["json"],
        key="session_upload",
        help="The file from the Download button on the Select step.",
    )
    if uploaded is not None and st.button(
        "Resume from this file", key="session_resume_upload"
    ):
        try:
            data = session_store.parse_session(uploaded.getvalue(), label=uploaded.name)
        except session_store.SessionError as exc:
            theme.banner("danger", "Could not resume", str(exc))
            return
        _apply_resumed(state, data)

    for note in st.session_state.get("_resume_notes", []):
        st.caption(note)


def _apply_resumed(state: WizardState, data: dict) -> None:
    """Copy a validated session onto the wizard and rerun.

    Shared by the on-disk picker and the uploader so the two cannot drift —
    in particular so neither can skip seeding the widget-backed fields and
    leave a resumed session showing empty tenant boxes.
    """
    st.session_state["_resume_notes"] = session_store.restore(state, data)
    # The target and username fields are widget-backed; once rendered,
    # session_state drives them and ``value=`` is ignored. Same fix as
    # _pump_discovery and _quick_fill.
    for key, side in (("src", state.source), ("dst", state.dest)):
        st.session_state[f"{key}_target"] = side.target_raw
        st.session_state[f"{key}_user"] = side.username
    st.rerun()


def _render_save_session(state: WizardState) -> None:
    """Save from Connect too, not only from Plan.

    The expensive thing to lose is the selection, and by the time someone is
    on Connect again they may have already lost it. This is here for the
    other direction: save before closing the tab.
    """
    if not any(
        (
            state.selected_reports_added,
            state.selected_dashboards_added,
            state.selected_field_wids,
            state.selected_time_calculation_wids,
        )
    ):
        return
    if st.button("Save this session", key="session_save_connect"):
        path = session_store.save_session(
            state, directory=workspace.user_dir(session_store.SESSION_DIR)
        )
        theme.banner(
            "success",
            "Session saved",
            f"Written to `{path}`. Resume it from this step after a reload.",
            remedy=(
                "Keep this tab's URL. Saved sessions are private to the "
                f"workspace in the address bar (`{workspace.shared_link()}`), "
                "so a link without it will not find this session."
            ),
            remedy_label="Before you close the tab",
        )


def _render_before_you_start() -> None:
    """What has to be true before any of this works.

    All four of these are in CLAUDE.md and were in no part of the product.
    Three of them are only discovered by failing: the implementer gate on
    Select after a sweep, the Put grant on Run after a failed write, and the
    pending-security-change delay never — it reads as an intermittent
    permissions bug. The host trap returns HTTP 500, which reads like an
    outage rather than a typo.
    """
    with st.expander("Before you start — what you need", expanded=False):
        theme.checklist(
            [
                "An Integration System User on BOTH tenants with Get and Put on "
                "Configuration Set: Custom Reports and Fields.",
                "After any security change in Workday, run 'Activate Pending "
                "Security Policy Changes' — grants are not live until you do, "
                "and until then this reads as an intermittent permission error.",
                "Dashboards, prompt sets, prompt fields and time calculations "
                "additionally need an implementer account. That is an account "
                "type, not a domain grant — no security configuration changes "
                "it. The connection test below reports which you have.",
                "Use the services host (impl-services1.wd12…), not the browser "
                "host (impl.wd12…). A mismatched host and tenant returns HTTP "
                "500, which looks like an outage rather than a typo.",
                "Implementation or Sandbox tenants only. Nothing this tool "
                "writes can be undone by it — the web service has no delete "
                "operation.",
            ]
        )


def render(state: WizardState) -> None:
    st.header("Connect")
    _render_before_you_start()
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

    # Placed below the two connection panes so the primary path (source +
    # destination) reads first; the package loader is the alternative for
    # someone who does not need or have a live source.
    st.divider()
    _render_resume(state)
    _render_save_session(state)
    _render_package_loader(state)


def gate(state: WizardState) -> list[Blocker]:
    blockers = []
    # A loaded package IS the source — no live source connection is needed.
    if state.package is None and not state.source.verified:
        blockers.append(
            Blocker(
                node_id=None,
                title="Source not connected",
                detail="The source tenant connection has not been tested successfully.",
                remedy="Enter source credentials and click Test source connection, "
                       "or load a stored package above.",
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
