"""Step 4: Conflicts — probe the destination, review CREATE/SKIP per object.

UPDATE is deliberately never offered here. Whether ``Put_Calculated_Field``
with a reference does a full replace or a merge is unverified (see
CLAUDE.md), and the engine's own ``default_action()`` never proposes UPDATE
either — only CREATE (missing) or SKIP (exists, or existence is UNKNOWN). An
UNKNOWN existence result must never be treated as "missing": that's exactly
the case that would create a duplicate of something that's actually already
there, and it hard-blocks via ``validate_plan``.

**The probe is not allowed to run without cross-tenant matching.** A targeted
lookup by ``Calculated_Field_ID`` answers "does the destination hold a field
that was created the same way this one was", which is a different question
from "does the destination already have this field" — the same field is
``CRTMNU01_Commit - HR Dashboard_03_Is Top Performer`` on one tenant and
``Custom Object Data - Is Top Performer`` on another. Answering the first
question and acting on it plans a CREATE for a field that is already there;
Workday then rejects the write with "Enter a unique WQL alias for the business
object" and the run halts on the first one. Recovering the real answer needs a
sweep of the destination, which is why this step now sweeps before it probes.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from wdmigrator.api import (
    Action,
    Blocker,
    build_plan,
    iter_check_existence,
    validate_plan,
)
from wdmigrator.api import TIME_TRACKING_KINDS, TIME_TRACKING_SERVICE_NAME
from wdmigrator.ui import theme
from wdmigrator.ui.components import render_blockers, render_job_progress
from wdmigrator.ui.indexes import (
    bulk_build_indexes,
    destination_index_specs,
    destination_match_indexes,
    destination_matching_ready,
)
from wdmigrator.ui.runner import READ_TIME_BUDGET, pump, start_job
from wdmigrator.ui.state import WizardState, clear_downstream_of_closure

STEP_ID = "conflicts"


def _needs_tt(state: WizardState) -> bool:
    return any(
        n.kind in TIME_TRACKING_KINDS for n in (state.closure.nodes.values() if state.closure else ())
    )


def _start_probe(state: WizardState) -> None:
    clear_downstream_of_closure(state)
    tt_connection = (
        state.dest.connection.for_service(TIME_TRACKING_SERVICE_NAME)
        if _needs_tt(state)
        else None
    )
    state.existence_job = start_job(
        iter_check_existence(
            state.dest.connection,
            state.closure,
            tt_connection=tt_connection,
            **destination_match_indexes(state),
        )
    )


def _render_destination_indexes(state: WizardState) -> bool:
    """The two destination sweeps that make cross-tenant matching possible.

    Both are destination reads, not writes. Normally already built from the
    Select step — it builds these alongside the source sweeps so there is one
    Build click instead of two — so most of the time this renders as a status
    confirmation rather than an outstanding "Build" button. Kept here as a
    fallback (and for the Rebuild option) in case a session skipped Select's
    combined build, including a package-loaded run whose Select page used to
    return before destination matching started.
    """
    theme.section(
        "Destination matching",
        "Needed so shared fields are reused instead of duplicated. Rebuild if the destination was refreshed.",
        eyebrow="Required before probing",
    )
    if state.dest.connection is None:
        theme.banner(
            "danger",
            "Destination is not connected",
            "Cross-tenant matching reads the destination tenant. Go back to Connect.",
        )
        return False
    # A measure's BI_Calculated_Measure_ID is Workday-generated with a
    # tenant-local sequence number, so two tenants can never agree on one —
    # which makes this index the *only* thing standing between a shared measure
    # and a duplicate. It is one page, so rebuilding it is nearly free; the
    # caption below says when that is worth doing.
    specs = destination_index_specs(state.dest.connection)
    force_start = False
    with st.container():
        cols = st.columns([3, 1])
        with cols[1]:
            if st.button(
                "Re-run now",
                key="dest_index_rerun_now",
                disabled=state.dest_index_job is not None,
                use_container_width=True,
            ):
                for spec in specs:
                    setattr(state, spec.index_attr, None)
                state.implementer_required = False
                # Rebuild from this step in one click: clear cached indexes and
                # start the destination sweep in this same render.
                state.dest_index_job = None
                force_start = True
    running = bulk_build_indexes(
        state,
        specs,
        job_attr="dest_index_job",
        button_label="Build destination indexes",
        auto_start=force_start,
    )
    if not destination_matching_ready(state):
        theme.banner(
            "warning",
            "Destination sweeps not built",
            "Probing without these indexes can plan CREATE for fields the destination already has.",
            remedy="Wait for both indexes above, or click Build destination indexes.",
        )
    else:
        st.caption("Rebuild if the destination was refreshed this session.")
    return running


def _pump_probe(state: WizardState, *, auto_refresh: bool) -> None:
    job = state.existence_job
    pump(job, time_budget=READ_TIME_BUDGET)
    last = job.last_event
    fraction = last.fraction if last is not None else 0.0
    render_job_progress(job, label="Destination existence check", fraction=fraction)

    if job.error is not None:
        state.existence_job = None
    elif job.done:
        existence = {p.node.node_id: p.existence for p in job.events}
        state.plan = build_plan(state.closure, existence,
                                overrides=state.action_overrides,
                                reference_decisions=state.reference_decisions)
        state.existence_job = None
        st.rerun()
    elif auto_refresh:
        st.rerun()


def _render_overrides(state: WizardState) -> None:
    plan = state.plan
    rows = []
    for node in plan.ordered_nodes:
        existence = plan.existence.get(node.node_id)
        rows.append(
            {
                "node_id": node.node_id,
                "kind": node.kind.value,
                "name": node.name or "(unnamed)",
                "existence": existence.state.value if existence else "unknown",
                "action": plan.action_for(node).value,
            }
        )
    df = pd.DataFrame(rows)

    shells = [
        e for e in plan.existence.values()
        if getattr(e, "is_shell", False)
    ]
    if shells:
        theme.banner(
            "warning",
            f"{len(shells)} shell dashboard(s) will be completed by UPDATE",
            "A shell is a dashboard whose admin config exists in the destination "
            "but every tab is empty — the trace of a prior mid-run failure. It "
            "would otherwise probe as FOUND and skip forever; UPDATE has been "
            "verified live for exactly this case.",
        )
    st.caption("Change create or skip in the table. Update is only auto-set for empty-shell dashboards.")
    edited = st.data_editor(
        df,
        hide_index=True,
        use_container_width=True,
        disabled=["node_id", "kind", "name", "existence"],
        column_config={
            "action": st.column_config.SelectboxColumn(
                options=["create", "skip", "update"], required=True
            ),
        },
        key="conflicts_editor",
    )
    dirty = False
    for _, row in edited.iterrows():
        action = Action(row["action"])
        current = plan.action_for(
            next(n for n in plan.ordered_nodes if n.node_id == row["node_id"])
        )
        if action is not current:
            state.action_overrides[row["node_id"]] = action
            dirty = True
    if dirty:
        state.plan = build_plan(
            state.closure, plan.existence,
            overrides=state.action_overrides,
            reference_decisions=state.reference_decisions,
        )
        st.rerun()


def render(state: WizardState, *, heading: bool = True) -> None:
    if heading:
        st.header("Conflicts")
        st.caption(
            "Probes the destination tenant for every object in the resolved closure to "
            "decide CREATE vs SKIP. This is real, targeted destination traffic — one Get "
            "per object, not a bulk pull. It starts once destination matching is built."
        )

    if state.closure is None:
        theme.banner("danger", "No resolved closure", remedy="Go back to Plan.")
        return

    if state.existence_job is None:
        dest_running = _render_destination_indexes(state)
        st.divider()
        if dest_running:
            st.rerun()
            return

    if state.existence_job is None and state.plan is None:
        dest_live = getattr(state.dest.connection, "service", None) is not None
        if destination_matching_ready(state) and dest_live:
            _start_probe(state)
            st.rerun()
            return
        if st.button(
            f"Check existence for {len(state.closure)} objects",
            key="conflicts_start",
            disabled=not destination_matching_ready(state),
        ):
            _start_probe(state)
            st.rerun()
        return

    if state.existence_job is not None:
        auto_refresh = st.toggle(
            "Auto-refresh probe progress",
            value=True,
            key="conflicts_probe_autorefresh",
            help="Turn this off if the page keeps jumping while you scroll.",
        )
        if not auto_refresh:
            st.caption(
                "Auto-refresh is paused so you can scroll this step. "
                "Click Refresh progress to continue the probe."
            )
            if st.button("Refresh progress", key="conflicts_probe_refresh"):
                _pump_probe(state, auto_refresh=False)
            return
        _pump_probe(state, auto_refresh=True)
        return

    counts = state.plan.counts()
    unknown = state.plan.unknown_nodes()
    # A cross-tenant match is a weaker claim than an ID match — it says "this
    # looks like the same field" — so the count is shown rather than left
    # implicit. Every one of these is an object that would otherwise have been
    # duplicated.
    matched = [e for e in state.plan.existence.values() if e.matched_by]
    theme.figures(
        [(k.capitalize(), v) for k, v in counts.items()]
        + [("Unknown", len(unknown)), ("Matched cross-tenant", len(matched))],
        tones={"Create": "write", "Unknown": "danger" if unknown else "muted"},
    )
    if matched:
        with st.expander(
            f"{len(matched)} object(s) matched on shape, not on business ID"
        ):
            st.caption(
                "These exist in the destination under a different ID, so they will "
                "be reused and their dependents rewritten to point at them."
            )
            by_id = {n.node_id: n for n in state.plan.ordered_nodes}
            st.dataframe(
                [
                    {
                        "name": (by_id[e.node_id].name if e.node_id in by_id else None)
                        or e.node_id,
                        "matched by": e.matched_by,
                        "dest_wid": e.dest_wid,
                    }
                    for e in matched
                ],
                use_container_width=True,
                hide_index=True,
            )
    if unknown:
        theme.banner(
            "danger",
            f"{len(unknown)} object(s) have an unknown destination state",
            "The probe hit something other than a clean found or not-found fault. "
            "Unknown is never treated as missing — creating something that already "
            "exists is exactly the duplicate this refuses to risk.",
            remedy="Re-check below; if it persists, investigate the fault before continuing.",
        )

    _render_overrides(state)

    if st.button("Re-check existence against destination", key="conflicts_recheck"):
        _start_probe(state)
        st.rerun()

    st.divider()
    theme.section("Validation", eyebrow="Plan check")
    render_blockers(validate_plan(state.plan), empty_message="Plan is valid")


def gate(state: WizardState) -> list[Blocker]:
    if not destination_matching_ready(state):
        # Belt and braces with the disabled button: this is the check that
        # holds if the plan was carried in from anywhere else, and it is the
        # difference between reusing a shared object and duplicating it.
        return [
            Blocker(
                node_id=None,
                title="Destination not swept for cross-tenant matching",
                detail=(
                    "Business IDs do not identify an object across tenants. Without "
                    "the destination calculated-field and calculated-measure "
                    "indexes, every object whose ID differs is reported absent and "
                    "planned as a CREATE."
                ),
                remedy="Wait for both destination indexes above, or click Build destination indexes.",
            )
        ]
    if state.plan is None:
        return [
            Blocker(
                node_id=None,
                title="Destination not yet checked",
                detail="Run the existence check against the destination before continuing.",
                remedy="Build destination indexes, then run Check existence.",
            )
        ]
    return validate_plan(state.plan)
