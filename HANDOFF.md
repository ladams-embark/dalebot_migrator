# Handoff — Dale Bot / Workday Migration Tool

_Last updated: 2026-07-30_

## What this project is
A Python tool that migrates configuration (calculated fields + custom report
definitions) from a SOURCE Workday tenant to a DESTINATION tenant via the
**Report_Metadata** SOAP web service. Authoritative context lives in:

- `AGENT.md` — operating manual: rules, project map, build order, setup. **Start here.**
- `CLAUDE.md` — detailed domain knowledge, module interfaces, zeep patterns.
- `docs/START_HERE.md` — the 6-step build plan (auth → discovery → ordering →
  writer → validation → cli).
- `docs/WSDL_NOTES.md` — full WSDL breakdown (operations, field lists,
  architectural gotchas).
- `README.md` — setup and the day-to-day run commands.

## Where the code lives
Everything is one installable package, `wdmigrator`, under `src/`:

- `src/wdmigrator/` — `auth/`, `discovery/`, `migrate/`, `validation/`. Each is a
  real package with an `__init__.py` docstring; **the modules themselves are not
  yet written.** Build order is in `docs/START_HERE.md`.
- `src/wdmigrator/assets/report_metadata_wsdl.xml` — local copy of the tenant
  WSDL (Report_MetadataService, v47.0). Point `zeep` at this to build a client
  OFFLINE (no tenant round-trip needed just to construct the client). Get its
  path via `from wdmigrator import DEFAULT_WSDL_PATH` — never hardcode it.
- `scripts/get_calculated_field.py` — verified prototype of the
  `Get_Calculated_Fields` read call. Effectively an early version of what
  `discovery/inventory.py` will formalize.
- `scripts/selfcheck.py` — offline environment verification.

## Done this session (2026-07-30) — repo restructure
Consolidated the old two-tree layout (`workday-migrator/` scaffold +
`workday_client/` prototype) into the single `src/wdmigrator/` package above, and
made the project actually runnable:

- **Moved off OneDrive** to `C:\dev\dalebot_migrator`. The old path had two
  space-containing segments and OneDrive was syncing `.venv`/`__pycache__`.
  All moves used `git mv`, so history is preserved.
- **Packaged it**: added `pyproject.toml` (src layout, editable install,
  `wdmigrator` console script reserved for step 6). `requirements.txt` is now a
  thin `-e .[dev]` shim. Deps have one source of truth.
- **Fixed the zeep pin**: was `==4.2.1`, but this file recorded the prototype as
  verified on **4.3.3**. Now `>=4.3.3`; installed and confirmed at 4.3.3.
- **Created `.venv`** with all deps installed (Python 3.12.6).
- **Made `pytest` safe and fast**: `addopts = -m 'not live'` in pyproject, so a
  bare `pytest` never contacts a tenant. Live tests opt in via
  `@pytest.mark.live` and auto-skip with a clear reason when `.env` is absent.
- **Added offline verification**: `scripts/selfcheck.py` and
  `tests/test_wsdl_contract.py` assert the documented WSDL facts (operation
  names, services host, versioned path). **Both green: 7 tests pass in ~1.5s,
  zero network calls.**
- **Deduplicated docs**: two competing `CLAUDE.md` files merged into one; the
  original charter preserved at `docs/PROJECT_CHARTER.md`. The 7 KB verbatim copy
  of AGENT.md in `.claude/agents/DaleBotHelper.agent.md` was replaced with a real
  agent definition (it had no frontmatter, so it was non-functional) that points
  at these docs instead of duplicating them.
- **Single root `.gitignore`** replacing the two per-subdirectory ones.
- The OneDrive copy at `Desktop\DaleBot\Dale Bot` was deleted after confirming
  everything was pushed to `origin/master`.

## Done previous session (2026-07-29)
Rewrote the read prototype (then at `workday_client/get_calculated_field.py`, now
`scripts/get_calculated_field.py`) to match the WSDL exactly:
- Service `Report_Metadata` (was wrongly guessed `Report_Builder`); version
  `v47.0` (was `v42.0`); operation `Get_Calculated_Fields` (plural).
- Fixed `Request_References` / `Request_Criteria` — they are an XSD **choice**,
  so at most one is sent (previously both were sent as siblings = invalid).
  `Request_Criteria` is an empty type and carries no filters.
- Added `Response_Group` with `Include_Calculated_Field_Data: true` — required
  to get the actual field definitions back.
- Loads the local WSDL by default (`WD_WSDL_PATH` to override with a live
  `...?wsdl` URL); env vars aligned to the migrator's `WD_SOURCE_*` /
  `WD_WWS_VERSION` convention; `id_type` validated to `WID` | `Calculated_Field_ID`.

**Verified offline** with zeep 4.3.3: client builds from the local WSDL,
operation resolves, endpoint reads
`https://impl-services1.wd12.myworkday.com/ccx/service/commitconsulting_dpt1/Report_Metadata/v47.0`,
and the request serializes to valid SOAP. No tenant was contacted; no writes.

## Next step
Two open threads — the build plan says do step 1 first:

1. **`src/wdmigrator/auth/client.py`** (START_HERE step 1) — `make_client()` and
   `verify_connection()`. The structure is ready and empty, waiting for it.
2. **The WRITE side**: `Put_Calculated_Field` (shape: optional
   `Calculated_Field_Reference` + required `Calculated_Field_Data`). Per the hard
   rules it MUST default to dry-run and never write to the destination without
   `dry_run=false` AND explicit user confirmation. In the migrator proper this
   lands as `src/wdmigrator/migrate/writer.py` (START_HERE step 4).

Nothing has been verified against a live tenant yet — `.env` has never been
populated, so no real credentials exist in this workspace. Creating `.env` from
`.env.example` and running `pytest -m live` is the first thing that needs a human.

## Reminders that bite (from docs/START_HERE.md)
- Services host `impl-services1.wd12.myworkday.com` ≠ UI host `impl.wd12.myworkday.com`.
- Version goes in the URL path (`.../Report_Metadata/v47.0`).
- ISU username format is `username@tenant`.
- Cross-tenant references use the stable `Calculated_Field_Reference_ID`, not the
  tenant-specific WID.
- PUT loop is sequential (each response WID can feed the next payload).
