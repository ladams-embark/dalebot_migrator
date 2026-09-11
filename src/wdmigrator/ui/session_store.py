"""Saving and resuming a wizard session.

Streamlit keeps ``st.session_state`` in the server process, keyed by a
websocket connection. Reload the browser tab and that connection is gone, and
with it every tenant URL, every username, and every object the user picked.
The steps that got them there are not cheap: a report catalog sweep is about
two and a half minutes, and a selection of thirty reports assembled across
half a dozen searches is not something anyone wants to redo because they
closed a laptop lid.

**A session snapshot restores inputs, never approvals.** That distinction is
the entire safety argument for this module. What comes back is the work of
gathering: which tenants, which usernames, which objects. What does not come
back is anything that stands for a human having looked at something and
agreed to it — no reviewed dry run, no typed destination tenant name, no
acknowledged warnings, no plan. Those are re-earned in the new session
against a plan built in the new session, because this tool writes to a
service with no delete operation and a rehydrated tick-box is not consent.

Passwords are never written. Neither is a ``Connection``: it holds a live
authenticated SOAP client, and the only way to get one back is to
authenticate again.

Indexes are absent for a different reason — they already persist. Sweeps
cache to ``out/cache`` under their own tenant-scoped keys
(:data:`wdmigrator.discovery.inventory.CACHE_ROOT`), and Select reloads them
on the way past, so duplicating ~34 MB of calculated fields per session file
would buy nothing.

File shape (JSON, forward-compatible via ``$schema_version``)::

    {
      "$schema_version": 1,
      "saved_at": "2026-09-11T...",
      "source": {"target_raw": "...", "username": "...", "tenant": "..."},
      "dest":   {"target_raw": "...", "username": "...", "tenant": "..."},
      "object_kinds": ["reports"],
      "selected_field_wids": [...],
      "selected_reports_added": {"<wid>": {...payload...}},
      "selected_dashboards_added": {"<wid>": {...payload...}},
      "selected_time_calculation_wids": [...],
      "reference_decisions": [{"source_wid": ..., "action": ..., ...}],
      "report_sharing": "UNSHARED",
      "package_name": null
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wdmigrator.api import ReferenceAction, ReferenceDecision, ReportSharing

SCHEMA_VERSION = 1

#: Alongside ``out/cache`` and the run logs. Gitignored with the rest of
#: ``out/`` — these files carry tenant names and usernames.
SESSION_DIR = Path("out") / "sessions"

#: Session files are rotated rather than accumulated. The value of an old one
#: drops off a cliff — a selection is interesting until it has been migrated
#: and then it is history — and an unbounded directory of files holding
#: report payloads is a liability nobody asked for.
KEEP_SESSIONS = 10


class SessionError(RuntimeError):
    """A saved session could not be read, or its shape was unexpected."""


@dataclass(frozen=True)
class SessionSummary:
    """The header of a saved session — enough to offer it without loading it."""

    path: Path
    saved_at: str
    source_tenant: str
    dest_tenant: str
    object_counts: dict[str, int]

    @property
    def total_objects(self) -> int:
        return sum(self.object_counts.values())

    @property
    def label(self) -> str:
        """One line for a picker. Says what is in it and where it came from —
        resuming the wrong session would silently point a write at the wrong
        tenant, so both tenants are named up front rather than in a caption."""
        parts = ", ".join(
            f"{count} {noun}" for noun, count in sorted(self.object_counts.items()) if count
        )
        return (
            f"{parts or 'nothing selected'} — "
            f"{self.source_tenant or '?'} to {self.dest_tenant or '?'} "
            f"({_short_timestamp(self.saved_at)})"
        )


def _short_timestamp(raw: str) -> str:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return raw or "unknown time"


def _tenant_of(side) -> str:
    return side.target.tenant if side.target is not None else ""


def _object_counts(state) -> dict[str, int]:
    return {
        "reports": len(state.selected_reports_added),
        "dashboards": len(state.selected_dashboards_added),
        "calculated fields": len(state.selected_field_wids),
        "time calculations": len(state.selected_time_calculation_wids),
    }


def snapshot(state) -> dict:
    """The serialisable half of ``state``.

    Kept as an explicit allow-list rather than "everything except a deny-list".
    A field added to :class:`~wdmigrator.ui.state.WizardState` later should
    default to *not* being persisted: the cost of forgetting to add a
    selection here is that a user re-picks it, and the cost of forgetting to
    exclude a new acknowledgement flag is that the tool writes to a tenant on
    the strength of a tick nobody made this session.
    """
    return {
        "$schema_version": SCHEMA_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "target_raw": state.source.target_raw,
            "username": state.source.username,
            "tenant": _tenant_of(state.source),
        },
        "dest": {
            "target_raw": state.dest.target_raw,
            "username": state.dest.username,
            "tenant": _tenant_of(state.dest),
        },
        "object_kinds": list(state.object_kinds),
        "selected_field_wids": sorted(state.selected_field_wids),
        "selected_reports_added": dict(state.selected_reports_added),
        "selected_dashboards_added": dict(state.selected_dashboards_added),
        "selected_time_calculation_wids": sorted(state.selected_time_calculation_wids),
        "reference_decisions": [
            {
                "source_wid": d.source_wid,
                "action": d.action.value,
                "replacement_type": d.replacement_type,
                "replacement_value": d.replacement_value,
                "note": d.note,
            }
            for d in state.reference_decisions.values()
        ],
        "report_sharing": state.report_sharing.value,
        "package_name": state.package.name if state.package is not None else None,
    }


def _safe_stem(state) -> str:
    raw = f"{_tenant_of(state.source) or 'source'}-to-{_tenant_of(state.dest) or 'dest'}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-") or "session"


def save_session(state, *, directory: str | Path | None = None) -> Path:
    """Write a snapshot and prune old ones. Returns the file written."""
    folder = Path(directory) if directory is not None else SESSION_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{stamp}-{_safe_stem(state)}.json"
    # ``default=str`` for the same reason packages need it: report payloads
    # come out of zeep carrying datetimes and Decimals.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot(state), indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    _prune(folder)
    return path


def _prune(folder: Path) -> None:
    existing = sorted(folder.glob("*.json"))
    for stale in existing[:-KEEP_SESSIONS]:
        stale.unlink(missing_ok=True)


def load_session(path: str | Path) -> dict:
    """Read one session file. Raises :class:`SessionError` on anything odd."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SessionError(f"Could not read {p.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise SessionError(f"{p.name} is not a session file.")
    version = data.get("$schema_version")
    if version != SCHEMA_VERSION:
        raise SessionError(
            f"{p.name} was written by a different version of this tool "
            f"(schema {version!r}, expected {SCHEMA_VERSION})."
        )
    return data


def list_sessions(directory: str | Path | None = None) -> list[SessionSummary]:
    """Newest first. Unreadable files are skipped, not raised — a picker
    should not blow up because one file in the folder is half-written."""
    folder = Path(directory) if directory is not None else SESSION_DIR
    if not folder.is_dir():
        return []
    out: list[SessionSummary] = []
    for candidate in sorted(folder.glob("*.json"), reverse=True):
        try:
            data = load_session(candidate)
        except SessionError:
            continue
        out.append(
            SessionSummary(
                path=candidate,
                saved_at=data.get("saved_at", ""),
                source_tenant=(data.get("source") or {}).get("tenant", ""),
                dest_tenant=(data.get("dest") or {}).get("tenant", ""),
                object_counts={
                    "reports": len(data.get("selected_reports_added") or {}),
                    "dashboards": len(data.get("selected_dashboards_added") or {}),
                    "calculated fields": len(data.get("selected_field_wids") or []),
                    "time calculations": len(
                        data.get("selected_time_calculation_wids") or []
                    ),
                },
            )
        )
    return out


def restore(state, data: dict) -> list[str]:
    """Copy a snapshot onto ``state``. Returns notes on what did not come back.

    The caller is expected to show those notes. A user who resumes a session
    and sees their thirty reports sitting there will reasonably assume
    everything else came back too; being told plainly that the dry run and the
    acknowledgements did not is the difference between a resumed session and a
    resumed session someone trusts more than they should.
    """
    source = data.get("source") or {}
    dest = data.get("dest") or {}
    state.source.target_raw = source.get("target_raw", "")
    state.source.username = source.get("username", "")
    state.dest.target_raw = dest.get("target_raw", "")
    state.dest.username = dest.get("username", "")

    state.object_kinds = list(data.get("object_kinds") or [])
    state.selected_field_wids = set(data.get("selected_field_wids") or [])
    state.selected_reports_added = dict(data.get("selected_reports_added") or {})
    state.selected_reports = dict(state.selected_reports_added)
    state.selected_dashboards_added = dict(data.get("selected_dashboards_added") or {})
    state.selected_dashboards = dict(state.selected_dashboards_added)
    state.selected_time_calculation_wids = set(
        data.get("selected_time_calculation_wids") or []
    )
    state.reference_decisions = {
        d["source_wid"]: ReferenceDecision(
            source_wid=d["source_wid"],
            action=ReferenceAction(d["action"]),
            replacement_type=d.get("replacement_type"),
            replacement_value=d.get("replacement_value"),
            note=d.get("note", ""),
        )
        for d in (data.get("reference_decisions") or [])
    }
    try:
        state.report_sharing = ReportSharing(data.get("report_sharing"))
    except ValueError:
        state.report_sharing = ReportSharing.UNSHARED

    # Remembered so the first source connection can check it. Restoring a
    # selection of WIDs and then connecting to a different tenant would leave
    # every one of them pointing at nothing, or worse, at something else.
    state.restored_source_tenant = source.get("tenant", "")

    notes = [
        "Passwords are never saved — enter them again and test both connections.",
        "The dry run, the plan, and every acknowledgement have to be redone. "
        "They are deliberately not restored: approval belongs to the plan you "
        "are about to run, not to a file.",
    ]
    if data.get("package_name"):
        notes.append(
            f"This session had the stored package {data['package_name']!r} "
            "loaded. Load it again below."
        )
    return notes


__all__ = [
    "KEEP_SESSIONS",
    "SCHEMA_VERSION",
    "SESSION_DIR",
    "SessionError",
    "SessionSummary",
    "list_sessions",
    "load_session",
    "restore",
    "save_session",
    "snapshot",
]
