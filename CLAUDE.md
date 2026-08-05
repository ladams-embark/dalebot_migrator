# Workday Tenant Configuration Migration Tool — CLAUDE.md

## Role
You are a senior integration engineer building a Python CLI tool that migrates calculated fields and custom report definitions from a SOURCE Workday tenant to a DESTINATION Workday tenant via SOAP web services. You have full file access and can run code. Prioritize correctness and reversibility over speed. Never invent Workday API behavior — everything here has been verified against a live tenant.

---

## Hard rules — never violate
- NEVER hardcode credentials, tenant URLs, ISU passwords, or tokens. All secrets live in `.env` (gitignored).
- If a credential appears anywhere, strip it and tell the user to rotate it and use `.env` instead.
- The DESTINATION tenant is a write target — treat every Put as potentially destructive.
- **This service has no delete operation.** Nothing the tool writes can be undone by the tool; it has to be removed by hand in the Workday UI, object by object. Bias every decision toward refusing a write.
- `DRY_RUN=true` is the default. Never write to a tenant without dry-run=false AND explicit user confirmation.
- **Source and destination must never be the same tenant for a live run.** Enforced unconditionally in `safety.py` with no override — an override that exists is one that eventually gets clicked, and the failure mode is silently corrupting the tenant being migrated *from*. Dry runs against a same-tenant config stay allowed.
- Safety checks belong in the engine, re-validated per object — never in the UI alone.
- Only test against Sandbox or Implementation tenants. Warn loudly before anything touches Production.
- Only make changes directly requested. No unrequested features, abstractions, or refactors.
- Before each coding session, state: which files you will touch, what you will change, and what the stop condition is. Wait for confirmation before writing to a tenant.

---

## Current state of the project
The package structure and dev environment are in place and verified offline
(`scripts/selfcheck.py` and `pytest` both green, neither contacting a tenant).
The read path is verified **live** against the source tenant.

The end goal is a Streamlit app: the user supplies source + destination tenant
URLs and credentials, selects reports and/or calculated fields, reviews the
resolved dependency order and any conflicts, then pushes — with a
success/failure overview afterwards. Auth on the app itself is out of scope
for now.

All module paths below are relative to `src/wdmigrator/`, so `auth/client.py`
means `src/wdmigrator/auth/client.py`, imported as `wdmigrator.auth.client`.

Build order:
1. `config/targets.py` + `safety.py` — **done.** Tenant URL parsing and the
   write guard. Built first deliberately: the guard has to exist before any
   code that can write to a tenant. 57 offline tests.
2. `auth/client.py` ← **next**
3. `discovery/inventory.py` — index sweeps at Count=999, disk cache
4. `migrate/ordering.py` — DAG + Kahn topological sort (child-most first)
5. `migrate/resolver.py` — WID classification + dependency closure walker
6. `migrate/planner.py` — CREATE/UPDATE/SKIP via destination probing
7. `migrate/writer.py` — dry-run default; inspect `Exceptions_Response_Data`
8. `api.py` — the only engine module the UI imports; generator-based
9. `ui/` — Streamlit gated wizard; then `validation/verify.py`, `cli.py`
10. Tests in `tests/` alongside each module (see the Testing approach section)

Two structural rules for this build:
- **Nothing under `src/wdmigrator/` except `ui/` may import streamlit or
  pandas.** The engine stays importable and testable without them.
