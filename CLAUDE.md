# Workday Tenant Configuration Migration Tool — CLAUDE.md

## Role
You are a senior integration engineer building a Python CLI tool that migrates calculated fields and custom report definitions from a SOURCE Workday tenant to a DESTINATION Workday tenant via SOAP web services. You have full file access and can run code. Prioritize correctness and reversibility over speed. Never invent Workday API behavior — everything here has been verified against a live tenant.

---

## Hard rules — never violate
- NEVER hardcode credentials, tenant URLs, ISU passwords, or tokens. All secrets live in `.env` (gitignored).
- If a credential appears anywhere, strip it and tell the user to rotate it and use `.env` instead.
- The DESTINATION tenant is a write target — treat every Put as potentially destructive.
- `DRY_RUN=true` is the default. Never write to a tenant without dry-run=false AND explicit user confirmation.
- Only test against Sandbox or Implementation tenants. Warn loudly before anything touches Production.
- Only make changes directly requested. No unrequested features, abstractions, or refactors.
- Before each coding session, state: which files you will touch, what you will change, and what the stop condition is. Wait for confirmation before writing to a tenant.

---

## Current state of the project
The package structure and dev environment are in place and verified offline
(`scripts/selfcheck.py` and `pytest` both green, neither contacting a tenant).
The migration modules themselves are still unwritten.

All module paths below are relative to `src/wdmigrator/`, so `auth/client.py`
means `src/wdmigrator/auth/client.py`, imported as `wdmigrator.auth.client`.

Build order:
1. `auth/client.py` ← **start here**
2. `discovery/inventory.py`
3. `migrate/ordering.py`
4. `migrate/writer.py`
5. `validation/verify.py`
6. `cli.py`
7. Tests in `tests/` alongside each module (see the Testing approach section)

---

## Workday domain knowledge

### The two hosts — critical, do not mix up
Every myworkday.com tenant has TWO separate hostnames:
- **UI host** (browser only): `impl.wd12.myworkday.com`
- **Services host** (all API calls): `impl-services1.wd12.myworkday.com`

SOAP calls, WSDL fetches, and REST calls all use the **services host**.
The `.env` stores `WD_SOURCE_SERVICES_HOST` and `WD_DEST_SERVICES_HOST` separately.

SOAP endpoint:
```
https://{services_host}/ccx/service/{tenant}/{service}/{version}
```

WSDL (verified working):
```
https://impl-services1.wd12.myworkday.com/ccx/service/commitconsulting_dpt1/Report_Metadata/v47.0?wsdl
```

### The Report_Metadata service
- **Service name**: `Report_Metadata` (confirmed from live tenant WSDL)
- **Security domain**: Special OX Web Services (System functional area)
- **NOT in the public WWS directory** — restricted service
- **Tenant version**: v47.0 — must be in the URL path, not just the SOAP envelope

Operations used:

| Operation | Direction |
|-----------|-----------|
| `Get_Calculated_Fields` | Read from source |
| `Put_Calculated_Field` | Write to destination |
| `Get_Tenanted_Report_Definitions` | Read from source |
| `Put_Tenanted_Report_Definition` | Write to destination |

### Authentication
SOAP WS-Security with ISU credentials. Username format: `{isu_username}@{tenant}`.

Each tenant needs its own ISU with **Special OX Web Services** domain granted:
- Source ISU: needs Get permission
- Destination ISU: needs Get + Put permission

After any ISU permission change in Workday: run "Activate Pending Security Policy Changes" — changes are NOT immediate and the ISU will silently return empty data until activated.

### Get_Calculated_Fields request structure
```
Get_Calculated_Fields_Request
  Request_References   (optional) — list of Calculated_Field WID references to fetch specific fields
  Request_Criteria     (optional) — currently empty type in WSDL, so omit or leave empty to fetch all
  Response_Filter      (optional) — Page (int), Count (int) for pagination
  Response_Group       (optional) — Include_Reference (bool), Include_Calculated_Field_Data (bool)
```
Always set `Include_Calculated_Field_Data=True` in Response_Group, otherwise you get stubs.

### Put_Calculated_Field request structure
```
Put_Calculated_Field_Request
  Calculated_Field_Reference   (optional) — omit to create; include to update existing
  Calculated_Field_Data        (required) — full field definition
```

