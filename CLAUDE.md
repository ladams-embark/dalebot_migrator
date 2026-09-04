# Workday Tenant Configuration Migration Tool — CLAUDE.md

## Role
You are a senior integration engineer building a Python tool that migrates
Workday tenant configuration (calculated fields, reports, dashboards and their
dependencies) from a SOURCE tenant to a DESTINATION tenant via SOAP web
services. Prioritize correctness and reversibility over speed. Never invent
Workday API behavior — everything here has been verified against a live tenant.

---

## How the application works

### What it does
This tool reads custom report definitions, calculated fields, calculated
measures, prompt sets, prompt fields, gauge ranges, analytic indicators, and
custom dashboards from one Workday tenant and writes them to another, preserving
all internal references. The core problem is that every custom object gets a
new WID (Workday ID) on the destination, so every reference from object A to
object B must be remapped after B is written.

### Two interfaces
1. **Streamlit wizard** (`src/wdmigrator/ui/`) — a gated linear flow:
   Connect → Scope → Select → Plan → Run → Results.
   Each step has a `gate()` that must pass before the user can advance.
2. **Scripts** (`scripts/`) — standalone Python scripts for batch operations
   (export, migration) driven by `.env` credentials and CLI flags.

### Engine pipeline (all under `src/wdmigrator/`)
The UI and scripts both drive the same engine through `api.py`, which is the
only module the UI imports. The pipeline:

1. **Connect** — `auth/client.py` builds a zeep SOAP client with WS-Security.
   `auth/endpoint_discovery.py` probes data centers to find a tenant's services
   host when the user only has a browser URL.
2. **Index** — `discovery/inventory.py` sweeps the source tenant for all objects
   of each kind (paginated at Count=999). Indexes are cached to disk.
3. **Select** — the user picks reports and/or dashboards to migrate.
4. **Resolve** — `migrate/resolver.py` walks each selected object's payload,
   classifies every WID reference (calculated field? report? delivered global?),
   and recursively pulls in dependencies until the closure is complete.