- **Every long-running engine operation is a generator yielding progress
  events**, not a blocking call with a callback. This is what makes
  cancellable progress work under Streamlit's rerun model, and it serves a
  `click` CLI equally well.

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
https://impl-services1.wd12.myworkday.com/ccx/service/commitconsulting_dpt1/Core_Implementation_Service/v47.0?wsdl
```

### The Core_Implementation_Service service
- **Service name**: `Core_Implementation_Service` (confirmed live 2026-07-30 — see below)
- **Tenant version**: v47.0 — must be in the URL path, not just the SOAP envelope
- **`Report_Metadata` is NOT usable on this tenant**: its WSDL defines the same
  operations and resolves fine, but every call (`Get_Calculated_Fields`,
  `Get_Tenanted_Report_Definitions`) fails with a SOAP fault —
  `SOAP-ENV:Client.validationError`, "The web service or version is invalid
  for the requested operation" — even with the ISU confirmed as a proper
  Integration System User, with both **Special OX Web Services** and **Custom
  Reports and Fields** domain access granted and activated. The identical
  operations succeed via `Core_Implementation_Service` with the same
  credentials, same tenant, same version. Use `Core_Implementation_Service`,
  not `Report_Metadata`, going forward.

Operations used (all confirmed present on `Core_Implementation_Service`; read
side confirmed working live, write side not yet tested):

| Operation | Direction |
|-----------|-----------|
| `Get_Calculated_Fields` | Read from source |
| `Put_Calculated_Field` | Write to destination |
| `Get_Tenanted_Report_Definitions` | Read from source |
| `Put_Tenanted_Report_Definition` | Write to destination |

### Authentication
SOAP WS-Security with ISU credentials. Username format: `{isu_username}@{tenant}`.

Each tenant needs its own ISU with **Get and Put** granted on the
**Configuration Set: Custom Reports and Fields** domain — both source and
destination ISUs need both permissions (not the asymmetric Get-only /
Get+Put split this doc previously assumed). **Special OX Web Services
access is not required** — despite the domain name being suggestive, it
turned out not to be the one that actually gates these operations.

Note: on the source tenant, domain access alone did not make `Report_Metadata`
work (see above) — `Core_Implementation_Service` is what actually succeeded.

After any ISU permission change in Workday: run "Activate Pending Security Policy Changes" — changes are NOT immediate and the ISU will silently return empty data until activated.

### Live-verified facts (tested 2026-07-31 — build on these, do not re-derive)

| Fact | Consequence |
|---|---|
| `Response_Filter.Count` accepts **999**, not 100 | Full CF index = 10 pages ≈ **25s**. Full report index = 6 pages ≈ **158s**. Cheap enough to just build and cache. |
| `Report_Name` criteria is **exact-match** (substring returns 0 hits) | Server-side search is useless for discovery. A local index is mandatory, not an optimization. |
| Reference-only report sweep returns **no name** — only `{WID, Custom_Report_ID}` | A browsable report index needs `Include_..._Data=True` (the slower path). |
| **`Custom_Report_ID` is returned but rejected as a lookup key** | Verified on 18/18 sampled reports: feeding the `Custom_Report_ID` from a report's own reference back into `Request_References` fails with "is not a valid ID value for type = 'Custom_Report_ID'", while the same report resolves fine by WID. Reports must therefore be matched across tenants by **exact name** (`Request_Criteria.Report_Name`), which does work. Calculated fields are unaffected — `Calculated_Field_ID` works normally. |
| Report names are **not unique** — 7 of 999 sampled reports shared one | A duplicated name must resolve to UNKNOWN, never a guess. Overwriting the wrong report cannot be undone. |
| `Put_Calculated_Field_ResponseType` contains `Exceptions_Response_Data` | **A HTTP-200 no-fault PUT can still have failed.** Always inspect it — "no fault" ≠ success. |
| `Put_Tenanted_Report_Definition_ResponseType` has **no** exceptions block | Error handling is asymmetric between the two writers. |
| Volumes on `commitconsulting_dpt1`: ~9,650+ CFs (grows as report-scoped fields get promoted to global — was 9,652, now 9,654) / ~5,153 reports | Index once, cache, rate-limit at ~8 calls/sec. Re-sweep rather than trust an old cache if a dependency unexpectedly won't resolve. |
| **No Configuration Package / Object Transporter API exists** | Probed `Object_Transporter`, `Configuration_Package`, `Integrations`, `Custom_Object`, `Change_Set`, `Solution`, `Workday_Extensibility` — none expose a package/transport/migration operation. OX is UI-only tooling. Scope by explicit user selection instead. |
| This service has **no delete operation** | Nothing written can be undone by the tool. See `src/wdmigrator/safety.py`. |
| **`Tenanted_Report_Column_Data.External_Field_Reference` is not always a `Calculated_Field` reference — it's a superset `External_Field` ID space** | Its enumeration (`External_FieldReferenceEnumeration` in the WSDL) is `WID`, `Calculated_Field_ID`, `Custom_Field_ID`, `Computed_Data_Source_Field_ID`, `Cube_Field_Last_Entry_ID`, `Custom_Field_Data_Set_ID`, `Extension_Computed_Data_Field_Reference_ID`, `External_Analytics_Data_Source_Field_ID`, plus two `Business_View_*_Field` variants — several distinct underlying object types share one WID namespace. `Get_Calculated_Fields` by WID returning `NOT_FOUND` for a column reference is real and not a bug — **but it does not by itself mean the field is unmigratable.** See the next two rows: it can also mean "report-scoped calculated field, not yet promoted to global" (fixable, see below) or "delivered field, passes through fine regardless" (also fine — confirmed live on "AE Previous Worker," which has exactly this shape of reference and migrates successfully). There is nothing in this WSDL that distinguishes those cases from a genuinely unmigratable `Custom_Field_ID`-space reference ahead of time — **do not build a pre-flight blocker on "not a Calculated_Field" alone; it was tried and reverted for producing false positives.** |
| **No operation exists to Get/Put a plain custom field** — still true, but confirm before assuming a `NOT_FOUND` is one | Checked every operation in the bundled WSDL for anything custom-field-shaped: `Get/Put_Custom_Object_Field_Types_for_OX`, `Get/Put_Custom_Object_Rules_for_OX`, `Get/Put_Custom_Object_Validations_for_OX`, `Get/Put_Custom_Object_for_OX` — all of these manage **Custom Object** field *type definitions* (Workday's user-defined-business-object feature), not an individual custom field hung off a standard object like Worker, and not a plain `Custom_Field_ID`-space object either. If a report column's `External_Field_Reference` genuinely does resolve to a `Custom_Field_ID`-space object, this tool has no way to create it — the underlying field would have to already exist on the destination some other way. **No live case of this has actually been confirmed** (see below — the one case investigated turned out to be something else entirely). Don't assume a `NOT_FOUND` means this without checking the next row first. |
| **A `NOT_FOUND` on `Get_Calculated_Fields` can mean "report-scoped calculated field, not yet promoted" — and once promoted, there is a real activation delay before the *same* WID resolves** | Workday's Report Writer supports calculated fields defined inline within a single report (never registered as a tenant-wide `Calculated_Field`, invisible to `Get_Calculated_Fields` for that reason — not a WSDL gap, this WSDL has no schema location for an inline report-scoped field's definition either, checked `Tenanted_Report_Column_DataType` and `Tenanted_Report_Definition_DataType` in full). **Confirmed live 2026-07-31** on `commitconsulting_dpt1`'s "PLNF - All Workers" report, column `CF_LRV_-_Home_State`, WID `da06ec2634331001f8e8b6fa2e4d0000`: initially `NOT_FOUND` (live PUT failed with `Invalid ID value ... is not a valid ID value for type = 'WID'`). After the user promoted it to a global calculated field in the Workday UI, it was **still** `NOT_FOUND` from both a targeted `Get_Calculated_Fields(wid=...)` and a full bulk index sweep immediately afterward — an activation delay, not a failed promotion. Some real (unmeasured, several minutes) time later, both the targeted lookup and a fresh bulk sweep found it — `Name: 'CF LRV - Home State'`, **the same WID it always had**. No new object was created. Once visible, the existing resolve → probe → create pipeline worked with zero code changes. **Consequence:** on a `NOT_FOUND` for a report's `External_Field_Reference`, ask whether it might be report-scoped before concluding it's out of scope — promoting it to global and retrying (allowing real time for activation, and rebuilding the CF index/cache fresh rather than trusting a sweep that ran too soon) may simply fix it. |
| **A filter condition's `Filter_Instances_Reference` (a fixed comparison value pointing at a specific business-object instance) fails the same way as an unresolvable field reference — and `Ignore_When_No_Target_Value` does NOT suppress it, despite the name** | `Condition_Item_DataType` has both `Filter_Instances_Reference -> wd:InstanceObjectType` (optional, `minOccurs=0`) and a sibling `Ignore_When_No_Target_Value: xsd:boolean`, undocumented in the WSDL (no `<xsd:documentation>` on either). **Confirmed live 2026-08-03** on `commitconsulting_dpt1`'s "Luke's Fancy Report": its top-level filter condition's `Filter_Instances_Reference` WID (`e5593fb2f0f41001b5a2ddb588c90000`) doesn't exist on the destination. Setting `Ignore_When_No_Target_Value=True` alongside the untouched reference and PUTting anyway **still failed live** with the identical `Invalid ID value ... is not a valid ID value for type = 'WID'` fault — ruled out empirically, not assumed. **Fix, also confirmed live**: since the field is optional in the schema, `migrate/writer.py:build_report_payload` now strips `Filter_Instances_Reference` (and `Ignore_When_No_Target_Value`, meaningless without it) from every filter condition on every report, unconditionally — same treatment as the owner reference. Read back afterward: the filter condition survives with everything else intact (operator, source field), just no default value. This is a fixed-value reference to *specific tenant data* (a particular Cost Center, Location, Worker, whatever the filtered field is on) — there is no generic "does this WID exist" operation to pre-check it, same underlying limit as the `External_Field` case above, so unconditional stripping (not a probe-and-decide) is the only reliable option. |
| **The services host is a property of the data center (pod), not the tenant — and there is no directory API mapping a tenant ID to one** | Every tenant on a given pod shares the same services host; only the URL *path* differs (`/ccx/service/{tenant}/...`). The only way to find a tenant's services host is to actually try — see `auth/endpoint_discovery.py`, which probes a curated list of known Implementation/Sandbox data centers with a real WSDL fetch. **Confirmed live 2026-08-03**: the `web` tenant's data center ("dc1" in the login-URL sense — plain `impl.workday.com`, no pod number) actually serves SOAP from `wd2-impl-services1.workday.com` — a "2" that does not appear anywhere in the login/UI host. There is no formula from UI host to services host; each data center's pattern has to be confirmed live once, independently, the same way `wd12`'s was. **Three data centers verified so far, and they split into two distinct naming families, not one formula**: `dc1` → `wd2-impl-services1.workday.com` (no `wdNN` in the login URL at all); `wd12` → `impl-services1.wd12.myworkday.com` and `wd501` → `impl-services1.wd501.myworkday.com` (both have an explicit `wdNN` pod number in their login URL, and both use the `.myworkday.com`-suffixed pattern — 2/2 so far for that shape). `wd501` was confirmed via `commitconsulting`'s REST API Endpoint page in Workday admin, the same way `web`'s was found — an initial guessed host for `wd501` (`wd501-impl-services1.workday.com`, following the `dc1` family by analogy) turned out wrong, didn't even resolve via DNS. The remaining unverified entries in `KNOWN_IMPL_DATA_CENTERS` (`wd3`/`wd5`/`wd10`/`wd102`/`wd103`/`wd105`) now each carry two candidate hosts — the `.myworkday.com` pattern tried first (now 2/2 for `wdNN`-numbered data centers), falling back to the `.workday.com` pattern — but neither is confirmed until discovery actually succeeds against them live. |
| **`classify_environment` only matched `impl` as a strict hostname prefix — not as its own hyphenated token — so a discovered tenant on a non-`wdNN.myworkday.com` pattern would misclassify as `UNKNOWN`** | `wd2-impl-services1.workday.com` (the `web` tenant's real host, see above) failed `first.startswith("impl")` since the label is `wd2-impl-services1`, not `impl-...`. `UNKNOWN` is treated exactly like `PRODUCTION` by the safety layer — meaning a tenant `endpoint_discovery.py` had *already* confirmed was Implementation/Sandbox (that's the only kind of data center it searches) would still hit an unsafe-destination guard. **Fixed 2026-08-03** in `config/targets.py`: `classify_environment` now also matches `impl` as an exact hyphen-split token (`"impl" in first.split("-")`), not just a leading prefix. Both confirmed real shapes (`impl.wd12.myworkday.com` and `wd2-impl-services1.workday.com`) now classify correctly as `IMPLEMENTATION`. |
| **A calculated field names the calculated fields it depends on by `Calculated_Field_Reference_ID` — a bare string with no WID anywhere — so a WID-only dependency walk misses almost half of them** | Confirmed live 2026-08-05 on `commitconsulting` (wd501). The "Add or Reference" blocks that carry a nested field (`Business_Object_Field`, `Condition_Field`, `Related_Field`, `Related_Business_Object_Field`, `Sort_Field`, `Default_Value_Field`, `External_Field`, and ~6 more) hold `Calculated_Field_Reference_ID` + `Calculated_Field_Name` + `Calculated_Field_Class_Name` as plain sibling strings, with the WID slot `Class_Report_Field_Reference` set to **null**. The only WID in the block is `Business_Object_Reference` — the business object the field *lives on*, not the field. **Measured: 612 of 1,399 fields (43.7%) reference another calculated field exclusively this way; 1,035 references, 100% resolvable in the index.** `migrate/ordering.py:extract_wid_refs` found none of them, so `resolve_closure` recorded the business-object WID as a pass-through and dropped the dependency — the dependent field then landed in the destination referencing a field that was never created. Found via "Jordan Demo" on this tenant: closure resolved to 2 objects when the correct answer was 4. **Fix:** `ordering.extract_reference_id_refs()` collects nested `Calculated_Field_Reference_ID` values (excluding the field's own top-level one) and `resolver.resolve_closure` maps them to WIDs through the index's reference-id lookup. Because these are business IDs, not WIDs, they are stable across tenants and `substitute_wids` must leave them alone — the dependency only needs to *exist* in the destination first, which correct ordering guarantees. A nested id absent from the index is recorded in `Closure.unresolved_reference_ids` and hard-blocks in the Resolve step: unlike an unmatched WID (usually a delivered object passing through), the payload states outright that the target is a calculated field, so not finding it is a genuine missing dependency. |

### Get_Calculated_Fields request structure
```
Get_Calculated_Fields_Request
  Request_References   (optional) — Calculated_Field references, by WID or Calculated_Field_ID
  Request_Criteria     (optional) — EMPTY type in the WSDL: no filtering is possible at all
  Response_Filter      (optional) — Page (int), Count (int, max 999) for pagination
  Response_Group       (optional) — Include_Reference (bool), Include_Calculated_Field_Data (bool)
