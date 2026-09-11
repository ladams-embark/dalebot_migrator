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
from wdmigrator.ui.indexes import (
    BUILD_ESTIMATE_SECONDS,
    _DEFAULT_ESTIMATE_SECONDS,
    format_duration,
)
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


def _sweep_kinds(chosen: list[str]) -> list[str]:
    """Every index Select will sweep for this scope, source and destination.

    Mirrors ``select._source_specs`` / ``_destination_specs``. Duplicated
    rather than derived from them because those need a live ``Connection`` to
    build a spec, and Scope deliberately has not touched a tenant yet.
    """
    kinds = ["calculated_field"]  # Always swept: resolution classifies against it.
    if "reports" in chosen:
        kinds.append("report")
    if "dashboards" in chosen:
        kinds += ["dashboard", "prompt_set", "prompt_field"]
    if "time_calculations" in chosen:
        kinds += ["time_calculation", "time_calculation_group", "time_calculation_tag"]
    if "reports" in chosen or "dashboards" in chosen:
        kinds += ["gauge_range", "analytic_indicator"]
    # Both destination sweeps run on Select too — cross-tenant matching needs
    # them before Plan can probe anything, so they are part of the wait.
    return kinds + ["calculated_field", "calculated_measure"]


def _sweep_seconds(chosen: list[str]) -> float:
    """Figures come from :data:`wdmigrator.ui.indexes.BUILD_ESTIMATE_SECONDS`,
    measured live, so this and Select's countdown cannot drift apart."""
    return sum(
        BUILD_ESTIMATE_SECONDS.get(kind, _DEFAULT_ESTIMATE_SECONDS)
        for kind in _sweep_kinds(chosen)
    )


def _sweep_estimate(chosen: list[str]) -> str:
    """How long the next step will spend reading before anything is pickable.

    Scope's whole argument for existing is that the sweeps are expensive
    enough to be worth choosing before starting. It was asking for the choice
    without ever saying what it cost. The report catalog alone is ~2.5
    minutes against a tenant with ~5,150 reports — long enough that a user
    with no number in front of them reasonably concludes the app has hung.
    """
    return (
        f"Roughly {format_duration(_sweep_seconds(chosen))} of catalog "
        "reading on the next step before everything is pickable — both "
        "tenants, cached to disk afterwards. You can start picking sooner "
        "than that."
    )


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

    if chosen:
        st.caption(_sweep_estimate(chosen))


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
