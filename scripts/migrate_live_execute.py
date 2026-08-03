"""
migrate_live_execute.py
-------------------------
LIVE write. Rebuilds the exact same plan as migrate_report_example.py
(same resolve -> probe -> build_plan sequence) and verifies its hash
matches the one already reviewed in dry run before doing anything — if
source-tenant data changed between the dry run and now, the hash will
differ and this refuses to proceed rather than execute a plan nobody
actually reviewed.

Sequential, child-most-first (calculated fields before the report that
references them), stop_on_failure=True. Every record — success or not —
gets printed; nothing is silently dropped.

Run it:
  python scripts/migrate_live_execute.py <report_wid> <expected_plan_hash>
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def main(report_wid: str, expected_plan_hash: str) -> None:
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

    cf_cache = api.cache_path(source_conn, "calculated_field")
    cf_index = api.load_index(cf_cache, tenant=source_conn.target.tenant)
    if cf_index is None:
        raise SystemExit("No cached CF index — run find_cf_dependent_report.py first.")

    report_result = api.lookup_report(source_conn, wid=report_wid)
    if report_result.outcome.value != "found" or report_result.data is None:
        raise SystemExit(f"Could not fetch report {report_wid}: {report_result.fault}")

    closure = api.resolve(cf_index, selected_reports={report_wid: report_result.data})

    print("Re-probing destination existence (must be re-checked, not reused from dry run):")
    existence = {}
    for progress in api.iter_check_existence(dest_conn, closure):
        existence[progress.node.node_id] = progress.existence
        print(f"  {progress.node.kind.value:16} {progress.node.name!r:50} -> {progress.existence.state.value}")

    plan = api.build_plan(closure, existence)
    actual_hash = plan.plan_hash()
    print(f"\nRebuilt plan hash: {actual_hash}")
    print(f"Expected (dry-run) plan hash: {expected_plan_hash}")
    if actual_hash != expected_plan_hash:
        raise SystemExit(
            "Plan hash mismatch — the plan changed since the dry run that was reviewed. "
            "Refusing to execute live against an unreviewed plan."
        )

    blockers = api.validate_plan(plan)
    if blockers:
        print("\nBLOCKERS — refusing to execute live:")
        for b in blockers:
            print(f"  - {b.title}: {b.detail}")
        raise SystemExit(1)

    # Every report this tool creates is owned by the fixed destination
    # support account, not the source owner or the connecting ISU.
    owner_reference = api.build_owner_reference(workday_username="wd-support")

    guard = api.WriteGuard(
        source=source_conn.target,
        dest=dest_conn.target,
        dry_run=False,
        plan_hash=plan.plan_hash(),
        confirmed_tenant_name=dest_conn.target.tenant,
        dry_run_reviewed=True,
        source_verified=True,
        dest_verified=True,
        source_username=source_conn.username,
        dest_username=dest_conn.username,
    )

    guard_findings = api.evaluate_guards(guard)
    blocking = [g for g in guard_findings if g.level.value == "block"]
    if blocking:
        print("\nSAFETY GUARD BLOCKED THE LIVE RUN:")
        for g in blocking:
            print(f"  - {g.title}: {g.detail} (remedy: {g.remedy})")
        raise SystemExit(1)
    for g in guard_findings:
        print(f"  guard warning: {g.title} — {g.detail}")

    print("\n--- LIVE EXECUTION ---")
    records = []
    for progress in api.iter_execute(dest_conn, plan, guard, owner_reference=owner_reference):
        records.append(progress.record)
        print(f"  [{progress.position}/{progress.total}] {progress.record.name!r} -> {progress.record.status.value}")
        if progress.record.dest_wid:
            print(f"      dest_wid: {progress.record.dest_wid}")
        if progress.record.exceptions:
            for exc in progress.record.exceptions:
                print(f"      exception: {exc.classification} — {exc.message}")
        if progress.record.fault:
            print(f"      fault: {progress.record.fault}")

    print(f"\nLive execution summary: {api.summarise(records)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python scripts/migrate_live_execute.py <report_wid> <expected_plan_hash>")
        raise SystemExit(1)
    main(sys.argv[1], sys.argv[2])
