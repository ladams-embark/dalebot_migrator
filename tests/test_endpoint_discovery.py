"""Tests for services-host discovery. Offline — requests.get is faked, no
network calls, no real data centers contacted.
"""

import pytest

from wdmigrator.auth.endpoint_discovery import (
    DataCenter,
    DiscoveryAttempt,
    EndpointNotFoundError,
    discover_services_host,
    iter_discover_services_host,
)

WSDL_BODY = "<?xml version='1.0'?><wsdl:definitions>...</wsdl:definitions>"

DATA_CENTERS = (
    DataCenter("dc_a", "host-a.example.com", verified=True),
    DataCenter("dc_b", "host-b.example.com", verified=False),
    DataCenter("dc_c", "host-c.example.com", verified=False),
)


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def fake_requests_get(answers):
    """answers: {host: FakeResponse | Exception}"""

    def _get(url, timeout):
        for host, answer in answers.items():
            if host in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected URL in test: {url}")

    return _get


class TestIterDiscoverServicesHost:
    def test_stops_at_the_first_data_center_that_returns_a_real_wsdl(self, monkeypatch):
        import wdmigrator.auth.endpoint_discovery as mod

        monkeypatch.setattr(
            mod.requests,
            "get",
            fake_requests_get(
                {
                    "host-a.example.com": FakeResponse(404, "not found"),
                    "host-b.example.com": FakeResponse(200, WSDL_BODY),
                    "host-c.example.com": FakeResponse(200, WSDL_BODY),
                }
            ),
        )
        attempts = list(
            iter_discover_services_host("acme", data_centers=DATA_CENTERS)
        )
        assert [a.data_center for a in attempts] == ["dc_a", "dc_b"]
        assert attempts[0].ok is False
        assert attempts[1].ok is True
        assert attempts[1].services_host == "host-b.example.com"

    def test_a_non_wsdl_200_is_not_treated_as_success(self, monkeypatch):
        """A data center can return HTTP 200 with an HTML error page —
        that's not a WSDL and must not be mistaken for one."""
        import wdmigrator.auth.endpoint_discovery as mod

        monkeypatch.setattr(
            mod.requests,
            "get",
            fake_requests_get(
                {
                    "host-a.example.com": FakeResponse(200, "<html>not a wsdl</html>"),
                    "host-b.example.com": FakeResponse(200, WSDL_BODY),
                    "host-c.example.com": FakeResponse(200, WSDL_BODY),
                }
            ),
        )
        attempts = list(
            iter_discover_services_host("acme", data_centers=DATA_CENTERS)
        )
        assert attempts[0].ok is False
        assert attempts[1].ok is True

    def test_connection_errors_are_treated_as_a_miss_not_raised(self, monkeypatch):
        import wdmigrator.auth.endpoint_discovery as mod
        import requests as requests_module

        monkeypatch.setattr(
            mod.requests,
            "get",
            fake_requests_get(
                {
                    "host-a.example.com": requests_module.ConnectionError("DNS failed"),
                    "host-b.example.com": FakeResponse(200, WSDL_BODY),
                    "host-c.example.com": FakeResponse(200, WSDL_BODY),
                }
            ),
        )
        attempts = list(
            iter_discover_services_host("acme", data_centers=DATA_CENTERS)
        )
        assert attempts[0].ok is False
        assert "DNS failed" in attempts[0].detail
        assert attempts[1].ok is True

    def test_no_match_tries_every_data_center_then_stops(self, monkeypatch):
        import wdmigrator.auth.endpoint_discovery as mod

        monkeypatch.setattr(
            mod.requests,
            "get",
            fake_requests_get(
                {
                    "host-a.example.com": FakeResponse(404, "no"),
                    "host-b.example.com": FakeResponse(404, "no"),
                    "host-c.example.com": FakeResponse(404, "no"),
                }
            ),
        )
        attempts = list(
            iter_discover_services_host("acme", data_centers=DATA_CENTERS)
        )
        assert len(attempts) == 3
        assert all(not a.ok for a in attempts)


class TestDiscoverServicesHost:
    def test_returns_the_match(self, monkeypatch):
        import wdmigrator.auth.endpoint_discovery as mod

        monkeypatch.setattr(
            mod.requests,
            "get",
            fake_requests_get(
                {
                    "host-a.example.com": FakeResponse(404, "no"),
                    "host-b.example.com": FakeResponse(200, WSDL_BODY),
                    "host-c.example.com": FakeResponse(200, WSDL_BODY),
                }
            ),
        )
        result = discover_services_host(
            "acme", data_centers=DATA_CENTERS, version="v46.0"
        )
        assert result.services_host == "host-b.example.com"
        assert result.data_center == "dc_b"
        assert result.version == "v46.0"

    def test_raises_when_nothing_matches(self, monkeypatch):
        import wdmigrator.auth.endpoint_discovery as mod

        monkeypatch.setattr(
            mod.requests,
            "get",
            fake_requests_get(
                {
                    "host-a.example.com": FakeResponse(404, "no"),
                    "host-b.example.com": FakeResponse(404, "no"),
                    "host-c.example.com": FakeResponse(404, "no"),
                }
            ),
        )
        with pytest.raises(EndpointNotFoundError):
            discover_services_host("acme", data_centers=DATA_CENTERS)
