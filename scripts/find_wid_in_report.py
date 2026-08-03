"""
find_wid_in_report.py
------------------------
Fetch a report fresh and locate every field path referencing a given WID.
Read-only.

Run it:
  python scripts/find_wid_in_report.py <report_wid> <target_wid>
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def find_paths(obj, target, path=""):
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            hits.extend(find_paths(v, target, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(find_paths(v, target, f"{path}[{i}]"))
    elif obj == target:
        hits.append(path)
    return hits


def main(report_wid: str, target_wid: str) -> None:
    target = api.target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )
    conn = api.connect(
        target,
        os.environ["WD_SOURCE_ISU_USERNAME"],
        os.environ["WD_SOURCE_ISU_PASSWORD"],
        role=api.Role.SOURCE,
    )
    result = api.lookup_report(conn, wid=report_wid)
    assert result.outcome.value == "found", result.fault

    paths = find_paths(result.data, target_wid)
    print(f"Occurrences of {target_wid}:")
    for p in paths:
        print(" ", p)
    if not paths:
        print("  (not found)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python scripts/find_wid_in_report.py <report_wid> <target_wid>")
        raise SystemExit(1)
    main(sys.argv[1], sys.argv[2])
