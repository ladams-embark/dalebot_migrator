"""Offline tests for the write guard.

These are the most important tests in the project: they are what stands
between a plan and an irreversible write to a live tenant. No markers, no
network, no .env.
"""

import pytest

from wdmigrator.config.targets import target_from_parts
from wdmigrator.safety import (
    ALLOW_NON_IMPL_ENV_VAR,
    GuardViolation,
    Level,
    WriteGuard,
    assert_write_allowed,
    blocking_guards,
    evaluate_guards,
)

IMPL_HOST = "impl-services1.wd12.myworkday.com"
PROD_HOST = "wd12.myworkday.com"

SOURCE = target_from_parts(IMPL_HOST, "commitconsulting_dpt1")
DEST = target_from_parts(IMPL_HOST, "client_sandbox_tenant")


def ready_to_write(**overrides) -> WriteGuard:
    """A guard that passes every check, so each test can break exactly one thing."""
    defaults = dict(
        source=SOURCE,
        dest=DEST,
        dry_run=False,
        plan_hash="abc123",
        dry_run_reviewed=True,
        source_verified=True,
        dest_verified=True,
        source_username="src_isu",
        dest_username="dest_isu",
    )
    return WriteGuard(**{**defaults, **overrides})


class TestDefaults:
    def test_dry_run_defaults_to_true(self):
        """Callers must opt in to writing; forgetting the flag must not write."""
        assert WriteGuard(source=SOURCE, dest=DEST).dry_run is True

    def test_the_happy_path_actually_passes(self):
        """Guard against writing rules so strict that nothing legitimate works."""
        assert blocking_guards(ready_to_write()) == []
        assert_write_allowed(ready_to_write())  # must not raise


class TestSameTenantBlock:
    def test_same_tenant_blocks_live_execution(self):
        guard = ready_to_write(dest=SOURCE)
        ids = {g.id for g in blocking_guards(guard)}
        assert "same_tenant" in ids

    def test_same_tenant_raises_even_when_everything_else_is_perfect(self):
        with pytest.raises(GuardViolation, match="same tenant"):
            assert_write_allowed(ready_to_write(dest=SOURCE))

    def test_same_tenant_is_not_overridable_by_the_env_var(self, monkeypatch):
        """The prod override must not double as a same-tenant override."""
        monkeypatch.setenv(ALLOW_NON_IMPL_ENV_VAR, "1")
        with pytest.raises(GuardViolation):
            assert_write_allowed(
                ready_to_write(dest=SOURCE, confirmed_tenant_name=SOURCE.tenant)
            )

    def test_same_tenant_detected_across_case_differences(self):
        aliased = target_from_parts(IMPL_HOST.upper(), SOURCE.tenant.upper())
        with pytest.raises(GuardViolation):
            assert_write_allowed(ready_to_write(dest=aliased))

    def test_same_tenant_is_surfaced_during_dry_run_too(self):
        """Shown from the first screen rather than sprung at the execute step."""
        guard = WriteGuard(source=SOURCE, dest=SOURCE, dry_run=True)
        assert "same_tenant" in {g.id for g in evaluate_guards(guard)}

    def test_dry_run_is_still_permitted_with_the_same_tenant(self):
        """The current .env is same-tenant; the whole flow must stay exercisable."""
        guard = WriteGuard(source=SOURCE, dest=SOURCE, dry_run=True)
        # Only the same-tenant finding — no write-path rules leak into dry run.
        assert {g.id for g in evaluate_guards(guard)} == {"same_tenant"}


