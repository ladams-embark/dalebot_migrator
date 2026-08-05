"""Rendering the engine's safety findings for a human.

The engine (``wdmigrator.safety``) is the real gate: ``assert_write_allowed``
is re-checked before every single write inside ``iter_execute``, not just
once by this module. Everything here is display only — it turns a list of
``Guard`` into banners so a user can see *why* Execute is disabled, and lets
them acknowledge WARN-level findings. Deleting this module would make the
app unusable, not unsafe: the engine still refuses the write on its own.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import Guard, Level
from wdmigrator.ui import theme


def render_guards(guards: list[Guard], *, key_prefix: str = "") -> set[str]:
    """Render guard findings as banners.

    BLOCK guards render with no way to dismiss them — they clear only when the
    underlying condition does. WARN guards render as a checkbox; returns the
    set of guard ids the user has ticked "I understand" for, so the caller can
    fold that into ``WriteGuard.warnings_acknowledged``.
    """
    acknowledged: set[str] = set()
    if not guards:
        theme.banner("success", "No blockers", "Nothing is standing between this plan and a live run.")
        return acknowledged

    for guard in guards:
        if guard.level is Level.BLOCK:
            theme.banner("danger", guard.title, guard.detail, remedy=guard.remedy or None)
        else:
            theme.banner("warning", guard.title, guard.detail, remedy=guard.remedy or None)
            if st.checkbox(
                f"I understand: {guard.title}",
                key=f"{key_prefix}ack_{guard.id}",
            ):
                acknowledged.add(guard.id)
    return acknowledged
