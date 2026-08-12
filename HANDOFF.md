# Handoff — Dale Bot / Workday Migration Tool

_Last updated: 2026-08-11 (session 10)_

## What this project is
A Python tool that migrates configuration (calculated fields, custom report
definitions, custom dashboards and their prompt sets) from a SOURCE Workday
tenant to a DESTINATION tenant via the
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

- `src/wdmigrator/` — `auth/`, `config/`, `discovery/`, `migrate/`, `ui/`,
  `validation/`, plus `api.py`, `safety.py`, `secrets.py`, `ratelimit.py`.
  **All built and green except `validation/verify.py`, which is still a stub.**
  `api.py` is the only module `ui/` imports.
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

**Branch:** `master`, and everything is merged into it. Session 10 landed
three commits on `claude/prepaid-certification-migration-ae7877`, each merged
`--no-ff` matching the pattern of every other feature branch here. **Nothing
is pushed** — `master` is ahead of the remote. `gh` CLI is **not installed** —
PRs, if wanted, must be opened in the browser.

**State: the tool migrates calculated fields, reports (including composites
and matrix reports), calculated measures, custom dashboards and prompt sets,
end to end, live-verified.** Engine and `ui/` are both built. **571 offline
tests green** (`pytest` reports `571 passed, 9 deselected`); no `.env` and no
network needed.

> The "613 offline tests" figure carried by this file through session 9, and
> the "618" briefly written during session 10, were both wrong — never
> measured, only inferred from pytest's progress dots. 571 is the number
> `pytest` actually prints. Quote the summary line, not the dots.

```powershell
.\.venv\Scripts\Activate.ps1
pytest              # offline suite, no .env or network needed
pytest -m live      # read-only source-tenant tests
python scripts/selfcheck.py
```

### Tenants — read this before running anything

**The destination changed in session 10.** It was `commitconsulting` @ wd501
through session 9. As of 2026-08-11 `.env` names:

| | tenant | services host |
|---|---|---|
| source | `commitconsulting_dpt1` | `impl-services1.wd12.myworkday.com` |
| destination | `commitconsulting_dpt3` | `impl-services1.wd12.myworkday.com` |

Do not assume either half of that pair from memory — read `.env`, then verify
the pairing live. Hosts confirmed so far: `commitconsulting_dpt1`, `_dpt3` and
`_dpt5` are all on `impl-services1.wd12.myworkday.com`; plain
`commitconsulting` is on `impl-services1.wd501.myworkday.com`.

**`.env` has had the host/tenant pairing wrong in every session so far**, and
still did at the start of session 10 — it pointed *both* tenants at `wd501`
when both were on `wd12`. Session 9 had it pairing `commitconsulting_dpt1`
with `wd501` and `commitconsulting` with `wd2-impl-services1.workday.com`. A
mismatch returns HTTP 500 on the WSDL fetch, which reads like an outage rather
than a config error, so **check this first on any connection failure — it has
never once been the code.** Confirm a pairing by fetching
`https://{host}/ccx/service/{tenant}/Core_Implementation_Service/v46.0?wsdl`
and looking for HTTP 200 with a ~5.5 MB body.

⚠️ **As of the end of session 10 the two `.env` host lines are still wrong**
— both read `impl-services1.wd501.myworkday.com` and both need to be
`impl-services1.wd12.myworkday.com`. Session 10 ran with in-process overrides
rather than editing the credentials file. Fix them before the wizard will
connect.

**The destination refreshes.** `commitconsulting` was refreshed overnight
during session 9 and lost 24 migrated objects. That is normal for an
implementation tenant and not a bug — but it means a "the destination already
has this" assumption goes stale without warning. The planner's destination
probe handles it correctly (everything reverts to CREATE), and a full
destination sweep is the reliable check when a probe result looks surprising.

