"""Live execution — the only step that can write to a tenant.

Composed into the Run step. ``batch_size=1``: at most one object is pulled
from the generator per Streamlit rerun, regardless of how much of the pump
time budget is left. Pause and Cancel are serviced between :func:`pump`
calls, i.e. always between objects — a browser refresh or a click mid-run
can never leave an object half-written. The engine re-checks
``assert_write_allowed`` inside ``write_node`` before every single write,
not just once here.
"""

from __future__ import annotations

import streamlit as st

import pandas as pd

from wdmigrator.api import (
    Blocker,
    BlockingReference,
    GuardViolation,
    ReferenceAction,
    ReferenceDecision,
    WriteStatus,
    build_plan,
    find_preflight_reference_candidates,
    find_reference_sites,
    TIME_TRACKING_KINDS,
    TIME_TRACKING_SERVICE_NAME,
    iter_check_existence,
    iter_execute,
)
from wdmigrator.ui import theme
from wdmigrator.ui.components import render_job_progress
from wdmigrator.ui.indexes import _format_duration, destination_match_indexes
from wdmigrator.ui.runner import READ_TIME_BUDGET, WRITE_TIME_BUDGET, pump, start_job
from wdmigrator.ui.state import WizardState, build_guard, owner_reference
from wdmigrator.ui.steps import confirm

STEP_ID = "execute"


def _blocked_record(state: WizardState):
    """The failed record naming a reference the destination could not resolve.

    Only ``Invalid ID value`` faults and exceptions produce one — a schema error
    or an entitlement problem is not fixable by substituting a reference, and
    must not be offered as though it were.
    """
    for record in reversed(state.execute_records):
        if record.blocking_reference is not None:
            return record
    return None


def _other_objects_referencing(state: WizardState, value: str, exclude_node_id: str) -> list[str]:
    """Every OTHER object in the plan whose payload also names ``value``.

    A read-only, in-memory scan of what is already loaded — no tenant call.
    It exists purely to SHOW the reach of a decision before it is made, not to
    create that reach: ``_apply_reference_decisions`` in the writer already
    substitutes a decided WID everywhere it appears in any node's payload,
    because every payload is built with the full, accumulating
    ``plan.reference_decisions`` map, not just the entry for whatever failed
    first. So a decision here already answers every occurrence of this exact
    value once — this listing is what lets the user see that up front instead
    of taking it on faith.
    """
    names = []
    for node in state.plan.ordered_nodes:
        if node.node_id == exclude_node_id:
            continue
        if find_reference_sites(node, value):
            names.append(node.name or node.node_id)
    return names


def _required_replacements_missing(state: WizardState) -> list[str]:
    """Names of preflight entries flagged replace-required with no valid decision.

    "Valid" means a REPLACE decision carrying both a replacement type and
    a value. A BLANK decision on a required row is refused because the
    schema rejects it — surfacing it here is what lets the wizard block
    Start instead of letting Workday reject the whole batch.
    """
    missing: list[str] = []
    for value, info in state.blocking_references.items():
        if not info.get("replace_required"):
            continue
        decision = state.reference_decisions.get(value)
        if (
            decision is None
            or decision.action is not ReferenceAction.REPLACE
            or not decision.replacement_type
            or not decision.replacement_value
        ):
            missing.append(info.get("node_name") or value)
    return missing


