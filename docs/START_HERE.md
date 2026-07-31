# Start Here — Workday Migration Tool

Everything you need to know is in `../CLAUDE.md`. This file is the immediate action plan.

All module paths below are relative to `src/wdmigrator/` — so `auth/client.py`
means `src/wdmigrator/auth/client.py`, imported as `wdmigrator.auth.client`.

---

## Before you write any code

1. **Activate the venv** — it already exists at the project root:
   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```
   If it's missing: `python -m venv .venv` then `pip install -r requirements.txt`.

2. **Verify the environment offline** — no credentials or network needed:
   ```powershell
   python scripts/selfcheck.py
   pytest
   ```
   Both should pass. This confirms the package imports, the bundled WSDL loads,
   a zeep client builds offline, and all four SOAP operations resolve.

3. **Set up `.env`** — copy `.env.example` to `.env` and fill in real credentials.
   Only needed once you're ready for live tenant calls. `.env` is gitignored;
   never commit it.

4. **Verify the live WSDL is reachable** (only when moving to live calls):
   ```bash
   curl -s "https://impl-services1.wd12.myworkday.com/ccx/service/commitconsulting_dpt1/Core_Implementation_Service/v47.0?wsdl" | head -5
   ```
   Should return XML starting with `<wsdl:definitions`. If not, check services host in `.env`.

   Note: you do **not** need this to develop. The WSDL is bundled at
   `src/wdmigrator/assets/core_implementation_service_wsdl.xml` and reachable via
   `from wdmigrator import DEFAULT_WSDL_PATH`.

   Also note: `Report_Metadata` exposes the identical operations but is
   rejected live on this tenant regardless of domain security — use
   `Core_Implementation_Service`. See `docs/WSDL_NOTES.md` for the full story.

---

## Build order

### Step 1 — `auth/client.py`
Build and test first, before touching any other module.

Functions to implement:
- `make_client(services_host, tenant, isu_username, isu_password, service, version) → zeep.Client`
- `verify_connection(client) → bool`

Test: `pytest -m live tests/test_auth.py` — mark these `@pytest.mark.live`; they
must pass against the live source tenant before moving on.

See `CLAUDE.md` for the exact zeep initialization pattern.

---

### Step 2 — `discovery/inventory.py`
Depends on: `auth/client.py`

Functions to implement:
- `get_all_calculated_fields(client) → list[dict]`
- `get_all_report_definitions(client) → list[dict]`

Uses pagination (Page/Count). Always set `Include_Calculated_Field_Data=True`.
See `CLAUDE.md` for the pagination pattern.

Test: `pytest -m live tests/test_discovery.py` — mark `@pytest.mark.live`; asserts
non-empty list from source tenant. Save a sanitized response into
`tests/fixtures/` so step 3 can be tested offline.

---

### Step 3 — `migrate/ordering.py`
Depends on: nothing (pure logic, no tenant calls)

Functions to implement:
- `extract_custom_wid_refs(field_data, custom_wids) → set[str]`
- `build_dag(fields, custom_wids) → dict`
- `topological_sort(dag, fields) → list[dict]`
- `substitute_wids(obj, wid_map) → dict`

Test: `pytest tests/test_ordering.py` — pure unit tests with fixture data. No
marker, no `.env`, no network. This is the fast inner loop and it covers the
highest-risk logic in the project — invest in it.

Key insight: walk the serialized field dict recursively looking for any key named `WID`.
If that WID is in `custom_wids`, it's a dependency. Global/delivered WIDs won't be in `custom_wids`.

---

### Step 4 — `migrate/writer.py`
Depends on: `auth/client.py`, `migrate/ordering.py`

Functions to implement:
- `put_calculated_field(client, field_data, dry_run=True) → str | None`
- `put_report_definition(client, report_data, dest_isu_ref, dry_run=True) → str | None`

`DRY_RUN=true` is the default. Writer must never call the destination if `dry_run=True`.
In dry-run mode, log exactly what would be sent.

Test: `pytest tests/test_writer.py` — always dry_run=True, zero destination calls.
Use a mock transport so this needs no marker and stays in the offline suite.

---

### Step 5 — `validation/verify.py`
Depends on: `auth/client.py`

Functions to implement:
- `verify_fields_migrated(dest_client, source_fields) → dict[str, bool]`
- `verify_reports_migrated(dest_client, source_reports) → dict[str, bool]`

Fetches each object from destination by `Calculated_Field_Reference_ID`.
Returns pass/fail per object.

---

### Step 6 — `cli.py`
Depends on: all modules above

Lives at `src/wdmigrator/cli.py`. `pyproject.toml` already declares the console
script `wdmigrator = "wdmigrator.cli:cli"`, so it works as `wdmigrator ...` the
moment the file exists (the venv install is editable — no reinstall needed).

Commands:
```
wdmigrator discover          # list all fields + reports in source tenant
wdmigrator migrate --dry-run # show what would be migrated (default)
wdmigrator migrate           # real migration (prompts for confirmation)
wdmigrator verify            # check destination matches source

python -m wdmigrator.cli discover    # equivalent, no console script needed
```

Use `click` for CLI. All commands read from `.env`. `migrate` command defaults to dry-run
and requires `--no-dry-run` flag plus interactive confirmation before real writes.

---

## Key things that will bite you if you forget

1. **Services host ≠ UI host** — SOAP calls go to `impl-services1.wd12.myworkday.com`, NOT `impl.wd12.myworkday.com`
2. **Version must be in the URL path** — `https://.../Core_Implementation_Service/v47.0` not just `Core_Implementation_Service`
3. **ISU username format** — `username@tenant`, not just `username`
4. **Activate Pending Security Policy Changes** — must be run in Workday UI after ISU permission changes or the ISU silently returns empty data
5. **PUT loop is sequential** — cannot parallelize because each PUT's response WID feeds the next field's payload
6. **Global WIDs pass through unchanged** — only remap WIDs that appear in `custom_source_wids`
7. **Always dry_run=True by default** — never flip this without user confirmation
