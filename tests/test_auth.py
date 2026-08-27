"""Tests for credential handling and client construction.

Most of these are offline: building a zeep client from the bundled WSDL needs
no tenant, so the endpoint-pinning guarantee — the one that stops a write
landing in the wrong tenant — is provable without a network call.

The `live` tests at the bottom read from the source tenant only and never write.
"""

import pytest

from wdmigrator.auth import (
    AuthError,
    Connection,
    Credentials,
    Role,
    TIME_TRACKING_SERVICE_NAME,
    make_client,
    verify_connection,
)
from wdmigrator.config.targets import target_from_parts
from wdmigrator.secrets import Secret

SOURCE = target_from_parts("impl-services1.wd12.myworkday.com", "commitconsulting_dpt1")
DEST = target_from_parts("impl-services1.wd12.myworkday.com", "some_other_tenant")


def creds(username="lmcneil", password="pw-not-real") -> Credentials:
    return Credentials(username=username, password=Secret(password))


class TestCredentials:
    def test_bare_username_gets_the_tenant_appended(self):
        assert creds().ws_username("acme_impl") == "lmcneil@acme_impl"

    def test_already_qualified_username_is_not_double_suffixed(self):
        """The prototype's bug: it would produce user@tenant@tenant."""
        c = creds(username="lmcneil@acme_impl")
        assert c.ws_username("acme_impl") == "lmcneil@acme_impl"

    def test_qualifier_matching_is_case_insensitive(self):
        c = creds(username="lmcneil@ACME_IMPL")
        assert c.ws_username("acme_impl") == "lmcneil@acme_impl"

    def test_username_qualified_with_a_different_tenant_is_rejected(self):
        """Silently rewriting this would authenticate somewhere unintended."""
        c = creds(username="lmcneil@production_tenant")
        with pytest.raises(AuthError, match="production_tenant"):
            c.ws_username("acme_impl")


class TestEmailAddressUsernames:
    """An ISU username is sometimes an email address.

    That collides with the ``user@tenant`` qualification format: both are
    ``something@something``, so an email username was previously rejected as
    "qualified with the wrong tenant" and there was no way to connect at all.
    """

    def test_an_email_username_gets_the_tenant_appended(self):
        c = creds(username="jane.doe@acme.com")
        assert c.ws_username("acme_impl") == "jane.doe@acme.com@acme_impl"

    def test_a_fully_qualified_email_username_is_not_double_suffixed(self):
        c = creds(username="jane.doe@acme.com@acme_impl")
        assert c.ws_username("acme_impl") == "jane.doe@acme.com@acme_impl"

    def test_a_fully_qualified_email_matches_the_tenant_case_insensitively(self):
        c = creds(username="jane.doe@acme.com@ACME_IMPL")
        assert c.ws_username("acme_impl") == "jane.doe@acme.com@acme_impl"

    def test_an_email_qualified_with_another_tenant_still_appends(self):
        """Not an error: 'acme.com' is a mail domain, not a tenant, so this is
        an unqualified email username rather than a wrong-tenant paste."""
        c = creds(username="jane.doe@acme.com")
        assert c.ws_username("other_tenant") == "jane.doe@acme.com@other_tenant"

    def test_a_dotless_qualifier_is_still_treated_as_a_tenant(self):
        """The dot is what separates the two shapes. Without one this reads as
        a wrong-tenant qualification, which is the safer reading."""
        with pytest.raises(AuthError, match="localdomain"):
            creds(username="jane@localdomain").ws_username("acme_impl")

    def test_the_error_names_the_fully_qualified_escape_hatch(self):
        """The heuristic must never be a dead end — the message has to say how
        to force the literal username through."""
        with pytest.raises(AuthError, match=r"jane@localdomain@acme_impl"):
            creds(username="jane@localdomain").ws_username("acme_impl")

    def test_the_escape_hatch_actually_works(self):
        c = creds(username="jane@localdomain@acme_impl")
        assert c.ws_username("acme_impl") == "jane@localdomain@acme_impl"

    def test_a_username_with_no_local_part_is_rejected_clearly(self):
        """Its own error rather than the wrong-tenant one, which would have read
        'qualified with acme_impl but targets acme_impl'."""
        with pytest.raises(AuthError, match="nothing before"):
            creds(username="@acme_impl").ws_username("acme_impl")

    def test_whitespace_is_stripped(self):
        assert creds(username="  lmcneil  ").ws_username("t") == "lmcneil@t"

    @pytest.mark.parametrize("username", ["", "   "])
    def test_empty_username_rejected(self, username):
        with pytest.raises(AuthError):
            Credentials(username=username, password=Secret("pw"))

    def test_empty_password_rejected(self):
        with pytest.raises(AuthError):
            Credentials(username="u", password=Secret(""))

    def test_fingerprint_excludes_the_password(self):
        a = Credentials("lmcneil", Secret("password-one"))
        b = Credentials("lmcneil", Secret("password-two"))
        assert a.fingerprint(SOURCE) == b.fingerprint(SOURCE)

    def test_fingerprint_changes_with_tenant_and_username(self):
        c = creds()
        assert c.fingerprint(SOURCE) != c.fingerprint(DEST)
        assert c.fingerprint(SOURCE) != creds(username="other").fingerprint(SOURCE)

    def test_password_does_not_leak_through_repr(self):
        c = creds(password="hunter2-should-not-appear")
        for rendered in (repr(c), str(c), f"{c}", repr(c.password)):
            assert "hunter2-should-not-appear" not in rendered


