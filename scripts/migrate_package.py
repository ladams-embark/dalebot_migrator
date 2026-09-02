"""
migrate_package.py
------------------
Migrate a **stored package** (a pre-resolved source closure serialized to
disk) to a live destination tenant. No source tenant connection is used or
needed — the package already carries every payload the writer will send.

The destination side still runs live: cross-tenant matching against the
destination's calculated-field and calculated-measure indexes, existence
probing, and the ordered PUT loop. Pre-flight blanking of always-tenant-data
references (Instance_Reference) is applied automatically as the safe
default; REPLACE-required references (Top_Level_Node_Reference and friends)
must be answered on the command line with ``--replace``.

Run it:
  # List available packages
  python scripts/migrate_package.py --list

  # Dry-run: shows the plan, no writes
  python scripts/migrate_package.py commit-pit-crew-reports

  # Live run
  python scripts/migrate_package.py commit-pit-crew-reports --live

  # Provide a required replacement (repeatable)
  python scripts/migrate_package.py commit-pit-crew-reports --live \\
      --replace WID:Organization_Reference_ID:Region_Hierarchy

The destination is taken from ``.env`` (``WD_DEST_*``). No source .env vars
are read.
"""
import os
import sys
import argparse
from pathlib import Path

from dotenv import load_dotenv

from wdmigrator import api

load_dotenv()


def _connect_dest() -> api.Connection:
    return api.connect(
        api.target_from_parts(
            os.environ["WD_DEST_SERVICES_HOST"],
            os.environ["WD_DEST_TENANT"],
        ),
        os.environ["WD_DEST_ISU_USERNAME"],
        os.environ["WD_DEST_ISU_PASSWORD"],
        role=api.Role.DESTINATION,
    )


def _resolve_package_path(name_or_path: str) -> Path:
    """Accept either a bare name (looked up in the default packages dir) or a
    filesystem path. Names get the .json suffix filled in for convenience."""
    candidate = Path(name_or_path)
    if candidate.is_file():
        return candidate
    fallback = api.default_packages_dir() / (
        name_or_path if name_or_path.endswith(".json") else f"{name_or_path}.json"
    )
    if not fallback.is_file():
        raise SystemExit(
            f"No package matches {name_or_path!r}. Looked in {candidate} "
            f"and {fallback}. Run --list to see what is available."
        )
    return fallback


def _list_packages() -> None:
    metas = api.list_packages(api.default_packages_dir())
    if not metas:
        print(f"No packages found in {api.default_packages_dir()}.")
        return
    print(f"Packages in {api.default_packages_dir()}:")
    for m in metas:
        print(f"  {m.name}  ({m.node_count} nodes, source={m.source_tenant}, {m.captured_at})")
        if m.description:
            print(f"    {m.description[:200]}")


