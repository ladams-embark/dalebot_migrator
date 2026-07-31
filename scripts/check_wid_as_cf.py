"""
check_wid_as_cf.py
-------------------
One-off diagnostic: does a given WID resolve as a Calculated_Field WID on the
SOURCE tenant? Used to check whether a report column's External_Field_Reference
WID is actually present in the calculated-field index (and thus should have
been migrated as a dependency) or is something else entirely. Reads only.

Run it:
  python scripts/check_wid_as_cf.py <wid>
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def main(wid: str) -> None:
    target = api.target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )
    conn = api.connect(
        target,
        os.environ["WD_SOURCE_ISU_USERNAME"],
        os.environ["WD_SOURCE_ISU_PASSWORD"],
        role=api.Role.SOURCE,
    )

    result = api.lookup_calculated_field(conn, wid=wid)
    print(f"As a Calculated_Field WID on SOURCE: outcome={result.outcome.value} fault={result.fault}")
    if result.data:
        data = result.data.get("Calculated_Field_Data", {})
        print("  Name:", data.get("Name"))
        print("  Calculated_Field_Reference_ID:", data.get("Calculated_Field_Reference_ID"))
        print("  Class_Name:", data.get("Class_Name"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/check_wid_as_cf.py <wid>")
        raise SystemExit(1)
    main(sys.argv[1])
