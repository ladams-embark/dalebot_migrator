"""Tests for index sweeps, targeted lookups, and the disk cache.

The pagination and fault-classification logic is exercised offline against a
fake service, because it is ordinary logic and does not need a tenant. The
`live` tests at the bottom read from the source tenant only.
"""

import json
import time
from types import SimpleNamespace

import pytest

from wdmigrator.discovery import (
    CalculatedFieldSummary,
    Index,
    LookupOutcome,
    build_index,
    cache_path,
    classify_fault,
    ids_of,
    iter_calculated_field_index,
    load_index,
    lookup_calculated_field,
    save_index,
)


def cf_item(wid, ref_id, name="Field", class_name="Arithmetic Calculated Field"):
    return {
        "Calculated_Field_Reference": {
            "ID": [
                {"type": "WID", "_value_1": wid},
                {"type": "Calculated_Field_ID", "_value_1": ref_id},
            ]
        },
        "Calculated_Field_Data": {
            "Calculated_Field_Reference_ID": ref_id,
            "Name": name,
            "Class_Name": class_name,
            "Do_Not_Use": False,
            "Intermediate_Calculation": False,
        },
    }


class FakeConnection:
    """Stands in for a Connection without any transport.

    Records every call so tests can assert on page size and call count — the
    two things that actually matter for a rate-limited sweep.
    """

    def __init__(self, pages=None, fault=None, target_tenant="test_tenant"):
        self._pages = pages or []
        self._fault = fault
        self.calls = []
        self.target = SimpleNamespace(tenant=target_tenant)
        self.limiter = SimpleNamespace(wait=lambda: 0.0)
        self.service = SimpleNamespace(
            Get_Calculated_Fields=self._get,
            Get_Tenanted_Report_Definitions=self._get,
        )

    def _get(self, **kwargs):
        self.calls.append(kwargs)
        if self._fault:
            raise Exception(self._fault)
        page = int((kwargs.get("Response_Filter") or {}).get("Page", 1))
        return self._pages[page - 1]

    def redact(self, text):
        return text


def page(items, page_no, total_pages, total):
    return {
        "Response_Results": {
            "Total_Results": total,
            "Total_Pages": total_pages,
            "Page": page_no,
        },
        "Response_Data": {"Calculated_Field": items},
    }


class TestFaultClassification:
    """The riskiest logic here: mistaking an error for 'absent' causes duplicates."""

    @pytest.mark.parametrize(
        "message",
        [
            "Validation error occurred. Invalid ID value.  'ZZ' is not a valid ID "
            "value for type = 'Calculated_Field_ID'",
            "Validation error occurred. Invalid instance 1435$104 for "
            "Calculated_Field_Request_References--IS (15$40527)",
        ],
    )
    def test_real_not_found_faults_are_recognised(self, message):
        assert classify_fault(message) is LookupOutcome.NOT_FOUND

    @pytest.mark.parametrize(
        "message",
        [
            "The web service or version is invalid for the requested operation",
            "Invalid username or password",
            "HTTPSConnectionPool: Read timed out",
            "429 Too Many Requests",
            "",
        ],
    )
    def test_everything_else_is_unknown_not_missing(self, message):
        """An entitlement or transport failure must never read as 'safe to create'."""
        assert classify_fault(message) is LookupOutcome.UNKNOWN

    def test_matching_is_case_insensitive(self):
        assert (
            classify_fault("IS NOT A VALID ID VALUE FOR TYPE = 'X'")
            is LookupOutcome.NOT_FOUND
        )