def _apply_replacements(
    replacements: list[str],
) -> dict[str, api.ReferenceDecision]:
    """Parse ``--replace WID:TYPE:VALUE`` entries into ReferenceDecisions."""
    out: dict[str, api.ReferenceDecision] = {}
    for spec in replacements:
        wid, id_type, value = spec.split(":", 2)
        out[wid] = api.ReferenceDecision(
            source_wid=wid,
            action=api.ReferenceAction.REPLACE,
            replacement_type=id_type,
            replacement_value=value,
            note=f"CLI --replace: {id_type}={value}",
        )
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("package", nargs="?", help="Package name or file path")
    ap.add_argument("--list", action="store_true", help="List available packages and exit")
    ap.add_argument("--live", action="store_true", help="Write to destination (default is dry run)")
    ap.add_argument(
        "--owner", default=api.DEFAULT_REPORT_OWNER_USERNAME
        if hasattr(api, "DEFAULT_REPORT_OWNER_USERNAME") else "wd-support",
        help="Workday username to own every created report on the destination",
    )
    ap.add_argument(
        "--replace", action="append", default=[],
        help="Replace a source WID with a destination value. "
             "Format: SOURCE_WID:ID_TYPE:VALUE (repeatable)",
    )
    ap.add_argument(
        "--continue-on-failure", action="store_true",
        help="Do not stop at the first write failure — keep going and report all",
    )
    ap.add_argument(
        "--sharing",
        choices=["unshared", "all"],
        default="unshared",
        help="Report sharing on the destination. 'unshared' (default): "
             "Shared=False, only the owner sees each report. 'all': "
             "Shared=True with no restrictions — all authorized users. "
             "Specific-group sharing is not offered: those references are "
             "tenant-scoped and stripped either way.",
    )
    args = ap.parse_args(argv)

    if args.list:
        _list_packages()
        return
    if not args.package:
        ap.error("provide a package name/path, or --list")

    pkg_path = _resolve_package_path(args.package)
    package = api.load_package(pkg_path)
    print(f"Loaded package {package.name!r}")
    print(f"  source_tenant={package.source_tenant}  captured={package.captured_at}")
    print(f"  by kind: {package.counts_by_kind()}  (total {package.node_count})")

    dst = _connect_dest()
    status = api.verify_connection(dst)
    print(f"Destination verified: {status.ok} — {status.detail}")
    if not status.ok:
        raise SystemExit(1)

    # Destination indexes for cross-tenant matching
    print("Building destination indexes for cross-tenant matching...")
    dest_cf_index = None
    for prog in api.iter_calculated_field_index(dst):
        dest_cf_index = prog.index
    print(f"  destination CFs: {len(dest_cf_index)}")
    match_index = api.calculated_field_match_index(dest_cf_index)

    dest_measures = None
    for prog in api.iter_calculated_measure_index(dst):
        dest_measures = prog.index
    measure_match_index = api.calculated_measure_match_index(dest_measures)
    print(f"  destination measures: {len(dest_measures)}")

    # Probe the destination for existence
    print("Probing destination...")
    existence: dict = {}
    for prog in api.iter_check_existence(
        dst, package.closure,
        match_index=match_index,
        measure_match_index=measure_match_index,
    ):
        existence[prog.node.node_id] = prog.existence
    matched = [e for e in existence.values() if e.matched_by]
    print(f"  cross-tenant matched (shape, not ID): {len(matched)}")

    # Preflight defaults: BLANK for blank-safe, user-provided for replace-required
    stub_plan = api.build_plan(package.closure, existence)
    candidates = api.find_preflight_reference_candidates(stub_plan.ordered_nodes)
    print(f"Preflight found {len(candidates)} tenant-data reference(s):")
    replace_required = []
    for c in candidates:
        biz = {k: v for k, v in c.ids.items() if k != "WID"}
        marker = "" if c.default_action.value == "blank" else "  <-- REPLACE required"
        print(f"  {c.element}: {biz}  default={c.default_action.value}{marker}")
        if c.default_action.value == "replace":
            replace_required.append(c)

    decisions = _apply_replacements(args.replace)
    for c in candidates:
        if c.value in decisions:
            continue
        # BLANK and KEEP are both safe automatic defaults; REPLACE-required
        # rows stay undecided until the user supplies --replace, which is
        # already caught by the ``unmet`` check below.
        if c.default_action is api.ReferenceAction.BLANK:
            decisions[c.value] = api.ReferenceDecision(
                c.value, api.ReferenceAction.BLANK,
                note=f"Preflight default: always-tenant-data {c.element}",
            )
        elif c.default_action is api.ReferenceAction.KEEP:
            decisions[c.value] = api.ReferenceDecision(
                c.value, api.ReferenceAction.KEEP,
                note=f"Preflight default: delivered {c.element} — source WID passes through",
            )

    unmet = [
        c for c in replace_required if c.value not in decisions
    ]
    if unmet:
        print(f"\nBLOCKED: {len(unmet)} required replacement(s) not provided.")
        for c in unmet:
            biz = {k: v for k, v in c.ids.items() if k != "WID"}
            print(f"  --replace {c.value}:{list(biz)[0]}:<destination value>")
            print(f"    for {c.element} (source names: {biz})")
        raise SystemExit(1)

    plan = api.build_plan(
        package.closure, existence, reference_decisions=decisions
    )
    print(f"Plan: {plan.counts()}  hash={plan.plan_hash()}")

    blockers = api.validate_plan(plan)
    if blockers:
        print(f"BLOCKERS ({len(blockers)}) — refusing:")
        for b in blockers:
            print(f"  - {b.title}: {b.detail}")
        raise SystemExit(1)

    guard = api.WriteGuard(
        source=api.target_from_parts(
            package.source_services_host or "unknown", package.source_tenant
        ),
        dest=dst.target,
        dry_run=not args.live,
        plan_hash=plan.plan_hash(),
        confirmed_tenant_name=dst.target.tenant if args.live else "",
        dry_run_reviewed=args.live,
        source_verified=True,  # trust the package
        dest_verified=True,
        source_username=f"package:{package.name}",
        dest_username=dst.username,
    )

    sharing = (
        api.ReportSharing.SHARED_WITH_ALL_AUTHORIZED_USERS
        if args.sharing == "all"
        else api.ReportSharing.UNSHARED
    )
    print(f"\n--- {'LIVE' if args.live else 'DRY RUN'} --- sharing={sharing.value}")
    records = []
    for prog in api.iter_execute(
        dst, plan, guard,
        owner_reference=api.build_owner_reference(workday_username=args.owner),
        stop_on_failure=not args.continue_on_failure,
        report_sharing=sharing,
    ):
        r = prog.record
        records.append(r)
        marker = r.status.value.upper()
        line = f"  [{prog.position}/{prog.total}] {marker:14} {r.kind:18} {r.name!r}"
        if r.dest_wid:
            line += f" dest_wid={r.dest_wid}"
        print(line)
        if r.fault:
            print(f"      fault: {r.fault[:300]}")

    print(f"\nSummary: {api.summarise(records)}")


if __name__ == "__main__":
    main()
