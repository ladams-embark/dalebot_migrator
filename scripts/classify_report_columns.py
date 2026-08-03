"""
classify_report_columns.py
-----------------------------
Fetch a report fresh, enumerate every column's External_Field_Reference WID,
and check each one against Get_Calculated_Fields on the SOURCE tenant to see
which are genuine calculated fields (migratable) versus something else
(not). Read-only.

Run it:
  python scripts/classify_report_columns.py <report_wid>
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def main(report_wid: str) -> None:
    target = api.target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )
    conn = api.connect(
        target,
        os.environ["WD_SOURCE_ISU_USERNAME"],
        os.environ["WD_SOURCE_ISU_PASSWORD"],
        role=api.Role.SOURCE,
    )

    report = api.lookup_report(conn, wid=report_wid)
    if report.outcome.value != "found" or report.data is None:
        raise SystemExit(f"Could not fetch report: {report.fault}")

    data = report.data.get("Tenanted_Report_Definition_Data") or {}
    print(f"Report: {data.get('Name')!r}")
    columns = data.get("Tenanted_Report_Column_Data") or []
    print(f"{len(columns)} column(s)\n")

    calculated_fields = []
    other = []

    for col in columns:
        col_id = col.get("Report_Column_ID")
        label = col.get("Label_Override_XML_Alias") or col.get("Label_Override")
        ext_ids = api.ids_of(col.get("External_Field_Reference"))
        wid = ext_ids.get("WID")
        if not wid:
            print(f"[{col_id}] {label!r}: no External_Field_Reference")
            continue

        result = api.lookup_calculated_field(conn, wid=wid)
        if result.outcome.value == "found":
            cf_name = (result.data or {}).get("Calculated_Field_Data", {}).get("Name")
            cf_ref_id = (result.data or {}).get("Calculated_Field_Data", {}).get(
                "Calculated_Field_Reference_ID"
            )
            print(f"[{col_id}] {label!r}: CALCULATED FIELD -> {cf_name!r} (wid={wid}, ref_id={cf_ref_id!r})")
            calculated_fields.append((wid, cf_name, cf_ref_id))
        else:
            print(f"[{col_id}] {label!r}: not a calculated field (wid={wid}, outcome={result.outcome.value})")
            other.append((wid, label))

    print(f"\n{len(calculated_fields)} column(s) are genuine calculated fields; {len(other)} are not.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/classify_report_columns.py <report_wid>")
        raise SystemExit(1)
    main(sys.argv[1])
