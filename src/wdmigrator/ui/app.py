"""Wizard entry point — gated linear navigation.

Tabs or a sidebar are deliberately not used for navigation: either would let
a user click straight to Execute without passing through Conflicts and
Confirm. Instead each step exposes ``gate(state) -> list[Blocker]``, and the
*only* way to reach step N+1 is this module's own "Next" button, which stays
disabled until step N's gate returns empty. The step indicator at the top is
a read-only progress display, not a nav control — see
``wdmigrator.ui.components.step_nav``.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import redact
from wdmigrator.ui import components
from wdmigrator.ui.state import STEP_ORDER, STEP_TITLES, WizardState, get_state
from wdmigrator.ui.steps import confirm, conflicts, connect, execute, resolve, results, select

_STEPS = {
    "connect": connect,
    "select": select,
    "resolve": resolve,
    "conflicts": conflicts,
    "confirm": confirm,
    "execute": execute,
    "results": results,
}


def _unlocked_through(state: WizardState) -> int:
    """Index of the furthest step whose gate is currently satisfied, walking
    forward from Connect and stopping at the first one that isn't."""
    unlocked = -1
    for i, step_id in enumerate(STEP_ORDER):
        if _STEPS[step_id].gate(state):
            break
        unlocked = i
    return unlocked


def main() -> None:
    st.set_page_config(page_title="Workday Config Migrator", layout="wide")
    state = get_state()

    unlocked_through = _unlocked_through(state)
    components.step_nav(state.step, STEP_ORDER, STEP_TITLES, unlocked_through)
    st.divider()

    module = _STEPS[state.step]
    try:
        module.render(state)
    except Exception as exc:  # noqa: BLE001 - last-resort redaction boundary.
        # A zeep fault can carry the request envelope, which can carry a
        # WS-Security password in cleartext. Never let a raw traceback reach
        # the page — this is the most likely credential-leak path in the app.
        message = redact(str(exc), (state.source.password, state.dest.password))
        st.error(f"Unexpected error in the {STEP_TITLES[state.step]} step: {message}")
        return

    st.divider()
    current_index = STEP_ORDER.index(state.step)
    nav_cols = st.columns(2)
    with nav_cols[0]:
        if current_index > 0 and st.button("← Back", key="nav_back"):
            state.step = STEP_ORDER[current_index - 1]
            st.rerun()
    with nav_cols[1]:
        blockers = module.gate(state)
        if current_index < len(STEP_ORDER) - 1:
            if st.button("Next →", key="nav_next", disabled=bool(blockers), type="primary"):
                state.step = STEP_ORDER[current_index + 1]
                st.rerun()
        if blockers:
            with st.expander(f"{len(blockers)} blocker(s) before continuing"):
                for b in blockers:
                    st.caption(f"**{b.title}** — {b.detail}")
