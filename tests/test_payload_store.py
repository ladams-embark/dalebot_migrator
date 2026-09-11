"""Payloads on disk rather than in memory.

The reason this module exists is a measurement: a report index for
``commitconsulting_dpt1`` is 1.2 MB of summaries and 148 MB of payloads, and
holding the pair in RAM costs 585 MB. A hosted Streamlit app has ~2.7 GB for
*all* concurrent viewers and throttles from ~690 MB, so one consultant
sweeping one tenant was enough to degrade it for everybody.

What these tests are really protecting is the distinction between "this index
has no such object" and "this index says it has the object but cannot produce
it". The first is ordinary and means None; the resolver reads it as an
unresolved dependency and reports it. The second is a damaged cache, and if it
also answered None a corrupted store would present as a tidy list of missing
dependencies plus a closure that looked complete while silently omitting
objects — which, in a tool whose writes cannot be undone, is the worst failure
mode available.
"""

import datetime
import decimal
import json
from types import SimpleNamespace

import pytest

from wdmigrator.discovery import (
    CalculatedFieldSummary,
    Index,
    PayloadStore,
    PayloadUnavailable,
    calculated_field_match_index,
    iter_calculated_field_index,
    load_index,
    save_index,
    store_path_for,
)

from test_discovery import FakeConnection, cf_item


def summary(wid, ref_id="REF", name="Field"):
    return CalculatedFieldSummary(
        wid=wid,
        reference_id=ref_id,
        name=name,
        class_name="Arithmetic Calculated Field",
        do_not_use=False,
        intermediate=False,
    )


# ── the store itself ─────────────────────────────────────────────────────────


def test_round_trips_a_payload(tmp_path):
    store = PayloadStore.create(tmp_path / "k.payloads.sqlite")
    store.put_many([("w1", {"Name": "Alpha", "n": 1})])
    assert store.get("w1") == {"Name": "Alpha", "n": 1}


def test_absent_wid_is_none_not_an_error(tmp_path):
    store = PayloadStore.create(tmp_path / "k.payloads.sqlite")
    assert store.get("nope") is None


def test_create_discards_what_was_there(tmp_path):
    """A rebuild must not leave the previous sweep's objects behind.

    An object deleted from the source tenant between two sweeps would
    otherwise keep resolving out of a stale row and get migrated.
    """
    path = tmp_path / "k.payloads.sqlite"
    PayloadStore.create(path).put_many([("gone", {"Name": "Deleted upstream"})])
    assert PayloadStore(path).get("gone") is not None

    fresh = PayloadStore.create(path)
    assert fresh.get("gone") is None


def test_items_streams_everything(tmp_path):
    store = PayloadStore.create(tmp_path / "k.payloads.sqlite")
    store.put_many([(f"w{i}", {"i": i}) for i in range(250)])
    seen = dict(store.items())
    assert len(seen) == 250
    assert seen["w7"] == {"i": 7}


def test_put_many_is_idempotent_per_wid(tmp_path):
    store = PayloadStore.create(tmp_path / "k.payloads.sqlite")
    store.put_many([("w1", {"v": "first"})])
    store.put_many([("w1", {"v": "second"})])
    assert len(store) == 1
    assert store.get("w1") == {"v": "second"}


def test_serialises_what_zeep_hands_back(tmp_path):
    """zeep returns datetimes and Decimals, which JSON will not take.

    They round-trip as strings. That is not new — the all-JSON cache this
    replaced did the same thing — but it is load-bearing enough to pin.
    """
    store = PayloadStore.create(tmp_path / "k.payloads.sqlite")
    store.put_many(
        [("w1", {"when": datetime.datetime(2026, 9, 11), "how_much": decimal.Decimal("1.5")})]
    )
    assert store.get("w1") == {"when": "2026-09-11 00:00:00", "how_much": "1.5"}


def test_damaged_file_raises_rather_than_reporting_absence(tmp_path):
    path = tmp_path / "k.payloads.sqlite"
    PayloadStore.create(path)
    path.write_bytes(b"this is not a database")
    with pytest.raises(PayloadUnavailable):
        PayloadStore(path).get("w1")


def test_store_path_sits_beside_the_summary_file(tmp_path):
    assert store_path_for(tmp_path / "report.json") == tmp_path / "report.payloads.sqlite"


