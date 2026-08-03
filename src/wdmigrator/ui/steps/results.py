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

from wdmigrator.api import Blocker, summarise
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
    label = "Live execution" if state.execute_records else "Dry run (no live execution has been run)"
    if not records:
        st.info("Nothing has been run yet.")
        return

    st.subheader(label)
    counts = summarise(records)
    st.write(", ".join(f"{v} {k}" for k, v in counts.items() if v))

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
        )
    with col2:
        st.download_button(
            "Download results (JSON)", data=_records_to_json(records),
            file_name="migration_results.json", mime="application/json",
        )
    wid_map = _wid_map_from(records)
    with col3:
        if wid_map:
            st.download_button(
                "Download WID map (JSON)",
                data=json.dumps(wid_map, indent=2).encode("utf-8"),
                file_name="wid_map.json", mime="application/json",
            )

    st.divider()
    if st.button("Start a new migration", key="results_restart"):
        reset_downstream(state, from_step="select")
        state.step = "select"
        st.rerun()


def gate(state: WizardState) -> list[Blocker]:
    return []
