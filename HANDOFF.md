# Handoff — Dale Bot / Workday Migration Tool

_Last updated: 2026-07-29_

## What this project is
A Python tool that migrates configuration (calculated fields + custom report
definitions) from a SOURCE Workday tenant to a DESTINATION tenant via the
**Report_Metadata** SOAP web service. Authoritative context lives in:

- `CLAUDE.md` (root) — project rules and role.
- `workday-migrator/CLAUDE.md` — detailed domain knowledge + hard rules.
- `workday-migrator/START_HERE.md` — the 6-step build plan (auth → discovery →
  ordering → writer → validation → cli). **Read this first.**
- `workday-migrator/WSDL_NOTES.md` — full WSDL breakdown (operations, field
  lists, architectural gotchas).

## Two folders — how they relate
- `workday-migrator/` — the real tool. Scaffold only; all module dirs
  (`auth/`, `discovery/`, `migrate/`, `validation/`, `tests/`) are still EMPTY.
  Build order is in START_HERE.md.
- `workday_client/` — a standalone working prototype + assets:
  - `report_metadata_wsdl.xml` — local copy of the tenant WSDL
    (Report_MetadataService, v47.0). Point `zeep` at this to build a client
    OFFLINE (no tenant round-trip needed just to construct the client).
  - `get_calculated_field.py` — verified prototype of the `Get_Calculated_Fields`
    read call. This is effectively an early version of what
    `discovery/inventory.py` will formalize.

## Done this session (2026-07-29)
Rewrote `workday_client/get_calculated_field.py` to match the WSDL exactly:
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
Build the WRITE side: `Put_Calculated_Field`
(shape: optional `Calculated_Field_Reference` + required `Calculated_Field_Data`).
Per the hard rules it MUST default to dry-run and never write to the destination
without `dry_run=false` AND explicit user confirmation. In the migrator proper
this lands as `migrate/writer.py` (START_HERE Step 4); a standalone prototype
could sit next to `get_calculated_field.py`.

## Reminders that bite (from START_HERE.md)
- Services host `impl-services1.wd12.myworkday.com` ≠ UI host `impl.wd12.myworkday.com`.
- Version goes in the URL path (`.../Report_Metadata/v47.0`).
- ISU username format is `username@tenant`.
- Cross-tenant references use the stable `Calculated_Field_Reference_ID`, not the
  tenant-specific WID.
- PUT loop is sequential (each response WID can feed the next payload).