```
Always set `Include_Calculated_Field_Data=True` in Response_Group, otherwise you get stubs.

`Request_References` and `Request_Criteria` are an XSD **choice** — send at most one, never both.

### Put_Calculated_Field request structure
```
Put_Calculated_Field_Request
  Calculated_Field_Reference   (optional) — omit to create; include to update existing
  Calculated_Field_Data        (required) — full field definition
```
The response carries `Exceptions_Response_Data` → `Exceptions_Data` → `Exception_Data{Classification,
Message}`. **Inspect it on every PUT.** A 200 with no SOAP fault does not mean the write succeeded.

Open question, unverified: whether a PUT *with* a reference does a full replace or a merge. Until
that is tested, treat UPDATE as unsafe and prefer CREATE/SKIP.

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

### ⚠️ The superseded algorithm — do not use
An earlier version of this file said:

```python
custom_source_wids = {field.wid for field in source_fields}   # WRONG
```

That is incorrect and was never live-tested. `Get_Calculated_Fields` returns **9,652 fields on this
tenant, most of them Workday-delivered**, mixed in with the custom ones. Live inspection found no way
to tell them apart from the payload: identical ID-type combos (`Calculated_Field_ID` + `WID`), no
flag, no `Do_Not_Use` signal, no naming pattern. Treating every returned field as custom would
classify ~9,600 delivered fields as needing migration.

### The algorithm — verified live 2026-07-31

Custom-vs-delivered is decided by **targeted existence probing against the destination**, not by
enumerating the source. A field is delivered (or already migrated) if it resolves in the destination;
it is custom-and-missing if it does not:

```python
# Probe: does this object exist in the destination?
dest_client.service.Get_Calculated_Fields(
    Request_References={"Calculated_Field_Reference": [
        {"ID": [{"type": "Calculated_Field_ID", "_value_1": reference_id}]}]},
    Response_Group={"Include_Reference": True, "Include_Calculated_Field_Data": True})
