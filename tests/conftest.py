"""Shared pytest fixtures.

Design goal: `pytest` with no arguments must pass with NO .env and NO network
access. Anything that needs a real tenant is marked `live` and is deselected by
default (see addopts in pyproject.toml).
"""

import os

import pytest
from dotenv import load_dotenv

from wdmigrator import DEFAULT_WSDL_PATH

# Load .env if present. Absent .env is fine — only `live` tests need it.
load_dotenv()


# Env vars a live SOURCE-tenant test cannot run without.
_LIVE_SOURCE_VARS = (
    "WD_SOURCE_SERVICES_HOST",
    "WD_SOURCE_TENANT",
    "WD_SOURCE_ISU_USERNAME",
    "WD_SOURCE_ISU_PASSWORD",
)


def pytest_collection_modifyitems(config, items):
    """Skip `live` tests when the source credentials aren't configured.

    Without this, running `pytest -m live` on a fresh clone fails with a
    confusing auth error instead of a clear "not configured" skip.
    """
    missing = [v for v in _LIVE_SOURCE_VARS if not os.environ.get(v)]
    if not missing:
        return
    skip = pytest.mark.skip(
        reason=f"live test needs .env vars: {', '.join(missing)}"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def wsdl_path() -> str:
    """Filesystem path to the bundled Core_Implementation_Service WSDL (v47.0)."""
    assert DEFAULT_WSDL_PATH.is_file(), f"WSDL missing at {DEFAULT_WSDL_PATH}"
    return str(DEFAULT_WSDL_PATH)


@pytest.fixture(scope="session")
def offline_client(wsdl_path):
    """A zeep client built from the local WSDL, with NO credentials.

    Constructing a client from the local WSDL needs no tenant round-trip, so
    this is safe and fast. Use it to assert on schema shape, operation names,
    and request serialization. Calling an operation on it WOULD hit the tenant
    (the WSDL embeds the service address) and would fail unauthenticated —
    don't call operations here; that's what `live` tests are for.
    """
    from zeep import Client, Settings

    return Client(wsdl=wsdl_path, settings=Settings(strict=False, xml_huge_tree=True))