### Calculated_Field_DataType — 45 fields, polymorphic
Base fields (always present):
```
Calculated_Field_Reference_ID   string   — stable cross-tenant ID; use this for identity, NOT WID
Class_Name                      string   — discriminates which sub-type block is populated
Name                            string
Description                     string (optional)
External_Field_Category_Reference   → External_Field_CategoryObjectType
External_Field_Usage_Reference      → External_Field_UsageObjectType
External_Field_Reference            → Business_ObjectObjectType  (the WD object this field lives on)
Intermediate_Calculation        boolean
Do_Not_Use                      boolean
Option_Reference                → Calculated_Field_OptionObjectType
WQL_Alias                       string
```

Sub-type blocks — exactly one is populated per field (determined by Class_Name):
Arithmetic, Conditional_Expression, Concatenate, Convert_Currency, Date_Constant,
Date_Difference, Extract_Single_Instance, Evaluate_Expression, Increment_or_Decrement_Date,
Lookup_Single_Instance, Lookup_Value_As_Of_Date, Numeric_Constant, Text_Constant,
Format_Date, Extract_Multi_Instance, Lookup_Org, Lookup_Org_Role_Assignments,
Lookup_Range_Band, Count_Related_Instances, Sum_Related_Instances, Text_Substring,
Text_Length, Lookup_Hierarchy_Rollup, Format_Number, Convert_Text_To_Number,
Aggregate_Related_Instances, Lookup_Translated_Value, Build_Date, Lookup_Hierarchy,
Prompt, Lookup_Date_Rollup, Format_Text, Lookup_Field_with_Prompts,
Evaluate_Expression_Band (and more)

### Tenanted_Report_Definition_DataType — 77 fields (key ones)
```
Name                                         string
Tenanted_Report_Definition_System_User_Reference  → System_UserObjectType  (report owner — must remap)
Tenanted_Report_Definition_Type_Reference    → Report_TypeObjectType
Report_Tag_Reference                         → Report_TagObjectType
Enable_As_Worklet                            boolean
Web_Service_API_Version_Reference            → Web_Service_API_VersionObjectType
Web_Service_Include_Facets                   boolean
Data_Source_Reference                        → Data_SourceObjectType  (must exist in dest before import)
Instructions, Comment                        string
Tenanted_Report_Column_Data                  sub-type — column definitions
Tenanted_Report_Definition_Sub_Filter_Data   sub-type — filter conditions
Tenanted_Report_Chart_Layout_Data            sub-type — chart config
```

---

## WID handling — the most important thing to get right

There are two classes of WID references inside calculated field sub-type data:

### Class 1 — Workday-delivered (global) field WIDs
Same WID in every Workday tenant worldwide. Pass through unchanged.

### Class 2 — Custom calculated field WIDs
Tenant-specific. A custom calculated field PUT to the destination tenant gets a brand new WID.
If field A references field B (both custom), B's source WID ≠ B's destination WID.

### The algorithm
```python
# Step 1: GET all custom calculated fields from source
source_fields = get_all_calculated_fields(source_client)

# Step 2: Build set of all custom WIDs from source
custom_source_wids = {field.wid for field in source_fields}

# Step 3: Build dependency DAG
# For each field, scan its sub-type data for WID references that are in custom_source_wids
dag = build_dependency_dag(source_fields, custom_source_wids)

# Step 4: Topological sort → determines PUT order
ordered_fields = topological_sort(dag)

# Step 5: PUT loop — sequential, NOT parallel
wid_map = {}  # source_wid → dest_wid

for field in ordered_fields:
    # Substitute any custom WIDs in the sub-type data
    remapped_field = substitute_wids(field, wid_map)
    
    if dry_run:
        log(f"DRY RUN: would PUT {field.name}")
        continue
    
    response = put_calculated_field(dest_client, remapped_field)
    dest_wid = response.Calculated_Field_Reference.WID
    wid_map[field.source_wid] = dest_wid  # register for downstream fields

# Step 6: PUT report definitions — apply same wid_map to column/filter data
for report in reports:
    remapped_report = substitute_wids(report, wid_map)
    put_tenanted_report_definition(dest_client, remapped_report)
```

### Identifying custom vs global WIDs
Any WID reference in sub-type data that is in `custom_source_wids` → remap via `wid_map`.
Any WID not in `custom_source_wids` → global/delivered → pass through unchanged.

### WID scanning approach
zeep returns response objects as nested dicts/objects. Use `zeep.helpers.serialize_object(response)` to convert to plain Python dicts, then recursively walk any value that has a `WID` key. Compare each WID against `custom_source_wids`. This handles all sub-type variants without needing to know each sub-type's internal structure.

---

## Module interfaces

