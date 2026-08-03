"""
refresh_cf_index_and_search.py
---------------------------------
Rebuild the source CF index fresh (not from cache — a newly created global
calculated field wouldn't be in yesterday's cache) and search it by name.
Read-only.

Run it:
  python scripts/refresh_cf_index_and_search.py <substring>
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
    conn = api.connect(
        target,
        os.environ["WD_SOURCE_ISU_USERNAME"],
        os.environ["WD_SOURCE_ISU_PASSWORD"],
        role=api.Role.SOURCE,
    )

    print("Sweeping CF index fresh (~25-40s)...")

    def on_progress(p):
        print(f"  page {p.page}/{p.total_pages} ({p.fetched}/{p.total})", flush=True)

    index = api.build_index(api.iter_calculated_field_index(conn), on_progress=on_progress)
    cache_path = api.cache_path(conn, "calculated_field")
    api.save_index(index, cache_path)
    print(f"Refreshed and cached: {len(index)} fields at {cache_path}")

    needle = substring.lower()
    matches = [
        (wid, s.name, s.reference_id)
        for wid, s in index.summaries.items()
        if s.name and needle in s.name.lower()
    ]
    print(f"\n{len(matches)} match(es) for {substring!r}:")
    for wid, name, ref_id in matches:
        print(f"  wid={wid} name={name!r} reference_id={ref_id!r}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/refresh_cf_index_and_search.py <substring>")
        raise SystemExit(1)
    main(sys.argv[1])
