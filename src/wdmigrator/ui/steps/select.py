"""Step 2: Select — build source indexes, pick reports and/or calculated fields.

Server-side report search is exact-match only — substring queries return
zero hits, a documented Workday limitation, not a shortcut we chose (see
CLAUDE.md). A local index is therefore the only way to browse reports, and
building it once and caching it to disk is the whole strategy, not an
optimization on top of something faster.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from wdmigrator.api import (
    Blocker,
    LookupOutcome,
    cache_path,
    iter_calculated_field_index,
    iter_report_index,
    load_index,
    lookup_report,
    lookup_report_by_name,
    save_index,
)
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import WizardState

STEP_ID = "select"

_CF_MAX_RESULTS = 500
_REPORT_MAX_ROWS = 5000


def _load_or_prompt_index(state, *, kind, iterator_fn, job_attr, index_attr, label, connection):
    index = getattr(state, index_attr)
    job = getattr(state, job_attr)

    if index is None and job is None:
        cached = load_index(cache_path(connection, kind), tenant=connection.target.tenant)
        if cached is not None:
            setattr(state, index_attr, cached)
            index = cached

    col1, col2 = st.columns([3, 1])
    with col1:
        st.write(f"**{label} index**: {'not built' if index is None else f'{len(index)} items (cached)' if job is None else 'building…'}")
    with col2:
        button_label = "Rebuild" if index is not None else "Build"
        if st.button(f"{button_label} {label.lower()} index", key=f"build_{kind}"):
            setattr(state, job_attr, start_job(iterator_fn(connection)))
            setattr(state, index_attr, None)
            st.rerun()

    job = getattr(state, job_attr)
    if job is not None:
        pump(job, time_budget=0.8)
        last = job.last_event
        fraction = last.fraction if last is not None else 0.0
        render_job_progress(job, label=f"{label} index", fraction=fraction)
        if job.error is not None:
            setattr(state, job_attr, None)
            st.rerun()
        elif job.done:
            built = job.events[-1].index if job.events else None
            setattr(state, index_attr, built)
            setattr(state, job_attr, None)
            if built is not None:
                save_index(built, cache_path(connection, kind))
            st.rerun()
        else:
            st.rerun()

    return getattr(state, index_attr)


def _render_calculated_fields(state: WizardState) -> None:
    st.subheader("Calculated fields")
    st.caption(
        "Default is to select nothing here — dependency resolution in the "
        "next step automatically pulls in any calculated field a selected "
        "report references."
    )
    if state.cf_index is None:
        st.info("Build the calculated field index above to search directly.")
        return

    query = st.text_input("Search by name (substring)", key="cf_search")
    if query:
        matches = [
            (wid, summary)
            for wid, summary in state.cf_index.summaries.items()
            if summary.name and query.lower() in summary.name.lower()
        ][:_CF_MAX_RESULTS]
        if not matches:
            st.caption("No matches.")
        else:
            df = pd.DataFrame(
                [{"wid": wid, "name": s.name, "class": s.class_name} for wid, s in matches]
            )
            event = st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
                key="cf_search_table",
            )
            rows = event.selection["rows"] if event and event.selection else []
            if st.button("Add selected calculated fields", key="cf_add", disabled=not rows):
                for i in rows:
                    state.selected_field_wids.add(df.iloc[i]["wid"])
                st.rerun()

    if state.selected_field_wids:
        st.write(f"Selected calculated fields: {len(state.selected_field_wids)}")
        if st.button("Clear calculated field selections", key="cf_clear"):
            state.selected_field_wids.clear()
            st.rerun()


def _render_reports(state: WizardState) -> None:
    st.subheader("Reports")

    with st.expander("Add by exact name"):
        name = st.text_input("Exact report name", key="report_exact_name")
        if st.button("Look up", key="report_exact_lookup") and name:
            # lookup_report_by_name deliberately fetches no data — it exists
            # for the cheap destination existence probe in Conflicts, not for
            # pulling a report to migrate. Once it's resolved a unique WID
            # (handling the "name isn't unique" case safely), a second
            # targeted fetch by WID gets the full definition, including the
            # Name that the closure and every downstream step need.
            result = lookup_report_by_name(state.source.connection, name)
            if result.outcome is LookupOutcome.FOUND:
                full = lookup_report(state.source.connection, wid=result.wid)
                if full.outcome is LookupOutcome.FOUND and full.data is not None:
                    state.selected_reports_manual[full.wid] = full.data
                    st.success(f"Added '{name}'.")
                else:
                    st.error(
                        f"Found '{name}' by name but could not fetch its full "
                        f"definition: {full.fault or full.outcome.value}"
                    )
            elif result.outcome is LookupOutcome.NOT_FOUND:
                st.error(f"No report named exactly '{name}'.")
            else:
                st.error(
                    f"'{name}' is ambiguous — multiple reports share this name. "
                    "Report names are not guaranteed unique; use the index table instead, "
                    "where each row is a distinct WID."
                )

    if state.report_index is not None:
        summaries = list(state.report_index.summaries.items())[:_REPORT_MAX_ROWS]
        df = pd.DataFrame([{"wid": wid, "name": s.name, "owner": s.owner} for wid, s in summaries])
        query = st.text_input("Filter by name (substring, local)", key="report_filter")
        if query:
            df = df[df["name"].fillna("").str.contains(query, case=False)]
        event = st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="multi-row",
            key="report_table_select",
        )
        rows = event.selection["rows"] if event and event.selection else []
        table_selected = {}
        for i in rows:
            wid = df.iloc[i]["wid"]
            payload = state.report_index.payload(wid)
            if payload is not None:
                table_selected[wid] = payload
        state.selected_reports = {**state.selected_reports_manual, **table_selected}
    else:
        state.selected_reports = dict(state.selected_reports_manual)
        st.info("Build the report index above (~158s) to browse and multi-select reports by table.")

    if state.selected_reports:
        st.write(f"Selected reports: {len(state.selected_reports)}")
        if st.button("Clear report selections", key="report_clear"):
            state.selected_reports_manual = {}
            state.selected_reports = {}
            st.rerun()


def render(state: WizardState) -> None:
    st.header("Select")
    connection = state.source.connection
    if connection is None:
        st.error("Source is not connected — go back to Connect.")
        return

    _load_or_prompt_index(
        state,
        kind="calculated_field",
        iterator_fn=iter_calculated_field_index,
        job_attr="cf_index_job",
        index_attr="cf_index",
        label="Calculated field",
        connection=connection,
    )
    _load_or_prompt_index(
        state,
        kind="report",
        iterator_fn=iter_report_index,
        job_attr="report_index_job",
        index_attr="report_index",
        label="Report",
        connection=connection,
    )

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        _render_calculated_fields(state)
    with col2:
        _render_reports(state)


def gate(state: WizardState) -> list[Blocker]:
    blockers = []
    if not state.selected_field_wids and not state.selected_reports:
        blockers.append(
            Blocker(
                node_id=None,
                title="Nothing selected",
                detail="Select at least one report or calculated field to migrate.",
                remedy="Pick from the calculated field search or the report table/exact-name lookup.",
            )
        )
    if state.cf_index is None:
        blockers.append(
            Blocker(
                node_id=None,
                title="Calculated field index not built",
                detail=(
                    "Resolving dependencies needs the complete source calculated-field "
                    "index, even if you only selected reports — every WID a report "
                    "references has to be classified against it."
                ),
                remedy="Build the calculated field index above (~25s).",
            )
        )
    return blockers
