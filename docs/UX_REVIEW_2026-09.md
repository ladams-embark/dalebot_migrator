# UX review — 2026-09-11: the unaccompanied user

A second pass over the wizard, this time against one question: **can somebody
run a migration end to end with nobody sitting next to them?**

The August review (`docs/UX_REVIEW.md`) looked at correctness gaps between what
the engine knew and what the interface said. Most of its P1 list has since been
closed — `INDETERMINATE` now gets its own banner, `validation/verify.py` is
built and auto-runs on Results, "Re-probe and resume" exists, the plan exports
as JSON, `required_by` shows as a "why" column, and a run log is always written
to `out/`. The Scope step that landed on 2026-09-04 fixed the worst
navigation defect in the flow.

What is left is a different class of problem. The wizard is *safe* for a lone
user — the gates hold, `safety.py` is re-checked per write, and nothing starts
itself that writes. It is not yet *self-explanatory* for one. The flow assumes a
reader who already knows what a closure is, which Workday account types can read
a dashboard, and that a browser refresh will cost them forty minutes of work.
Every one of those assumptions is currently satisfied by a person standing
behind the user, and by nothing in the product.

Findings are ordered by how much they cost a solo user, not by effort. Each
names the file it lives in. Nothing here is a rewrite.

---

## Walkthrough: what a first-timer actually meets

Rendered offline through `AppTest`, plus the live app in a browser, so this is
the real element inventory rather than a reading of the source.

**Connect.** A branded header, a six-step rail, one line of instruction, then
two symmetrical credential panes. It looks good and it is obviously step 1 of 6.
It says nothing about what the tool does, what kind of Workday account is
needed, what will be written, or that nothing written can be taken back. There
are four competing ways to start — two "Quick fill" buttons, two "Find services
host" expanders, the URL fields themselves, and a stored-package loader below
the fold.

**Scope.** The cleanest screen in the app. Four checkboxes, each with a plain
sentence underneath explaining what comes along automatically. This is the model
the rest of the flow should be measured against.

