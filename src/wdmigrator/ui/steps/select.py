"""Step 3: Select — build indexes for the scoped kinds, then pick objects.

Object types are chosen on Scope, *before* this step. That is load-bearing:
a default of reports plus an auto-start sweep hid the dashboard picker and
never built the dashboard index. This step only sweeps and only renders
pickers for ``state.object_kinds``.

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
    TIME_TRACKING_SERVICE_NAME,
    Blocker,
    LookupOutcome,
    iter_analytic_indicator_index,
    iter_calculated_field_index,
    iter_dashboard_index,
    iter_gauge_range_index,
    iter_prompt_field_index,
    iter_prompt_set_index,
    iter_report_index,
    iter_time_calculation_group_index,
    iter_time_calculation_index,
    iter_time_calculation_tag_index,
    lookup_report,
    lookup_report_by_name,
)
from wdmigrator.ui import theme
from wdmigrator.ui.indexes import IndexSpec, bulk_build_indexes, destination_index_specs
from wdmigrator.ui.state import OBJECT_KINDS, WizardState, reset_downstream

STEP_ID = "select"

# Render caps. Both are below live tenant volumes (~9,650 calculated fields /
# ~5,150 reports on commitconsulting_dpt1), so both genuinely truncate — and a
# picker that silently hides rows is dangerous in a tool where picking the
# wrong object cannot be undone. Every truncation is called out in the UI.
_CF_MAX_RESULTS = 500
_REPORT_MAX_ROWS = 5000

def _source_specs(chosen: list[str], connection) -> list[IndexSpec]:
    """Which source sweeps this selection needs, in the order they should run.

    Catalog indexes the user will pick from run first so the picker appears
    before the calculated-field sweep (~25s) finishes. Resolve still needs
    that CF index — it is always included, last — and the Select gate will
    not unlock until it lands. Report-dependency indexes (gauge range,
    analytic indicator) run when there's any report-shaped selection —
    reports directly, or dashboards that carry them as worklets.
    """
    specs: list[IndexSpec] = []
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
    if "time_calculations" in chosen:
        # Time Tracking indexes live on Time_Tracking_Implementation_Service,
        # not Core. Open a sibling connection sharing the same target and
        # rate limiter — see Connection.for_service.
        tt_connection = connection.for_service(TIME_TRACKING_SERVICE_NAME)
        specs.extend(
            [
                IndexSpec(
                    kind="time_calculation_tag",
                    label="Time calculation tag",
                    iterator_fn=iter_time_calculation_tag_index,
                    connection=tt_connection,
                    index_attr="time_calculation_tag_index",
                    implementer_gated=True,
                ),
                IndexSpec(
                    kind="time_calculation_group",
                    label="Time calculation group",
                    iterator_fn=iter_time_calculation_group_index,
                    connection=tt_connection,
                    index_attr="time_calculation_group_index",
                    implementer_gated=True,
                ),
                IndexSpec(
                    kind="time_calculation",
                    label="Time calculation",
                    iterator_fn=iter_time_calculation_index,
                    connection=tt_connection,
                    index_attr="time_calculation_index",
                    implementer_gated=True,
                ),
            ]
        )
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
    # Always last: every WID inside a report or dashboard is classified
    # against the complete calculated-field index.
    specs.append(
        IndexSpec(
            kind="calculated_field",
            label="Calculated field",
            iterator_fn=iter_calculated_field_index,
            connection=connection,
            index_attr="cf_index",
        )
    )
    return specs


def _report_specs(connection) -> list[IndexSpec]:
    """The slow report sweep — its own job so exact-name add is not blocked."""
    return [
        IndexSpec(
            kind="report",
            label="Report",
            iterator_fn=iter_report_index,
            connection=connection,
            index_attr="report_index",
        )
    ]


def _bank_payloads(rows, df, store: dict, payload_for) -> int:
    """Copy newly highlighted rows into a wid-to-payload map. Add-only."""
    added = 0
    for i in rows:
        wid = df.iloc[i]["wid"]
        if wid in store:
            continue
        payload = payload_for(wid)
        if payload is not None:
            store[wid] = payload
            added += 1
    return added


def _bank_wids(rows, df, store: set) -> int:
    """Copy newly highlighted rows into a WID set. Add-only."""
    added = 0
    for i in rows:
        wid = df.iloc[i]["wid"]
        if wid not in store:
            store.add(wid)
            added += 1
    return added


def _destination_specs(connection) -> list[IndexSpec]:
    """The two DESTINATION sweeps Conflicts needs for cross-tenant matching.

    Built here too, alongside the source sweeps, rather than only when the
    user reaches Conflicts. Both sides are already connected and verified by
    the time Select renders (Connect's gate requires it), so there is no
    dependency reason to defer these — only asking again later added a second
    "click Build" round trip for a sweep the run needs unconditionally before
    Conflicts can probe anything. See ``wdmigrator.ui.steps.conflicts`` for
    why the sweep itself is not optional: ``Calculated_Field_ID`` is not a
    stable identity between independently-built tenants.

    Kept as a *separate* list from :func:`_source_specs`, rather than folded
    into one undifferentiated sweep, so a caller can still tell "source" from
    "destination" apart if it ever needs to (e.g. resetting one side's cache
    without touching the other).
    """
    return destination_index_specs(connection)


def _render_calculated_fields(state: WizardState) -> None:
    theme.section(
        "Calculated fields",
        "Optional — reports pull in the fields they use. Search, then highlight rows to add.",
        eyebrow="Optional",
    )
    if state.cf_index is None:
        theme.banner("neutral", "Index not built",
                     "Build the calculated field index above to search it directly.")
        return

    query = st.text_input("Search fields by name", key="cf_search")
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
            if rows and _bank_wids(rows, df, state.selected_field_wids):
                reset_downstream(state, from_step="plan")
                st.rerun()

    if state.selected_field_wids:
        theme.figures([("Fields selected", len(state.selected_field_wids))])
        if st.button("Clear calculated field selections", key="cf_clear"):
            state.selected_field_wids.clear()
            reset_downstream(state, from_step="plan")
            st.rerun()


def _render_reports(state: WizardState) -> None:
    theme.section(
        "Reports",
        "Highlight rows in the catalog, or add one by exact name while the catalog loads.",
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
                    reset_downstream(state, from_step="plan")
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
        query = st.text_input("Filter catalog by name", key="report_filter")
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
        # Bank immediately: the table reports row *positions* into the current
        # frame, so we copy WIDs out before a later filter repoints them.
        # Add-only — Clear is the way to drop a pick.
        if rows and _bank_payloads(
            rows, df, state.selected_reports_added,
            lambda wid: state.report_index.payload(wid),
        ):
            reset_downstream(state, from_step="plan")
            st.rerun()
    else:
        theme.banner(
            "neutral",
            "Index not built",
            "The report index is sweeping in the background (~2.5 minutes). "
            "Until it lands, add a report by its exact name above.",
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
            reset_downstream(state, from_step="plan")
            st.rerun()


def _render_dashboards(state: WizardState) -> None:
    theme.section(
        "Custom dashboards",
        "Picking one pulls in its worklet reports and prompt sets. Needs an implementer account.",
        eyebrow="Implementer account",
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
            "The dashboard index is sweeping in the background. Both flavours "
            "are swept — tabbed and untabbed are separate object types.",
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
    query = st.text_input("Filter dashboards by name", key="dashboard_filter")
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
    if rows and _bank_payloads(
        rows, df, state.selected_dashboards_added,
        lambda wid: state.dashboard_index.payload(wid),
    ):
        reset_downstream(state, from_step="plan")
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
            reset_downstream(state, from_step="plan")
            st.rerun()


def _render_time_calculations(state: WizardState) -> None:
    theme.section(
        "Time calculations",
        "Pick the calculations. Tags and groups they use are pulled in on Plan.",
        eyebrow="Time Tracking",
    )
    if state.time_calculation_index is None:
        theme.banner(
            "neutral",
            "Time calculation index not built",
            "The time-calculation index is sweeping in the background.",
        )
        return

    query = st.text_input(
        "Search Time Calculations by name (substring)", key="tc_search"
    )
    summaries = list(state.time_calculation_index.summaries.items())
    if query:
        needle = query.lower()
        summaries = [
            (wid, s)
            for wid, s in summaries
            if (s.name and needle in s.name.lower())
            or (s.reference_id and needle in s.reference_id.lower())
        ]
    if not summaries:
        st.caption("No matches.")
        return
    df = pd.DataFrame(
        [
            {
                "wid": wid,
                "name": s.name or s.reference_id or "(unnamed)",
                "id": s.reference_id or "",
                "priority": s.priority or "",
                "inactive": s.inactive,
            }
            for wid, s in summaries[:1000]
        ]
    )
    picked = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        key="tc_table",
    )
    rows = picked.get("selection", {}).get("rows", []) if isinstance(picked, dict) else []
    if hasattr(picked, "selection"):
        rows = picked.selection["rows"] if picked.selection else []
    if rows and _bank_wids(rows, df, state.selected_time_calculation_wids):
        reset_downstream(state, from_step="plan")
        st.rerun()

    if state.selected_time_calculation_wids:
        theme.figures(
            [("Time calculations selected", len(state.selected_time_calculation_wids))]
        )
        with st.expander(
            f"Selected time calculations ({len(state.selected_time_calculation_wids)})"
        ):
            for wid in sorted(state.selected_time_calculation_wids):
                s = state.time_calculation_index.summaries.get(wid)
                st.write(f"• {s.name if s else wid} — {s.reference_id if s else ''}")
        if st.button("Clear selections", key="tc_clear"):
            state.selected_time_calculation_wids = set()
            reset_downstream(state, from_step="plan")
            st.rerun()


def _render_package_summary(state: WizardState) -> None:
    """Read-only view of what a loaded package contains.

    Substitutes for the normal picker UI: with a package loaded there is no
    source connection and therefore no live indexes to browse. The picker
    would render an empty shell; this section shows what will actually be
    written instead.
    """
    pkg = state.package
    counts = pkg.counts_by_kind()
    theme.section(
        f"Loaded package: {pkg.name}",
        "Source-side selection is skipped when a package is loaded — every "
        "object here was captured at build time. Change what is written by "
        "loading a different package on the Connect step.",
        eyebrow="Read-only",
    )
    theme.figures(
        [("Total objects", pkg.node_count)]
        + [(k.capitalize(), v) for k, v in counts.items()]
    )
    st.caption(
        f"Source tenant: `{pkg.source_tenant}` — captured {pkg.captured_at}"
    )
    st.caption(pkg.description or "(no description)")
    with st.expander("Objects in the package"):
        rows = [
            {"kind": n.kind.value, "name": n.name or "(unnamed)",
             "selected": n.selected, "wid": n.source_wid}
            for n in pkg.closure.nodes.values()
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_destination_matching(state: WizardState, *, auto_start: bool = False) -> bool:
    """Destination CF/measure sweeps. Required even when the source is a package."""
    if state.dest.connection is None:
        theme.banner(
            "danger",
            "Destination is not connected",
            "Cross-tenant matching reads the destination tenant. Go back to Connect.",
        )
        return False
    theme.section(
        "Destination matching",
        "Needed so shared fields are reused instead of duplicated."
        + (
            ""
            if auto_start
            else " Click Build destination indexes to start."
        ),
        eyebrow="Starts automatically" if auto_start else "Manual start",
    )
    return bulk_build_indexes(
        state,
        _destination_specs(state.dest.connection),
        job_attr="dest_index_job",
        button_label="Build destination indexes",
        auto_start=auto_start,
    )


def _scope_caption(chosen: list[str]) -> str:
    labels = [OBJECT_KINDS[k] for k in chosen if k in OBJECT_KINDS]
    if not labels:
        return "No object types chosen — go back to Scope."
    return "Migrating: " + ", ".join(labels) + ". Change this on Scope."


def _render_pickers(state: WizardState, chosen: list[str]) -> None:
    """Only the pickers for kinds committed on Scope.

    Calculated fields used to render unconditionally as an "optional" column,
    which made a dashboard-only run look like a field-and-report run and hid
    the actual dashboard table below a loading screen.
    """
    if "dashboards" in chosen:
        _render_dashboards(state)
        st.divider()

    show_cf = "calculated_fields" in chosen
    show_reports = "reports" in chosen
    if show_cf and show_reports:
        col1, col2 = st.columns(2)
        with col1:
            _render_calculated_fields(state)
        with col2:
            _render_reports(state)
    elif show_cf:
        _render_calculated_fields(state)
    elif show_reports:
        _render_reports(state)

    if "time_calculations" in chosen:
        if show_cf or show_reports or "dashboards" in chosen:
            st.divider()
        _render_time_calculations(state)

    if "calculated_fields" not in chosen:
        state.selected_field_wids = set()
    if "reports" not in chosen:
        state.selected_reports = {}
        state.selected_reports_added = {}
    if "dashboards" not in chosen:
        state.selected_dashboards = {}
        state.selected_dashboards_added = {}
    if "time_calculations" not in chosen:
        state.selected_time_calculation_wids = set()


def render(state: WizardState) -> None:
    st.header("Select")
    if state.package is not None:
        _render_package_summary(state)
        st.divider()
        if _render_destination_matching(state, auto_start=False):
            st.rerun()
        return
    connection = state.source.connection
    if connection is None:
        theme.banner("danger", "Source is not connected", remedy="Go back to Connect.")
        return

    chosen = list(state.object_kinds)
    if not chosen:
        theme.banner(
            "warning",
            "No object types chosen",
            "Indexes are not built until Scope has a type ticked, so a "
            "dashboard run is not turned into a report sweep by default.",
            remedy="Go back to Scope and tick at least one object type.",
        )
        return

    st.caption(_scope_caption(chosen))

    specs = _source_specs(chosen, connection)
    report_specs = _report_specs(connection) if "reports" in chosen else []
    running = False
    theme.section("Source indexes", eyebrow="Starts automatically")
    running = bulk_build_indexes(
        state,
        specs,
        job_attr="source_index_job",
        button_label="Build source indexes",
        auto_start=True,
    ) or running
    if report_specs:
        theme.section("Report catalog", eyebrow="Background — does not block exact-name add")
        running = bulk_build_indexes(
            state,
            report_specs,
            job_attr="report_index_job",
            button_label="Build report index",
            auto_start=True,
        ) or running
    running = _render_destination_matching(state, auto_start=True) or running

    st.divider()
    # Pickers render while indexes are still sweeping: the dashboard catalog
    # is the first source stage, so it can be selected before the calculated
    # field sweep (~25s) finishes. Continue stays gated on the rest.
    _render_pickers(state, chosen)
    if running:
        st.rerun()


def gate(state: WizardState) -> list[Blocker]:
    blockers = []
    # Package is the source: selection is baked in, indexes are irrelevant.
    if state.package is not None:
        return blockers
    if not state.object_kinds:
        blockers.append(
            Blocker(
                node_id=None,
                title="No object types chosen",
                detail="Scope has to name the types before this step can pick objects.",
                remedy="Go back to Scope and tick at least one object type.",
            )
        )
        return blockers
    if not (
        state.selected_field_wids
        or state.selected_reports
        or state.selected_dashboards
        or state.selected_time_calculation_wids
    ):
        blockers.append(
            Blocker(
                node_id=None,
                title="Nothing selected",
                detail=(
                    "Select at least one dashboard, report, calculated field or "
                    "time calculation to migrate."
                ),
                remedy="Pick from the tables above.",
            )
        )
    # A Time Calculation names input tags, output tags, and one or more group
    # snapshots. Without the tag and group indexes those references never
    # enter the closure and the write fails against the live tenant.
    if state.selected_time_calculation_wids:
        for index, title, detail in (
            (
                state.time_calculation_tag_index,
                "Time calculation tag index not built",
                "A Time Calculation reads and writes tags (Regular, Overtime, "
                "etc.) which must exist in the destination first.",
            ),
            (
                state.time_calculation_group_index,
                "Time calculation group index not built",
                "A Time Calculation belongs to one or more groups (USA, CAN_BC, "
                "…) through snapshot references, and each named group has to "
                "exist in the destination first.",
            ),
        ):
            if index is None:
                blockers.append(
                    Blocker(
                        node_id=None,
                        title=title,
                        detail=(
                            detail
                            + " Resolution skips this reference kind when the "
                            "index is absent, so the gap surfaces as a live "
                            "write failure rather than a blocker here."
                        ),
                        remedy="Enable Time calculations above — the tag and group "
                               "indexes start with the source sweep.",
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
                remedy="Wait for the prompt set index (a few seconds), or rebuild it above.",
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
                remedy="Wait for the prompt field index (a few seconds), or rebuild it above.",
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
                        remedy="Wait for it above (a few seconds), or rebuild.",
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
                remedy="Wait for the calculated field index above (~25s), or rebuild it.",
            )
        )
    return blockers
