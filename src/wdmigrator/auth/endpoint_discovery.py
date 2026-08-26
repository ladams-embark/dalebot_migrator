"""Finding a tenant's services host when you only know its tenant ID.

There is no Workday directory API that maps a tenant ID to its services
host. The only way to find it is to actually try — this module does the
same thing you'd do by hand: attempt an unauthenticated WSDL fetch against
every known data center's services host, with the tenant ID in the path,
and see which one answers with a real WSDL.

**This works because the services host is a property of the data center
(pod), not the tenant.** Every tenant on a given pod shares the same
services host — only the URL *path* differs (`/ccx/service/{tenant}/...`).
Once a data center's host is confirmed once, live, it's confirmed for every
tenant on that pod. `KNOWN_IMPL_DATA_CENTERS` grows as new ones get
confirmed this way; entries not yet confirmed are best-effort guesses by
analogy with the two patterns actually seen so far (`impl-services1.wdNN.
myworkday.com` for wd12, `wdN-impl-services1.workday.com` for dc1 — the two
don't reduce to one formula, so there is no way to derive an unconfirmed
data center's host from its number alone).

**Implementation/Sandbox data centers only, deliberately.** Matches this
tool's own safety model (`safety.py` never treats Production as a safe
write target by default) — there's no reason to make discovering a
Production endpoint this easy.

No authentication happens here — this is an unauthenticated GET, same as
opening the WSDL URL in a browser. Nothing here writes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import requests

from wdmigrator.auth.client import DEFAULT_SERVICE_NAME, DEFAULT_VERSION

DEFAULT_TIMEOUT = 8.0  # seconds per attempt — several data centers get tried per lookup


@dataclass(frozen=True)
class DataCenter:
    label: str
    services_host: str
    #: True once a live WSDL fetch has actually succeeded against this host
    #: through this tool. False entries are unverified guesses by analogy —
    #: see the module docstring.
    verified: bool


#: Implementation/Sandbox data centers this tool knows how to try. Add a new
#: entry (verified=True) once discovery succeeds against a data center not
#: already listed here, so future lookups don't need to re-discover it.
#:
#: Two naming families confirmed live so far, and they do NOT reduce to one
#: formula — a data center's real host has to be confirmed independently,
#: not derived:
#:   - "impl-services1.wdNN.myworkday.com" (wd12, wd501, and wd504 — 3/3 of
#:     the data centers with an explicit "wdNN" in their production URL.
#:     wd12/wd501 confirmed 2026-08-03 via commitconsulting's REST API
#:     Endpoint page; wd504 confirmed 2026-08-26 by probing the "walmart"
#:     tenant.)
#:   - "wdN-impl-services1.workday.com" (dc1 only — the one data center with
#:     NO "wdNN" in its production URL at all, e.g. plain workday.com)
#: Since the wdNN-numbered data centers are now 3/3 on the myworkday.com
#: pattern, the still-unverified wd3/5/10/102/103/105 entries below try that
#: pattern first, then fall back to the workday.com-style guess.
KNOWN_IMPL_DATA_CENTERS: tuple[DataCenter, ...] = (
    DataCenter("dc1", "wd2-impl-services1.workday.com", verified=True),
    DataCenter("wd12", "impl-services1.wd12.myworkday.com", verified=True),
    DataCenter("wd501", "impl-services1.wd501.myworkday.com", verified=True),
    DataCenter("wd3 (myworkday guess)", "impl-services1.wd3.myworkday.com", verified=False),
    DataCenter("wd3 (workday guess)", "wd3-impl-services1.workday.com", verified=False),
    DataCenter("wd5 (myworkday guess)", "impl-services1.wd5.myworkday.com", verified=False),
    DataCenter("wd5 (workday guess)", "wd5-impl-services1.workday.com", verified=False),
    DataCenter("wd10 (myworkday guess)", "impl-services1.wd10.myworkday.com", verified=False),
    DataCenter("wd10 (workday guess)", "wd10-impl-services1.workday.com", verified=False),
    DataCenter("wd102 (myworkday guess)", "impl-services1.wd102.myworkday.com", verified=False),
    DataCenter("wd102 (workday guess)", "wd102-impl-services1.workday.com", verified=False),
    DataCenter("wd103 (myworkday guess)", "impl-services1.wd103.myworkday.com", verified=False),
    DataCenter("wd103 (workday guess)", "wd103-impl-services1.workday.com", verified=False),
    DataCenter("wd105 (myworkday guess)", "impl-services1.wd105.myworkday.com", verified=False),
    DataCenter("wd105 (workday guess)", "wd105-impl-services1.workday.com", verified=False),
    DataCenter("wd504", "impl-services1.wd504.myworkday.com", verified=True),
)


@dataclass(frozen=True)
class DiscoveryAttempt:
    data_center: str
    services_host: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DiscoveryResult:
    services_host: str
    data_center: str
    version: str


class EndpointNotFoundError(RuntimeError):
    """No known data center answered for this tenant."""


def _wsdl_reachable(
    services_host: str,
    tenant: str,
    service_name: str,
    version: str,
    *,
    timeout: float,
) -> tuple[bool, str]:
    url = f"https://{services_host}/ccx/service/{tenant}/{service_name}/{version}?wsdl"
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"{type(exc).__name__}: {exc}"

    body_start = response.text[:200].lstrip()
    looks_like_wsdl = body_start.startswith("<?xml") and "wsdl" in body_start.lower()
    if response.status_code == 200 and looks_like_wsdl:
        return True, "WSDL returned"
    return False, f"HTTP {response.status_code}: {body_start[:80]!r}"


def iter_discover_services_host(
    tenant: str,
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
    version: str = DEFAULT_VERSION,
    timeout: float = DEFAULT_TIMEOUT,
    data_centers: tuple[DataCenter, ...] = KNOWN_IMPL_DATA_CENTERS,
) -> Iterator[DiscoveryAttempt]:
    """Try every known Implementation/Sandbox data center for ``tenant``.

    Yields one :class:`DiscoveryAttempt` per data center tried, in order,
    stopping at the first success. A caller that wants the final answer
    should use :func:`discover_services_host` instead; this generator is for
    reporting progress ("trying wd12... trying wd5...") while it runs.
    """
    for dc in data_centers:
        ok, detail = _wsdl_reachable(
            dc.services_host, tenant, service_name, version, timeout=timeout
        )
        yield DiscoveryAttempt(
            data_center=dc.label, services_host=dc.services_host, ok=ok, detail=detail
        )
        if ok:
            return


def discover_services_host(
    tenant: str,
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
    version: str = DEFAULT_VERSION,
    timeout: float = DEFAULT_TIMEOUT,
    data_centers: tuple[DataCenter, ...] = KNOWN_IMPL_DATA_CENTERS,
) -> DiscoveryResult:
    """Drain :func:`iter_discover_services_host` and return the match.

    Raises :class:`EndpointNotFoundError` if no known data center answers —
    that means this tenant's data center isn't in ``KNOWN_IMPL_DATA_CENTERS``
    yet, not that the tenant doesn't exist. Find it manually (e.g. via
    Workday's API Client / REST endpoint admin page) and add a new
    ``DataCenter`` entry once confirmed.
    """
    for attempt in iter_discover_services_host(
        tenant,
        service_name=service_name,
        version=version,
        timeout=timeout,
        data_centers=data_centers,
    ):
        if attempt.ok:
            return DiscoveryResult(
                services_host=attempt.services_host,
                data_center=attempt.data_center,
                version=version,
            )
    raise EndpointNotFoundError(
        f"No known Implementation/Sandbox data center answered for tenant {tenant!r}. "
        "Its data center may not be in KNOWN_IMPL_DATA_CENTERS yet — find the services "
        "host manually (e.g. via Workday's API Client / REST endpoint admin page) and "
        "add it there."
    )
