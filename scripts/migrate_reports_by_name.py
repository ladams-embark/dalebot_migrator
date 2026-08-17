"""
migrate_reports_by_name.py
---------------------------
Plan (and optionally execute) a migration of standalone reports selected by
exact name, resolving the full closure — sub-reports, calculated measures and
calculated fields — and probing the destination with **cross-tenant
calculated-field matching** enabled, exactly like
`migrate_dashboards_example.py`. Without `match_index`, every calculated field
the two tenants already share is reported absent and re-created as a
duplicate (see CLAUDE.md).

Report names are looked up against the SOURCE tenant: first
`lookup_report_by_name` to resolve the WID (a duplicated name returns
UNKNOWN and is refused, never guessed at), then a targeted `lookup_report`
by that WID to fetch the full payload — the same two-step composition
`ui/steps/select.py` uses, kept apart because the cheap name-only probe used
for existence checks deliberately carries no data.

Run it:
  python scripts/migrate_reports_by_name.py "Report Name" ["Another" ...]
  python scripts/migrate_reports_by_name.py --live "Report Name" ...

Without --live it stops after the dry run, which is the only way to get a
plan hash to review. Live execution is a separate, deliberate invocation.

``--wid=<source_wid>[:<new name>]`` (repeatable) selects a report directly by
WID instead of by name, for the case two source reports genuinely share a
name and `lookup_report_by_name` refuses to guess. Optionally renames it
before resolution (rewriting Tenanted_Report_Definition_Data.Name), so two
same-named source reports can be migrated side by side under distinct
destination names instead of colliding.

``--blank=<source_wid>`` (repeatable) blanks a reference naming a specific
tenant data instance (a Company, GL account, etc.) that does not exist on the
destination — the same "reference to tenant data, not configuration" case
CLAUDE.md documents for filter conditions, applied here via the existing
`ReferenceDecision`/`_apply_reference_decisions` machinery (already wired
into every payload builder). Confirmed live: an unresolvable
`Instance_Reference` fails the whole write with "Invalid ID value", so use
this only for WIDs you have evidence will not resolve — never speculatively.

``--force-update=<Report Name>`` (repeatable) rewrites a report that already
exists in the destination instead of skipping it, mirroring
`migrate_dashboards_example.py`'s flag of the same name. UPDATE is otherwise
never automatic — overwriting destination configuration is the destructive
direction and this service has no delete — so it has to be named explicitly.
Use only when a bug fix in the writer needs to reach an object already
written; the destination WID is picked up from the normal existence probe,
not guessed.
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()

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


def load_or_sweep(connection, kind, iterator_fn, label):
    """Prefer a cached index; sweep fresh only if nothing is cached.

    Unlike `migrate_dashboards_example.py`'s always-fresh dashboard/prompt-set
    sweeps (those are one page and cheap), this reuses the indexes already
    banked by an earlier run against the same tenants — a full report sweep
    is 6 pages / ~158s, not worth repeating for a five-report selection.
    """
    index = api.load_index(api.cache_path(connection, kind), tenant=connection.target.tenant)
    if index is not None:
        print(f"  {label}: {len(index)} (cached)")
        return index
    index = None
    for progress in iterator_fn(connection):
        index = progress.index
    if index is None:
        raise SystemExit(f"{label} sweep returned nothing.")
    api.save_index(index, api.cache_path(connection, kind))
    print(f"  {label}: {len(index)} items (swept)")
    return index


def resolve_report_by_name(connection: api.Connection, name: str) -> tuple[str, dict]:
    """Name -> (wid, full payload) on the given tenant, refusing ambiguity."""
    result = api.lookup_report_by_name(connection, name)
    if result.outcome is api.LookupOutcome.NOT_FOUND:
        raise SystemExit(f"No report named exactly {name!r} on the source.")
    if result.outcome is api.LookupOutcome.UNKNOWN:
        raise SystemExit(
            f"{name!r} is ambiguous on the source: {result.fault} "
            "Refusing to guess which one to migrate."
        )
    full = api.lookup_report(connection, wid=result.wid)
    if full.outcome is not api.LookupOutcome.FOUND or full.data is None:
        raise SystemExit(f"Could not fetch full definition for {name!r}: {full.fault}")
    return result.wid, full.data


def resolve_report_by_wid(
    connection: api.Connection, wid: str, rename_to: str | None
) -> tuple[str, dict]:
    """WID -> full payload, for the case a name lookup can't disambiguate.

    Used only when two source reports genuinely share a name — picked by the
    user after being shown distinguishing detail (owner, columns, shared
    flag), never guessed. ``rename_to`` overrides ``Tenanted_Report_Definition
    _Data.Name`` before resolution, which is what the closure, the
    destination existence probe, and the write all read the name from — so
    the two land in the destination under distinct names instead of
    colliding.
    """
    full = api.lookup_report(connection, wid=wid)
    if full.outcome is not api.LookupOutcome.FOUND or full.data is None:
        raise SystemExit(f"Could not fetch full definition for wid {wid!r}: {full.fault}")
    if rename_to:
        data = full.data.get("Tenanted_Report_Definition_Data") or {}
        data["Name"] = rename_to
    return wid, full.data


def main(
    names: list[str],
    live: bool,
    wid_selections: list[tuple[str, str | None]],
    blank_refs: set[str],
    force_update: set[str] = frozenset(),
) -> None:
    source_conn = connect("SOURCE", api.Role.SOURCE)
    dest_conn = connect("DEST", api.Role.DESTINATION)
    verify(source_conn, "source")
    verify(dest_conn, "dest")

    if source_conn.target.identity() == dest_conn.target.identity():
        raise SystemExit("Source and destination are the same tenant.")

    print(f"\nLooking up {len(names)} report(s) by exact name on the source…")
    selected: dict[str, dict] = {}
    for name in names:
        wid, payload = resolve_report_by_name(source_conn, name)
        selected[wid] = payload
        print(f"  {name!r} -> {wid}")

    for wid, rename_to in wid_selections:
        wid, payload = resolve_report_by_wid(source_conn, wid, rename_to)
        selected[wid] = payload
        display_name = (payload.get("Tenanted_Report_Definition_Data") or {}).get("Name")
        print(f"  wid {wid} -> {display_name!r}" + (" (renamed)" if rename_to else ""))

    print("\nBuilding source indexes…")
    prompt_set_index = load_or_sweep(
        source_conn, "prompt_set", api.iter_prompt_set_index, "prompt sets"
    )
    prompt_field_index = load_or_sweep(
        source_conn, "prompt_field", api.iter_prompt_field_index, "prompt fields"
    )
    gauge_range_index = load_or_sweep(
        source_conn, "gauge_range", api.iter_gauge_range_index, "gauge ranges"
    )
    analytic_indicator_index = load_or_sweep(
        source_conn, "analytic_indicator", api.iter_analytic_indicator_index,
        "analytic indicators",
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

    dest_cf_index = api.load_index(
        api.cache_path(dest_conn, "calculated_field"), tenant=dest_conn.target.tenant
    )
    if dest_cf_index is None:
        print("  destination calculated fields: sweeping (~25s)…")
        dest_cf_index = load_or_sweep(
            dest_conn, "calculated_field", api.iter_calculated_field_index,
            "destination calculated fields",
        )
    else:
        print(f"  destination calculated fields: {len(dest_cf_index)} (cached)")
    match_index = api.calculated_field_match_index(dest_cf_index)

    dest_measures = None
    for progress in api.iter_calculated_measure_index(dest_conn):
        dest_measures = progress.index
    measure_match_index = api.calculated_measure_match_index(dest_measures)
    print(f"  destination calculated measures: {len(dest_measures)}")

    closure = api.resolve(
        cf_index,
        selected_reports=selected,
        measure_loader=api.measure_loader_for(source_conn),
        report_loader=api.report_loader_for(source_conn),
        prompt_set_index=prompt_set_index,
        prompt_field_index=prompt_field_index,
        gauge_range_index=gauge_range_index,
        analytic_indicator_index=analytic_indicator_index,
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

    reference_decisions = {
        wid: api.ReferenceDecision(
            source_wid=wid,
            action=api.ReferenceAction.BLANK,
            note="Blanked by operator: names a specific tenant data instance "
            "(e.g. a Company) that does not exist on the destination.",
        )
        for wid in blank_refs
    }
    if reference_decisions:
        print(f"\nBlanking {len(reference_decisions)} reference(s) by WID:")
        for wid in reference_decisions:
            print(f"  {wid}")

    overrides = {}
    if force_update:
        for node in closure.nodes.values():
            if node.name in force_update:
                overrides[node.node_id] = api.Action.UPDATE
                ex = existence.get(node.node_id)
                print(f"  forcing UPDATE: {node.name!r} (dest wid "
                      f"{ex.dest_wid if ex else None})")
        missing = force_update - {n.name for n in closure.nodes.values()}
        if missing:
            raise SystemExit(f"--force-update named objects not in this closure: {sorted(missing)}")

    plan = api.build_plan(
        closure, existence, overrides=overrides, reference_decisions=reference_decisions
    )
    print(f"\nPlan actions: {plan.counts()}")
    print(f"Plan hash: {plan.plan_hash()}")

    by_kind: dict[str, dict[str, int]] = {}
    for node in plan.ordered_nodes:
        tally = by_kind.setdefault(node.kind.value, {})
        action = plan.action_for(node).value
        tally[action] = tally.get(action, 0) + 1
    print("  by kind:")
    for kind in sorted(by_kind):
        counts = ", ".join(f"{a} {n}" for a, n in sorted(by_kind[kind].items()))
        print(f"    {kind:20} {counts}")

    unknown = plan.unknown_nodes()
    if unknown:
        by_id = {n.node_id: n for n in plan.ordered_nodes}
        print(f"\n{len(unknown)} node(s) with UNKNOWN existence:")
        for node_id in unknown:
            n = by_id.get(node_id)
            e = plan.existence[node_id]
            print(f"  - {n.name if n else node_id!r}: {e.fault}")

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

    print(f"\nSummary: {api.summarise(records)}")
    for record in records:
        if record.kind == "report":
            print(
                f"  {record.name!r} -> {record.status.value}  "
                f"dest_wid={record.dest_wid}"
            )
    if not live:
        print(f"\nPlan hash for a live run of this exact plan: {plan.plan_hash()}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    names: list[str] = []
    wid_selections: list[tuple[str, str | None]] = []
    blank_refs: set[str] = set()
    force_update: set[str] = set()
    live = False
    for arg in argv:
        if arg == "--live":
            live = True
        elif arg.startswith("--wid="):
            spec = arg.split("=", 1)[1]
            wid, _, rename_to = spec.partition(":")
            wid_selections.append((wid, rename_to or None))
        elif arg.startswith("--blank="):
            blank_refs.add(arg.split("=", 1)[1])
        elif arg.startswith("--force-update="):
            force_update.add(arg.split("=", 1)[1])
        else:
            names.append(arg)
    if not names and not wid_selections:
        print(__doc__)
        raise SystemExit(1)
    main(names, live, wid_selections, blank_refs, force_update)