**Dashboards require an implementer account on both tenants.** `ladams-impl`
works. A normal ISU (`lmcneil`) fails every dashboard operation with
`Processing error occurred. The task submitted is not authorized.` while
reading calculated fields and reports perfectly well. This is an account-type
gate, not a domain grant — see CLAUDE.md. Reports and calculated fields are
unaffected, and the UI explains the limitation once rather than surfacing a
raw fault.

### Next steps, roughly in order

0. **Three dashboards are fully planned and NOT written.** `Commit - HR
   Dashboard`, `Commit - Optimize Reporting Dashboard` and `Commit - Open
   Enrollment Command Center`, dpt1 → dpt3. Final plan: **99 CREATE / 66 SKIP,
   zero blockers, dry run clean, plan hash `202e8a742a6b91d8`.** Four live
   attempts in session 10 wrote **zero** objects — two halted on the WQL alias
   collision at object 1, the rest were dry runs, and the last was stopped by a
   permission prompt rather than anything in the tool. dpt3 is untouched.
   Re-probe before running: the plan hash is only valid against the destination
   as it was on 2026-08-11.

   Two things this run will exercise for the first time ever: **prompt sets
   have never been written to a tenant** (2 of them here, and
   `writer._map_prompt_set_members` has never executed — see open question 3),
   and the cross-tenant calculated-field matching added this session decides
   62 of the 165 objects.

1. **Drive the Streamlit wizard end to end by hand.** Still never done, and
   session 10 raised the stakes: every live migration this project has
   performed went through scripts in `scripts/` or the scratchpad, never the
   wizard's own Execute step, because driving credential-entry forms is not
   something the assistant can do. The dashboard picker (session 9) and the
   rewritten report/dashboard selection banking (session 10) are covered by
   `AppTest` only. **Session 10 fixed a bug that made it impossible to select
   objects across two different searches, and nobody has confirmed by hand
   that the fix feels right** — the tests pin the state contract, not the
   interaction. Someone with hands needs to do this once.
2. **Migrate an untabbed dashboard.** Only the tabbed flavour has been
   written live. `Cost Center Manager Dashboard` on the source resolves
   cleanly to 42 objects **including a prompt set**, so it exercises both the
   untabbed Put and the prompt-set write path — neither of which has ever run
   against a tenant.
3. **`validation/verify.py` is still a stub.** Every read-back this project
   has done was hand-written in a throwaway script.
4. **Report tags could be migrated properly.** They are currently stripped
   from every report because they always fail cross-tenant, but
   `Get_Report_Tags` / `Put_Report_Tag` both exist, so this is a clean
   follow-up rather than a limitation.

### Blockers and open questions

1. **`Put_Calculated_Field` with a reference: replace or merge?** Still
   unverified after ten sessions. Live runs have only exercised CREATE and
   SKIP for calculated fields, never UPDATE. Keep preferring CREATE/SKIP —
   session 9 deliberately excluded calculated fields from a forced-rewrite
   pass for exactly this reason, and session 10's run was 1 CREATE / 5 SKIP.
2. **Can `Data_Source_Reference` existence be probed in the destination?**
   Still open.
3. **`Prompt_Set_Member__All__Reference` remapping is unproven.**
   `writer._map_prompt_set_members` reads a written prompt set back and maps
   member WIDs by `Prompt_Set_Member_ID`, because `Put_Prompt_Set` returns
   only the set's own reference. No prompt set has ever been written, so this
   has never run. See next step 2.
4. **Genuine `Custom_Field`-space references remain out of scope** — no
   operation exists for them — but still no live case has been confirmed,
   only theorized. Every `NOT_FOUND` investigated so far turned out to be a
   report-scoped calculated field needing promotion, or a delivered field
   passing through fine.

### Local machine state (not in git)

`out/cache/<tenant>/{calculated_field,report,dashboard,prompt_set}.json` —
gitignored. The calculated-field index is the big one (~44 MB, **9,734 fields
on the source as of session 10**, up from 9,717 in session 9; it grows as
fields are promoted from report-scoped to global). Dashboard and prompt-set
indexes are trivial (179 and 57 items, one page each). Rebuilding the CF index
costs ~25s; `load_index()` reads it instantly.

