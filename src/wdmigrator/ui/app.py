"""Wizard entry point — gated linear navigation.

Tabs or a sidebar are deliberately not used for navigation: either would let
a user click straight to Run without passing through Plan. Instead each step
exposes ``gate(state) -> list[Blocker]``, and the *only* way to reach step
N+1 is this module's own Continue button (or Connect's auto-advance after
both sides verify), which stays disabled until step N's gate returns empty.
The step rail at the top is a read-only progress display, not a nav control
— see ``wdmigrator.ui.theme.stepper``.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
load_dotenv()

import streamlit as st

from wdmigrator.api import redact
from wdmigrator.ui import components, theme
from wdmigrator.ui.state import STEP_ORDER, STEP_TITLES, WizardState, get_state
from wdmigrator.ui.steps import connect, plan, results, run, scope, select

_STEPS = {
    "connect": connect,
    "scope": scope,
    "select": select,
    "plan": plan,
    "run": run,
    "results": results,
}

#: After both connections verify, skip the extra Continue click. Scope is
#: the first human decision (what to migrate) and must not be skipped —
#: except when a stored package already carries the objects. Select, Plan,
#: and Run stay manual.
_AUTO_ADVANCE_FROM = frozenset({"connect"})

#: One line under the step rail. The step body should not repeat this.
_STEP_HINT = {
    "connect": "Enter both tenants, then Test. Continue unlocks when both succeed.",
    "scope": "Tick the object types to migrate. Indexes are built on the next step.",
    "select": "Highlight a row to add it, or add a report by exact name. Clear drops a pick.",
    "plan": "Check CREATE vs SKIP, then tick that you read the dry run.",
    "run": "Type the destination tenant name, tick the box, then Start. Writes cannot be undone.",
    "results": "Download the log, or start a new migration.",
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
    st.set_page_config(
        page_title="Commit — Workday configuration migrator",
        page_icon=theme.FAVICON_PATH,
        layout="wide",
    )
    theme.inject()

    if os.environ.get("WDMIGRATOR_ALLOW_NON_IMPL") == "1":
        st.caption("ALLOW_NON_IMPL = 1")

    state = get_state()

    # Package-loaded runs advertise the package name in the source badge
    # rather than a tenant that isn't connected to anything.
    if state.package is not None:
        source_tenant_display = f"package: {state.package.name}"
        source_env_display = None
    elif state.source.target is not None:
        source_tenant_display = state.source.target.tenant
        source_env_display = state.source.target.environment
    else:
        source_tenant_display = None
        source_env_display = None
    theme.page_header(
        source_tenant=source_tenant_display,
        source_env=source_env_display,
        dest_tenant=state.dest.target.tenant if state.dest.target else None,
        dest_env=state.dest.target.environment if state.dest.target else None,
    )
    theme.stepper(state.step, STEP_ORDER, STEP_TITLES, _unlocked_through(state))
    st.caption(_STEP_HINT[state.step])
    st.divider()

    module = _STEPS[state.step]
    try:
        module.render(state)
    except Exception as exc:  # noqa: BLE001 - last-resort redaction boundary.
        # A zeep fault can carry the request envelope, which can carry a
        # WS-Security password in cleartext. Never let a raw traceback reach
        # the page — this is the most likely credential-leak path in the app.
        message = redact(str(exc), (state.source.password, state.dest.password))
        theme.banner("danger", f"Unexpected error in the {STEP_TITLES[state.step]} step", message)
        return

    st.divider()
    current_index = STEP_ORDER.index(state.step)
    blockers = module.gate(state)

    auto_advance = state.step in _AUTO_ADVANCE_FROM or (
        state.step == "scope" and state.package is not None
    )
    if (
        not state.hold_step
        and auto_advance
        and not blockers
        and current_index < len(STEP_ORDER) - 1
    ):
        state.step = STEP_ORDER[current_index + 1]
        st.rerun()

    nav_cols = st.columns([1, 1, 6])
    with nav_cols[0]:
        if current_index > 0 and st.button("Back", key="nav_back", use_container_width=True):
            state.hold_step = True
            state.step = STEP_ORDER[current_index - 1]
            st.rerun()
    with nav_cols[1]:
        if current_index < len(STEP_ORDER) - 1:
            next_title = STEP_TITLES[STEP_ORDER[current_index + 1]]
            if st.button(
                f"Continue to {next_title.lower()}",
                key="nav_next",
                disabled=bool(blockers),
                type="primary",
                use_container_width=True,
            ):
                state.hold_step = False
                state.step = STEP_ORDER[current_index + 1]
                st.rerun()

    if blockers and current_index < len(STEP_ORDER) - 1:
        first, *rest = blockers
        theme.banner("warning", first.title, first.detail, remedy=first.remedy or None)
        if rest:
            with st.expander(f"{len(rest)} more before continuing", expanded=False):
                components.render_blockers(rest)
