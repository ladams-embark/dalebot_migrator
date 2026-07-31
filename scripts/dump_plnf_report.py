"""
dump_plnf_report.py
--------------------
One-off diagnostic: dump the full "PLNF - All Workers" report payload from
the SOURCE tenant to a local JSON file, and locate every field path that
references a given WID (used to find which field on the report was behind
the "Invalid ID value ... is not a valid ID value for type = 'WID'" fault
on a live destination PUT). Reads the source tenant only. No writes.

Run it:
  python scripts/dump_plnf_report.py [wid_to_locate]
"""

import json
import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()

REPORT_WID = "2c942d935d6910010c3d14b9a6840000"  # PLNF - All Workers, source tenant


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


def main(target_wid: str | None) -> None:
    target = api.target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )
    conn = api.connect(
        target,
        os.environ["WD_SOURCE_ISU_USERNAME"],
        os.environ["WD_SOURCE_ISU_PASSWORD"],
        role=api.Role.SOURCE,
    )

    result = api.lookup_report(conn, wid=REPORT_WID)
    assert result.outcome.value == "found", result.fault

    out_path = os.path.join(os.path.dirname(__file__), "..", "out", "plnf_report_full.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result.data, f, indent=2, default=str)
    print(f"wrote {os.path.abspath(out_path)}")

    if target_wid:
        paths = find_paths(result.data, target_wid)
        print(f"\nOccurrences of {target_wid}:")
        for p in paths:
            print(" ", p)
        if not paths:
            print("  (not found anywhere in this report's own payload)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
