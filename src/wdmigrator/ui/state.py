"""Session-state model for the migration wizard.

Streamlit reruns the whole script top-to-bottom on every interaction; the
only thing that survives a rerun is ``st.session_state``. Everything the
wizard needs to remember lives in one ``WizardState`` stashed there under
:data:`STATE_KEY`. Local variables in step modules do not persist — reach
for this instead.

**Nothing here is a client or a credential at rest beyond the current
session.** ``ConnectionState.password`` holds the plain string a form field
just produced; it is wrapped into a ``Secret`` the moment ``api.connect()``
is called and is not read back out afterward for display. Never move any of
this into ``st.cache_data``/``st.cache_resource`` — those are process-global
and shared across sessions, which would leak one user's tenant access to
another the moment auth is added on top of this app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import streamlit as st

from wdmigrator.api import (
    Action,
    Closure,
    Connection,
    ConnectionStatus,
    Index,
    MigrationPlan,
    TenantTarget,
    WriteGuard,
    WriteRecord,
)
from wdmigrator.ui.runner import JobState

STATE_KEY = "wizard"

STEP_ORDER = ["connect", "select", "resolve", "conflicts", "confirm", "execute", "results"]
STEP_TITLES = {
    "connect": "Connect",
    "select": "Select",
    "resolve": "Resolve",
    "conflicts": "Conflicts",
    "confirm": "Confirm",
    "execute": "Execute",
    "results": "Results",
}


@dataclass
class ConnectionState:
    """What we know about one side (source or destination) of the migration."""

    target_raw: str = ""
    target: Optional[TenantTarget] = None
    username: str = ""
    password: str = ""
    connection: Optional[Connection] = None
    status: Optional[ConnectionStatus] = None
    verified_fingerprint: str = ""

    # Endpoint discovery ("I only know the tenant ID") — separate from
    # target_raw/target above since discovery works from a bare tenant ID,
    # not a URL, and needs its own in-progress job state.
    discovery_tenant_id: str = ""
    discovery_job: Optional[JobState] = None
    # st.expander defaults to closed on every rerun unless expanded= is
    # passed explicitly — without tracking this, the expander showing
    # discovery's own progress collapses on the very reruns that pump
    # generates, hiding the progress messages it exists to show.
    discovery_expanded: bool = False

    @property
    def verified(self) -> bool:
        return self.status is not None and self.status.ok and self.connection is not None


@dataclass
class WizardState:
    step: str = "connect"

    source: ConnectionState = field(default_factory=ConnectionState)
    dest: ConnectionState = field(default_factory=ConnectionState)

    cf_index: Optional[Index] = None
    cf_index_job: Optional[JobState] = None
    report_index: Optional[Index] = None
    report_index_job: Optional[JobState] = None

    # wid -> True for directly-selected calculated fields
    selected_field_wids: set = field(default_factory=set)
    # wid -> the report's raw payload dict. Recomputed each render as
    # {**selected_reports_manual, **<current table selection>}; only the
    # manual (exact-name-search) additions need to persist on their own,
    # since the report index table's own widget state already persists its
    # row selection across reruns.
    selected_reports_manual: dict = field(default_factory=dict)
    selected_reports: dict = field(default_factory=dict)

    closure: Optional[Closure] = None
    closure_error: Optional[str] = None

    existence_job: Optional[JobState] = None
    plan: Optional[MigrationPlan] = None
    # node_id -> Action, user overrides on top of default_action()
    action_overrides: dict = field(default_factory=dict)

    dry_run_job: Optional[JobState] = None
    dry_run_records: list = field(default_factory=list)
    dry_run_plan_hash: str = ""
    dry_run_reviewed: bool = False

    confirmed_tenant_name: str = ""
    warnings_acknowledged: set = field(default_factory=set)
    irreversible_ack: bool = False

    execute_job: Optional[JobState] = None
    execute_paused: bool = False
    execute_records: list = field(default_factory=list)


def get_state() -> WizardState:
    if STATE_KEY not in st.session_state:
        st.session_state[STATE_KEY] = WizardState()
    return st.session_state[STATE_KEY]


def reset_downstream(state: WizardState, *, from_step: str) -> None:
    """Wipe everything computed at or after ``from_step``.

    Streamlit has no dependency graph, so nothing recomputes on its own when
    an upstream input changes — a new credential, a changed selection. This
    is called explicitly at those points instead.
    """
    idx = STEP_ORDER.index(from_step)

    if idx <= STEP_ORDER.index("select"):
        state.cf_index = None
        state.cf_index_job = None
        state.report_index = None
        state.report_index_job = None
        state.selected_field_wids = set()
        state.selected_reports_manual = {}
        state.selected_reports = {}

    if idx <= STEP_ORDER.index("resolve"):
        state.closure = None
        state.closure_error = None

    if idx <= STEP_ORDER.index("conflicts"):
        state.existence_job = None
        state.plan = None
        state.action_overrides = {}

    if idx <= STEP_ORDER.index("confirm"):
        state.dry_run_job = None
        state.dry_run_records = []
        state.dry_run_plan_hash = ""
        state.dry_run_reviewed = False
        state.confirmed_tenant_name = ""
        state.warnings_acknowledged = set()

    if idx <= STEP_ORDER.index("execute"):
        state.execute_job = None
        state.execute_records = []

    # Never leave the user parked past the point their data just got wiped.
    if STEP_ORDER.index(state.step) > idx:
        state.step = from_step


DEFAULT_REPORT_OWNER_USERNAME = "wd-support"


def owner_reference(state: WizardState):
    """Every report this tool creates is owned by the fixed destination
    account below, not whoever owned it on the source — the source owner
    almost certainly doesn't exist in the destination tenant. Resolved by
    `WorkdayUserName` on the PUT itself (the same mechanism verified live for
    ad hoc owners), since Employee/Contingent Worker System User lookups
    expose no criteria to resolve a username to a WID ahead of time — only
    the reference on the write itself can do that resolution."""
    from wdmigrator.api import build_owner_reference

    return build_owner_reference(workday_username=DEFAULT_REPORT_OWNER_USERNAME)


def build_guard(state: WizardState, *, dry_run: bool) -> WriteGuard:
    """Derive a `WriteGuard` from current state. Cheap and pure — rebuilt
    fresh wherever it's needed rather than cached, so it's always evaluated
    against what the state actually says right now, not a stale snapshot."""
    plan_hash = state.plan.plan_hash() if state.plan is not None else ""
    return WriteGuard(
        source=state.source.target,
        dest=state.dest.target,
        dry_run=dry_run,
        plan_hash=plan_hash,
        confirmed_tenant_name=state.confirmed_tenant_name,
        dry_run_reviewed=bool(state.dry_run_reviewed and state.dry_run_plan_hash == plan_hash),
        source_verified=state.source.verified,
        dest_verified=state.dest.verified,
        source_username=state.source.username,
        dest_username=state.dest.username,
        warnings_acknowledged=frozenset(state.warnings_acknowledged),
    )
