"""
probe_dashboards.py
-------------------
Phase 0 for custom-dashboard migration: answer, against a live tenant, the
questions the WSDL alone cannot settle. **Read-only** — every call is a Get_*
against the SOURCE tenant.

Six questions, in the order they gate the design:

  1. ENTITLEMENT
     Does this ISU get past `Get_Custom_Dashboards_*` and `Get_Prompt_Sets` at
     all? The `Report_Metadata` precedent (see CLAUDE.md) is that a WSDL can
     define an operation perfectly and the tenant still reject every call.

  2. WHICH FLAVOUR, AND HOW MANY
     There is no `Get_Custom_Dashboards`. There is `_with_Tabs` (object
     `Custom_Landing_Page_Group`) and `_without_Tabs` (object
     `Custom_Landing_Page`) — separate operations, data types and ID spaces.
     Nothing in a reference says which one a given dashboard is, so both get
     swept. Also: does a sweep return *data*, given the Response_Group exposes
     only `Include_Reference` and no `Include_..._Data` flag?

  3. IS THE BUSINESS ID A USABLE LOOKUP KEY
     `Custom_Landing_Page_ID` is in the enumeration. So is `Custom_Report_ID`,
     which is returned by the API and then *rejected* as a lookup key (18/18
     sampled, CLAUDE.md). If dashboards share that trap, destination existence
     probing cannot match on the business ID and has to fall back to sweeping
     and matching by name — which is cheap here only because the volume is low,
     hence question 2.

  4. DASHBOARD -> REPORT EDGE
     A report reaches a dashboard as a *worklet*:
     `Worklets_Data.Worklet__All__Reference`, whose enumeration includes
     `Custom_Report_ID`. If those references carry a WID alongside the business
     ID, the existing `extract_report_refs` already finds them and needs no
     change. That is worth knowing before writing a new extractor.

  5. PROMPT SETS, AND WHICH WAY THE EDGE RUNS
     `Prompt_Set_Request_Criteria` can fetch prompt sets for one dashboard —
     the on-demand loader pattern, no index needed. But
     `Tenanted_Prompt_Set_Member_Data.Abstract_External_Parameter_Reference`
     points at a *report's* parameter, and the report payload carries no
     prompt-set reference in the other direction. If that holds, prompt sets
     are written AFTER their reports, not before — inverting the assumed order.
     If it somehow runs both ways, that is a cycle, and `topological_sort`
     hard-blocks on cycles.

  6. WHAT ELSE IS IN THERE
     Every distinct ID type in the dashboard payload, so the stripping rules
     (announcements, security groups) are written against what the tenant
     actually sends rather than what the schema permits.

Addressing and credentials come from `.env` (WD_SOURCE_*). The target tenant is
echoed before any call — if it is not the tenant you meant, stop.

Run it (from an activated venv, or via .venv\\Scripts\\python.exe):
  python scripts/probe_dashboards.py --dashboard "Commit - Optimize Reporting Dashboard"
  python scripts/probe_dashboards.py --dashboard "..." --dump-payload out/dashboard.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from zeep.helpers import serialize_object

from wdmigrator import api

# Explicit path: load_dotenv() walks up from the *caller's* directory, which is
# not the project root when this is run from anywhere else.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

FLAVOURS = {
    "without_Tabs": {
        "get": "Get_Custom_Dashboards_without_Tabs",
        "item": "Custom_Dashboard_without_Tabs",
        "reference": "Custom_Dashboard_without_Tabs_Reference",
        "data": "Custom_Dashboard_without_Tabs_Data",
        "request_reference": "Custom_Dashboard_without_Tabs_Reference",
        "id_type": "Custom_Landing_Page_ID",
    },
    "with_Tabs": {
        "get": "Get_Custom_Dashboards_with_Tabs",
        "item": "Custom_Dashboard_with_Tabs",
        "reference": "Custom_Dashboard_with_Tabs_Reference",
        "data": "Custom_Dashboard_with_Tabs_Data",
        "request_reference": "Custom_Dashboard_with_Tabs_Reference",
        "id_type": "Custom_Landing_Page_Group_ID",
    },
}


def connect(tenant: str | None = None, services_host: str | None = None):
    """Connect to the SOURCE tenant, with addressing overridable per run.

    Credentials always come from `.env` — a password in argv lands in shell
    history and in the process list — so an override only works if the same ISU
    can read the tenant you point at.
    """
    overridden = bool(tenant or services_host)
    tenant = tenant or os.environ["WD_SOURCE_TENANT"]
    services_host = services_host or os.environ["WD_SOURCE_SERVICES_HOST"]
    username = os.environ["WD_SOURCE_ISU_USERNAME"]
    target = api.target_from_parts(services_host, tenant)

    print("SOURCE TENANT FOR THIS PROBE (read-only)")
    print(f"  tenant: {target.tenant}")
    print(f"  host:   {target.services_host}")
    print(f"  user:   {username}")
    print(f"  addressing from: {'command-line flags' if overridden else '.env'}\n")

    return api.connect(
        target, username, os.environ["WD_SOURCE_ISU_PASSWORD"], role=api.Role.SOURCE
    )


def call(conn, operation, **kwargs):
    """One Get_*, rate-limited, returning (data, fault)."""
    conn.limiter.wait()
    try:
        raw = getattr(conn.service, operation)(**kwargs)
    except Exception as exc:  # noqa: BLE001 - the fault text IS the finding
        return None, conn.redact(str(exc))
    return serialize_object(raw) or {}, None


# ── 1 + 2: entitlement, flavours, volume, does a sweep carry data ────────────


def sweep(conn, flavour):
    spec = FLAVOURS[flavour]
    print(f"--- {spec['get']} ---")

    data, fault = call(
        conn,
        spec["get"],
        Response_Filter={"Page": 1, "Count": api.PAGE_SIZE},
        Response_Group={"Include_Reference": True},
    )
    if fault is not None:
        print(f"  FAULT: {fault}")
        print("  -> This flavour is not readable by this ISU. Everything below "
              "for it is moot.\n")
        return []

    results = data.get("Response_Results") or {}
    total = results.get("Total_Results")
    pages = int(results.get("Total_Pages") or 1)
    items = (data.get("Response_Data") or {}).get(spec["item"]) or []
    print(f"  OK — {total} dashboards, {pages} page(s), {len(items)} on page 1")

    if items:
        first = items[0]
        has_data = bool(first.get(spec["data"]))
        print(f"  Does a sweep carry {spec['data']}? {'YES' if has_data else 'NO'}")
        if not has_data:
            print("  -> Names are not available from a sweep. A browsable picker "
                  "and name-matching both need targeted fetches instead.")
        print(f"  ID types on the reference: {sorted(api.ids_of(first.get(spec['reference'])))}")

    collected = list(items)
    for page in range(2, pages + 1):
        more, fault = call(
            conn,
            spec["get"],
            Response_Filter={"Page": page, "Count": api.PAGE_SIZE},
            Response_Group={"Include_Reference": True},
        )
        if fault is not None:
            print(f"  page {page} FAULT: {fault}")
            break
        collected.extend((more.get("Response_Data") or {}).get(spec["item"]) or [])
    print(f"  collected {len(collected)} total\n")
    return collected


def find_by_name(items, flavour, name):
    spec = FLAVOURS[flavour]
    return [
        item
        for item in items
        if ((item.get(spec["data"]) or {}).get("Name") or "").strip() == name.strip()
    ]


# ── 3: is the business ID a usable lookup key ────────────────────────────────


def probe_lookup_key(conn, flavour, reference):
    spec = FLAVOURS[flavour]
    ids = api.ids_of(reference)
    print(f"--- Is {spec['id_type']} a usable lookup key? ---")

    wid = ids.get("WID")
    business = ids.get(spec["id_type"])
    print(f"  WID:               {wid}")
    print(f"  {spec['id_type']}: {business}")

    if wid:
        data, fault = call(
            conn,
            spec["get"],
            Request_References={
                spec["request_reference"]: [{"ID": [{"type": "WID", "_value_1": wid}]}]
            },
            Response_Group={"Include_Reference": True},
        )
        found = len((data or {}).get("Response_Data", {}).get(spec["item"]) or [])
        print(f"  by WID:            {'FOUND' if found else 'not found'}"
              + (f" — {fault}" if fault else ""))

    if business:
        data, fault = call(
            conn,
            spec["get"],
            Request_References={
                spec["request_reference"]: [
                    {"ID": [{"type": spec["id_type"], "_value_1": business}]}
                ]
            },
            Response_Group={"Include_Reference": True},
        )
        if fault:
            print(f"  by business ID:    REJECTED — {fault}")
            print("  -> Same trap as Custom_Report_ID. Destination probing must "
                  "sweep and match by name.")
        else:
            found = len((data or {}).get("Response_Data", {}).get(spec["item"]) or [])
            print(f"  by business ID:    {'FOUND' if found else 'not found'}")
            if found:
                print("  -> Usable as a cross-tenant identity, unlike Custom_Report_ID.")
    else:
        print(f"  -> No {spec['id_type']} returned at all; WID is the only handle.")
    print()


# ── 4 + 6: what the payload actually references ──────────────────────────────


def id_type_census(payload):
    """Every distinct ID type in the payload, and where it appears."""
    census: dict[str, set[str]] = {}

    def walk(obj, path=""):
        if isinstance(obj, dict):
            entries = obj.get("ID")
            if isinstance(entries, list):
                element = path.split(".")[-1].split("[")[0]
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("type"):
                        census.setdefault(entry["type"], set()).add(element)
            for key, value in obj.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(item, f"{path}[{i}]")

    walk(payload)
    return census


def probe_report_edge(data):
    print("--- Dashboard -> report edge ---")
    found = api.extract_report_refs(data)
    print(f"  extract_report_refs (existing code) finds {len(found)} report(s):")
    for wid, report_id in sorted(found.items(), key=lambda kv: kv[1]):
        print(f"    {report_id:50} wid={wid}")
    if found:
        print("  -> No new extractor needed for the worklet edge.")
    else:
        print("  -> The existing extractor finds nothing. Either this dashboard "
              "has no report worklets, or the references carry no WID sibling "
              "and a dedicated extractor is required.")
    print()
    return found


# ── 5: prompt sets, and which way the edge runs ──────────────────────────────


def probe_prompt_sets(conn, flavour, reference):
    spec = FLAVOURS[flavour]
    ids = api.ids_of(reference)
    wid = ids.get("WID")
    print("--- Prompt sets for this dashboard ---")

    if flavour == "with_Tabs":
        print("  NOTE: Prompt_Set_Request_Criteria.Custom_Dashboard_Reference is")
        print("  typed Custom_Landing_PageObjectType — the without-Tabs object.")
        print("  Whether a with-Tabs dashboard WID is accepted there is exactly")
        print("  what this call tests.")

    data, fault = call(
        conn,
        "Get_Prompt_Sets",
        Request_Criteria={
            "Custom_Dashboard_Reference": {"ID": [{"type": "WID", "_value_1": wid}]}
        },
        Response_Filter={"Page": 1, "Count": api.PAGE_SIZE},
        Response_Group={"Include_Reference": True},
    )
    if fault is not None:
        print(f"  FAULT: {fault}\n")
        return []

    items = (data.get("Response_Data") or {}).get("Prompt_Set") or []
    print(f"  OK — {len(items)} prompt set(s) scoped to this dashboard")

    for item in items:
        pdata = item.get("Prompt_Set_Data") or {}
        pids = api.ids_of(item.get("Prompt_Set_Reference"))
        members = pdata.get("Tenanted_Prompt_Set_Member_Data") or []
        if isinstance(members, dict):
            members = [members]
        print(f"    {pdata.get('Name')!r}")
        print(f"      ids: {pids}")
        print(f"      {len(members)} member(s)")
        for member in members:
            param = member.get("Abstract_External_Parameter_Reference")
            if param:
                print(f"      Abstract_External_Parameter_Reference -> {api.ids_of(param)}")
        print(f"      id types in payload: {sorted(id_type_census(pdata))}")

    if items:
        print("\n  EDGE DIRECTION: if Abstract_External_Parameter_Reference above")
        print("  resolves to a report parameter, the prompt set must be written")
        print("  AFTER its report — inverting the assumed order.")
    print()
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", required=True, help="Exact dashboard name")
    parser.add_argument("--dump-payload", help="Write the full payload here as JSON")
    parser.add_argument("--tenant", help="Override WD_SOURCE_TENANT")
    parser.add_argument("--services-host", help="Override WD_SOURCE_SERVICES_HOST")
    args = parser.parse_args()

    conn = connect(args.tenant, args.services_host)

    swept = {flavour: sweep(conn, flavour) for flavour in FLAVOURS}

    matches = {
        flavour: find_by_name(items, flavour, args.dashboard)
        for flavour, items in swept.items()
    }
    hits = {f: m for f, m in matches.items() if m}

    print("=" * 70)
    print(f"LOOKING FOR: {args.dashboard!r}")
    for flavour, found in matches.items():
        print(f"  {flavour}: {len(found)} match(es)")
    if not hits:
        print("\n  Not found in either flavour. If the sweeps above carried no")
        print("  Name, that is why — matching by name needs targeted fetches.")
        return
    print()

    flavour, found = next(iter(hits.items()))
    if len(found) > 1:
        print(f"  {len(found)} dashboards share this name — probing the first.\n")
    item = found[0]
    spec = FLAVOURS[flavour]
    reference = item.get(spec["reference"])
    data = item.get(spec["data"]) or {}

    print("=" * 70)
    print(f"FLAVOUR: {flavour}\n")

    probe_lookup_key(conn, flavour, reference)
    probe_report_edge(data)
    probe_prompt_sets(conn, flavour, reference)

    print("--- Every ID type in the dashboard payload ---")
    for id_type, elements in sorted(id_type_census(data).items()):
        print(f"  {id_type:45} in {', '.join(sorted(elements))}")
    print()

    print("--- Top-level blocks present ---")
    for key, value in sorted(data.items()):
        if value in (None, [], {}):
            continue
        shape = f"{len(value)} item(s)" if isinstance(value, list) else type(value).__name__
        print(f"  {key:50} {shape}")

    if args.dump_payload:
        path = Path(args.dump_payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(item, indent=2, default=str), encoding="utf-8")
        print(f"\nFull payload written to {path}")


if __name__ == "__main__":
    main()
