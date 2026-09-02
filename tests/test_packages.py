"""Round-trip tests for the stored-package format."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from wdmigrator.migrate.resolver import Closure, Node, NodeKind
from wdmigrator.packages import (
    PackageError,
    SCHEMA_VERSION,
    list_packages,
    load_package,
    package_from_closure,
    save_package,
)


def _sample_closure() -> Closure:
    a = Node(
        node_id="report:A", kind=NodeKind.REPORT, source_wid="A",
        reference_id="RPT-A", name="Report A",
        payload={"Tenanted_Report_Definition_Data": {"Name": "Report A"}},
        depends_on=frozenset({"calculated_field:X"}),
        selected=True,
    )
    x = Node(
        node_id="calculated_field:X", kind=NodeKind.CALCULATED_FIELD,
        source_wid="X", reference_id="CF-X", name="Field X",
        payload={"Calculated_Field_Data": {"Name": "Field X"}},
        required_by=frozenset({"report:A"}),
    )
    closure = Closure(
        nodes={"report:A": a, "calculated_field:X": x},
        unresolved_reference_ids={"CF-Y"},
        passthrough_wids={"DELIVERED_WID"},
    )
    return closure


class TestPackageRoundTrip:
    def test_save_then_load_reconstructs_the_closure(self, tmp_path: Path):
        closure = _sample_closure()
        pkg = package_from_closure(
            closure,
            name="sample",
            description="Test package",
            source_tenant="sageai",
            source_services_host="impl-services1.wd503.myworkday.com",
            captured_at="2026-09-02T00:00:00+00:00",
        )
        path = tmp_path / "sample.json"
        save_package(pkg, path)

        loaded = load_package(path)
        assert loaded.name == "sample"
        assert loaded.source_tenant == "sageai"
        assert set(loaded.closure.nodes) == set(closure.nodes)
        assert loaded.closure.nodes["report:A"].payload == closure.nodes["report:A"].payload
        assert loaded.closure.nodes["report:A"].depends_on == frozenset({"calculated_field:X"})
        assert loaded.closure.nodes["report:A"].selected is True
        assert loaded.closure.nodes["calculated_field:X"].required_by == frozenset({"report:A"})
        assert loaded.closure.unresolved_reference_ids == {"CF-Y"}
        assert loaded.closure.passthrough_wids == {"DELIVERED_WID"}

    def test_captured_at_is_populated_when_not_given(self, tmp_path: Path):
        pkg = package_from_closure(
            _sample_closure(), name="s", description="", source_tenant="t",
        )
        assert pkg.captured_at  # non-empty ISO string

    def test_write_is_atomic(self, tmp_path: Path):
        """An overwrite in place must not leave a torn file if the write races.

        Serialization writes to a suffixed temp then renames; we verify the
        temp is not left behind after a successful write.
        """
        path = tmp_path / "s.json"
        pkg = package_from_closure(_sample_closure(), name="s", description="", source_tenant="t")
        save_package(pkg, path)
        temps = list(tmp_path.glob("*.tmp"))
        assert temps == []


class TestPackageErrors:
    def test_wrong_schema_version_is_refused(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"$schema_version": 9999, "name": "x", "source_tenant": "t", "nodes": []}))
        with pytest.raises(PackageError, match="unsupported \\$schema_version"):
            load_package(p)

    def test_missing_required_field_is_refused(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"$schema_version": SCHEMA_VERSION, "name": "x"}))
        with pytest.raises(PackageError, match="missing required field"):
            load_package(p)

    def test_absent_file_yields_package_error(self, tmp_path: Path):
        with pytest.raises(PackageError, match="not found"):
            load_package(tmp_path / "no_such.json")

    def test_invalid_json_yields_package_error(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("{ not json")
        with pytest.raises(PackageError, match="not valid JSON"):
            load_package(p)


class TestListPackages:
    def test_lists_valid_packages_and_skips_bad_ones(self, tmp_path: Path):
        good = package_from_closure(_sample_closure(), name="good", description="ok", source_tenant="t")
        save_package(good, tmp_path / "good.json")
        # A non-package json in the same directory must not blow up the listing
        (tmp_path / "notes.json").write_text('{"random": "junk"}')
        metas = list_packages(tmp_path)
        assert [m.name for m in metas] == ["good"]
        assert metas[0].node_count == 2

    def test_empty_directory_returns_empty_list(self, tmp_path: Path):
        assert list_packages(tmp_path) == []

    def test_missing_directory_returns_empty_list(self, tmp_path: Path):
        assert list_packages(tmp_path / "does_not_exist") == []
