"""One private scratch area per browser session.

The engine's disk artifacts were written to fixed paths — ``out/sessions``,
``out/reference-maps``, ``out/errors`` — on the assumption that the person
running the app is the only person running the app. That holds when it is
started from a terminal. It stops holding the moment the app is hosted, where
one container serves every consultant at once and those paths become a single
shared namespace:

- the resume picker listed *everyone's* saved sessions, each labelled with the
  tenants and usernames it came from, and resuming one handed over the other
  person's full report payloads;
- ``KEEP_SESSIONS`` pruned by counting files in one directory, so an active
  consultant silently deleted a colleague's saved work;
- reference maps are keyed by destination tenant alone and replace rather than
  merge, so two people migrating into the same tenant overwrote each other.

None of that could cause an unreviewed write — approvals are never persisted,
and a reference map still has to be loaded by hand and is shown as an editable
table before anything uses it. It is a confidentiality and clobbering problem,
which is enough.

**The tenant index cache is deliberately left shared.** It is keyed by tenant,
holds no personal state, is written atomically, and a report sweep costs two
and a half minutes — two consultants working the same tenant should pay for
one sweep, not two. That one is a feature.

**The identifier lives in the URL.** It has to survive a browser reload, or
scoping would break the resume feature it is meant to protect: a fresh random
id per run would mean nobody could ever see their own saved session again.
``st.session_state`` dies with the websocket, and Streamlit cannot set a
cookie without a custom component, so the query string is what is left. The
consequence is worth stating plainly, and the UI does state it: the link is
the key to the saved work, and anyone you send it to lands in your workspace.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

import streamlit as st

#: Short, URL-safe, and not derived from anything about the user. Eight bytes
#: is far more than enough to keep a handful of concurrent consultants apart,
#: and this is a partition key, not a credential — the data behind it is
#: already reachable by anyone who can open the app.
_ID_BYTES = 8

#: Query-string parameter holding the workspace id.
QUERY_KEY = "w"

#: Mirrored into session_state so a rerun does not pay for the query lookup
#: or risk minting a second id inside one session.
_STATE_KEY = "_workspace_id"

_SAFE = re.compile(r"[^a-z0-9]")


def _clean(raw) -> str:
    """Query strings are user input. This one becomes a directory name.

    Stripped to lowercase alphanumerics, so ``?w=../../etc`` cannot climb out
    of the artifact directory — it reduces to something harmless, or to
    nothing, and nothing means a fresh id gets minted.

    A repeated parameter arrives as a list; take the first rather than letting
    ``str()`` render the whole list and happen to clean up into something
    plausible.
    """
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    return _SAFE.sub("", str(raw).lower())[: _ID_BYTES * 2]


def workspace_id() -> str:
    """This browser session's workspace id, minting one if needed."""
    existing = st.session_state.get(_STATE_KEY)
    if existing:
        return existing

    from_url = _clean(st.query_params.get(QUERY_KEY, ""))
    workspace = from_url or secrets.token_hex(_ID_BYTES)
    if not from_url:
        # Put it in the URL so a reload comes back to the same workspace.
        st.query_params[QUERY_KEY] = workspace
    st.session_state[_STATE_KEY] = workspace
    return workspace


def user_dir(base: Path | str) -> Path:
    """``base`` scoped to this browser session, created on demand.

    Callers pass the directory they would have written to before — the
    scoping is applied here rather than at each call site so a new artifact
    kind cannot forget to do it.
    """
    path = Path(base) / workspace_id()
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_user_dir(base: Path | str) -> Path | None:
    """:func:`user_dir`, but for callers that must not raise.

    Only the unhandled-exception handler uses this. That handler is the last
    thing standing between a zeep fault and a traceback rendered on the page
    with a cleartext WS-Security password in it, so nothing inside it may
    throw — least of all the bookkeeping that decides which folder to put the
    redacted report in. Falling back to the unscoped directory loses isolation
    for one file whose contents are already redacted; losing the handler would
    lose the redaction itself.
    """
    try:
        return user_dir(base)
    except Exception:  # noqa: BLE001 - deliberately total
        return None


def shared_link() -> str:
    """What to tell someone to bookmark, as a query string fragment."""
    return f"?{QUERY_KEY}={workspace_id()}"


__all__ = ["QUERY_KEY", "safe_user_dir", "shared_link", "user_dir", "workspace_id"]
