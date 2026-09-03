"""Pure-logic tests for the wizard's state model and each step's `gate()`.

`gate()` functions are deliberately pure — they read `WizardState` and call
engine functions (`validate_plan`, `evaluate_guards`), never `st.*` — so this
exercises exactly the logic that decides whether Execute is reachable
without needing to simulate a Streamlit rerun. `test_ui_app_smoke.py` covers
the "does the app actually boot" side; this covers "is the gating logic
itself correct."
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("streamlit")

from wdmigrator import api
from wdmigrator.ui import indexes, state as ui_state
from wdmigrator.ui.runner import JobState
from wdmigrator.ui.steps import confirm, conflicts, connect, execute, plan, resolve, results, run, select

SOURCE = api.target_from_parts("impl-services1.wd12.myworkday.com", "source_tenant")
DEST = api.target_from_parts("impl-services1.wd12.myworkday.com", "dest_tenant")


def _index(kind: str) -> api.Index:
    return api.Index(kind=kind, tenant="t", fetched_at=time.time())


def _matching_ready(state: ui_state.WizardState) -> ui_state.WizardState:
    """Give the wizard the two destination sweeps cross-tenant matching needs."""
    state.dest_cf_index = _index("calculated_field")
    state.dest_measure_index = _index("calculated_measure")
    return state


def _verified_side(target: api.TenantTarget, username: str = "u") -> ui_state.ConnectionState:
    side = ui_state.ConnectionState(target=target, username=username, password="pw")
    side.connection = object()  # gate logic only checks identity, never dereferences it
    side.status = api.ConnectionStatus(ok=True, detail="ok", fingerprint="fp", checked_at=time.time())
    side.verified_fingerprint = "fp"
    return side


class TestConnectStepGate:
    def test_blocks_until_both_sides_verified(self):
        state = ui_state.WizardState()
        assert connect.gate(state)

        state.source = _verified_side(SOURCE)
        assert any("Destination" in b.title for b in connect.gate(state))

        state.dest = _verified_side(DEST)
        assert connect.gate(state) == []


class TestSelectStepGate:
    def test_requires_a_selection_and_the_complete_cf_index(self):
        state = ui_state.WizardState()
        blockers = select.gate(state)
        assert any("Nothing selected" in b.title for b in blockers)
        assert any("index" in b.title.lower() for b in blockers)

        state.selected_field_wids = {"W1"}
        state.cf_index = api.Index(kind="calculated_field", tenant="t", fetched_at=time.time())
        assert select.gate(state) == []

    def test_cf_index_is_required_even_for_a_report_only_selection(self):
        """The index isn't optional just because nothing was picked from it
        directly — the resolver needs it to classify every WID a report
        references."""
        state = ui_state.WizardState()
        state.selected_reports = {"W2": {}}
        blockers = select.gate(state)
        assert any("index" in b.title.lower() for b in blockers)

    def test_a_dashboard_selection_satisfies_the_selection_requirement(self):
        state = ui_state.WizardState()
        state.selected_dashboards = {"DB1": {}}
        assert not any("Nothing selected" in b.title for b in select.gate(state))

    def test_a_dashboard_selection_requires_the_prompt_set_index(self):
        """Prompt sets cannot be fetched on demand — Workday's request criteria
        for them do not filter — so the index is the only route, and a dashboard
        that binds one would otherwise resolve as if it had no prompts."""
        state = ui_state.WizardState()
        state.cf_index = _index("calculated_field")
        state.selected_dashboards = {"DB1": {}}
        assert any("Prompt set index" in b.title for b in select.gate(state))

        state.prompt_set_index = _index("prompt_set")
        state.prompt_field_index = _index("prompt_field")
        state.gauge_range_index = _index("gauge_range")
        state.analytic_indicator_index = _index("analytic_indicator")
        assert select.gate(state) == []

    def test_a_dashboard_selection_requires_the_prompt_field_index(self):
        """A prompt set names its parameters as prompt fields, and `resolve`
        does not even look for that kind of reference when the index is absent
        — the dependency never enters the closure, so the gap surfaces as a
        live write failure rather than here."""
        state = ui_state.WizardState()
        state.cf_index = _index("calculated_field")
        state.prompt_set_index = _index("prompt_set")
        state.gauge_range_index = _index("gauge_range")
        state.analytic_indicator_index = _index("analytic_indicator")
        state.selected_dashboards = {"DB1": {}}
        assert any("Prompt field index" in b.title for b in select.gate(state))

        state.prompt_field_index = _index("prompt_field")
        assert select.gate(state) == []

    @pytest.mark.parametrize(
        "attr, kind, title",
        [
            ("gauge_range_index", "gauge_range", "Gauge range index"),
            ("analytic_indicator_index", "analytic_indicator", "Analytic indicator index"),
        ],
    )
    def test_a_report_selection_requires_the_report_dependency_indexes(
        self, attr, kind, title
    ):
        """Both are report dependencies — a gauge layout names a gauge range, a
        matrix measure names an analytic indicator — and both are silently
        skipped by `resolve` when the index is None."""
        state = ui_state.WizardState()
        state.cf_index = _index("calculated_field")
        state.gauge_range_index = _index("gauge_range")
        state.analytic_indicator_index = _index("analytic_indicator")
        state.selected_reports = {"R1": {}}
        assert select.gate(state) == []

        setattr(state, attr, None)
        assert any(title in b.title for b in select.gate(state))

    def test_a_calculated_field_only_selection_needs_none_of_the_report_indexes(self):
        """Nothing here depends on a gauge range or an indicator, so demanding
        those sweeps would be two pointless tenant calls."""
        state = ui_state.WizardState()
        state.cf_index = _index("calculated_field")
        state.selected_field_wids = {"W1"}
        assert select.gate(state) == []


class TestHydrateWizardState:
    def test_fills_fields_added_after_the_session_started(self):
        state = ui_state.WizardState()
        delattr(state, "hold_step")
        delattr(state, "run_log_path")
        ui_state.hydrate_wizard_state(state)
        assert state.hold_step is False
        assert state.run_log_path == ""

    def test_leaves_existing_values_alone(self):
        state = ui_state.WizardState(step="plan", hold_step=True)
        ui_state.hydrate_wizard_state(state)
        assert state.step == "plan"
        assert state.hold_step is True


class TestWizardStepOrder:
    def test_the_visible_wizard_is_five_steps(self):
        assert ui_state.STEP_ORDER == ["connect", "select", "plan", "run", "results"]
        for step_id in ui_state.STEP_ORDER:
            assert step_id in ui_state.STEP_TITLES


class TestPlanStepGate:
    def test_blocks_until_the_closure_exists(self):
        assert plan.gate(ui_state.WizardState())

    def test_blocks_until_the_destination_has_been_checked(self):
        state = _matching_ready(ui_state.WizardState())
        state.closure = api.Closure()
        blockers = plan.gate(state)
        assert any("not yet checked" in b.title.lower() or "not swept" in b.title.lower()
                   for b in blockers)


class TestRunStepGate:
    def test_blocks_before_any_execution(self):
        assert run.gate(ui_state.WizardState())

    def test_clears_once_records_exist(self):
        state = ui_state.WizardState()
        state.execute_records = [object()]
        assert run.gate(state) == []


class TestExecuteStartRequiresLiveGate:
    def test_start_stays_disabled_until_the_tenant_name_and_ack_are_set(self):
        state = ui_state.WizardState()
        state.source = _verified_side(SOURCE)
        state.dest = _verified_side(DEST)
        state.plan = api.MigrationPlan()
        state.dry_run_records = [object()]
        state.dry_run_reviewed = True
        state.dry_run_plan_hash = state.plan.plan_hash()
        assert execute._start_disabled(state)
        state.confirmed_tenant_name = DEST.tenant
        assert execute._start_disabled(state)
        state.irreversible_ack = True
        # Remaining engine BLOCKs (nothing to write on an empty plan, etc.)
        # may still disable Start — the live gate itself is no longer why.
        live = confirm.gate_live(state)
        if not live:
            assert not execute._start_disabled(state)


class TestResolveStepGate:
    def test_blocks_without_a_closure(self):
        assert resolve.gate(ui_state.WizardState())

    def test_blocks_on_a_closure_error(self):
        state = ui_state.WizardState()
        state.closure_error = "cycle detected"
        assert resolve.gate(state)

    def test_clears_once_a_closure_is_present(self):
        state = ui_state.WizardState()
        state.closure = api.Closure()
        assert resolve.gate(state) == []


class TestConflictsStepGate:
    def test_blocks_without_a_plan(self):
        assert conflicts.gate(_matching_ready(ui_state.WizardState()))

    def test_delegates_to_validate_plan(self):
        state = _matching_ready(ui_state.WizardState())
        state.plan = api.MigrationPlan()
        assert conflicts.gate(state) == api.validate_plan(state.plan)

    @pytest.mark.parametrize(
        "attr", ["dest_cf_index", "dest_measure_index"]
    )
    def test_blocks_until_the_destination_has_been_swept(self, attr):
        """`Calculated_Field_ID` and `BI_Calculated_Measure_ID` are not
        cross-tenant identities. Probing without a destination sweep reports
        every object whose ID differs as absent, plans a CREATE, and the write
        is then rejected as a duplicate alias — which is exactly how a wizard
        run failed on `Is Top Performer`."""
        state = _matching_ready(ui_state.WizardState())
        state.plan = api.MigrationPlan()
        setattr(state, attr, None)

        blockers = conflicts.gate(state)
        assert any("cross-tenant matching" in b.title for b in blockers)

    def test_the_matching_blocker_outranks_the_not_probed_one(self):
        """Order matters for the remedy shown: telling the user to click
        'Check existence' when the button is disabled is a dead end."""
        state = ui_state.WizardState()
        assert conflicts.gate(state)[0].title.startswith("Destination not swept")


class TestCrossTenantMatchingReachesTheProbe:
    """The wizard must probe the destination the same way the scripts do.

    This is the gap that produced a real failed migration: `iter_check_existence`
    grew `match_index`/`measure_match_index` and the UI kept calling it without
    them, so every calculated field whose `Calculated_Field_ID` differs between
    the tenants — the same field is
    `CRTMNU01_Commit - HR Dashboard_03_Is Top Performer` on one and
    `Custom Object Data - Is Top Performer` on another — probed NOT_FOUND,
    planned as CREATE, and was rejected on write with "Enter a unique WQL alias
    for the business object". The run halted on the first one.

    Both call sites are pinned. A re-probe without the indexes is the worse of
    the two: it would revert matches the first probe got right, so answering one
    reference question would arm a run that duplicates everything.
    """

    def _state(self) -> ui_state.WizardState:
        state = _matching_ready(ui_state.WizardState())
        state.closure = api.Closure()
        state.plan = api.MigrationPlan()
        state.dest = _verified_side(DEST)
        return state

    def _captured_kwargs(self, monkeypatch, module, start) -> dict:
        seen: dict = {}

        def fake(connection, closure, **kwargs):
            seen.update(kwargs)
            return iter(())

        monkeypatch.setattr(module, "iter_check_existence", fake)
        start(self._state())
        return seen

    def test_the_conflicts_probe_is_given_both_match_indexes(self, monkeypatch):
        seen = self._captured_kwargs(monkeypatch, conflicts, conflicts._start_probe)
        assert seen["match_index"] is not None
        assert seen["measure_match_index"] is not None

    def test_the_execute_reprobe_is_given_both_match_indexes(self, monkeypatch):
        seen = self._captured_kwargs(monkeypatch, execute, execute._start_reprobe)
        assert seen["match_index"] is not None
        assert seen["measure_match_index"] is not None

    def test_matching_is_off_only_when_the_destination_was_never_swept(self):
        """`destination_match_indexes` returning {} is what the gate exists to
        prevent, not a supported mode — so it must be reachable *only* from the
        unswept state, never from a half-built one."""
        state = _matching_ready(ui_state.WizardState())
        assert indexes.destination_match_indexes(state).keys() == {
            "match_index", "measure_match_index"
        }

        state.dest_measure_index = None
        assert indexes.destination_match_indexes(state) == {}
        assert not indexes.destination_matching_ready(state)


class TestConfirmStepGate:
    def _state_with_empty_plan(self) -> ui_state.WizardState:
        state = ui_state.WizardState()
        state.source = _verified_side(SOURCE)
        state.dest = _verified_side(DEST)
        state.plan = api.MigrationPlan()
        return state

    def test_blocks_before_any_dry_run(self):
        state = self._state_with_empty_plan()
        blockers = confirm.gate(state)
        assert any("Dry run" in b.title or "Nothing to write" in b.title for b in blockers)

    def test_a_dry_run_for_a_different_plan_hash_does_not_satisfy_the_gate(self):
        """The core rule: live execution requires a dry run for THIS plan,
        not just any dry run. An override in Conflicts changes the plan
        hash, which must invalidate a prior dry run automatically."""
        state = self._state_with_empty_plan()
        state.dry_run_records = [object()]
        state.dry_run_plan_hash = "stale-hash-from-a-different-plan"
        state.dry_run_reviewed = True
        state.confirmed_tenant_name = DEST.tenant
        state.irreversible_ack = True

        blockers = confirm.gate(state)
        assert any("Dry run" in b.title for b in blockers)

    def test_dry_run_reviewed_flag_alone_is_not_enough_without_the_matching_hash(self):
        state = self._state_with_empty_plan()
        state.dry_run_reviewed = True  # ticked, but no actual dry run recorded
        blockers = confirm.gate(state)
        assert any("Dry run" in b.title for b in blockers)

    def test_tenant_name_must_match_exactly(self):
        state = self._state_with_empty_plan()
        state.confirmed_tenant_name = "not_the_dest_tenant"
        blockers = confirm.gate(state)
        assert any("Tenant name" in b.title for b in blockers)

    def test_irreversibility_checkbox_is_required(self):
        state = self._state_with_empty_plan()
        state.dry_run_records = [object()]
        state.dry_run_plan_hash = state.plan.plan_hash()
        state.dry_run_reviewed = True
        state.confirmed_tenant_name = DEST.tenant
        state.irreversible_ack = False
        blockers = confirm.gate(state)
        assert any("Irreversibility" in b.title for b in blockers)

    def test_report_creates_need_no_manual_owner_remap(self):
        """Every report is owned by the fixed destination account — there is
        no per-run owner input left to be missing, so a report CREATE should
        never produce an owner-related blocker."""
        node = api.Node(
            node_id="report:W9", kind=api.NodeKind.REPORT, source_wid="W9",
            reference_id="R9", name="A Report", payload={},
        )
        state = self._state_with_empty_plan()
        state.plan = api.MigrationPlan(ordered_nodes=[node], actions={"report:W9": api.Action.CREATE})
        state.dry_run_records = [object()]
        state.dry_run_plan_hash = state.plan.plan_hash()
        state.dry_run_reviewed = True
        state.confirmed_tenant_name = DEST.tenant
        state.irreversible_ack = True

        blockers = confirm.gate(state)
        assert not any("owner" in b.title.lower() for b in blockers)

    def test_satisfied_confirm_state_has_no_blockers(self):
        node = api.Node(
            node_id="calculated_field:W1", kind=api.NodeKind.CALCULATED_FIELD,
            source_wid="W1", reference_id="CF_A", name="Field", payload={},
        )
        state = self._state_with_empty_plan()
        state.plan = api.MigrationPlan(ordered_nodes=[node], actions={"calculated_field:W1": api.Action.CREATE})
        state.dry_run_records = [object()]
        state.dry_run_plan_hash = state.plan.plan_hash()
        state.dry_run_reviewed = True
        state.confirmed_tenant_name = DEST.tenant
        state.irreversible_ack = True
        assert confirm.gate(state) == []

    def test_same_tenant_source_and_dest_still_blocks_the_live_gate(self):
        """The engine's own guard (`same_tenant`, no override) must surface
        here even when every UI-level checkbox has been satisfied."""
        node = api.Node(
            node_id="calculated_field:W1", kind=api.NodeKind.CALCULATED_FIELD,
            source_wid="W1", reference_id="CF_A", name="Field", payload={},
        )
        state = self._state_with_empty_plan()
        state.plan = api.MigrationPlan(ordered_nodes=[node], actions={"calculated_field:W1": api.Action.CREATE})
        state.dest = _verified_side(SOURCE, username="u")  # identical tenant to source
        state.dry_run_records = [object()]
        state.dry_run_plan_hash = state.plan.plan_hash()
        state.dry_run_reviewed = True
        state.confirmed_tenant_name = SOURCE.tenant
        state.irreversible_ack = True

        blockers = confirm.gate(state)
        assert any(b.title == "Source and destination are the same tenant" for b in blockers)


class TestExecuteStepGate:
    def test_blocks_before_any_execution(self):
        assert execute.gate(ui_state.WizardState())

    def test_blocks_while_a_job_is_in_progress(self):
        state = ui_state.WizardState()
        state.execute_job = JobState()
        assert execute.gate(state)

    def test_clears_once_records_exist_and_no_job_is_running(self):
        state = ui_state.WizardState()
        state.execute_records = [object()]
        assert execute.gate(state) == []


class TestResetDownstream:
    def test_credential_scoped_reset_wipes_indexes_and_everything_after(self):
        state = ui_state.WizardState()
        state.cf_index = api.Index(kind="calculated_field", tenant="t", fetched_at=time.time())
        state.selected_field_wids = {"W1"}
        state.closure = api.Closure()
        state.plan = api.MigrationPlan()
        state.dry_run_reviewed = True
        state.execute_records = [object()]

        ui_state.reset_downstream(state, from_step="select")

        assert state.cf_index is None
        assert state.selected_field_wids == set()
        assert state.closure is None
        assert state.plan is None
        assert state.dry_run_reviewed is False
        assert state.execute_records == []

    def test_credential_scoped_reset_also_wipes_dashboard_state(self):
        """A new credential can mean a different account entirely — including
        one that is no longer an implementer — so the dashboard indexes and the
        implementer flag must not survive it."""
        state = ui_state.WizardState()
        state.dashboard_index = api.Index(kind="dashboard", tenant="t", fetched_at=time.time())
        state.prompt_set_index = api.Index(kind="prompt_set", tenant="t", fetched_at=time.time())
        state.selected_dashboards = {"DB1": {}}
        state.implementer_required = True

        ui_state.reset_downstream(state, from_step="select")

        assert state.dashboard_index is None
        assert state.prompt_set_index is None
        assert state.selected_dashboards == {}
        assert state.implementer_required is False

    def test_credential_scoped_reset_wipes_the_destination_sweeps(self):
        """A destination index swept against a *different* destination would
        vouch for objects this tenant has never had, and every one of those is
        an object the plan would then skip instead of creating."""
        state = _matching_ready(ui_state.WizardState())

        ui_state.reset_downstream(state, from_step="select")

        assert state.dest_cf_index is None
        assert state.dest_measure_index is None

    def test_plan_scoped_reset_keeps_the_destination_sweeps(self):
        """They are scoped to the connection, not the plan — re-resolving after
        an override should not cost a second 25-second destination sweep."""
        state = _matching_ready(ui_state.WizardState())

        ui_state.reset_downstream(state, from_step="plan")

        assert state.dest_cf_index is not None
        assert state.dest_measure_index is not None

    def test_plan_scoped_reset_keeps_selections_and_indexes(self):
        """Re-resolving after an override shouldn't force rebuilding a
        158-second report index."""
        state = ui_state.WizardState()
        state.cf_index = api.Index(kind="calculated_field", tenant="t", fetched_at=time.time())
        state.selected_field_wids = {"W1"}
        state.plan = api.MigrationPlan()

        ui_state.reset_downstream(state, from_step="plan")

        assert state.cf_index is not None
        assert state.selected_field_wids == {"W1"}
        assert state.plan is None

    def test_never_leaves_the_current_step_past_the_wiped_point(self):
        state = ui_state.WizardState()
        state.step = "results"
        ui_state.reset_downstream(state, from_step="plan")
        assert state.step == "plan"

    def test_run_scoped_reset_keeps_the_plan(self):
        state = ui_state.WizardState()
        state.plan = api.MigrationPlan()
        state.dry_run_reviewed = True
        state.execute_records = [object()]
        ui_state.reset_downstream(state, from_step="run")
        assert state.plan is not None
        assert state.dry_run_reviewed is True
        assert state.execute_records == []


class TestBuildGuard:
    def test_dry_run_reviewed_requires_the_plan_hash_to_match(self):
        state = ui_state.WizardState()
        state.source = _verified_side(SOURCE)
        state.dest = _verified_side(DEST)
        state.plan = api.MigrationPlan()
        state.dry_run_reviewed = True
        state.dry_run_plan_hash = "wrong-hash"

        guard = ui_state.build_guard(state, dry_run=False)
        assert guard.dry_run_reviewed is False

        state.dry_run_plan_hash = state.plan.plan_hash()
        guard = ui_state.build_guard(state, dry_run=False)
        assert guard.dry_run_reviewed is True

    def test_dry_run_guard_never_gets_checked_by_assert_write_allowed(self):
        """assert_write_allowed is a caller contract for the live path only —
        calling it on a dry_run=True guard is itself a bug, and raises."""
        state = ui_state.WizardState()
        state.source = _verified_side(SOURCE)
        state.dest = _verified_side(DEST)
        guard = ui_state.build_guard(state, dry_run=True)
        with pytest.raises(api.GuardViolation):
            api.assert_write_allowed(guard)


class TestOwnerReference:
    """Every report this tool creates is owned by a fixed destination
    account — there is no per-run input for it anymore."""

    def test_always_returns_the_fixed_workday_username_regardless_of_state(self):
        state = ui_state.WizardState()
        ref = ui_state.owner_reference(state)
        assert ref["ID"][0]["type"] == "WorkdayUserName"
        assert ref["ID"][0]["_value_1"] == ui_state.DEFAULT_REPORT_OWNER_USERNAME
        assert ui_state.DEFAULT_REPORT_OWNER_USERNAME == "wd-support"


class TestDrain:
    def test_drain_runs_a_local_generator_to_completion(self):
        from wdmigrator.ui.runner import drain, start_job

        def gen():
            yield 1
            yield 2
            return "done"

        job = start_job(gen())
        drain(job)
        assert job.done
        assert job.events == [1, 2]
        assert job.result == "done"
        assert job.error is None


class TestRunLog:
    def test_writes_once_under_out(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        state = ui_state.WizardState()
        record = api.WriteRecord(
            node_id="n1",
            kind="report",
            name="R",
            reference_id="R1",
            action=api.Action.CREATE,
            status=api.WriteStatus.SUCCESS,
            dry_run=True,
        )
        results._write_run_log(state, [record])
        assert state.run_log_path
        path = __import__("pathlib").Path(state.run_log_path)
        assert path.exists()
        assert "migration-" in path.name
        first = path.read_text()
        results._write_run_log(state, [record])
        assert list((__import__("pathlib").Path("out")).glob("migration-*.json")) == [path]
        assert path.read_text() == first