def _populate_preflight_references(state: WizardState) -> None:
    """Enumerate always-tenant-data references before the first write attempt.

    The old fault-driven loop learned about one bad reference at a time — write,
    fail, decide, restart, write, fail on the next, restart. A report with 31
    default Companies took 31 round trips just to get past parameter defaults.
    This walks every payload in the plan once and pre-fills the same table the
    fault loop uses, keyed by source WID, so the user can bulk-blank the whole
    set in one pass before the run starts.

    Blank is a safe default and gets applied immediately, not on an "Apply"
    click — a user who takes no action still gets a working migration. The
    plan is rebuilt with those decisions so ``Start live execution`` uses the
    blanked payload rather than the source-WID-carrying one that would fail
    on the first Instance_Reference.

    Guarded by ``plan_hash`` so it re-runs only when the plan changes shape —
    an override, a new decision, an added node. Never removes an entry: a
    reference the fault loop discovered later stays in the table alongside the
    pre-flight ones (both share the same schema).
    """
    plan = state.plan
    if plan is None:
        return
    plan_hash = plan.plan_hash()
    if state.preflight_populated_for_hash == plan_hash:
        return

    seeded_a_default = False
    for candidate in find_preflight_reference_candidates(plan.ordered_nodes):
        if candidate.value in state.blocking_references:
            continue
        business = {
            id_type: id_value
            for id_type, id_value in candidate.ids.items()
            if id_type != "WID"
        }
        replace_required = candidate.default_action is ReferenceAction.REPLACE
        # ``default_from_preflight`` also carries KEEP for delivered id types
        # (Event_Classification_Value_ID etc.) — the writer treats KEEP as a
        # no-op, so the source WID passes through unchanged.
        state.blocking_references[candidate.value] = {
            "reference": BlockingReference(
                value=candidate.value, id_type=candidate.id_type
            ),
            "node_name": candidate.node_name or candidate.node_id,
            "elements": [candidate.element],
            "business": business,
            "other_objects": list(candidate.other_node_names),
            "preflight": True,
            #: True when the field is required on the schema, so BLANK produces
            #: an invalid payload. The UI shows a warning, does not auto-seed
            #: a decision, and blocks Start until the user provides a
            #: destination replacement.
            "replace_required": replace_required,
            "default_action": candidate.default_action.value,
        }
        # Seed a decision only when the default is a safe automatic choice:
        # BLANK for tenant-local prompt defaults, KEEP for delivered content
        # whose WID is shared across tenants. REPLACE-required rows are left
        # undecided on purpose — a schema-required field cannot be blanked,
        # and there is no automatic replacement.
        if (
            not replace_required
            and candidate.value not in state.reference_decisions
        ):
            note = (
                f"Preflight default: delivered {candidate.element} "
                f"({', '.join(k for k in candidate.ids if k != 'WID')})"
                if candidate.default_action is ReferenceAction.KEEP
                else f"Preflight default: always-tenant-data {candidate.element}"
            )
            state.reference_decisions[candidate.value] = ReferenceDecision(
                source_wid=candidate.value,
                action=candidate.default_action,
                note=note,
            )
            seeded_a_default = True

    if seeded_a_default:
        state.plan = build_plan(
            state.closure,
            plan.existence,
            overrides=state.action_overrides,
            reference_decisions=state.reference_decisions,
        )
        state.dry_run_plan_hash = state.plan.plan_hash()
        plan_hash = state.plan.plan_hash()

    state.preflight_populated_for_hash = plan_hash


def _collect_blockers(state: WizardState) -> None:
    """Fold any newly-discovered unresolvable reference into the running table.

    Workday reports one bad reference per attempt, so the full set only emerges
    over several. Accumulating means the table grows into the complete picture
    instead of flickering between single rows, and a decision already made is
    never asked about twice.
    """
    for record in state.execute_records:
        blocking = record.blocking_reference
        if blocking is None or blocking.value in state.blocking_references:
            continue
        node = next(
            (n for n in state.plan.ordered_nodes if n.node_id == record.node_id), None
        )
        sites = find_reference_sites(node, blocking.value) if node is not None else []
        business = {}
        for site in sites:
            for id_type, id_value in site.ids.items():
                if id_type != "WID":
                    business[id_type] = id_value
        state.blocking_references[blocking.value] = {
            "reference": blocking,
            "node_name": record.name or record.node_id,
            "elements": sorted({s.element for s in sites}),
            "business": business,
            # Computed once, at discovery time, against the plan as it stood
            # then — see _other_objects_referencing. Not re-derived later, so
            # the table doesn't shift under the user while they're deciding.
            "other_objects": _other_objects_referencing(
                state, blocking.value, record.node_id
            ),
        }


