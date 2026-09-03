"""Step 3: Resolve — expand the selection into the full dependency closure.

Makes zero tenant calls: the source calculated-field index already holds
every calculated field, so classifying a WID found inside a report or
another field's payload is a free in-memory lookup, not a network round
trip. See ``wdmigrator.migrate.resolver`` for the full story.

Recomputing only happens on an explicit click, never automatically on
rerun (past the first time) — nothing in this wizard mutates plan state
without the user asking for it, including a "harmless" recompute that would
silently discard any downstream action overrides.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import (
    Blocker,
    CycleError,
    PartialIndexError,
    measure_loader_for,
    report_loader_for,
    resolve,
    topological_sort,
)
from wdmigrator.ui import theme
from wdmigrator.ui.state import WizardState, clear_downstream_of_closure

STEP_ID = "resolve"


def _compute(state: WizardState) -> None:
    try:
        closure = resolve(
            state.cf_index,
            selected_field_wids=state.selected_field_wids,
            selected_reports=state.selected_reports,
            # Reports that use calculated measures pull them in as dependencies.
            # Measures are not indexed, so this makes a targeted source call per
            # distinct measure — a handful per report, not a sweep.
            measure_loader=measure_loader_for(state.source.connection),
            # Composite reports render sub-reports, which must exist in the
            # destination first. Same on-demand contract as measures. Also
            # covers the reports a dashboard shows as worklets, which are
            # named the same way.
            report_loader=report_loader_for(state.source.connection),
            selected_dashboards=state.selected_dashboards,
            # Indexes rather than loaders: none of these object kinds has
            # usable request criteria, so there is no on-demand route to take.
            #
            # Passing None is NOT the same as "there are none of these" — the
            # resolver skips extracting that kind of reference entirely, so an
            # index left out here means the dependency never enters the closure
            # and is discovered by the destination rejecting the write. The
            # Select gate refuses to advance without them for that reason.
            prompt_set_index=state.prompt_set_index,
            prompt_field_index=state.prompt_field_index,
            gauge_range_index=state.gauge_range_index,
            analytic_indicator_index=state.analytic_indicator_index,
            dashboard_index=state.dashboard_index,
            selected_time_calculation_wids=state.selected_time_calculation_wids,
            time_calculation_index=state.time_calculation_index,
            time_calculation_tag_index=state.time_calculation_tag_index,
            time_calculation_group_index=state.time_calculation_group_index,
        )
    except PartialIndexError as exc:
        state.closure = None
        state.closure_error = f"Calculated field index is incomplete: {exc}"
        return
    except KeyError as exc:
        state.closure = None
        state.closure_error = f"Selected calculated field {exc} is not in the source index."
        return

    try:
        topological_sort(closure.nodes)
    except CycleError as exc:
        state.closure = None
        state.closure_error = f"Dependency cycle detected: {' -> '.join(exc.cycle)}"
        return

    state.closure = closure
    state.closure_error = None
    clear_downstream_of_closure(state)


def render(state: WizardState, *, heading: bool = True) -> None:
    if heading:
        st.header("Resolve")
    if state.package is not None:
        st.caption("Using the closure from the loaded package.")
    elif heading:
        st.caption(
            "Expands your selection into everything that must migrate with it, in "
            "child-most-first order. Makes no tenant calls — the source index already "
            "holds every calculated field, so this is an in-memory walk."
        )

    # A package-loaded run has state.closure set at load time; Recompute is
    # hidden because there is nothing to recompute against.
    if state.package is None and (
        state.closure is None
        or st.button("Recompute closure", key="resolve_recompute")
    ):
        _compute(state)

    if state.closure_error:
        theme.banner(
            "danger",
            "Could not resolve dependencies",
            state.closure_error,
            remedy="Go back to Select and adjust the selection, or rebuild the "
                   "calculated field index if it may be stale.",
        )
        return
    if state.closure is None:
        return

    counts = state.closure.counts_by_kind()
    theme.figures(
        [("Total objects", len(state.closure))]
        + [(k.capitalize(), v) for k, v in counts.items()]
        + [
            ("Selected", len(state.closure.selected_nodes)),
            ("Pulled in", len(state.closure.pulled_in_nodes)),
        ]
    )

    if state.closure.unresolved_measure_ids:
        missing = sorted(state.closure.unresolved_measure_ids)
        theme.banner(
            "danger",
            f"{len(missing)} calculated measure(s) could not be fetched from the source",
            "A report being migrated uses these, but the source tenant did not "
            "return them: " + ", ".join(missing[:3])
            + (f", and {len(missing) - 3} more" if len(missing) > 3 else ""),
            remedy="Check that the source ISU can call Get_Calculated_Measures.",
        )

    # A payload that names a calculated field by Calculated_Field_Reference_ID
    # states outright that the target IS a calculated field. Not finding it in
    # the source index therefore means a genuinely missing dependency, and a
    # write referencing it will fail — unlike an unmatched WID, which is
    # usually just a delivered object passing through.
    if state.closure.unresolved_reference_ids:
        missing = sorted(state.closure.unresolved_reference_ids)
        theme.banner(
            "danger",
            f"{len(missing)} referenced calculated field(s) are not in the source index",
            "These are named directly as calculated fields by something being "
            "migrated, so they are real dependencies, not pass-throughs: "
            + ", ".join(missing[:5])
            + (f", and {len(missing) - 5} more" if len(missing) > 5 else ""),
            remedy="Rebuild the calculated field index — it may predate them. If they "
                   "still do not appear, they are report-scoped and must be promoted "
                   "to global in the Workday UI before this migration can succeed.",
        )

    ordered = topological_sort(state.closure.nodes)
    by_id = {n.node_id: n for n in ordered}
    with st.expander(f"Migration order (child-most first) — {len(ordered)} objects"):
        st.dataframe(
            [
                {
                    "order": i + 1,
                    "kind": n.kind.value,
                    "name": n.name,
                    "why": (
                        "selected"
                        if n.selected
                        else ", ".join(
                            (by_id[rid].name or rid)
                            for rid in sorted(n.required_by)
                            if rid in by_id
                        )
                        or "pulled in"
                    ),
                    "wid": n.source_wid,
                }
                for i, n in enumerate(ordered)
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.caption("Changed the selection? Go back to Select.")


def gate(state: WizardState) -> list[Blocker]:
    blockers = []
    if state.closure is None or state.closure_error:
        blockers.append(
            Blocker(
                node_id=None,
                title="Dependencies not resolved",
                detail=state.closure_error or "Dependencies have not been resolved yet.",
                remedy="Resolve dependencies with no errors before continuing.",
            )
        )
        return blockers

    # `validate_plan` checks these too, and is the real backstop for every
    # caller. Repeating them here is not redundancy: this gate runs before a
    # plan exists, so the user hears about a missing dependency at Resolve
    # rather than a step later at Conflicts.
    for missing, kind, remedy in (
        (
            sorted(state.closure.unresolved_reference_ids),
            "calculated field",
            "Rebuild the calculated field index, or promote the field to global in "
            "Workday if it is report-scoped.",
        ),
        (
            sorted(state.closure.unresolved_measure_ids),
            "calculated measure",
            "A report-scoped measure cannot be created by this tool — it has to be "
            "removed from the report, or the report migrated without it.",
        ),
        (
            sorted(state.closure.unresolved_report_ids),
            "sub-report",
            "Check the source ISU can read the sub-report. A composite cannot "
            "render a sub-report the destination does not have.",
        ),
        (
            sorted(state.closure.unresolved_prompt_set_ids),
            "prompt set",
            "Rebuild the prompt set index in Select. Reading prompt sets needs an "
            "implementer account, so confirm the source connection is one.",
        ),
        (
            sorted(state.closure.unresolved_prompt_field_ids),
            "prompt field",
            "Rebuild the prompt field index in Select. Reading prompt fields needs "
            "an implementer account, so confirm the source connection is one.",
        ),
        (
            sorted(state.closure.unresolved_gauge_range_ids),
            "gauge range",
            "Rebuild the gauge range index in Select (one page).",
        ),
        (
            sorted(state.closure.unresolved_dashboard_ids),
            "nested dashboard",
            "Rebuild the dashboard index in Select. A dashboard shown inside "
            "another has to exist in the destination first.",
        ),
        (
            sorted(state.closure.unresolved_time_calculation_tag_ids),
            "time calculation tag",
            "Rebuild the time calculation tag index in Select (one page).",
        ),
        (
            sorted(state.closure.unresolved_time_calculation_group_ids),
            "time calculation group",
            "Rebuild the time calculation group index in Select (one page).",
        ),
    ):
        if missing:
            blockers.append(
                Blocker(
                    node_id=None,
                    title=f"{len(missing)} {kind}(s) could not be resolved",
                    detail=(
                        f"Something being migrated names these as a {kind}, but they "
                        f"are not available on the source: {', '.join(missing[:3])}"
                        + (f" (+{len(missing) - 3} more)" if len(missing) > 3 else "")
                        + ". Writing an object that references one will fail."
                    ),
                    remedy=remedy,
                )
            )
    return blockers
