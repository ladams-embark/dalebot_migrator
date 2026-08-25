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
    IMPLEMENTER_REQUIRED_REMEDY,
    Blocker,
    LookupOutcome,
    iter_analytic_indicator_index,
    iter_calculated_field_index,
    iter_dashboard_index,
    iter_gauge_range_index,
    iter_prompt_field_index,
    iter_prompt_set_index,
    iter_report_index,
    lookup_report,
    lookup_report_by_name,
)
from wdmigrator.ui import theme
from wdmigrator.ui.indexes import IndexSpec, bulk_build_indexes
from wdmigrator.ui.state import WizardState

STEP_ID = "select"

# Render caps. Both are below live tenant volumes (~9,650 calculated fields /
# ~5,150 reports on commitconsulting_dpt1), so both genuinely truncate — and a
# picker that silently hides rows is dangerous in a tool where picking the
# wrong object cannot be undone. Every truncation is called out in the UI.
_CF_MAX_RESULTS = 500
_REPORT_MAX_ROWS = 5000

#: What the user picks first. Dashboards are listed last deliberately — they sit
#: at the end of the dependency chain, and they are the only kind with an
#: account-level prerequisite.
_OBJECT_KINDS = {
    "reports": "Reports",
    "calculated_fields": "Calculated fields",
    "dashboards": "Custom dashboards",
}


def _source_specs(chosen: list[str], connection) -> list[IndexSpec]:
    """Which source sweeps this selection needs, in the order they should run.

    The calculated-field sweep is always first: resolving dependencies needs the
    complete index no matter what the user picked from it, since every WID
    encountered inside a report or dashboard is classified against it. Report-
    dependency indexes (gauge range, analytic indicator) run next when there's
    any report-shaped selection — reports directly, or dashboards that carry
    them as worklets. The dashboard implementer-gated three come last so that
    the cheap common-case sweeps are already in when a non-implementer account
    hits the wall.
    """
    specs = [
        IndexSpec(
            kind="calculated_field",
            label="Calculated field",
            iterator_fn=iter_calculated_field_index,
            connection=connection,
            index_attr="cf_index",
        )
    ]
    if "reports" in chosen or "dashboards" in chosen:
        specs.append(
            IndexSpec(
                kind="gauge_range",
                label="Gauge range",
                iterator_fn=iter_gauge_range_index,
                connection=connection,
                index_attr="gauge_range_index",
            )
        )
        specs.append(
            IndexSpec(
                kind="analytic_indicator",
                label="Analytic indicator",
                iterator_fn=iter_analytic_indicator_index,
                connection=connection,
                index_attr="analytic_indicator_index",
            )
        )
    if "reports" in chosen:
        specs.append(
            IndexSpec(
                kind="report",
                label="Report",
                iterator_fn=iter_report_index,
                connection=connection,
                index_attr="report_index",
            )
        )
    if "dashboards" in chosen:
        specs.extend(
            [
                IndexSpec(
                    kind="dashboard",
                    label="Dashboard",
                    iterator_fn=iter_dashboard_index,
                    connection=connection,
                    index_attr="dashboard_index",
                    implementer_gated=True,
                ),
                IndexSpec(
                    kind="prompt_set",
                    label="Prompt set",
                    iterator_fn=iter_prompt_set_index,
                    connection=connection,
                    index_attr="prompt_set_index",
                    implementer_gated=True,
                ),
                IndexSpec(
                    kind="prompt_field",
                    label="Prompt field",
                    iterator_fn=iter_prompt_field_index,
                    connection=connection,
                    index_attr="prompt_field_index",
                    implementer_gated=True,
                ),
            ]
        )
    return specs


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
                    state.selected_reports_added[full.wid] = full.data
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
        # Adding is an explicit button, not a read of the live table selection,
        # for the same reason the calculated field picker works this way: the
        # table reports *row positions into the frame it was handed*, so
        # retyping the filter makes those positions refer to different reports.
        # Selections have to be banked before the frame changes underneath them,
        # or picking reports across two different searches is impossible.
        if st.button("Add selected reports", key="report_add", disabled=not rows):
            for i in rows:
                wid = df.iloc[i]["wid"]
                payload = state.report_index.payload(wid)
                if payload is not None:
                    state.selected_reports_added[wid] = payload
            st.rerun()
    else:
        theme.banner(
            "neutral",
            "Index not built",
            "Build the report index above to browse and multi-select from a table. "
            "Until then, exact-name lookup is the only way to add a report.",
        )

    state.selected_reports = dict(state.selected_reports_added)

    if state.selected_reports:
        theme.figures([("Reports selected", len(state.selected_reports))])
        # Added reports are no longer visible as highlighted table rows once the
        # filter moves on, so they are listed by name. Picking the wrong report
        # cannot be undone in the destination, which makes "what exactly is in
        # my selection" worth showing rather than just counting.
        with st.expander(f"Selected reports ({len(state.selected_reports)})"):
            for wid, payload in state.selected_reports.items():
                data = payload.get("Tenanted_Report_Definition_Data") or {}
                st.write(f"- {data.get('Name') or wid}")
        if st.button("Clear report selections", key="report_clear"):
            state.selected_reports_added = {}
            state.selected_reports = {}
            st.rerun()