def _decision_rows(state: WizardState) -> list:
    rows = []
    for value, info in state.blocking_references.items():
        existing = state.reference_decisions.get(value)
        business_type = next(iter(info["business"]), "")
        other = info.get("other_objects") or []
        if other:
            shown = ", ".join(other[:2])
            more = f" (+{len(other) - 2} more)" if len(other) > 2 else ""
            also_affects = f"{shown}{more}"
        else:
            also_affects = "—"
        replace_required = bool(info.get("replace_required"))
        # Default decision reflects the element policy captured at preflight:
        # required rows default to REPLACE (user has to fill in a value),
        # delivered id types default to KEEP (source WID passes through),
        # everything else defaults to BLANK (safe drop).
        default_from_preflight = info.get("default_action")
        if replace_required:
            default_action = ReferenceAction.REPLACE
        elif default_from_preflight:
            try:
                default_action = ReferenceAction(default_from_preflight)
            except ValueError:
                default_action = ReferenceAction.BLANK
        else:
            default_action = ReferenceAction.BLANK
        rows.append({
            "Object": info["node_name"],
            "Where": ", ".join(info["elements"]) or "(not located)",
            "Identified as": ", ".join(
                f"{k} = {v}" for k, v in info["business"].items()
            ) or info["reference"].id_type,
            "Required": "REPLACE required" if replace_required else "",
            "Also affects": also_affects,
            "Decision": (
                existing.action.value if existing else default_action.value
            ),
            "Replacement ID type": (
                (existing.replacement_type if existing else None) or business_type
            ),
            "Replacement value": (
                (existing.replacement_value if existing else None) or ""
            ),
            "_wid": value,
            "_replace_required": replace_required,
        })
    return rows


def _apply_decisions(state: WizardState, rows: list, edited) -> None:
    """Fold the edited table back into decisions.

    Rows are matched to their source WID **positionally**, against the list the
    table was built from, rather than by reading a hidden ``_wid`` column back
    out of the editor. Whether a column hidden through ``column_config`` still
    appears in the returned frame is a Streamlit implementation detail, and
    depending on it would fail silently — every decision would land on the wrong
    reference, or raise a KeyError. Row order is guaranteed; that is enough.
    """
    for row, source in zip(edited.to_dict("records"), rows):
        action = ReferenceAction(row["Decision"])
        if action is ReferenceAction.REPLACE and not (
            row["Replacement ID type"] and row["Replacement value"]
        ):
            continue  # incomplete; the submit button is gated on these
        state.reference_decisions[source["_wid"]] = ReferenceDecision(
            source_wid=source["_wid"],
            action=action,
            replacement_type=row["Replacement ID type"] or None,
            replacement_value=row["Replacement value"] or None,
        )


def _plan_needs_tt(state: WizardState) -> bool:
    return any(
        n.kind in TIME_TRACKING_KINDS
        for n in (state.plan.ordered_nodes if state.plan else ())
    )


def _closure_needs_tt(state: WizardState) -> bool:
    return any(
        n.kind in TIME_TRACKING_KINDS
        for n in (state.closure.nodes.values() if state.closure else ())
    )


def _start_reprobe(state: WizardState) -> None:
    # Same match indexes as the Conflicts probe, and not optional here either:
    # a re-probe without them would revert every cross-tenant match back to
    # CREATE, so answering one reference question would silently arm a run that
    # duplicates every shared object.
    tt_connection = (
        state.dest.connection.for_service(TIME_TRACKING_SERVICE_NAME)
        if _closure_needs_tt(state)
        else None
    )
    state.reprobe_job = start_job(
        iter_check_existence(
            state.dest.connection,
            state.closure,
            tt_connection=tt_connection,
            **destination_match_indexes(state),
        )
    )
    state.execute_records = []
    state.execute_job = None


