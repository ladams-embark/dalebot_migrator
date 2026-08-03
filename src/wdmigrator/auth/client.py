"""Building authenticated SOAP clients, pinned to the tenant you asked for.

Two things here matter more than the rest.

**The endpoint is always pinned explicitly.** A WSDL embeds the service address
of whichever tenant it was fetched from, and zeep will happily send to that
address no matter which tenant you *think* you configured. The bundled WSDL was
captured from the source tenant, so a destination client built from it would
send writes to the source. Since this service has no delete operation, that
class of bug is unrecoverable. Every client built here is rebound to
``target.endpoint(...)`` via ``create_service``, so the WSDL is used only for
its schema and the address always comes from the :class:`TenantTarget`.

**The WS-Security username is ``{isu_username}@{tenant}``.** The old prototype
appended the tenant unconditionally while its docstring said the env var
"usually" already contained it — which would silently produce
``user@tenant@tenant``. Here an already-qualified username is accepted, but a
mismatched one is rejected rather than quietly overridden.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from enum import Enum

from requests import Session
from zeep import Client, Settings
from zeep.transports import Transport
from zeep.wsse import UsernameToken

from wdmigrator import DEFAULT_WSDL_PATH
from wdmigrator.config.targets import TenantTarget
from wdmigrator.ratelimit import RateLimiter
from wdmigrator.secrets import Secret, redact

#: Confirmed working service. `Report_Metadata` defines the same operations but
#: is rejected live on this tenant — see docs/WSDL_NOTES.md.
DEFAULT_SERVICE_NAME = "Core_Implementation_Service"

#: Hardcoded rather than read from WD_WWS_VERSION — a version has to work
#: across every tenant this tool talks to in one run (source and destination
#: are almost always different tenants), and the max supported version isn't
#: the same everywhere: v47.0 on commitconsulting_dpt1/dpt5, but only v46.0 on
#: the "web" tenant (confirmed live 2026-08-03 — v47.x and v48.0 both reject
#: with "Invalid request service version" there, v46.0 down to v42.0 all
#: return a real WSDL). v46.0 is the highest version confirmed to work on
#: every tenant seen so far.
DEFAULT_VERSION = "v46.0"

DEFAULT_TIMEOUT = 60


class AuthError(RuntimeError):
    """Could not build or verify an authenticated client."""


class Role(str, Enum):
    """Which side of the migration a connection is for.

    Carried on the connection so write paths can assert they were handed a
    destination, and read paths can refuse to be pointed at one by accident.
    """

    SOURCE = "source"
    DESTINATION = "destination"


@dataclass(frozen=True)
class Credentials:
    """ISU credentials. The password is a :class:`Secret`, never a bare str."""

    username: str
    password: Secret

    def __post_init__(self) -> None:
        if not self.username or not self.username.strip():
            raise AuthError("ISU username is required.")
        if not self.password:
            raise AuthError("ISU password is required.")

    def ws_username(self, tenant: str) -> str:
        """The WS-Security username: ``{isu_username}@{tenant}``.

        Accepts an already-qualified username so a user who pastes
        ``lmcneil@acme_impl`` is not double-suffixed. A username qualified with
        a *different* tenant is an error, not something to silently correct —
        it usually means credentials were copy-pasted from another tenant, and
        quietly rewriting it would authenticate somewhere unintended.
        """
        username = self.username.strip()
        if "@" not in username:
            return f"{username}@{tenant}"

        local, _, qualifier = username.rpartition("@")
        if qualifier.lower() != tenant.lower():
            raise AuthError(
                f"Username {username!r} is qualified with tenant {qualifier!r} "
                f"but this connection targets {tenant!r}. Use the bare ISU "
                "username, or correct the tenant."
            )
        return f"{local}@{tenant}"

    def fingerprint(self, target: TenantTarget) -> str:
        """Stable ID for (host, tenant, username) — **never** the password.

        The UI uses this to notice that credentials changed after a successful
        connection test, so downstream state can be invalidated rather than
        riding a green check that no longer applies.
        """
        host, tenant = target.identity()
        raw = f"{host}|{tenant}|{self.username.strip().lower()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class Connection:
    """An authenticated, address-pinned client for one tenant."""

    target: TenantTarget
    role: Role
    service: object  # zeep ServiceProxy, rebound to `endpoint`
    client: Client
    username: str
    endpoint: str
    service_name: str
    version: str
    limiter: RateLimiter = field(default_factory=RateLimiter)
    #: Kept only so error text and envelopes can be redacted before display.
    _secrets: tuple[Secret, ...] = field(default=(), repr=False)

    @property
    def label(self) -> str:
        return f"{self.role.value}: {self.target.label}"

    def is_destination(self) -> bool:
        return self.role is Role.DESTINATION

    def redact(self, text: str) -> str:
        """Strip this connection's credentials out of arbitrary text."""
        return redact(text, self._secrets)


@dataclass(frozen=True)
class ConnectionStatus:
    ok: bool
    detail: str
    fingerprint: str
    checked_at: float
    endpoint: str = ""


def resolve_wsdl_source(target: TenantTarget, service_name: str, version: str) -> str:
    """Pick the WSDL to build the schema from.

    Precedence: explicit ``WD_WSDL_PATH`` override, then the target's live
    ``?wsdl``, then the bundled copy. The live WSDL is preferred over the
    bundled one because the bundled copy can drift from the tenant; the address
    it declares does not matter either way, since the endpoint is pinned
    separately.
    """
    override = os.environ.get("WD_WSDL_PATH")
    if override:
        return override
    if target.services_host and target.tenant:
        return target.wsdl_url(service_name, version)
    return str(DEFAULT_WSDL_PATH)


