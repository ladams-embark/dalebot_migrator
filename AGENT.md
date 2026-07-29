# AGENT.md — Coding Agent Brief: Workday Tenant Config Migration Tool

You are a senior integration engineer building a Python tool that migrates
configuration (calculated fields and custom report definitions) from a SOURCE
Workday tenant to a DESTINATION tenant via the **Report_Metadata** SOAP web
service. Prioritize correctness, reversibility, and not breaking the destination
tenant over speed or cleverness.

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
   (`workday_client/report_metadata_wsdl.xml`) or official Workday docs. If you
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

Root: `Dale Bot/`

- `AGENT.md` — this file.
- `HANDOFF.md` — session-by-session status log. **Read first each session**;
  update it at the end of a session.
- `CLAUDE.md` (root) — original project charter.
- `workday-migrator/` — the real tool (scaffold only so far):
  - `CLAUDE.md` — detailed domain knowledge + hard rules.
  - `START_HERE.md` — the authoritative 6-step build plan. Follow this order.
  - `WSDL_NOTES.md` — full WSDL breakdown: operations, field lists, gotchas.
  - `.env.example` — copy to `.env` and fill in; canonical env var names.
  - `requirements.txt` — dependencies.
  - `auth/  discovery/  migrate/  validation/  tests/` — **currently EMPTY.**
- `workday_client/` — working prototype + assets:
  - `report_metadata_wsdl.xml` — local copy of the tenant WSDL (v47.0). Point
    `zeep` at this to build a client **offline**.
  - `get_calculated_field.py` — verified `Get_Calculated_Fields` read prototype;
    the model for `discovery/inventory.py`.

---

## Build order (from `workday-migrator/START_HERE.md`)

1. `auth/client.py` — build + verify a zeep client. **Start here.**
2. `discovery/inventory.py` — `get_all_calculated_fields`, `get_all_report_definitions` (paginated).
3. `migrate/ordering.py` — dependency DAG + topological sort + WID substitution (pure logic, no tenant calls).
4. `migrate/writer.py` — `put_calculated_field`, `put_report_definition` (dry-run by default).
5. `validation/verify.py` — confirm destination matches source.
6. `cli.py` — `discover` / `migrate --dry-run` / `migrate` / `verify` (use `click`).

Write a test (or a dry-run against recorded/mock responses) alongside every module
that talks to a tenant, so the destination is never hit during normal testing.

---

## Environment & setup

Set these in `.env` (see `workday-migrator/.env.example` for exact names):
`WD_SOURCE_SERVICES_HOST`, `WD_SOURCE_TENANT`, `WD_SOURCE_ISU_USERNAME`,
`WD_SOURCE_ISU_PASSWORD`, the `WD_DEST_*` equivalents, `WD_WWS_VERSION` (v47.0),
`WD_OX_SERVICE_NAME` (Report_Metadata), and `DRY_RUN` (default true).

```bash
cd workday-migrator
pip install -r requirements.txt          # zeep, requests, click, pytest, python-dotenv
cp .env.example .env                      # then fill in real credentials
pytest                                    # tests that need no tenant must pass offline
```

Stack: Python + `zeep` (SOAP) + `requests` (HTTP Basic auth with the ISU) +
`.env` for config + `pytest`. A thin CLI first; add a UI only if asked.

---

## Working with the Report_Metadata SOAP service

- Build the zeep client from the **local WSDL** (`workday_client/report_metadata_wsdl.xml`)
  so construction needs no tenant round-trip. The WSDL embeds the service address,
  so real operation calls still go to the tenant over HTTPS. Allow a `WD_WSDL_PATH`
  override pointing at a live `...?wsdl` URL.
- Service is `Report_Metadata`, version `v47.0` (fixed in schema). Endpoint pattern:
  `https://{services_host}/ccx/service/{tenant}/Report_Metadata/{version}`.
- Read ops are `Get_*` (plural for lists); write ops are `Put_*` (singular).
- `Get_Calculated_Fields` request: `Request_References` and `Request_Criteria` are
  an XSD **choice** — send at most one, never both. Always include `Response_Group`
  with `Include_Calculated_Field_Data: true` or you get references without data.
- Field IDs use type `WID` or `Calculated_Field_ID`. For cross-tenant references
  use the stable `Calculated_Field_Reference_ID`, **not** the tenant-specific WID.

### The WSDL file is large
`report_metadata_wsdl.xml` is ~767 KB minified onto 4 lines. Do not Read it whole —
`grep` it, or slice by character range with Python, to extract specific type
definitions.

---

## Gotchas that will bite you

- **Services host ≠ UI host**: SOAP goes to `impl-services1.wd12.myworkday.com`,
  not `impl.wd12.myworkday.com`.
- **Version in the URL path**: `.../Report_Metadata/v47.0`, not bare `Report_Metadata`.
- **ISU username format**: `username@tenant`, not just `username`.
- **Activate Pending Security Policy Changes** in the Workday UI after ISU
  permission changes, or the ISU silently returns empty data.
- **PUT loop is sequential**: each write's response WID can feed the next payload —
  don't parallelize writes.
- **Remap only custom WIDs**: global/delivered WIDs pass through unchanged.

---

## Current status (2026-07-29)

Read side (`Get_Calculated_Fields`) prototype is built and verified offline. Next
step is the write side, `Put_Calculated_Field` (shape: optional
`Calculated_Field_Reference` + required `Calculated_Field_Data`), dry-run first.
See `HANDOFF.md` for the latest detail and update it when you finish a session.