```

- **Resolves** → EXISTS. Capture the destination WID and seed `wid_map` with it.
- **Fault** `Invalid ID value.  'X' is not a valid ID value for type = 'Calculated_Field_ID'` → MISSING.
- **Any other fault** → UNKNOWN. Must NOT be collapsed into MISSING: "missing" means "create", and
  creating something that already exists is how you get duplicates. Block the run instead.

This one mechanism does both jobs — custom-vs-delivered discrimination *and* conflict detection — and
costs O(dependency closure) instead of O(9652).

Full flow:

```python
# 1. Build the source calculated-field index (Count=999 → 10 pages, ~25s). Cache it.
cf_index = build_cf_index(source_client)          # {wid: slim record}

# 2. User explicitly selects reports and/or calculated fields (no Config Package API exists).

# 3. Resolve the dependency closure. Report references are WID-ONLY with no type marker, so each
#    WID must be classified: present in cf_index → calculated field; otherwise probe once and cache.
closure = resolve_closure(selected, cf_index)

# 4. Topological sort → child-most calculated fields FIRST, building up. Cycles are a hard block.
ordered = topological_sort(closure)

# 5. Pre-flight against the DESTINATION (see probe above) → CREATE / UPDATE / SKIP per object.
#    Seed wid_map with the destination WIDs of everything that already EXISTS — otherwise a SKIP on
#    an existing dependency leaves downstream payloads pointing at a meaningless source WID.
plan = classify_against_destination(dest_client, ordered)

