"""Tenant authentication and zeep client construction.

Clients are always rebound to the target tenant's endpoint rather than trusting
the address embedded in the WSDL — see `client.py` for why that matters.
"""

from wdmigrator.auth.client import (
    AuthError,
    Connection,
    ConnectionStatus,
    Credentials,
    Role,
    make_client,
    verify_connection,
)

__all__ = [
    "AuthError",
    "Connection",
    "ConnectionStatus",
    "Credentials",
    "Role",
    "make_client",
    "verify_connection",
]