def _render_dashboards(state: WizardState) -> None:
    theme.section(
        "Custom dashboards",
        "A dashboard sits at the end of the chain: picking one pulls in the reports it "
        "shows as worklets, the prompt sets those use, and every calculated field "
        "underneath. Reading them requires an implementer account.",
        eyebrow="Requires an implementer account",
    )

    # Set before the early returns below so every path agrees on what is
    # selected, including the ones that never reach the table.
    state.selected_dashboards = dict(state.selected_dashboards_added)

    if state.implementer_required:
        theme.banner(
            "warning",
            "This account cannot read custom dashboards",
            IMPLEMENTER_REQUIRED_REMEDY,
            remedy="Reports and calculated fields are unaffected — you can migrate "
                   "those with this connection.",
        )
        return

    if state.dashboard_index is None:
        theme.banner(
            "neutral",
            "Index not built",
            "Build the dashboard index above. Both dashboard flavours are swept — "
            "tabbed and untabbed are separate object types in Workday and nothing "
            "identifies which a dashboard is ahead of time.",
        )
        return

    df = pd.DataFrame(
        [
            {
                "wid": wid,
                "name": s.name,
                "layout": "tabbed" if s.tabbed else "single page",
                "items": s.worklet_count,
            }
            for wid, s in state.dashboard_index.summaries.items()
        ]
    )
    query = st.text_input("Filter by name (substring, local)", key="dashboard_filter")
    if query and not df.empty:
        df = df[df["name"].fillna("").str.contains(query, case=False)]

    event = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        key="dashboard_table_select",
    )
    rows = event.selection["rows"] if event and event.selection else []
    # Banked on an explicit add rather than read off the live table, for the
    # same reason as reports: the table's row positions are relative to the
    # frame it was handed, so retyping the filter silently repoints them.
    if st.button("Add selected dashboards", key="dashboard_add", disabled=not rows):
        for i in rows:
            wid = df.iloc[i]["wid"]
            payload = state.dashboard_index.payload(wid)
            if payload is not None:
                state.selected_dashboards_added[wid] = payload
        st.rerun()

    state.selected_dashboards = dict(state.selected_dashboards_added)

    if state.selected_dashboards:
        theme.figures([("Dashboards selected", len(state.selected_dashboards))])
        with st.expander(f"Selected dashboards ({len(state.selected_dashboards)})"):
            for wid, payload in state.selected_dashboards.items():
                summary = state.dashboard_index.summaries.get(wid)
                st.write(f"- {getattr(summary, 'name', None) or wid}")
        if st.button("Clear dashboard selections", key="dashboard_clear"):
            state.selected_dashboards_added = {}
            state.selected_dashboards = {}
            st.rerun()


