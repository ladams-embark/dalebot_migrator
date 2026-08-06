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

from wdmigrator.api import (
    Blocker,
    GuardViolation,
    ReferenceAction,
    ReferenceDecision,
    find_reference_sites,
    iter_execute,
)
from wdmigrator.ui import theme
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import (
    WizardState,
    build_guard,
    owner_reference,
    reset_downstream,
)

STEP_ID = "execute"


def _blocked_record(state: WizardState):
    """The failed record naming a reference the destination could not resolve.

    Only ``Invalid ID value`` faults produce one — a schema error or an
    entitlement problem is not fixable by substituting a reference, and must not
    be offered as though it were.
    """
    for record in reversed(state.execute_records):
        if record.blocking_reference is not None:
            return record
    return None


def _render_reference_resolution(state: WizardState, record) -> None:
    """Ask what to do about one unresolvable reference, then retry.

    Fault-driven rather than pre-flight on purpose: a real report carries ~90
    references the tool does not migrate, and almost all of them are delivered
    objects that pass through fine. Asking about all of them would bury the
    handful that actually break. Workday names the offending identifier in the
    fault, so this asks about exactly that one.
    """
    blocking = record.blocking_reference
    node = next(
        (n for n in state.plan.ordered_nodes if n.node_id == record.node_id), None
    )
    sites = find_reference_sites(node, blocking.value) if node is not None else []

    theme.section(
        "Unresolvable reference",
        f"{record.name!r} could not be written because the destination has no "
        f"object matching this {blocking.id_type}.",
        eyebrow="Needs a decision",
    )
    theme.card(
        blocking.id_type,
        meta=blocking.value,
        note=(f"Appears at {len(sites)} place(s) in this object."
              if sites else "Not located in this object's payload."),
    )

    if sites:
        others = {}
        for site in sites:
            for id_type, value in site.ids.items():
                if id_type != "WID":
                    others[id_type] = value
        st.caption("Where it appears: " + ", ".join(
            sorted({s.element for s in sites})
        ))
        if others:
            st.caption("Also identified as: " + ", ".join(
                f"`{k}` = {v}" for k, v in others.items()
            ))

    choice = st.radio(
        "What should this reference become?",
        ["Leave it blank", "Point it at something else"],
        key=f"refdec_choice_{blocking.value}",
        help="Blanking drops the value — a prompt default disappears, a filter "
             "loses its comparison value. The object still migrates.",
    )

    replacement_type = replacement_value = None
    if choice == "Point it at something else":
        cols = st.columns(2)
        with cols[0]:
            replacement_type = st.text_input(
                "ID type",
                value=next(iter(k for k in (
                    t for s in sites for t in s.ids if t != "WID"
                )), ""),
                key=f"refdec_type_{blocking.value}",
                help="e.g. Organization_Reference_ID",
            )
        with cols[1]:
            replacement_value = st.text_input(
                "ID value in the destination",
                key=f"refdec_value_{blocking.value}",
                help="Look this up in the destination tenant — there is no "
                     "generic way for this tool to list candidates.",
            )

    ready = choice == "Leave it blank" or (replacement_type and replacement_value)
    if st.button("Apply and re-check destination",
                 key=f"refdec_apply_{blocking.value}",
                 type="primary", disabled=not ready):
        state.reference_decisions[blocking.value] = ReferenceDecision(
            source_wid=blocking.value,
            action=(ReferenceAction.BLANK if choice == "Leave it blank"
                    else ReferenceAction.REPLACE),
            replacement_type=replacement_type or None,
            replacement_value=replacement_value or None,
        )
        # Back to Conflicts, deliberately, rather than retrying in place.
        # Two reasons, both safety: the decision changes the payload and
        # therefore the plan hash, which invalidates the reviewed dry run
        # exactly as a Conflicts override does; and objects written before the
        # failure need a fresh probe so they come back as SKIP instead of being
        # written a second time.
        reset_downstream(state, from_step="conflicts")
        st.rerun()

    st.caption(
        "Applying a decision returns you to Conflicts to re-probe. Objects already "
        "written come back as SKIP, and the changed payload needs a fresh dry run "
        "before it can go live — the same rule an action override follows."
    )
    if state.reference_decisions:
        st.caption(f"{len(state.reference_decisions)} decision(s) recorded so far. "
                   "They survive re-probing and are covered by the plan hash.")


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

    job = state.execute_job

    if job is None and not state.execute_records:
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

    blocked = _blocked_record(state)
    if blocked is not None:
        st.divider()
        _render_reference_resolution(state, blocked)
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
