"""Which tenant are we pointed at, and how dangerous is writing to it.

A ``TenantTarget`` is pure addressing — services host, tenant name, and an
environment classification. Credentials deliberately do NOT live here: targets
get logged, hashed, and rendered in the UI, and a credential on this object
would leak through all three.

The two hosts are easy to mix up and the mistake is silent:

- **UI host** (browser only): ``impl.wd12.myworkday.com``
- **Services host** (all SOAP/REST): ``impl-services1.wd12.myworkday.com``

Users paste the UI URL because that is what their browser shows, so
``parse_tenant_url`` accepts it and derives the services host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

#: Path segments that are Workday routing, never a tenant name.
_RESERVED_SEGMENTS = frozenset(
    {"wday", "ccx", "service", "authgwy", "d", "login", "home"}
)

#: A services host has a "-servicesN" suffix on its first label
#: (``impl-services1.wd12...``, ``wd5-services1.myworkday.com``).
_SERVICES_LABEL = re.compile(r"^(?P<base>.+?)-services\d+$")
#: Production REST API Endpoint hosts use a *bare* ``servicesN`` first label
#: (``services1.wd108.myworkday.com`` for aesseal). That is already the SOAP
#: host — rewriting it as ``services1-services1...`` is how Connect failed
#: when that URL was pasted.
_BARE_SERVICES_LABEL = re.compile(r"^services\d+$")


class TenantURLError(ValueError):
    """The supplied tenant URL could not be parsed into host + tenant."""


class Environment(str, Enum):
    """How dangerous is writing to this tenant.

    ``UNKNOWN`` is not a neutral value — :mod:`wdmigrator.safety` treats it
    exactly like ``PRODUCTION``. Classification must fail toward blocking, so
    anything we cannot positively identify as an implementation or sandbox
    tenant is refused by default.
    """

    IMPLEMENTATION = "implementation"
    SANDBOX = "sandbox"
    PRODUCTION = "production"
    UNKNOWN = "unknown"

    @property
    def is_safe_write_target(self) -> bool:
        """True only for environments we allow live writes to without an override."""
        return self in (Environment.IMPLEMENTATION, Environment.SANDBOX)


@dataclass(frozen=True)
class TenantTarget:
    """A tenant we can address. Contains no credentials — see module docstring."""

    tenant: str
    services_host: str
    ui_host: str
    environment: Environment
    #: True when we inferred ``services_host`` rather than being given it.
    #: Only the ``impl.wdNN`` mapping is verified against a live tenant, so the
    #: UI must render a derived host as editable and make the user confirm it
    #: by running a connection test.
    services_host_derived: bool
    raw_input: str = ""

    def identity(self) -> tuple[str, str]:
        """Case-normalised (services_host, tenant), for equality comparisons.

        Two targets with the same identity address the same tenant, which is
        the check that stops a migration writing into its own source.
        """
        return (self.services_host.lower(), self.tenant.lower())

    def endpoint(self, service: str, version: str) -> str:
        """SOAP endpoint. The version belongs in the path, not just the envelope."""
        return f"https://{self.services_host}/ccx/service/{self.tenant}/{service}/{version}"

    def wsdl_url(self, service: str, version: str) -> str:
        return f"{self.endpoint(service, version)}?wsdl"

    @property
    def label(self) -> str:
        """Short human-readable form for logs and UI chrome."""
        return f"{self.tenant} @ {self.services_host}"


def derive_services_host(host: str) -> tuple[str, bool]:
    """Map a host to its services host.

    Returns ``(services_host, was_derived)``. ``was_derived`` is False when the
    input was already a services host, meaning the caller can trust it.

    Only ``impl.wdNN.myworkday.com -> impl-services1.wdNN.myworkday.com`` is
    verified against a live tenant. Other pods (notably production, which uses
    a different shape entirely) are guesses, so anything derived here is
    flagged for user confirmation rather than used silently.
    """
    host = host.strip().lower()
    if not host:
        raise TenantURLError("No host to derive a services host from.")

    labels = host.split(".")
    if _SERVICES_LABEL.match(labels[0]) or _BARE_SERVICES_LABEL.match(labels[0]):
        return host, False

    labels[0] = f"{labels[0]}-services1"
    return ".".join(labels), True


def _ui_host_for(services_host: str) -> str:
    """Best-effort inverse of :func:`derive_services_host`, for display only."""
    labels = services_host.split(".")
    match = _SERVICES_LABEL.match(labels[0])
    if match:
        labels[0] = match.group("base")
        return ".".join(labels)
    if _BARE_SERVICES_LABEL.match(labels[0]) and len(labels) > 1:
        return ".".join(labels[1:])
    return ".".join(labels)


def classify_environment(host: str, tenant: str) -> Environment:
    """Classify a tenant's environment, erring toward the unsafe answer.

    Deliberately host-driven. A tenant *name* containing "sandbox" is not
    enough on its own: a production host with a tenant called ``acme_sandbox``
    must not be classified SANDBOX and become writable. The tenant name is only
    consulted once the host already looks non-production.
    """
    host = (host or "").strip().lower()
    tenant = (tenant or "").strip().lower()
    if not host:
        return Environment.UNKNOWN

    first = host.split(".")[0]
    base = _SERVICES_LABEL.match(first)
    if base:
        first = base.group("base")

    # "impl" as a leading prefix (impl.wd12.myworkday.com, verified live) or
    # as its own hyphenated token (wd2-impl-services1.workday.com, also
    # verified live — see auth/endpoint_discovery.py) both mean
    # Implementation. A plain substring check would be too loose (would also
    # match e.g. "implement"); checking hyphen-split tokens avoids that while
    # still covering both confirmed real shapes.
    if first.startswith("impl") or "impl" in first.split("-"):
        return Environment.IMPLEMENTATION
    if first.startswith("sbx") or "sandbox" in first:
        return Environment.SANDBOX
    if first in ("www", "wd") or re.fullmatch(r"wd\d+", first):
        return Environment.PRODUCTION
    # ``services1.wd108.myworkday.com`` — production REST API Endpoint shape
    # (aesseal). The SOAP host's first label is the services token, the pod
    # number is the next label.
    if _BARE_SERVICES_LABEL.match(first):
        return Environment.PRODUCTION

    # Unrecognised host shape. Treated as PRODUCTION by the safety layer.
    return Environment.UNKNOWN


def _extract_tenant(path: str) -> str:
    """Pull the tenant name out of a Workday URL path.

    Handles the three shapes users actually paste::

        /wday/authgwy/TENANT/login.htmld     (the login page)
        /TENANT/d/home.htmld                 (a logged-in UI page)
        /ccx/service/TENANT/Service/v47.0    (an API endpoint)
    """
    segments = [s for s in path.split("/") if s]
    if not segments:
        raise TenantURLError(
            "URL has no path, so it contains no tenant name. Expected something "
            "like https://impl.wd12.myworkday.com/wday/authgwy/YOUR_TENANT/login.htmld"
        )

    # Anchored forms first — these are unambiguous.
    for anchor in ("authgwy", "service"):
        if anchor in segments:
            index = segments.index(anchor) + 1
            if index < len(segments):
                return segments[index]

    for segment in segments:
        stem = segment.split(".")[0].lower()
        if stem not in _RESERVED_SEGMENTS:
            return segment

    raise TenantURLError(
        f"Could not find a tenant name in path {path!r} — every segment looked "
        "like Workday routing rather than a tenant."
    )


def parse_tenant_url(raw: str) -> TenantTarget:
    """Parse a pasted Workday URL into an addressable :class:`TenantTarget`.

    Accepts UI URLs, API endpoints, and bare hostname+path, with or without a
    scheme. A bare tenant name with no host is rejected — we cannot invent a
    host, and guessing one would be a silent misdirection.
    """
    if raw is None or not raw.strip():
        raise TenantURLError("Tenant URL is empty.")

    candidate = raw.strip()
    if "://" not in candidate:
        # urlparse treats a scheme-less string as a path, losing the host.
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()

    if not host:
        raise TenantURLError(f"No hostname found in {raw!r}.")
    if "." not in host:
        raise TenantURLError(
            f"{raw!r} does not look like a Workday URL. Paste the full tenant "
            "URL from your browser, e.g. "
            "https://impl.wd12.myworkday.com/wday/authgwy/YOUR_TENANT/login.htmld"
        )

    tenant = _extract_tenant(parsed.path)
    services_host, derived = derive_services_host(host)

    return TenantTarget(
        tenant=tenant,
        services_host=services_host,
        ui_host=_ui_host_for(services_host),
        environment=classify_environment(services_host, tenant),
        services_host_derived=derived,
        raw_input=raw.strip(),
    )


def target_from_parts(services_host: str, tenant: str) -> TenantTarget:
    """Build a target from an explicit services host + tenant.

    The path for ``.env`` values and for the UI's editable host field, where
    the user has already told us the services host directly.
    """
    services_host = (services_host or "").strip().lower()
    tenant = (tenant or "").strip()
    if not services_host:
        raise TenantURLError("services_host is required.")
    if not tenant:
        raise TenantURLError("tenant is required.")

    resolved, derived = derive_services_host(services_host)
    return TenantTarget(
        tenant=tenant,
        services_host=resolved,
        ui_host=_ui_host_for(resolved),
        environment=classify_environment(resolved, tenant),
        services_host_derived=derived,
        raw_input=f"{services_host}/{tenant}",
    )