class TestLookup:
    def test_found_returns_both_ids_and_the_payload(self):
        conn = FakeConnection(
            pages=[{"Response_Data": {"Calculated_Field": [cf_item("W1", "CF_ONE")]}}]
        )
        result = lookup_calculated_field(conn, reference_id="CF_ONE")
        assert result.outcome is LookupOutcome.FOUND
        assert (result.wid, result.reference_id) == ("W1", "CF_ONE")
        assert result.data is not None

    def test_missing_id_fault_reports_not_found(self):
        conn = FakeConnection(
            fault="Validation error occurred. Invalid ID value.  'ZZ' is not a "
            "valid ID value for type = 'Calculated_Field_ID'"
        )
        assert lookup_calculated_field(conn, reference_id="ZZ").outcome is (
            LookupOutcome.NOT_FOUND
        )

    def test_entitlement_fault_reports_unknown(self):
        conn = FakeConnection(
            fault="The web service or version is invalid for the requested operation"
        )
        result = lookup_calculated_field(conn, reference_id="CF_ONE")
        assert result.outcome is LookupOutcome.UNKNOWN
        assert result.fault

    def test_empty_result_set_is_not_found(self):
        conn = FakeConnection(pages=[{"Response_Data": {"Calculated_Field": []}}])
        assert lookup_calculated_field(conn, reference_id="CF").outcome is (
            LookupOutcome.NOT_FOUND
        )

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"reference_id": "A", "wid": "B"}],
    )
    def test_requires_exactly_one_identifier(self, kwargs):
        with pytest.raises(ValueError):
            lookup_calculated_field(FakeConnection(), **kwargs)


class TestIndexSweep:
    def test_pages_until_total_pages_and_accumulates(self):
        conn = FakeConnection(
            pages=[
                page([cf_item("W1", "A"), cf_item("W2", "B")], 1, 3, 5),
                page([cf_item("W3", "C"), cf_item("W4", "D")], 2, 3, 5),
                page([cf_item("W5", "E")], 3, 3, 5),
            ]
        )
        index = build_index(iter_calculated_field_index(conn))
        assert len(index) == 5
        assert set(index.summaries) == {"W1", "W2", "W3", "W4", "W5"}
        assert len(conn.calls) == 3

    def test_requests_999_rows_per_page(self):
        """The whole cost model depends on this; 100 would be 10x the pages."""
        conn = FakeConnection(pages=[page([cf_item("W1", "A")], 1, 1, 1)])
        build_index(iter_calculated_field_index(conn))
        assert conn.calls[0]["Response_Filter"]["Count"] == 999

    def test_requests_field_data_not_just_references(self):
        """Reference-only responses are stubs — useless for a picker or a walk."""
        conn = FakeConnection(pages=[page([cf_item("W1", "A")], 1, 1, 1)])
        build_index(iter_calculated_field_index(conn))
        assert conn.calls[0]["Response_Group"]["Include_Calculated_Field_Data"] is True

    def test_progress_events_report_position_and_completion(self):
        conn = FakeConnection(
            pages=[
                page([cf_item("W1", "A")], 1, 2, 2),
                page([cf_item("W2", "B")], 2, 2, 2),
            ]
        )
        events = list(iter_calculated_field_index(conn))
        assert [e.page for e in events] == [1, 2]
        assert [e.complete for e in events] == [False, True]
        assert events[0].result is None
        assert events[-1].result is not None
        assert events[0].fraction == pytest.approx(0.5)

    def test_partial_results_survive_early_cancellation(self):
        """A cancelled sweep must keep what it fetched — the report sweep is ~160s."""
        conn = FakeConnection(
            pages=[
                page([cf_item("W1", "A")], 1, 3, 3),
                page([cf_item("W2", "B")], 2, 3, 3),
                page([cf_item("W3", "C")], 3, 3, 3),
            ]
        )
        sweep = iter_calculated_field_index(conn)
        first = next(sweep)
        sweep.close()  # simulate the user hitting Cancel

        assert first.complete is False
        assert first.result is None, "a partial sweep must not look finished"
        assert set(first.index.summaries) == {"W1"}, "fetched data must be retained"
        assert len(conn.calls) == 1, "cancelling must stop further tenant calls"

    def test_full_payloads_are_retained_for_dependency_walking(self):
        conn = FakeConnection(pages=[page([cf_item("W1", "A")], 1, 1, 1)])
        index = build_index(iter_calculated_field_index(conn))
        assert index.payload("W1")["Calculated_Field_Data"]["Name"] == "Field"

    def test_summary_fields_are_extracted(self):
        conn = FakeConnection(
            pages=[page([cf_item("W1", "CF_A", name="Total Comp")], 1, 1, 1)]
        )
        summary = build_index(iter_calculated_field_index(conn)).summaries["W1"]
        assert (summary.reference_id, summary.name) == ("CF_A", "Total Comp")

    def test_items_without_a_wid_are_skipped_not_crashed_on(self):
        broken = {"Calculated_Field_Reference": {"ID": []}, "Calculated_Field_Data": {}}
        conn = FakeConnection(pages=[page([broken, cf_item("W1", "A")], 1, 1, 2)])
        assert set(build_index(iter_calculated_field_index(conn)).summaries) == {"W1"}

    def test_single_page_response_terminates(self):
        conn = FakeConnection(pages=[page([cf_item("W1", "A")], 1, 1, 1)])
        assert len(build_index(iter_calculated_field_index(conn))) == 1
        assert len(conn.calls) == 1

    def test_empty_tenant_does_not_hang(self):
        conn = FakeConnection(pages=[page([], 1, 1, 0)])
        assert len(build_index(iter_calculated_field_index(conn))) == 0