def make_client(
    target: TenantTarget,
    credentials: Credentials,
    *,
    role: Role = Role.SOURCE,
    service_name: str | None = None,
    version: str | None = None,
    wsdl_source: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    calls_per_second: float | None = None,
) -> Connection:
    """Build an authenticated client bound to ``target``'s endpoint.

    The returned :class:`Connection` exposes ``.service``, which is rebound to
    the target's endpoint rather than whatever address the WSDL declared.
    """
    service_name = service_name or os.environ.get(
        "WD_OX_SERVICE_NAME", DEFAULT_SERVICE_NAME
    )
    version = version or DEFAULT_VERSION
    endpoint = target.endpoint(service_name, version)
    wsdl = wsdl_source or resolve_wsdl_source(target, service_name, version)

    ws_username = credentials.ws_username(target.tenant)

    session = Session()
    transport = Transport(session=session, timeout=timeout, operation_timeout=timeout)
    settings = Settings(strict=False, xml_huge_tree=True)
    wsse = UsernameToken(ws_username, credentials.password.reveal())

    try:
        client = Client(wsdl=wsdl, settings=settings, transport=transport, wsse=wsse)
    except Exception as exc:  # noqa: BLE001 - re-raised with context, redacted
        raise AuthError(
            f"Could not load the WSDL for {target.label} from {wsdl}: "
            f"{redact(str(exc), [credentials.password])}"
        ) from exc

    service = _bind_to_endpoint(client, endpoint, target)

    limiter = (
        RateLimiter(calls_per_second=calls_per_second)
        if calls_per_second is not None
        else RateLimiter()
    )

    return Connection(
        target=target,
        role=role,
        service=service,
        client=client,
        username=ws_username,
        endpoint=endpoint,
        service_name=service_name,
        version=version,
        limiter=limiter,
        _secrets=(credentials.password,),
    )


def _bind_to_endpoint(client: Client, endpoint: str, target: TenantTarget):
    """Rebind the service proxy to ``endpoint``, ignoring the WSDL's address.

    This is the guard against writing to the wrong tenant. See the module
    docstring — it is the reason this function exists rather than just
    returning ``client.service``.
    """
    try:
        wsdl_service = next(iter(client.wsdl.services.values()))
        port = next(iter(wsdl_service.ports.values()))
        binding_name = port.binding.name
    except StopIteration as exc:
        raise AuthError(
            f"WSDL for {target.label} declares no service/port to bind to."
        ) from exc

    service = client.create_service(binding_name, endpoint)

    bound = service._binding_options.get("address")
    if bound != endpoint:
        # Never proceed on an unpinned client: the fallback would be the
        # WSDL's own address, i.e. potentially another tenant.
        raise AuthError(
            f"Refusing to use a client whose endpoint could not be pinned. "
            f"Wanted {endpoint}, got {bound}."
        )
    return service


def verify_connection(connection: Connection) -> ConnectionStatus:
    """Cheapest possible authenticated round-trip. Read-only, never writes.

    Uses ``Get_Calculated_Fields`` with ``Count=1`` and no field data — one
    row, references only.
    """
    checked_at = time.time()
    fingerprint = hashlib.sha256(
        f"{connection.endpoint}|{connection.username}".encode()
    ).hexdigest()[:16]

    try:
        connection.limiter.wait()
        connection.service.Get_Calculated_Fields(
            Response_Filter={"Page": 1, "Count": 1},
            Response_Group={
                "Include_Reference": True,
                "Include_Calculated_Field_Data": False,
            },
        )
    except Exception as exc:  # noqa: BLE001 - converted to a status, not raised
        return ConnectionStatus(
            ok=False,
            detail=_explain_failure(connection, exc),
            fingerprint=fingerprint,
            checked_at=checked_at,
            endpoint=connection.endpoint,
        )

    return ConnectionStatus(
        ok=True,
        detail=f"Authenticated to {connection.target.label} as {connection.username}.",
        fingerprint=fingerprint,
        checked_at=checked_at,
        endpoint=connection.endpoint,
    )


def _explain_failure(connection: Connection, exc: Exception) -> str:
    """Turn a SOAP/transport failure into something actionable.

    The faults this service returns are famously unhelpful — the wrong service
    name yields a generic "web service or version is invalid" that reads like a
    version problem. These hints come from failures actually hit against a live
    tenant.
    """
    message = connection.redact(str(exc))
    lowered = message.lower()

    if "web service or version is invalid" in lowered:
        return (
            f"{message}\n\nThis usually means the ISU is not entitled to "
            f"{connection.service_name} on this tenant, rather than a version "
            "problem. Confirm the ISU is an Integration System User with the "
            "required domain access, and that 'Activate Pending Security "
            "Policy Changes' has been run."
        )
    if "invalid username or password" in lowered or "failed to authenticate" in lowered:
        return (
            f"{message}\n\nCheck the ISU username and password. The username is "
            f"sent as {connection.username!r}."
        )
    if "404" in message:
        return (
            f"{message}\n\nEndpoint not found: {connection.endpoint}. Check the "
            "services host and that the version is in the URL path."
        )
    return message
