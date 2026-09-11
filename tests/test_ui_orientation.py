"""Three things a user working alone has nobody to ask about.

How long the next step will take, what the words mean, and what to do if the
run turns out to have been a mistake. None of them changes what the tool
writes; all of them are the difference between a wizard you can drive on your
own and one you need somebody sitting next to you for.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.api import Action, WriteRecord, WriteStatus
from wdmigrator.config.targets import target_from_parts
from wdmigrator.ui.state import STATE_KEY, WizardState
from wdmigrator.ui.steps import results as results_step
from wdmigrator.ui.steps import scope as scope_step

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestScopeEstimate:
    """Scope's whole reason to exist is that the sweeps are expensive enough
    to choose before starting. It was asking for the choice without ever
    saying what it cost — and the report catalog alone is ~2.5 minutes, long
    enough that a user with no number in front of them concludes it hung."""

    def test_reports_carry_the_slow_catalog_sweep(self):
        line = scope_step._sweep_estimate(["reports"])
        assert "min" in line, "the report sweep is minutes, and should read as minutes"

    def test_calculated_fields_alone_are_much_faster_than_reports(self):
        fields = scope_step._sweep_estimate(["calculated_fields"])
        reports = scope_step._sweep_estimate(["reports"])
        assert fields != reports

    def test_more_kinds_never_estimate_less_time(self):
        assert scope_step._sweep_seconds(
            ["reports", "dashboards", "time_calculations"]
        ) > scope_step._sweep_seconds(["reports"])

    def test_the_destination_sweeps_are_counted(self):
        """They run on Select too — cross-tenant matching needs them before
        Plan can probe anything — so leaving them out would under-promise."""
        kinds = scope_step._sweep_kinds(["reports"])
        assert kinds.count("calculated_field") == 2, "source and destination"
        assert "calculated_measure" in kinds

    def test_dashboards_drag_in_the_prompt_sweeps(self):
        """Picking a dashboard sweeps prompt sets and prompt fields too — the
        estimate has to cover what the next step will actually do."""
        kinds = scope_step._sweep_kinds(["dashboards"])
        assert {"dashboard", "prompt_set", "prompt_field"} <= set(kinds)

    def test_it_says_picking_can_start_sooner(self):
        """The number is the total. Without this the user waits for all of it
        before touching anything, which is not required."""
        assert "start picking sooner" in scope_step._sweep_estimate(["reports"])


class TestGlossary:
    def _app(self, step="connect"):
        at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        at.session_state[STATE_KEY] = WizardState(step=step)
        at.run(timeout=20)
        return at

    def test_it_is_on_the_page(self):
        at = self._app()
        assert not at.exception
        assert [e for e in at.expander if "Glossary" in str(e.label)]

    @pytest.mark.parametrize(
        "term", ["WID", "ISU", "Implementer account", "Dependency closure", "Worklet"]
    )
    def test_the_load_bearing_terms_are_defined(self, term):
        at = self._app()
        glossary = [e for e in at.expander if "Glossary" in str(e.label)][0]
        rendered = " ".join(str(m.value) for m in glossary.markdown)
        assert f"**{term}**" in rendered

    def test_it_is_on_every_step_not_just_the_first(self):
        """The terms are not front-loaded — "worklet" first matters on Plan
        and "INDETERMINATE" on Results."""
        for step in ("scope", "select", "results"):
            at = self._app(step)
            assert [e for e in at.expander if "Glossary" in str(e.label)], step

    def test_it_survives_a_step_that_could_not_render(self):
        """The error path used to return before the nav bar, taking the
        glossary and the Back button with it."""
        at = self._app("plan")
        rendered = " ".join(str(m.value) for m in at.markdown)
        assert "Unexpected error" in rendered, "this state is the one that fails"
        assert [e for e in at.expander if "Glossary" in str(e.label)]
        assert [b for b in at.button if b.key == "nav_back_error"]

    def test_a_failed_step_still_offers_a_way_back(self):
        at = self._app("plan")
        at.button(key="nav_back_error").click().run(timeout=20)
        assert at.session_state[STATE_KEY].step == "select"


def _record(action, status, *, name, kind="report", dest_wid="DEST1", node_id="report:S1"):
    return WriteRecord(
        node_id=node_id,
        kind=kind,
        name=name,
        reference_id="R",
        action=action,
        status=status,
        dry_run=False,
        dest_wid=dest_wid,
    )


class TestRollbackWorksheet:
    """There is no rollback — the service has no delete operation. But "you
    cannot undo this" is not the same as "you have no idea what to undo", and
    the destination WIDs someone would need to do it by hand live in this
    run's records and nowhere else once the tab closes."""

    def _state(self) -> WizardState:
        state = WizardState(step="results")
        state.dest.target = target_from_parts(
            "impl-services1.wd12.myworkday.com", "commitconsulting_dpt5"
        )
        return state

    def test_created_objects_are_listed_with_their_destination_wid(self):
        records = [_record(Action.CREATE, WriteStatus.SUCCESS, name="Headcount")]
        sheet = results_step._rollback_worksheet(self._state(), records)
        assert "Headcount" in sheet
        assert "destination WID: DEST1" in sheet
        assert "commitconsulting_dpt5" in sheet

    def test_it_says_plainly_that_there_is_no_rollback(self):
        sheet = results_step._rollback_worksheet(self._state(), [])
        assert "has no delete operation" in sheet

    def test_skipped_objects_are_excluded_and_the_exclusion_is_explained(self):
        """A SKIP wrote nothing. Listing it would send someone hunting for an
        object to delete that was already there before the run."""
        records = [
            _record(Action.CREATE, WriteStatus.SUCCESS, name="Created"),
            _record(Action.SKIP, WriteStatus.SKIPPED, name="AlreadyThere"),
        ]
        sheet = results_step._rollback_worksheet(self._state(), records)
        assert "Created" in sheet
        assert "AlreadyThere" not in sheet
        assert "reused unchanged" in sheet

    def test_updates_are_called_out_as_unreversible_separately(self):
        """Reversing an update needs the definition that was there before it,
        which this tool never held. Saying so beats an unexplained absence."""
        records = [_record(Action.UPDATE, WriteStatus.SUCCESS, name="Overwritten")]
        sheet = results_step._rollback_worksheet(self._state(), records)
        assert "Overwritten" in sheet
        assert "cannot reconstruct" in sheet

    def test_indeterminate_objects_are_flagged_in_place(self):
        """The PUT left the tool and no answer came back. Whoever is undoing
        this needs to check rather than assume in either direction."""
        records = [
            _record(Action.CREATE, WriteStatus.INDETERMINATE, name="Maybe", dest_wid=None)
        ]
        sheet = results_step._rollback_worksheet(self._state(), records)
        assert "INDETERMINATE" in sheet
        assert "may or may not" in sheet
        assert "(not returned)" in sheet

    def test_a_run_that_created_nothing_says_so(self):
        records = [_record(Action.SKIP, WriteStatus.SKIPPED, name="AlreadyThere")]
        sheet = results_step._rollback_worksheet(self._state(), records)
        assert "every object already existed" in sheet

    def test_a_failed_create_is_not_listed_as_something_to_delete(self):
        records = [_record(Action.CREATE, WriteStatus.FAILED, name="NeverLanded")]
        sheet = results_step._rollback_worksheet(self._state(), records)
        assert "NeverLanded" not in sheet

    def test_it_is_offered_after_a_live_run(self):
        state = self._state()
        state.execute_records = [
            _record(Action.CREATE, WriteStatus.SUCCESS, name="Headcount")
        ]
        at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        at.session_state[STATE_KEY] = state
        at.run(timeout=20)
        assert not at.exception
        labels = [str(b.label) for b in at.download_button]
        assert any("rollback worksheet" in label.lower() for label in labels)

    def test_it_is_not_offered_after_a_dry_run(self):
        """Nothing was written, so there is nothing to undo — offering the
        worksheet would imply otherwise."""
        state = self._state()
        state.dry_run_records = [
            _record(Action.CREATE, WriteStatus.SUCCESS, name="Headcount")
        ]
        at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        at.session_state[STATE_KEY] = state
        at.run(timeout=20)
        assert not at.exception
        labels = [str(b.label) for b in at.download_button]
        assert not any("rollback worksheet" in label.lower() for label in labels)
