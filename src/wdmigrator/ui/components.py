"""Shared render helpers used by more than one wizard step.

Purely presentational — nothing here calls into the engine or touches
``st.session_state`` beyond widget keys it's explicitly given.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import Blocker, ConnectionStatus, Environment, TenantTarget
from wdmigrator.ui.runner import JobState

_ENV_BADGE = {
    Environment.IMPLEMENTATION: ("🟢", "Implementation"),
    Environment.SANDBOX: ("🟢", "Sandbox"),
    Environment.PRODUCTION: ("🔴", "PRODUCTION"),
    Environment.UNKNOWN: ("🔴", "UNKNOWN (treated as Production)"),
}


def render_target_card(label: str, target: TenantTarget | None) -> None:
    if target is None:
        st.info(f"{label}: not set")
        return
    icon, env_label = _ENV_BADGE[target.environment]
    st.markdown(f"**{label}** — {icon} {env_label}")
    st.caption(f"`{target.tenant}` @ `{target.services_host}`")
    if target.services_host_derived:
        st.caption("⚠️ Services host was derived from the URL you pasted, not typed directly — confirm it by testing the connection.")


def render_connection_status(status: ConnectionStatus | None) -> None:
    if status is None:
        st.caption("Not tested yet.")
        return
    if status.ok:
        st.success(f"Connected — {status.detail}", icon="✅")
    else:
        st.error(f"Connection failed — {status.detail}", icon="❌")


def render_blockers(blockers: list[Blocker], *, empty_message: str = "No blockers.") -> None:
    if not blockers:
        st.success(empty_message)
        return
    for b in blockers:
        where = f" ({b.node_id})" if b.node_id else ""
        st.error(f"**{b.title}{where}**\n\n{b.detail}\n\n*Remedy:* {b.remedy}", icon="🚫")


def render_job_progress(job: JobState | None, *, label: str, fraction: float = 0.0) -> None:
    if job is None:
        return
    if job.error is not None:
        st.error(f"{label} failed: {job.error}", icon="❌")
        return
    if job.cancelled:
        st.warning(f"{label} cancelled.", icon="⏹️")
        return
    if job.done:
        st.success(f"{label} complete.", icon="✅")
        return
    st.progress(min(max(fraction, 0.0), 1.0), text=f"{label}…")


def step_nav(current: str, order: list[str], titles: dict[str, str], unlocked_through: int) -> None:
    """Render the wizard's step indicator. Not clickable — advancing only
    happens through each step's own Next button, so a user can never jump
    past an unpassed gate."""
    cols = st.columns(len(order))
    for i, (col, step_id) in enumerate(zip(cols, order)):
        with col:
            title = titles[step_id]
            if step_id == current:
                st.markdown(f"**➡️ {title}**")
            elif i <= unlocked_through:
                st.markdown(f"✅ {title}")
            else:
                st.markdown(f"🔒 {title}")
