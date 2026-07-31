"""
find_cf_by_external_field.py
------------------------------
One-off diagnostic. Hypothesis: a report column's External_Field_Reference
WID, when it points at a CUSTOM calculated field, is not that field's own
Calculated_Field_Reference WID — it's a distinct "exposed field" WID that a
calculated field's own record also carries (as its base External_Field_Reference
field, per CLAUDE.md's Calculated_Field_DataType listing). If that's right,
matching a report column's External_Field_Reference against every
calculated field's OWN External_Field_Reference should find the calculated
field that defines it.

Uses the on-disk CF index cache if present (build it via the app's Select
step first, or scripts/selfcheck-style full sweep) rather than re-sweeping
9,652 fields live. Reads only.

Run it:
  python scripts/find_cf_by_external_field.py <wid>
"""

import json
import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def main(target_wid: str) -> None:
    target = api.target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )
    cache_path = api.cache_path(
        # cache_path only needs .target.tenant; build a throwaway Connection-like
        # object is overkill, so just replicate its path convention directly.
        type("C", (), {"target": target})(),
        "calculated_field",
    )

    index = None
    if os.path.isfile(cache_path):
        index = api.load_index(cache_path, tenant=target.tenant)
        print(f"Loaded cached CF index: {cache_path} ({len(index) if index else 0} fields)")
    else:
        print(f"No cache at {cache_path} — sweeping live (~25s)...")
        conn = api.connect(
            target,
            os.environ["WD_SOURCE_ISU_USERNAME"],
            os.environ["WD_SOURCE_ISU_PASSWORD"],
            role=api.Role.SOURCE,
        )
        index = api.build_index(api.iter_calculated_field_index(conn))
        api.save_index(index, cache_path)

    matches = []
    for wid, payload in index.payloads.items():
        data = payload.get("Calculated_Field_Data") or {}
        ref = data.get("External_Field_Reference") or {}
        ids = api.ids_of(ref)
        if ids.get("WID") == target_wid:
            matches.append((wid, data.get("Name"), data.get("Calculated_Field_Reference_ID")))

    print(f"\nCalculated fields whose own External_Field_Reference == {target_wid}:")
    if not matches:
        print("  (none)")
    for wid, name, ref_id in matches:
        print(f"  wid={wid} name={name!r} reference_id={ref_id!r}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/find_cf_by_external_field.py <wid>")
        raise SystemExit(1)
    main(sys.argv[1])
