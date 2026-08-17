"""Step 6: Execute — the only step that can write to a tenant.

``batch_size=1``: at most one object is pulled from the generator per
Streamlit rerun, regardless of how much of the pump time budget is left.
Pause and Cancel are serviced between :func:`pump` calls, i.e. always
between objects — a browser refresh or a click mid-run can never leave an
object half-written. The engine re-checks ``assert_write_allowed`` inside
``write_node`` before every single write, not just once here.
"""

from __future__ import annotations

import streamlit as st

import pandas as pd

from wdmigrator.api import (
    Blocker,
    GuardViolation,
    ReferenceAction,
    ReferenceDecision,
    build_plan,
    find_reference_sites,
    iter_check_existence,
    iter_execute,
)
from wdmigrator.ui import theme
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.indexes import destination_match_indexes
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import WizardState, build_guard, owner_reference

STEP_ID = "execute"


def _blocked_record(state: WizardState):
    """The failed record naming a reference the destination could not resolve.

    Only ``Invalid ID value`` faults and exceptions produce one — a schema error
    or an entitlement problem is not fixable by substituting a reference, and
    must not be offered as though it were.
    """
    for record in reversed(state.execute_records):
        if record.blocking_reference is not None:
            return record
    return None


def _collect_blockers(state: WizardState) -> None:
    """Fold any newly-discovered unresolvable reference into the running table.

    Workday reports one bad reference per attempt, so the full set only emerges
    over several. Accumulating means the table grows into the complete picture
    instead of flickering between single rows, and a decision already made is
    never asked about twice.
    """
    for record in state.execute_records:
        blocking = record.blocking_reference
        if blocking is None or blocking.value in state.blocking_references:
            continue
        node = next(
            (n for n in state.plan.ordered_nodes if n.node_id == record.node_id), None
        )
        sites = find_reference_sites(node, blocking.value) if node is not None else []
        business = {}
        for site in sites:
            for id_type, id_value in site.ids.items():
                if id_type != "WID":
                    business[id_type] = id_value
        state.blocking_references[blocking.value] = {
            "reference": blocking,
            "node_name": record.name or record.node_id,
            "elements": sorted({s.element for s in sites}),
            "business": business,
        }


def _decision_rows(state: WizardState) -> list:
    rows = []
    for value, info in state.blocking_references.items():
        existing = state.reference_decisions.get(value)
        business_type = next(iter(info["business"]), "")
        rows.append({
            "Object": info["node_name"],
            "Where": ", ".join(info["elements"]) or "(not located)",
            "Identified as": ", ".join(
                f"{k} = {v}" for k, v in info["business"].items()
            ) or info["reference"].id_type,
            "Decision": (
                existing.action.value if existing else ReferenceAction.BLANK.value
            ),
            "Replacement ID type": (
                (existing.replacement_type if existing else None) or business_type
            ),
            "Replacement value": (
                (existing.replacement_value if existing else None) or ""
            ),
            "_wid": value,
        })
    return rows


def _apply_decisions(state: WizardState, rows: list, edited) -> None:
    """Fold the edited table back into decisions.

    Rows are matched to their source WID **positionally**, against the list the
    table was built from, rather than by reading a hidden ``_wid`` column back
    out of the editor. Whether a column hidden through ``column_config`` still
    appears in the returned frame is a Streamlit implementation detail, and
    depending on it would fail silently — every decision would land on the wrong
    reference, or raise a KeyError. Row order is guaranteed; that is enough.
    """
    for row, source in zip(edited.to_dict("records"), rows):
        action = ReferenceAction(row["Decision"])
        if action is ReferenceAction.REPLACE and not (
            row["Replacement ID type"] and row["Replacement value"]
        ):
            continue  # incomplete; the submit button is gated on these
        state.reference_decisions[source["_wid"]] = ReferenceDecision(
            source_wid=source["_wid"],
            action=action,
            replacement_type=row["Replacement ID type"] or None,
            replacement_value=row["Replacement value"] or None,
        )


