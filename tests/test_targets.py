"""Offline tests for tenant URL parsing and environment classification.

No markers: these are pure logic and must run with no .env and no network.
"""

import pytest

from wdmigrator.config import (
    Environment,
    TenantURLError,
    classify_environment,
    derive_services_host,
    parse_tenant_url,
    target_from_parts,
)

# The exact URL shape a user pastes out of the browser address bar.
LOGIN_URL = "https://impl.wd12.myworkday.com/wday/authgwy/commitconsulting_dpt1/login.htmld"


class TestParseTenantURL:
    @pytest.mark.parametrize(
        "url",
        [
            LOGIN_URL,
            LOGIN_URL.removeprefix("https://"),  # users often drop the scheme
            "http://impl.wd12.myworkday.com/wday/authgwy/commitconsulting_dpt1/login.htmld",
            "https://impl.wd12.myworkday.com/commitconsulting_dpt1/d/home.htmld",
            "https://impl-services1.wd12.myworkday.com/ccx/service/commitconsulting_dpt1/Core_Implementation_Service/v47.0",
        ],
    )
    def test_extracts_tenant_from_every_shape_users_paste(self, url):
        assert parse_tenant_url(url).tenant == "commitconsulting_dpt1"

    def test_ui_host_is_converted_to_services_host(self):
        """The whole point: SOAP must not be sent to the UI host."""
        target = parse_tenant_url(LOGIN_URL)
        assert target.services_host == "impl-services1.wd12.myworkday.com"
        assert target.ui_host == "impl.wd12.myworkday.com"
        assert target.services_host_derived is True

    def test_services_host_input_is_not_double_suffixed(self):
        target = parse_tenant_url(
            "https://impl-services1.wd12.myworkday.com/ccx/service/t1/Svc/v47.0"
        )
        assert target.services_host == "impl-services1.wd12.myworkday.com"
        # Given directly, so no confirmation prompt is needed.
        assert target.services_host_derived is False

    def test_endpoint_puts_version_in_the_path(self):
        """A version only in the SOAP envelope 404s on this tenant."""
        target = parse_tenant_url(LOGIN_URL)
        assert target.endpoint("Core_Implementation_Service", "v47.0") == (
            "https://impl-services1.wd12.myworkday.com/ccx/service/"
            "commitconsulting_dpt1/Core_Implementation_Service/v47.0"
        )
        assert target.wsdl_url("Core_Implementation_Service", "v47.0").endswith("?wsdl")

    @pytest.mark.parametrize(
        "bad, reason",
        [
            ("", "empty"),
            ("   ", "whitespace only"),
            ("commitconsulting_dpt1", "bare tenant name, no host"),
            ("https://impl.wd12.myworkday.com", "host but no path/tenant"),
        ],
    )
    def test_rejects_input_it_cannot_resolve(self, bad, reason):
        """Guessing a host would silently point the tool at the wrong tenant."""
        with pytest.raises(TenantURLError):
            parse_tenant_url(bad)

    def test_error_message_shows_the_expected_format(self):
        with pytest.raises(TenantURLError, match="authgwy"):
            parse_tenant_url("notaurl")


class TestIdentity:
    def test_same_tenant_via_different_url_forms_compares_equal(self):
        """The same-tenant guard depends on this: UI URL and API URL must match."""
        from_ui = parse_tenant_url(LOGIN_URL)
        from_api = parse_tenant_url(
            "https://impl-services1.wd12.myworkday.com/ccx/service/"
            "commitconsulting_dpt1/Core_Implementation_Service/v47.0"
        )
        assert from_ui.identity() == from_api.identity()

    def test_identity_is_case_insensitive(self):
        a = target_from_parts("IMPL-SERVICES1.WD12.MYWORKDAY.COM", "Tenant_A")
        b = target_from_parts("impl-services1.wd12.myworkday.com", "tenant_a")
        assert a.identity() == b.identity()

    def test_different_tenants_on_one_host_are_distinct(self):
        a = target_from_parts("impl-services1.wd12.myworkday.com", "tenant_a")
        b = target_from_parts("impl-services1.wd12.myworkday.com", "tenant_b")
        assert a.identity() != b.identity()


class TestEnvironmentClassification:
    @pytest.mark.parametrize(
        "host, expected",
        [
            ("impl.wd12.myworkday.com", Environment.IMPLEMENTATION),
            ("impl-services1.wd12.myworkday.com", Environment.IMPLEMENTATION),
            ("sbx.wd5.myworkday.com", Environment.SANDBOX),
            ("wd12.myworkday.com", Environment.PRODUCTION),
            ("www.wd12.myworkday.com", Environment.PRODUCTION),
            ("something-unexpected.example.com", Environment.UNKNOWN),
        ],
    )
    def test_classifies_from_host(self, host, expected):
        assert classify_environment(host, "any_tenant") is expected

    def test_tenant_name_cannot_make_a_production_host_look_safe(self):
        """The fail-safe direction: a tenant called *_sandbox on prod is still prod.

        If tenant names could downgrade the classification, naming a production
        tenant "acme_sandbox" would quietly make it a writable target.
        """
        assert (
            classify_environment("wd12.myworkday.com", "acme_sandbox")
            is Environment.PRODUCTION
        )
        assert (
            classify_environment("unknown.example.com", "acme_sandbox")
            is Environment.UNKNOWN
        )

    def test_only_impl_and_sandbox_are_safe_write_targets(self):
        assert Environment.IMPLEMENTATION.is_safe_write_target
        assert Environment.SANDBOX.is_safe_write_target
        assert not Environment.PRODUCTION.is_safe_write_target
        # The critical one — unknown must behave like production.
        assert not Environment.UNKNOWN.is_safe_write_target

    def test_missing_host_is_unknown_not_safe(self):
        assert classify_environment("", "t") is Environment.UNKNOWN


class TestNoCredentialsOnTarget:
    def test_target_carries_no_credential_fields(self):
        """Targets get logged, hashed, and rendered — a secret here would leak."""
        target = parse_tenant_url(LOGIN_URL)
        fields = set(vars(target))
        assert not fields & {"password", "username", "isu_password", "secret"}


class TestDeriveServicesHost:
    def test_flags_derivation_so_the_ui_can_ask_for_confirmation(self):
        _, derived = derive_services_host("impl.wd12.myworkday.com")
        assert derived is True

    def test_does_not_flag_a_host_that_was_already_correct(self):
        host, derived = derive_services_host("impl-services1.wd12.myworkday.com")
        assert (host, derived) == ("impl-services1.wd12.myworkday.com", False)

    def test_rejects_empty_host(self):
        with pytest.raises(TenantURLError):
            derive_services_host("")
