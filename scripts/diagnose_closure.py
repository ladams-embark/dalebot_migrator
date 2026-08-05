"""
diagnose_closure.py
-------------------
Walk the full dependency closure for a report (or a calculated field) and show
*why* each reference resolved the way it did — specifically for nested,
multi-level calculated fields, which `classify_report_columns.py` cannot see
because it only inspects a report's own columns one level deep.

For every WID the closure walker treated as a pass-through (i.e. "not a
calculated field, nothing to migrate"), this re-checks it with a targeted
`Get_Calculated_Fields` against the SOURCE tenant. That single check is what
discriminates the three ways a multi-level dependency goes wrong:

  RESOLVES BY WID BUT IS NOT IN THE INDEX
      The index is stale or was swept incomplete. The field exists globally
      right now, so a rebuild fixes it. This is also the shape you get after
      promoting a report-scoped field to global — there is a real activation
      delay (minutes) during which a sweep still misses it.

  NOT FOUND BY WID EITHER
      Either a Workday-delivered field (passes through fine, not a problem) or
      a report-scoped calculated field that has never been promoted to global.
      The latter must be promoted in the Workday UI; nothing in this tool can
      create it. Both look identical here — see CLAUDE.md, there is nothing in
      the WSDL that tells them apart ahead of time.

  UNKNOWN
      The probe failed for some other reason (entitlement, transport). Do not
      interpret it as either of the above.

Read-only: it only ever calls Get_* against the source tenant.

Addressing comes from `.env` (WD_SOURCE_SERVICES_HOST / WD_SOURCE_TENANT) and
is echoed before any call. **The wizard does not use `.env`** — it uses
whatever was typed into its Connect form — so if you are explaining a failure
that happened there, check the echoed tenant matches, and override it with
--tenant / --services-host if it does not. Credentials always come from `.env`;
a password passed in argv lands in shell history.

Run it (from an activated venv, or via .venv\\Scripts\\python.exe):
  python scripts/diagnose_closure.py --report "PLNF - All Workers"
  python scripts/diagnose_closure.py --cf-wid <wid>
  python scripts/diagnose_closure.py --report "..." --rebuild-index
  python scripts/diagnose_closure.py --report "..." --tenant other_tenant_dpt5
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def _connect(tenant: str | None, services_host: str | None):
    """Connect to the SOURCE tenant.

    Addressing (host + tenant) can be overridden per-run, because this script
    is used to explain a failure that happened *in the wizard* — and the wizard
    takes whatever was typed into its Connect form, which need not be what
    `.env` says. Diagnosing the wrong tenant produces confident nonsense, so
    the target is echoed before any call is made.

    Credentials are never taken from the command line: a password in argv ends
    up in shell history and in the process list. Those still come from `.env`,
    which means an override only works if the same ISU can read both tenants.
    """
    overridden = bool(tenant or services_host)
    tenant = tenant or os.environ["WD_SOURCE_TENANT"]
    services_host = services_host or os.environ["WD_SOURCE_SERVICES_HOST"]
    username = os.environ["WD_SOURCE_ISU_USERNAME"]

    target = api.target_from_parts(services_host, tenant)

    print("SOURCE TENANT FOR THIS DIAGNOSIS")
    print(f"  tenant:  {target.tenant}")
    print(f"  host:    {target.services_host}")
    print(f"  user:    {username}")
    print(f"  addressing from: {'command-line flags' if overridden else '.env'}")
    print("  If that is not the tenant the failed migration read from, stop and")
    print("  pass --tenant/--services-host — otherwise this diagnoses the wrong")
    print("  tenant and the verdict will be wrong.\n")

    return api.connect(target, username, os.environ["WD_SOURCE_ISU_PASSWORD"],
                       role=api.Role.SOURCE)


def _get_index(conn, *, rebuild: bool):
    path = api.cache_path(conn, "calculated_field")
    if not rebuild:
        cached = api.load_index(path, tenant=conn.target.tenant)
        if cached is not None:
            age_min = cached.age_seconds() / 60
            print(f"Using cached calculated-field index: {len(cached):,} fields, "
                  f"built {age_min:.0f} min ago")
            if age_min > 30:
                print("  NOTE: this index is not fresh. A field promoted to global "
                      "since it was built will not be in it.")
            return cached

    print("Sweeping the calculated-field index (~25s)...")
    index = api.build_index(api.iter_calculated_field_index(conn))
    api.save_index(index, path)
    print(f"  {len(index):,} fields indexed.")
    return index


#: Reference elements that can legitimately hold a calculated field. Everything
#: else in a report or calculated-field payload points at some other object type
#: — a data source, a business object, a report column type — and *must* come
#: back NOT_FOUND from Get_Calculated_Fields. Probing those and reporting them
#: as suspicious is how the first version of this script cried wolf: it flagged
#: 12 perfectly normal references as possible missing dependencies.
#:
#: Note that even here, "not a calculated field" is not the same as "broken".
#: `External_Field_Reference` is a *superset* ID space (WID, Calculated_Field_ID,
#: Custom_Field_ID, Computed_Data_Source_Field_ID, and more — see CLAUDE.md), so
#: a delivered report field appears at exactly this path and passes through
#: fine. This narrows where to look; it does not by itself find a fault.
_CF_BEARING_REFERENCES = {"External_Field_Reference"}


def _reference_name(path: str) -> str:
    """The element name a WID was found at, ignoring list indices."""
    tail = path.split(".")[-1]
    return tail.split("[")[0]


def _walk_refs(obj, path="", out=None):
    """Every WID inside obj, paired with the JSON path it sits at."""
    out = out if out is not None else []
    if isinstance(obj, dict):
        entries = obj.get("ID")
        if isinstance(entries, list):
            for entry in entries:
                if (
                    isinstance(entry, dict)
                    and entry.get("type") == "WID"
                    and entry.get("_value_1")
                ):
                    out.append((entry["_value_1"], path))
        for key, value in obj.items():
            _walk_refs(value, f"{path}.{key}" if path else key, out)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _walk_refs(item, f"{path}[{i}]", out)
    return out


def _dependency_payload(node):
    if node.kind is api.NodeKind.REPORT:
        return node.payload.get("Tenanted_Report_Definition_Data") or {}
    return node.payload.get("Calculated_Field_Data") or {}


def _print_tree(closure, node, depth=0, seen=None):
    """Print the closure as a dependency tree, child-most nesting visible."""
    seen = seen if seen is not None else set()
    indent = "    " * depth
    marker = "*" if node.selected else "-"
    print(f"{indent}{marker} [{node.kind.value}] {node.name!r}")

    if node.node_id in seen:
        print(f"{indent}      (already shown above)")
        return
    seen.add(node.node_id)

    for dep_id in sorted(node.depends_on):
        dep = closure.nodes.get(dep_id)
        if dep is not None:
            _print_tree(closure, dep, depth + 1, seen)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--report", help="Exact report name.")
    group.add_argument("--cf-wid", help="Calculated field WID.")
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="Force a fresh sweep instead of using the cache.",
    )
    parser.add_argument(
        "--tenant",
        help="Source tenant ID, if it differs from WD_SOURCE_TENANT in .env — "
             "e.g. when the wizard read from a tenant .env does not point at.",
    )
    parser.add_argument(
        "--services-host",
        help="Source services host, if it differs from WD_SOURCE_SERVICES_HOST.",
    )
    args = parser.parse_args()

    conn = _connect(args.tenant, args.services_host)
    index = _get_index(conn, rebuild=args.rebuild_index)

    selected_reports: dict[str, dict] = {}
    selected_field_wids: list[str] = []

    if args.report:
        found = api.lookup_report_by_name(conn, args.report)
        if found.outcome is not api.LookupOutcome.FOUND:
            raise SystemExit(
                f"Could not resolve report {args.report!r}: {found.outcome.value} "
                f"{found.fault or ''}"
            )
        full = api.lookup_report(conn, wid=found.wid)
        if full.outcome is not api.LookupOutcome.FOUND or full.data is None:
            raise SystemExit(f"Could not fetch report definition: {full.fault}")
        selected_reports[full.wid] = full.data
    else:
        selected_field_wids.append(args.cf_wid)

    closure = api.resolve(
        index,
        selected_field_wids=selected_field_wids,
        selected_reports=selected_reports,
    )

    print(f"\n{'=' * 70}\nCLOSURE: {len(closure)} object(s)")
    print(f"  {len(closure.selected_nodes)} selected, "
          f"{len(closure.pulled_in_nodes)} pulled in as dependencies")
    print(f"  {len(closure.passthrough_wids)} reference(s) treated as pass-through\n")

    print("DEPENDENCY TREE (child-most nested underneath)")
    print("-" * 70)
    for node in closure.selected_nodes:
        _print_tree(closure, node)

    depth_note = max(
        (len(n.depends_on) for n in closure.nodes.values()), default=0
    )
    print(f"\nDeepest single node fans out to {depth_note} direct dependency(ies).")

    # Where does each pass-through WID actually sit? A WID at
    # Data_Source_Reference is *supposed* to miss Get_Calculated_Fields.
    paths: dict[str, list[str]] = {}
    for node in closure.nodes.values():
        for wid, path in _walk_refs(_dependency_payload(node)):
            if wid in closure.passthrough_wids:
                paths.setdefault(wid, []).append(f"{node.name}: {path}")

    candidates = sorted(
        wid for wid, where in paths.items()
        if any(_reference_name(p.split(": ", 1)[-1]) in _CF_BEARING_REFERENCES
               for p in where)
    )
    expected = sorted(set(paths) - set(candidates))

    print(f"\n{'=' * 70}")
    print(f"PASS-THROUGH REFERENCES ({len(closure.passthrough_wids)})")
    print("-" * 70)

    by_kind: dict[str, int] = {}
    for wid in expected:
        for where in paths[wid]:
            by_kind[_reference_name(where.split(": ", 1)[-1])] = (
                by_kind.get(_reference_name(where.split(": ", 1)[-1]), 0) + 1
            )
    print(f"  {len(expected)} point at object types that are NOT calculated fields.")
    print("  These are expected and are not probed:")
    for name, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"    {n:3d}  {name}")

    print(f"\n  {len(candidates)} sit at a reference that COULD hold a calculated")
    print("  field. Probing only these:")
    print("-" * 70)

    stale, absent, unknown = [], [], []
    for wid in candidates:
        result = api.lookup_calculated_field(conn, wid=wid)
        where = paths[wid][0]
        if result.outcome is api.LookupOutcome.FOUND:
            data = (result.data or {}).get("Calculated_Field_Data") or {}
            stale.append((wid, data.get("Name")))
            print(f"  [INDEX GAP] {wid}")
            print(f"      resolves live as {data.get('Name')!r} but is NOT in the index")
            print(f"      at {where}")
        elif result.outcome is api.LookupOutcome.NOT_FOUND:
            absent.append(wid)
            print(f"  [NOT A CF ] {wid}")
            print(f"      at {where}")
        else:
            unknown.append((wid, result.fault))
            print(f"  [UNKNOWN  ] {wid}: {result.fault}")

    print(f"\n{'=' * 70}\nVERDICT")
    print("-" * 70)
    if stale:
        print(f"{len(stale)} field(s) exist globally RIGHT NOW but were missing from")
        print("the index used to resolve — a real missed dependency. Rebuild the")
        print("index and re-run the migration:")
        for wid, name in stale:
            print(f"    {name!r}  ({wid})")
        print("\n  In the wizard: Select -> 'Rebuild calculated field index'.")
    elif unknown:
        print("Some probes failed for reasons other than 'not found'. Resolve those")
        print("first — do not read them as absent.")
    elif absent:
        print(f"{len(absent)} reference(s) sit at External_Field_Reference but are not")
        print("calculated fields. That is NOT automatically a fault: this element is a")
        print("superset ID space, and a Workday-DELIVERED report field lives here too")
        print("and migrates fine. It is only a problem if the live PUT failed with")
        print("  'Invalid ID value ... is not a valid ID value for type = WID'")
        print("naming one of these WIDs — in which case it is most likely a")
        print("report-scoped calculated field that was never promoted to global.")
        print("\n  Check the fault text on the failing object in Results before")
        print("  acting on this. Do not promote fields speculatively.")
    else:
        print("Every calculated-field-bearing reference resolved into the closure.")
        print("Dependency resolution is not the problem here — get the fault text")
        print("from the failing object in the wizard's Results step.")


if __name__ == "__main__":
    try:
        main()
    except KeyError as exc:
        raise SystemExit(f"Missing environment variable: {exc}") from exc