**Stale cache risk, confirmed live three times** — and session 10's instance
cost most of the debugging time for a migration that turned out to have
nothing wrong with it. A calculated field that exists in the tenant is absent
from an index swept before it appeared, and `resolve` then hard-blocks
reporting it as a genuinely missing dependency, which is indistinguishable
from a real one. In session 10 a 4-day-old index made
`Prepaid Spend Amortization Installment - CF LRV - Supplier Invoice
Accounting Date - 1212285275` look missing; a fresh sweep found it
immediately. **If `unresolved_reference_ids` is non-empty, rebuild the index
before believing it.** Do not trust a cache's age.

### The approved build plan

`C:\Users\LucasAdams\.claude\plans\knowing-what-we-know-swirling-treasure.md`
holds the full architecture, the Streamlit design, and the safety model.

---

## Done this session (2026-08-11, session 10) — Prepaid Certification migrated, picker selection bug fixed

Two pieces of work: a live report migration to a **new destination tenant**,
and a UI bug the migration had nothing to do with. Commit `2f61065`, merge
`e716e6d`. 618 offline tests.

### `Prepaid Certification` migrated live, dpt1 → dpt3

The user reported it "failing". **Neither cause was the report**, and both are
environment conditions this handoff had already warned about in general terms:

1. **Both `.env` services hosts pointed at the wrong pod** — `wd501` for two
   tenants that are both on `wd12`. Presents as HTTP 500 on the WSDL fetch.
2. **The cached calculated-field index was 4 days stale**, so `resolve`
   hard-blocked claiming a real calculated field was a missing dependency. A
   fresh sweep (9,717 → 9,734 fields) resolved it with no code change.

With both corrected the migration was unremarkable: closure of 6 objects
(3 reports, 3 calculated fields), **1 CREATE and 5 SKIP** — every dependency
already existed in dpt3. Plan hash `f6d6a0fcbfc6027f`, re-verified on re-probe
before the live write. Result 1 success / 0 failed / 0 indeterminate. Dest WID
`332111f5bc6610011af1dadfd9750000`.

`Prepaid Certification` is a **composite** report — no columns of its own, it
renders two sub-reports. Read-back confirmed every field matches the source
except `Shared` (stripped deliberately; not a dashboard worklet), both
composite sub-report references point at destination WIDs, and no migrated
dependency's source WID survived anywhere in the payload.

Worth knowing: this migration leaned on pre-existing destination state. The
three calculated fields belong to the sub-reports, not to the composite
parent, and all five dependencies were already in dpt3. **A refresh of dpt3
would make this a much larger run.**

### The picker selection bug — reports *and* dashboards

Selecting reports across more than one search term was impossible. The report
and dashboard pickers rebuilt their selection every rerun by reading
`st.dataframe`'s live selection, which is **a list of row positions into the
frame the widget was just handed**. Retyping the filter above the table
repointed those positions at different objects, or at nothing, silently
discarding everything picked under the previous search.

`state.py` had encoded the wrong assumption outright — *"the report index
table's own widget state already persists its row selection across reruns"* —
true only while the frame is unchanged, which is exactly not the case when
filtering.

Both now bank on an explicit add button into a persistent dict, the pattern
`_render_calculated_fields` already used and which is why that picker never
had the bug. `selected_reports_manual` → `selected_reports_added`;
`selected_dashboards_added` is new.

Since selections are no longer visible as highlighted rows once the filter
moves on, each picker lists what it holds by name — picking the wrong object
cannot be undone in the destination. The dashboard picker also gained the
clear button it never had.

