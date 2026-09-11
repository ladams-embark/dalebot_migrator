"""Full object payloads, kept on disk instead of in memory.

An :class:`~wdmigrator.discovery.inventory.Index` holds two things: slim
summaries that drive the pickers, and the full payload of every object, which
only dependency resolution reads. Measured against ``commitconsulting_dpt1``,
the split is not close — a report index is 1.2 MB of summaries and 148 MB of
payloads, so 99% of it exists to answer questions about the handful of objects
somebody actually selected.

Holding all of it in RAM costs 585 MB per index, which is survivable on a
laptop and fatal anywhere else. Streamlit Community Cloud gives an app ~2.7 GB
across *every* concurrent viewer and begins throttling around 690 MB, so one
consultant sweeping one tenant was enough to degrade the app for everybody,
and two sweeping at once could take the container down mid-migration — losing
the session of anyone who happened to be partway through a write.

So payloads live in a SQLite file beside the summary JSON and are fetched by
WID on demand. The picker gets its 1.2 MB, the resolver gets its handful of
payloads, and nothing holds 148 MB to do it.

**Every method opens its own connection.** An :class:`Index` is parked in
``st.session_state`` and read again on the next rerun, which may land on a
different thread; a long-lived ``sqlite3.Connection`` sitting in there would
be a cross-thread hazard. A store is therefore just a path, and opening
SQLite against a local file costs tens of microseconds — far below the SOAP
calls this exists to avoid repeating.

**Concurrent access is expected, not merely tolerated.** The cache is keyed by
tenant and shared: two consultants sweeping the same tenant should get one
sweep's worth of work, not two. WAL journalling lets readers run against a
file another session is writing, and a busy timeout absorbs the overlap.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Iterable, Iterator, Mapping

#: WAL readers never block on a writer, which is the whole point here: one
#: session finishing a sweep must not stall another session's resolution.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
)

#: Long enough to cover another session's page-sized commit, short enough that
#: a genuinely stuck file surfaces as an error rather than a hang.
_BUSY_TIMEOUT_SECONDS = 10.0


class PayloadUnavailable(RuntimeError):
    """The index lists this object but its payload could not be read.

    Distinct from "no such object", and deliberately not collapsed into it.
    ``Index.payload()`` returning ``None`` is meaningful — the resolver reads
    it as an unresolved dependency and records it for the user to act on. If a
    damaged or deleted store also answered ``None``, a corrupted cache would
    present as a tidy list of "missing" dependencies and a closure that looks
    complete while silently omitting objects. Failing loudly is the only safe
    reading of a payload that should be there and isn't.
    """


class PayloadStore:
    """WID-keyed payload storage for one (tenant, kind) pair."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"PayloadStore({self.path!s})"

    # ── lifecycle ────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, path: str | Path) -> "PayloadStore":
        """Open for writing, discarding anything already there.

        A rebuild must not leave yesterday's payloads behind: an object
        deleted from the source tenant between sweeps would otherwise keep
        resolving from a stale row, and migrate.
        """
        store = cls(path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        # Drop rather than unlink: another session may hold this file open,
        # and replacing the inode underneath it would give it a stale handle.
        with store._open(write=True) as conn:
            conn.execute("DROP TABLE IF EXISTS payloads")
            conn.execute("CREATE TABLE payloads (wid TEXT PRIMARY KEY, body TEXT NOT NULL)")
        return store

    def exists(self) -> bool:
        return self.path.is_file()

    @contextmanager
    def _open(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """A connection that is actually closed afterwards.

        ``with sqlite3.connect(...)`` manages the *transaction*, not the
        connection — it commits and leaves the handle open. Since every method
        here opens its own, relying on that would leak a descriptor per payload
        lookup, and dependency resolution performs hundreds. It also leaves the
        WAL sidecar behind, which makes a later corrupted database read as
        empty rather than fail.
        """
        conn = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_SECONDS)
        try:
            for pragma in _PRAGMAS:
                conn.execute(pragma)
            if write:
                with conn:
                    yield conn
            else:
                yield conn
        finally:
            conn.close()

    # ── writing ──────────────────────────────────────────────────────────────

    def put_many(self, items: Iterable[tuple[str, Mapping]]) -> int:
        """Write a batch in one transaction. Returns how many rows were written.

        Batched per page rather than per object because a sweep page carries
        999 of them, and 999 transactions would make the disk the bottleneck
        in a loop whose whole purpose is to keep up with the network.

        ``default=str`` for the same reason the summary cache needs it: zeep
        hands back datetimes and Decimals, which JSON will not take. This does
        mean a payload round-trips as strings, which was already true of the
        previous all-JSON cache and is what the writers expect.
        """
        rows = [(wid, json.dumps(payload, default=str)) for wid, payload in items]
        if not rows:
            return 0
        with self._open(write=True) as conn:
            conn.executemany("INSERT OR REPLACE INTO payloads VALUES (?, ?)", rows)
        return len(rows)

    # ── reading ──────────────────────────────────────────────────────────────

    def get(self, wid: str) -> dict | None:
        """One payload by WID, or None if this store has no such row."""
        try:
            with self._open() as conn:
                row = conn.execute(
                    "SELECT body FROM payloads WHERE wid = ?", (wid,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise PayloadUnavailable(f"Could not read {self.path.name}: {exc}") from exc
        if row is None:
            return None
        return json.loads(row[0])

    def __contains__(self, wid: str) -> bool:
        return self.get(wid) is not None

    def __len__(self) -> int:
        try:
            with self._open() as conn:
                return int(conn.execute("SELECT COUNT(*) FROM payloads").fetchone()[0])
        except sqlite3.Error:
            return 0

    def items(self) -> Iterator[tuple[str, dict]]:
        """Every payload, streamed.

        One connection and one cursor for the whole scan, because the callers
        are the cross-tenant match builders, which touch every object in the
        index — 8,981 of them on the destination tenant. Fetching those one
        ``get()`` at a time would open 8,981 connections to answer a question
        SQLite can answer with a single sequential read.
        """
        try:
            with self._open() as conn:
                for wid, body in conn.execute("SELECT wid, body FROM payloads"):
                    yield wid, json.loads(body)
        except sqlite3.Error as exc:
            raise PayloadUnavailable(f"Could not read {self.path.name}: {exc}") from exc


def store_path_for(index_path: str | Path) -> Path:
    """Where the payloads for a given summary-cache file live.

    Derived from the JSON path rather than stored in it, so the pair travels
    together: delete the summary file and the orphaned store is simply never
    consulted again, and ``Rebuild all`` replaces both.
    """
    p = Path(index_path)
    return p.with_name(f"{p.stem}.payloads.sqlite")


__all__ = [
    "PayloadStore",
    "PayloadUnavailable",
    "store_path_for",
]
