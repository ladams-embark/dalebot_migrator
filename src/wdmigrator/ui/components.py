"""Shared render helpers used by more than one wizard step.

Purely presentational — nothing here calls into the engine or touches
``st.session_state`` beyond widget keys it's explicitly given.

Everything renders through :mod:`wdmigrator.ui.theme` rather than
``st.success``/``st.error``/``st.warning``: those ship emoji icons by default,
which the Commit brand rules out. The step rail lives in ``theme.stepper``.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import Blocker, Capabilities, ConnectionStatus, TenantTarget
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


def render_capabilities(capabilities: Capabilities | None) -> None:
    """What this account can reach, said at connection time.

    The implementer requirement is an account-type gate, not a domain grant —
    no security configuration inside Workday moves it. Left undiscovered it
    surfaces on Select as a failed sweep, three steps and a full index build
    after the point where the user could have done something about it.
    """
    if capabilities is None:
        return
    if capabilities.implementer is True:
        theme.banner("success", "Implementer account", capabilities.detail)
        return
    if capabilities.implementer is False:
        theme.banner(
            "warning",
            "Standard Integration System User",
            capabilities.detail,
            remedy="Connect with an implementer account if you need dashboards, "
                   "prompt sets, prompt fields or time calculations.",
        )
        return
    theme.banner(
        "neutral",
        "Could not check account type",
        capabilities.detail,
        remedy="Dashboard-shaped objects may or may not work — the Select step "
               "will say for certain.",
        remedy_label="Next",
    )


def render_blocker(blocker: Blocker) -> None:
    """One blocker, in the tone its ``waiting`` flag asks for.

    A sweep that finishes on its own and a permission the user has to go and
    fix are both "you cannot continue yet", but only one of them is a
    problem. Rendering them identically is what trains people to ignore the
    red ones.
    """
    if blocker.waiting:
        theme.banner(
            "neutral",
            blocker.title,
            blocker.detail,
            remedy=blocker.remedy or None,
            where=blocker.node_id or None,
            remedy_label="Next",
        )
        return
    theme.banner(
        "danger", blocker.title, blocker.detail,
        remedy=blocker.remedy or None, where=blocker.node_id or None,
    )


def render_blockers(blockers: list[Blocker], *, empty_message: str = "No blockers.") -> None:
    """Render engine ``Blocker``s. The single renderer for these — the wizard's
    Next button, the Conflicts validation panel, and the app-level gate all
    show the same shape, so they show it the same way."""
    if not blockers:
        theme.banner("success", empty_message)
        return
    for b in blockers:
        render_blocker(b)


#: Terms the wizard uses as though everyone knows them. Most are Workday's
#: own vocabulary and the rest are this tool's; either way the user meeting
#: them for the first time is doing so on a page that is about to write to a
#: tenant. Ordered roughly by when they first appear in the flow.
_GLOSSARY = [
    (
        "Tenant",
        "One Workday environment — Implementation, Sandbox, Production. This "
        "tool reads from a source tenant and writes to a destination tenant, "
        "and they must be different ones for a live run.",
    ),
    (
        "Services host vs browser host",
        "Every tenant has two hostnames. The one in your browser's address "
        "bar (impl.wd12…) is not the one the API lives on "
        "(impl-services1.wd12…). Using the browser host returns HTTP 500, "
        "which looks like an outage rather than a typo.",
    ),
    (
        "ISU",
        "Integration System User — the service account this tool signs in as. "
        "Needs Get and Put on the Configuration Set: Custom Reports and "
        "Fields security configuration, on both tenants.",
    ),
    (
        "Implementer account",
        "A category of Workday account, not a permission. Dashboards, prompt "
        "sets, prompt fields and time calculations can only be read or "
        "written by one, and no security domain grant substitutes.",
    ),
    (
        "WID",
        "Workday ID — the internal identifier for an object. WIDs are "
        "tenant-local: a calculated field written to the destination gets a "
        "new one, which is why every reference to it has to be rewritten "
        "afterwards. That rewriting is most of what this tool does.",
    ),
    (
        "Business ID / reference ID",
        "A human-assigned identifier such as Calculated_Field_Reference_ID, "
        "unlike a WID. Business IDs are usually the same on both tenants, so "
        "they are left alone rather than remapped.",
    ),
    (
        "Dependency closure",
        "Your selection plus everything it needs to work — the calculated "
        "fields a report uses, the fields those fields use, and so on. Built "
        "on the Plan step, in memory, with no tenant calls.",
    ),
    (
        "CREATE / SKIP / UPDATE",
        "What will happen to each object. SKIP means the destination already "
        "has it and it will be reused unchanged. CREATE means it does not and "
        "one will be made. UPDATE overwrites an existing object and is used "
        "sparingly.",
    ),
    (
        "Dry run",
        "The whole migration with every write suppressed. It builds the exact "
        "payloads that would be sent and reports what each would do, without "
        "sending any of them.",
    ),
    (
        "Worklet",
        "One tile on a dashboard. A dashboard names its reports as worklets, "
        "and each of those reports has to name the dashboard back — which is "
        "why dashboards are written twice, once empty and once complete.",
    ),
    (
        "Prompt set",
        "The set of runtime prompts a dashboard offers. It has to exist in "
        "the destination before a dashboard referencing it can be written.",
    ),
]


def render_glossary() -> None:
    """The vocabulary, in one collapsed place on every step.

    Every one of these terms appears in the wizard's own copy without
    explanation, and the copy cannot stop to define them without becoming
    unreadable. A user working alone has nobody to lean over and ask.
    """
    with st.expander("Glossary — what the words on this page mean"):
        for term, meaning in _GLOSSARY:
            st.markdown(f"**{term}** — {meaning}")


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