def _start_reprobe(state: WizardState) -> None:
    # Same match indexes as the Conflicts probe, and not optional here either:
    # a re-probe without them would revert every cross-tenant match back to
    # CREATE, so answering one reference question would silently arm a run that
    # duplicates every shared object.
    state.reprobe_job = start_job(
        iter_check_existence(
            state.dest.connection,
            state.closure,
            **destination_match_indexes(state),
        )
    )
    state.execute_records = []
    state.execute_job = None


def _pump_reprobe(state: WizardState) -> None:
    """Re-probe in place, then rebuild the plan carrying the new decisions.

    Re-probing is not optional: objects written before the failure now exist,
    and without a fresh probe they would be planned as CREATE and written a
    second time. Doing it here rather than sending the user back to Conflicts
    is the only change — the safety property is identical.
    """
    job = state.reprobe_job
    pump(job, time_budget=0.8)
    last = job.last_event
    render_job_progress(
        job,
        label="Re-checking the destination",
        fraction=last.fraction if last is not None else 0.0,
    )

    if job.error is not None:
        state.reprobe_job = None
        return
    if not job.done:
        st.rerun()
        return

    existence = {p.node.node_id: p.existence for p in job.events}
    state.plan = build_plan(
        state.closure,
        existence,
        overrides=state.action_overrides,
        reference_decisions=state.reference_decisions,
    )
    # The mapping table IS the review of this change. A decision alters the
    # payload and therefore the plan hash, which would otherwise invalidate the
    # dry-run approval and force the entire Confirm gate again for every single
    # reference. Re-stamping here says: the user saw exactly what changed, in a
    # table, and authorised it. Everything else the guard checks — tenant name
    # retyped, irreversibility acknowledged, both sides verified, destination a
    # safe environment — is untouched and still has to hold.
    state.dry_run_plan_hash = state.plan.plan_hash()
    state.reprobe_job = None
    st.rerun()


def _render_reference_resolution(state: WizardState) -> None:
    """One table for every unresolvable reference found so far.

    Fault-driven rather than pre-flight: a real report carries ~90 references
    the tool does not migrate and almost all are delivered objects that pass
    through fine, so triaging all of them would bury the handful that break.
    Workday names the offending identifier, so only those appear here.
    """
    theme.section(
        "References the destination cannot resolve",
        "These point at tenant data rather than configuration — a prompt default, "
        "a filter value, a matrix pointer. They cannot be migrated, so each needs "
        "a decision. Blanking drops the value; the object still migrates.",
        eyebrow="Needs a decision",
    )

    rows = _decision_rows(state)
    edited = st.data_editor(
        pd.DataFrame(rows).drop(columns=["_wid"]),
        hide_index=True,
        use_container_width=True,
        disabled=["Object", "Where", "Identified as"],
        column_config={
            "Decision": st.column_config.SelectboxColumn(
                options=[a.value for a in ReferenceAction], required=True,
            ),
            "Replacement ID type": st.column_config.TextColumn(
                help="Only used when the decision is 'replace' — e.g. "
                     "Organization_Reference_ID."
            ),
            "Replacement value": st.column_config.TextColumn(
                help="The identifier in the DESTINATION tenant. There is no "
                     "generic way for this tool to list candidates, so look it "
                     "up in Workday."
            ),
        },
        key="reference_decision_table",
    )

    incomplete = [
        r["Object"] for r in edited.to_dict("records")
        if r["Decision"] == ReferenceAction.REPLACE.value
        and not (r["Replacement ID type"] and r["Replacement value"])
    ]
    if incomplete:
        theme.banner(
            "warning",
            f"{len(incomplete)} row(s) set to replace with no value",
            "Fill in both the ID type and the value, or set those rows back to blank.",
        )

    if st.button("Apply and re-check destination", key="refdec_apply",
                 type="primary", disabled=bool(incomplete)):
        _apply_decisions(state, rows, edited)
        _start_reprobe(state)
        st.rerun()

    st.caption(
        "Re-checking picks up anything already written so it is skipped rather than "
        "written twice. Your other approvals stay in place — you can start execution "
        "again straight afterwards."
    )


