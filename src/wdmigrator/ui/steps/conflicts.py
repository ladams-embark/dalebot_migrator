"""Step 4: Conflicts — probe the destination, review CREATE/SKIP per object.

UPDATE is deliberately never offered here. Whether ``Put_Calculated_Field``
with a reference does a full replace or a merge is unverified (see
CLAUDE.md), and the engine's own ``default_action()`` never proposes UPDATE
either — only CREATE (missing) or SKIP (exists, or existence is UNKNOWN). An
UNKNOWN existence result must never be treated as "missing": that's exactly
the case that would create a duplicate of something that's actually already
there, and it hard-blocks via ``validate_plan``.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from wdmigrator.api import Action, Blocker, build_plan, iter_check_existence, validate_plan
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import WizardState, reset_downstream

STEP_ID = "conflicts"


def _start_probe(state: WizardState) -> None:
    state.existence_job = start_job(iter_check_existence(state.dest.connection, state.closure))
    state.plan = None
    reset_downstream(state, from_step="confirm")


def _pump_probe(state: WizardState) -> None:
    job = state.existence_job
    pump(job, time_budget=0.8)
    last = job.last_event
    fraction = last.fraction if last is not None else 0.0
    render_job_progress(job, label="Destination existence check", fraction=fraction)

    if job.error is not None:
        state.existence_job = None
    elif job.done:
        existence = {p.node.node_id: p.existence for p in job.events}
        state.plan = build_plan(state.closure, existence, overrides=state.action_overrides)
        state.existence_job = None
        st.rerun()
    else:
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

    st.caption("UPDATE is not offered — treat it as unsafe until replace-vs-merge semantics are verified.")
    edited = st.data_editor(
        df,
        hide_index=True,
        use_container_width=True,
        disabled=["node_id", "kind", "name", "existence"],
        column_config={
            "action": st.column_config.SelectboxColumn(options=["create", "skip"], required=True),
        },
        key="conflicts_editor",
    )
    if st.button("Apply overrides", key="conflicts_apply"):
        for _, row in edited.iterrows():
            state.action_overrides[row["node_id"]] = Action(row["action"])
        state.plan = build_plan(state.closure, plan.existence, overrides=state.action_overrides)
        reset_downstream(state, from_step="confirm")
        st.rerun()


def render(state: WizardState) -> None:
    st.header("Conflicts")
    st.caption(
        "Probes the destination tenant for every object in the resolved closure to "
        "decide CREATE vs SKIP. This is real, targeted destination traffic — one Get "
        "per object, not a bulk pull."
    )

    if state.closure is None:
        st.error("No resolved closure — go back to Resolve.")
        return

    if state.existence_job is None and state.plan is None:
        if st.button(f"Check existence for {len(state.closure)} objects", key="conflicts_start"):
            _start_probe(state)
            st.rerun()
        return

    if state.existence_job is not None:
        _pump_probe(state)
        return

    counts = state.plan.counts()
    st.write("**Plan:** " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    unknown = state.plan.unknown_nodes()
    if unknown:
        st.error(
            f"{len(unknown)} object(s) have an UNKNOWN destination state — the probe "
            "hit something other than a clean found/not-found fault. These block "
            "progress until resolved.",
            icon="🚫",
        )

    _render_overrides(state)

    if st.button("Re-check existence against destination", key="conflicts_recheck"):
        _start_probe(state)
        st.rerun()

    blockers = validate_plan(state.plan)
    st.divider()
    st.subheader("Validation")
    if blockers:
        for b in blockers:
            where = f" ({b.node_id})" if b.node_id else ""
            st.error(f"**{b.title}{where}**\n\n{b.detail}\n\n*Remedy:* {b.remedy}", icon="🚫")
    else:
        st.success("No blockers.")


def gate(state: WizardState) -> list[Blocker]:
    if state.plan is None:
        return [
            Blocker(
                node_id=None,
                title="Destination not yet checked",
                detail="Run the existence check against the destination before continuing.",
                remedy="Click 'Check existence' above.",
            )
        ]
    return validate_plan(state.plan)
