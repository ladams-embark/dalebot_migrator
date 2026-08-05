# UX and build review — 2026-08-05

Review of the wizard as it stands after the Commit rebrand, covering the whole
`Connect → Select → Resolve → Conflicts → Confirm → Execute → Results` flow and
the engine behind it.

**The engine is in good shape.** The generator-per-operation contract, the
gate-per-step navigation, the destination-probe approach to custom-vs-delivered
classification, and the re-checked write guard are all sound, and the reasoning
is documented where it happened rather than lost. Nothing below is a rewrite —
these are gaps between what the engine already knows and what the interface
tells the user.

Items marked **[done]** were fixed in this pass. Everything else is a
recommendation with the file it would touch.

---

## P1 — the ones with teeth

### 1. `INDETERMINATE` writes get no special treatment in the UI
`WriteRecord.needs_reprobe` exists (`migrate/writer.py:105`) and the status enum
carries a deliberate comment explaining why `INDETERMINATE` is not `FAILED`: a
transport failure on a PUT leaves the destination in an unknown state, and
retrying duplicates a committed object. **Nothing in `ui/` ever reads that
property.** `steps/results.py` renders it as one more value in a status column,
visually identical to a clean skip.

This is the most dangerous outcome the tool can produce, and it currently
whispers.

> **Fix:** in `steps/results.py`, hoist indeterminate records into a banner
> above the table — "N object(s) may or may not have been written; re-probe the
> destination before retrying any of them" — with a button that re-runs
> `iter_check_existence` against just those nodes.

### 2. Nothing is verified after it is written
`validation/verify.py` is still a docstring stub. The tool reports success from
the PUT response alone. That is weakest exactly where it matters most:
`Put_Tenanted_Report_Definition_ResponseType` has **no** `Exceptions_Response_Data`
block at all (`CLAUDE.md`), so for a report there is no in-band failure signal —
a read-back is the only real confirmation that the object exists and looks right.

> **Fix:** build `verify_fields_migrated()` / `verify_reports_migrated()` as
> generators, and add a "Verify against destination" panel to Results. This is
> step 5 of `docs/START_HERE.md` and is already on the pre-flight checklist.

### 3. A failed live run cannot be resumed
`steps/execute.py:29` passes `stop_on_failure=True` — correct, since continuing
past a failed dependency writes children that reference something that does not
exist. But there is no path forward afterward except restarting the wizard from
Select. Recovery works (a fresh probe finds the already-written objects and
plans them as SKIP), yet it means re-sweeping indexes and re-doing every
confirmation gate, which is exactly the state in which people start clicking
fast.

> **Fix:** keep `state.execute_records` and the accumulated `wid_map` on
> failure, and offer "Re-probe and resume" that rebuilds the plan for the
> unwritten remainder only.

### 4. Silent truncation in the report picker **[done]**
`_REPORT_MAX_ROWS = 5000` against a tenant holding ~5,150 reports — and the
filter ran *after* the cap, so up to 150 reports were unreachable no matter what
was typed. In a tool where selecting the wrong object cannot be undone, a picker
that hides rows without saying so is not a display detail.

Fixed in `steps/select.py`: filter first, cap second, and say so when the cap
bites. Same treatment for the 500-row calculated-field search.

### 5. Envelope inspector keyed on a non-unique name **[done]**
`steps/confirm.py` looked records up by `name`, but report names are documented
as non-unique (7 of 999 sampled shared one). Selecting the second of two
same-named reports showed the first one's envelope — on the screen whose only
job is confirming what is about to be written. Now keyed on `node_id`.

---

## P2 — workflow completeness

### 6. The plan cannot leave the app before it runs
Confirm inspects one envelope at a time; the CSV/JSON exports only exist in
Results, after the fact. There is no way to hand a plan to a colleague for
review, keep it as a change-request attachment, or diff two runs.

> **Fix:** a "Download plan (JSON)" button on Confirm, containing the ordered
> nodes, per-object action, existence state, and plan hash. The hash is already
> computed and is what pins the dry run — putting it in the export makes the
> reviewed artifact and the executed plan provably the same one.

### 7. Resolve does not say *why* an object is in the closure
`Node.required_by` exists and `Closure` already separates `selected_nodes` from
`pulled_in_nodes`, but only as counts. When a closure comes back with 40 objects
and someone wants to shrink it, "what dragged this in?" is unanswerable without
reading the payloads.