def _start(state: WizardState) -> None:
    guard = build_guard(state, dry_run=False)
    try:
        generator = iter_execute(
            state.dest.connection, state.plan, guard,
            owner_reference=owner_reference(state), stop_on_failure=True,
        )
    except GuardViolation as exc:
        theme.banner("danger", "Blocked by the write guard", str(exc))
        return
    state.execute_job = start_job(generator)
    state.execute_records = []
    state.execute_paused = False


def render(state: WizardState) -> None:
    st.header("Execute")

    if state.plan is None:
        theme.banner("danger", "No plan", remedy="Go back to Conflicts.")
        return

    # A re-probe kicked off from the mapping table owns the page while it runs.
    if state.reprobe_job is not None:
        _pump_reprobe(state)
        return

    _collect_blockers(state)
    job = state.execute_job

    if job is None and not state.execute_records:
        # The table outlives a failed attempt: decisions already made stay
        # visible and editable, so a second reference does not hide the first.
        if state.blocking_references:
            _render_reference_resolution(state)
            st.divider()

        theme.figures(
            [("Objects to write", state.plan.writes_planned)], tones={"Objects to write": "write"}
        )
        theme.banner(
            "warning",
            f"This writes to {state.dest.target.tenant}",
            "Objects are written one at a time, in dependency order, and each one's "
            "destination WID feeds the next. Pause and cancel take effect between "
            "objects, never mid-write.",
            remedy="Nothing written here can be undone by this tool.",
        )
        if st.button("Start live execution", key="execute_start", type="primary"):
            _start(state)
            st.rerun()
        return

    if job is not None:
        col1, col2, _ = st.columns([1, 1, 4])
        with col1:
            if not state.execute_paused:
                if st.button("Pause", key="execute_pause", use_container_width=True):
                    state.execute_paused = True
                    st.rerun()
            else:
                if st.button("Resume", key="execute_resume", type="primary",
                             use_container_width=True):
                    state.execute_paused = False
                    st.rerun()
        with col2:
            if st.button("Cancel", key="execute_cancel", use_container_width=True):
                job.cancel()
                st.rerun()

        if not state.execute_paused and job.running:
            pump(job, time_budget=0.8, batch_size=1)

        last = job.last_event
        fraction = last.fraction if last is not None else 0.0
        render_job_progress(job, label="Live execution", fraction=fraction)
        if last is not None:
            st.caption(f"{last.position}/{last.total}: {last.node.name or last.node.node_id} — {last.record.status.value}")

        if job.events:
            st.dataframe(
                [
                    {"name": p.node.name or p.node.node_id, "action": p.record.action.value, "status": p.record.status.value}
                    for p in job.events
                ],
                use_container_width=True, hide_index=True,
            )

        if job.error is not None:
            state.execute_records = [p.record for p in job.events]
            state.execute_job = None
        elif job.cancelled:
            state.execute_records = [p.record for p in job.events]
            state.execute_job = None
            theme.banner(
                "warning",
                "Execution cancelled",
                "It stopped cleanly between objects, so nothing is half-written — but "
                "objects already written cannot be undone by this tool.",
            )
        elif job.done:
            state.execute_records = [p.record for p in job.events]
            state.execute_job = None
            # Stay here if something stopped on an unresolvable reference —
            # that is answerable in place, and bouncing to Results would hide
            # the one question that would let the run finish.
            if _blocked_record(state) is None:
                state.step = "results"
            st.rerun()
        elif not state.execute_paused:
            st.rerun()
        return

    if state.blocking_references:
        st.divider()
        _render_reference_resolution(state)
        return

    theme.banner(
        "success",
        f"Execution finished — {len(state.execute_records)} record(s)",
        "Continue to Results for the per-object outcome and the exports.",
    )


def gate(state: WizardState) -> list[Blocker]:
    if state.execute_job is not None:
        return [Blocker(None, "Execution in progress", "Wait for the run to finish, or cancel it.", "")]
    if not state.execute_records:
        return [Blocker(None, "Not executed yet", "Live execution has not been run.", "Click Start live execution above.")]
    return []
