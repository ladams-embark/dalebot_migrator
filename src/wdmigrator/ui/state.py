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
    Closure,
    Connection,
    ConnectionStatus,
    Index,
    MigrationPlan,
    TenantTarget,
    WriteGuard,
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

    # SOURCE indexes. Everything Select needs to browse pickers and to resolve
    # dependencies. Dashboards, prompt sets, and prompt fields require an
    # implementer account — a normal ISU gets "The task submitted is not
    # authorized" — so they're separate from the always-needed calculated-field
    # sweep and are skipped rather than blocking the whole build.
    #
    # A prompt set's members name prompt fields; a report's gauge layout names
    # a gauge range; a matrix measure names an analytic indicator. All three
    # are single pages, and all three are read by `resolve` rather than by a
    # picker — with any of them absent, `resolve_closure` does not even *look*
    # for that kind of reference, so the dependency silently never enters the
    # closure and the write fails live.
    cf_index: Optional[Index] = None
    report_index: Optional[Index] = None
    dashboard_index: Optional[Index] = None
    prompt_set_index: Optional[Index] = None
    prompt_field_index: Optional[Index] = None
    gauge_range_index: Optional[Index] = None
    analytic_indicator_index: Optional[Index] = None
    #: One job drains every source index sweep back-to-back — Build once,
    #: everything comes in. See ``wdmigrator.ui.indexes.bulk_build_indexes``.
    source_index_job: Optional[JobState] = None
    #: Set when a dashboard/prompt-set/prompt-field sweep failed with the
    #: implementer fault, so the Select step can explain it once rather than
    #: showing a raw error.
    implementer_required: bool = False

    # DESTINATION sweeps. These are what make cross-tenant matching possible,
    # and they are a correctness requirement rather than an optimization:
    # `Calculated_Field_ID` and `BI_Calculated_Measure_ID` are not stable
    # identities between independently-built tenants, so without these every
    # shared object probes as absent and is planned as CREATE. The destination
    # then rejects the duplicate ("Enter a unique WQL alias for the business
    # object") and the run halts on the first one. Held on the wizard rather
    # than rebuilt per probe because the calculated-field sweep costs ~25s.
    dest_cf_index: Optional[Index] = None
    dest_measure_index: Optional[Index] = None
    dest_index_job: Optional[JobState] = None

    # wid -> True for directly-selected calculated fields
    selected_field_wids: set = field(default_factory=set)
    # wid -> the report's raw payload dict, for every report the user has
    # explicitly added — from the index table or from the exact-name lookup.
    #
    # This has to accumulate on its own rather than be read back off the
    # table widget. ``st.dataframe`` reports its selection as *row positions
    # into the frame it was just handed*, so the moment the filter box
    # changes the frame, those positions describe different reports (or none).
    # Deriving the selection from the widget each rerun therefore silently
    # dropped everything picked under a previous search term, making it
    # impossible to select reports across more than one search.
    selected_reports_added: dict = field(default_factory=dict)
    selected_reports: dict = field(default_factory=dict)
    # wid -> the dashboard's raw payload dict, from the dashboard index. Unlike
    # reports there is no exact-name lookup to merge in: dashboards have no
    # usable request criteria at all, so the index table is the only source.
    # Accumulated behind an explicit add, for the same reason reports are —
    # see ``selected_reports_added``.
    selected_dashboards_added: dict = field(default_factory=dict)
    selected_dashboards: dict = field(default_factory=dict)

    closure: Optional[Closure] = None
    closure_error: Optional[str] = None

    existence_job: Optional[JobState] = None
    plan: Optional[MigrationPlan] = None
    # node_id -> Action, user overrides on top of default_action()
    action_overrides: dict = field(default_factory=dict)
    # source WID -> ReferenceDecision, for references the destination cannot
    # resolve. Held on the wizard rather than the plan so a re-probe does not
    # discard answers the user has already given.
    reference_decisions: dict = field(default_factory=dict)

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
    # source WID -> {"reference", "node_id", "node_name", "sites"} for every
    # reference the destination could not resolve. Accumulates across attempts:
    # Workday reports one failure at a time, so the complete picture only
    # emerges over several, and losing the earlier ones would make the table
    # flicker between single rows instead of building up.
    blocking_references: dict = field(default_factory=dict)
    # Re-probe kicked off by submitting the mapping table, so a decision does
    # not cost a trip back through Conflicts and Confirm.
    reprobe_job: Optional[JobState] = None


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
        state.report_index = None
        state.dashboard_index = None
        state.prompt_set_index = None
        state.prompt_field_index = None
        state.gauge_range_index = None
        state.analytic_indicator_index = None
        state.source_index_job = None
        # Credential-scoped, like the source indexes above: this reset is what
        # runs when a connection changes, and a destination index swept against
        # a *different* destination would silently authorise skipping objects
        # that tenant has never had.
        state.dest_cf_index = None
        state.dest_measure_index = None
        state.dest_index_job = None
        state.implementer_required = False
        state.selected_field_wids = set()
        state.selected_reports_added = {}
        state.selected_reports = {}
        state.selected_dashboards_added = {}
        state.selected_dashboards = {}
        state.reference_decisions = {}

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
        state.blocking_references = {}
        state.reprobe_job = None

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
