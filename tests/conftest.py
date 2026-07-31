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


@pytest.fixture(autouse=True)
def _isolate_service_config(request, monkeypatch):
    """Stop a developer's .env from leaking into the offline suite.

    `load_dotenv()` above populates the process environment, so without this an
    offline test would silently exercise whatever service name and version
    happen to be in .env rather than the code's own defaults. That is how a
    stale `WD_OX_SERVICE_NAME=Report_Metadata` produced a green-looking test
    run pointed at a service that does not work on this tenant.

    `live` tests are exempt: reading the real configuration is the point.
    """
    if "live" in request.keywords:
        return
    for var in ("WD_OX_SERVICE_NAME", "WD_WWS_VERSION", "WD_WSDL_PATH"):
        monkeypatch.delenv(var, raising=False)


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


@pytest.fixture(scope="session")
def nested_fields_fixture() -> dict:
    """Calculated fields captured from a live tenant, with identifiers scrubbed.

    Real structure (all 34 polymorphic sub-type blocks, real nesting), fake
    names and IDs — so offline tests exercise the actual payload shape without
    committing tenant configuration.
    """
    import json
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "nested_calculated_fields.json"
    assert path.is_file(), f"missing fixture: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def live_source_target():
    """A TenantTarget for the real SOURCE tenant, built from .env.

    Only reachable in `live` tests — the skip hook above deselects those when
    the WD_SOURCE_* vars are absent.
    """
    from wdmigrator.config.targets import target_from_parts

    return target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )


@pytest.fixture(scope="session")
def live_source_credentials():
    from wdmigrator.auth import Credentials
    from wdmigrator.secrets import Secret

    return Credentials(
        username=os.environ["WD_SOURCE_ISU_USERNAME"],
        password=Secret(os.environ["WD_SOURCE_ISU_PASSWORD"]),
    )


@pytest.fixture(scope="session")
def live_source_connection(live_source_target, live_source_credentials):
    """An authenticated SOURCE connection. Read-only by convention.

    There is deliberately no destination-connection fixture. Nothing in the
    test suite should hold a writable client — see the `dest` marker note in
    pyproject.toml.
    """
    from wdmigrator.auth import Role, make_client

    return make_client(live_source_target, live_source_credentials, role=Role.SOURCE)
