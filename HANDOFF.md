# Handoff — Dale Bot / Workday Migration Tool

_Last updated: 2026-07-31 (session 3)_

## What this project is
A Python tool that migrates configuration (calculated fields + custom report
definitions) from a SOURCE Workday tenant to a DESTINATION tenant via the
**Core_Implementation_Service** SOAP web service. (`Report_Metadata` exposes
the same operations but is rejected live on this tenant regardless of domain
security — see `docs/WSDL_NOTES.md`.) Authoritative context lives in:

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
- `src/wdmigrator/assets/core_implementation_service_wsdl.xml` — local copy of
  the tenant WSDL (Core_Implementation_Service, v47.0). Point `zeep` at this to
  build a client OFFLINE (no tenant round-trip needed just to construct the
  client). Get its path via `from wdmigrator import DEFAULT_WSDL_PATH` — never
  hardcode it.
- `scripts/get_calculated_field.py` — verified prototype of the
  `Get_Calculated_Fields` read call. Effectively an early version of what
  `discovery/inventory.py` will formalize.
- `scripts/selfcheck.py` — offline environment verification.

## PICK UP HERE — next session

**Branch:** `core-implementation-service-migration`, pushed through commit
`cde04c3`. **Not merged to master, and no PR exists** — `gh` CLI is not
installed on this machine, so the PR has to be opened in the browser:
`https://github.com/ladams-embark/dalebot_migrator/compare/master...core-implementation-service-migration`

**State:** engine steps 1–7 of 10 are built and green. **Nothing has ever been
written to a tenant.** All live testing has been read-only, and
`tests/test_writer.py` is offline-only by design — it has a test asserting no
test in it carries a `live`/`dest` marker, because a test that writes leaves
permanent residue in a tenant with no delete operation.

```powershell
.\.venv\Scripts\Activate.ps1
pytest              # 206 offline, ~12s, no .env or network needed
pytest -m live      # 9 read-only source-tenant tests, ~50s
python scripts/selfcheck.py
```

**Next step: `api.py` (build order step 8)** — the generator-based facade that
the UI imports, and the *only* engine module it may import. Then step 9 (`ui/`
Streamlit wizard) and step 10 (`validation/verify.py`, `cli.py`).

Before building the UI, re-read the Streamlit section of the plan file: the
chunked-runner pattern, the "no network at render time" rule, and the ban on
putting credentials or clients in `st.cache_data`/`st.cache_resource` are the
parts that are easy to get wrong and expensive to retrofit.

`writer.py` is built but **has never executed against a tenant**. Its first
real run must be: a distinct sandbox destination, dry run first, a handful of
objects — not a full migration.

### Blockers and open questions

1. **No real destination tenant.** `.env` has `WD_DEST_*` pointing at the same
   tenant as the source (`commitconsulting_dpt1`, same host, same ISU
   `lmcneil`). `safety.py` blocks live runs in that configuration by design and
   with no override. Dry runs work fine, so everything except actual writes can
   be developed and tested. **A distinct impl/sandbox tenant is required before
   any live migration.**
2. **`Put_Calculated_Field` with a reference: replace or merge?** Unverified.
   Until tested, treat UPDATE as unsafe and prefer CREATE/SKIP.
3. **Can `Data_Source_Reference` existence be probed in the destination?** If
   not, it becomes a manual pre-flight checklist item on the confirm step.
4. **Can the destination ISU own a report?** Likely yes (`WorkdayUserName`
   accepts a plain string), unverified.

### Local machine state (not in git)

`out/cache/commitconsulting_dpt1/calculated_field.json` — 42 MB, all 9,652
calculated fields. Gitignored. Rebuilding costs ~36s; `load_index()` reads it
instantly. Delete it if the source tenant's fields change.

### The approved build plan

`C:\Users\LucasAdams\.claude\plans\knowing-what-we-know-swirling-treasure.md`
holds the full architecture, the Streamlit design, and the safety model.

---

## Done this session (2026-07-31, session 3) — engine steps 1-7

Built the migration engine bottom-up, safety first. Commits `d68e230`
(steps 1-5) and `cde04c3` (step 6).

**Modules built** — all under `src/wdmigrator/`:
- `config/targets.py` — `TenantTarget`, `parse_tenant_url()` for pasted browser
  URLs, host-driven environment classification.
- `safety.py` — `WriteGuard` / `assert_write_allowed()`.
- `secrets.py` — `Secret` wrapper + redaction.
- `ratelimit.py` — 8 calls/sec limiter.
- `auth/client.py` — `make_client()` / `verify_connection()`.
- `discovery/inventory.py` — index sweeps, three-valued lookups, disk cache.
- `migrate/ordering.py` — Kahn topological sort, `substitute_wids()`.
- `migrate/resolver.py` — dependency closure. Pure, no tenant calls.
- `migrate/planner.py` — destination probing, CREATE/UPDATE/SKIP.
- `migrate/writer.py` — the only module that mutates. Dry run serializes the
  real envelope through zeep's binding without sending it (so schema errors
  surface cheaply); live writes re-check the guard per object, inspect
  `Exceptions_Response_Data`, and refuse to run through a non-DESTINATION
  connection. Failure or indeterminate halts the run, and every remaining
  object is still reported rather than silently dropped.

