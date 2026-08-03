"""
find_cf_dependent_report.py
-----------------------------
One-off: build/load the source report index (~158s the first time, cached
after) and the cached CF index, then find reports whose closure includes a
genuine Calculated_Field dependency (not the External_Field/Custom_Field
dead end PLNF - All Workers hit). Reads the source tenant only.

Prints candidates sorted by fewest calculated-field dependencies first —
smallest safe live-execution example.

Run it:
  python scripts/find_cf_dependent_report.py [max_candidates]
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def main(max_candidates: int) -> None:
    target = api.target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )
    conn = api.connect(
        target,
        os.environ["WD_SOURCE_ISU_USERNAME"],
        os.environ["WD_SOURCE_ISU_PASSWORD"],
        role=api.Role.SOURCE,
    )

    cf_cache = api.cache_path(conn, "calculated_field")
    cf_index = api.load_index(cf_cache, tenant=target.tenant) if os.path.isfile(cf_cache) else None
    if cf_index is None:
        print("Building CF index (~25s)...")
        cf_index = api.build_index(api.iter_calculated_field_index(conn))
        api.save_index(cf_index, cf_cache)
    print(f"CF index: {len(cf_index)} fields")

    report_cache = api.cache_path(conn, "report")
    report_index = api.load_index(report_cache, tenant=target.tenant) if os.path.isfile(report_cache) else None
    if report_index is None:
        print("Building report index (~158s, one time)...")

        def on_progress(p):
            print(f"  page {p.page}/{p.total_pages} ({p.fetched}/{p.total})", flush=True)

        report_index = api.build_index(api.iter_report_index(conn), on_progress=on_progress)
        api.save_index(report_index, report_cache)
    print(f"Report index: {len(report_index)} reports")

    candidates = []
    for wid, payload in report_index.payloads.items():
        summary = report_index.summaries.get(wid)
        if summary is None or not summary.name:
            continue
        try:
            closure = api.resolve(cf_index, selected_reports={wid: payload})
        except Exception:
            continue
        cf_count = closure.counts_by_kind().get("calculated_field", 0)
        if cf_count > 0:
            candidates.append((cf_count, summary.name, wid))

    candidates.sort()
    print(f"\n{len(candidates)} reports depend on at least one calculated field. Smallest first:")
    for cf_count, name, wid in candidates[:max_candidates]:
        print(f"  {cf_count} CF dep(s) — {name!r} (wid={wid})")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
