"""Step 7: Results — outcome summary, per-object detail, exports.

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

import streamlit as st

from wdmigrator.api import Blocker, VerifyStatus, iter_verify, summarise, summarise_verify
from wdmigrator.ui import theme
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import WizardState, reset_downstream

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


def render(state: WizardState) -> None:
    st.header("Results")

    records = state.execute_records or state.dry_run_records
    if not records:
        theme.banner("neutral", "Nothing has been run yet",
                     "Results appear here after a dry run or a live execution.")
        return

    is_live = bool(state.execute_records)
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

    if is_live:
        st.divider()
        _render_verify(state)

    st.divider()
    if st.button("Start a new migration", key="results_restart"):
        reset_downstream(state, from_step="select")
        state.step = "select"
        st.rerun()


def _render_verify(state: WizardState) -> None:
    """Read every written object back and compare structural signals.

    The writer's SUCCESS bit has been observed to lie — HANDOFF names the
    "0 failed" run in which two of three dashboards came back as empty
    shells. Only a read-back caught it, and every migration since has run
    one by hand. This is that check, wired to a button.
    """
    theme.section(
        "Verification",
        "Read every written object back from the destination and compare "
        "structural signals to the source (tab count, worklet count, member "
        "count, columns). The writer's own success bit has reported clean "
        "runs where dashboards came back as empty shells — this is the "
        "check that catches that.",
        eyebrow="Post-run read-back",
    )

    if state.verify_job is not None:
        pump(state.verify_job, time_budget=0.8)
        last = state.verify_job.last_event
        fraction = last.fraction if last is not None else 0.0
        render_job_progress(state.verify_job, label="Verification", fraction=fraction)
        if state.verify_job.error is not None:
            state.verify_job = None
            st.rerun()
        elif state.verify_job.done:
            state.verify_records = [event.record for event in state.verify_job.events]
            state.verify_job = None
            st.rerun()
        else:
            st.rerun()
        return

    if not state.verify_records:
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
        st.rerun()


def gate(state: WizardState) -> list[Blocker]:
    return []