> **Fix:** add a "pulled in by" column to the migration-order table in
> `steps/resolve.py`.

### 8. A stale index has a known failure mode that the UI never names
Confirmed live: a calculated field promoted from report-scoped to global stays
invisible to `Get_Calculated_Fields` — and to a full bulk sweep — for several
minutes after promotion, under its original WID. So "dependency won't resolve"
has a specific, common, non-obvious cause: *your index predates the promotion*.

Index age is now displayed (`steps/select.py`), which is half the fix.

> **Fix:** when Resolve fails on a missing dependency, say the index's age in
> the error and offer a rebuild inline, rather than making the user infer it.

### 9. No CLI
`cli.py` was step 10 and is unbuilt. `api.py`'s generator contract was designed
to serve one ("it costs nothing for a CLI, which can just drain it in a loop"),
so this is mostly wiring. It would make dry runs scriptable and repeatable
migrations reviewable in version control.

### 10. Connection details are retyped every session
Correct for passwords — no argument. But `.env` already holds
`WD_SOURCE_SERVICES_HOST` / `_TENANT` / `_ISU_USERNAME` for both sides, and the
`scripts/` prototypes read them.

> **Fix:** an opt-in "Load from .env" that fills host, tenant, and username
> only, never the password. Worth doing deliberately and documenting: it moves
> tenant addressing from "typed each time" to "whatever the file says," which is
> a real change in how easy it is to point at the wrong tenant.

---

## P3 — polish

11. **Conflicts overrides need an explicit "Apply".** Changing dropdowns and
    navigating away silently discards them. Either apply on change, or block
    Next while the editor is dirty.
12. **A live run leaves no file behind unless someone clicks Download.** For an
    irreversible operation, always writing a timestamped record under `out/`
    would be cheap insurance.
13. **No ETA during Execute.** The rate limit is a known ~8 calls/sec, so
    remaining time is calculable rather than mysterious.
14. **The same-tenant warning only renders when both URLs parse.** Display-only
    — `safety.py` blocks the write regardless — but the warning is most useful
    while someone is still typing.

---

## Dead code removed in this pass

Found with `pyflakes` plus a call-site sweep; all confirmed unreferenced by both
`src/` and `tests/` before deletion.

| Location | Removed |
|---|---|
| `api.py` | unused `Iterator` import |
| `safety.py` | unused `Environment` import |
| `ui/state.py` | unused `Action`, `WriteRecord` imports |
| `ui/runner.py` | `pump(on_event=...)` parameter, `JobState.started_at` |
| `ui/secrets.py` | `redact_for_display()` |
| `ui/safety_ui.py` | `has_blockers()`, `blocker_count()` |
| `ui/components.py` | `step_nav()` (superseded by `theme.stepper`) |

`components.render_blockers()` was also dead, but as *duplication* rather than
surplus: `ui/app.py` and `steps/conflicts.py` each had their own inline copy of
the same rendering. Both now call the shared one.

---

## The rebrand, in short

- **`.streamlit/config.toml`** carries the brand natively — palette, 6px radius,
  and the four real Open Sans files via `[[theme.fontFaces]]`. Delete every line
  of custom CSS and the app is still recognisably Commit.
- **`ui/theme.py`** adds what native theming cannot reach: the header band, the
  step rail, status banners, cards, figures, and the cyan checkmark as the
  bullet glyph. It targets Streamlit by `data-testid` only — never the generated
  `.st-emotion-cache-*` names, which do not survive upgrades.
- **All 36 emoji and dingbats are gone.** Status is carried by color plus a
  3px top border rule; the Commit check is the only glyph.
- **Nothing is fetched from a CDN.** Fonts, logo, and the dotted-pixel pattern
  are served from `./static`, which matters on the locked-down networks these
  tenants sit behind.
- **`tests/test_ui_brand.py`** guards the mechanically checkable rules — no
  emoji, no `st.success`/`st.error`/`st.warning` (they ship emoji icons), only
  font weights 400/700, navy-tinted shadows, no left-border-stripe cards, assets
  present, no external URLs.

One trap worth recording, because it was hit and it fails quietly: **the
stylesheet must be re-emitted on every run.** Streamlit reconciles the DOM by
element position and removes anything a run does not re-produce, so an
"inject once" session flag renders correctly on first load and then drops back
to bare Streamlit chrome on the first click. Re-emitting does not stack
duplicates — the element is replaced in place. Verified in the browser: 70 rules
live, exactly one `<style>` tag, before and after a rerun.