# 6. PUT loop — sequential, NOT parallel. Each response WID feeds the next payload.
for node in plan.ordered_nodes:
    remapped = substitute_wids(node.data, wid_map)
    if dry_run:
        log(f"DRY RUN: would PUT {node.name}")
        continue
    response = put_calculated_field(dest_client, remapped)
    # A HTTP-200 no-fault PUT can STILL have failed — see Exceptions_Response_Data below.
    check_exceptions(response)
    wid_map[node.source_wid] = response.Calculated_Field_Reference.WID

# 7. PUT report definitions — same wid_map applied to column/filter data, owner remapped.
```

### Identifying custom vs global WIDs
Decided per object by the destination probe above. A WID that resolves in the destination is
delivered or already migrated → pass through / use the destination WID. A WID that does not resolve
is custom → must be created, and downstream references to it remapped via `wid_map`.

### WID scanning approach
zeep returns response objects as nested dicts/objects. Use `zeep.helpers.serialize_object(response)`
to convert to plain Python dicts, then recursively walk for `ID` lists / `WID` keys. This handles all
sub-type variants without needing to know each sub-type's internal structure.

Note that report definition references are **WID-only** — roughly 36 of 41 references on a sampled
report carried just `{'WID': ...}` with no `type` discriminator. You cannot tell a calculated field
from a data source or business object by inspection; classify by index lookup, then by probe.

---

## Module interfaces

### config/targets.py — BUILT
```python
class Environment(str, Enum):   # IMPLEMENTATION | SANDBOX | PRODUCTION | UNKNOWN
    @property
    def is_safe_write_target(self) -> bool: ...
    # UNKNOWN is NOT neutral — safety.py treats it exactly like PRODUCTION.

