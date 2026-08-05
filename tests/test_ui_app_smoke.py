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


def test_next_button_is_disabled_with_no_credentials_entered():
    # Keyed on "nav_next" rather than the button's label: the label is a
    # presentation choice that the Commit rebrand already changed once, but
    # the key is the wizard's actual gating contract.
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    at.run(timeout=15)
    next_buttons = [b for b in at.button if b.key == "nav_next"]
    assert next_buttons
    assert next_buttons[0].disabled