# ── Index, with and without a store ──────────────────────────────────────────


def test_in_memory_index_is_unchanged(tmp_path):
    """No store attached means the old behaviour, exactly.

    Every hand-built index in the rest of the suite depends on this.
    """
    index = Index(kind="calculated_field", tenant="t", fetched_at=0.0,
                  summaries={"w1": summary("w1")}, payloads={"w1": {"Name": "Alpha"}})
    assert index.payload("w1") == {"Name": "Alpha"}
    assert index.payload("absent") is None
    assert dict(index.iter_payloads()) == {"w1": {"Name": "Alpha"}}


def test_store_backed_index_reads_through(tmp_path):
    store = PayloadStore.create(tmp_path / "k.payloads.sqlite")
    store.put_many([("w1", {"Name": "Alpha"})])
    index = Index(kind="calculated_field", tenant="t", fetched_at=0.0,
                  summaries={"w1": summary("w1")}, store=store)

    assert index.payloads == {}
    assert index.payload("w1") == {"Name": "Alpha"}


def test_unknown_wid_is_still_none_on_a_store_backed_index(tmp_path):
    """The resolver asks about WIDs that may belong to another kind entirely.

    ``_calculated_field_node`` calls ``index.payload(wid)`` and treats None as
    "not a calculated field, look elsewhere". That path has to keep working.
    """
    store = PayloadStore.create(tmp_path / "k.payloads.sqlite")
    index = Index(kind="calculated_field", tenant="t", fetched_at=0.0,
                  summaries={"w1": summary("w1")}, store=store)
    assert index.payload("some-other-kind-of-wid") is None


def test_listed_but_missing_payload_is_an_error(tmp_path):
    """The index claims this object. The store cannot produce it. That is
    corruption, and collapsing it to None would fabricate an unresolved
    dependency and drop the object from a closure that still looked whole."""
    store = PayloadStore.create(tmp_path / "k.payloads.sqlite")
    index = Index(kind="calculated_field", tenant="t", fetched_at=0.0,
                  summaries={"w1": summary("w1")}, store=store)

    with pytest.raises(PayloadUnavailable) as excinfo:
        index.payload("w1")
    assert "rebuild" in str(excinfo.value).lower()


def test_in_memory_payload_wins_over_the_store(tmp_path):
    store = PayloadStore.create(tmp_path / "k.payloads.sqlite")
    store.put_many([("w1", {"Name": "from disk"})])
    index = Index(kind="calculated_field", tenant="t", fetched_at=0.0,
                  summaries={"w1": summary("w1")},
                  payloads={"w1": {"Name": "from memory"}}, store=store)
    assert index.payload("w1") == {"Name": "from memory"}


# ── the sweep ────────────────────────────────────────────────────────────────


def _page(items, page=1, total_pages=1):
    return {
        "Response_Results": {"Total_Results": len(items), "Total_Pages": total_pages,
                             "Page": page},
        "Response_Data": {"Calculated_Field": items},
    }


def test_sweep_without_a_store_accumulates_in_memory():
    connection = FakeConnection(pages=[_page([cf_item("w1", "R1")])])
    final = list(iter_calculated_field_index(connection))[-1]
    assert final.index.store is None
    assert final.index.payloads["w1"]["Calculated_Field_Data"]["Name"] == "Field"


def test_sweep_with_a_store_holds_no_payloads_in_memory(tmp_path):
    """The whole point: 4,515 report payloads stay on disk, not in the process."""
    store = PayloadStore.create(tmp_path / "cf.payloads.sqlite")
    connection = FakeConnection(
        pages=[_page([cf_item("w1", "R1"), cf_item("w2", "R2")], total_pages=2),
               _page([cf_item("w3", "R3")], page=2, total_pages=2)]
    )
    final = list(iter_calculated_field_index(connection, payload_store=store))[-1]

    assert final.index.payloads == {}
    assert len(final.index.summaries) == 3
    assert final.index.payload("w2")["Calculated_Field_Data"]["Name"] == "Field"
    assert len(store) == 3