class TestEndpointPinning:
    """The bundled WSDL declares the SOURCE tenant's address.

    If a destination client inherited that address, writes would land in the
    source tenant — and this service has no delete operation. These tests are
    the regression guard for exactly that.
    """

    def test_client_uses_the_target_endpoint_not_the_wsdl_address(self, wsdl_path):
        conn = make_client(
            DEST, creds(), role=Role.DESTINATION, wsdl_source=wsdl_path
        )
        assert conn.endpoint == (
            "https://impl-services1.wd12.myworkday.com/ccx/service/"
            "some_other_tenant/Core_Implementation_Service/v46.0"
        )
        assert conn.service._binding_options["address"] == conn.endpoint

    def test_the_wsdl_embedded_tenant_does_not_leak_into_the_endpoint(self, wsdl_path):
        conn = make_client(
            DEST, creds(), role=Role.DESTINATION, wsdl_source=wsdl_path
        )
        # The bundled WSDL was captured from commitconsulting_dpt1.
        assert "commitconsulting_dpt1" not in conn.service._binding_options["address"]

    def test_version_is_in_the_url_path(self, wsdl_path):
        conn = make_client(SOURCE, creds(), wsdl_source=wsdl_path, version="v47.0")
        assert conn.endpoint.endswith("/Core_Implementation_Service/v47.0")

    def test_operations_survive_the_rebinding(self, wsdl_path):
        conn = make_client(SOURCE, creds(), wsdl_source=wsdl_path)
        for op in (
            "Get_Calculated_Fields",
            "Put_Calculated_Field",
            "Get_Tenanted_Report_Definitions",
            "Put_Tenanted_Report_Definition",
        ):
            assert hasattr(conn.service, op), f"{op} lost when rebinding the address"


class TestConnectionMetadata:
    def test_role_is_recorded(self, wsdl_path):
        src = make_client(SOURCE, creds(), role=Role.SOURCE, wsdl_source=wsdl_path)
        dst = make_client(DEST, creds(), role=Role.DESTINATION, wsdl_source=wsdl_path)
        assert not src.is_destination()
        assert dst.is_destination()

    def test_ws_security_username_is_tenant_qualified(self, wsdl_path):
        conn = make_client(SOURCE, creds(), wsdl_source=wsdl_path)
        assert conn.username == "lmcneil@commitconsulting_dpt1"

    def test_connection_redacts_its_own_credentials(self, wsdl_path):
        conn = make_client(
            SOURCE, creds(password="super-secret-value"), wsdl_source=wsdl_path
        )
        assert "super-secret-value" not in conn.redact(
            "fault text containing super-secret-value"
        )

    def test_connection_repr_does_not_leak_the_password(self, wsdl_path):
        conn = make_client(
            SOURCE, creds(password="super-secret-value"), wsdl_source=wsdl_path
        )
        assert "super-secret-value" not in repr(conn)


class TestWSDLResolution:
    def test_explicit_wsdl_path_env_override_wins(self, wsdl_path, monkeypatch):
        from wdmigrator.auth.client import resolve_wsdl_source

        monkeypatch.setenv("WD_WSDL_PATH", wsdl_path)
        assert resolve_wsdl_source(SOURCE, "Svc", "v47.0") == wsdl_path

    def test_defaults_to_the_live_wsdl_url_for_the_target(self, monkeypatch):
        from wdmigrator.auth.client import resolve_wsdl_source

        monkeypatch.delenv("WD_WSDL_PATH", raising=False)
        resolved = resolve_wsdl_source(DEST, "Core_Implementation_Service", "v47.0")
        assert resolved.startswith("https://impl-services1.wd12.myworkday.com/")
        assert resolved.endswith("?wsdl")
        # A destination client must not default to the source's WSDL.
        assert "some_other_tenant" in resolved


