"""Step 2: Scope — choose object types before any index is built.

Select used to default this choice to reports and start sweeping immediately
after Connect auto-advanced. The dashboard (and time-calculation) pickers
then never appeared, because their indexes were not in the default sweep and
the kinds chooser was hidden behind the loading screen. Choosing types here
is the human decision; building indexes is Select's job afterwards.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import Blocker
from wdmigrator.ui import theme
from wdmigrator.ui.state import OBJECT_KINDS, WizardState, reset_downstream

STEP_ID = "scope"

#: One line under each kind so a first-time user knows what they are opting
#: into — especially that a dashboard run pulls reports and fields in as
#: dependencies without those kinds having to be ticked here.
_KIND_HELP = {
    "reports": (
        "Pick reports from the catalog or by exact name. Calculated fields "
        "they use come along automatically."
    ),
    "calculated_fields": (
        "Search and pick individual calculated fields. Use this when the "
        "field itself is what you want to migrate, not just a report dependency."
    ),
    "dashboards": (
        "Needs an implementer account. Includes custom dashboards and "
        "Workday-delivered ones (Home, and so on). Worklet reports, prompt "
        "sets, prompt fields and calculated fields they use come along "
        "automatically — you do not need to tick those types just to "
        "migrate a dashboard."
    ),
    "time_calculations": (
        "Time Tracking Implementation Service. Tags and groups they use come "
        "along automatically."
    ),
}

#: Kinds whose Get and Put operations are gated on the account *type* being an
#: implementer. No domain grant substitutes, so a standard ISU can tick these
#: and get nowhere. Connect probes for this; here is where the answer is spent.
_IMPLEMENTER_KINDS = ("dashboards", "time_calculations")


def _implementer_verdict(state: WizardState) -> tuple[bool, str]:
    """``(available, explanation)`` for the implementer-gated kinds.

    Both sides matter: the source has to read the object and the destination
    has to write it, and either one failing wastes the whole run. An unprobed
    or inconclusive side is *not* treated as a refusal — an outage is not an
    entitlement finding — but it is said out loud.
    """
    denied = [
        name
        for name, side in (("Source", state.source), ("Destination", state.dest))
        if side.capabilities is not None and side.capabilities.implementer is False
    ]
    if denied:
        return False, (
            f"{' and '.join(denied)} is not an implementer account. "
            "Dashboards and time calculations cannot be read or written with "
            "it, and no security domain grant changes that — it needs a "
            "different account. Reports and calculated fields still work."
        )

    unknown = [
        name
        for name, side in (("Source", state.source), ("Destination", state.dest))
        if side.capabilities is None or side.capabilities.implementer is None
    ]
    if unknown:
        return True, (
            f"{' and '.join(unknown)} could not be checked for implementer "
            "access, so these may fail later on."
        )
    return True, ""


def render(state: WizardState) -> None:
    st.header("Scope")
    if state.package is not None:
        st.caption(
            "A stored package is loaded — its objects are already chosen. "
            "Continue to review them on Select."
        )
        return

    theme.section(
        "What to migrate",
        "Pick the object types first. Indexes are built on the next step, "
        "only for what you choose here.",
        eyebrow="Before indexes",
    )

    implementer_ok, implementer_note = _implementer_verdict(state)
    if implementer_note:
        theme.banner(
            "danger" if not implementer_ok else "warning",
            "Dashboards and time calculations are unavailable"
            if not implementer_ok
            else "Dashboards and time calculations are unverified",
            implementer_note,
        )

    # Checkboxes rather than a multiselect: each kind has a consequence
    # (implementer account, a 2.5-minute report sweep) that a collapsed
    # chip list would hide. Empty default is deliberate — the previous
    # reports default is what skipped the dashboard workflow.
    chosen: list[str] = []
    for key, label in OBJECT_KINDS.items():
        gated = key in _IMPLEMENTER_KINDS and not implementer_ok
        checked = st.checkbox(
            label,
            value=key in state.object_kinds and not gated,
            key=f"scope_{key}",
            disabled=gated,
        )
        # Caption rather than the ``help`` tooltip: the consequence of ticking
        # a box should not be behind a hover on a page this short.
        st.caption(_KIND_HELP[key])
        if checked and not gated:
            chosen.append(key)

    if list(chosen) != list(state.object_kinds):
        # A kind that is no longer in scope must not keep a stale selection
        # or a half-built index from a previous pass.
        reset_downstream(state, from_step="select")
        state.object_kinds = list(chosen)


def gate(state: WizardState) -> list[Blocker]:
    if state.package is not None:
        return []

    implementer_ok, implementer_note = _implementer_verdict(state)
    if not implementer_ok:
        blocked = [
            OBJECT_KINDS[key] for key in _IMPLEMENTER_KINDS if key in state.object_kinds
        ]
        if blocked:
            return [
                Blocker(
                    node_id=None,
                    title=f"{' and '.join(blocked)} need an implementer account",
                    detail=implementer_note,
                    remedy=(
                        "Go back to Connect and sign in with an implementer "
                        "account, or untick these types."
                    ),
                )
            ]

    if state.object_kinds:
        return []
    return [
        Blocker(
            node_id=None,
            title="No object types chosen",
            detail=(
                "Choose at least one type to migrate. Indexes are not built "
                "until you continue — that is how a dashboard run gets a "
                "dashboard catalog instead of a report sweep."
            ),
            remedy="Tick Dashboards, Reports, Calculated fields, or Time calculations.",
        )
    ]