**Audited every other widget read back into state.** The two `st.data_editor`
call sites are safe and were left alone: neither has a filter that can reorder
its frame, `conflicts.py` applies edits keyed on `node_id`, and
`execute.py:_apply_decisions` already documents why positional matching holds
there. `_render_calculated_fields` was already correct.

Five new `AppTest` cases. Note the standing limitation — the Select step
cannot be reached in a browser without tenant credentials, so this is
`AppTest` coverage only.

### Cross-tenant calculated-field matching — the big one

Migrating three dashboards to dpt3 surfaced a defect that had been latent since
2026-07-31, and it is the most consequential thing found this session.

**`Calculated_Field_Reference_ID` is not a stable cross-tenant identity.**
CLAUDE.md had asserted it was since the read path was first verified. It is
stable only when both tenants acquired the field the same way, and dpt1 and
dpt3 have completely different conventions —
`CRTMNU01_Commit - HR Dashboard_03_Is Top Performer` against
`Custom Object Data - Is Top Performer` for one and the same field.

The destination probe therefore reported **62 of 110 calculated fields as
absent when they were present**. The original plan was 164 CREATE / 1 SKIP;
the correct plan is 99 / 66. Running the first one would have put 62 duplicate
fields into dpt3, on the same business objects, with no delete operation.

**How it surfaced** is worth remembering, because the symptom pointed nowhere
near the cause. Object 1 of the live run failed with `Enter a unique WQL alias
for the business object using zero through 9, A through Z, a through z, or the
_ symbol` — which reads as a character-set complaint and is a **uniqueness**
complaint. The alias was entirely legal characters. Of 9,734 source fields,
zero had an illegal character.

**The first fix considered was wrong and was rejected on the user's challenge.**
Stripping `WQL_Alias` (it is `minOccurs="0"`, so legal) would have made all 164
writes succeed — and produced 62 duplicates, because a colliding alias is
nearly always evidence that the destination already *has* the field. Across all
62 live collisions, **zero** turned out to be an unrelated field squatting on
the alias. The lesson generalises: an error that can be silenced by deleting a
field from the payload deserves a check on whether the error was telling you
something true.

`planner._match_calculated_field_across_tenants` re-checks an ID miss in three
tiers, returning UNKNOWN rather than guessing whenever a tier finds more than
one candidate — see CLAUDE.md for the tiers and their justification. Opt-in via
`iter_check_existence(..., match_index=...)` since it costs a destination sweep.

**One attempted change was reverted.** Forcing an UNKNOWN object to CREATE via
`overrides` trips `validate_plan`, so `MigrationPlan.overridden` was added to
let a human override bypass that blocker — until two existing tests
(`test_writing_an_unknown_object_blocks`,
`test_writing_an_ambiguously_named_report_is_blocked`) showed the rule was
deliberate, not an oversight. The right encoding is to adjudicate **existence**
rather than the action, which is what the run script now does for the two
`Year-Month` fields. **Do not weaken that blocker.**

15 new offline tests in `test_planner.py`.

---

## Done this session (2026-08-07, session 9) — custom dashboards, live end to end

Added custom dashboards as a third selectable object kind, with prompt sets
and worklet reports as dependencies. **`Commit - Optimize Reporting Dashboard`
migrated end to end, `commitconsulting_dpt1` → `commitconsulting`, 25/25
objects, both tabs populated, confirmed by read-back.** Commit `a9a28fd`,
merge `6353478`. 613 offline tests.

**Phase 0 first: probed the WSDL and the live tenant before writing code.**
`scripts/probe_dashboards.py` (read-only, kept) answers the questions the
schema cannot. Four findings reshaped the design before a line of engine code
was written:

- Dashboards are **two unrelated object types** — tabbed is a
  `Custom_Landing_Page_Group`, untabbed a `Custom_Landing_Page` — with
  separate Get, Put, data block and ID space, and nothing in a reference says
  which you have. Both are swept. 52 untabbed + 127 tabbed, one page each.
