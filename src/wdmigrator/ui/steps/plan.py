"""Step 3: Plan — resolve, probe, review CREATE/SKIP, dry-run.

Mechanical work starts itself: the closure is computed on first visit, the
destination is probed once matching indexes are ready, and the dry run
serializes in-process (no tenant call). The human work on this step is
reviewing the plan and ticking that the dry run was read.
"""

from __future__ import annotations

from wdmigrator.api import Blocker
from wdmigrator.ui.steps import confirm, conflicts, resolve
from wdmigrator.ui.state import WizardState

import streamlit as st

STEP_ID = "plan"


def render(state: WizardState) -> None:
    st.header("Plan")
    resolve.render(state, heading=False)
    if state.closure is None or state.closure_error:
        return
    st.divider()
    conflicts.render(state, heading=False)
    if state.plan is None or state.existence_job is not None:
        return
    st.divider()
    confirm.render_plan_review(state)


def gate(state: WizardState) -> list[Blocker]:
    blockers = resolve.gate(state)
    if blockers:
        return blockers
    blockers = conflicts.gate(state)
    if blockers:
        return blockers
    return confirm.gate_plan_review(state)
