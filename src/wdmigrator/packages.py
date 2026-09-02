"""Stored packages: pre-resolved source closures serialized to disk.

A package is one file that captures everything the migrator needs from the
source tenant to write a fixed set of objects to any destination: the full
payloads, the dependency closure, and the topological order. The destination
side stays live — cross-tenant matching, existence probing, and writing all
still happen at run time — but the *source* half of the workflow (Connect,
Select, Resolve) is skipped, because the package already holds its output.

Why a package is not a wid_map or a partial dump:
  * The migrator's writer needs the full source payloads to build the SOAP
    envelope, remap references, and apply the tenant-scoped stripping rules.
    A wid_map is the artefact of a WRITE, not the input to one.
  * A closure is not just a list of node ids — the topological order, the
    depends_on edges, and the ``selected`` vs. ``required_by`` distinctions
    all matter for the wizard's presentation. Serialising the whole Closure
    lets the loading side reconstruct the same object graph.

File shape (JSON, forward-compatible via ``$schema_version``)::

    {
      "$schema_version": 1,
      "name": "sageai-admin-reports",
      "description": "...",
      "source_tenant": "sageai",
      "source_services_host": "impl-services1.wd503.myworkday.com",
      "captured_at": "2026-09-02T...",
      "wdmigrator_version": "0.1.0",
      "nodes": [
        {"node_id": "...", "kind": "report", "source_wid": "...",
         "reference_id": "...", "name": "...", "payload": {...},
         "depends_on": [...], "class_name": null,
         "selected": true, "required_by": [...]}
      ],
      "unresolved": {
        "reference_ids": [], "measure_ids": [], "report_ids": [],
        "prompt_set_ids": [], "prompt_field_ids": [],
        "gauge_range_ids": [], "dashboard_ids": [],
        "time_calculation_tag_ids": [], "time_calculation_group_ids": []
      },
      "passthrough_wids": [],
      "unmigratable_indicator_wids": []
    }

Packages are treated as **untrusted-but-versioned** input: an unknown
``$schema_version`` raises rather than silently loading a partial closure.
Every set-typed field becomes a sorted list on save so diffs stay clean, and
lists come back as sets on load so downstream code sees the same shape it
expects from a fresh :func:`resolve_closure` call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from wdmigrator.migrate.resolver import Closure, Node, NodeKind

SCHEMA_VERSION = 1


class PackageError(RuntimeError):
    """A stored package could not be read or its shape was unexpected."""


@dataclass(frozen=True)
class PackageMetadata:
    """The header of a package — enough to list one without loading its payloads."""

    name: str
    description: str
    source_tenant: str
    captured_at: str
    node_count: int
    path: Path


@dataclass
class Package:
    """A stored package: metadata plus the full pre-resolved closure."""

    name: str
    description: str
    source_tenant: str
    source_services_host: str
    captured_at: str
    closure: Closure
    wdmigrator_version: str = ""

    def counts_by_kind(self) -> dict[str, int]:
        return self.closure.counts_by_kind()

    @property
    def node_count(self) -> int:
        return len(self.closure.nodes)


def _node_to_dict(node: Node) -> dict:
    return {
        "node_id": node.node_id,
        "kind": node.kind.value,
        "source_wid": node.source_wid,
        "reference_id": node.reference_id,
        "name": node.name,
        "payload": node.payload,
        "depends_on": sorted(node.depends_on),
        "class_name": node.class_name,
        "selected": node.selected,
        "required_by": sorted(node.required_by),
    }


def _node_from_dict(data: dict) -> Node:
    return Node(
        node_id=data["node_id"],
        kind=NodeKind(data["kind"]),
        source_wid=data["source_wid"],
        reference_id=data.get("reference_id"),
        name=data.get("name"),
        payload=data["payload"],
        depends_on=frozenset(data.get("depends_on") or ()),
        class_name=data.get("class_name"),
        selected=bool(data.get("selected", False)),
        required_by=frozenset(data.get("required_by") or ()),
    )


def save_package(
    package: Package, path: str | Path, *, indent: int | None = 2
) -> None:
    """Write ``package`` to ``path`` as JSON.

    Set-typed fields are sorted on the way out so two packages built at
    different times over the same closure produce byte-identical files —
    ``sorted(frozenset)`` beats ``list(frozenset)`` for reviewability.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "$schema_version": SCHEMA_VERSION,
        "name": package.name,
        "description": package.description,
        "source_tenant": package.source_tenant,
        "source_services_host": package.source_services_host,
        "captured_at": package.captured_at,
        "wdmigrator_version": package.wdmigrator_version,
        "nodes": [_node_to_dict(n) for n in package.closure.nodes.values()],
        "unresolved": {
            "reference_ids": sorted(package.closure.unresolved_reference_ids),
            "measure_ids": sorted(package.closure.unresolved_measure_ids),
            "report_ids": sorted(package.closure.unresolved_report_ids),
            "prompt_set_ids": sorted(package.closure.unresolved_prompt_set_ids),
            "prompt_field_ids": sorted(package.closure.unresolved_prompt_field_ids),
            "gauge_range_ids": sorted(package.closure.unresolved_gauge_range_ids),
            "dashboard_ids": sorted(package.closure.unresolved_dashboard_ids),
            "time_calculation_tag_ids": sorted(
                package.closure.unresolved_time_calculation_tag_ids
            ),
            "time_calculation_group_ids": sorted(
                package.closure.unresolved_time_calculation_group_ids
            ),
        },
        "passthrough_wids": sorted(package.closure.passthrough_wids),
        "unmigratable_indicator_wids": sorted(
            package.closure.unmigratable_indicator_wids
        ),
    }
    tmp = p.with_suffix(p.suffix + f".{id(package)}.tmp")
    tmp.write_text(json.dumps(obj, indent=indent, default=str), encoding="utf-8")
    tmp.replace(p)


