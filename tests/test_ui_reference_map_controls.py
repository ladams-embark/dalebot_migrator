"""The Save/Reuse controls on the reference table, rendered.

The store itself is covered in ``test_reference_maps.py``. What matters here
is the wiring: reuse must be a click rather than an automatic rewrite of what
gets written, and applying one must move the plan hash so the review that
covered the old payloads does not silently cover the new ones.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.api import ReferenceAction, ReferenceDecision
from wdmigrator.config.targets import target_from_parts
from wdmigrator.ui import reference_maps
from wdmigrator.ui.state import STATE_KEY, WizardState
from wdmigrator.ui.steps import execute as execute_step

ROOT = pathlib.Path(__file__).resolve().parents[1]

DEST = "commitconsulting_dpt5"


class _StubConnection:
    pass


def _state() -> WizardState:
    from wdmigrator.migrate.planner import MigrationPlan
    from wdmigrator.migrate.writer import BlockingReference

    from tests.test_ui_reference_decisions import node_with_reference

    state = WizardState(step="run")
    state.dest.target = target_from_parts("impl-services1.wd12.myworkday.com", DEST)
    state.dest.connection = _StubConnection()
    state.source.connection = _StubConnection()
    state.plan = MigrationPlan(ordered_nodes=[node_with_reference()])
    state.blocking_references = {
        "ORG_WID": {
            "reference": BlockingReference(value="ORG_WID", id_type="WID"),
            "node_name": "Jordan Demo",
            "elements": ["Instance_Reference"],
            "business": {"Organization_Reference_ID": "TOP"},
            "other_objects": [],
        }
    }
    return state


def _render(state, tmp_path, monkeypatch):
    """Render just the map controls — the surrounding step needs a live plan
    pipeline that has nothing to do with what is under test."""
    monkeypatch.setattr(reference_maps, "MAP_DIR", tmp_path)
    at = AppTest.from_string(
        "import streamlit as st\n"
        "from wdmigrator.ui.steps import execute\n"
        "execute._render_reference_map_controls(st.session_state['s'])\n"
    )
    at.session_state["s"] = state
    at.run(timeout=15)
    assert not at.exception, at.exception
    return at


def _saved_map(tmp_path, **decisions):
    reference_maps.save_map(decisions, dest_tenant=DEST, directory=tmp_path)


class TestReuse:
    def test_reuse_is_disabled_with_no_saved_map(self, tmp_path, monkeypatch):
        at = _render(_state(), tmp_path, monkeypatch)
        button = [b for b in at.button if b.key == "refmap_apply"][0]
        assert button.disabled
        assert "(0)" in button.label

    def test_a_saved_map_that_covers_this_run_is_offered(self, tmp_path, monkeypatch):
        _saved_map(
            tmp_path,
            ORG_WID=ReferenceDecision(source_wid="ORG_WID", action=ReferenceAction.BLANK),
        )
        at = _render(_state(), tmp_path, monkeypatch)
        button = [b for b in at.button if b.key == "refmap_apply"][0]
        assert not button.disabled
        assert "(1)" in button.label

    def test_it_is_never_applied_without_a_click(self, tmp_path, monkeypatch):
        """Applying a map rewrites what gets written. Doing that on render,
        from a file on disk, is precisely the unreviewed change this tool is
        built to refuse."""
        _saved_map(
            tmp_path,
            ORG_WID=ReferenceDecision(source_wid="ORG_WID", action=ReferenceAction.BLANK),
        )
        state = _state()
        _render(state, tmp_path, monkeypatch)
        assert state.reference_decisions == {}

    def test_a_map_answering_nothing_in_this_run_says_so(self, tmp_path, monkeypatch):
        """Otherwise a greyed-out button with a saved map present reads as a
        bug rather than as "different objects this time"."""
        _saved_map(
            tmp_path,
            SOMETHING_ELSE=ReferenceDecision(
                source_wid="SOMETHING_ELSE", action=ReferenceAction.BLANK
            ),
        )
        at = _render(_state(), tmp_path, monkeypatch)
        captions = " ".join(str(c.value) for c in at.caption)
        assert "answers none of the references below" in captions

    def test_an_unreadable_map_warns_and_does_not_stop_the_table(self, tmp_path, monkeypatch):
        (tmp_path / f"{DEST}.json").write_text("{not json", encoding="utf-8")
        at = _render(_state(), tmp_path, monkeypatch)
        rendered = " ".join(str(m.value) for m in at.markdown)
        assert "could not be read" in rendered
        assert [b for b in at.button if b.key == "refmap_apply"]


class TestSave:
    def test_save_is_disabled_until_something_has_been_decided(self, tmp_path, monkeypatch):
        at = _render(_state(), tmp_path, monkeypatch)
        assert [b for b in at.button if b.key == "refmap_save"][0].disabled

    def test_decisions_are_written_under_the_destination_tenant(self, tmp_path, monkeypatch):
        state = _state()
        state.reference_decisions = {
            "ORG_WID": ReferenceDecision(
                source_wid="ORG_WID",
                action=ReferenceAction.REPLACE,
                replacement_type="Organization_Reference_ID",
                replacement_value="DEST_TOP",
            )
        }
        at = _render(state, tmp_path, monkeypatch)
        at.button(key="refmap_save").click().run(timeout=15)
        assert not at.exception

        loaded = reference_maps.load_map(DEST, directory=tmp_path)
        assert loaded.decisions["ORG_WID"].replacement_value == "DEST_TOP"

    def test_nothing_is_offered_when_the_destination_is_unknown(self, tmp_path, monkeypatch):
        """A map keyed to no tenant could be applied to any tenant, which is
        the one thing the key exists to prevent."""
        state = _state()
        state.dest.target = None
        at = _render(state, tmp_path, monkeypatch)
        assert not [b for b in at.button if b.key in ("refmap_save", "refmap_apply")]
