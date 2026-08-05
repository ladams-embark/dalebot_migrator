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
from wdmigrator.ui import theme
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.runner import pump, start_job
from wdmigrator.ui.state import WizardState

STEP_ID = "select"

# Render caps. Both are below live tenant volumes (~9,650 calculated fields /
# ~5,150 reports on commitconsulting_dpt1), so both genuinely truncate — and a
# picker that silently hides rows is dangerous in a tool where picking the
# wrong object cannot be undone. Every truncation is called out in the UI.
_CF_MAX_RESULTS = 500
_REPORT_MAX_ROWS = 5000

#: Measured live against commitconsulting_dpt1 (~9,650 fields / ~5,150 reports
#: at Count=999). Shown up front so a first-time user knows which button is
#: the 25-second one and which is the two-and-a-half-minute one before
#: clicking, not after.
_BUILD_ESTIMATE = {"calculated_field": "about 25 seconds", "report": "about 2.5 minutes"}


def _age_label(seconds: float) -> str:
    """Human-readable cache age.

    Worth surfacing rather than hiding: a calculated field promoted from
    report-scoped to global in the Workday UI stays invisible to a sweep for
    several minutes afterward (confirmed live — see CLAUDE.md). When a
    dependency unexpectedly won't resolve, "this index is 3 days old" is the
    first thing worth knowing.
    """
    minutes = seconds / 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


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
        if job is not None:
            status = "building…"
        elif index is None:
            status = f"not built — takes {_BUILD_ESTIMATE[kind]}"
        else:
            status = f"{len(index):,} items, cached {_age_label(index.age_seconds())}"
        st.write(f"**{label} index**: {status}")
    with col2:
        if job is not None:
            # The report sweep runs about two and a half minutes. Without this
            # the only way out is a browser refresh, which loses the session.
            if st.button("Cancel", key=f"cancel_{kind}", use_container_width=True):
                job.cancel()
                st.rerun()
        else:
            button_label = "Rebuild" if index is not None else "Build"
            if st.button(f"{button_label} {label.lower()} index", key=f"build_{kind}",
                         use_container_width=True):
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
        elif job.cancelled:
            # A partial sweep is real data but it is not the whole tenant, and
            # resolve_closure refuses a partial index for exactly that reason.
            # Discard it rather than cache a half-tenant index to disk.
            setattr(state, job_attr, None)
            theme.banner(
                "warning",
                f"{label} index build cancelled",
                "The partial sweep was discarded — a half-built index would make "
                "dependency resolution look complete when it isn't.",
            )
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
    theme.section(
        "Calculated fields",
        "Selecting nothing here is the normal case — resolving dependencies in the next "
        "step automatically pulls in every calculated field a selected report references.",
        eyebrow="Optional",
    )
    if state.cf_index is None:
        theme.banner("neutral", "Index not built",
                     "Build the calculated field index above to search it directly.")
        return

    query = st.text_input("Search by name (substring)", key="cf_search")
    if query:
        all_matches = [
            (wid, summary)
            for wid, summary in state.cf_index.summaries.items()
            if summary.name and query.lower() in summary.name.lower()
        ]
        matches = all_matches[:_CF_MAX_RESULTS]
        if not matches:
            st.caption("No matches.")
        else:
            if len(all_matches) > len(matches):
                theme.banner(
                    "warning",
                    f"Showing {len(matches)} of {len(all_matches)} matches",
                    f"The table is capped at {_CF_MAX_RESULTS} rows, so "
                    f"{len(all_matches) - len(matches)} match(es) are not listed.",
                    remedy="Narrow the search before selecting.",
                )
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
        theme.figures([("Fields selected", len(state.selected_field_wids))])
        if st.button("Clear calculated field selections", key="cf_clear"):
            state.selected_field_wids.clear()
            st.rerun()


def _render_reports(state: WizardState) -> None:
    theme.section(
        "Reports",
        "Pick from the index table, or add one by its exact name. Report names are not "
        "guaranteed unique — a duplicated name is refused rather than guessed at.",
        eyebrow="Usually where you start",
    )

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
                    theme.banner("success", f"Added “{name}”")
                else:
                    theme.banner(
                        "danger",
                        f"Could not fetch “{name}”",
                        f"It resolved by name, but fetching its full definition failed: "
                        f"{full.fault or full.outcome.value}",
                    )
            elif result.outcome is LookupOutcome.NOT_FOUND:
                theme.banner(
                    "danger",
                    f"No report named exactly “{name}”",
                    "Workday matches report names exactly here — a substring returns nothing.",
                    remedy="Check the spelling, or find it in the index table instead.",
                )
            else:
                theme.banner(
                    "danger",
                    f"“{name}” is ambiguous",
                    "More than one report shares this name, and picking the wrong one would "
                    "be unrecoverable.",
                    remedy="Use the index table instead — each row there is a distinct WID.",
                )

    if state.report_index is not None:
        # Filter first, cap second. The other order silently drops reports past
        # the cap out of the *tenant*, not just out of the table — the tenant
        # holds ~5,150 reports against a 5,000-row cap, so a report could be
        # unfindable no matter what the user typed.
        df = pd.DataFrame(
            [{"wid": wid, "name": s.name, "owner": s.owner}
             for wid, s in state.report_index.summaries.items()]
        )
        query = st.text_input("Filter by name (substring, local)", key="report_filter")
        if query:
            df = df[df["name"].fillna("").str.contains(query, case=False)]
        matched = len(df)
        df = df.head(_REPORT_MAX_ROWS)
        if matched > len(df):
            theme.banner(
                "warning",
                f"Showing {len(df):,} of {matched:,} reports",
                f"The table is capped at {_REPORT_MAX_ROWS:,} rows, so "
                f"{matched - len(df):,} report(s) are not listed.",
                remedy="Narrow the filter above, or add the report by its exact name.",
            )
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
        theme.banner(
            "neutral",
            "Index not built",
            "Build the report index above to browse and multi-select from a table. "
            "Until then, exact-name lookup is the only way to add a report.",
        )

    if state.selected_reports:
        theme.figures([("Reports selected", len(state.selected_reports))])
        if st.button("Clear report selections", key="report_clear"):
            state.selected_reports_manual = {}
            state.selected_reports = {}
            st.rerun()


def render(state: WizardState) -> None:
    st.header("Select")
    connection = state.source.connection
    if connection is None:
        theme.banner("danger", "Source is not connected", remedy="Go back to Connect.")
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
