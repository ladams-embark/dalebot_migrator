"""Reusable answers to "the destination has no such object", per destination.

The reference table on Run is the hardest thing the wizard asks anyone to do.
A report with thirty-one default Companies produces thirty-one rows, each one
a source WID the destination cannot resolve, each needing a human to decide
between dropping the value and naming a destination-side replacement. There
is no way for the tool to answer them: they point at tenant *data*, and no
amount of dependency resolution conjures an organization that is not there.

What the tool can do is stop asking twice. The answers are stable — the same
source WID against the same destination tenant has the same right answer next
month as it does today — so they are worth keeping.

**Keyed by destination tenant, and that is the whole point.** A replacement
value like ``Organization_Reference_ID = CC_9`` is meaningful only in the
tenant it was looked up in. A map applied to a different destination would
substitute identifiers that mean something else there, or nothing, and a
reference substitution is a change to what gets written. So maps are stored
per destination and are never applied to a tenant they were not built for.

**Never applied automatically.** Loading one is a click, and it says how many
rows it will answer before it answers them. The alternative — silently
rewriting payloads from a file on disk — is exactly the kind of unreviewed
change this tool exists to refuse.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wdmigrator.api import ReferenceAction, ReferenceDecision

SCHEMA_VERSION = 1

#: Alongside out/cache, out/sessions and out/errors. Gitignored: the values in
#: here are destination-tenant identifiers.
MAP_DIR = Path("out") / "reference-maps"


class ReferenceMapError(RuntimeError):
    """A saved reference map could not be read, or its shape was unexpected."""


@dataclass(frozen=True)
class ReferenceMap:
    """Decisions previously made against one destination tenant."""

    dest_tenant: str
    saved_at: str
    decisions: dict

    def __len__(self) -> int:
        return len(self.decisions)


def _safe(tenant: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", tenant.strip()).strip("-").lower()


def map_path(dest_tenant: str, *, directory=None) -> Path:
    folder = Path(directory) if directory is not None else MAP_DIR
    return folder / f"{_safe(dest_tenant) or 'unknown'}.json"


def save_map(decisions: dict, *, dest_tenant: str, directory=None) -> Path:
    """Write every decision made in this run, replacing any previous map.

    Replacing rather than merging is deliberate. A decision the user has since
    changed should not be resurrected by a stale entry underneath it, and the
    in-session map is always the complete set — ``reference_decisions``
    accumulates across the whole run rather than being rebuilt per attempt.
    """
    path = map_path(dest_tenant, directory=directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "$schema_version": SCHEMA_VERSION,
        "dest_tenant": dest_tenant,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "decisions": [
            {
                "source_wid": d.source_wid,
                "action": d.action.value,
                "replacement_type": d.replacement_type,
                "replacement_value": d.replacement_value,
                "note": d.note,
            }
            for d in decisions.values()
        ],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_map(dest_tenant: str, *, directory=None) -> ReferenceMap | None:
    """Read the map for this destination, or ``None`` if there is not one."""
    path = map_path(dest_tenant, directory=directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReferenceMapError(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(data, dict) or data.get("$schema_version") != SCHEMA_VERSION:
        raise ReferenceMapError(
            f"{path.name} was written by a different version of this tool."
        )
    # A map that has drifted onto the wrong tenant must not be applied on the
    # strength of its filename. The tenant inside the file is authoritative.
    stored_tenant = data.get("dest_tenant", "")
    if stored_tenant != dest_tenant:
        raise ReferenceMapError(
            f"{path.name} was saved against `{stored_tenant}`, not "
            f"`{dest_tenant}`. Replacement identifiers only mean anything in "
            "the tenant they were looked up in."
        )
    decisions = {}
    for entry in data.get("decisions") or []:
        try:
            decisions[entry["source_wid"]] = ReferenceDecision(
                source_wid=entry["source_wid"],
                action=ReferenceAction(entry["action"]),
                replacement_type=entry.get("replacement_type"),
                replacement_value=entry.get("replacement_value"),
                note=entry.get("note", ""),
            )
        except (KeyError, ValueError) as exc:
            raise ReferenceMapError(f"{path.name} holds an unreadable row: {exc}") from exc
    return ReferenceMap(
        dest_tenant=stored_tenant,
        saved_at=data.get("saved_at", ""),
        decisions=decisions,
    )


def applicable(reference_map: ReferenceMap, blocking_references: dict) -> dict:
    """The subset of a map that answers references this run actually hit.

    A map accumulated over several migrations holds answers for objects not in
    this selection. Carrying those into ``reference_decisions`` would inflate
    the plan hash with entries that substitute nothing, making two identical
    runs produce different hashes and quietly invalidating a review.
    """
    return {
        wid: decision
        for wid, decision in reference_map.decisions.items()
        if wid in blocking_references
    }


__all__ = [
    "MAP_DIR",
    "SCHEMA_VERSION",
    "ReferenceMap",
    "ReferenceMapError",
    "applicable",
    "load_map",
    "map_path",
    "save_map",
]
