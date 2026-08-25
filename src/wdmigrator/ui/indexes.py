"""Bulk index sweeps inside the wizard, shared by Select and Conflicts.

Two steps sweep tenants, for opposite reasons. **Select** builds the SOURCE
indexes that dependency resolution reads. **Conflicts** builds the DESTINATION
indexes that cross-tenant matching reads — and those are not optional:
``Calculated_Field_ID`` is not a stable identity between independently-built
tenants, so without a destination sweep every field the two tenants already
share probes as absent, is planned as CREATE, and the write is then rejected by
the destination's own uniqueness rule ("Enter a unique WQL alias for the
business object").

Both build the same set of indexes the same way — pre-load anything already
cached to disk, then run every remaining sweep back-to-back inside one job so
the user clicks Build once and walks away. Individual per-index buttons and
per-index job slots are gone; a single ``JobState`` per side is what remains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

import streamlit as st

from wdmigrator.api import (
    Connection,
    IndexProgress,
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
#: at Count=999). Shown up front so a first-time user knows what they're
#: waiting on before clicking, not after. Everything not listed is a single
#: page.
BUILD_ESTIMATE = {
    "calculated_field": "about 25 seconds",
    "report": "about 2.5 minutes",
}
_DEFAULT_ESTIMATE = "a few seconds"


@dataclass(frozen=True)
class IndexSpec:
    """One index to sweep: what to call, where to stash the result."""

    kind: str
    label: str
    iterator_fn: Callable[[Connection], Iterator[IndexProgress]]
    connection: Connection
    index_attr: str
    #: Dashboard/prompt-set/prompt-field sweeps fail with a distinct "task not
    #: authorized" fault on non-implementer accounts. When one gated stage
    #: fails that way the bulk build marks the flag and skips the other gated
    #: stages, rather than raising three identical errors in a row.
    implementer_gated: bool = False


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


@dataclass
class _StageEvent:
    """One tick from :func:`_chained_build`. Carries the current stage's
    label and the underlying :class:`IndexProgress` so the UI can show one
    combined bar."""

    stage: int
    total_stages: int
    label: str
    kind: str
    fraction: float
    progress: IndexProgress | None = None


def _load_cached(state, specs: list[IndexSpec]) -> None:
    """Populate any index attr whose disk cache is present. Free; no clicks."""
    for spec in specs:
        if getattr(state, spec.index_attr) is not None:
            continue
        cached = load_index(
            cache_path(spec.connection, spec.kind),
            tenant=spec.connection.target.tenant,
        )
        if cached is not None:
            setattr(state, spec.index_attr, cached)


def _chained_build(state, specs: list[IndexSpec]) -> Iterator[_StageEvent]:
    """Sweep every spec back-to-back inside one generator.

    Each finished index is stashed on ``state`` and saved to disk before the
    next one starts, so a cancel mid-run leaves everything already built
    intact. A gated stage that raises an implementer fault sets
    ``state.implementer_required`` and moves on; the remaining gated stages in
    this run are skipped rather than each raising the identical fault.
    """
    total = len(specs)
    for stage, spec in enumerate(specs, start=1):
        if spec.implementer_gated and state.implementer_required:
            continue
        try:
            for progress in spec.iterator_fn(spec.connection):
                yield _StageEvent(
                    stage=stage,
                    total_stages=total,
                    label=spec.label,
                    kind=spec.kind,
                    fraction=progress.fraction,
                    progress=progress,
                )
                if progress.complete:
                    setattr(state, spec.index_attr, progress.index)
                    save_index(progress.index, cache_path(spec.connection, spec.kind))
        except Exception as exc:  # noqa: BLE001 - classified; a real error re-raises
            if spec.implementer_gated and requires_implementer(str(exc)):
                state.implementer_required = True
                yield _StageEvent(
                    stage=stage,
                    total_stages=total,
                    label=spec.label,
                    kind=spec.kind,
                    fraction=1.0,
                    progress=None,
                )
                continue
            raise


def _summarise(state, specs: list[IndexSpec]) -> None:
    """One status line per index so the UI shows what's built, what's missing,
    and — for a cached one — how old it is. These labels are what
    ``test_ui_app_smoke.py`` grep for; keep them stable."""
    for spec in specs:
        index = getattr(state, spec.index_attr)
        if index is not None:
            status = f"{len(index):,} items, cached {age_label(index.age_seconds())}"
        elif spec.implementer_gated and state.implementer_required:
            status = "skipped — implementer account required"
        else:
            estimate = BUILD_ESTIMATE.get(spec.kind, _DEFAULT_ESTIMATE)
            status = f"not built — takes {estimate}"
        st.write(f"**{spec.label} index**: {status}")


def _missing(state, specs: list[IndexSpec]) -> list[IndexSpec]:
    return [
        s
        for s in specs
        if getattr(state, s.index_attr) is None
        and not (s.implementer_gated and state.implementer_required)
    ]


def bulk_build_indexes(
    state,
    specs: list[IndexSpec],
    *,
    job_attr: str,
    button_label: str,
) -> None:
    """Render one status list + one Build button + one progress bar.

    Any spec whose cache is on disk is filled in silently — no click needed.
    The Build button only surfaces the sweeps left; a Rebuild button under it
    handles the "destination refreshed, this cache is stale" case (confirmed
    live — see CLAUDE.md). ``job_attr`` is a plain attribute name on ``state``
    so Select and Conflicts can share this function while keeping independent
    jobs.
    """
    _load_cached(state, specs)
    _summarise(state, specs)

    job = getattr(state, job_attr)

    if job is not None:
        col1, col2 = st.columns([3, 1])
        with col1:
            last = job.last_event
            fraction = 0.0
            label = button_label
            if isinstance(last, _StageEvent):
                stage_frac = (last.stage - 1 + max(0.0, min(1.0, last.fraction))) / last.total_stages
                fraction = min(1.0, stage_frac)
                label = f"{last.label} ({last.stage}/{last.total_stages})"
            render_job_progress(job, label=label, fraction=fraction)
        with col2:
            if st.button("Cancel", key=f"{job_attr}_cancel", use_container_width=True):
                job.cancel()
                st.rerun()

        pump(job, time_budget=0.8)
        if job.error is not None:
            setattr(state, job_attr, None)
            st.rerun()
        elif job.cancelled:
            setattr(state, job_attr, None)
            theme.banner(
                "warning",
                "Index build cancelled",
                "Whichever indexes had already finished are kept — the one running "
                "when you cancelled was discarded, since a half-built index would "
                "make dependency resolution look complete when it isn't.",
            )
            st.rerun()
        elif job.done:
            setattr(state, job_attr, None)
            st.rerun()
        else:
            st.rerun()
        return

    missing = _missing(state, specs)
    col1, col2 = st.columns([3, 1])
    with col1:
        if missing:
            estimates = ", ".join(
                BUILD_ESTIMATE.get(s.kind, _DEFAULT_ESTIMATE) for s in missing
            )
            if st.button(
                f"{button_label} ({len(missing)} to build: {estimates})",
                key=f"{job_attr}_start",
                type="primary",
                use_container_width=True,
            ):
                setattr(state, job_attr, start_job(_chained_build(state, missing)))
                st.rerun()
    with col2:
        built = [s for s in specs if getattr(state, s.index_attr) is not None]
        if built and st.button(
            "Rebuild all",
            key=f"{job_attr}_rebuild",
            use_container_width=True,
        ):
            for spec in built:
                setattr(state, spec.index_attr, None)
            state.implementer_required = False
            setattr(state, job_attr, start_job(_chained_build(state, specs)))
            st.rerun()
