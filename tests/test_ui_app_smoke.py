"""Smoke test: the wizard boots and renders Connect with no exceptions.

Entirely offline — with no credentials entered, nothing in the Connect step
makes a network call, so this is safe to run in the default `pytest`
collection alongside everything else.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_app_boots_to_connect_step_with_no_exceptions():
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.run(timeout=15)
    assert not at.exception


def test_connect_step_is_the_initial_step():
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.run(timeout=15)
    headers = [h.value for h in at.header]
    assert "Connect" in headers


class _StubTarget:
    tenant = "stub_tenant"


class _StubConnection:
    """Enough of a Connection for the Select step's index bookkeeping.

    No service attribute on purpose: touching one would be a tenant call, and
    reaching for it in a test is exactly the mistake worth failing loudly on.
    """

    target = _StubTarget()


def _select_step_app(**state_kwargs):
    from wdmigrator.ui.state import STATE_KEY, WizardState

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    state = WizardState(step="select", **state_kwargs)
    state.source.connection = _StubConnection()
    at.session_state[STATE_KEY] = state
    at.run(timeout=20)
    return at


def test_select_step_renders_with_the_default_object_kind():
    at = _select_step_app()
    assert not at.exception
    assert "Select" in [h.value for h in at.header]


def test_choosing_dashboards_renders_the_dashboard_section():
    """The dashboard picker and its two extra indexes only appear when asked
    for, so a user without an implementer account never sees a section they
    cannot use."""
    at = _select_step_app()
    kinds = [w for w in at.multiselect if w.key == "object_kinds"]
    assert kinds, "object kind chooser is missing"
    kinds[0].set_value(["reports", "dashboards"]).run(timeout=20)
    assert not at.exception
    assert any("Dashboard index" in str(w.value) for w in at.markdown)


def test_the_implementer_requirement_is_explained_rather_than_shown_as_an_error():
    at = _select_step_app(implementer_required=True)
    kinds = [w for w in at.multiselect if w.key == "object_kinds"]
    kinds[0].set_value(["dashboards"]).run(timeout=20)
    assert not at.exception
    rendered = " ".join(str(w.value) for w in at.markdown)
    assert "implementer account" in rendered


def test_next_button_is_disabled_with_no_credentials_entered():
    # Keyed on "nav_next" rather than the button's label: the label is a
    # presentation choice that the Commit rebrand already changed once, but
    # the key is the wizard's actual gating contract.
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.run(timeout=15)
    next_buttons = [b for b in at.button if b.key == "nav_next"]
    assert next_buttons
    assert next_buttons[0].disabled
