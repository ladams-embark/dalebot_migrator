# dalebot_migrator

Migrates configuration (calculated fields, calculated measures, custom report
definitions, custom dashboards and their prompt sets) from a
SOURCE Workday tenant to a DESTINATION tenant via the
**Core_Implementation_Service** SOAP web service. (`Report_Metadata` exposes
the same operations but is rejected live on this tenant regardless of domain
security — see `docs/WSDL_NOTES.md`.)

> **Safety:** the destination tenant is a write target. `DRY_RUN=true` is the
> default everywhere. Implementation/Sandbox tenants only — never Production.

## Quick start

```powershell
.\.venv\Scripts\Activate.ps1
python scripts/selfcheck.py     # offline: proves the env is wired up correctly
pytest                          # offline test suite (~1.5s, no network)
```

Both should pass on a clean checkout with **no `.env` and no network access**.
If `.venv` is missing:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## The dev loop

| Command | What it does | Touches a tenant? |
|---|---|---|
| `python scripts/selfcheck.py` | Verifies package, deps, WSDL, offline client, prototype | No |
| `pytest` | Offline suite only — the fast inner loop | No |
| `pytest -k ordering` | Single module, fastest feedback | No |
| `pytest -m live` | Opt in to real SOURCE-tenant calls | Yes — read only |
| `python scripts/get_calculated_field.py` | Verified `Get_Calculated_Fields` read prototype | Yes — read only |

`pytest` with no arguments **never contacts a tenant**. That's enforced by
`addopts = -m 'not live'` in `pyproject.toml`, not by convention. Tests needing a
real tenant are marked `@pytest.mark.live` and auto-skip with a clear reason when
`.env` isn't populated.

## Configuration

```powershell
Copy-Item .env.example .env    # then fill in real credentials
```

`.env` is gitignored and must stay that way. Never hardcode credentials, tenant
URLs, ISU passwords, or tokens.

Two gotchas that cost time if missed:
- SOAP goes to the **services** host (`impl-services1.wd12.myworkday.com`), not
  the UI host (`impl.wd12.myworkday.com`).
- The ISU username is sent as `username@tenant`, but enter just the ISU
  name — `Credentials.ws_username` appends the tenant. An ISU username
  that is itself an email address works; so does an already-qualified
  `name@tenant`.

## Layout

```
src/wdmigrator/     The package. auth/ discovery/ migrate/ validation/
                    are real packages but the modules are NOT YET WRITTEN.
  assets/           Bundled tenant WSDL (v47.0) — build zeep clients offline.
scripts/            selfcheck.py + the verified read prototype.
tests/              conftest.py fixtures + the offline WSDL contract tests.
docs/               Build plan, WSDL notes, original charter.
```

Get the WSDL path from the package, never hardcode it:

```python
from wdmigrator import DEFAULT_WSDL_PATH
```

## Where to read next

| File | Purpose |
|---|---|
| `AGENT.md` | **Start here.** Operating manual: rules, project map, build order, gotchas. |
| `HANDOFF.md` | Session-by-session status log. What's done, what's next. |
| `CLAUDE.md` | Deep reference: Workday domain knowledge, module interfaces, zeep patterns, the WID remapping algorithm. |
| `docs/START_HERE.md` | The authoritative 6-step build plan, in order. |
| `docs/WSDL_NOTES.md` | WSDL breakdown: operations, field lists, architectural notes. |
| `docs/PROJECT_CHARTER.md` | Original charter. Historical — superseded by `CLAUDE.md`. |

## Why this lives in `C:\dev`

Not in OneDrive, deliberately. OneDrive syncs `.venv/` and `__pycache__/`, which
causes file-lock and sync-conflict errors mid-run, and the old path contained two
space-separated segments that broke tooling. Keep this project on a local,
non-synced path. Git is the backup — the remote is
[`ladams-embark/dalebot_migrator`](https://github.com/ladams-embark/dalebot_migrator).
