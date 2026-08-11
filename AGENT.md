# AGENT.md — Coding Agent Brief: Workday Tenant Config Migration Tool

You are a senior integration engineer building a Python tool that migrates
configuration (calculated fields, calculated measures, custom report
definitions, custom dashboards and their prompt sets) from a SOURCE
Workday tenant to a DESTINATION tenant via the **Core_Implementation_Service**
SOAP web service (`Report_Metadata` exposes the same operations but is
rejected live on this tenant — see `docs/WSDL_NOTES.md`). Prioritize
correctness, reversibility, and not breaking the destination tenant over
speed or cleverness.

This file is your operating manual. Deeper reference lives in the docs listed
under "Project map" — read them before writing code in a given area.

---

## Golden rules (never violate)

1. **No secrets in code.** Never hardcode credentials, tenant URLs, ISU
   passwords, OAuth secrets, or tokens. Everything lives in `.env` (gitignored).
   If a credential appears anywhere in a file or a message, strip it and tell the
   user to rotate it and set it as an env var.
2. **Destination is destructive.** Every `Put_*` (write) is potentially
   destructive. `DRY_RUN=true` is the default. Never write to the destination
   tenant without `dry_run=false` AND explicit user confirmation in the same turn.
3. **Impl/Sandbox only.** Only test against Implementation or Sandbox tenants.
   Warn loudly before anything could touch Production.
4. **Verify, don't invent.** Never invent Workday endpoints, operation names,
   payload shapes, or field names. Confirm against the local WSDL
   (`src/wdmigrator/assets/core_implementation_service_wsdl.xml`) or official Workday docs. If you
   can't confirm a capability, say so and propose how to verify it — don't guess.
5. **Stay in scope.** Only make changes directly requested. No unrequested
   features, abstractions, or refactors. Never modify files outside the project
   directory.
6. **Announce before you act.** Before building a feature, state which files you
   will touch, what you'll change, and the stop condition. Wait for confirmation
   before anything that writes to a tenant or adds a dependency.
7. **Report after each step.** End each step with `✅ [what was completed]` and
   the next decision point.

---

## Project map

Root: `C:\dev\dalebot_migrator` (deliberately outside OneDrive — see README.md).

```
CLAUDE.md              Detailed domain knowledge + hard rules. The deep reference.
AGENT.md               This file — the operating manual.
HANDOFF.md             Session-by-session status log. Read first, update last.
README.md              Setup + the run commands for the dev loop.
pyproject.toml         Deps, package config, pytest config (single source of truth).
requirements.txt       Thin shim: `-e .[dev]`.
.env.example           Copy to `.env` and fill in. Canonical env var names.

docs/
  START_HERE.md        The authoritative 6-step build plan. Follow this order.
  WSDL_NOTES.md        Full WSDL breakdown: operations, field lists, gotchas.
  PROJECT_CHARTER.md   Original charter (historical; superseded by CLAUDE.md).

src/wdmigrator/        The installed package (`import wdmigrator`).
  __init__.py          Exposes DEFAULT_WSDL_PATH — always use this for the WSDL.
  assets/
    core_implementation_service_wsdl.xml   Local tenant WSDL (v47.0). Build zeep clients OFFLINE.
  auth/                Step 1 — client.py. NOT YET IMPLEMENTED.
  discovery/           Step 2 — inventory.py. NOT YET IMPLEMENTED.
  migrate/             Steps 3-4 — ordering.py, writer.py. NOT YET IMPLEMENTED.
  validation/          Step 5 — verify.py. NOT YET IMPLEMENTED.
  cli.py               Step 6. NOT YET CREATED.

scripts/
  selfcheck.py         Offline env verification. Run this first, always.
  get_calculated_field.py   Verified Get_Calculated_Fields read prototype;
                            the model for discovery/inventory.py.

tests/
  conftest.py          Fixtures: `offline_client` (no creds, no network), `wsdl_path`.
  test_wsdl_contract.py     Offline guards on documented WSDL facts.
  fixtures/            Recorded/mock API responses for offline tests.
```

The four subpackages contain only `__init__.py` docstrings so far. They exist as
real packages (not bare empty dirs) so git tracks them and imports resolve.

---

## Build order (from `docs/START_HERE.md`)

Modules live under `src/wdmigrator/`, so `auth/client.py` below means
`src/wdmigrator/auth/client.py` and is imported as `wdmigrator.auth.client`.

1. `auth/client.py` — build + verify a zeep client. **Start here.**
2. `discovery/inventory.py` — `get_all_calculated_fields`, `get_all_report_definitions` (paginated).
3. `migrate/ordering.py` — dependency DAG + topological sort + WID substitution (pure logic, no tenant calls).
4. `migrate/writer.py` — `put_calculated_field`, `put_report_definition` (dry-run by default).
5. `validation/verify.py` — confirm destination matches source.
6. `cli.py` — `discover` / `migrate --dry-run` / `migrate` / `verify` (use `click`).

