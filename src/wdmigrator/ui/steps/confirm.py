"""Step 5: Confirm — owner remap, an automatic dry run, then the live gate.

This is where the plan's own hard rule lives: **live execution requires a
dry run that has already been run and reviewed for this exact plan hash.**
Any override made in Conflicts changes the plan hash, which invalidates a
prior dry run automatically — there is no way to review a dry run for one
plan and then execute a different one.

The dry run now runs automatically as soon as a plan is ready, and again
whenever the plan hash changes — there is no "Run dry run" button to click.
That is safe to do without asking: the engine's dry-run write path doesn't
call ``assert_write_allowed`` at all, and never contacts the destination (see
``writer.py`` — it builds and serializes the real SOAP envelope through
zeep's binding without sending it). Automating *that* click removes pure
mechanical friction; it does not relax the gate one step down, which still
requires the dry run's output to have been reviewed, the destination tenant
name retyped, and irreversibility acknowledged before Execute will start a
live run — this service has no delete operation, so those three stay manual
on purpose.

``evaluate_guards()`` is still called here in dry-run mode purely for display
(it always reports the same-tenant finding, even in dry run) so the user sees
ahead of time what will block a live run later.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import (
    Action,
    Blocker,
    Level,
    NodeKind,
    TIME_TRACKING_KINDS,
    TIME_TRACKING_SERVICE_NAME,
    evaluate_guards,
    iter_execute,
)
from wdmigrator.ui import safety_ui, theme
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import DEFAULT_REPORT_OWNER_USERNAME, WizardState, build_guard, owner_reference

STEP_ID = "confirm"


def _plan_has_report_creates(state: WizardState) -> bool:
    plan = state.plan
    return any(
        node.kind is NodeKind.REPORT and plan.action_for(node) is Action.CREATE
        for node in plan.ordered_nodes
    )


def _render_owner_remap(state: WizardState) -> None:
    theme.section("Report owner", eyebrow="Remapped automatically")
    theme.checklist(
        [
            "Reports carry a System_User_Reference for their owner, and the source "
            "owner almost certainly doesn't exist in the destination tenant.",
            f"Every report this tool creates is owned by {DEFAULT_REPORT_OWNER_USERNAME} "
            "on the destination, resolved by WorkdayUserName at write time.",
        ]
    )


def _run_dry_run(state: WizardState) -> None:
    guard = build_guard(state, dry_run=True)
    tt_connection = (
        state.dest.connection.for_service(TIME_TRACKING_SERVICE_NAME)
        if any(
            n.kind in TIME_TRACKING_KINDS for n in state.plan.ordered_nodes
        )
        else None
    )
    generator = iter_execute(
        state.dest.connection, state.plan, guard,
        owner_reference=owner_reference(state), stop_on_failure=False,
        tt_connection=tt_connection,
    )
    state.dry_run_job = start_job(generator)
    state.dry_run_records = []
    state.dry_run_reviewed = False
    state.dry_run_plan_hash = ""


def _pump_dry_run(state: WizardState) -> None:
    job = state.dry_run_job
    pump(job, time_budget=0.8)
    last = job.last_event
    fraction = last.fraction if last is not None else 0.0
    render_job_progress(job, label="Dry run", fraction=fraction)

    if job.error is not None:
        state.dry_run_job = None
    elif job.done:
        state.dry_run_records = [p.record for p in job.events]
        state.dry_run_plan_hash = state.plan.plan_hash()
        state.dry_run_job = None
        st.rerun()
    else:
        st.rerun()


def _render_dry_run_results(state: WizardState) -> None:
    theme.banner(
        "info",
        f"{len(state.dry_run_records)} object(s) serialized",
        "No calls were made to the destination tenant.",
    )
    rows = [
        {
            "name": r.name,
            "kind": r.kind,
            "action": r.action.value,
            "status": r.status.value,
        }
        for r in state.dry_run_records
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    with st.expander("Inspect a serialized envelope"):
        # Keyed by node_id, not by name. Report names are not unique in Workday
        # (7 of 999 sampled reports shared one), so a name-keyed lookup would
        # quietly show the first match's envelope while the user believed they
        # were reviewing the second — on the screen whose entire job is
        # confirming what is about to be written.
        by_node = {r.node_id: r for r in state.dry_run_records}
        if by_node:
            choice = st.selectbox(
                "Object",
                list(by_node),
                format_func=lambda node_id: by_node[node_id].name or node_id,
                key="dry_run_envelope_choice",
            )
            st.code(by_node[choice].envelope or "(no envelope)", language="xml")

    state.dry_run_reviewed = st.checkbox(
        "I have reviewed this dry run's output.",
        value=state.dry_run_reviewed,
        key="dry_run_reviewed_ack",
    )


def render(state: WizardState) -> None:
    st.header("Confirm")

    if state.plan is None:
        theme.banner("danger", "No plan", remedy="Go back to Conflicts.")
        return

    counts = state.plan.counts()
    theme.figures(
        [("Writes planned", state.plan.writes_planned)]
        + [(k.capitalize(), v) for k, v in counts.items()],
        tones={"Writes planned": "write"},
    )
    st.caption(f"Destination: `{state.dest.target.tenant}`")

    if _plan_has_report_creates(state):
        _render_owner_remap(state)

    st.divider()
    theme.section(
        "Dry run",
        "Required before a live run, and pinned to this exact plan. Runs "
        "automatically below — and re-runs itself if you go back and change the "
        "plan — so there's nothing to click here. It never contacts the "
        "destination tenant.",
        eyebrow="Automatic",
    )

    plan_hash = state.plan.plan_hash()
    dry_run_stale = bool(state.dry_run_records) and state.dry_run_plan_hash != plan_hash
    if state.dry_run_job is None and (not state.dry_run_records or dry_run_stale):
        _run_dry_run(state)
        st.rerun()

    if state.dry_run_job is not None:
        _pump_dry_run(state)
    elif state.dry_run_records and state.dry_run_plan_hash == plan_hash:
        _render_dry_run_results(state)

    dry_guard = build_guard(state, dry_run=True)
    dry_findings = evaluate_guards(dry_guard)
    if dry_findings:
        with st.expander(f"What a live run would hit right now ({len(dry_findings)})"):
            theme.checklist([f"{g.title} — {g.detail}" for g in dry_findings])

    st.divider()
    theme.section(
        "Live execution gate",
        "Everything below has to pass before the destination tenant is touched. "
        "This service has no delete operation — nothing written here can be undone by "
        "this tool, only by hand in the Workday UI, object by object.",
        eyebrow="Step 2 of 2",
    )
    state.confirmed_tenant_name = st.text_input(
        f"Type the destination tenant name to confirm (`{state.dest.target.tenant}`)",
        value=state.confirmed_tenant_name, key="confirm_tenant_name",
    )

    live_guard = build_guard(state, dry_run=False)
    guards = evaluate_guards(live_guard)
    acknowledged = safety_ui.render_guards(guards, key_prefix="confirm_")
    state.warnings_acknowledged |= acknowledged

    state.irreversible_ack = st.checkbox(
        f"I understand this will write {state.plan.writes_planned} object(s) to "
        f"`{state.dest.target.tenant}` and that this cannot be undone by this tool.",
        value=state.irreversible_ack, key="confirm_irreversible_ack",
    )


def gate(state: WizardState) -> list[Blocker]:
    blockers: list[Blocker] = []
    if state.plan is None:
        return [Blocker(None, "No plan", "Resolve conflicts before confirming.", "Go back to Conflicts.")]

    if state.plan.writes_planned == 0:
        blockers.append(Blocker(None, "Nothing to write", "Every object resolved to SKIP.", "Nothing to execute."))

    if state.dry_run_plan_hash != state.plan.plan_hash() or not state.dry_run_records:
        blockers.append(Blocker(None, "Dry run required", "No dry run has been run for this exact plan.", "Run the dry run above."))
    elif not state.dry_run_reviewed:
        blockers.append(Blocker(None, "Dry run not reviewed", "The dry run output has not been marked reviewed.", "Check 'I have reviewed this dry run's output.'"))

    if state.dest.target is not None and state.confirmed_tenant_name.strip() != state.dest.target.tenant:
        blockers.append(Blocker(None, "Tenant name not confirmed", "The typed tenant name does not match the destination.", f"Type '{state.dest.target.tenant}' exactly."))

    if not state.irreversible_ack:
        blockers.append(Blocker(None, "Irreversibility not acknowledged", "The irreversibility checkbox is not checked.", "Check the box above."))

    live_guard = build_guard(state, dry_run=False)
    for g in evaluate_guards(live_guard):
        if g.level is Level.BLOCK:
            blockers.append(Blocker(None, g.title, g.detail, g.remedy))

    return blockers
