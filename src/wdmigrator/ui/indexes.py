"""Sweeping tenant indexes inside the wizard, shared by Select and Conflicts.

Two steps sweep tenants, for opposite reasons. **Select** builds the SOURCE
indexes that dependency resolution reads. **Conflicts** builds the DESTINATION
indexes that cross-tenant matching reads — and those are not optional:
``Calculated_Field_ID`` is not a stable identity between independently-built
tenants, so without a destination sweep every field the two tenants already
share probes as absent, is planned as CREATE, and the write is then rejected by
the destination's own uniqueness rule ("Enter a unique WQL alias for the
business object").

Both need the same machinery — load a cached index if there is one, otherwise
sweep it through the chunked runner so the page stays responsive and
cancellable — so it lives here rather than being written twice.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import (
    cache_path,
    calculated_field_match_index,
    calculated_measure_match_index,
    load_index,
    requires_implementer,
    save_index,
)
from wdmigrator.ui import theme
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.runner import pump, start_job

#: Measured live against commitconsulting_dpt1 (~9,650 fields / ~5,150 reports
#: at Count=999). Shown up front so a first-time user knows which button is
#: the 25-second one and which is the two-and-a-half-minute one before
#: clicking, not after. Everything not listed is a single page.
BUILD_ESTIMATE = {
    "calculated_field": "about 25 seconds",
    "report": "about 2.5 minutes",
}
_DEFAULT_ESTIMATE = "a few seconds"


def destination_matching_ready(state) -> bool:
    """Whether both destination sweeps cross-tenant matching needs are present."""
    return state.dest_cf_index is not None and state.dest_measure_index is not None


def destination_match_indexes(state) -> dict:
    """Keyword arguments for :func:`~wdmigrator.api.iter_check_existence`.

    Derived here rather than at each call site so the two steps that probe the
    destination — Conflicts, and Execute's re-probe after a reference decision
    — cannot drift apart. If they did, a re-probe would silently re-plan as
    CREATE everything the first probe had correctly matched, which is how a
    duplicate gets written into a tenant with no delete operation.

    Returns empty when the sweeps have not been built, which turns matching
    off. Callers must gate on :func:`destination_matching_ready` rather than
    relying on that — off is exactly the state that produced this bug.
    """
    if not destination_matching_ready(state):
        return {}
    return {
        "match_index": calculated_field_match_index(state.dest_cf_index),
        "measure_match_index": calculated_measure_match_index(state.dest_measure_index),
    }


def age_label(seconds: float) -> str:
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


def load_or_prompt_index(
    state,
    *,
    kind,
    iterator_fn,
    job_attr,
    index_attr,
    label,
    connection,
    implementer_gated: bool = False,
):
    """Show one index's status, offer to build it, and pump a running build.

    ``implementer_gated`` says this object kind cannot be read at all by a
    plain ISU. Only those kinds may set ``state.implementer_required`` — a
    calculated-field or report sweep failing is an ordinary error, and letting
    it raise the implementer banner would explain the failure wrongly.
    """
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
            estimate = BUILD_ESTIMATE.get(kind, _DEFAULT_ESTIMATE)
            status = f"not built — takes {estimate}"
        else:
            status = f"{len(index):,} items, cached {age_label(index.age_seconds())}"
        st.write(f"**{label} index**: {status}")
    with col2:
        if job is not None:
            # The report sweep runs about two and a half minutes. Without this
            # the only way out is a browser refresh, which loses the session.
            if st.button("Cancel", key=f"cancel_{kind}_{index_attr}", use_container_width=True):
                job.cancel()
                st.rerun()
        else:
            button_label = "Rebuild" if index is not None else "Build"
            if st.button(f"{button_label} {label.lower()} index",
                         key=f"build_{kind}_{index_attr}",
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
            # A dashboard, prompt-set or prompt-field sweep failing this way is
            # not a bug and not a transient error — it means the connected
            # account is not an implementer. Recorded once so the picker can
            # explain it, rather than surfacing a raw SOAP fault the user
            # cannot act on.
            if implementer_gated and requires_implementer(str(job.error)):
                state.implementer_required = True
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
