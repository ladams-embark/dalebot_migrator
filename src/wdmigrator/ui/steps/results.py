"""Results — outcome summary, per-object detail, exports, and a run log.

Shows whichever run is most recent: a live execution if one happened, else
the last dry run. Export bytes are built straight from ``state.*_records``
each render — at the scale this app deals with (a closure's worth of
objects, not the full 9,652/5,153 tenant volume), that's cheap enough not to
need session-state caching before ``st.download_button``'s own rerun.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

import pandas as pd

from wdmigrator.api import (
    Action,
    Blocker,
    ReferenceAction,
    ReferenceDecision,
    TIME_TRACKING_KINDS,
    TIME_TRACKING_SERVICE_NAME,
    VerifyStatus,
    build_plan,
    find_nodes_using_reference_values,
    iter_check_existence,
    iter_execute,
    iter_verify,
    summarise,
    summarise_verify,
)
from wdmigrator.ui import theme
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.indexes import destination_match_indexes
from wdmigrator.ui.runner import READ_TIME_BUDGET, WRITE_TIME_BUDGET, pump, start_job
from wdmigrator.ui.state import WizardState, build_guard, owner_reference, reset_downstream

STEP_ID = "results"


def _records_to_csv(records) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["node_id", "kind", "name", "reference_id", "action", "status", "dest_wid", "fault"])
    for r in records:
        writer.writerow(
            [r.node_id, r.kind, r.name, r.reference_id, r.action.value, r.status.value, r.dest_wid, r.fault]
        )
    return buf.getvalue().encode("utf-8")


def _records_to_json(records) -> bytes:
    return json.dumps(
        [
            {
                "node_id": r.node_id,
                "kind": r.kind,
                "name": r.name,
                "reference_id": r.reference_id,
                "action": r.action.value,
                "status": r.status.value,
                "dest_wid": r.dest_wid,
                "exceptions": [{"classification": e.classification, "message": e.message} for e in r.exceptions],
                "fault": r.fault,
                "dry_run": r.dry_run,
            }
            for r in records
        ],
        indent=2,
    ).encode("utf-8")


def _wid_map_from(records) -> dict:
    return {r.reference_id or r.node_id: r.dest_wid for r in records if r.dest_wid}


def _dest_can_read(state: WizardState) -> bool:
    """Live SOAP client has ``.service``. AppTest stubs omit it on purpose."""
    connection = state.dest.connection
    return connection is not None and getattr(connection, "service", None) is not None


def _write_run_log(state: WizardState, records) -> None:
    """Always persist a run log under ``out/``. Once per set of records."""
    if state.run_log_path:
        return
    out = Path("out")
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"migration-{stamp}.json"
    payload = {
        "written_at": stamp,
        "live": bool(state.execute_records),
        "destination_tenant": (
            state.dest.target.tenant if state.dest.target is not None else None
        ),
        "plan_hash": state.plan.plan_hash() if state.plan is not None else "",
        "run_log_path": str(path),
        "records": json.loads(_records_to_json(records).decode("utf-8")),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    state.run_log_path = str(path)


def render(state: WizardState) -> None:
    st.header("Results")

    records = state.execute_records or state.dry_run_records
    if not records:
        theme.banner("neutral", "Nothing has been run yet",
                     "A dry run or live execution shows up here.")
        return

    _write_run_log(state, records)

    is_live = bool(state.execute_records)
    indeterminate = [r for r in records if getattr(r, "needs_reprobe", False)]
    if indeterminate:
        theme.banner(
            "danger",
            f"{len(indeterminate)} object(s) ended INDETERMINATE",
            "A PUT timed out or the transport failed after the request left "
            "this tool. The destination may already hold the object. This is "
            "not a skip — do not retry those writes without re-probing, or "
            "you can create a duplicate this service cannot delete.",
            remedy="Go back to Run and use Re-probe and resume, or re-check "
                   "the objects in Workday before writing again.",
        )

    theme.section(
        "Live execution" if is_live else "Dry run",
        None if is_live else "No live execution has been run — these are serialized "
                             "payloads, not writes.",
        eyebrow="Wrote to the destination" if is_live else "Nothing was written",
    )

    counts = summarise(records)
    shown = {k: v for k, v in counts.items() if v}
    theme.figures(
        list(shown.items()),
        tones={k: "danger" for k in shown if k in ("failed", "indeterminate")},
    )

    rows = [
        {
            "name": r.name,
            "kind": r.kind,
            "action": r.action.value,
            "status": r.status.value,
            "dest_wid": r.dest_wid,
            "exceptions": "; ".join(f"{e.classification}: {e.message}" for e in r.exceptions) or None,
            "fault": r.fault,
        }
        for r in records
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button(
            "Download results (CSV)", data=_records_to_csv(records),
            file_name="migration_results.csv", mime="text/csv",
            use_container_width=True,
        )
    with col2:
        st.download_button(
            "Download results (JSON)", data=_records_to_json(records),
            file_name="migration_results.json", mime="application/json",
            use_container_width=True,
        )
    wid_map = _wid_map_from(records)
    with col3:
        if wid_map:
            st.download_button(
                "Download WID map (JSON)",
                data=json.dumps(wid_map, indent=2).encode("utf-8"),
                file_name="wid_map.json", mime="application/json",
                use_container_width=True,
            )

    if state.run_log_path:
        st.caption(f"Run log written to `{state.run_log_path}`")

    if is_live:
        st.divider()
        _render_restore(state)
        st.divider()
        _render_verify(state)

    st.divider()
    if st.button("Start a new migration", key="results_restart"):
        reset_downstream(state, from_step="select")
        state.step = "scope"
        st.rerun()


def _preflight_rows_for_restore(state: WizardState) -> list[dict]:
    """One row per preflight-discovered reference. Existing REPLACE decisions
    come back with their values pre-filled, so an in-progress restore session
    is not lost across reruns."""
    rows = []
    for value, info in state.blocking_references.items():
        if not info.get("preflight"):
            continue
        existing = state.reference_decisions.get(value)
        biz = info.get("business") or {}
        default_type = next(iter(biz), "")
        rows.append({
            "Object": info["node_name"],
            "Element": ", ".join(info.get("elements") or []) or "(unknown)",
            "Source value": ", ".join(f"{k}={v}" for k, v in biz.items()) or value,
            "Also on": ", ".join(info.get("other_objects") or []) or "—",
            "Restore as (ID type)": (
                (existing.replacement_type if existing else None) or default_type
            ),
            "Restore as (value on dest)": (
                (existing.replacement_value if existing else None) or ""
            ),
            "_wid": value,
        })
    return rows


def _apply_restore_edits(state: WizardState, rows, edited) -> set[str]:
    """Fold edited rows back into ``state.reference_decisions``.

    Positional match to ``rows`` — same rule as the pre-execution table on
    Execute: never trust that a hidden column round-trips through the editor.

    Returns the source WIDs that ended up as REPLACE. Rows left blank stay
    BLANK (unchanged from the initial run's preflight default), so nothing
    the user did not touch changes.
    """
    replaced: set[str] = set()
    for row, source in zip(edited.to_dict("records"), rows):
        wid = source["_wid"]
        rtype = row["Restore as (ID type)"]
        rvalue = row["Restore as (value on dest)"]
        if rtype and rvalue:
            state.reference_decisions[wid] = ReferenceDecision(
                source_wid=wid,
                action=ReferenceAction.REPLACE,
                replacement_type=rtype,
                replacement_value=rvalue,
                note="Restored on destination via post-run Results step.",
            )
            replaced.add(wid)
        else:
            # Empty row: fall back to the preflight default (BLANK for tenant
            # data, KEEP for delivered content). Reading the original default
            # off ``blocking_references`` — writing a hard-coded BLANK here
            # would silently strip prompt defaults on delivered references
            # (event classifications etc.) that the initial pass correctly
            # KEPT.
            info = state.blocking_references.get(wid) or {}
            default = info.get("default_action") or ReferenceAction.BLANK.value
            try:
                action = ReferenceAction(default)
            except ValueError:
                action = ReferenceAction.BLANK
            if action is ReferenceAction.REPLACE:
                # REPLACE-required rows are never restored to REPLACE without
                # a value here — that would break the invariant. Fall back to
                # BLANK; the row will fail its own required-field check.
                action = ReferenceAction.BLANK
            state.reference_decisions[wid] = ReferenceDecision(
                source_wid=wid,
                action=action,
                note=(
                    "Preflight default kept (delivered content passes through)."
                    if action is ReferenceAction.KEEP
                    else "Preflight default: always-tenant-data (kept blank)."
                ),
            )
    return replaced


def _start_restore_reprobe(state: WizardState, replaced: set[str]) -> None:
    """Kick off a scoped destination re-probe for the restore pass.

    Only the nodes that name any newly-replaced WID need to become UPDATEs —
    everything else stays SKIP because it was written correctly the first
    time. The re-probe still runs across the whole closure so we pick up the
    fresh ``dest_wid`` for every FOUND object; the ``restore_update_node_ids``
    set narrows which ones flip to UPDATE afterward.
    """
    state.restore_update_node_ids = find_nodes_using_reference_values(
        state.plan.ordered_nodes, replaced
    )
    tt_connection = (
        state.dest.connection.for_service(TIME_TRACKING_SERVICE_NAME)
        if any(n.kind in TIME_TRACKING_KINDS for n in state.plan.ordered_nodes)
        else None
    )
    state.restore_reprobe_job = start_job(
        iter_check_existence(
            state.dest.connection,
            state.closure,
            tt_connection=tt_connection,
            **destination_match_indexes(state),
        )
    )
    state.restore_records = []


def _pump_restore_reprobe(state: WizardState) -> None:
    job = state.restore_reprobe_job
    pump(job, time_budget=READ_TIME_BUDGET)
    last = job.last_event
    render_job_progress(
        job,
        label="Restore — re-probing destination",
        fraction=last.fraction if last is not None else 0.0,
    )
    if job.error is not None:
        state.restore_reprobe_job = None
        return
    if not job.done:
        st.rerun()
        return

    existence = {p.node.node_id: p.existence for p in job.events}
    # Overrides: only the nodes that name a restored reference become UPDATE.
    # Existing overrides (e.g. shell-dashboard UPDATE from the initial run)
    # are preserved so the restore does not silently downgrade them.
    overrides = dict(state.action_overrides)
    for node_id in state.restore_update_node_ids:
        overrides[node_id] = Action.UPDATE
    state.plan = build_plan(
        state.closure,
        existence,
        overrides=overrides,
        reference_decisions=state.reference_decisions,
    )
    # Re-stamp the dry-run hash so the guard on the second pass still holds —
    # same rationale as the mid-run reference-decision table on Execute: the
    # visible mapping IS the review.
    state.dry_run_plan_hash = state.plan.plan_hash()
    state.restore_reprobe_job = None
    # Start the restore execute pass immediately — the mapping table already
    # was the review, so we don't drop the user back at a "Start" click.
    guard = build_guard(state, dry_run=False)
    tt_connection = (
        state.dest.connection.for_service(TIME_TRACKING_SERVICE_NAME)
        if any(n.kind in TIME_TRACKING_KINDS for n in state.plan.ordered_nodes)
        else None
    )
    state.restore_execute_job = start_job(
        iter_execute(
            state.dest.connection, state.plan, guard,
            owner_reference=owner_reference(state), stop_on_failure=True,
            tt_connection=tt_connection,
            report_sharing=state.report_sharing,
        )
    )
    st.rerun()


def _pump_restore_execute(state: WizardState) -> None:
    job = state.restore_execute_job
    pump(job, time_budget=WRITE_TIME_BUDGET, batch_size=1)
    last = job.last_event
    render_job_progress(
        job,
        label="Restore — writing UPDATEs",
        fraction=last.fraction if last is not None else 0.0,
    )
    if job.error is not None:
        state.restore_records = [p.record for p in job.events]
        state.restore_execute_job = None
        st.rerun()
    elif job.done:
        state.restore_records = [p.record for p in job.events]
        state.restore_execute_job = None
        st.rerun()
    else:
        st.rerun()


def _render_restore(state: WizardState) -> None:
    """Optional post-run pass: restore Instance_Reference defaults with real
    destination business ids.

    The initial run blanked every always-tenant-data reference (Preflight's
    safe default). Some of those genuinely SHOULD have a destination value —
    a report whose company-picker default should point at *this* tenant's
    top-level company, for example. This section lets the user fill in that
    value, and the writer does an UPDATE on only the objects that carry it.

    Skipped entirely when Preflight found nothing (no rows), or when the
    initial run wrote nothing that could still be updated.
    """
    rows = _preflight_rows_for_restore(state)
    if not rows:
        return

    theme.section(
        "Restore prompt defaults on the destination",
        "Preflight blanked every reference below because it names data from "
        "the source tenant. If a destination value exists for one — say a "
        "top-level company, or a Workday Release — enter its business id and "
        "the corresponding object will be UPDATE-written to carry it. Rows "
        "left blank stay blank; the initial write is untouched.",
        eyebrow="Optional",
    )

    if state.restore_reprobe_job is not None:
        _pump_restore_reprobe(state)
        return
    if state.restore_execute_job is not None:
        _pump_restore_execute(state)
        return

    if state.restore_records:
        counts = summarise(state.restore_records)
        shown = {k: v for k, v in counts.items() if v}
        theme.figures(
            list(shown.items()),
            tones={k: "danger" for k in shown if k in ("failed", "indeterminate")},
        )
        touched = [
            {
                "name": r.name,
                "kind": r.kind,
                "action": r.action.value,
                "status": r.status.value,
                "fault": r.fault,
            }
            for r in state.restore_records
            if r.action is Action.UPDATE or r.fault
        ]
        if touched:
            st.dataframe(touched, use_container_width=True, hide_index=True)
        else:
            st.caption(
                "No objects needed an UPDATE — either every row was left "
                "blank, or the affected objects had already picked up the "
                "restored values."
            )
        st.caption(
            "Restoration finished. You can edit values below and click "
            "Restore again to apply additional changes."
        )

    edited = st.data_editor(
        pd.DataFrame(rows).drop(columns=["_wid"]),
        hide_index=True,
        use_container_width=True,
        disabled=["Object", "Element", "Source value", "Also on"],
        column_config={
            "Restore as (ID type)": st.column_config.TextColumn(
                help="Business-id type on the destination — e.g. "
                     "Organization_Reference_ID, Workday_Release_ID. Usually "
                     "the same type as the Source value column shows."
            ),
            "Restore as (value on dest)": st.column_config.TextColumn(
                help="The business id in the DESTINATION tenant. Look it up "
                     "in Workday; there is no generic 'list candidates' API."
            ),
        },
        key="restore_table_editor",
    )

    incomplete = [
        r["Object"] for r in edited.to_dict("records")
        if bool(r["Restore as (ID type)"]) != bool(r["Restore as (value on dest)"])
    ]
    if incomplete:
        theme.banner(
            "warning",
            f"{len(incomplete)} row(s) partially filled",
            "Fill in both the ID type and the value, or clear both to leave "
            "the row blank.",
        )

    if st.button(
        "Restore selected values on destination",
        key="restore_apply",
        type="primary",
        disabled=bool(incomplete),
    ):
        replaced = _apply_restore_edits(state, rows, edited)
        if not replaced:
            theme.banner(
                "info",
                "Nothing to restore",
                "Every row was left blank. No UPDATE run started.",
            )
            return
        _start_restore_reprobe(state, replaced)
        st.rerun()

    st.caption(
        "Re-probes the destination first so already-existing objects come "
        "back with their destination WIDs, then UPDATEs only the ones "
        "carrying your restored values. Everything else stays SKIP."
    )


def _render_verify(state: WizardState) -> None:
    """Read every written object back and compare structural signals.

    The writer's SUCCESS bit has been observed to lie — HANDOFF names the
    "0 failed" run in which two of three dashboards came back as empty
    shells. Only a read-back caught it, and every migration since has run
    one by hand. This is that check, wired to a button.
    """
    theme.section(
        "Verification",
        "Reads written objects back and compares structure to the source.",
        eyebrow="Post-run read-back",
    )

    if state.verify_job is not None:
        if state.verify_job.error is not None:
            theme.banner(
                "danger",
                "Verification failed",
                str(state.verify_job.error),
                remedy="Re-verify below once the destination is reachable.",
            )
            if st.button("Re-verify", key="verify_rerun_after_error"):
                state.verify_job = None
                state.verify_records = []
                st.rerun()
            return
        pump(state.verify_job, time_budget=READ_TIME_BUDGET)
        last = state.verify_job.last_event
        fraction = last.fraction if last is not None else 0.0
        render_job_progress(state.verify_job, label="Verification", fraction=fraction)
        if state.verify_job.done:
            state.verify_records = [event.record for event in state.verify_job.events]
            state.verify_job = None
            st.rerun()
        else:
            st.rerun()
        return

    if not state.verify_records:
        if _dest_can_read(state):
            state.verify_job = start_job(
                iter_verify(state.dest.connection, state.plan.ordered_nodes, state.execute_records)
            )
            st.rerun()
            return
        if st.button("Verify against destination", key="verify_start", type="primary"):
            state.verify_job = start_job(
                iter_verify(state.dest.connection, state.plan.ordered_nodes, state.execute_records)
            )
            st.rerun()
        return

    counts = summarise_verify(state.verify_records)
    shown = {k: v for k, v in counts.items() if v}
    theme.figures(
        list(shown.items()),
        tones={
            VerifyStatus.MISMATCH.value: "danger",
            VerifyStatus.MISSING.value: "danger",
            VerifyStatus.ERROR.value: "danger",
        },
    )

    problems = [r for r in state.verify_records if not r.ok and r.status is not VerifyStatus.SKIPPED]
    if problems:
        theme.banner(
            "danger",
            f"{len(problems)} object(s) did not verify",
            "Read-back disagreed with the source or the destination reports "
            "the object missing. Each row below shows which signals differ.",
        )
        st.dataframe(
            [
                {
                    "name": r.name,
                    "kind": r.kind,
                    "status": r.status.value,
                    "findings": "; ".join(str(f) for f in r.findings) or r.fault or "",
                }
                for r in problems
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        theme.banner(
            "success",
            "Every written object verified against the source",
        )

    if st.button("Re-verify", key="verify_rerun"):
        state.verify_records = []
        state.verify_job = None
        st.rerun()


def gate(state: WizardState) -> list[Blocker]:
    return []
