# Handoff — Dale Bot / Workday Migration Tool

_Last updated: 2026-08-03 (session 6)_

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

**Branch:** working directly on `master` now — `core-implementation-service-migration`
was merged 2026-08-03 via [PR #1](https://github.com/ladams-embark/dalebot_migrator/pull/1)
(opened and merged in the browser — `gh` CLI is not installed on this
machine), and this session's work (endpoint discovery, version hardcoding)
was committed straight to `master`, same as the small housekeeping commit
right after the merge. No open PR right now.

**State:** engine steps 1–8 are built and green. `ui/` (step 9, the Streamlit
wizard) is built: `app.py`, `state.py`, `runner.py`, `safety_ui.py`,
`secrets.py`, `components.py`, plus `steps/{connect,select,resolve,conflicts,
confirm,execute,results}.py`, root `streamlit_app.py`, `.streamlit/
config.toml`. `pyproject.toml` has the `ui` extra
(`streamlit>=1.40`, `pandas>=2.2`), installed in `.venv`.

**A live write has now succeeded — the first one this project has ever made.**
See "Done this session" below. `writer.py` is no longer unproven: a
calculated field and a report (with a correctly WID-remapped dependency
between them) were both created live on a genuinely distinct destination
tenant, `commitconsulting_dpt5`, and read back to confirm. The "no real
destination tenant" blocker from earlier sessions is resolved — `.env`'s
`WD_DEST_*` now points at `commitconsulting_dpt5`, not the source tenant.

```powershell
.\.venv\Scripts\Activate.ps1
pytest              # offline suite, no .env or network needed
pytest -m live      # read-only source-tenant tests
python scripts/selfcheck.py
```

**Next step: manual walkthrough of the Streamlit wizard end-to-end**
(`streamlit run streamlit_app.py`), then step 10 (`validation/verify.py`,
`cli.py`). The UI has been exercised live piecemeal (Connect, Select,
Resolve, Conflicts) via manual testing and bug fixes during this session, but
never all the way through Confirm → Execute → Results in the browser itself
— the successful live write above went through hand-written scripts in
`scripts/` (see below), not the wizard's own Execute step, because driving
credential-entry forms isn't something the assistant can do. Someone with
hands needs to walk the wizard itself through a live run at least once.

**"PLNF - All Workers" is resolved — it migrates cleanly now.** What looked
like a hard capability boundary (session 4's "New known limitation," see
CLAUDE.md's verified-facts table for the full corrected writeup) turned out
to be a **report-scoped calculated field that hadn't been promoted to
global**, not a `Custom_Field`-space object this tool can't touch. Once the
user promoted `CF_LRV_-_Home_State` to a global calculated field in the
Workday UI, it took a real activation delay (both a targeted
`Get_Calculated_Fields(wid=...)` and the bulk index sweep still returned
`NOT_FOUND` immediately after promotion) before it became visible **under
its original WID** — no new object, no code changes needed once it was
actually readable. Live-verified end to end: `CF LRV - Home State` created,
`PLNF - All Workers` created, the report column's WID correctly remapped to
the new destination CF, owner correctly set to `wd-support`.

A pre-flight check that tried to auto-detect "unmigratable" `External_Field`
references was built and then **reverted** during this same session — it
would have false-positived on "AE Previous Worker" (a report that already
migrates successfully live has a non-`Calculated_Field` `External_Field`
reference too — a delivered field that passes through fine). "Not a
`Calculated_Field` right now" cannot be reliably distinguished from "not a
`Calculated_Field`, ever" using anything in this WSDL — see CLAUDE.md.
**The only reliable move on a `NOT_FOUND` for one of these is to ask whether
it's a report-scoped calculated field that needs promoting to global, wait
for activation, and retry** — not to treat it as a hard block.

**A second, unrelated wall was found and fixed (2026-08-03): a filter
condition's `Filter_Instances_Reference`** (its fixed comparison value —
e.g. "Location = Austin Office" — a reference to a specific business-object
*instance*, not a field definition) fails the exact same way if that
instance doesn't exist on the destination. Tried
`Ignore_When_No_Target_Value=True` first, live, on the untouched reference —
**still failed**, same fault, ruled out empirically. Fixed instead by
stripping `Filter_Instances_Reference` (and the now-meaningless
`Ignore_When_No_Target_Value`) unconditionally in
`migrate/writer.py:build_report_payload`, the same treatment already given
to the report owner — the field is optional in the schema
(`minOccurs="0"`), so there's nothing Workday requires there. Live-verified
on "Luke's Fancy Report": created successfully, filter condition intact
minus its default value. See CLAUDE.md's verified-facts table for the full
writeup.

### Blockers and open questions

1. ~~No real destination tenant~~ — **resolved.** `commitconsulting_dpt5` is
   live-verified as a working, distinct destination.
2. **`Put_Calculated_Field` with a reference: replace or merge?** Still
   unverified — the live test so far only exercised CREATE (dest didn't have
   the field) and SKIP (dest already had it), never UPDATE. Until tested,
   continue treating UPDATE as unsafe and prefer CREATE/SKIP.
3. **Can `Data_Source_Reference` existence be probed in the destination?**
   Still open.
4. ~~Can the destination ISU own a report?~~ — **confirmed yes.** Verified
   live: `build_owner_reference(workday_username=<plain ISU username>)`
   (not the WS-Security-qualified `user@tenant` string) correctly sets
   `Tenanted_Report_Definition_System_User_Reference`, read back correctly
   after the write.
5. ~~`External_Field_Reference` on report columns can point outside this
   tool's scope~~ — **resolved for the case found.** It was a report-scoped
   calculated field needing promotion + activation time, not a genuine
   capability gap; see above. Genuine `Custom_Field`-space references (a
   plain field, never a calculated field) remain out of scope — no operation
   exists for those — but no live case of that has actually been confirmed
   yet, only theorized.

### Local machine state (not in git)

`out/cache/commitconsulting_dpt1/calculated_field.json` — 42+ MB, all
calculated fields (9,654 as of this session — it grows as fields get
promoted from report-scoped to global). Gitignored. Rebuilding costs ~30-40s;
`load_index()` reads it instantly. **Stale cache risk confirmed live this
session**: a just-promoted calculated field was invisible to a cache built
too soon after promotion. If a report's dependency isn't resolving and you
suspect it was recently changed in Workday, rebuild fresh rather than
trusting the cache's age alone.

### The approved build plan

`C:\Users\LucasAdams\.claude\plans\knowing-what-we-know-swirling-treasure.md`
holds the full architecture, the Streamlit design, and the safety model.

---

## Done this session (2026-08-03, session 6) — hardcoded v46.0, tenant-ID endpoint discovery

Triggered by trying to connect a genuinely new tenant (`web`) that didn't
match any of this tool's prior assumptions — surfaced two real gaps at once.

**Domain requirement corrected — was documented wrong.** CLAUDE.md's
Authentication section and the Connect step's own help caption both said the
ISU needs **Special OX Web Services**. Per the user's direct confirmation:
that's not actually required. What's required is **Get and Put on
Configuration Set: Custom Reports and Fields**, and — unlike what was
previously documented — both source and destination ISUs need *both*
permissions, not an asymmetric Get-only/Get+Put split. Corrected in three
places: CLAUDE.md's Authentication section and pre-flight checklist,
`ui/steps/connect.py`'s help caption, and `auth/client.py`'s
`_explain_failure` (the "web service or version is invalid" fault
explanation now names the right domain instead of saying "required domain
access" generically). Not independently re-verified live by this session —
recorded as the user's direct correction, same trust level as the REST
endpoint URLs that resolved `web` and `wd501` earlier.

**Quick-fill button added to Connect** (`ui/steps/connect.py`) — "Quick
fill: commitconsulting_dpt1" on both Source and Destination, since it's the
common-case tenant. Hardcodes the tenant ID and its known services host
(`impl-services1.wd12.myworkday.com`) directly in the UI code — confirmed
with the user this is fine despite CLAUDE.md's hard rule grouping "tenant
URLs" with credentials, since a bare tenant identifier isn't a secret and
is already throughout this repo's docs/tests. Deliberately does **not**
extend to username/password — those stay typed, never prefilled. Reuses
the exact same session_state-write pattern already proven live for
discovery's auto-fill, but **could not be re-verified live itself this
time** — the dev server failed to launch (`spawn EPERM`, an environment
issue unrelated to this change; `.claude/launch.json` and the venv's
`streamlit.exe` are both intact) both times it was attempted this session.
Worth an actual click-through next session before trusting it fully.

**API version hardcoded.** `WD_WWS_VERSION` is no longer read.
`DEFAULT_VERSION` in `auth/client.py` is now `"v46.0"` — confirmed live to
work on every tenant seen so far (`commitconsulting_dpt1`/`dpt5` max out at
`v47.0`, `web` maxes out at `v46.0`; a single run touches two tenants at
once, source and destination, so the version has to work on both, not just
whichever one is highest).

**New module: `auth/endpoint_discovery.py`.** Given just a tenant ID, tries
an actual WSDL fetch against a curated list of known Implementation/Sandbox
data centers (seeded from the user's own reference implementation for
finding a tenant's data center) until one answers — real network calls, not
inference from an unrelated URL. Key insight that shaped the design: **the
services host is a property of the data center, not the tenant** — every
tenant on a pod shares the same host, only the URL path differs — so once a
data center's host is confirmed once, it's confirmed for every tenant on
it. No caching (per explicit choice — probes fresh every time), impl/sandbox
only (matches the safety model; no reason to make discovering a Production
endpoint easy).

**Found live, same session, right after landing the first version**: the
initial `wd501` guess (`wd501-impl-services1.workday.com`, by analogy with
`dc1`'s naming) was wrong — didn't even resolve via DNS, caught when the
user tried `commitconsulting` (a tenant on `wd501`) and discovery correctly
came up empty rather than silently guessing. The real host, found via
`commitconsulting`'s REST API Endpoint page (same technique as finding
`web`'s), is `impl-services1.wd501.myworkday.com` — the **same naming
family as `wd12`**, not `dc1`'s. That's now 2/2 confirmed data centers with
an explicit `wdNN` pod number in their login URL using the
`.myworkday.com`-suffixed pattern, versus 1/1 for `dc1` (no pod number at
all) using the `wdN-impl-services1.workday.com` pattern. The remaining
unverified entries (`wd3`/`wd5`/`wd10`/`wd102`/`wd103`/`wd105`) now each
carry two candidate hosts, `.myworkday.com` tried first given the updated
odds, falling back to `.workday.com` — still unconfirmed either way, and
each needs its own live confirmation the same way `dc1`/`wd12`/`wd501` got
theirs. 3 of 15 seeded candidates are now actually live-verified; see
`KNOWN_IMPL_DATA_CENTERS`.

**Real bug found and fixed along the way**: `classify_environment` only
matched `impl` as a strict hostname *prefix*. The `web` tenant's real
services host, `wd2-impl-services1.workday.com`, has `impl` as its own
hyphenated token instead — so a tenant discovery had *already confirmed* as
Implementation/Sandbox would still misclassify as `UNKNOWN` and get treated
as risky/production by the safety gate. Fixed in `config/targets.py`; both
confirmed real host shapes now classify correctly.

**Two more real bugs found and fixed live in the browser** while wiring the
discovery flow into `ui/steps/connect.py`:
1. `st.expander` collapses back to closed on every rerun unless `expanded=`
   is passed explicitly — since the discovery job reruns repeatedly while
   pumping progress, the expander was hiding its own progress messages the
   moment they'd appear. Now tracked in `ConnectionState.discovery_expanded`.
2. Once a widget's `key` exists in Streamlit's `session_state`, passing a
   different `value=` on a later call to the same widget is silently
   ignored — `session_state[key]` is what actually drives the display from
   then on. The discovered URL wasn't reaching the "tenant URL" text field
   until this was fixed by writing directly to
   `st.session_state[target_widget_key]` instead of just the dataclass field.

**Verified live end to end, in the actual browser** (not just offline
tests): typed `commitconsulting_dpt1` into the new "Find services host"
field, clicked it, watched it correctly skip `dc1` and land on `wd12` (both
the target card and the URL text field updated correctly with the right
environment badge). 377 offline tests passing.

New offline tests: `tests/test_endpoint_discovery.py` (6 tests, `requests.get`
faked — no real network calls in the default suite), 2 new cases in
`tests/test_targets.py` for the `classify_environment` fix, 2 existing
`tests/test_auth.py` assertions updated for the new default version.

## Done this session (2026-08-03, session 5) — Filter_Instances_Reference fix

Second live wall found and fixed, unrelated to session 4's `External_Field`
finding. Migrating "Luke's Fancy Report" (`commitconsulting_dpt1` →
`commitconsulting_dpt5`) failed live on a filter condition's
`Filter_Instances_Reference` — a fixed comparison value (a specific
business-object instance: a particular Cost Center, Location, Worker,
whatever the filtered field is on) that doesn't exist on the destination.
Structurally different from the column-reference wall: that one was about a
*field definition* being invisible; this one is about a reference to
specific *tenant data*, which this tool was never going to create or verify
in the first place.

**Tested and ruled out `Ignore_When_No_Target_Value` empirically before
building anything** — the field sits right next to `Filter_Instances_Reference`
in `Condition_Item_DataType` and its name suggested it might suppress
exactly this validation. Wrote a one-off script
(`scripts/test_ignore_when_no_target_value.py`) that fetched the report,
recursively set the flag `True` on every filter condition carrying an
instance reference, left the reference itself untouched, and ran it
live. Same fault, identical WID. Confirmed via
`scripts/find_wid_in_report.py` that the failure really was the patched
field, not something else — first attempt actually failed on a *different*,
unrelated column reference first (the report also depended on a genuine
calculated field, `CF ESI - Workday`, that wasn't in the — again stale —
CF index cache; same "rebuild fresh" fix as session 4).

**Fix**: `migrate/writer.py`'s `build_report_payload` now calls a new
`_strip_filter_instance_references()` — a recursive walk (same pattern as
`extract_wid_refs`, not tied to a specific parent container) that removes
`Filter_Instances_Reference` and `Ignore_When_No_Target_Value` from every
filter condition, unconditionally. `Filter_Instances_Reference` is
`minOccurs="0"` in the schema, so this is a legal, well-formed payload, not
a workaround. 4 new offline tests in `tests/test_writer.py`
(`TestFilterInstanceStripping`). 368 offline tests total.

**Live-verified end to end**: "Luke's Fancy Report" created on
`commitconsulting_dpt5` (`dest_wid 3027e60674561000bcd934f424510000`), its
calculated-field dependency `CF ESI - Workday` created first
(`dest_wid 2bf676e597c31000bc25073b67c60000`), owner correctly `wd-support`.
Read back afterward: `Filter_Instances_Reference: []`,
`Ignore_When_No_Target_Value: False` — both cleanly absent — and the rest of
the filter condition (operator, source field) intact.

New diagnostic scripts: `scripts/test_ignore_when_no_target_value.py`,
`scripts/find_wid_in_report.py`.

## Done this session (2026-07-31, session 4, continued) — owner fixed to wd-support, External_Field investigation, PLNF resolved

**Report owner is now a fixed constant, not a per-run manual input.** Every
report this tool creates is owned by `wd-support`
(`ui/state.py:DEFAULT_REPORT_OWNER_USERNAME`), resolved via `WorkdayUserName`
at write time. Removed the owner-mode radio/text-input UI and the "no owner
set" blocker from `ui/steps/confirm.py` entirely — there's nothing left to
input. Live-verified: reads back as WID `545a0799733c40b7847399ade3039c64`
on `commitconsulting_dpt5`, exactly the WID originally supplied, confirming
`wd-support` resolves correctly by username even though it's invisible to
`Get_Integration_System_Users` (must be a different `System_User` subtype —
`Employee`/`Contingent_Worker` System User lookups have no filterable
criteria in this WSDL, so there's no way to search for it directly by name;
only the reference-on-write resolves it).

**Built, then reverted, a pre-flight blocker for "unmigratable" `External_Field`
references** (`migrate/resolver.py` gained `Node.unresolvable_external_field_wids`,
`migrate/planner.py`'s `validate_plan` gained a matching check). Reverted
after proving it produces false positives: "AE Previous Worker" — a report
already confirmed to migrate successfully live — has a column whose
`External_Field_Reference` is *also* not a `Calculated_Field` (a delivered
field that passes through fine). There is no signal available anywhere in
this WSDL that distinguishes "not a calculated field, and never will be"
from "not a calculated field, yet" — see the PLNF resolution below for why
that distinction matters. Git history has the full attempt and revert if
this is revisited; the code is back to matching origin exactly.

**"PLNF - All Workers" now migrates successfully — the External_Field wall
from earlier in this session was a false alarm, not a hard capability
boundary.** Full story:

1. The failing WID (`da06ec2634331001f8e8b6fa2e4d0000`, report column
   `CF_LRV_-_Home_State`) returned clean `NOT_FOUND` from
   `Get_Calculated_Fields`, live, with no fault — looked exactly like a
   `Custom_Field_ID`-space object this tool has no operation for.
2. User's hypothesis: it's a Workday **report-scoped calculated field**
   (defined inline via Report Writer, never registered as a tenant-wide
   `Calculated_Field`) rather than a `Custom_Field`. Checked the WSDL schema
   for `Tenanted_Report_Column_DataType` and `Tenanted_Report_Definition_DataType`
   in full (94 fields combined) for anywhere a report could carry an inline
   calculated-field definition — there is none, so even if the hypothesis
   were right, this WSDL still couldn't read or write it *as a report-scoped
   field*. That part of the investigation was a genuine dead end.
3. User promoted the field to a global calculated field in the Workday UI.
   Immediately after, both a targeted `Get_Calculated_Fields(wid=...)` and a
   full bulk index sweep still returned `NOT_FOUND` for the same WID —
   **an activation delay**, not a failed promotion.
4. Some real (unmeasured, at least a few minutes — several other diagnostic
   calls happened in between) time later, a fresh targeted lookup found it —
   `Name: 'CF LRV - Home State'`, **same WID as always**. A follow-up bulk
   sweep also picked it up. No new object was ever created; the same WID
   simply wasn't visible until activation finished.
5. Once visible, zero code changes were needed: `resolve_closure` picked it
   up as a genuine calculated-field dependency automatically (its WID now
   matched an entry in the refreshed `cf_index`), and the existing
   create-CF-then-create-report pipeline handled it exactly like "AE
   Previous Worker." Live-verified end to end: `CF LRV - Home State`
   created (`dest_wid f12b507378ef10020afc0840bcf80000`), `PLNF - All
   Workers` created (`dest_wid f12b507378ef10020afc268702070000`), the
   report's column correctly remapped to the new CF's destination WID, owner
   correctly `wd-support`. Both read back and confirmed correct.

**Diagnostic scripts added this session** (all read-only except the two
`migrate_*` ones, which are dry-run-first by design):
`scripts/diagnose_report_lookup.py`, `scripts/dump_plnf_report.py`,
`scripts/check_wid_as_cf.py`, `scripts/check_wid_as_system_user.py`,
`scripts/resolve_wd_support_account.py`, `scripts/find_cf_dependent_report.py`,
`scripts/find_cf_by_external_field.py`, `scripts/find_cf_by_name.py`,
`scripts/refresh_cf_index_and_search.py`, `scripts/classify_report_columns.py`,
`scripts/migrate_report_example.py`, `scripts/migrate_live_execute.py`,
`scripts/verify_dest_objects.py`. Worth keeping — `classify_report_columns.py`
in particular (per-column live classification, not cache-dependent) is the
right first move any time a report's dependency isn't resolving as expected.

## Done this session (2026-07-31, session 4) — ui/ built, first live write

Built the entire `ui/` package (step 9) per the approved plan: the Streamlit
wizard (`Connect → Select → Resolve → Conflicts → Confirm → Execute →
Results`), chunked runner (`ui/runner.py` — generators drained in
time-budgeted batches across reruns, `batch_size=1` for writes so a
cancel/refresh can't leave an object half-written), and safety-model
rendering (`ui/safety_ui.py`) on top of the untouched engine guards. Added
`ui = ["streamlit>=1.40", "pandas>=2.2"]` to `pyproject.toml`.

**Found and fixed two real bugs during manual walkthrough**, both traced with
read-only diagnostic scripts (now in `scripts/`) rather than guessed at:

1. **Reports added via "Add by exact name" carried no `Name`.** The UI reused
   `lookup_report_by_name()` — deliberately data-free, it's the cheap
   existence probe `planner.probe_node` uses against the *destination* — to
   pull a report's full definition from the *source*. With no `Name`, the
   report's own downstream existence probe then searched the destination for
   an empty string, which `lookup_report_by_name` correctly refuses (returns
   `UNKNOWN`, not `NOT_FOUND`) — collapsing what should have been a clean
   `NOT_FOUND` → `CREATE` into a blocking `UNKNOWN` → `SKIP`. One root cause,
   two symptoms. Fixed in `ui/steps/select.py`: resolve the name to a WID via
   `lookup_report_by_name`, then a second targeted `lookup_report(wid=...)`
   fetches the full definition. Regression tests in `tests/test_discovery.py`
   (`TestReportLookupByNameThenFullFetch`) pin the composition.
2. **A running Streamlit dev process can serve stale imported code** after
   editing a file under `src/wdmigrator/ui/` — the in-browser "Rerun" prompt
   reruns the script, but doesn't reliably bust `sys.modules` for the
   package's own submodules. A full process restart is what actually picks up
   the fix. Worth remembering for any future live UI debugging session.

**First successful live write against a real, distinct destination tenant**
(`commitconsulting_dpt5`) — everything before this was read-only. Two
objects, chosen because the report ("AE Previous Worker") depends on exactly
one calculated field ("AE CF LRV JA Previous Worker"), via hand-written
scripts (`scripts/migrate_report_example.py` for dry run,
`scripts/migrate_live_execute.py` for live, `scripts/verify_dest_objects.py`
to read the results back) rather than the wizard's own Execute step:

- Calculated field created first (child-most-first, per the topological
  sort) → new destination WID `f12b507378ef1002038ec7a812f70000`.
- Report created second, `wid_map` correctly substituting the calculated
  field's source WID with its new destination WID in the report's column
  data, owner remapped to the destination ISU → new destination WID
  `c832794e42dd100203a5ce5034030000`.
- Read back from the destination afterward: names, owner, and the
  remapped `External_Field_Reference` on the dependent column all correct.
- **Bug caught along the way:** `Connection.username` is the WS-Security
  *qualified* `user@tenant` string used for SOAP auth, not a plain username —
  passing it straight into `build_owner_reference(workday_username=...)`
  fails with `Invalid ID value ... not a valid ID value for type =
  'WorkdayUserName'`. Fixed by using the plain ISU username from `.env`
  instead. Only affected the throwaway scripts, not `writer.py` itself, but
  worth remembering if `ui/steps/confirm.py`'s owner-remap input ever
  defaults to something derived from `Connection.username`.

**Also found and documented a real scope boundary, not a bug**: a report
column's `External_Field_Reference` isn't always a `Calculated_Field`
WID — see "New known limitation" above and CLAUDE.md's verified-facts table.
Found via a live PUT failure on "PLNF - All Workers", traced by parsing the
bundled WSDL directly (`External_FieldReferenceEnumeration` lists
`Custom_Field_ID` as a sibling of `Calculated_Field_ID`) rather than guessing.

## Done this session (2026-07-31, session 3, continued on Sonnet) — step 8: api.py

Built `src/wdmigrator/api.py`, the last engine module — the facade `ui/` (step
9) will import instead of reaching into `auth`/`discovery`/`migrate`/`config`/
`safety`/`secrets` directly. Mostly a curated re-export of ~60 names under
their existing names (renaming an already-well-named function would just be a
second name for the same thing); two functions are genuinely new:

- `connect(target, username, password, role=...)` — takes plain strings and
  wraps the password in `Secret` internally, so `ui/` code never has to import
  `wdmigrator.secrets` just to authenticate.
- `resolve(cf_index, ...)` — a documented alias for `resolve_closure`. Kept as
  a separate name because the plan originally specified it as
  `iter_resolve_plan`, a generator; the resolver turned out to need zero
  tenant calls (session 3's earlier work), so there's nothing to report
  progress on and it stayed synchronous. `tests/test_api.py` pins that it is
  *not* a generator function, so a future change that makes it slow again is
  forced to reconsider the API shape rather than silently blocking a UI rerun.

`tests/test_api.py` (74 tests) checks three things beyond normal correctness:
1. Every re-exported name is the *actual* object from its source module
   (`is`, not just present) — catches a typo'd re-export shadowing the real
   symbol.
2. Every engine module (`api` plus everything under `auth`/`discovery`/
   `migrate`/`config`/`safety`/`secrets`/`validation`) parses via `ast` with no
   `import streamlit`/`import pandas` anywhere, and all of them still import
   successfully with `streamlit` import-blocked at the builtin level (simulates
   a bare CLI install with no `ui` extra).
3. The four long-running facade functions declare an `Iterator[...]` return
   annotation (checking the annotation, not `inspect.isgeneratorfunction` —
   these are thin wrappers that `return` a generator built elsewhere, so the
   function's own body has no `yield` and that check reports a false negative).

**Found two bugs in my own first draft of the streamlit-ban test**, both from
naive string matching rather than parsing: the substring check
`"import streamlit" in source` matched this very module's docstring ("must not
import streamlit or pandas"), and `inspect.isgeneratorfunction` on the
wrapper functions returned False even though calling them produces a real
generator. Fixed with `ast`-based import parsing and return-annotation
checking respectively — both are in the code now, not just noted here.

**Mutation-tested the streamlit ban**, and it's worth understanding the result:
injecting a real `import streamlit` into `planner.py` broke collection
immediately with `ModuleNotFoundError`, because streamlit isn't installed in
this venv at all — a stronger signal than my specific test firing, but it also
means the AST-based test hasn't yet been *positively* proven to fire in an
environment where streamlit *is* installed (i.e. after the `ui` extra is
added). Verified the AST logic directly against synthetic source strings
instead (plain import, from-import, and the docstring-mention false-positive
case) — all classified correctly. Re-check this test once streamlit is an
installed dependency in step 9, since that changes which failure mode you'd
actually hit.

316 offline tests total, 9 live read-only, `selfcheck.py` green.

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