def test_sweep_flushes_each_page_not_just_the_last(tmp_path):
    """Buffered per page so a 999-object page is one transaction. A page that
    only landed at the end would mean a cancelled sweep wrote nothing."""
    store = PayloadStore.create(tmp_path / "cf.payloads.sqlite")
    connection = FakeConnection(
        pages=[_page([cf_item("w1", "R1")], total_pages=2),
               _page([cf_item("w2", "R2")], page=2, total_pages=2)]
    )
    events = iter_calculated_field_index(connection, payload_store=store)
    next(events)
    assert store.get("w1") is not None


# ── the disk cache ───────────────────────────────────────────────────────────


def test_saved_index_no_longer_carries_payloads(tmp_path):
    """1.2 MB instead of 149 MB, in miniature."""
    store = PayloadStore.create(store_path_for(tmp_path / "cf.json"))
    store.put_many([("w1", {"Name": "Alpha"})])
    index = Index(kind="calculated_field", tenant="t", fetched_at=time_now(),
                  summaries={"w1": summary("w1")}, store=store)
    save_index(index, tmp_path / "cf.json")

    document = json.loads((tmp_path / "cf.json").read_text())
    assert "payloads" not in document
    assert document["payload_store"] == "cf.payloads.sqlite"


def test_cache_round_trip_keeps_payloads_reachable(tmp_path):
    store = PayloadStore.create(store_path_for(tmp_path / "cf.json"))
    store.put_many([("w1", {"Name": "Alpha"})])
    save_index(
        Index(kind="calculated_field", tenant="t", fetched_at=time_now(),
              summaries={"w1": summary("w1")}, store=store),
        tmp_path / "cf.json",
    )

    loaded = load_index(tmp_path / "cf.json", tenant="t")
    assert loaded.payloads == {}
    assert loaded.payload("w1") == {"Name": "Alpha"}


def test_summary_file_outliving_its_payloads_is_refused(tmp_path):
    """Better a rebuild than an index whose every lookup raises halfway
    through dependency resolution."""
    store = PayloadStore.create(store_path_for(tmp_path / "cf.json"))
    store.put_many([("w1", {"Name": "Alpha"})])
    save_index(
        Index(kind="calculated_field", tenant="t", fetched_at=time_now(),
              summaries={"w1": summary("w1")}, store=store),
        tmp_path / "cf.json",
    )
    store_path_for(tmp_path / "cf.json").unlink()

    assert load_index(tmp_path / "cf.json", tenant="t") is None


def test_caches_written_before_this_change_still_load(tmp_path):
    """Nobody should have to throw away a cache to pick up the new format.

    Old files carry payloads inline; they load as-is and are replaced by the
    small form on the next rebuild.
    """
    (tmp_path / "cf.json").write_text(json.dumps({
        "kind": "calculated_field",
        "tenant": "t",
        "fetched_at": time_now(),
        "summaries": {"w1": {"wid": "w1", "reference_id": "REF", "name": "Field",
                             "class_name": "Arithmetic Calculated Field",
                             "do_not_use": False, "intermediate": False}},
        "payloads": {"w1": {"Name": "Alpha"}},
    }))
    loaded = load_index(tmp_path / "cf.json", tenant="t")
    assert loaded is not None
    assert loaded.store is None
    assert loaded.payload("w1") == {"Name": "Alpha"}


def test_cross_tenant_matching_works_off_a_store(tmp_path):
    """Matching reads every payload in the index. If it silently saw none, the
    planner would plan CREATE for fields the destination already has and the
    write would land duplicates in a tenant with no delete operation.
    """
    payload = cf_item("w1", "R1", name="Headcount")
    # A shape is (Name, Class_Name, business object WID); without the business
    # object it is incomplete and must never match.
    payload["Calculated_Field_Data"]["External_Field_Reference"] = {
        "ID": [{"type": "WID", "_value_1": "bo-worker"}]
    }
    payload["Calculated_Field_Data"]["WQL_Alias"] = "headcount"

    store = PayloadStore.create(tmp_path / "cf.payloads.sqlite")
    store.put_many([("w1", payload)])
    index = Index(kind="calculated_field", tenant="dest", fetched_at=0.0,
                  summaries={"w1": summary("w1")}, store=store)

    built = calculated_field_match_index(index)
    assert built.shape_of["w1"] == ("Headcount", "Arithmetic Calculated Field", "bo-worker")
    assert built.by_alias["headcount"] == ["w1"]
    assert built.reference_id_of["w1"] == "REF"


def time_now():
    import time
    return time.time()
