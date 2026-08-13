"""
migrate_dashboards_example.py
-----------------------------
Plan (and optionally execute) a migration of custom dashboards, resolving the
full closure — worklet reports, prompt sets, calculated measures and calculated
fields — and probing the destination with **cross-tenant calculated-field
matching** enabled.

That last part is why this exists alongside `migrate_report_example.py`.
`Calculated_Field_ID` is not a stable identity across independently-built
tenants (see CLAUDE.md), so the destination probe must be given a
`match_index` built from a destination sweep. Without it, every calculated
field the two tenants already share is reported absent and re-created as a
duplicate — 62 of 110 on the run this script was written for.

Both connections must be **implementer accounts**: a normal ISU cannot read or
write dashboards at all, regardless of domain grants.

Run it:
  python scripts/migrate_dashboards_example.py "Dashboard Name" ["Another" ...]
  python scripts/migrate_dashboards_example.py --live "Dashboard Name" ...

Without --live it stops after the dry run, which is the only way to get a plan
hash to review. Live execution is a separate, deliberate invocation.

``--treat-as-new <Calculated_Field_ID>`` (repeatable) records a human decision
that a specific calculated field is absent from the destination, for the case
where matching genuinely cannot decide — several destination fields share the
field's name, class and business object, and it carries no WQL alias to tell
them apart. It adjudicates **existence**, not the action, because
``validate_plan`` refuses to create an object whose existence is UNKNOWN
however the action was set, and that rule is deliberate. Use it only when
creating a duplicate is the understood, accepted outcome.
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()

#: Host/tenant pairings confirmed live. `.env` has had this wrong in every
#: session so far, and the failure looks like an outage rather than a config
#: error, so it is worth naming explicitly when a connection fails.
KNOWN_PAIRINGS = """
  commitconsulting_dpt1  impl-services1.wd12.myworkday.com
  commitconsulting_dpt3  impl-services1.wd12.myworkday.com
  commitconsulting_dpt5  impl-services1.wd12.myworkday.com
  commitconsulting       impl-services1.wd501.myworkday.com