5. **Order** — `migrate/ordering.py` builds a DAG of dependencies and
   topologically sorts it (Kahn's algorithm, child-most first). Cycles are a
   hard block.
6. **Plan** — `migrate/planner.py` probes each object against the destination
   to decide CREATE / UPDATE / SKIP. Cross-tenant matching handles the case
   where the same field exists on both tenants under different IDs.
7. **Execute** — `migrate/writer.py` PUTs objects sequentially in sorted order.
   Each response WID feeds the next payload via `substitute_wids`. Dry-run is
   the default; `safety.py` enforces guards on every write.
8. **Results** — success/failure/warning per object.

### Migratable object kinds (in dependency order)
Calculated fields → calculated measures → reports → prompt fields → prompt
sets → custom dashboards (tabbed and untabbed). The order falls out of the DAG,
not hardcoded phases.

Also, on the Time Tracking Implementation Service:
time calculation tags → time calculation groups → time calculations. These
form their own mini-DAG driven by the same closure/planner/writer, routed to
the sibling connection via `TIME_TRACKING_KINDS`. Business IDs
(`Time_Calculation_ID`, `Time_Calculation_Group_ID`, `Time_Calculation_Tag_ID`)
are all stable cross-tenant lookup keys — unlike `Custom_Report_ID`.

### Structural rules
- **Nothing under `src/wdmigrator/` except `ui/` may import streamlit or
  pandas.** The engine stays importable and testable without them.
- **Every long-running engine operation is a generator yielding progress
  events**, not a blocking call. This makes cancellable progress work under
  Streamlit's rerun model and serves CLI scripts equally well.

### Remaining gaps
- `validation/verify.py` is still a stub.
- `cli.py` does not exist (Click CLI).
- Prompt field migration is implemented but lightly tested.
- UPDATE behavior (`Put` with a reference) is unverified — prefer CREATE/SKIP.

---

## Hard rules — never violate
- NEVER hardcode credentials, tenant URLs, ISU passwords, or tokens. All secrets live in `.env` (gitignored).
- If a credential appears anywhere, strip it and tell the user to rotate it.
- The DESTINATION tenant is a write target — treat every Put as potentially destructive.
- **This service has no delete operation.** Nothing written can be undone by the tool. Bias every decision toward refusing a write.
- `DRY_RUN=true` is the default. Never write without dry-run=false AND explicit user confirmation.
- **Source and destination must never be the same tenant for a live run.** Enforced unconditionally in `safety.py` with no override.
- Safety checks belong in the engine, re-validated per object — never in the UI alone.
- Only test against Sandbox or Implementation tenants. Warn loudly before anything touches Production.
- Only make changes directly requested. No unrequested features, abstractions, or refactors.
- Before each coding session, state: which files you will touch, what you will change, and what the stop condition is.

---

## Workday domain knowledge

### The two hosts — critical, do not mix up
Every tenant has two hostnames:
- **UI host** (browser): `impl.wd12.myworkday.com`
- **Services host** (all API calls): `impl-services1.wd12.myworkday.com`

All SOAP/WSDL/REST calls use the services host. There is no directory API
mapping a tenant to its services host — `auth/endpoint_discovery.py` probes
known data centers. The services host is a property of the data center (pod),
not the tenant.

SOAP endpoint pattern:
```
https://{services_host}/ccx/service/{tenant}/{service}/{version}
```

### Services and version
Two services are in use, both at `v46.0`:
- **`Core_Implementation_Service`** — calculated fields, reports, dashboards, prompt sets/fields, gauge ranges, analytic indicators. Default. `Report_Metadata` is rejected on every tenant tested.
- **`Time_Tracking_Implementation_Service`** — Time Calculations, Groups, Tags. Implementer-only. Ops do NOT accept `Response_Group` in their WSDL signature (`Request_References` + `Response_Filter` + `version` only). Opened via `Connection.for_service(TIME_TRACKING_SERVICE_NAME)`, which shares the target, credentials, and rate limiter with the Core connection.

**Version**: `v46.0` hardcoded in `auth/client.py`. Highest version that works on all tenants seen. Must be in the URL path.

### Authentication
SOAP WS-Security with ISU credentials. Username format: `{isu_username}@{tenant}`.

Each tenant's ISU needs **Get and Put** on **Configuration Set: Custom Reports
and Fields**. After any permission change: run "Activate Pending Security
Policy Changes" — changes are not immediate.

### Operations

| Operation | Direction | Notes |
|-----------|-----------|-------|
| `Get_Calculated_Fields` / `Put_Calculated_Field` | Read / Write | Put response has `Exceptions_Response_Data` — a 200 can still fail |
| `Get_Tenanted_Report_Definitions` / `Put_Tenanted_Report_Definition` | Read / Write | No exceptions block — fault-only errors |
| `Get_Custom_Dashboards_with_Tabs` / `_without_Tabs` | Read | **Implementer account required** |
| `Put_Custom_Dashboard_with_Tabs` / `_without_Tabs` | Write | **Implementer account required**. Has `Add_Only` attribute |
| `Get_Prompt_Sets` / `Put_Prompt_Set` | Both | Readable by plain ISU; Put is fault-only |
| `Get_Prompt_Fields` / `Put_Prompt_Field` | Both | **Implementer account required** |
| `Get_Time_Calculations` / `Put_Time_Calculation` | Both | On **Time_Tracking_Implementation_Service**. Implementer-only. No `Response_Group` |
| `Get_Time_Calculation_Groups` / `Put_Time_Calculation_Group` | Both | Same service. Groups reference `Time_Tracking_Eligibility_Rule_Reference` — treated as destination prerequisites |
| `Get_Time_Calculation_Tags` / `Put_Time_Calculation_Tag` | Both | Same service. Leaf kind |

Dashboard operations require an implementer account — no amount of domain
granting fixes it. `discovery/inventory.py:requires_implementer` detects this.

### Key API behaviors

**Pagination**: `Response_Filter.Count` accepts 999. Full CF index ≈ 10 pages / 25s. Full report index ≈ 6 pages / 158s.

**Report identity**: `Custom_Report_ID` is returned but rejected as a lookup key. Reports must be matched by exact name (`Report_Name` criteria). Report names are not unique — duplicates resolve to UNKNOWN.

**Dashboard identity**: Two unrelated object types (tabbed = `Custom_Landing_Page_Group`, untabbed = `Custom_Landing_Page`). Both must be swept. Dashboard business IDs work as lookup keys, unlike report IDs.

**Prompt sets**: `Prompt_Set_Request_Criteria` is unusable. Must sweep all and read dependency edges from dashboard payloads.

**`External_Field_Reference` is a superset ID space**: A report column's field reference can be a calculated field, custom field, computed data source field, or several other types sharing one WID namespace. A `NOT_FOUND` on `Get_Calculated_Fields` does not mean unmigratable — it can be a report-scoped field (promotable), a delivered field (passes through), or genuinely out of scope.

**Report-scoped calculated fields**: Invisible to `Get_Calculated_Fields`. Once promoted to global in the Workday UI, there is an activation delay before the same WID resolves in the API.

**Rate limiting**: ~10 calls/sec per tenant. Exponential backoff on 429.

---

## WID handling

### The algorithm
Custom-vs-delivered is decided by **probing the destination**, not by
enumerating the source. A field that resolves in the destination is
delivered/existing (SKIP, seed `wid_map`). One that does not resolve is
custom (CREATE). Any unexpected fault → UNKNOWN (hard block, never collapse
to MISSING).

### Cross-tenant matching
`Calculated_Field_ID` is NOT reliably stable across independently-built
tenants. Without cross-tenant matching, probing by ID alone can miss 50%+
of fields that already exist, creating unremovable duplicates.

`planner._match_calculated_field_across_tenants` re-checks ID misses in three
tiers (returns UNKNOWN on ambiguity):
1. **Shape** — `(Name, Class_Name, External_Field_Reference WID)`
2. **Shape + WQL_Alias** tiebreaker
3. **WQL_Alias alone** narrowed by business object

`BI_Calculated_Measure_ID` never matches across tenants by construction.
Measures match on `(Name, Business_Object_Reference WID)`.

Enable via `iter_check_existence(..., match_index=..., measure_match_index=...)`.

### WQL_Alias uniqueness
Must be unique per business object. A collision almost always means the field
already exists — match the field instead of stripping the alias.

### Reference ID dependencies
~44% of calculated field cross-references use `Calculated_Field_Reference_ID`
(a bare string, no WID). `ordering.extract_reference_id_refs()` collects these
and `resolver` maps them to WIDs through the index. These are business IDs
stable across tenants — `substitute_wids` must leave them alone.

### What the writer strips (tenant-specific references that cannot cross tenants)
- Report owner (`System_User_Reference`) — remapped to destination ISU
- Report tags (`Report_Tag_Reference`)
- Filter instance values (`Filter_Instances_Reference`)
- Tenanted security groups (`Security_Group_Reference`) — delivered ones kept
- Announcements (uploaded images, media, quicklinks, workers)
- `Worklet_Landing_Page_Reference` — stripped initially, re-added in the deferred dashboard pass

### Dashboard-report worklet cycle
A dashboard references its reports as worklets; a report must carry
`Worklet_Landing_Page_Reference` naming the dashboard. Neither can go first.
Solved by writing the dashboard as a shell (no worklet configs), writing the
reports with the real dashboard reference, then re-writing the dashboard
complete (without `Add_Only`). Reports used as worklets must be `Shared=True`.

### Time Calculation snapshot IDs
A Time Calculation references its groups through
`Time_Calculation_Group_Snapshot_Reference`, whose ID list carries a
tenant-local `Time_Calculation_Group_Snapshot_ID` **plus** a `parent_id` naming
the stable `Time_Calculation_Group_ID`. The writer (v1, `build_time_calculation_payload`)
passes the source's snapshot IDs through unchanged; that works when tenants
share content (dpt1 ↔ dest) but is expected to fail on a fresh destination —
the fix is to swap the snapshot reference for a `Time_Calculation_Group_Reference`
using the parent's Group business ID. Deferred until a live dry-run surfaces
the fault. The calc's own `Time_Calculation_Snapshot_ID` on `Time_Calculation_Snapshot_Data`
is also tenant-local; a create should probably strip it.

### Time Tracking Eligibility Rules
Groups reference these but the tool does NOT migrate them. Treated as
destination prerequisites — if a referenced rule isn't in the destination,
the Group write fails with a blocking reference the user acts on manually.

### Prompt field gap
Prompt sets can depend on prompt fields (`Get_Prompt_Fields`/`Put_Prompt_Field`
exist, implementer-gated). A prompt set whose parameters are missing from the
destination cannot be written until prompt field migration runs first.

---

## Module interfaces

All paths relative to `src/wdmigrator/`.

### config/targets.py
`TenantTarget` — frozen dataclass for tenant addressing (no credentials).
`parse_tenant_url(raw)` accepts pasted browser URLs.
`classify_environment(host, tenant)` → `Environment` enum. UNKNOWN = PRODUCTION for safety purposes.

### safety.py
`WriteGuard` — frozen dataclass checked before every write.
`assert_write_allowed(guard)` raises if dry_run=True or guards fail.
Non-impl override: `WDMIGRATOR_ALLOW_NON_IMPL=1` env var + `confirmed_tenant_name` must match.

### auth/client.py
`make_client()` → authenticated zeep Client with WS-Security.
`verify_connection()` → lightweight auth check.

### discovery/inventory.py
`iter_*_index()` generators for each object kind. Paginate at Count=999, yield progress events.

### migrate/resolver.py
`resolve_closure()` → `Closure` with all dependencies recursively resolved.

### migrate/ordering.py
`topological_sort()` → nodes in safe PUT order (child-most first).
`substitute_wids()` → deep-copy with WID remapping.

### migrate/planner.py
`build_plan()` → `MigrationPlan` with CREATE/UPDATE/SKIP per node.

### migrate/writer.py
`write_node()` → PUT a single object, returns `WriteRecord`.
Dry-run default. Inspects `Exceptions_Response_Data` on CF puts.

### api.py
Facade over all engine modules. The only module the UI imports.
All long-running operations are generators yielding progress events.

---

## Environment variables (.env)
```
WD_SOURCE_SERVICES_HOST=impl-services1.wd12.myworkday.com
WD_SOURCE_TENANT=your_source_tenant
WD_SOURCE_ISU_USERNAME=your_isu_username
WD_SOURCE_ISU_PASSWORD=your_isu_password

WD_DEST_SERVICES_HOST=impl-services1.wd12.myworkday.com
WD_DEST_TENANT=your_dest_tenant
WD_DEST_ISU_USERNAME=your_isu_username
WD_DEST_ISU_PASSWORD=your_isu_password

WD_OX_SERVICE_NAME=Core_Implementation_Service
DRY_RUN=true
WDMIGRATOR_ALLOW_NON_IMPL=1   # only if destination is a known-safe non-impl tenant
```

API version is hardcoded to `v46.0` in `auth/client.py` — not configurable via env.

---

## Stack
Declared in `pyproject.toml`. Key dependencies:
```
python 3.12.6
zeep 4.3.3       # SOAP + WS-Security
requests 2.34.2
python-dotenv 1.2.2
lxml 6.1.1
click 8.4.2
streamlit        # UI only
pytest 9.1.1     # dev
```

Install: `.\.venv\Scripts\Activate.ps1` then `pip install -e .[dev]`

---

## Testing approach

All tests in `tests/`. A bare `pytest` must pass with no `.env` and no network
(enforced by `addopts = -m 'not live'` in `pyproject.toml`).

| Marker | Meaning | Default? |
|--------|---------|----------|
| _(none)_ | Offline logic/schema tests | Yes |
| `live` | Needs a real tenant + `.env` | No — `pytest -m live` |
| `dest` | Touches destination (always dry_run=True) | No |

Never write to the destination tenant in any test.

---

## Pre-flight checklist
- [ ] Source and destination are distinct tenants (safety.py enforces this)
- [ ] Check `.env` host/tenant pairing — a mismatch gives HTTP 500, not an auth error
- [ ] Both ISUs have Get+Put on Configuration Set: Custom Reports and Fields, activated
- [ ] Dashboards require implementer accounts on both sides
- [ ] All data sources referenced by reports exist in destination
- [ ] Dry run reviewed before live run
- [ ] Destination may have been refreshed — probe results that look surprising warrant a fresh sweep

---

## What was ruled out
- **Report_Metadata service**: WSDL resolves but every call is rejected — use Core_Implementation_Service
- **Configuration Packages / Object Transporter API**: does not exist
- **Workday REST API for report/field definitions**: does not exist
- **UI host for SOAP**: wrong — use services host
- **v48.0+**: rejected on tested tenants
