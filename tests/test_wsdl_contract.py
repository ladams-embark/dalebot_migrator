"""Offline checks that the bundled WSDL still matches the facts the code relies on.

These run with no .env and no network. If one of these fails, an assumption
documented in docs/WSDL_NOTES.md has changed and the migration code built on it
needs review.
"""

import pytest

from wdmigrator.discovery.inventory import ALL_DASHBOARD_FLAVOURS

EXPECTED_OPERATIONS = (
    "Get_Calculated_Fields",
    "Put_Calculated_Field",
    "Get_Tenanted_Report_Definitions",
    "Put_Tenanted_Report_Definition",
)


def test_wsdl_asset_is_bundled(wsdl_path):
    assert wsdl_path.endswith("core_implementation_service_wsdl.xml")


def test_client_builds_offline(offline_client):
    """The whole offline workflow depends on this: no tenant needed to build."""
    assert offline_client is not None


@pytest.mark.parametrize("operation", EXPECTED_OPERATIONS)
def test_operation_exists(offline_client, operation):
    """Operation names are confirmed from the WSDL, never invented."""
    assert hasattr(offline_client.service, operation), (
        f"{operation} not found in WSDL — docs/WSDL_NOTES.md is out of date"
    )


@pytest.mark.parametrize(
    "operation",
    [spec[op] for spec in ALL_DASHBOARD_FLAVOURS for op in ("get", "put")],
)
def test_dashboard_operations_exist(offline_client, operation):
    """Custom and Workday-delivered Get/Put pairs, both flavours."""
    assert hasattr(offline_client.service, operation), (
        f"{operation} not found in WSDL — dashboard flavour table is out of date"
    )


def test_endpoint_uses_services_host_and_versioned_path(offline_client):
    """Guards two documented gotchas at once:

    - SOAP goes to the *services* host, not the UI host.
    - The API version must be in the URL path.
    """
    binding = next(iter(offline_client.wsdl.services.values()))
    address = next(iter(binding.ports.values())).binding_options["address"]

    assert "-services" in address, f"expected services host, got {address}"
    assert "/Core_Implementation_Service/v47.0" in address, (
        f"expected versioned path .../Core_Implementation_Service/v47.0, got {address}"
    )