def _pump_reprobe(state: WizardState) -> None:
    """Re-probe in place, then rebuild the plan carrying the new decisions.

    Re-probing is not optional: objects written before the failure now exist,
    and without a fresh probe they would be planned as CREATE and written a
    second time. Doing it here rather than sending the user back to Conflicts
    is the only change — the safety property is identical.
    """
    job = state.reprobe_job
    pump(job, time_budget=READ_TIME_BUDGET)
    last = job.last_event
    render_job_progress(
        job,
        label="Re-checking the destination",
        fraction=last.fraction if last is not None else 0.0,
    )

    if job.error is not None:
        state.reprobe_job = None
        return
    if not job.done:
        st.rerun()
        return

    existence = {p.node.node_id: p.existence for p in job.events}
    state.plan = build_plan(
        state.closure,
        existence,
        overrides=state.action_overrides,
        reference_decisions=state.reference_decisions,
    )
    # The mapping table IS the review of this change. A decision alters the
    # payload and therefore the plan hash, which would otherwise invalidate the
    # dry-run approval and force the entire Confirm gate again for every single
    # reference. Re-stamping here says: the user saw exactly what changed, in a
    # table, and authorised it. Everything else the guard checks — tenant name
    # retyped, irreversibility acknowledged, both sides verified, destination a
    # safe environment — is untouched and still has to hold.
    state.dry_run_plan_hash = state.plan.plan_hash()
    state.reprobe_job = None
    st.rerun()


