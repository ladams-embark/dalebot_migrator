"""Confirm — owner remap, an automatic dry run, then the live gate.

Composed into Plan (dry-run review) and Run (live gate).

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

import json

import streamlit as st

from wdmigrator.api import (
    Action,
    Blocker,
    Level,
    NodeKind,
    ReportSharing,
    TIME_TRACKING_KINDS,
    TIME_TRACKING_SERVICE_NAME,
    evaluate_guards,
    iter_execute,
)
from wdmigrator.ui import safety_ui, theme
from wdmigrator.ui.runner import drain, start_job
from wdmigrator.ui.state import DEFAULT_REPORT_OWNER_USERNAME, WizardState, build_guard, owner_reference


_SHARING_LABELS = {
    ReportSharing.UNSHARED: (
        "Not shared (owner only) — the historical default"
    ),
    ReportSharing.SHARED_WITH_ALL_AUTHORIZED_USERS: (
        "Share with all authorized users"
    ),
}
_SHARING_HELP = {
    ReportSharing.UNSHARED: (
        "Only the report owner will see the report on the destination. Whoever "
        "adopts the report decides who else sees it later. The source's "
        "restricted-to security groups do not migrate — they are tenant-scoped."
    ),
    ReportSharing.SHARED_WITH_ALL_AUTHORIZED_USERS: (
        "Anyone with the domain access to see custom reports of this data "
        "source's class will see the report. Equivalent to 'Shared = True' "
        "with no restricted-to groups. Still cannot copy the source's specific "
        "security groups — those are tenant-scoped and stripped either way."
    ),
}

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


def _render_report_sharing(state: WizardState) -> None:
    """Radio for the run-level report sharing choice.

    Changing this changes the payload sent to the destination (``Shared`` and
    ``Enable_As_Worklet`` values), so the choice is captured here and the dry
    run is re-run afterwards — the ``dry_run_plan_hash`` reset below is what
    triggers that automatically on the next render.
    """
    theme.section(
        "Report sharing",
        "How every migrated report should land on the destination. Applies to "
        "every non-worklet report in the run; dashboard worklet reports are "
        "kept shared regardless — a worklet with Shared=False is rejected by "
        "the dashboard write.",
        eyebrow="Choose one",
    )
    options = list(_SHARING_LABELS)
    current = state.report_sharing if state.report_sharing in options else ReportSharing.UNSHARED
    chosen = st.radio(
        "Report sharing",
        options,
        index=options.index(current),
        format_func=lambda v: _SHARING_LABELS[v],
        help=(
            "Not shared: only the report's owner sees it on the destination. "
            "Share with all authorized users: anyone with domain access to "
            "the report's data source class sees it. Specific-groups sharing "
            "is not offered — those references are tenant-scoped and stripped "
            "for that reason."
        ),
        key="confirm_report_sharing",
        label_visibility="collapsed",
    )
    st.caption(_SHARING_HELP[chosen])
    if chosen is not state.report_sharing:
        state.report_sharing = chosen
        # A new sharing choice changes what gets written, so any prior dry run
        # is stale. Clearing the hash re-runs it on the next render, at which
        # point the reviewed-checkbox is un-ticked (it is bound to the fresh
        # results block).
        state.dry_run_plan_hash = ""
        state.dry_run_reviewed = False
        st.rerun()


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
        report_sharing=state.report_sharing,
    )
    state.dry_run_reviewed = False
    state.dry_run_records = []
    state.dry_run_plan_hash = ""
    state.dry_run_job = start_job(generator)
    drain(state.dry_run_job)
    if state.dry_run_job.error is not None:
        theme.banner("danger", "Dry run failed", str(state.dry_run_job.error))
        state.dry_run_job = None
        return
    state.dry_run_records = [p.record for p in state.dry_run_job.events]
    state.dry_run_plan_hash = state.plan.plan_hash()
    state.dry_run_job = None


def _plan_export_bytes(state: WizardState) -> bytes:
    plan = state.plan
    payload = {
        "plan_hash": plan.plan_hash(),
        "destination_tenant": state.dest.target.tenant if state.dest.target else None,
        "writes_planned": plan.writes_planned,
        "counts": plan.counts(),
        "nodes": [
            {
                "node_id": n.node_id,
                "kind": n.kind.value,
                "name": n.name,
                "action": plan.action_for(n).value,
                "existence": (
                    plan.existence[n.node_id].state.value
                    if n.node_id in plan.existence
                    else None
                ),
                "selected": n.selected,
                "required_by": sorted(n.required_by),
            }
            for n in plan.ordered_nodes
        ],
    }
    return json.dumps(payload, indent=2).encode("utf-8")


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


def render_plan_review(state: WizardState) -> None:
    """Owner remap, sharing, automatic dry run, downloadable plan."""
    if state.plan is None:
        return

    counts = state.plan.counts()
    theme.figures(
        [("Writes planned", state.plan.writes_planned)]
        + [(k.capitalize(), v) for k, v in counts.items()],
        tones={"Writes planned": "write"},
    )
    st.caption(f"Destination: `{state.dest.target.tenant}`")
    st.download_button(
        "Download plan (JSON)",
        data=_plan_export_bytes(state),
        file_name=f"migration-plan-{state.plan.plan_hash()}.json",
        mime="application/json",
        key="plan_download_json",
    )

    if _plan_has_report_creates(state):
        _render_owner_remap(state)
        _render_report_sharing(state)

    st.divider()
    theme.section(
        "Dry run",
        "Required before a live run, and pinned to this exact plan. Runs "
        "automatically — and re-runs itself if the plan changes. It never "
        "contacts the destination tenant.",
        eyebrow="Automatic",
    )

    plan_hash = state.plan.plan_hash()
    dry_run_stale = bool(state.dry_run_records) and state.dry_run_plan_hash != plan_hash
    if not state.dry_run_records or dry_run_stale:
        _run_dry_run(state)

    if state.dry_run_records and state.dry_run_plan_hash == state.plan.plan_hash():
        _render_dry_run_results(state)

    dry_guard = build_guard(state, dry_run=True)
    dry_findings = evaluate_guards(dry_guard)
    if dry_findings:
        with st.expander(f"What a live run would hit right now ({len(dry_findings)})"):
            theme.checklist([f"{g.title} — {g.detail}" for g in dry_findings])


def render_live_gate(state: WizardState) -> None:
    """Tenant-name, warning acknowledgements, irreversibility. No writes."""
    if state.plan is None:
        theme.banner("danger", "No plan", remedy="Go back to Plan.")
        return

    theme.section(
        "Live execution gate",
        "Everything below has to pass before the destination tenant is touched. "
        "This service has no delete operation — nothing written here can be undone by "
        "this tool, only by hand in the Workday UI, object by object.",
        eyebrow="Required before Start",
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


def render(state: WizardState) -> None:
    """Kept for tests that import confirm.render; the wizard uses Plan + Run."""
    st.header("Confirm")
    if state.plan is None:
        theme.banner("danger", "No plan", remedy="Go back to Plan.")
        return
    render_plan_review(state)
    st.divider()
    render_live_gate(state)


def gate_plan_review(state: WizardState) -> list[Blocker]:
    """Plan step: valid plan + reviewed dry run. Does not require the live gate."""
    blockers: list[Blocker] = []
    if state.plan is None:
        return [Blocker(None, "No plan", "Resolve conflicts before confirming.", "Stay on this step until the destination probe finishes.")]

    if state.plan.writes_planned == 0:
        blockers.append(Blocker(None, "Nothing to write", "Every object resolved to SKIP.", "Nothing to execute."))

    if state.dry_run_plan_hash != state.plan.plan_hash() or not state.dry_run_records:
        blockers.append(Blocker(None, "Dry run required", "No dry run has been run for this exact plan.", "The dry run starts automatically above."))
    elif not state.dry_run_reviewed:
        blockers.append(Blocker(None, "Dry run not reviewed", "The dry run output has not been marked reviewed.", "Check 'I have reviewed this dry run's output.'"))
    return blockers


def gate_live(state: WizardState) -> list[Blocker]:
    """Run step: tenant name, irreversibility, engine BLOCKs."""
    blockers: list[Blocker] = []
    if state.plan is None:
        return [Blocker(None, "No plan", "Resolve the plan before running.", "Go back to Plan.")]

    if state.dest.target is not None and state.confirmed_tenant_name.strip() != state.dest.target.tenant:
        blockers.append(Blocker(None, "Tenant name not confirmed", "The typed tenant name does not match the destination.", f"Type '{state.dest.target.tenant}' exactly."))

    if not state.irreversible_ack:
        blockers.append(Blocker(None, "Irreversibility not acknowledged", "The irreversibility checkbox is not checked.", "Check the box above."))

    live_guard = build_guard(state, dry_run=False)
    for g in evaluate_guards(live_guard):
        if g.level is Level.BLOCK:
            blockers.append(Blocker(None, g.title, g.detail, g.remedy))
    return blockers


def gate(state: WizardState) -> list[Blocker]:
    """Full confirm gate (plan review + live). Used by tests."""
    return gate_plan_review(state) + gate_live(state)