- Unlike `Custom_Report_ID`, a dashboard's business ID **does** work as a
  lookup key, so dashboards get real cross-tenant identity.
- **`Prompt_Set_Request_Criteria` is unusable, both fields.** The
  report-scoped one is accepted and silently ignored (three different reports,
  all 57 prompt sets each time); the dashboard-scoped one is typed to the
  untabbed object and rejects a tabbed dashboard outright. So prompt sets are
  indexed, not loaded on demand.
- **Dashboards require an implementer account.** A normal ISU fails every
  dashboard operation with "The task submitted is not authorized" while
  reading 9,716 calculated fields fine. Different failure from the
  `Report_Metadata` one — that says the *binding* is invalid, this says the
  *account* is not allowed. `discovery/inventory.py:requires_implementer`
  detects it so the UI explains it once.

**Five more things only the live write could find**, in the order they broke:

1. **Report tags always fail.** Blocked object 18 of 25;
   `Custom_Report_Tag_ID` is tenant-specific by construction and 5 of the 10
   reports shared one. Now stripped from **every** report, per the user's
   explicit instruction, not just dashboard ones.
2. **Matrix measures and dimensions** are created *inline* on a sub-report via
   `Matrix_Measures_Data`, so their destination WIDs are never reported back
   and `Matrix_Measure_DataType` has no reference element to discover them
   from. Read-back proved the measure was already in the destination under the
   same business ID with only a stale WID. `_INLINE_CHILD_REFERENCES` drops
   the dead WID and lets the business ID resolve.
3. **Metadata security groups do not resolve cross-tenant.** The session
   started by keeping `Workday-Delivered_Security_Group_Reference` on the
   reasoning that a delivered business ID resolves anywhere — wrong, disproved
   live on `implementers_wkdyGroup`. Both tenanted and delivered security
   groups are now stripped; Workday repopulates the destination's own defaults.
4. **The dashboard and its worklet reports are mutually dependent**, and both
   ends are validated at write time — the dashboard rejects a worklet whose
   report does not name it, and the report cannot name a dashboard that does
   not exist yet, *not even by stable business ID*. Broken with a three-phase
   write (`_defer_dashboard_worklets`): shell with no worklets → re-write the
   reports naming the real dashboard → dashboard complete with `Add_Only`
   dropped. Same shape as `_defer_summary_calculations`.
5. **A dashboard worklet must be a `Shared` report.** This one cost the most.
   With `Shared=False` every worklet was rejected as "not valid for the
   assigned dashboard", *even as the dashboard's only worklet*. Ruled out
   first, each by direct test: the landing-page association (present and
   correct), WID mapping (25 mappings, 0 unresolved), activation delay
   (minutes), worklet capacity (fails with 1 against `Max_Worklets_Allowed=6`),
   the security domain (the source's WID resolves on **both** tenants as
   `Custom Report Administration` — a delivered global), and tenant-scoped
   config IDs (`-6-*` vs the destination's `-3-*`; stripping changed nothing).
   `Shared` is **not** the same as the `Restricted_to_*` references, which
   stay stripped — so a worklet report lands shared with no inherited
   restrictions.

**Also corrected a false dependency cycle.** `Worklet_Landing_Page_Reference`
was being read as a back-pointer and stripped; it is actually the declaration
that a report may appear on a dashboard. The writer strips it, so the resolver
must ignore it too or `topological_sort` hard-blocks on
`dashboard → report → dashboard`. The field list lives in
`resolver.WORKLET_BACKREF_FIELDS` and is imported by the writer so the two
cannot drift. **General rule this is an instance of:
`resolver._dependency_payload` must reflect what the writer will actually
send, not what the source returned.**

**Destination refreshed overnight mid-session**, wiping 24 already-migrated
objects. Caught by a full destination sweep rather than trusting the probe —
worth repeating, since re-creating objects that already exist is unrecoverable
here. The re-run was clean because the planner correctly reverted everything
to CREATE.

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