def _render_reference_resolution(state: WizardState) -> None:
    """One table for every unresolvable reference the run will hit.

    Two sources feed this table:
      * **Pre-flight** — a walk of every plan payload for reference elements
        that are *always* pointers to tenant data (parameter defaults, filter
        instances). No tenant round trip; surfaced before the first write so
        the user resolves the batch in one pass instead of one-per-restart.
      * **Fault-driven** — the classic path: a live write fails, Workday names
        one bad reference, and the row lands here. Kept alongside the
        pre-flight rows because Workday emits one at a time and the pre-flight
        set does not cover every case that can break (a dangling matrix
        pointer, a deleted delivered object).
    """
    preflight_count = sum(
        1 for info in state.blocking_references.values() if info.get("preflight")
    )
    fault_count = len(state.blocking_references) - preflight_count
    if preflight_count and not fault_count:
        title = f"{preflight_count} tenant-data reference(s) to decide before starting"
        detail = (
            "Found by pre-flight: these payloads name specific companies, "
            "workers, or orgs from the source tenant that almost certainly "
            "do not exist on the destination. Left as-is, the first live "
            "write would fail on one of them; the whole run would then have "
            "to restart per reference. Deciding them here does it once."
        )
        eyebrow = "Resolve before starting"
    elif preflight_count and fault_count:
        title = "References the destination cannot resolve"
        detail = (
            f"{preflight_count} found by pre-flight (tenant data — parameter "
            f"defaults, filter instances) and {fault_count} surfaced by "
            "earlier live-write failures. All share the same schema and are "
            "resolved together."
        )
        eyebrow = "Needs a decision"
    else:
        title = "References the destination cannot resolve"
        detail = (
            "These point at tenant data rather than configuration — a prompt "
            "default, a filter value, a matrix pointer. Blanking drops the "
            "value; the object still migrates. A decision here is applied "
            "automatically to every occurrence of this exact reference for "
            "the rest of the migration — including any objects listed under "
            "'Also affects' — so you only decide once per distinct reference, "
            "even when the same value blocks several objects."
        )
        eyebrow = "Needs a decision"
    theme.section(title, detail, eyebrow=eyebrow)

    rows = _decision_rows(state)

    # Bulk actions before the table, so a 31-row list is one click instead of
    # 31 dropdown changes. Both write back to state and re-render — the table
    # then shows the new default. Cheap because ``reference_decisions`` is what
    # ``_decision_rows`` already reads to pre-populate the Decision column.
    bulk_cols = st.columns([1, 1, 1, 3])
    with bulk_cols[0]:
        if st.button(
            f"Blank all ({len(rows)})",
            key="refdec_blank_all",
            help="Set every row's decision to blank. Blanking a reference "
                 "drops it from the payload; the object still migrates.",
            disabled=not rows,
        ):
            for source in rows:
                state.reference_decisions[source["_wid"]] = ReferenceDecision(
                    source_wid=source["_wid"],
                    action=ReferenceAction.BLANK,
                )
            st.rerun()
    with bulk_cols[1]:
        if st.button(
            "Keep all",
            key="refdec_keep_all",
            help="Leave every source WID exactly as it is. Use when the "
                 "references point at delivered content — event "
                 "classifications, business process types, currencies — "
                 "whose WID is the same on the destination tenant.",
            disabled=not rows,
        ):
            for source in rows:
                state.reference_decisions[source["_wid"]] = ReferenceDecision(
                    source_wid=source["_wid"],
                    action=ReferenceAction.KEEP,
                )
            st.rerun()
    with bulk_cols[2]:
        if st.button(
            "Clear decisions",
            key="refdec_clear",
            help="Reset every row's decision, so nothing is applied yet.",
            disabled=not rows,
        ):
            for source in rows:
                state.reference_decisions.pop(source["_wid"], None)
            st.rerun()

    edited = st.data_editor(
        pd.DataFrame(rows).drop(columns=["_wid", "_replace_required"]),
        hide_index=True,
        use_container_width=True,
        disabled=["Object", "Where", "Identified as", "Required", "Also affects"],
        column_config={
            "Required": st.column_config.TextColumn(
                help="Rows marked 'REPLACE required' point at a schema-required "
                     "field. Blank is not a legal move for these — Workday "
                     "refuses the write. Fill in a destination value."
            ),
            "Decision": st.column_config.SelectboxColumn(
                options=[a.value for a in ReferenceAction], required=True,
            ),
            "Replacement ID type": st.column_config.TextColumn(
                help="Only used when the decision is 'replace' — e.g. "
                     "Organization_Reference_ID."
            ),
            "Replacement value": st.column_config.TextColumn(
                help="The identifier in the DESTINATION tenant. There is no "
                     "generic way for this tool to list candidates, so look it "
                     "up in Workday."
            ),
        },
        key="reference_decision_table",
    )

    incomplete = [
        r["Object"] for r in edited.to_dict("records")
        if r["Decision"] == ReferenceAction.REPLACE.value
        and not (r["Replacement ID type"] and r["Replacement value"])
    ]
    if incomplete:
        theme.banner(
            "warning",
            f"{len(incomplete)} row(s) set to replace with no value",
            "Fill in both the ID type and the value, or set those rows back to blank.",
        )
    # Guard the required rows regardless of the decision the user picked in
    # the table — a required row set to BLANK is still not shippable.
    still_required = [
        source["Object"]
        for source, row in zip(rows, edited.to_dict("records"))
        if source["_replace_required"]
        and (
            row["Decision"] != ReferenceAction.REPLACE.value
            or not (row["Replacement ID type"] and row["Replacement value"])
        )
    ]
    if still_required and not incomplete:
        theme.banner(
            "warning",
            f"{len(still_required)} required reference(s) still need replacement",
            "These reference elements are required by the schema — blanking "
            "them produces a payload Workday rejects. Provide a destination "
            "value for each.",
        )

    # Pre-flight is the "before the first attempt" case. Skipping the re-probe
    # here is correct: nothing has been written yet, so the destination state
    # is unchanged, and starting the run is what should happen next.
    if state.execute_records:
        apply_label = "Apply and re-check destination"
        apply_caption = (
            "Re-checking picks up anything already written so it is skipped "
            "rather than written twice. Your other approvals stay in place — "
            "you can start execution again straight afterwards."
        )
    else:
        apply_label = "Apply decisions"
        apply_caption = (
            "Decisions take effect on the payload sent to the destination — "
            "nothing has been written yet, so no re-check is needed. Start "
            "the run below when the table looks right."
        )
    if st.button(apply_label, key="refdec_apply",
                 type="primary", disabled=bool(incomplete)):
        _apply_decisions(state, rows, edited)
        if state.execute_records:
            _start_reprobe(state)
        else:
            # Fold the new decisions into the plan so plan_hash reflects them
            # and the guard's dry-run-reviewed stamp still matches. Same
            # rationale as the reprobe branch's re-hash — the mapping table is
            # the review of the payload change.
            state.plan = build_plan(
                state.closure,
                state.plan.existence,
                overrides=state.action_overrides,
                reference_decisions=state.reference_decisions,
            )
            state.dry_run_plan_hash = state.plan.plan_hash()
        st.rerun()

    st.caption(apply_caption)


