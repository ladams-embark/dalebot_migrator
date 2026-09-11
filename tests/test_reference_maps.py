"""Reference answers are reusable, but only against the tenant they answer for.

The reference table on Run is the hardest thing the wizard asks anyone to do,
and it is the one thing the tool genuinely cannot answer on their behalf —
these references point at tenant data, and no amount of dependency resolution
conjures an organization that is not there. So the goal is not to answer them;
it is to stop asking the same thirty-one questions on the second migration to
the same destination.

The tenant key is the safety property. ``Organization_Reference_ID = CC_9``
means something in the tenant it was looked up in and something else, or
nothing, anywhere else — and applying a map rewrites what gets written.
"""

from __future__ import annotations

import json

import pytest

from wdmigrator.api import ReferenceAction, ReferenceDecision
from wdmigrator.ui import reference_maps


def _decisions() -> dict:
    return {
        "W_ORG": ReferenceDecision(
            source_wid="W_ORG",
            action=ReferenceAction.REPLACE,
            replacement_type="Organization_Reference_ID",
            replacement_value="CC_9",
        ),
        "W_CO": ReferenceDecision(source_wid="W_CO", action=ReferenceAction.BLANK),
        "W_EVT": ReferenceDecision(source_wid="W_EVT", action=ReferenceAction.KEEP),
    }


class TestRoundTrip:
    def test_decisions_come_back_intact(self, tmp_path):
        reference_maps.save_map(_decisions(), dest_tenant="dpt5", directory=tmp_path)
        loaded = reference_maps.load_map("dpt5", directory=tmp_path)
        assert len(loaded) == 3
        org = loaded.decisions["W_ORG"]
        assert org.action is ReferenceAction.REPLACE
        assert org.replacement_type == "Organization_Reference_ID"
        assert org.replacement_value == "CC_9"
        assert loaded.decisions["W_CO"].action is ReferenceAction.BLANK
        assert loaded.decisions["W_EVT"].action is ReferenceAction.KEEP

    def test_no_map_for_this_destination_is_not_an_error(self, tmp_path):
        assert reference_maps.load_map("never_seen", directory=tmp_path) is None

    def test_saving_replaces_rather_than_merges(self, tmp_path):
        """A decision the user has since changed must not be resurrected by a
        stale entry underneath it. The in-session map is always complete."""
        reference_maps.save_map(_decisions(), dest_tenant="dpt5", directory=tmp_path)
        reference_maps.save_map(
            {"W_ORG": ReferenceDecision(source_wid="W_ORG", action=ReferenceAction.BLANK)},
            dest_tenant="dpt5",
            directory=tmp_path,
        )
        loaded = reference_maps.load_map("dpt5", directory=tmp_path)
        assert set(loaded.decisions) == {"W_ORG"}
        assert loaded.decisions["W_ORG"].action is ReferenceAction.BLANK


class TestTenantScoping:
    def test_each_destination_gets_its_own_file(self, tmp_path):
        reference_maps.save_map(_decisions(), dest_tenant="dpt5", directory=tmp_path)
        reference_maps.save_map({}, dest_tenant="dpt7", directory=tmp_path)
        assert reference_maps.load_map("dpt5", directory=tmp_path).decisions
        assert not reference_maps.load_map("dpt7", directory=tmp_path).decisions

    def test_a_file_moved_onto_the_wrong_tenant_is_refused(self, tmp_path):
        """The tenant inside the file is authoritative, not the filename.
        Applying CC_9 to a tenant where CC_9 is a different cost centre would
        substitute a wrong value into a write that cannot be undone."""
        reference_maps.save_map(_decisions(), dest_tenant="dpt5", directory=tmp_path)
        (tmp_path / "dpt5.json").rename(tmp_path / "dpt7.json")
        with pytest.raises(reference_maps.ReferenceMapError, match="saved against"):
            reference_maps.load_map("dpt7", directory=tmp_path)

    def test_tenant_names_are_made_filename_safe(self, tmp_path):
        path = reference_maps.map_path("Commit/Consulting DPT5", directory=tmp_path)
        assert path.name == "commit-consulting-dpt5.json"


class TestApplicability:
    def _map(self) -> reference_maps.ReferenceMap:
        return reference_maps.ReferenceMap(
            dest_tenant="dpt5", saved_at="", decisions=_decisions()
        )

    def test_only_references_this_run_actually_hit_are_offered(self):
        """A map accumulated over several migrations holds answers for objects
        that are not in this selection. Folding those into reference_decisions
        would change the plan hash without changing a single payload, so two
        identical runs would hash differently and invalidate a review."""
        usable = reference_maps.applicable(self._map(), {"W_ORG": {}, "W_OTHER": {}})
        assert set(usable) == {"W_ORG"}

    def test_nothing_in_common_yields_nothing(self):
        assert reference_maps.applicable(self._map(), {"W_UNRELATED": {}}) == {}


class TestUnreadableFiles:
    def test_a_corrupt_file_raises_rather_than_loading_partially(self, tmp_path):
        (tmp_path / "dpt5.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(reference_maps.ReferenceMapError):
            reference_maps.load_map("dpt5", directory=tmp_path)

    def test_a_foreign_schema_version_raises(self, tmp_path):
        (tmp_path / "dpt5.json").write_text(
            json.dumps({"$schema_version": 99, "dest_tenant": "dpt5"}), encoding="utf-8"
        )
        with pytest.raises(reference_maps.ReferenceMapError, match="different version"):
            reference_maps.load_map("dpt5", directory=tmp_path)

    def test_an_unreadable_row_raises_rather_than_being_skipped(self, tmp_path):
        """Silently dropping a row would leave the user believing a reference
        was answered when it was not, and the write would fail on it."""
        (tmp_path / "dpt5.json").write_text(
            json.dumps(
                {
                    "$schema_version": reference_maps.SCHEMA_VERSION,
                    "dest_tenant": "dpt5",
                    "decisions": [{"source_wid": "W0", "action": "teleport"}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(reference_maps.ReferenceMapError, match="unreadable row"):
            reference_maps.load_map("dpt5", directory=tmp_path)
