"""Credential entry and redaction for the wizard.

Password fields never get echoed back anywhere except inside the widget
itself. Nothing here stores a ``Secret`` object — ``api.connect()`` wraps the
plain string internally, so this module never has to import
``wdmigrator.secrets`` (or even know that type exists) to do its job. The
redaction helper exists for anything the wizard displays that might
otherwise leak a password — an auth failure detail line, a raw envelope
preview.
"""

from __future__ import annotations

import streamlit as st

from wdmigrator.api import redact


def username_input(label: str, *, key: str, value: str = "") -> str:
    return st.text_input(label, value=value, key=key, autocomplete="off")


def password_input(label: str, *, key: str) -> str:
    """No ``value=`` here on purpose — Streamlit would echo whatever we pass
    back into the page's HTML on every rerun. The widget owns its own state
    across reruns via ``key``; we only ever read it out."""
    return st.text_input(label, type="password", key=key, autocomplete="off")


def redact_for_display(text: str, *values: str) -> str:
    return redact(text, values)