class TestServiceSwitching:
    """`Connection.for_service()` opens a sibling client on another SOAP service.

    Time Calculations, Groups, and Tags live on
    Time_Tracking_Implementation_Service — not on Core — so the engine has to
    hold two connections against the same tenant. These tests cover the wiring
    without needing the TT WSDL bundled.
    """

    def test_time_tracking_constant_is_exported_from_auth(self):
        assert TIME_TRACKING_SERVICE_NAME == "Time_Tracking_Implementation_Service"

    def test_time_tracking_constant_is_reexported_from_api(self):
        from wdmigrator import api

        assert api.TIME_TRACKING_SERVICE_NAME == TIME_TRACKING_SERVICE_NAME

    def test_for_service_without_retained_credentials_raises(self):
        # A bare Connection (e.g. one hand-built by a test) has no credentials
        # and cannot open a sibling — the error must say why.
        conn = Connection(
            target=SOURCE,
            role=Role.SOURCE,
            service=object(),
            client=object(),
            username="whatever@commitconsulting_dpt1",
            endpoint="https://example/",
            service_name="Core_Implementation_Service",
            version="v46.0",
        )
        with pytest.raises(AuthError, match="retained credentials"):
            conn.for_service("Time_Tracking_Implementation_Service")

    def test_for_service_reuses_credentials_and_shares_the_limiter(self, wsdl_path):
        # Offline coverage: use the bundled Core WSDL for both the parent and
        # the "sibling" build. The point of this test is the wiring — same
        # credentials → same ws-username, same limiter object — not that a
        # different WSDL loaded.
        conn = make_client(SOURCE, creds(), wsdl_source=wsdl_path)
        # for_service resolves the WSDL from the tenant unless WD_WSDL_PATH is
        # set. Point it at the bundled one so this stays offline.
        import os as _os
        _os.environ["WD_WSDL_PATH"] = wsdl_path
        try:
            sibling = conn.for_service("Core_Implementation_Service")
        finally:
            del _os.environ["WD_WSDL_PATH"]
        assert sibling is not conn
        assert sibling.target is conn.target
        assert sibling.role is conn.role
        assert sibling.username == conn.username
        assert sibling.limiter is conn.limiter, "TT and Core share the tenant's rate budget"


@pytest.mark.live
class TestLiveSourceConnection:
    """Read-only checks against the real source tenant. Never writes.

    Deliberately contains no wrong-password test. Workday ISUs can be locked
    out after repeated failed authentications, and a test suite that runs on
    every change is exactly the thing that would trip that threshold — locking
    the account this whole project depends on. The failure path is covered
    offline instead (redaction in `TestConnectionMetadata`, fault explanation
    in `TestFailureExplanation`).
    """

    def test_verify_connection_succeeds(self, live_source_connection):
        status = verify_connection(live_source_connection)
        assert status.ok, status.detail

    def test_status_reports_the_pinned_endpoint(self, live_source_connection):
        status = verify_connection(live_source_connection)
        assert status.endpoint == live_source_connection.endpoint
        assert "Core_Implementation_Service" in status.endpoint

    def test_for_service_opens_time_tracking_implementation_service(
        self, live_source_connection
    ):
        """The plumbing that Time Calculation migration relies on."""
        tt = live_source_connection.for_service(TIME_TRACKING_SERVICE_NAME)
        assert tt.service_name == TIME_TRACKING_SERVICE_NAME
        assert TIME_TRACKING_SERVICE_NAME in tt.endpoint
        # Ops we need for Time Calculation migration must be exposed.
        for op in (
            "Get_Time_Calculations",
            "Put_Time_Calculation",
            "Get_Time_Calculation_Groups",
            "Put_Time_Calculation_Group",
            "Get_Time_Calculation_Tags",
            "Put_Time_Calculation_Tag",
        ):
            assert hasattr(tt.service, op), f"{op} missing from TT service"


class TestFailureExplanation:
    """The service's faults are unhelpful; these check we add the missing context."""

    def _status_for(self, wsdl_path, exc):
        from wdmigrator.auth.client import _explain_failure

        conn = make_client(SOURCE, creds(password="pw-value"), wsdl_source=wsdl_path)
        return _explain_failure(conn, exc)

    def test_wrong_service_fault_points_at_entitlement_not_version(self, wsdl_path):
        """This exact fault cost real debugging time — it reads like a version bug."""
        detail = self._status_for(
            wsdl_path,
            Exception("The web service or version is invalid for the requested operation"),
        )
        assert "Integration System User" in detail
        assert "Activate Pending Security Policy Changes" in detail

    def test_404_names_the_endpoint_actually_used(self, wsdl_path):
        detail = self._status_for(wsdl_path, Exception("404 Client Error"))
        assert "commitconsulting_dpt1/Core_Implementation_Service/v46.0" in detail

    def test_explanation_redacts_the_password(self, wsdl_path):
        detail = self._status_for(wsdl_path, Exception("fault mentioning pw-value"))
        assert "pw-value" not in detail
