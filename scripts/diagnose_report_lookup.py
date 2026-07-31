"""
diagnose_report_lookup.py
--------------------------
One-off diagnostic for the "report has no Name after being added by exact
name" bug. Reads the SOURCE tenant only — no writes, nothing destination-
related touched.

The suspicion: `lookup_report_by_name()` deliberately fetches no report data
(it's the cheap existence probe Conflicts uses against the destination), so
the UI's "Add by exact name" flow follows it with a second, targeted
`lookup_report(wid=...)` call that sets `Include_Tenanted_Report_Definition_Data=True`.
That combination (`Request_References` + `Include_..._Data=True`) has never
been verified live for a REPORT — only its `.outcome` was ever asserted
(tests/test_planner.py), never that `.data` actually carries the Data block.
Given this project's history of Workday quirks that looked fine on paper
(Report_Metadata, Custom_Report_ID as a lookup key), that gap is exactly
where a silent live surprise would hide.

This prints, for one report name, three things side by side:
  1. lookup_report_by_name()  — the existence-probe call (expected: no Name)
  2. lookup_report(wid=...)   — targeted fetch by WID (suspect)
  3. find_report_by_exact_name() — Request_Criteria + Include_Data=True in
     one call (the other, never-live-tested path)

Run it:
  python scripts/diagnose_report_lookup.py "PLNF - All Workers"

Needs WD_SOURCE_* in .env (see .env.example). Reads only.
"""

import json
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def _dump(label: str, obj) -> None:
    print(f"\n=== {label} ===")
    if obj is None:
        print("None")
        return
    print(json.dumps(obj, indent=2, default=str)[:4000])


def main(name: str) -> None:
    import os

    target = api.target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )
    conn = api.connect(
        target,
        os.environ["WD_SOURCE_ISU_USERNAME"],
        os.environ["WD_SOURCE_ISU_PASSWORD"],
        role=api.Role.SOURCE,
    )

    print(f"Looking up report named exactly: {name!r}")

    by_name = api.lookup_report_by_name(conn, name)
    print(f"\n1) lookup_report_by_name -> outcome={by_name.outcome.value} wid={by_name.wid} "
          f"reference_id={by_name.reference_id} fault={by_name.fault}")
    _dump("lookup_report_by_name().data", by_name.data)
    if by_name.data is not None:
        has_data_block = "Tenanted_Report_Definition_Data" in by_name.data
        print(f"   -> has Tenanted_Report_Definition_Data key: {has_data_block}")

    if by_name.wid:
        by_wid = api.lookup_report(conn, wid=by_name.wid)
        print(f"\n2) lookup_report(wid={by_name.wid!r}) -> outcome={by_wid.outcome.value} "
              f"fault={by_wid.fault}")
        _dump("lookup_report(wid=...).data", by_wid.data)
        if by_wid.data is not None:
            has_data_block = "Tenanted_Report_Definition_Data" in by_wid.data
            name_value = (by_wid.data.get("Tenanted_Report_Definition_Data") or {}).get("Name")
            print(f"   -> has Tenanted_Report_Definition_Data key: {has_data_block}")
            print(f"   -> Name value: {name_value!r}")

    summary = api.find_report_by_exact_name(conn, name)
    print(f"\n3) find_report_by_exact_name -> {summary!r}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: python scripts/diagnose_report_lookup.py "Exact Report Name"')
        raise SystemExit(1)
    main(sys.argv[1])
