"""
verify_dest_objects.py
------------------------
Post-migration sanity check: fetch the two objects just created on the
destination tenant by their new WIDs and confirm they read back correctly.
Reads the destination tenant only.

Run it:
  python scripts/verify_dest_objects.py
"""

import os

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()

CF_DEST_WID = "2bf676e597c31000bc25073b67c60000"
REPORT_DEST_WID = "3027e60674561000bcd934f424510000"


def main() -> None:
    target = api.target_from_parts(
        os.environ["WD_DEST_SERVICES_HOST"], os.environ["WD_DEST_TENANT"]
    )
    conn = api.connect(
        target,
        os.environ["WD_DEST_ISU_USERNAME"],
        os.environ["WD_DEST_ISU_PASSWORD"],
        role=api.Role.DESTINATION,
    )

    cf = api.lookup_calculated_field(conn, wid=CF_DEST_WID)
    print(f"Calculated field: outcome={cf.outcome.value}")
    if cf.data:
        data = cf.data.get("Calculated_Field_Data", {})
        print(f"  Name: {data.get('Name')!r}")
        print(f"  Class_Name: {data.get('Class_Name')}")
        print(f"  Calculated_Field_Reference_ID: {data.get('Calculated_Field_Reference_ID')}")

    report = api.lookup_report(conn, wid=REPORT_DEST_WID)
    print(f"\nReport: outcome={report.outcome.value}")
    if report.data:
        data = report.data.get("Tenanted_Report_Definition_Data", {})
        print(f"  Name: {data.get('Name')!r}")
        owner_ids = api.ids_of(data.get("Tenanted_Report_Definition_System_User_Reference"))
        print(f"  Owner: {owner_ids}")
        columns = data.get("Tenanted_Report_Column_Data") or []
        print(f"  Columns: {len(columns)}")
        for col in columns:
            ext_ids = api.ids_of(col.get("External_Field_Reference"))
            print(f"    Report_Column_ID={col.get('Report_Column_ID')} External_Field WID={ext_ids.get('WID')}")

        top_filter = data.get("Tenanted_Report_Definition_Top_Level_Filter_Data")
        print(f"  Top_Level_Filter_Data: {top_filter}")


if __name__ == "__main__":
    main()
