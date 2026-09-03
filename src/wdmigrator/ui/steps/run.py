"""Step 4: Run — live gate, then the only step that can write.

The Plan step already required a reviewed dry run. This step is the remaining
human confirmation (destination tenant name, irreversibility) plus Start.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import Blocker
from wdmigrator.ui.steps import confirm, execute
from wdmigrator.ui.state import WizardState

STEP_ID = "run"


def render(state: WizardState) -> None:
    st.header("Run")
    confirm.render_live_gate(state)
    st.divider()
    execute.render(state, heading=False)


def gate(state: WizardState) -> list[Blocker]:
    return execute.gate(state)
