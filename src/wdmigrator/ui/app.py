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
from wdmigrator.ui import components, errors, theme, workspace
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


def prioritise_blockers(blockers):
    """Actionable blockers before merely-unfinished ones.

    Only the first is shown inline; the rest go behind an expander. Without
    this, which blocker gets promoted is down to the order a gate happened to
    append them in, so a user could be shown "waiting for an index sweep"
    while the thing actually holding them up — an empty selection — sat
    collapsed underneath it.
    """
    return sorted(blockers, key=lambda b: b.waiting)


def _render_back_only(state: WizardState) -> None:
    """The escape hatch for a step that could not render at all."""
    index = STEP_ORDER.index(state.step)
    if index == 0:
        return
    st.divider()
    if st.button(f"Back to {STEP_TITLES[STEP_ORDER[index - 1]].lower()}", key="nav_back_error"):
        state.hold_step = True
        state.step = STEP_ORDER[index - 1]
        st.rerun()


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

    # One-shot. Shown on the render *after* the change that caused it, which
    # is the first render the user actually sees — the reset itself happens
    # inside a callback that ends in st.rerun().
    if state.discarded_notice:
        theme.banner("warning", "Downstream work was cleared", state.discarded_notice)
        state.discarded_notice = ""

    st.divider()

    module = _STEPS[state.step]
    try:
        module.render(state)
    except Exception as exc:  # noqa: BLE001 - last-resort redaction boundary.
        # A zeep fault can carry the request envelope, which can carry a
        # WS-Security password in cleartext. Never let a raw traceback reach
        # the page — this is the most likely credential-leak path in the app.
        secrets = (state.source.password, state.dest.password)
        message = redact(str(exc), secrets)
        log_path = errors.write_error_log(
            exc,
            step=state.step,
            state=state,
            secrets=secrets,
            directory=workspace.safe_user_dir(errors.ERROR_DIR),
        )
        theme.banner(
            "danger",
            f"Unexpected error in the {STEP_TITLES[state.step]} step",
            message,
            remedy=(
                f"The full traceback is in `{log_path}` — passwords stripped, "
                "safe to send on."
                if log_path
                else "Retry the step, or go back and re-test the connection."
            ),
            remedy_label="Next",
        )
        # The step body is what failed, so Continue would be meaningless — but
        # returning here used to take the whole nav bar with it, leaving the
        # user on a dead page with no way back to the step whose input caused
        # this. Back, at least, always works.
        _render_back_only(state)
        components.render_glossary()
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
        if current_index > 0 and st.button("Back", key="nav_back", width="stretch"):
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
                width="stretch",
            ):
                state.hold_step = False
                state.step = STEP_ORDER[current_index + 1]
                st.rerun()

    if blockers and current_index < len(STEP_ORDER) - 1:
        first, *rest = prioritise_blockers(blockers)
        components.render_blocker(first)
        if rest:
            with st.expander(f"{len(rest)} more before continuing", expanded=False):
                components.render_blockers(rest)

    # Last thing on every step. The wizard's copy uses WID, ISU, implementer,
    # closure and worklet as though everyone knows them, and a user working
    # alone has nobody to lean over and ask.
    components.render_glossary()
