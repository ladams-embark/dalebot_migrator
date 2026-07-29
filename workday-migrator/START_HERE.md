# Start Here — Workday Migration Tool

Everything you need to know is in `CLAUDE.md`. This file is the immediate action plan.

---

## Before you write any code

1. **Set up `.env`** — copy `.env.example` to `.env` and fill in real credentials
2. **Install dependencies** — `pip install -r requirements.txt`
3. **Verify the WSDL is reachable**:
   ```bash
   curl -s "https://impl-services1.wd12.myworkday.com/ccx/service/commitconsulting_dpt1/Report_Metadata/v47.0?wsdl" | head -5
   ```
   Should return XML starting with `<wsdl:definitions`. If not, check services host in `.env`.

---

## Build order

### Step 1 — `auth/client.py`
Build and test first, before touching any other module.

Functions to implement:
- `make_client(services_host, tenant, isu_username, isu_password, service, version) → zeep.Client`
- `verify_connection(client) → bool`

Test: `pytest tests/test_auth.py` — must pass against live source tenant before moving on.

See `CLAUDE.md` for the exact zeep initialization pattern.

---

### Step 2 — `discovery/inventory.py`
Depends on: `auth/client.py`

Functions to implement:
- `get_all_calculated_fields(client) → list[dict]`
- `get_all_report_definitions(client) → list[dict]`

Uses pagination (Page/Count). Always set `Include_Calculated_Field_Data=True`.
See `CLAUDE.md` for the pagination pattern.

Test: `pytest tests/test_discovery.py` — asserts non-empty list from source tenant.

---

### Step 3 — `migrate/ordering.py`
Depends on: nothing (pure logic, no tenant calls)

Functions to implement:
- `extract_custom_wid_refs(field_data, custom_wids) → set[str]`
- `build_dag(fields, custom_wids) → dict`
- `topological_sort(dag, fields) → list[dict]`
- `substitute_wids(obj, wid_map) → dict`

Test: `pytest tests/test_ordering.py` — pure unit tests with fixture data. No `.env` needed.

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

Commands:
```
python cli.py discover          # list all fields + reports in source tenant
python cli.py migrate --dry-run # show what would be migrated (default)
python cli.py migrate           # real migration (prompts for confirmation)
python cli.py verify            # check destination matches source
```

Use `click` for CLI. All commands read from `.env`. `migrate` command defaults to dry-run
and requires `--no-dry-run` flag plus interactive confirmation before real writes.

---

## Key things that will bite you if you forget

1. **Services host ≠ UI host** — SOAP calls go to `impl-services1.wd12.myworkday.com`, NOT `impl.wd12.myworkday.com`
2. **Version must be in the URL path** — `https://.../Report_Metadata/v47.0` not just `Report_Metadata`
3. **ISU username format** — `username@tenant`, not just `username`
4. **Activate Pending Security Policy Changes** — must be run in Workday UI after ISU permission changes or the ISU silently returns empty data
5. **PUT loop is sequential** — cannot parallelize because each PUT's response WID feeds the next field's payload
6. **Global WIDs pass through unchanged** — only remap WIDs that appear in `custom_source_wids`
7. **Always dry_run=True by default** — never flip this without user confirmation
