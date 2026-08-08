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
from wdmigrator.ui import state as ui_state
from wdmigrator.ui.runner import JobState
from wdmigrator.ui.steps import confirm, conflicts, connect, execute, resolve, select

SOURCE = api.target_from_parts("impl-services1.wd12.myworkday.com", "source_tenant")
DEST = api.target_from_parts("impl-services1.wd12.myworkday.com", "dest_tenant")


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
        state.cf_index = api.Index(kind="calculated_field", tenant="t", fetched_at=time.time())
        state.selected_dashboards = {"DB1": {}}
        assert any("Prompt set index" in b.title for b in select.gate(state))

        state.prompt_set_index = api.Index(kind="prompt_set", tenant="t", fetched_at=time.time())
        assert select.gate(state) == []


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
        assert conflicts.gate(ui_state.WizardState())

    def test_delegates_to_validate_plan(self):
        state = ui_state.WizardState()
        state.plan = api.MigrationPlan()
        assert conflicts.gate(state) == api.validate_plan(state.plan)


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

    def test_resolve_scoped_reset_keeps_selections_and_indexes(self):
        """Re-resolving after an override shouldn't force rebuilding a
        158-second report index."""
        state = ui_state.WizardState()
        state.cf_index = api.Index(kind="calculated_field", tenant="t", fetched_at=time.time())
        state.selected_field_wids = {"W1"}
        state.plan = api.MigrationPlan()

        ui_state.reset_downstream(state, from_step="resolve")

        assert state.cf_index is not None
        assert state.selected_field_wids == {"W1"}
        assert state.plan is None

    def test_never_leaves_the_current_step_past_the_wiped_point(self):
        state = ui_state.WizardState()
        state.step = "results"
        ui_state.reset_downstream(state, from_step="conflicts")
        assert state.step == "conflicts"


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