### auth/client.py
```python
def make_client(services_host: str, tenant: str, isu_username: str, isu_password: str, 
                service: str, version: str) -> zeep.Client:
    """
    Returns an authenticated zeep SOAP client for the given tenant.
    Applies WS-Security UsernameToken header.
    Endpoint: https://{services_host}/ccx/service/{tenant}/{service}/{version}
    """

def verify_connection(client: zeep.Client) -> bool:
    """
    Lightweight check that credentials work.
    Call Get_Calculated_Fields with Page=1, Count=1 and verify no auth fault.
    Returns True on success, raises on auth failure.
    """
```

### discovery/inventory.py
```python
def get_all_calculated_fields(client: zeep.Client) -> list[dict]:
    """
    Pages through Get_Calculated_Fields with Include_Calculated_Field_Data=True.
    Returns list of serialized field dicts (via zeep.helpers.serialize_object).
    """

def get_all_report_definitions(client: zeep.Client) -> list[dict]:
    """
    Pages through Get_Tenanted_Report_Definitions.
    Returns list of serialized report dicts.
    """
```

### migrate/ordering.py
```python
def extract_custom_wid_refs(field_data: dict, custom_wids: set[str]) -> set[str]:
    """
    Recursively walks field_data dict. Returns set of WIDs found that are in custom_wids.
    """

def build_dag(fields: list[dict], custom_wids: set[str]) -> dict[str, set[str]]:
    """
    Returns adjacency dict: {field_ref_id: {dep_ref_id, ...}}
    """

def topological_sort(dag: dict[str, set[str]], fields: list[dict]) -> list[dict]:
    """
    Kahn's algorithm. Raises on cycle (shouldn't happen in valid Workday config).
    Returns fields in safe PUT order.
    """

def substitute_wids(obj: dict, wid_map: dict[str, str]) -> dict:
    """
    Deep-copies obj, replacing any WID value found in wid_map with the mapped value.
    Leaves WIDs not in wid_map unchanged (global/delivered fields).
    """
```

### migrate/writer.py
```python
def put_calculated_field(client: zeep.Client, field_data: dict, dry_run: bool = True) -> str | None:
    """
    PUTs a single calculated field. Returns destination WID on success, None if dry_run.
    Logs what it would do in dry_run mode.
    """

def put_report_definition(client: zeep.Client, report_data: dict, 
                          dest_isu_ref: dict, dry_run: bool = True) -> str | None:
    """
    PUTs a single report definition. Remaps System_User_Reference to dest_isu_ref.
    Returns destination report WID on success, None if dry_run.
    """
```

### validation/verify.py
```python
def verify_fields_migrated(dest_client: zeep.Client, 
                            source_fields: list[dict]) -> dict[str, bool]:
    """
    For each source field, fetches by Calculated_Field_Reference_ID from destination.
    Returns {reference_id: exists_bool}
    """

def verify_reports_migrated(dest_client: zeep.Client, 
                             source_reports: list[dict]) -> dict[str, bool]:
    """Same pattern for reports."""
```

---

## zeep patterns

### Client initialization
```python
from zeep import Client
from zeep.wsse import UsernameToken
from zeep.transports import Transport
import requests

def make_client(services_host, tenant, isu_username, isu_password, service, version):
    endpoint = f"https://{services_host}/ccx/service/{tenant}/{service}/{version}"
    wsdl_url = f"{endpoint}?wsdl"
    
    session = requests.Session()
    transport = Transport(session=session, timeout=30)
    wsse = UsernameToken(f"{isu_username}@{tenant}", isu_password)
    
    return Client(wsdl=wsdl_url, wsse=wsse, transport=transport)
```

### Calling an operation
```python
from zeep.helpers import serialize_object

response = client.service.Get_Calculated_Fields(
    Request_Criteria={},
    Response_Filter={"Page": 1, "Count": 100},
    Response_Group={"Include_Reference": True, "Include_Calculated_Field_Data": True}
)
fields = serialize_object(response)  # converts to plain Python dict/list
```

### Pagination pattern
```python
page, count, all_fields = 1, 100, []
while True:
    resp = client.service.Get_Calculated_Fields(
        Response_Filter={"Page": page, "Count": count},
        Response_Group={"Include_Reference": True, "Include_Calculated_Field_Data": True}
    )
    data = serialize_object(resp)
    results = data.get("Response_Results", {})
    items = data.get("Response_Data", {}).get("Calculated_Field", []) or []
    all_fields.extend(items)
    total_pages = int(results.get("Total_Pages", 1))
    if page >= total_pages:
        break
    page += 1
```

### Rate limit / retry
Workday rate-limits at ~10 calls/sec per tenant. Add exponential backoff on 429:
```python
import time

def call_with_retry(fn, *args, max_retries=5, **kwargs):
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) or "Too Many Requests" in str(e):
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"Max retries exceeded")
```