Write a test (or a dry-run against recorded/mock responses) alongside every module
that talks to a tenant, so the destination is never hit during normal testing.

**Test marker discipline** — this is how the destination stays safe:
- Offline tests (pure logic, fixtures, schema shape): no marker. `pytest` runs these.
- Anything needing a real tenant: `@pytest.mark.live`. Deselected by default.
- Anything touching the destination: `@pytest.mark.live` **and** `@pytest.mark.dest`,
  and `dry_run=True` always.

---

## Environment & setup

Set these in `.env` (see `.env.example` for exact names):
`WD_SOURCE_SERVICES_HOST`, `WD_SOURCE_TENANT`, `WD_SOURCE_ISU_USERNAME`,
`WD_SOURCE_ISU_PASSWORD`, the `WD_DEST_*` equivalents, `WD_WWS_VERSION` (v47.0),
`WD_OX_SERVICE_NAME` (Core_Implementation_Service), and `DRY_RUN` (default true).

A `.venv` already exists at the project root. Activate it, then:

```powershell
.\.venv\Scripts\Activate.ps1
python scripts/selfcheck.py    # offline: proves env is wired up. Run this first.
pytest                         # offline suite only — no .env, no network needed
pytest -m live                 # opt in to real source-tenant calls (needs .env)
```

`pytest` with no args NEVER touches a tenant — `addopts = -m 'not live'` in
pyproject.toml enforces it. Keep it that way.

Stack: Python 3.12 + `zeep` 4.3.3 (SOAP) + `requests` (HTTP Basic auth with the
ISU) + `.env` for config + `pytest`. A thin CLI first; add a UI only if asked.

---

## Working with the Core_Implementation_Service SOAP service

- Build the zeep client from the **local WSDL** so construction needs no tenant
  round-trip. Get its path from the package, never hardcode it:
  `from wdmigrator import DEFAULT_WSDL_PATH`. The WSDL embeds the service
  address, so real operation calls still go to the tenant over HTTPS. Allow a
  `WD_WSDL_PATH` override pointing at a live `...?wsdl` URL.
- Service is `Core_Implementation_Service`, version `v47.0` (confirmed max
  version this tenant supports — v48.0+ return HTTP 500). Endpoint pattern:
  `https://{services_host}/ccx/service/{tenant}/Core_Implementation_Service/{version}`.
  `Report_Metadata` defines the identical operations but is rejected live on
  this tenant with a `Client.validationError` fault for this ISU, regardless
  of domain security — confirmed 2026-07-30, see `docs/WSDL_NOTES.md`.
- Read ops are `Get_*` (plural for lists); write ops are `Put_*` (singular).
- `Get_Calculated_Fields` request: `Request_References` and `Request_Criteria` are
  an XSD **choice** — send at most one, never both. Always include `Response_Group`
  with `Include_Calculated_Field_Data: true` or you get references without data.
- Field IDs use type `WID` or `Calculated_Field_ID`. For cross-tenant references
  use the stable `Calculated_Field_Reference_ID`, **not** the tenant-specific WID.

### The WSDL file is large
`src/wdmigrator/assets/core_implementation_service_wsdl.xml` is ~5.9 MB.
Do not Read it whole — `grep` it, or slice by character range with Python, to
extract specific type definitions.

---

## Gotchas that will bite you

- **Services host ≠ UI host**: SOAP goes to `impl-services1.wd12.myworkday.com`,
  not `impl.wd12.myworkday.com`.
- **Version in the URL path**: `.../Core_Implementation_Service/v47.0`, not bare `Core_Implementation_Service`.
- **ISU username format**: `username@tenant`, not just `username`.
- **Activate Pending Security Policy Changes** in the Workday UI after ISU
  permission changes, or the ISU silently returns empty data.
- **PUT loop is sequential**: each write's response WID can feed the next payload —
  don't parallelize writes.
- **Remap only custom WIDs**: global/delivered WIDs pass through unchanged.

---

## Current status (2026-07-31)

Repo restructured into a single installable `wdmigrator` package with a working
offline dev loop (`scripts/selfcheck.py` + `pytest`, both green, neither touching
a tenant). Read side (`Get_Calculated_Fields`) prototype is built and verified
**live** against the source tenant via `Core_Implementation_Service` (not
`Report_Metadata` — see `docs/WSDL_NOTES.md` for why). Next step is the write
side, `Put_Calculated_Field` (shape: optional `Calculated_Field_Reference` +
required `Calculated_Field_Data`), dry-run first.
See `HANDOFF.md` for the latest detail and update it when you finish a session.
