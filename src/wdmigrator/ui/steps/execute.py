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

from wdmigrator.api import Blocker, GuardViolation, iter_execute
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import WizardState, build_guard, owner_reference

STEP_ID = "execute"


def _start(state: WizardState) -> None:
    guard = build_guard(state, dry_run=False)
    try:
        generator = iter_execute(
            state.dest.connection, state.plan, guard,
            owner_reference=owner_reference(state), stop_on_failure=True,
        )
    except GuardViolation as exc:
        st.error(f"Blocked: {exc}", icon="🚫")
        return
    state.execute_job = start_job(generator)
    state.execute_records = []
    state.execute_paused = False


def render(state: WizardState) -> None:
    st.header("Execute")

    if state.plan is None:
        st.error("No plan — go back to Conflicts.")
        return

    job = state.execute_job

    if job is None and not state.execute_records:
        st.warning(
            f"This will write {state.plan.writes_planned} object(s) to "
            f"`{state.dest.target.tenant}`. This cannot be undone by this tool.",
            icon="⚠️",
        )
        if st.button("Start live execution", key="execute_start", type="primary"):
            _start(state)
            st.rerun()
        return

    if job is not None:
        col1, col2 = st.columns(2)
        with col1:
            if not state.execute_paused:
                if st.button("Pause", key="execute_pause"):
                    state.execute_paused = True
                    st.rerun()
            else:
                if st.button("Resume", key="execute_resume"):
                    state.execute_paused = False
                    st.rerun()
        with col2:
            if st.button("Cancel", key="execute_cancel"):
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
            st.warning("Execution cancelled. Objects already written cannot be undone by this tool.", icon="⏹️")
        elif job.done:
            state.execute_records = [p.record for p in job.events]
            state.execute_job = None
            state.step = "results"
            st.rerun()
        elif not state.execute_paused:
            st.rerun()
        return

    st.success(f"Execution finished — {len(state.execute_records)} record(s). See Results.")


def gate(state: WizardState) -> list[Blocker]:
    if state.execute_job is not None:
        return [Blocker(None, "Execution in progress", "Wait for the run to finish, or cancel it.", "")]
    if not state.execute_records:
        return [Blocker(None, "Not executed yet", "Live execution has not been run.", "Click Start live execution above.")]
    return []
