"""
probe_time_calculations.py
--------------------------
Phase 0 for time-calculation migration: answer, against a live tenant, the
questions the WSDL alone cannot settle. **Read-only** — every call is a Get_*
against the SOURCE tenant.

Six questions, in the order they gate the design:

  1. ENTITLEMENT
     Which Time_Calculation-shaped operations does this WSDL actually expose,
     and does this ISU get past them? The Report_Metadata / dashboard precedent
     (see CLAUDE.md) is that a WSDL can define an operation perfectly and the
     tenant still reject every call, or gate it behind an implementer account.

  2. SWEEP SHAPE AND VOLUME
     How many time calculations does the source tenant hold, does a sweep
     carry Data or only References (like `Get_Custom_Dashboards_*`), and is
     the top-level container the `_Data` element or something else? This is
     what decides whether a browsable picker exists or the UI has to fetch
     per-object first.

  3. IS THE BUSINESS ID A USABLE LOOKUP KEY
     `Time_Calculation_ID` is presumably in the enumeration. So is
     `Custom_Report_ID`, which is returned by the API and then *rejected* as
     a lookup key (18/18 sampled, CLAUDE.md). If time calcs share that trap,
     destination existence probing cannot match on the business ID and has
     to fall back to sweeping and matching by name.

  4. INTERNAL REFERENCES (what does a TC point at)
     A time calculation is presumably an expression tree over calculated
     fields, delivered time-tracking objects, other time calculations, and
     maybe worker/position references. The census of ID types in one real
     payload tells the resolver what edges to walk.

  5. REVERSE EDGE (who points at a TC)
     Do reports, dashboards, or calculated fields reference time calculations?
     If so, TCs are mid-DAG (must be written before them). If not, TCs are a
     leaf and can migrate independently. This probe grep's a sample report and
     dashboard payload for anything shaped like a Time_Calculation reference.

  6. WHAT ELSE IS IN THERE
     Every distinct ID type in the payload, so stripping rules (owner refs,
     tenanted security groups, etc.) can be written against what the tenant
     actually sends rather than what the schema permits.

Addressing and credentials come from `.env` (WD_SOURCE_*). If the worktree has
no local `.env`, the main checkout's `.env` (found via `git rev-parse
--git-common-dir`) is used. The target tenant is echoed before any call — if it
is not the tenant you meant, stop.

Run it (from an activated venv, or via .venv\\Scripts\\python.exe):
  python scripts/probe_time_calculations.py
  python scripts/probe_time_calculations.py --name "Overtime Hours"
  python scripts/probe_time_calculations.py --dump-payload out/time_calc.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from zeep.helpers import serialize_object

from wdmigrator import api


def load_env() -> Path:
    """Try local `.env` first, then walk to the main worktree's `.env`.

    The migration branch typically lives in a worktree that has no `.env` of
    its own — copying credentials into every worktree is exactly the mistake
    the memory note about credentials in chat warns about.
    """
    here = Path(__file__).resolve().parent.parent
    local = here / ".env"
    if local.exists():
        load_dotenv(local)
        return local

    try:
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=here, text=True
        ).strip()
    except Exception:
        common = ""
    if common:
        main_root = (Path(common).resolve().parent if Path(common).is_absolute()
                     else (here / common).resolve().parent)
        candidate = main_root / ".env"
        if candidate.exists():
            load_dotenv(candidate)
            return candidate

    raise SystemExit(
        f"No .env found at {local} or in the main worktree. Copy .env in, or "
        "point at it with `dotenv -f <path> run ...`."
    )


# ── 1: enumerate what the WSDL exposes ───────────────────────────────────────


def enumerate_ops(conn) -> dict[str, list[str]]:
    """Every service op mentioning Time or Calculation, split by verb."""
    ops = [op for op in dir(conn.service) if not op.startswith("_")]
    buckets = {"Get": [], "Put": [], "Other": []}
    for op in sorted(ops):
        if "Time" not in op and "Calculation" not in op:
            continue
        if op.startswith("Get_"):
            buckets["Get"].append(op)
        elif op.startswith("Put_"):
            buckets["Put"].append(op)
        else:
            buckets["Other"].append(op)
    return buckets


def call(conn, operation, **kwargs):
    """One Get_*, rate-limited, returning (data, fault).

    Retries once without Response_Group if the fault mentions that arg — the
    Time_Tracking_Implementation_Service ops accept only Request_References,
    Response_Filter, and version.
    """
    conn.limiter.wait()
    try:
        raw = getattr(conn.service, operation)(**kwargs)
    except Exception as exc:  # noqa: BLE001 - the fault text IS the finding
        message = str(exc)
        if "Response_Group" in kwargs and "Response_Group" in message:
            kwargs.pop("Response_Group", None)
            conn.limiter.wait()
            try:
                raw = getattr(conn.service, operation)(**kwargs)
            except Exception as exc2:  # noqa: BLE001
                return None, conn.redact(str(exc2))
            return serialize_object(raw) or {}, None
        return None, conn.redact(message)
    return serialize_object(raw) or {}, None


# ── 2: sweep shape and volume ────────────────────────────────────────────────


def sweep(conn, get_op: str):
    """Page 1 of Get_Time_Calculations. Returns (items, response_shape)."""
    print(f"--- {get_op} (page 1, Count={api.PAGE_SIZE}) ---")

    # Try the plain sweep with Include_Reference. If the WSDL insists on a
    # richer Response_Group (as some ops do), the fault text will say so.
    data, fault = call(
        conn,
        get_op,
        Response_Filter={"Page": 1, "Count": api.PAGE_SIZE},
        Response_Group={"Include_Reference": True},
    )
    if fault is not None:
        # A common variant: the op requires no Response_Group at all, or a
        # different flag. Retry with just the filter.
        print(f"  first attempt FAULT: {fault}")
        print("  retrying without Response_Group…")
        data, fault = call(
            conn,
            get_op,
            Response_Filter={"Page": 1, "Count": api.PAGE_SIZE},
        )
        if fault is not None:
            print(f"  retry FAULT: {fault}")
            print("  -> This operation is not readable by this ISU on this tenant.\n")
            return [], {}

    results = data.get("Response_Results") or {}
    total = results.get("Total_Results")
    pages = int(results.get("Total_Pages") or 1)
    top_level_keys = sorted((data.get("Response_Data") or {}).keys())
    print(f"  OK — Total_Results={total}, Total_Pages={pages}")
    print(f"  Response_Data top-level keys: {top_level_keys}")

    # The container element name is not knowable a priori — pick the first list.
    container = None
    items: list = []
    for key, value in (data.get("Response_Data") or {}).items():
        if isinstance(value, list) and value:
            container = key
            items = value
            break
        if isinstance(value, dict):
            container = key
            items = [value]
            break
    print(f"  container element: {container!r}  ({len(items)} on page 1)\n")
    return items, {"container": container, "total": total, "pages": pages}


def collect_all(conn, get_op: str, container: str, pages: int) -> list:
    items: list = []
    for page in range(1, pages + 1):
        data, fault = call(
            conn,
            get_op,
            Response_Filter={"Page": page, "Count": api.PAGE_SIZE},
            Response_Group={"Include_Reference": True},
        )
        if fault is not None:
            print(f"  page {page} FAULT: {fault}")
            break
        page_items = (data.get("Response_Data") or {}).get(container) or []
        if isinstance(page_items, dict):
            page_items = [page_items]
        items.extend(page_items)
    return items


# ── 3 + 6: shape of one item ─────────────────────────────────────────────────


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


def top_level_shape(item, reference_key_guess: str | None):
    print("--- Item shape ---")
    ref = None
    data = None
    for key in item.keys():
        if key.endswith("_Reference"):
            ref = key
        if key.endswith("_Data"):
            data = key
    print(f"  reference key: {ref}")
    print(f"  data key:      {data}")
    if ref:
        print(f"  ID types on reference: {sorted(api.ids_of(item.get(ref)))}")
    return ref, data


def probe_lookup_key(conn, get_op: str, request_ref_name: str, reference_key: str,
                     container: str, reference_value: dict):
    ids = api.ids_of(reference_value)
    print(f"--- Is Time_Calculation_ID a usable lookup key? ---")
    wid = ids.get("WID")
    business = ids.get("Time_Calculation_ID")
    print(f"  WID:                 {wid}")
    print(f"  Time_Calculation_ID: {business}")

    if wid:
        data, fault = call(
            conn,
            get_op,
            Request_References={
                request_ref_name: [{"ID": [{"type": "WID", "_value_1": wid}]}]
            },
            Response_Group={"Include_Reference": True},
        )
        found = len(((data or {}).get("Response_Data") or {}).get(container) or [])
        print(f"  by WID:              {'FOUND' if found else 'not found'}"
              + (f" — {fault}" if fault else ""))

    if business:
        data, fault = call(
            conn,
            get_op,
            Request_References={
                request_ref_name: [
                    {"ID": [{"type": "Time_Calculation_ID", "_value_1": business}]}
                ]
            },
            Response_Group={"Include_Reference": True},
        )
        if fault:
            print(f"  by business ID:      REJECTED — {fault}")
            print("  -> Same trap as Custom_Report_ID. Cross-tenant matching "
                  "must fall back to (Name, shape) tiers.")
        else:
            found = len(((data or {}).get("Response_Data") or {}).get(container) or [])
            print(f"  by business ID:      {'FOUND' if found else 'not found'}")
            if found:
                print("  -> Usable as a stable cross-tenant identity.")
    else:
        print("  -> No Time_Calculation_ID returned at all; WID is the only handle.")
    print()


def find_by_name(items, data_key: str, name: str):
    matches = []
    for item in items:
        d = item.get(data_key) or {}
        # Try Name, Time_Calculation_Name, then any *Name key.
        for key in ("Name", "Time_Calculation_Name"):
            value = d.get(key)
            if isinstance(value, str) and value.strip() == name.strip():
                matches.append(item)
                break
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", help="Exact time-calculation name to probe. "
                        "If omitted, the first item on page 1 is probed.")
    parser.add_argument("--dump-payload", help="Write the probed item as JSON")
    parser.add_argument("--dump-index", help="Write the full index (references only) as JSON")
    parser.add_argument("--tenant", help="Override WD_SOURCE_TENANT")
    parser.add_argument("--services-host", help="Override WD_SOURCE_SERVICES_HOST")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Cap on pages to collect (default: all)")
    parser.add_argument("--service", default=None,
                        help="Override WSDL service (default: Core_Implementation_Service; "
                             "try Time_Tracking, Absence_Management, Compensation)")
    parser.add_argument("--probe-services", action="store_true",
                        help="Enumerate Time/Calculation ops across a list of "
                             "candidate services and exit")
    args = parser.parse_args()

    env_path = load_env()
    print(f"env loaded from: {env_path}\n")

    overridden = bool(args.tenant or args.services_host)
    tenant = args.tenant or os.environ["WD_SOURCE_TENANT"]
    services_host = args.services_host or os.environ["WD_SOURCE_SERVICES_HOST"]
    username = os.environ["WD_SOURCE_ISU_USERNAME"]
    target = api.target_from_parts(services_host, tenant)

    print("SOURCE TENANT FOR THIS PROBE (read-only)")
    print(f"  tenant: {target.tenant}")
    print(f"  host:   {target.services_host}")
    print(f"  user:   {username}")
    print(f"  addressing from: {'command-line flags' if overridden else '.env'}\n")

    def connect_for(service_name: str | None):
        return api.connect(
            target, username, os.environ["WD_SOURCE_ISU_PASSWORD"],
            role=api.Role.SOURCE,
            service_name=service_name,
        )

    if args.probe_services:
        candidates = [
            "Core_Implementation_Service",
            "Time_Tracking",
            "Time_Tracking_Setup",
            "Time_Calculation",
            "Time_Calculations",
            "Time_Off",
            "Absence_Management",
            "Human_Resources",
            "Workforce_Planning",
            "Workforce_Enablement",
            "Compensation",
            "Payroll",
            "Payroll_Interface",
            "Staffing",
            "Financial_Management",
            "Benefits_Administration",
            "Recruiting",
            "Learning",
            "Performance_Management",
            "Talent",
            "Integrations",
            "External_Integrations",
            "Notification",
            "Identity_Management",
        ]
        print("=" * 70)
        print("Probing candidate services for Time/Calculation ops\n")
        for service_name in candidates:
            print(f"--- {service_name} ---")
            try:
                probe_conn = connect_for(service_name)
            except Exception as exc:  # noqa: BLE001
                print(f"  connect FAILED: {exc.__class__.__name__}: {exc}\n")
                continue
            buckets = enumerate_ops(probe_conn)
            if any(buckets.values()):
                for verb, ops in buckets.items():
                    if ops:
                        print(f"  {verb}:")
                        for op in ops:
                            print(f"    {op}")
            else:
                print("  (no Time/Calculation ops on this service)")
            print()
        return

    conn = connect_for(args.service)

    # ── 1: what does the WSDL expose ────────────────────────────────────────
    buckets = enumerate_ops(conn)
    print("=" * 70)
    print(f"STEP 1 — Time/Calculation operations exposed on {conn.service_name}")
    for verb, ops in buckets.items():
        print(f"  {verb}:")
        for op in ops:
            print(f"    {op}")
    print()

    get_ops = buckets["Get"]
    # Prefer the plain Time_Calculation sweep over Groups / Tags — those are
    # separate kinds and want their own probe run.
    tc_get = (
        next((op for op in get_ops if op == "Get_Time_Calculations"), None)
        or next((op for op in get_ops if "Time_Calculation" in op), None)
    )
    if not tc_get:
        print("No Get_Time_Calculation* operation is exposed by this WSDL/version.")
        print("Time calculations may live under a separate service (WTT / Time")
        print("Tracking) rather than Core_Implementation_Service. Design must")
        print("branch here — either add a second zeep client for that service,")
        print("or drop time-calculation migration from scope.")
        return

    print(f"Using: {tc_get}\n")

    # ── 2: sweep ────────────────────────────────────────────────────────────
    print("=" * 70)
    print("STEP 2 — Sweep\n")
    items, shape = sweep(conn, tc_get)
    if not items:
        return

    container = shape["container"]
    pages = shape["pages"]
    if args.max_pages is not None:
        pages = min(pages, args.max_pages)

    if args.dump_index:
        all_items = collect_all(conn, tc_get, container, pages)
        path = Path(args.dump_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        # References only — the sweep already carries no _Data.
        thin = [
            {k: v for k, v in it.items() if not k.endswith("_Data")}
            for it in all_items
        ]
        path.write_text(json.dumps(thin, indent=2, default=str), encoding="utf-8")
        print(f"  full thin index ({len(thin)}) written to {path}\n")

    # ── pick the item to inspect ────────────────────────────────────────────
    ref_key, data_key = top_level_shape(items[0], None)
    if data_key is None:
        # sweep did not carry _Data — need targeted Get. Fetch the first item.
        wid = api.ids_of(items[0].get(ref_key)).get("WID")
        print(f"\n  sweep did not carry _Data. Fetching by WID {wid}…")
        # Guess the Request_References element name from the WSDL type name.
        # For most flavours it is the same as the reference key.
        req_ref_name = ref_key
        data, fault = call(
            conn,
            tc_get,
            Request_References={
                req_ref_name: [{"ID": [{"type": "WID", "_value_1": wid}]}]
            },
            Response_Group={"Include_Reference": True},
        )
        if fault:
            print(f"  targeted Get FAULT: {fault}")
            print(f"  Response_Group may need additional Include_* flags — inspect "
                  f"the WSDL for {tc_get}Response_Group.")
            return
        fetched = ((data or {}).get("Response_Data") or {}).get(container) or []
        if isinstance(fetched, dict):
            fetched = [fetched]
        if not fetched:
            print("  targeted Get returned nothing.")
            return
        item = fetched[0]
        ref_key, data_key = top_level_shape(item, None)
    else:
        if args.name:
            matches = find_by_name(items, data_key, args.name)
            if not matches:
                # Not on page 1 — collect all and search.
                all_items = collect_all(conn, tc_get, container, pages)
                matches = find_by_name(all_items, data_key, args.name)
            if not matches:
                print(f"\n  No time calculation named {args.name!r}. Aborting.")
                return
            item = matches[0]
        else:
            item = items[0]

    print()
    reference = item.get(ref_key) if ref_key else None
    data = item.get(data_key) or {} if data_key else {}

    # ── 3: lookup key ───────────────────────────────────────────────────────
    if reference:
        # Best guess for the request wrapper name (SOAP requires the singular
        # form of the item element inside Request_References).
        request_ref_name = ref_key
        probe_lookup_key(conn, tc_get, request_ref_name, ref_key, container, reference)

    # ── 4 + 6: internal references, ID census ───────────────────────────────
    print("--- Every ID type in the item payload ---")
    for id_type, elements in sorted(id_type_census(item).items()):
        print(f"  {id_type:45} in {', '.join(sorted(elements))}")
    print()

    print("--- Top-level blocks present in _Data ---")
    for key, value in sorted(data.items()):
        if value in (None, [], {}):
            continue
        shape_note = f"{len(value)} item(s)" if isinstance(value, list) else type(value).__name__
        print(f"  {key:50} {shape_note}")
    print()

    # ── 5: reverse edge — do reports/dashboards mention time calcs? ─────────
    print("--- Reverse edge (does anything else reference time calculations?) ---")
    tc_id_types = {t for t in id_type_census(item) if "Time_Calculation" in t}
    if not tc_id_types:
        print("  (no Time_Calculation-typed IDs on the item itself; skipping)")
    else:
        print(f"  Time_Calculation ID types in play: {sorted(tc_id_types)}")
        print("  A follow-up run should pull one report and one dashboard payload")
        print("  and grep them for these ID types. If none appear, TCs are a leaf")
        print("  in the DAG and can migrate independently of reports/dashboards.")
    print()

    if args.dump_payload:
        path = Path(args.dump_payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(item, indent=2, default=str), encoding="utf-8")
        print(f"Full payload written to {path}")


if __name__ == "__main__":
    main()
