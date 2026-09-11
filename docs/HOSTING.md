# Running this app hosted, for several people at once

Everything here applies when the wizard runs as a shared Streamlit app rather
than from somebody's terminal. It was written against Streamlit Community
Cloud, whose constraints are the tightest, but the reasoning holds anywhere one
process serves several consultants.

The short version: a hosted deployment breaks four assumptions the app used to
make, and all four have been dealt with. This document says what they were, so
that a future change does not quietly reintroduce one.

---

## What a hosted container actually gives you

| | Community Cloud |
|---|---|
| Memory | ~2.7 GB hard ceiling, throttling from ~690 MB — for the **whole app**, not per viewer |
| CPU | 2 cores, shared by every concurrent session |
| Disk | ~50 GB, and **wiped whenever the container restarts** |
| Restarts | On redeploy, on waking from idle, on running out of memory |
| Visibility | Free apps are reachable by anyone with the URL unless viewers are restricted |

The memory figure is the one that bites, because it is shared and because
exceeding it does not degrade gracefully — the container dies and takes every
session with it, including anyone partway through a write.

---

## 1. Memory: payloads do not live in RAM

**The problem.** An `Index` held slim summaries for the pickers *and* the full
payload of every object. Measured on `commitconsulting_dpt1`, a report index is
1.2 MB of summaries and 148 MB of payloads, and the pair costs **585 MB
resident**. One consultant sweeping one tenant put the app at the throttling
threshold; source plus destination put them at ~1.2 GB on their own. Two people
doing it at once was a dead container.

**What changed.** Payloads stream to a SQLite file beside the summary cache
(`wdmigrator/discovery/payload_store.py`) and are read by WID on demand. The
same index now costs **4 MB** and loads in 0.01s instead of 1.3s. A payload
lookup is 0.31 ms. The cross-tenant match builders, which genuinely do need
every payload, stream the store in one pass — 8,776 destination fields in
0.14s with no measurable growth.

**What to preserve.** `Index.payload()` returns `None` for "no such object in
this index" and *raises* `PayloadUnavailable` for "this index lists the object
but the store cannot produce it". Do not collapse those. The resolver reads
`None` as an unresolved dependency and reports it to the user; a damaged cache
answering `None` would present as a tidy list of missing dependencies plus a
closure that looked complete while silently omitting objects.

---

## 2. Isolation: one filesystem, several consultants

**The problem.** `out/sessions`, `out/reference-maps` and `out/errors` were
fixed paths. The resume picker listed everyone's saved sessions — each labelled
with the tenants and usernames it came from, and resuming one handed over the
other person's full report payloads. `KEEP_SESSIONS` pruned by counting files
in one directory, so an active consultant silently deleted a colleague's saved
work. Reference maps keyed only by destination tenant replaced rather than
merged, so two people migrating into the same tenant overwrote each other.

None of it could cause an unreviewed write — approvals are never persisted, and
a reference map still has to be loaded by hand and is shown as an editable
table before anything uses it. It is a confidentiality and clobbering problem,
which is enough.

**What changed.** Each browser session gets its own scratch area under those
directories (`wdmigrator/ui/workspace.py`). A boundary test in
`tests/test_ui_boundaries.py` rejects any call site that passes one of those
constants without `workspace.user_dir()` around it.

**The identifier is in the URL**, as `?w=…`. It has to survive a browser reload
or the scoping would break the resume feature it exists to protect — a fresh
random id per run means nobody can ever see their own saved session again.
`st.session_state` dies with the websocket and Streamlit cannot set a cookie
without a custom component, so the query string is what is left.

The consequence, which the UI states plainly where someone saves a session: the
link is the key to that saved work. Send someone your URL and they land in your
workspace. The id is sanitised on the way in — it arrives as user input and
becomes a directory name.

**The index cache is deliberately still shared.** It is keyed by tenant, holds
nothing personal, is written atomically, and a report sweep costs two and a
half minutes that two consultants on the same tenant should not each pay for.
There is a test asserting it stays that way.

---

## 3. Durability: the filesystem is not storage

**The problem.** Server-side session saves survive a browser reload. They do
not survive the container restarting, and `out/` goes with it — so the resume
feature evaporates exactly when somebody comes back the next morning to finish.

**What changed.** Select offers a **download**, Connect accepts the file back.
The two saves are labelled by how they fail rather than by how they work: the
server-side one is faster, the downloaded one is the one that keeps.

Uploaded files get the same schema check as files read off disk, and the same
rule about what is restored — inputs, never approvals. Hand-editing
`dry_run_reviewed` into the JSON does not survive the round trip, and a test
pins that.

Note that the **index cache is also wiped** on restart. That is survivable
(sweeps re-run) but it means the first person in after a redeploy pays the 2.5
minute report sweep again.

---

## 4. Rate limiting: Workday counts the tenant, not the caller

**The problem.** Workday rate-limits at roughly 10 calls/sec per tenant, and
the limiter was scoped to a `Connection`. That enforces the budget only while
exactly one person is working. Hosted, each consultant got their own 8
calls/sec, so two on the same tenant were already over — and a 429 lands on
whichever request arrives next, which may be somebody else's write rather than
the sweep that caused it.

**What changed.** Limiters come from a process-wide registry keyed by host and
tenant (`wdmigrator/ratelimit.py`), so every session pointed at a tenant shares
one paced stream. A later caller cannot widen an existing budget.

**Expect this to be visible.** Two people sweeping the same tenant will each
see it take longer. That is Workday's constraint surfacing honestly instead of
both of them being throttled.

---

## Before you deploy

- **Restrict who can view the app** if the platform allows it. Nothing here
  authenticates anyone: the wizard is a client, and the only credential is the
  ISU each consultant types in. But the app reveals tenant names and object
  inventories to whoever can open it and supply credentials, and the free tier
  is public by default.
- **Do not put tenant credentials in the deployment's secrets.** Quick fill
  reads `.env` per side and simply does not render when there is none, which is
  the right behaviour for a shared app: each consultant supplies their own ISU.
  A destination credential baked into a shared app is a write target anybody
  with the URL could aim at.
- **Check `showErrorDetails = "none"` is still set** in `.streamlit/config.toml`.
  A zeep fault can carry the request envelope, and the envelope can carry a
  WS-Security password in cleartext. The app has its own redacting handler on
  top of this, not instead of it.
- **`requirements.txt` installs `-e .[dev]`**, which pulls pytest and friends
  onto the container. Harmless but wasteful; narrow it to `-e .` if install
  time or image size becomes a problem.

## What is still not handled

- **No cleanup of stale workspace directories.** On a hosted container the
  restart does it. Running locally for a long time, `out/sessions/<id>/` will
  accumulate one folder per browser session. They are small and gitignored.
- **Concurrency is bounded by CPU, not just memory.** The wizard drains its
  generators in time-budgeted chunks on the script thread, so several people
  sweeping at once share two cores. Nothing breaks; everything gets slower.
- **No cross-session coordination of writes.** Two consultants migrating the
  same objects into the same destination at the same time would both plan
  CREATE and both write. The tool has never guarded against this and still
  does not — the destination's own uniqueness rules are what catch it.
