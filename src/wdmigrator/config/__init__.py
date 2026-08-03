"""Tenant targeting and configuration.

Holds the parsing/classification of *which tenant we are pointed at* — kept
separate from credentials, which never live in a TenantTarget.
"""

from wdmigrator.config.targets import (
    Environment,
    TenantTarget,
    TenantURLError,
    classify_environment,
    derive_services_host,
    parse_tenant_url,
    target_from_parts,
)

__all__ = [
    "Environment",
    "TenantTarget",
    "TenantURLError",
    "classify_environment",
    "derive_services_host",
    "parse_tenant_url",
    "target_from_parts",
]
