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
from wdmigrator.ui.state import WizardState, reset_downstream

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
            # destination first. Same on-demand contract as measures.
            report_loader=report_loader_for(state.source.connection),
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
    reset_downstream(state, from_step="conflicts")


def render(state: WizardState) -> None:
    st.header("Resolve")
    st.caption(
        "Expands your selection into everything that must migrate with it, in "
        "child-most-first order. Makes no tenant calls — the source index already "
        "holds every calculated field, so this is an in-memory walk."
    )

    if state.closure is None or st.button("Recompute closure", key="resolve_recompute"):
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
    with st.expander(f"Migration order (child-most first) — {len(ordered)} objects"):
        st.dataframe(
            [
                {"order": i + 1, "kind": n.kind.value, "name": n.name, "wid": n.source_wid}
                for i, n in enumerate(ordered)
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.caption("Changed your selection? Go back to Select, then come back here and Recompute.")


def gate(state: WizardState) -> list[Blocker]:
    blockers = []
    if state.closure is None or state.closure_error:
        blockers.append(
            Blocker(
                node_id=None,
                title="Dependencies not resolved",
                detail=state.closure_error or "Click Recompute closure to resolve dependencies.",
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