@dataclass(frozen=True)
class TenantTarget:
    # Pure addressing. Carries NO credentials — targets get logged, hashed and
    # rendered in the UI, and a secret here would leak through all three.
    tenant: str; services_host: str; ui_host: str
    environment: Environment; services_host_derived: bool; raw_input: str
    def identity(self) -> tuple[str, str]:   # (host, tenant), case-normalised
    def endpoint(self, service: str, version: str) -> str
    def wsdl_url(self, service: str, version: str) -> str

def parse_tenant_url(raw: str) -> TenantTarget   # accepts pasted browser URLs
def target_from_parts(services_host: str, tenant: str) -> TenantTarget
def classify_environment(host: str, tenant: str) -> Environment
def derive_services_host(host: str) -> tuple[str, bool]   # (host, was_derived)
```
Classification is **host-driven**: a tenant *named* `acme_sandbox` on a production host still
classifies PRODUCTION. Only `impl.wdNN → impl-services1.wdNN` is a verified host mapping, so anything
derived is flagged `services_host_derived=True` for the UI to confirm by connection test.

### safety.py — BUILT
```python
@dataclass(frozen=True)
class WriteGuard:
    source: TenantTarget; dest: TenantTarget
    dry_run: bool = True          # callers must opt IN to writing
    plan_hash: str; confirmed_tenant_name: str; dry_run_reviewed: bool
    source_verified: bool; dest_verified: bool
    source_username: str; dest_username: str