"""


def connect(role_env: str, role: api.Role) -> api.Connection:
    return api.connect(
        api.target_from_parts(
            os.environ[f"WD_{role_env}_SERVICES_HOST"],
            os.environ[f"WD_{role_env}_TENANT"],
        ),
        os.environ[f"WD_{role_env}_ISU_USERNAME"],
        os.environ[f"WD_{role_env}_ISU_PASSWORD"],
        role=role,
    )


def verify(connection: api.Connection, label: str) -> None:
    status = api.verify_connection(connection)
    print(f"{label} verified: {status.ok} — {status.detail}")
    if not status.ok:
        raise SystemExit(
            f"{label} connection failed. Check the .env host/tenant pairing "
            f"FIRST — it has never once been the code. Confirmed pairings:\n"
            f"{KNOWN_PAIRINGS}"
        )


def sweep(connection: api.Connection, kind: str, iterator_fn, label: str):
    """Build an index fresh and cache it.

    Rebuilt rather than loaded: dashboards and prompt sets are one page each,
    and a stale index has produced a false 'missing dependency' three times
    across this project's sessions.
    """
    index = None
    for progress in iterator_fn(connection):
        index = progress.index
    if index is None:
        raise SystemExit(f"{label} sweep returned nothing.")
    api.save_index(index, api.cache_path(connection, kind))
    print(f"  {label}: {len(index)} items")
    return index


def adjudicate_as_new(closure, existence: dict, reference_ids: set[str]) -> None:
    """Record that named calculated fields are absent from the destination.

    Written into ``existence`` rather than passed as an action override: an
    override leaves the probe result UNKNOWN, and `validate_plan` blocks
    creating an UNKNOWN object no matter how the action was set. The decision
    being made here is about existence, so that is where it belongs — and it
    stays visible on the plan afterwards as the recorded fault text.
    """
    seen = set()
    for node in closure.nodes.values():
        if node.reference_id in reference_ids:
            existence[node.node_id] = api.Existence(
                node_id=node.node_id,
                state=api.LookupOutcome.NOT_FOUND,
                fault=(
                    "Adjudicated as new by the operator: destination matching "
                    "could not decide between several equally-good candidates."
                ),
            )
            seen.add(node.reference_id)
            print(f"  adjudicated as NEW: {node.name!r} ({node.reference_id})")
    unmatched = reference_ids - seen
    if unmatched:
        raise SystemExit(
            "--treat-as-new named calculated field(s) that are not in this "
            f"closure: {sorted(unmatched)}"
        )


def main(names: list[str], live: bool, treat_as_new: set[str]) -> None:
    source_conn = connect("SOURCE", api.Role.SOURCE)
    dest_conn = connect("DEST", api.Role.DESTINATION)
    verify(source_conn, "source")
    verify(dest_conn, "dest")

    if source_conn.target.identity() == dest_conn.target.identity():
        raise SystemExit("Source and destination are the same tenant.")

    print("\nBuilding source indexes…")
    dashboard_index = sweep(
        source_conn, "dashboard", api.iter_dashboard_index, "dashboards"
    )
    prompt_set_index = sweep(
        source_conn, "prompt_set", api.iter_prompt_set_index, "prompt sets"
    )
    # A prompt set's members point at these, and it cannot be written before
    # they exist in the destination. Only the custom ones become dependencies;
    # delivered parameters are WID-only and pass through.
    prompt_field_index = sweep(
        source_conn, "prompt_field", api.iter_prompt_field_index, "prompt fields"
    )
    # A report gauge layout points at one of these; the report cannot be
    # written before it exists. One page.
    gauge_range_index = sweep(
        source_conn, "gauge_range", api.iter_gauge_range_index, "gauge ranges"
    )
    cf_index = api.load_index(
        api.cache_path(source_conn, "calculated_field"),
        tenant=source_conn.target.tenant,
    )
    if cf_index is None:
        raise SystemExit(
            "No cached calculated-field index for the source. Build one first "
            "(~25s) — resolution classifies every WID against it."
        )
    print(f"  calculated fields: {len(cf_index)} (cached)")

    # The destination sweep that makes cross-tenant matching possible.
    dest_cf_index = api.load_index(
        api.cache_path(dest_conn, "calculated_field"), tenant=dest_conn.target.tenant
    )
    if dest_cf_index is None:
        print("  destination calculated fields: sweeping (~25s)…")
        dest_cf_index = sweep(
            dest_conn, "calculated_field", api.iter_calculated_field_index,
            "destination calculated fields",
        )
    else:
        print(f"  destination calculated fields: {len(dest_cf_index)} (cached)")
    match_index = api.calculated_field_match_index(dest_cf_index)

    # Calculated measures need the same treatment, and more urgently: their
    # BI_Calculated_Measure_ID is Workday-generated per tenant, so it can never
    # match across two. One page, so this is swept every run rather than cached.
    dest_measures = None
    for progress in api.iter_calculated_measure_index(dest_conn):
        dest_measures = progress.index
    measure_match_index = api.calculated_measure_match_index(dest_measures)
    print(f"  destination calculated measures: {len(dest_measures)}")

    wanted = set(names)
    selected = {
        wid: dashboard_index.payload(wid)
        for wid, summary in dashboard_index.summaries.items()
        if summary.name in wanted
    }
    found_names = {
        s.name for s in dashboard_index.summaries.values() if s.name in wanted
    }
    missing = wanted - found_names
    if missing:
        raise SystemExit(f"No dashboard named exactly: {sorted(missing)}")
    print(f"\nSelected {len(selected)} dashboard(s).")

    closure = api.resolve(
        cf_index,
        selected_dashboards=selected,
        measure_loader=api.measure_loader_for(source_conn),
        report_loader=api.report_loader_for(source_conn),
        prompt_set_index=prompt_set_index,
        prompt_field_index=prompt_field_index,
        gauge_range_index=gauge_range_index,
        dashboard_index=dashboard_index,
    )
    print(f"Closure: {closure.counts_by_kind()} (total {len(closure)})")

    print("\nProbing destination…")
    existence = {}
    for progress in api.iter_check_existence(
        dest_conn,
        closure,
        match_index=match_index,
        measure_match_index=measure_match_index,
    ):
        existence[progress.node.node_id] = progress.existence

    matched = [e for e in existence.values() if e.matched_by]
    if matched:
        print(
            f"  {len(matched)} calculated field(s) matched cross-tenant rather "
            "than by Calculated_Field_ID — they will be reused, not duplicated."
        )

    if treat_as_new:
        adjudicate_as_new(closure, existence, treat_as_new)

    plan = api.build_plan(closure, existence)
    print(f"\nPlan actions: {plan.counts()}")
    print(f"Plan hash: {plan.plan_hash()}")

    # Per-kind breakdown, because "74 creates" is not reviewable on its own —
    # two prompt sets among them means something very different from two more
    # calculated fields.
    by_kind: dict[str, dict[str, int]] = {}
    for node in plan.ordered_nodes:
        tally = by_kind.setdefault(node.kind.value, {})
        action = plan.action_for(node).value
        tally[action] = tally.get(action, 0) + 1
    print("  by kind:")
    for kind in sorted(by_kind):
        counts = ", ".join(f"{a} {n}" for a, n in sorted(by_kind[kind].items()))
        print(f"    {kind:20} {counts}")

    blockers = api.validate_plan(plan)
    if blockers:
        print(f"\nBLOCKERS ({len(blockers)}) — refusing:")
        for blocker in blockers:
            print(f"  - {blocker.title}: {blocker.detail}")
            print(f"    remedy: {blocker.remedy}")
        raise SystemExit(1)
    print("No blockers.")

    guard = api.WriteGuard(
        source=source_conn.target,
        dest=dest_conn.target,
        dry_run=not live,
        plan_hash=plan.plan_hash(),
        confirmed_tenant_name=dest_conn.target.tenant if live else "",
        dry_run_reviewed=live,
        source_verified=True,
        dest_verified=True,
        source_username=source_conn.username,
        dest_username=dest_conn.username,
    )
    if live:
        blocking = [g for g in api.evaluate_guards(guard) if g.level.value == "block"]
        if blocking:
            print("\nSAFETY GUARD BLOCKED THE RUN:")
            for finding in blocking:
                print(f"  - {finding.title}: {finding.detail}")
                print(f"    remedy: {finding.remedy}")
            raise SystemExit(1)

    print(f"\n--- {'LIVE EXECUTION' if live else 'DRY RUN'} ---")
    records = []
    for progress in api.iter_execute(
        dest_conn,
        plan,
        guard,
        owner_reference=api.build_owner_reference(workday_username="wd-support"),
    ):
        record = progress.record
        records.append(record)
        if record.status.value not in ("success", "skipped", "not_attempted"):
            print(
                f"  [{progress.position}/{progress.total}] "
                f"{record.status.value.upper():14} {record.kind:18} {record.name!r}"
            )
            if record.fault:
                print(f"      fault: {record.fault}")
            for exception in record.exceptions or []:
                print(f"      exception: {exception.classification} — {exception.message}")
        elif live and record.status.value == "success" and progress.position % 20 == 0:
            print(f"  …{progress.position}/{progress.total}")

    print(f"\nSummary: {api.summarise(records)}")
    for record in records:
        if "dashboard" in record.kind:
            print(
                f"  {record.name!r} -> {record.status.value}  "
                f"dest_wid={record.dest_wid}"
            )
    if not live:
        print(f"\nPlan hash for a live run of this exact plan: {plan.plan_hash()}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    dashboards: list[str] = []
    new_fields: set[str] = set()
    expecting_field = False
    live = False
    for arg in argv:
        if expecting_field:
            new_fields.add(arg)
            expecting_field = False
        elif arg == "--live":
            live = True
        elif arg == "--treat-as-new":
            expecting_field = True
        elif arg.startswith("--treat-as-new="):
            new_fields.add(arg.split("=", 1)[1])
        else:
            dashboards.append(arg)
    if expecting_field:
        raise SystemExit("--treat-as-new needs a Calculated_Field_ID after it.")
    if not dashboards:
        print(__doc__)
        raise SystemExit(1)
    main(dashboards, live, new_fields)