def _unfinished_records(state: WizardState) -> list:
    """Writes that did not land cleanly — FAILED or INDETERMINATE.

    SUCCESS and SKIPPED are done; NOT_ATTEMPTED never made it onto the wire.
    INDETERMINATE is the dangerous one: the PUT may have committed, so a
    retry without re-probing can duplicate an object this service cannot
    delete.
    """
    return [
        r
        for r in state.execute_records
        if r.status in (WriteStatus.FAILED, WriteStatus.INDETERMINATE)
    ]


def _eta_caption(job) -> str | None:
    """Remaining-time caption from objects already written this run."""
    last = job.last_event
    if last is None or not last.total or last.position >= last.total:
        return None
    durations = [
        p.record.duration_ms for p in job.events if p.record.duration_ms > 0
    ]
    avg_s = (sum(durations) / len(durations) / 1000.0) if durations else 0.15
    remaining = last.total - last.position
    return (
        f"{_format_duration(remaining * max(0.15, avg_s))} remaining "
        f"({remaining} object(s) left)"
    )


def _start_disabled(state: WizardState) -> bool:
    """Start is a live write — the Run-step gate and required replacements
    both have to be clear. Never auto-start."""
    return bool(_required_replacements_missing(state) or confirm.gate_live(state))


def _start(state: WizardState) -> None:
    if _start_disabled(state):
        return
    guard = build_guard(state, dry_run=False)
    tt_connection = (
        state.dest.connection.for_service(TIME_TRACKING_SERVICE_NAME)
        if _plan_needs_tt(state)
        else None
    )
    try:
        generator = iter_execute(
            state.dest.connection, state.plan, guard,
            owner_reference=owner_reference(state), stop_on_failure=True,
            tt_connection=tt_connection,
            report_sharing=state.report_sharing,
        )
    except GuardViolation as exc:
        theme.banner("danger", "Blocked by the write guard", str(exc))
        return
    state.execute_job = start_job(generator)
    state.execute_records = []
    state.execute_paused = False