def evaluate_guards(guard: WriteGuard) -> list[Guard]    # all findings at once
def blocking_guards(guard: WriteGuard) -> list[Guard]
def assert_write_allowed(guard: WriteGuard) -> None      # raises GuardViolation
```
Call `assert_write_allowed` immediately before **every** write, not once per run. It raises if
reached with `dry_run=True` — a dry run must never touch a write path at all.
Override for a non-impl destination is the `WDMIGRATOR_ALLOW_NON_IMPL=1` env var **plus** retyping
the tenant name. Only the exact string `"1"` counts. It does **not** unlock the same-tenant block.

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

WD_OX_SERVICE_NAME=Core_Implementation_Service
DRY_RUN=true
```

**`WD_WWS_VERSION` is no longer read.** As of 2026-08-03, the API version is
hardcoded to `DEFAULT_VERSION = "v46.0"` in `auth/client.py` — a version has
to work across whichever tenants a run touches (source and destination are
almost always different), and the max supported version isn't the same
everywhere: `v47.0` on `commitconsulting_dpt1`/`dpt5`, but only `v46.0` on
the `web` tenant (`v47.x`/`v48.0` both reject with "Invalid request service
version" there). `v46.0` is the highest version confirmed to work on every
tenant seen so far. If a future tenant doesn't support `v46.0`, lower it
here rather than reintroducing a per-run env override — every tenant in a
single run has to share one version regardless.

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
- [ ] **A real, distinct destination tenant exists.** As of 2026-07-31 `.env` points `WD_DEST_*` at the same tenant as the source (`commitconsulting_dpt1`, same host, same ISU). `safety.py` blocks live runs in that configuration; dry runs still work.
- [ ] Source ISU has Get on Configuration Set: Custom Reports and Fields + activated
- [ ] Destination ISU has Get and Put on Configuration Set: Custom Reports and Fields + activated
- [ ] All data sources referenced by reports exist in destination tenant
- [ ] Destination tenant version matches source (or is compatible)
- [ ] Report owner (System_User_Reference) remapping strategy confirmed
- [ ] Dry run executed and output reviewed before real run
- [ ] Whether `Put_Calculated_Field` with a reference replaces or merges is **still unverified** — until it is, prefer CREATE/SKIP over UPDATE

---

## What was discovered and ruled out
- **OX UI workflow / Configuration Packages**: not used — direct SOAP Get/Put is available
- **Workday REST API for report/field definitions**: does not exist
- **`Reporting_Analytics` SOAP service**: does not exist at v47.0
- **`Report_Metadata` service**: WSDL resolves and defines the right operations
  and version, but every live call is rejected with `Client.validationError` /
  "The web service or version is invalid for the requested operation" — for
  this ISU, on this tenant, regardless of domain security (Special OX Web
  Services + Custom Reports and Fields both confirmed granted+activated).
  Confirmed not an auth, IP, OAuth/API-Client, version, or code issue: the
  same ISU/version/transport succeeds calling `Get_Workers` on `Staffing`, and
  succeeds calling the identical `Get_Calculated_Fields` operation via
  `Core_Implementation_Service`. Root cause looks like an entitlement gap
  specific to the `Report_Metadata` service binding on this tenant — use
  `Core_Implementation_Service` instead (confirmed live 2026-07-30).
- **Public WWS directory**: does not list `Report_Metadata` — it is a restricted service
- **UI host for SOAP** (`impl.wd12.myworkday.com`): wrong — use services host (`impl-services1.wd12.myworkday.com`)
- **WSDL without version in URL path**: returns 404 on this tenant — version must be in path
- **v48.0 and above**: return HTTP 500 on this tenant — v47.0 is the current max supported version
