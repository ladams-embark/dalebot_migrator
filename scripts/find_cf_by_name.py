"""
find_cf_by_name.py
---------------------
Search the cached CF index (local, client-side — Get_Calculated_Fields has
no server-side name filter) for a field matching a name/substring. Used to
check whether "CF_LRV_-_Home_State" exists as a real Calculated_Field under
a WID *different* from the one referenced on PLNF - All Workers' report
column (da06ec2634331001f8e8b6fa2e4d0000), which returned NOT_FOUND when
looked up directly by that WID.

Run it:
  python scripts/find_cf_by_name.py <substring>
"""

import os
import sys

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def main(substring: str) -> None:
    target = api.target_from_parts(
        os.environ["WD_SOURCE_SERVICES_HOST"], os.environ["WD_SOURCE_TENANT"]
    )

    cache_path = api.cache_path(type("C", (), {"target": target})(), "calculated_field")
    index = api.load_index(cache_path, tenant=target.tenant)
    if index is None:
        raise SystemExit(f"No cached CF index at {cache_path}.")

    needle = substring.lower()
    matches = [
        (wid, s.name, s.reference_id)
        for wid, s in index.summaries.items()
        if s.name and needle in s.name.lower()
    ]
    print(f"{len(matches)} match(es) for {substring!r} in {len(index)} cached fields:")
    for wid, name, ref_id in matches:
        print(f"  wid={wid} name={name!r} reference_id={ref_id!r}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/find_cf_by_name.py <substring>")
        raise SystemExit(1)
    main(sys.argv[1])