class TestDestinationEnvironment:
    @pytest.mark.parametrize(
        "host, tenant",
        [
            (PROD_HOST, "acme_prod"),  # production
            ("mystery.example.com", "acme"),  # unknown -> treated as production
        ],
    )
    def test_unsafe_destination_blocks_without_override(self, host, tenant, monkeypatch):
        monkeypatch.delenv(ALLOW_NON_IMPL_ENV_VAR, raising=False)
        guard = ready_to_write(dest=target_from_parts(host, tenant))
        assert "unsafe_destination_environment" in {
            g.id for g in blocking_guards(guard)
        }

    def test_a_tenant_named_sandbox_on_prod_is_still_blocked(self, monkeypatch):
        monkeypatch.delenv(ALLOW_NON_IMPL_ENV_VAR, raising=False)
        guard = ready_to_write(dest=target_from_parts(PROD_HOST, "acme_sandbox"))
        with pytest.raises(GuardViolation):
            assert_write_allowed(guard)

    def test_override_requires_retyping_the_tenant_name(self, monkeypatch):
        monkeypatch.setenv(ALLOW_NON_IMPL_ENV_VAR, "1")
        guard = ready_to_write(dest=target_from_parts(PROD_HOST, "acme_prod"))
        assert "unconfirmed_tenant_name" in {g.id for g in blocking_guards(guard)}

    def test_override_plus_correct_name_permits_the_write(self, monkeypatch):
        monkeypatch.setenv(ALLOW_NON_IMPL_ENV_VAR, "1")
        guard = ready_to_write(
            dest=target_from_parts(PROD_HOST, "acme_prod"),
            confirmed_tenant_name="acme_prod",
        )
        assert_write_allowed(guard)  # must not raise

    def test_a_wrong_confirmation_name_does_not_pass(self, monkeypatch):
        monkeypatch.setenv(ALLOW_NON_IMPL_ENV_VAR, "1")
        guard = ready_to_write(
            dest=target_from_parts(PROD_HOST, "acme_prod"),
            confirmed_tenant_name="acme_pro",
        )
        with pytest.raises(GuardViolation):
            assert_write_allowed(guard)

    @pytest.mark.parametrize("value", ["", "0", "true", "yes", "TRUE"])
    def test_only_exactly_1_enables_the_override(self, value, monkeypatch):
        """A truthy-looking string must not accidentally unlock production."""
        monkeypatch.setenv(ALLOW_NON_IMPL_ENV_VAR, value)
        guard = ready_to_write(dest=target_from_parts(PROD_HOST, "acme_prod"))
        with pytest.raises(GuardViolation):
            assert_write_allowed(guard)


class TestDryRunPrerequisite:
    def test_live_run_requires_a_reviewed_dry_run(self):
        """You can only go live on a plan you have already dry-run."""
        guard = ready_to_write(dry_run_reviewed=False)
        assert "dry_run_not_reviewed" in {g.id for g in blocking_guards(guard)}

    def test_live_run_requires_a_plan(self):
        guard = ready_to_write(plan_hash="", dry_run_reviewed=False)
        assert "no_plan" in {g.id for g in blocking_guards(guard)}


class TestConnectionVerification:
    @pytest.mark.parametrize(
        "kwargs, expected",
        [
            ({"source_verified": False}, "source_unverified"),
            ({"dest_verified": False}, "dest_unverified"),
        ],
    )
    def test_unverified_connections_block(self, kwargs, expected):
        assert expected in {g.id for g in blocking_guards(ready_to_write(**kwargs))}


class TestWarnings:
    def test_same_isu_username_warns_but_does_not_block(self):
        guard = ready_to_write(source_username="same_isu", dest_username="same_isu")
        findings = {g.id: g for g in evaluate_guards(guard)}
        assert findings["same_isu_username"].level is Level.WARN
        assert_write_allowed(guard)  # a warning must not stop a legitimate write


class TestDryRunNeverWrites:
    def test_reaching_the_write_path_in_dry_run_is_an_error(self):
        """A dry run must not call a write path at all; if it does, fail loudly."""
        with pytest.raises(GuardViolation, match="dry run"):
            assert_write_allowed(ready_to_write(dry_run=True))


class TestGuardReporting:
    def test_all_problems_are_reported_at_once(self):
        """So the user fixes everything in one pass, not one refused click at a time."""
        guard = ready_to_write(
            source_verified=False, dest_verified=False, dry_run_reviewed=False
        )
        ids = {g.id for g in blocking_guards(guard)}
        assert {"source_unverified", "dest_unverified", "dry_run_not_reviewed"} <= ids

    def test_every_guard_tells_the_user_how_to_fix_it(self):
        guard = ready_to_write(dest=SOURCE, source_verified=False)
        for finding in evaluate_guards(guard):
            assert finding.title and finding.detail and finding.remedy

    def test_violation_message_names_the_failures(self):
        with pytest.raises(GuardViolation) as excinfo:
            assert_write_allowed(ready_to_write(source_verified=False))
        assert "Source connection not verified" in str(excinfo.value)
