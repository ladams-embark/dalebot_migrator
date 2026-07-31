"""
migrate_report_example.py
--------------------------
End-to-end pipeline for ONE report and its calculated-field dependencies:
resolve closure -> probe destination existence -> build plan -> validate ->
dry run. Always stops after the dry run and prints the results.

Live execution is a deliberately separate, explicit step
(migrate_live_execute.py) — never bundled into the same run as the dry run,
per the project's hard rule that a live write always needs its own
confirmation, not a flag on a script that already ran.

Run it:
  python scripts/migrate_report_example.py <report_wid>
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def build_connections():
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
    return source_conn, dest_conn


def main(report_wid: str) -> None:
    source_conn, dest_conn = build_connections()

    src_status = api.verify_connection(source_conn)
    dst_status = api.verify_connection(dest_conn)
    print(f"source verified: {src_status.ok} — {src_status.detail}")
    print(f"dest verified:   {dst_status.ok} — {dst_status.detail}")
    if not (src_status.ok and dst_status.ok):
        raise SystemExit("Connection verification failed.")

    if source_conn.target.identity() == dest_conn.target.identity():
        raise SystemExit("Source and destination are the same tenant — refusing even to plan a live path.")

    cf_cache = api.cache_path(source_conn, "calculated_field")
    cf_index = api.load_index(cf_cache, tenant=source_conn.target.tenant)
    if cf_index is None:
        raise SystemExit(f"No cached CF index at {cf_cache} — run find_cf_dependent_report.py first.")

    report_result = api.lookup_report(source_conn, wid=report_wid)
    if report_result.outcome.value != "found" or report_result.data is None:
        raise SystemExit(f"Could not fetch report {report_wid}: {report_result.fault}")

    report_name = (report_result.data.get("Tenanted_Report_Definition_Data") or {}).get("Name")
    print(f"\nReport: {report_name!r} ({report_wid})")

    closure = api.resolve(cf_index, selected_reports={report_wid: report_result.data})
    print(f"Closure: {closure.counts_by_kind()}")

    print("\nProbing destination existence:")
    existence = {}
    for progress in api.iter_check_existence(dest_conn, closure):
        existence[progress.node.node_id] = progress.existence
        print(
            f"  {progress.node.kind.value:16} {progress.node.name!r:50} -> {progress.existence.state.value}"
            + (f" ({progress.existence.fault})" if progress.existence.fault else "")
        )

    plan = api.build_plan(closure, existence)
    print(f"\nPlan actions: {plan.counts()}")
    print(f"Plan hash: {plan.plan_hash()}")

    blockers = api.validate_plan(plan)
    if blockers:
        print("\nBLOCKERS — stopping before dry run:")
        for b in blockers:
            print(f"  - {b.title}: {b.detail}")
            print(f"    remedy: {b.remedy}")
        raise SystemExit(1)
    print("No blockers.")

    # Every report this tool creates is owned by the fixed destination
    # support account, not the source owner or the connecting ISU.
    owner_reference = api.build_owner_reference(workday_username="wd-support")

    guard = api.WriteGuard(
        source=source_conn.target,
        dest=dest_conn.target,
        dry_run=True,
        plan_hash=plan.plan_hash(),
        source_verified=True,
        dest_verified=True,
        source_username=source_conn.username,
        dest_username=dest_conn.username,
    )

    print("\n--- DRY RUN ---")
    dry_records = []
    for progress in api.iter_execute(dest_conn, plan, guard, owner_reference=owner_reference):
        dry_records.append(progress.record)
        print(f"  [{progress.position}/{progress.total}] {progress.record.name!r} -> {progress.record.status.value}")
        if progress.record.fault:
            print(f"      fault: {progress.record.fault}")

    print(f"\nDry-run summary: {api.summarise(dry_records)}")
    print(f"\nPlan hash for a live run of this exact plan: {plan.plan_hash()}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/migrate_report_example.py <report_wid>")
        raise SystemExit(1)
    main(sys.argv[1])
