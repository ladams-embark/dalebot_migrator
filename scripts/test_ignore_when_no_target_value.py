"""
test_ignore_when_no_target_value.py
--------------------------------------
One-off experiment: does setting Ignore_When_No_Target_Value=True on a
filter condition let Put_Tenanted_Report_Definition succeed even when that
condition's Filter_Instances_Reference points at something the destination
doesn't have? Undocumented in the WSDL (no <xsd:documentation> on the
field) — this is the live test to find out, not a guess.

Fetches "Luke's Fancy Report" from source, finds every filter condition
item carrying a Filter_Instances_Reference, sets Ignore_When_No_Target_Value
= True on each (the reference itself is left untouched — that's the whole
point: prove whether the flag alone is enough), then runs it through the
normal resolve -> plan -> dry-run -> live pipeline.

Run it:
  python scripts/test_ignore_when_no_target_value.py            # dry run only
  python scripts/test_ignore_when_no_target_value.py --live      # dry run, then live if clean
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()

REPORT_NAME = "Luke's Fancy Report"


def patch_condition_items(data: dict) -> int:
    """Recursively find every Condition_Item_Data dict carrying a
    Filter_Instances_Reference and set Ignore_When_No_Target_Value=True on
    it, in place. Returns how many were patched."""
    count = 0

    def walk(obj):
        nonlocal count
        if isinstance(obj, dict):
            if "Filter_Instances_Reference" in obj and obj.get("Filter_Instances_Reference"):
                obj["Ignore_When_No_Target_Value"] = True
                count += 1
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return count


def main(live: bool) -> None:
    source_target = api.target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )
    source_conn = api.connect(
        source_target,
        os.environ["WD_SOURCE_ISU_USERNAME"],
        os.environ["WD_SOURCE_ISU_PASSWORD"],
        role=api.Role.SOURCE,
    )
    dest_target = api.target_from_parts(
        os.environ["WD_DEST_SERVICES_HOST"], os.environ["WD_DEST_TENANT"]
    )
    dest_conn = api.connect(
        dest_target,
        os.environ["WD_DEST_ISU_USERNAME"],
        os.environ["WD_DEST_ISU_PASSWORD"],
        role=api.Role.DESTINATION,
    )

    src_status = api.verify_connection(source_conn)
    dst_status = api.verify_connection(dest_conn)
    print(f"source verified: {src_status.ok} — {src_status.detail}")
    print(f"dest verified:   {dst_status.ok} — {dst_status.detail}")
    if not (src_status.ok and dst_status.ok):
        raise SystemExit("Connection verification failed.")

    # Two-step: resolve the name to a WID safely (handles ambiguity), then a
    # targeted fetch by WID for the full definition (lookup_report_by_name
    # alone carries no data).
    by_name = api.lookup_report_by_name(source_conn, REPORT_NAME)
    if by_name.outcome.value != "found":
        raise SystemExit(f"Could not find {REPORT_NAME!r} on source: {by_name.outcome.value} {by_name.fault}")
    full = api.lookup_report(source_conn, wid=by_name.wid)
    if full.outcome.value != "found" or full.data is None:
        raise SystemExit(f"Could not fetch full definition: {full.fault}")

    report_wid = by_name.wid
    report_data = full.data.get("Tenanted_Report_Definition_Data") or {}
    print(f"\nReport: {report_data.get('Name')!r} ({report_wid})")

    patched = patch_condition_items(report_data)
    print(f"Patched {patched} filter condition item(s) with Ignore_When_No_Target_Value=True")
    if patched == 0:
        print("WARNING: no Filter_Instances_Reference found anywhere on this report. "
              "The failure may be coming from something else — proceeding anyway to see what happens.")

    cf_cache = api.cache_path(source_conn, "calculated_field")
    cf_index = api.load_index(cf_cache, tenant=source_conn.target.tenant)
    if cf_index is None:
        print("No cached CF index — building fresh...")
        cf_index = api.build_index(api.iter_calculated_field_index(source_conn))
        api.save_index(cf_index, cf_cache)

    closure = api.resolve(cf_index, selected_reports={report_wid: full.data})
    print(f"Closure: {closure.counts_by_kind()}")

    print("\nProbing destination existence:")
    existence = {}
    for progress in api.iter_check_existence(dest_conn, closure):
        existence[progress.node.node_id] = progress.existence
        print(f"  {progress.node.kind.value:16} {progress.node.name!r:50} -> {progress.existence.state.value}")

    plan = api.build_plan(closure, existence)
    print(f"\nPlan actions: {plan.counts()}")
    print(f"Plan hash: {plan.plan_hash()}")

    blockers = api.validate_plan(plan)
    if blockers:
        print("\nBLOCKERS — stopping:")
        for b in blockers:
            print(f"  - {b.title}: {b.detail}")
        raise SystemExit(1)

    owner_reference = api.build_owner_reference(workday_username="wd-support")

    dry_guard = api.WriteGuard(
        source=source_conn.target, dest=dest_conn.target, dry_run=True,
        plan_hash=plan.plan_hash(), source_verified=True, dest_verified=True,
        source_username=source_conn.username, dest_username=dest_conn.username,
    )
    print("\n--- DRY RUN ---")
    dry_records = []
    for progress in api.iter_execute(dest_conn, plan, dry_guard, owner_reference=owner_reference):
        dry_records.append(progress.record)
        print(f"  [{progress.position}/{progress.total}] {progress.record.name!r} -> {progress.record.status.value}")
        if progress.record.fault:
            print(f"      fault: {progress.record.fault}")
    print(f"Dry-run summary: {api.summarise(dry_records)}")

    if not live:
        print("\n(dry run only — pass --live to actually execute)")
        return

    if any(r.status.value == "failed" for r in dry_records):
        raise SystemExit("Dry run had failures — not proceeding to live.")

    print("\n--- LIVE EXECUTION ---")
    live_guard = api.WriteGuard(
        source=source_conn.target, dest=dest_conn.target, dry_run=False,
        plan_hash=plan.plan_hash(), confirmed_tenant_name=dest_conn.target.tenant,
        dry_run_reviewed=True, source_verified=True, dest_verified=True,
        source_username=source_conn.username, dest_username=dest_conn.username,
    )
    guard_findings = api.evaluate_guards(live_guard)
    blocking = [g for g in guard_findings if g.level.value == "block"]
    if blocking:
        print("SAFETY GUARD BLOCKED THE LIVE RUN:")
        for g in blocking:
            print(f"  - {g.title}: {g.detail} (remedy: {g.remedy})")
        raise SystemExit(1)

    live_records = []
    for progress in api.iter_execute(dest_conn, plan, live_guard, owner_reference=owner_reference):
        live_records.append(progress.record)
        print(f"  [{progress.position}/{progress.total}] {progress.record.name!r} -> {progress.record.status.value}")
        if progress.record.dest_wid:
            print(f"      dest_wid: {progress.record.dest_wid}")
        if progress.record.fault:
            print(f"      fault: {progress.record.fault}")
    print(f"\nLive execution summary: {api.summarise(live_records)}")


if __name__ == "__main__":
    main(live="--live" in sys.argv)