class TestIdsOf:
    def test_flattens_the_id_list(self):
        assert ids_of(cf_item("W1", "A")["Calculated_Field_Reference"]) == {
            "WID": "W1",
            "Calculated_Field_ID": "A",
        }

    @pytest.mark.parametrize("value", [None, {}, {"ID": None}])
    def test_tolerates_missing_references(self, value):
        assert ids_of(value) == {}


class TestDiskCache:
    def _index(self, tenant="test_tenant"):
        return Index(
            kind="calculated_field",
            tenant=tenant,
            fetched_at=time.time(),
            summaries={"W1": CalculatedFieldSummary("W1", "CF_A", "Name", "Class")},
            payloads={"W1": cf_item("W1", "CF_A")},
        )

    def test_round_trips(self, tmp_path):
        path = save_index(self._index(), tmp_path / "cf.json")
        loaded = load_index(path)
        assert len(loaded) == 1
        assert loaded.summaries["W1"].reference_id == "CF_A"
        assert loaded.payload("W1") is not None

    def test_rejects_a_cache_from_a_different_tenant(self, tmp_path):
        """A cross-tenant cache would misclassify what already exists."""
        path = save_index(self._index(tenant="tenant_a"), tmp_path / "cf.json")
        assert load_index(path, tenant="tenant_b") is None
        assert load_index(path, tenant="tenant_a") is not None

    def test_rejects_a_stale_cache(self, tmp_path):
        index = self._index()
        index.fetched_at = time.time() - 10_000
        path = save_index(index, tmp_path / "cf.json")
        assert load_index(path, max_age_seconds=60) is None

    def test_missing_or_corrupt_cache_returns_none_rather_than_raising(self, tmp_path):
        assert load_index(tmp_path / "nope.json") is None
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        assert load_index(broken) is None

    def test_cache_contains_no_credentials(self, tmp_path):
        path = save_index(self._index(), tmp_path / "cf.json")
        raw = path.read_text(encoding="utf-8").lower()
        for forbidden in ("password", "isu_password", "wsse", "secret"):
            assert forbidden not in raw

    def test_cache_path_is_namespaced_per_tenant(self, tmp_path):
        a = cache_path(FakeConnection(target_tenant="tenant_a"), "cf", root=tmp_path)
        b = cache_path(FakeConnection(target_tenant="tenant_b"), "cf", root=tmp_path)
        assert a != b
        assert "tenant_a" in str(a)


@pytest.mark.live
class TestLiveDiscovery:
    """Read-only against the source tenant."""

    def test_lookup_of_a_known_delivered_field_succeeds(self, live_source_connection):
        result = lookup_calculated_field(
            live_source_connection, reference_id="PV_Global_Currency"
        )
        assert result.outcome is LookupOutcome.FOUND
        assert result.wid

    def test_lookup_of_a_nonexistent_id_is_not_found_not_unknown(
        self, live_source_connection
    ):
        """Pins the real fault text that the whole conflict-detection layer rests on."""
        result = lookup_calculated_field(
            live_source_connection, reference_id="WDMIGRATOR_NO_SUCH_FIELD_XYZ"
        )
        assert result.outcome is LookupOutcome.NOT_FOUND, result.fault

    def test_first_index_page_returns_999_rows(self, live_source_connection):
        first = next(iter_calculated_field_index(live_source_connection))
        assert first.fetched == 999
        assert first.total > 9000