def render(state: WizardState, *, heading: bool = True) -> None:
    if heading:
        st.header("Execute")

    if state.plan is None:
        theme.banner("danger", "No plan", remedy="Go back to Plan.")
        return

    # A re-probe kicked off from the mapping table owns the page while it runs.
    if state.reprobe_job is not None:
        _pump_reprobe(state)
        return

    _collect_blockers(state)
    # Runs once per plan hash: enumerates always-tenant-data references so the
    # table appears before the first write attempt, not only after a failure.
    _populate_preflight_references(state)
    job = state.execute_job

    if job is None and not state.execute_records:
        # The table outlives a failed attempt: decisions already made stay
        # visible and editable, so a second reference does not hide the first.
        if state.blocking_references:
            _render_reference_resolution(state)
            st.divider()

        # Required-replacement rows must be answered before Start. Blank is
        # not a legal move for these (Workday rejects the payload with
        # "Required Mutex error"), so pre-flight refuses to advance until
        # every one carries a REPLACE decision with both fields set.
        missing_required = _required_replacements_missing(state)

        theme.figures(
            [("Objects to write", state.plan.writes_planned)], tones={"Objects to write": "write"}
        )
        theme.banner(
            "warning",
            f"This writes to {state.dest.target.tenant}",
            "Objects are written one at a time, in dependency order, and each one's "
            "destination WID feeds the next. Pause and cancel take effect between "
            "objects, never mid-write.",
            remedy="Nothing written here can be undone by this tool.",
        )
        if missing_required:
            theme.banner(
                "danger",
                f"{len(missing_required)} required reference(s) not answered",
                "These reference elements are marked required on the schema, "
                "and blanking them would fail the write with 'Required Mutex "
                "error.' Set each to 'replace' with the destination-tenant "
                "value in the table above before starting.",
                remedy="Fill in the replacement type and value for every "
                       "row flagged as required.",
            )
        start_blocked = _start_disabled(state)
        if start_blocked and not missing_required:
            st.caption(
                "Complete the live execution gate above before starting — "
                "destination tenant name, warning acknowledgements, and the "
                "irreversibility checkbox. This never starts itself."
            )
        if st.button(
            "Start live execution",
            key="execute_start",
            type="primary",
            disabled=start_blocked,
        ):
            _start(state)
            st.rerun()
        return

    if job is not None:
        col1, col2, _ = st.columns([1, 1, 4])
        with col1:
            if not state.execute_paused:
                if st.button("Pause", key="execute_pause", use_container_width=True):
                    state.execute_paused = True
                    st.rerun()
            else:
                if st.button("Resume", key="execute_resume", type="primary",
                             use_container_width=True):
                    state.execute_paused = False
                    st.rerun()
        with col2:
            if st.button("Cancel", key="execute_cancel", use_container_width=True):
                job.cancel()
                st.rerun()

        if not state.execute_paused and job.running:
            pump(job, time_budget=WRITE_TIME_BUDGET, batch_size=1)

        last = job.last_event
        fraction = last.fraction if last is not None else 0.0
        render_job_progress(job, label="Live execution", fraction=fraction)
        if last is not None:
            st.caption(f"{last.position}/{last.total}: {last.node.name or last.node.node_id} — {last.record.status.value}")
        eta = _eta_caption(job)
        if eta:
            st.caption(eta)

        if job.events:
            st.dataframe(
                [
                    {"name": p.node.name or p.node.node_id, "action": p.record.action.value, "status": p.record.status.value}
                    for p in job.events
                ],
                use_container_width=True, hide_index=True,
            )

        if job.error is not None:
            state.execute_records = [p.record for p in job.events]
            state.execute_job = None
            st.rerun()
        elif job.cancelled:
            state.execute_records = [p.record for p in job.events]
            state.execute_job = None
            theme.banner(
                "warning",
                "Execution cancelled",
                "It stopped cleanly between objects, so nothing is half-written — but "
                "objects already written cannot be undone by this tool.",
            )
        elif job.done:
            state.execute_records = [p.record for p in job.events]
            state.execute_job = None
            # Stay here if something stopped on an unresolvable reference —
            # that is answerable in place, and bouncing to Results would hide
            # the one question that would let the run finish.
            # Stay here on a blocking reference (answerable in place) or on
            # FAILED / INDETERMINATE (must re-probe before a resume). Only a
            # clean finish auto-advances — live Start never fires itself.
            if _blocked_record(state) is None and not _unfinished_records(state):
                state.step = "results"
            st.rerun()
        elif not state.execute_paused:
            st.rerun()
        return

    if state.blocking_references:
        st.divider()
        _render_reference_resolution(state)
        return

    unfinished = _unfinished_records(state)
    if unfinished:
        indeterminate = [r for r in unfinished if r.needs_reprobe]
        if indeterminate:
            theme.banner(
                "danger",
                f"{len(indeterminate)} object(s) ended INDETERMINATE",
                "A transport or protocol failure left the destination in an "
                "unknown state — the PUT may have committed. This is not a "
                "normal skip or a clean failure. Re-probe before touching "
                "those objects again; retrying a committed write creates a "
                "duplicate this tool cannot delete.",
                remedy="Re-probe below, then Start again. Already-present "
                       "objects become SKIP.",
            )
        else:
            theme.banner(
                "danger",
                f"{len(unfinished)} object(s) failed",
                "Objects written before the failure are already on the "
                "destination and cannot be undone by this tool. Re-probe so "
                "those become SKIP, then start again from what is left.",
            )
        if st.button(
            "Re-probe and resume",
            key="execute_resume_reprobe",
            type="primary",
        ):
            _start_reprobe(state)
            st.rerun()
        st.caption(
            "Re-probing does not write. Start live execution stays a separate "
            "click after the probe finishes."
        )
        return

    theme.banner(
        "success",
        f"Execution finished — {len(state.execute_records)} record(s)",
        "Continue to Results for the per-object outcome and the exports.",
    )


def gate(state: WizardState) -> list[Blocker]:
    if state.execute_job is not None:
        return [Blocker(None, "Execution in progress", "Wait for the run to finish, or cancel it.", "")]
    if not state.execute_records:
        return [Blocker(None, "Not executed yet", "Live execution has not been run.", "Click Start live execution above.")]
    return []
