"""Shared render helpers used by more than one wizard step.

Purely presentational — nothing here calls into the engine or touches
``st.session_state`` beyond widget keys it's explicitly given.

Everything renders through :mod:`wdmigrator.ui.theme` rather than
``st.success``/``st.error``/``st.warning``: those ship emoji icons by default,
which the Commit brand rules out. The step rail lives in ``theme.stepper``.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import Blocker, ConnectionStatus, TenantTarget
from wdmigrator.ui import theme
from wdmigrator.ui.runner import JobState


def render_target_card(label: str, target: TenantTarget | None) -> None:
    if target is None:
        theme.card(label, pill=theme.env_pill(None))
        return
    theme.card(
        label,
        pill=theme.env_pill(target.environment),
        meta=f"{target.tenant} @ {target.services_host}",
        note=(
            "Services host was derived from the URL you pasted, not typed directly — "
            "confirm it by testing the connection."
            if target.services_host_derived
            else None
        ),
    )


def render_connection_status(status: ConnectionStatus | None) -> None:
    if status is None:
        st.caption("Not tested yet.")
        return
    if status.ok:
        theme.banner("success", "Connected", status.detail)
    else:
        theme.banner("danger", "Connection failed", status.detail)


def render_blockers(blockers: list[Blocker], *, empty_message: str = "No blockers.") -> None:
    """Render engine ``Blocker``s. The single renderer for these — the wizard's
    Next button, the Conflicts validation panel, and the app-level gate all
    show the same shape, so they show it the same way."""
    if not blockers:
        theme.banner("success", empty_message)
        return
    for b in blockers:
        theme.banner("danger", b.title, b.detail, remedy=b.remedy or None, where=b.node_id or None)


def render_job_progress(
    job: JobState | None, *, label: str, fraction: float = 0.0, detail: str | None = None
) -> None:
    """Render one job's progress bar, or its terminal state once it stops.

    ``detail`` is an optional caption under the bar — a live sweep passes
    something like "3,204 / 9,650 fetched · about 20s remaining" so a user
    can tell the run is actually moving, not just that a bar exists. Callers
    with nothing more specific than a fraction (most jobs) simply omit it.
    """
    if job is None:
        return
    if job.error is not None:
        theme.banner("danger", f"{label} failed", str(job.error))
        return
    if job.cancelled:
        theme.banner("warning", f"{label} cancelled")
        return
    if job.done:
        theme.banner("success", f"{label} complete")
        return
    st.progress(min(max(fraction, 0.0), 1.0), text=f"{label}…")
    if detail:
        st.caption(detail)