**The writer's safety tests were mutation-tested, not just run.** Removing the
per-object guard failed 3 tests; ignoring `Exceptions_Response_Data` failed 2.
Both mutations were reverted and verified. Worth repeating if that logic is
ever refactored — a green suite that cannot fail is worse than no suite.

**Two bugs found by testing that unit tests alone would have missed:**

1. **Clients inherited the WSDL's embedded endpoint.** A WSDL carries the
   address of the tenant it was fetched from, and the bundled one names the
   *source*. A destination client built from it would have sent **writes to the
   source tenant**. `make_client` now rebinds via `create_service` and refuses
   to return an unpinned client.
2. **Reports always probed as NOT_FOUND, even when present** — every migration
   would have duplicated every report. `Custom_Report_ID` is returned on every
   report reference but rejected as a lookup key (18/18 sampled). Reports are
   now matched by exact name. Caught only by running the full flow end-to-end
   against the live tenant, with source == destination so everything *should*
   have been detected as existing.

**Measurements that drove the design** (source tenant, live):
- `Count=999` works → CF index is 10 pages / ~20s / ~34 MB. Report index is
  6 pages / ~160s.
- 9,652 calculated fields, 5,153 reports.
- **~95 WID references per report.** Probing each to classify it would cost
  ~12s per report; with the complete index it is a free set lookup. This is why
  `resolve_closure()` needs no network at all.
- 48% of reports reference calculated fields (avg 3.9, max 18).
- **Only 6 nested calculated-field edges exist in the whole tenant.** Live data
  barely exercises the DAG, so `tests/fixtures/nested_calculated_fields.json`
  holds an anonymised capture — real structure and nesting, synthetic
  identifiers. A real client tenant will stress the ordering far harder than
  this one can.

**Also corrected `CLAUDE.md`:** the documented custom-WID algorithm was wrong
and had never been live-tested. It treated every source field as custom, but
`Get_Calculated_Fields` returns mostly Workday-delivered fields with no payload
discriminator — it would have tried to migrate ~9,600 delivered fields.
Replaced with destination existence probing.

## Done earlier (2026-07-31, session 2) — live tenant testing, service switch

First live tenant calls, using credentials the user supplied for a demo
Implementation tenant (`commitconsulting_dpt1`). Found and fixed several
issues before landing on a working configuration:

- **Auth bug**: the read prototype was using plain HTTP Basic Auth instead of
  the documented WS-Security `UsernameToken` (`isu_user@tenant`). Fixed in
  `scripts/get_calculated_field.py`.
- **Stray credential file**: the user's `.env` was created as `auth.env`,
  which is NOT covered by `.gitignore`'s `.env`/`.env.*` patterns — a real risk
  of committing credentials. Renamed to `.env`; confirmed it's now ignored.
- **`Report_Metadata` doesn't work on this tenant**: every call (`Get_Calculated_Fields`,
  `Get_Tenanted_Report_Definitions`) failed with `SOAP-ENV:Client.validationError`
  — "The web service or version is invalid for the requested operation" — even
  after confirming the account is a proper ISU with both **Special OX Web
  Services** and **Custom Reports and Fields** domain access granted and
  activated. Isolated via elimination: the same ISU/tenant/version succeeded
  calling `Staffing.Get_Workers` (ruling out auth/OAuth/IP/tenant-version
  issues), and succeeded calling the *identical* `Get_Calculated_Fields`
  operation via **`Core_Implementation_Service`** instead — which also exposes
  `Put_Calculated_Field`, `Get_Tenanted_Report_Definitions`,
  `Put_Tenanted_Report_Definition`, and `Put_Tenanted_Report_Definition_Base`.
- **Switched the whole project** to `Core_Implementation_Service`: `CLAUDE.md`,
  `docs/WSDL_NOTES.md`, `AGENT.md`, `README.md`, `pyproject.toml`, the
  DaleBotHelper agent description, `scripts/get_calculated_field.py`,
  `scripts/selfcheck.py`, `tests/conftest.py`, `tests/test_wsdl_contract.py`,
  and `src/wdmigrator/__init__.py`. Fetched and bundled a fresh live WSDL as
  the new offline asset (`src/wdmigrator/assets/core_implementation_service_wsdl.xml`,
  replacing `report_metadata_wsdl.xml` as `DEFAULT_WSDL_PATH`).
- Read side (`Get_Calculated_Fields`) is now **verified live**: successfully
  pulled all 9,652 calculated fields from the source tenant.
- Write side (`Put_Calculated_Field`) has NOT been tested — per the hard
  rules, that needs explicit user confirmation and dry-run first.

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

Read side is now verified live against the source tenant via
`Core_Implementation_Service` (see above). `.env` is populated in this
workspace with source-tenant credentials only — destination tenant vars are
still blank.

## Reminders that bite (from docs/START_HERE.md)
- Services host `impl-services1.wd12.myworkday.com` ≠ UI host `impl.wd12.myworkday.com`.
- Version goes in the URL path (`.../Core_Implementation_Service/v47.0`).
- Use `Core_Implementation_Service`, not `Report_Metadata` — the latter is
  rejected live on this tenant regardless of domain security (see
  `docs/WSDL_NOTES.md`).
- ISU username format is `username@tenant`.
- Cross-tenant references use the stable `Calculated_Field_Reference_ID`, not the
  tenant-specific WID.
- PUT loop is sequential (each response WID can feed the next payload).
