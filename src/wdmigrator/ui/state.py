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

from dataclasses import MISSING, dataclass, field, fields
from typing import Optional

import streamlit as st

from wdmigrator.api import (
    Closure,
    Connection,
    ConnectionStatus,
    Index,
    MigrationPlan,
    Package,
    ReportSharing,
    TenantTarget,
    WriteGuard,
)
from wdmigrator.ui.runner import JobState

STATE_KEY = "wizard"

STEP_ORDER = ["connect", "select", "plan", "run", "results"]
STEP_TITLES = {
    "connect": "Connect",
    "select": "Select",
    "plan": "Plan",
    "run": "Run",
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

    #: A stored package loaded in place of a live source tenant. Populated by
    #: the "Load stored package" affordance on the Connect step. When set:
    #:   * the source connection is not required (the package IS the source);
    #:   * Select renders a read-only summary of the loaded closure;
    #:   * Plan's resolve pass is a no-op — ``state.closure`` is set straight
    #:     from the package on load, so nothing here calls ``resolve_closure``
    #:     again;
    #:   * Plan / Run / Results run unchanged.
    #: Cleared by ``reset_downstream("select")`` or when the user clicks the
    #: "Clear loaded package" button on Connect.
    package: Optional[Package] = None

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
    #: Time Tracking indexes. Live on Time_Tracking_Implementation_Service and
    #: are swept through a TT sibling connection built from
    #: ``state.source.connection``. Only fetched when the user picks
    #: "time_calculations" in Select.
    time_calculation_index: Optional[Index] = None
    time_calculation_tag_index: Optional[Index] = None
    time_calculation_group_index: Optional[Index] = None
    #: One job drains every source index sweep back-to-back — starts itself
    #: on Select when a live connection is present. See
    #: ``wdmigrator.ui.indexes.bulk_build_indexes``.
    source_index_job: Optional[JobState] = None
    #: Report index is a separate job so exact-name add does not wait on the
    #: ~2.5 minute report sweep. Browse table fills in when this finishes.
    report_index_job: Optional[JobState] = None
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
    #: Directly-selected Time Calculations (WIDs). Tag and Group dependencies
    #: are pulled in automatically by resolve_closure using the corresponding
    #: indexes.
    selected_time_calculation_wids: set = field(default_factory=set)

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
    #: Post-run read-back verifier. Formalises the hand-written scripts every
    #: prior migration relied on to prove the objects landed — the writer's
    #: SUCCESS bit has been observed to lie (HANDOFF: "0 failed" with two
    #: empty-shell dashboards). Populated by :mod:`~wdmigrator.validation`.
    verify_job: Optional[JobState] = None
    verify_records: list = field(default_factory=list)
    #: Post-run restoration pass — the user optionally re-populates
    #: Instance_References that Preflight blanked, and the writer UPDATE-writes
    #: only the objects that named any of those. Kept separate from the initial
    #: run's records so both stay visible in Results.
    restore_reprobe_job: Optional[JobState] = None
    restore_execute_job: Optional[JobState] = None
    restore_records: list = field(default_factory=list)
    #: Node ids that the restoration pass should UPDATE. Populated when the
    #: user clicks Restore and cleared after the pass completes. Only these
    #: nodes flip to UPDATE; everything else stays SKIP for the second pass.
    restore_update_node_ids: set = field(default_factory=set)
    # source WID -> {"reference", "node_id", "node_name", "sites"} for every
    # reference the destination could not resolve. Accumulates across attempts:
    # Workday reports one failure at a time, so the complete picture only
    # emerges over several, and losing the earlier ones would make the table
    # flicker between single rows instead of building up.
    blocking_references: dict = field(default_factory=dict)
    #: Plan hash the pre-flight scan last ran against. Guards against re-running
    #: it every rerun, while still forcing a fresh pass whenever the plan
    #: changes shape (an override, a new decision, an added node).
    preflight_populated_for_hash: str = ""
    #: How reports should land on the destination — see :class:`ReportSharing`.
    #: Defaults to UNSHARED, the historical behaviour and the safest option:
    #: only the report's owner can see it until someone chooses otherwise. The
    #: Plan step exposes a radio to switch to SHARED_WITH_ALL_AUTHORIZED_USERS.
    report_sharing: ReportSharing = ReportSharing.UNSHARED
    # Re-probe kicked off by submitting the mapping table, so a decision does
    # not cost a trip back through Plan.
    reprobe_job: Optional[JobState] = None
    #: Set when the user clicks Back so Connect's auto-advance (both sides
    #: verified, skip to Select) does not bounce them forward again.
    hold_step: bool = False
    #: Set after a live run log is written under ``out/`` so a rerun does not
    #: create a second file for the same records.
    run_log_path: str = ""


def hydrate_wizard_state(state: WizardState) -> None:
    """Fill attributes added after this session's ``WizardState`` was created.

    Streamlit keeps the same instance across reruns, including a script reload
    that does not restart the process. A newly deployed field such as
    ``hold_step`` is therefore missing on an in-flight session, which would
    otherwise crash the nav bar with ``AttributeError``.
    """
    for f in fields(WizardState):
        if hasattr(state, f.name):
            continue
        if f.default is not MISSING:
            setattr(state, f.name, f.default)
        elif f.default_factory is not MISSING:  # type: ignore[misc]
            setattr(state, f.name, f.default_factory())
        else:
            setattr(state, f.name, None)


def get_state() -> WizardState:
    if STATE_KEY not in st.session_state:
        st.session_state[STATE_KEY] = WizardState()
        return st.session_state[STATE_KEY]
    state = st.session_state[STATE_KEY]
    hydrate_wizard_state(state)
    return state


def reset_downstream(state: WizardState, *, from_step: str) -> None:
    """Wipe everything computed at or after ``from_step``.

    Streamlit has no dependency graph, so nothing recomputes on its own when
    an upstream input changes — a new credential, a changed selection. This
    is called explicitly at those points instead.
    """
    idx = STEP_ORDER.index(from_step)

    if idx <= STEP_ORDER.index("select"):
        # NOTE: ``state.package`` is deliberately NOT cleared here. A stored
        # package is *source-side* state, but ``reset_downstream("select")``
        # fires on any credential change — including the destination's,
        # where the package is still valid. Callers that need to clear the
        # package do so explicitly (see the Load/Clear buttons on Connect,
        # and the source-side branch of :func:`_attempt_connect`).
        state.cf_index = None
        state.report_index = None
        state.dashboard_index = None
        state.prompt_set_index = None
        state.prompt_field_index = None
        state.gauge_range_index = None
        state.analytic_indicator_index = None
        state.time_calculation_index = None
        state.time_calculation_tag_index = None
        state.time_calculation_group_index = None
        state.source_index_job = None
        state.report_index_job = None
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
        state.selected_time_calculation_wids = set()
        state.reference_decisions = {}

    if idx <= STEP_ORDER.index("plan"):
        state.closure = None
        state.closure_error = None
        state.existence_job = None
        state.plan = None
        state.action_overrides = {}
        state.dry_run_job = None
        state.dry_run_records = []
        state.dry_run_plan_hash = ""
        state.dry_run_reviewed = False
        state.confirmed_tenant_name = ""
        state.warnings_acknowledged = set()
        state.irreversible_ack = False

    if idx <= STEP_ORDER.index("run"):
        state.execute_job = None
        state.execute_paused = False
        state.execute_records = []
        state.blocking_references = {}
        state.preflight_populated_for_hash = ""
        state.reprobe_job = None
        state.verify_job = None
        state.verify_records = []
        state.restore_reprobe_job = None
        state.restore_execute_job = None
        state.restore_records = []
        state.restore_update_node_ids = set()
        state.run_log_path = ""

    # Never leave the user parked past the point their data just got wiped.
    if STEP_ORDER.index(state.step) > idx:
        state.step = from_step


def clear_downstream_of_closure(state: WizardState) -> None:
    """Keep the current closure; drop the plan, dry run, and any writes.

    Used after a fresh resolve so an old CREATE/SKIP table cannot outlive
    the objects it was built from.
    """
    state.existence_job = None
    state.plan = None
    state.action_overrides = {}
    state.dry_run_job = None
    state.dry_run_records = []
    state.dry_run_plan_hash = ""
    state.dry_run_reviewed = False
    state.confirmed_tenant_name = ""
    state.warnings_acknowledged = set()
    state.irreversible_ack = False
    state.execute_job = None
    state.execute_paused = False
    state.execute_records = []
    state.blocking_references = {}
    state.preflight_populated_for_hash = ""
    state.reprobe_job = None
    state.verify_job = None
    state.verify_records = []
    state.restore_reprobe_job = None
    state.restore_execute_job = None
    state.restore_records = []
    state.restore_update_node_ids = set()
    state.run_log_path = ""


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
    against what the state actually says right now, not a stale snapshot.

    When a stored package is loaded there is no live source connection: the
    guard's source identity comes from the package's captured tenant instead,
    and ``source_verified`` is True because the package IS the source (its
    fidelity was proved when it was captured, not at run time).
    """
    plan_hash = state.plan.plan_hash() if state.plan is not None else ""
    if state.package is not None:
        # A package's captured tenant is the guard's "source" identity — same
        # tenant/host it was resolved against. When the package predates the
        # source_services_host field, fall back to a synthetic host that still
        # yields a distinct identity() from any real destination.
        from wdmigrator.api import Environment, TenantTarget
        source_target = TenantTarget(
            tenant=state.package.source_tenant,
            services_host=(state.package.source_services_host
                           or f"package:{state.package.name}"),
            ui_host="",
            environment=Environment.UNKNOWN,
            services_host_derived=False,
            raw_input=f"package:{state.package.name}",
        )
        source_username = f"package:{state.package.name}"
        source_verified = True
    else:
        source_target = state.source.target
        source_username = state.source.username
        source_verified = state.source.verified
    return WriteGuard(
        source=source_target,
        dest=state.dest.target,
        dry_run=dry_run,
        plan_hash=plan_hash,
        confirmed_tenant_name=state.confirmed_tenant_name,
        dry_run_reviewed=bool(state.dry_run_reviewed and state.dry_run_plan_hash == plan_hash),
        source_verified=source_verified,
        dest_verified=state.dest.verified,
        source_username=source_username,
        dest_username=state.dest.username,
        warnings_acknowledged=frozenset(state.warnings_acknowledged),
    )