**Select.** For a dashboard run: six index status lines, three section headings
("Source indexes", "Report catalog", "Destination matching"), two Build buttons
whose labels run to `Build source indexes (6 to build: a few seconds, a few
seconds, a few seconds, a few seconds, a few seconds, about 25 seconds)`, and
then — below all of it — the actual picker. While the sweeps run, the bottom of
the page shows two red *Fix:* banners, one of which ("Calculated field index not
built") resolves itself in twenty-five seconds with no action from anyone.

**Plan.** Three engine stages stacked behind dividers with their explanations
switched off (see finding 4). The user sees a figures row, an unexplained
"Recompute closure" button, another index status block, a CREATE/SKIP table they
may edit, a serialized-envelope inspector, and a checkbox that asks them to
certify they reviewed a dry run.

**Run.** The tenant-name retype, the irreversibility tick, and — often — a table
of tenant-data references demanding destination business IDs the user is
expected to go look up in Workday by hand.

**Results.** Counts, a per-object table, three downloads, an automatic read-back
verification, and a restore pass. The strongest step in the app.

---

## 1. A browser refresh destroys the session

`ui/state.py` keeps everything in one `WizardState` inside `st.session_state`,
and its module docstring is explicit that nothing may move to
`st.cache_data`/`st.cache_resource` — correct, for the credential-leak reason it
gives. The consequence is that **indexes are the only thing that survives**:
`ui/indexes.py` writes each completed sweep to disk via `save_index`, so those
come back. Selections, the resolved closure, the plan, action overrides,
reference decisions, the reviewed-dry-run stamp and the acknowledgements do not.

A refresh, a laptop sleep that drops the websocket, or a stray Streamlit "Rerun"
therefore costs a solo user everything but the catalogs — realistically the
forty minutes they spent picking 43 reports and answering 31 reference
questions. There is nobody there to say "don't press F5".

**Fix.** Serialize `WizardState` to `out/sessions/<id>.json` after every
mutating step, excluding `ConnectionState.password` and `.connection`, and offer
"Resume last session" on Connect (credentials get retyped; nothing else does).
`packages.py` already proves the closure round-trips, and `hydrate_wizard_state`
already handles a state object missing newer fields, so the hard parts are done.

## 2. The prerequisites exist only in `CLAUDE.md`

Four things reliably stop a migration, and the product mentions none of them
before the user hits them:

| Prerequisite | Where the user finds out today |
|---|---|
| Dashboards / prompt sets / prompt fields / time calculations need an **implementer** account | Step 3, after the sweep fails |
| Both ISUs need **Get and Put** on Configuration Set: Custom Reports and Fields | Step 5, when a write fails |
| Security changes need **Activate Pending Security Policy Changes** to take effect | Never — it reads as an intermittent permission bug |
| SOAP goes to the **services** host, not the UI host | A mismatch returns HTTP 500, which reads like an outage |

The last one is documented in `HANDOFF.md` as having been wrong in `.env` for
every session up to and including session 10, by people who wrote the tool.

**Fix, in two parts.** First, a "Before you start" panel on Connect carrying that
checklist — it is four lines and it is already written, in `CLAUDE.md`. Second,
and more valuable: **probe capability at connection test.** After
`verify_connection` succeeds, issue one `Get_Custom_Dashboards_without_Tabs`
with `Count=1` and classify the fault with the existing
`discovery/inventory.py:requires_implementer`. Render the answer as a chip on
the target card next to the environment pill: *Implementer* or *Standard ISU —
dashboards unavailable*. That moves the single biggest blocker in the tool from
step 3, after a sweep, to step 1, before anything.

This also fixes a side effect of Connect's auto-advance (`ui/app.py:42`): the
moment both sides verify, the user is thrown to Scope, so `status.detail` — the
only place the app says what it actually connected to — is on screen for one
render and then gone.

## 3. You cannot deselect one object

`ui/steps/select.py` banks highlighted rows additively into
`selected_reports_added`, `selected_dashboards_added` and `selected_field_wids`
(`_bank_payloads` / `_bank_wids`), and there is no per-item removal anywhere.
The only affordances are "Clear report selections" (`select.py:375`), "Clear
dashboard selections" (`select.py:457`) and "Clear calculated field selections"
(`select.py:263`) — each of which drops **everything** of that kind.

Pick twelve reports across four different search terms, notice the seventh was
the wrong `Span of Control`, and the remedy is to clear all twelve and redo the
search. The add-only design is right (it is what fixed selections vanishing when
the filter changed), but it needs an inverse.

**Fix.** The "Selected reports (N)" expander already enumerates the picks by
name. Make it an `st.data_editor` with a checkbox column, or put a small remove
control on each row. This is the cheapest item on the list and probably the most
frequently felt.

## 4. Plan suppresses the explanations of what Plan is doing

`ui/steps/plan.py` composes three modules:

```20:30:src/wdmigrator/ui/steps/plan.py
def render(state: WizardState) -> None:
    st.header("Plan")
    resolve.render(state, heading=False)
    if state.closure is None or state.closure_error:
        return
    st.divider()
    conflicts.render(state, heading=False)
    if state.plan is None or state.existence_job is not None:
        return
    st.divider()
    confirm.render_plan_review(state)
```

In both `resolve.py` and `conflicts.py` the explanatory caption sits *inside*
the `if heading:` branch. So the sentence explaining that resolve "expands your
selection into everything that must migrate with it, in child-most-first order"
and the sentence explaining that the probe is "real, targeted destination
traffic — one Get per object" are written, are good, and **never render in the
wizard**. They only appear in the standalone render paths the tests use.

The result is the densest screen in the app presented with the least prose: a
figures row, a "Recompute closure" button with no stated purpose, an index
status block, a CREATE/SKIP editor, an envelope inspector, and an attestation
checkbox — in that order, separated by dividers, with nothing saying they are
three different questions.

**Fix.** Decouple the caption from `heading`, and give each stage a
`theme.section` header so the page reads as three numbered questions: what has
to come along, what the destination already has, and what exactly will be sent.

## 5. The dry-run attestation has no rubric

The gate that stands between a plan and an irreversible write is:

```228:232:src/wdmigrator/ui/steps/confirm.py
    state.dry_run_reviewed = st.checkbox(
        "I have reviewed this dry run's output.",
        value=state.dry_run_reviewed,
        key="dry_run_reviewed_ack",
    )
```

The design around it is sound — the review is pinned to `plan_hash`, so any
override invalidates it automatically. But the app asks the user to certify they
read something without ever saying what a problem looks like. With a colleague
present, "look at the creates, and check those two cross-tenant matches" happens
out loud. Alone, this is a checkbox between the user and Start, and it will get
ticked.

**Fix.** Replace the bare checkbox with a short, generated "what to check" list
derived from the plan itself — it is all already computed:

- *N objects will be created in `dest_tenant`.* (`plan.counts()`)
- *M matched the destination on shape, not on business ID* — the weaker claim,
  already surfaced in `conflicts.py` but on a different step and behind an
  expander.
- *K references will be blanked* (the preflight set).
- *Any UNKNOWN existence result*, which already hard-blocks but deserves naming
  here too.

Then keep the checkbox. It means something once it is attached to a list.

## 6. Blockers cannot say "waiting on the machine"

`Blocker` (`migrate/planner.py:161`) is `node_id / title / detail / remedy`, and
`ui/app.py` renders every one identically as a warning banner with a bold
**Fix:**. During a Select sweep that produces two red banners, one of which
("Calculated field index not built") the user can do nothing about and which
clears itself in twenty-five seconds.

A lone user reads that as *something is wrong*. It is not — it is *not finished
yet*. Those need different visual treatment or the red ones stop meaning
anything.

**Fix.** Add a flavour to `Blocker` (a `waiting: bool`, or a small enum) and have
`app.py` render waiting blockers in the neutral tone with the estimate the index
layer already computes: "Building the calculated field index — Continue unlocks
automatically, about 25s left."

Related copy bug, same area: that blocker's text reads "even if you only
selected reports" (`select.py:838`) regardless of what was actually chosen. On
the dashboard path it is simply wrong, and it reads like the app has lost track
of the user's answer.

## 7. Select leads with bookkeeping instead of with the choice

Six status lines, three headings, two build buttons with runaway labels, and a
"Destination matching" section on a page titled *Select* — before the picker.
The destination sweep is genuinely required (`ui/indexes.py` explains why in
detail, and the reasoning is right), but nothing on screen tells the user why
reading the *destination* belongs on the step where they choose *source*
objects.

**Fix.** Collapse the whole index apparatus to one progress line plus an
expander — "Preparing catalogs — 3 of 6 done, about 30s remaining" — and put the
picker first. Say the destination reason once, in one sentence: "We also read
the destination so fields you already have are reused instead of duplicated."
The per-index detail stays available for anyone who wants it; it just stops
being the first thing on the page.

## 8. The reference-decision table is the hardest screen and has the least help

`ui/steps/execute.py:_render_reference_resolution` can present a solo user with
thirty-one rows, each wanting a "Replacement ID type" and a "Replacement value"
that exist in the *destination* tenant. The only guidance is a column tooltip:
"There is no generic way for this tool to list candidates, so look it up in
Workday."

The bulk actions (Blank all / Keep all / Clear), the safe automatic defaults, and
the "Also affects" column are all good work, and they carry most users through.
The people they do not carry are the ones with `replace_required` rows, which
cannot be blanked and have no automatic answer.

**Fix**, in descending order of value:

1. Show the source object's human-readable descriptor beside the raw identifier.
   The payload usually carries one; "Global Modern Services" is answerable,
   `Company_Reference_ID = GMS` less so, a bare WID not at all.
2. Name the Workday task that lists the ID in the caption, rather than saying
   "look it up in Workday".
3. **Let the answers be saved and reloaded per destination tenant.**
   `ReferenceDecision` is a plain dataclass keyed by source WID; a
   `reference-map-<dest_tenant>.json` next to the package directory would mean a
   repeat migration never retypes thirty-one company IDs. This is the single
   biggest time saver available to somebody doing this work more than once.

## 9. The app can load a package but can never make one

`api.py` exports `save_package` and `package_from_closure`; the only caller is
`scripts/migrate_package.py`. `ui/steps/connect.py` can *load* a stored package
and does it well. So a consultant who assembles a 59-object selection in the
wizard has no way to keep it, and the repeatability story — the one that turns
this from a one-off tool into something shippable to a client — is available
only from the command line.

**Fix.** A "Save this selection as a package" button on Plan, enabled once the
closure resolves. One button, against an API that already exists.

## 10. Quick fill ships one specific tenant, on both sides

```45:46:src/wdmigrator/ui/steps/connect.py
_QUICK_FILL_TENANT = "commitconsulting_dpt1"
_QUICK_FILL_SERVICES_HOST = "impl-services1.wd12.myworkday.com"
```

`_render_side` offers `Quick fill: commitconsulting_dpt1` identically on Source
and Destination (`connect.py:206`). On the destination side it fills the URL,
pulls `WD_DEST_ISU_USERNAME`/`_PASSWORD` out of `.env`, and — if both are
present — runs the connection test immediately. Both URL fields also carry a
`commitconsulting_dpt1` URL as their *placeholder*; on screen it reads as a
pre-filled default (the reviewer I had look at the live page reported it as
populated, not as placeholder text).

The comment above the constant is careful about credentials, and it is right
that a tenant ID is not a secret. The risk is different: this is a
prominent, one-click button that points the **write target** at a specific
tenant, shipped in the product, for anyone who is not this project's authors.

**Fix.** Drive it from an env var, hide the button when unset, and either drop it
from the destination pane or make it require a confirm there.

## 11. Going back silently discards approvals

Changing a selection on Select calls `reset_downstream(state, from_step="plan")`,
which clears the closure, the plan, the action overrides, the dry run, the
reviewed stamp, the typed tenant name and the irreversibility tick. That is
correct behaviour — a reviewed dry run must not outlive the plan it reviewed —
but it happens with no warning before and no notice after. A user who steps back
to add one more report returns to Plan and finds the gate reset, with no
explanation of what they did to cause it.

**Fix.** Say it. Either warn before ("Changing the selection discards the
reviewed dry run — you will need to review it again") or state it after, the way
Connect already does when credentials change to a different tenant
(`connect.py:89`). That banner is the right pattern and it exists.

## 12. When something breaks, the user has nothing to send anyone

`.streamlit/config.toml` sets `showErrorDetails = "none"` and `ui/app.py` wraps
every step render in a redaction boundary. Both are correct and should stay: a
zeep fault can carry a WS-Security password in cleartext. The side effect is
that a genuine bug reaches a solo user as "Unexpected error in the Plan step"
plus a redacted one-liner, with nothing to attach to a message asking for help.

**Fix.** Write the redacted traceback to `out/errors/<timestamp>.log` and print
the path in the banner. The redaction already happens; only the file is missing.

## 13. Smaller things

- **No plain-English statement of the outcome.** Plan shows counts by action and
  a hash. Nowhere does one sentence say: "This creates 12 calculated fields and
  3 reports in `dest_tenant`, reuses 105 objects that already exist, and deletes
  nothing." That sentence is what a lone user needs in order to decide, and what
  they would paste into a change ticket.
- **Results has no rollback worksheet.** The service has no delete operation, so
  backing out means a human deleting objects in Workday by hand. The three
  downloads are results CSV, results JSON, and a WID map keyed by reference ID —
  none framed as "here is exactly what was created; delete these to back out". A
  fourth export (kind, name, destination WID, business ID), labelled as such,
  costs almost nothing.
- **No end-to-end time expectation.** Per-index estimates
  (`indexes.py:BUILD_ESTIMATE`) and the execution ETA are both good. What is
  missing is the up-front one, on Scope, where the chosen kinds already
  determine the sweeps: "Reports means about 3 minutes of catalog building
  before you can pick anything."
- **Vocabulary is the engine's.** Closure, node, existence, conflicts, preflight,
  guard, shell dashboard, index, Put. Some is unavoidable (WID). Most is not,
  and a glossary in a persistent help expander would cost nothing. Note this
  does not conflict with the no-sidebar-navigation rule in `ui/app.py` — that
  rule protects the gating, which lives in the Continue button, not in the
  absence of a sidebar. A help-only sidebar is compatible with it.
- **Scope duplicates its own help text**, passing each string to both `help=`
  and `st.caption` (`scope.py:72-74`). Harmless, but it makes the tooltip
  pointless.
- **Maintenance:** every table and most buttons pass `use_container_width`,
  which Streamlit deprecated with a removal date that has now passed. 27 call
  sites across `ui/`. Worth a sweep before an upgrade takes the page down.

---

## Suggested order

Grouped by what they change, not by clock time.

**First — the things that lose work or hide a blocker.** Session persistence
(1), the capability probe at Connect (2), and per-item deselection (3). These
are the three that a lone user hits hardest, and none of them touches the engine
or the safety model. (1) is the most invasive: a serializer for `WizardState`
plus a resume affordance on Connect. (2) and (3) are localized to
`steps/connect.py` and `steps/select.py`.

**Second — make the two decision screens explain themselves.** Unhide the Plan
captions (4), give the dry-run attestation a generated rubric (5), and add the
waiting flavour to `Blocker` (6). (4) and (6) are small; (5) is a new render
block reading data the plan already holds.

**Third — the repeat-user leverage.** Saveable reference maps (8.3) and "save
this selection as a package" (9). Both are wiring to APIs that already exist,
and together they are what makes a second migration cheaper than the first.

**Anytime.** The quick-fill constant (10), the reset warning (11), the error log
file (12), and the smaller items in (13).

None of this asks the engine to change. The gates, the guard re-checks, the
generator contract and the dry-run-before-live rule are all doing their job —
this is about the interface saying out loud what the engine already knows.
