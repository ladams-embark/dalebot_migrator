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

from wdmigrator.api import Blocker, CycleError, PartialIndexError, resolve, topological_sort
from wdmigrator.ui import theme
from wdmigrator.ui.state import WizardState, reset_downstream

STEP_ID = "resolve"


def _compute(state: WizardState) -> None:
    try:
        closure = resolve(
            state.cf_index,
            selected_field_wids=state.selected_field_wids,
            selected_reports=state.selected_reports,
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
