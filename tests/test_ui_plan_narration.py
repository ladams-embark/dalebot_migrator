"""Plan has to say what it is doing.

``plan.py`` composes three engine stages behind dividers. Both
``resolve.render`` and ``conflicts.render`` kept their explanatory caption
inside ``if heading:`` — and Plan is the only caller in the product, passing
``heading=False``. So the sentences explaining what a dependency closure is
and what the destination probe does were written, were correct, and never
reached a single user. These tests are what stop that happening again.
"""

from __future__ import annotations

import pathlib
import time

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.api import Closure, Index
from wdmigrator.ui.state import STATE_KEY, WizardState

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _StubTarget:
    tenant = "stub_tenant"


class _StubConnection:
    target = _StubTarget()


def _plan_step_app(**kwargs):
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    state = WizardState(step="plan", closure=Closure(), **kwargs)
    state.source.connection = _StubConnection()
    state.dest.connection = _StubConnection()
    for attr, kind in (
        ("dest_cf_index", "calculated_field"),
        ("dest_measure_index", "calculated_measure"),
    ):
        setattr(state, attr, Index(kind=kind, tenant="stub_tenant", fetched_at=time.time()))
    at.session_state[STATE_KEY] = state
    at.run(timeout=20)
    return at


def _text(at) -> str:
    return " ".join(str(w.value) for w in at.markdown)


def test_the_dependency_stage_explains_itself():
    at = _plan_step_app()
    assert not at.exception
    rendered = _text(at)
    assert "1. Everything that has to come along" in rendered
    assert "child-most-first" in rendered or "children before" in rendered


def test_the_destination_stage_explains_itself_and_says_it_does_not_write():
    at = _plan_step_app()
    rendered = _text(at)
    assert "2. What the destination already has" in rendered
    assert "nothing is written on this step" in rendered.lower()


def test_the_three_stages_are_numbered_so_they_read_as_separate_questions():
    """Three stages behind plain dividers read as one undifferentiated wall."""
    at = _plan_step_app()
    rendered = _text(at)
    assert "1. " in rendered and "2. " in rendered


def test_a_package_run_still_gets_a_dependency_explanation():
    """With a package loaded the closure is not recomputed, but the user still
    has to be told what the list in front of them is."""
    from wdmigrator.api import package_from_closure

    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    state = WizardState(step="plan", closure=Closure())
    state.package = package_from_closure(
        Closure(), name="sample", description="", source_tenant="t"
    )
    state.source.connection = _StubConnection()
    state.dest.connection = _StubConnection()
    for attr, kind in (
        ("dest_cf_index", "calculated_field"),
        ("dest_measure_index", "calculated_measure"),
    ):
        setattr(state, attr, Index(kind=kind, tenant="stub_tenant", fetched_at=time.time()))
    at.session_state[STATE_KEY] = state
    at.run(timeout=20)
    assert not at.exception
    rendered = _text(at)
    assert "1. Everything that has to come along" in rendered
    assert "captured in the package" in rendered
