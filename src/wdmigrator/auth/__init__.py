"""Tenant authentication and zeep client construction.

Clients are always rebound to the target tenant's endpoint rather than trusting
the address embedded in the WSDL — see `client.py` for why that matters.
"""

from wdmigrator.auth.client import (
    DEFAULT_SERVICE_NAME,
    DEFAULT_VERSION,
    AuthError,
    Connection,
    ConnectionStatus,
    Credentials,
    Role,
    make_client,
    verify_connection,
)
from wdmigrator.auth.endpoint_discovery import (
    DataCenter,
    DiscoveryAttempt,
    DiscoveryResult,
    EndpointNotFoundError,
    KNOWN_IMPL_DATA_CENTERS,
    discover_services_host,
    iter_discover_services_host,
)

__all__ = [
    "DEFAULT_SERVICE_NAME",
    "DEFAULT_VERSION",
    "AuthError",
    "Connection",
    "ConnectionStatus",
    "Credentials",
    "Role",
    "make_client",
    "verify_connection",
    "DataCenter",
    "DiscoveryAttempt",
    "DiscoveryResult",
    "EndpointNotFoundError",
    "KNOWN_IMPL_DATA_CENTERS",
    "discover_services_host",
    "iter_discover_services_host",
]