def render(state: WizardState) -> None:
    st.header("Select")
    connection = state.source.connection
    if connection is None:
        theme.banner("danger", "Source is not connected", remedy="Go back to Connect.")
        return

    theme.section(
        "What are you migrating?",
        "Pick the object kinds you want to work with. Each one you enable needs its "
        "source index built first — dependencies are resolved from those indexes, "
        "not by querying the tenant object by object.",
        eyebrow="Start here",
    )
    chosen = st.multiselect(
        "Object kinds",
        options=list(_OBJECT_KINDS),
        default=["reports"],
        format_func=lambda key: _OBJECT_KINDS[key],
        key="object_kinds",
    )

    specs = _source_specs(chosen, connection)
    bulk_build_indexes(
        state,
        specs,
        job_attr="source_index_job",
        button_label="Build source indexes",
    )

    st.divider()
    if "dashboards" in chosen:
        _render_dashboards(state)
        st.divider()

    col1, col2 = st.columns(2)
    with col1:
        _render_calculated_fields(state)
    with col2:
        if "reports" in chosen:
            _render_reports(state)
    if "reports" not in chosen:
        state.selected_reports = {}
    if "dashboards" not in chosen:
        state.selected_dashboards = {}


def gate(state: WizardState) -> list[Blocker]:
    blockers = []
    if not (
        state.selected_field_wids or state.selected_reports or state.selected_dashboards
    ):
        blockers.append(
            Blocker(
                node_id=None,
                title="Nothing selected",
                detail=(
                    "Select at least one dashboard, report or calculated field to "
                    "migrate."
                ),
                remedy="Pick from the tables above.",
            )
        )
    if state.selected_dashboards and state.prompt_set_index is None:
        blockers.append(
            Blocker(
                node_id=None,
                title="Prompt set index not built",
                detail=(
                    "A dashboard binds its runtime prompts to prompt sets, and those "
                    "have to exist in the destination first. They cannot be fetched "
                    "on demand — the request criteria Workday exposes for them do "
                    "not filter — so the index is the only way to resolve them."
                ),
                remedy="Build the prompt set index above (a few seconds).",
            )
        )
    if state.selected_dashboards and state.prompt_field_index is None:
        blockers.append(
            Blocker(
                node_id=None,
                title="Prompt field index not built",
                detail=(
                    "A prompt set names its parameters as prompt fields, and a prompt "
                    "set cannot be written until they exist in the destination. "
                    "Without the index, resolution does not look for them at all — "
                    "the dependency never enters the closure and the prompt set "
                    "fails against the live tenant instead of here."
                ),
                remedy="Build the prompt field index above (a few seconds).",
            )
        )
    # Both are dependencies of *reports*, and a dashboard drags its worklet
    # reports in, so either selection needs them.
    if state.selected_reports or state.selected_dashboards:
        for index, title, detail in (
            (
                state.gauge_range_index,
                "Gauge range index not built",
                "A report with a gauge layout names a gauge range, which has to "
                "exist in the destination first.",
            ),
            (
                state.analytic_indicator_index,
                "Analytic indicator index not built",
                "A matrix measure names an analytic indicator. Most need no "
                "migration — indicator WIDs are shared across tenants — but "
                "without the index the writer also cannot tell which ones are "
                "dangling on the source and must be stripped.",
            ),
        ):
            if index is None:
                blockers.append(
                    Blocker(
                        node_id=None,
                        title=title,
                        detail=(
                            detail
                            + " Resolution skips this whole class of reference when "
                            "the index is absent, so the gap surfaces as a live "
                            "write failure rather than a blocker here."
                        ),
                        remedy="Build it above (a few seconds).",
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