def load_package(path: str | Path) -> Package:
    """Read a package back from disk. Raises :class:`PackageError` on any
    unrecognised schema version or missing field."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackageError(f"Package file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise PackageError(f"Package {p} is not valid JSON: {exc}") from exc

    version = data.get("$schema_version")
    if version != SCHEMA_VERSION:
        raise PackageError(
            f"Package {p} has unsupported $schema_version={version!r}; "
            f"this build supports {SCHEMA_VERSION}."
        )
    for required in ("name", "source_tenant", "nodes"):
        if required not in data:
            raise PackageError(f"Package {p} missing required field {required!r}.")

    unresolved = data.get("unresolved") or {}
    closure = Closure(
        nodes={n["node_id"]: _node_from_dict(n) for n in data["nodes"]},
        passthrough_wids=set(data.get("passthrough_wids") or ()),
        unresolved_reference_ids=set(unresolved.get("reference_ids") or ()),
        unresolved_measure_ids=set(unresolved.get("measure_ids") or ()),
        unresolved_report_ids=set(unresolved.get("report_ids") or ()),
        unresolved_prompt_set_ids=set(unresolved.get("prompt_set_ids") or ()),
        unresolved_prompt_field_ids=set(unresolved.get("prompt_field_ids") or ()),
        unresolved_gauge_range_ids=set(unresolved.get("gauge_range_ids") or ()),
        unresolved_dashboard_ids=set(unresolved.get("dashboard_ids") or ()),
        unresolved_time_calculation_tag_ids=set(
            unresolved.get("time_calculation_tag_ids") or ()
        ),
        unresolved_time_calculation_group_ids=set(
            unresolved.get("time_calculation_group_ids") or ()
        ),
        unmigratable_indicator_wids=set(
            data.get("unmigratable_indicator_wids") or ()
        ),
    )
    return Package(
        name=data["name"],
        description=data.get("description") or "",
        source_tenant=data["source_tenant"],
        source_services_host=data.get("source_services_host") or "",
        captured_at=data.get("captured_at") or "",
        wdmigrator_version=data.get("wdmigrator_version") or "",
        closure=closure,
    )


def package_from_closure(
    closure: Closure,
    *,
    name: str,
    description: str,
    source_tenant: str,
    source_services_host: str = "",
    wdmigrator_version: str = "",
    captured_at: str | None = None,
) -> Package:
    """Wrap a live-resolved closure as a package ready to save.

    ``captured_at`` defaults to the current UTC time in RFC 3339 form so a
    saved package always carries a real timestamp; pass an explicit value only
    when reproducing an old capture (rare — usually you want ``now``).
    """
    if captured_at is None:
        captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return Package(
        name=name,
        description=description,
        source_tenant=source_tenant,
        source_services_host=source_services_host,
        captured_at=captured_at,
        closure=closure,
        wdmigrator_version=wdmigrator_version,
    )


def list_packages(directory: str | Path) -> list[PackageMetadata]:
    """Enumerate the header of every ``*.json`` file under ``directory``.

    Skips files that are not packages (unknown schema, missing fields) rather
    than raising — a browsing UI should not blow up on one bad file. A caller
    that needs strictness can still load each candidate directly.
    """
    p = Path(directory)
    if not p.is_dir():
        return []
    out: list[PackageMetadata] = []
    for candidate in sorted(p.glob("*.json")):
        try:
            pkg = load_package(candidate)
        except PackageError:
            continue
        out.append(
            PackageMetadata(
                name=pkg.name,
                description=pkg.description,
                source_tenant=pkg.source_tenant,
                captured_at=pkg.captured_at,
                node_count=pkg.node_count,
                path=candidate,
            )
        )
    return out


#: Default location for packaged migrations shipped with the tool. Callers can
#: override to point at a user-writable directory when adding new packages
#: without editing the checkout.
def default_packages_dir() -> Path:
    """The ``packages/`` directory at the top of the repo."""
    return Path(__file__).resolve().parents[2] / "packages"


__all__ = [
    "Package",
    "PackageError",
    "PackageMetadata",
    "SCHEMA_VERSION",
    "default_packages_dir",
    "list_packages",
    "load_package",
    "package_from_closure",
    "save_package",
]