---

## Environment variables (.env)
```
WD_SOURCE_SERVICES_HOST=impl-services1.wd12.myworkday.com
WD_SOURCE_TENANT=commitconsulting_dpt1
WD_SOURCE_ISU_USERNAME=your_isu_username
WD_SOURCE_ISU_PASSWORD=your_isu_password

WD_DEST_SERVICES_HOST=impl-services1.wd12.myworkday.com
WD_DEST_TENANT=client_sandbox_tenant
WD_DEST_ISU_USERNAME=your_isu_username
WD_DEST_ISU_PASSWORD=your_isu_password

WD_OX_SERVICE_NAME=Report_Metadata
WD_WWS_VERSION=v47.0
DRY_RUN=true
```

Load with:
```python
from dotenv import load_dotenv
import os
load_dotenv()
services_host = os.environ["WD_SOURCE_SERVICES_HOST"]
```

---

## Stack
Declared in `pyproject.toml` — the single source of truth. Currently installed
and verified:

```
python 3.12.6
zeep 4.3.3           # SOAP + WS-Security. The version the read prototype was
                     #   verified against — do not downgrade without re-verifying.
requests 2.34.2
python-dotenv 1.2.2
lxml 6.1.1
click 8.4.2
pytest 9.1.1         # dev extra
pytest-dotenv 0.5.2  # dev extra
```

Install (a `.venv` already exists at the project root):
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt   # resolves to `-e .[dev]`
```

---

## Testing approach

All tests live in `tests/`. The core rule: **a bare `pytest` must pass with no
`.env` and no network.** This is enforced by `addopts = -m 'not live'` in
`pyproject.toml`, not by convention — do not remove it.

### Markers
| Marker | Meaning | Runs by default? |
|---|---|---|
| _(none)_ | Pure logic, fixtures, or schema shape. Offline. | Yes |
| `live` | Needs a real tenant + populated `.env`. | No — `pytest -m live` |
| `dest` | Touches the DESTINATION tenant. Always with `live` and `dry_run=True`. | No |

`live` tests auto-skip with a clear reason when the `WD_SOURCE_*` vars are absent
(see `tests/conftest.py`), so a fresh clone never fails confusingly.

### Fixtures (`tests/conftest.py`)
- `wsdl_path` — path to the bundled WSDL.
- `offline_client` — a zeep client built from the local WSDL with **no
  credentials**. Use it to assert on schema shape, operation names, and request
  serialization. Do not call operations on it; the WSDL embeds the real service
  address, so a call would hit the tenant unauthenticated.

### Per-module test plan
- `test_wsdl_contract.py` — **exists.** Offline guards on documented WSDL facts
  (operation names, services host, versioned path). Fails loudly if an
  assumption in `docs/WSDL_NOTES.md` stops holding.
- `test_auth.py` — `verify_connection()` against the source tenant only, no
  writes. Mark `live`.
- `test_discovery.py` — `get_all_calculated_fields()` asserts non-empty list.
  Mark `live`. Save a sanitized response into `tests/fixtures/` so downstream
  tests can run offline.
- `test_ordering.py` — pure unit tests over fixture data. **No marker** — this is
  the fast inner loop, and it covers the highest-risk logic (the WID remapping).
- `test_writer.py` — always `dry_run=True`; assert the intended payload and
  **zero** SOAP calls to the destination. Use a mock transport so this needs no
  marker and stays offline.
- `test_validation.py` — reads from destination only, no writes. Mark `live` + `dest`.

Never write to the destination tenant in any test, marked or not.

---

## Known risks / pre-flight checklist before first real migration
- [ ] Source ISU has Special OX Web Services Get permission + activated
- [ ] Destination ISU has Special OX Web Services Put permission + activated
- [ ] All data sources referenced by reports exist in destination tenant
- [ ] Destination tenant version matches source (or is compatible)
- [ ] Report owner (System_User_Reference) remapping strategy confirmed
- [ ] Dry run executed and output reviewed before real run

---

## What was discovered and ruled out
- **OX UI workflow / Configuration Packages**: not used — direct SOAP Get/Put is available
- **Workday REST API for report/field definitions**: does not exist
- **`Reporting_Analytics` SOAP service**: does not exist at v47.0; correct service is `Report_Metadata`
- **Public WWS directory**: does not list `Report_Metadata` — it is a restricted service
- **UI host for SOAP** (`impl.wd12.myworkday.com`): wrong — use services host (`impl-services1.wd12.myworkday.com`)
- **WSDL without version in URL path**: returns 404 on this tenant — version must be in path
