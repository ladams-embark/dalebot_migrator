"""Enforces the other half of the `api.py` boundary: `tests/test_api.py`
checks that the engine never imports streamlit/pandas, but nothing checked
that `ui/` only reaches into `wdmigrator.api` rather than `auth`,
`discovery`, `migrate`, `config`, `safety`, or `secrets` directly —
HANDOFF.md flagged this as a gap to close once `ui/` existed.

Pure AST source scanning, deliberately not importing `wdmigrator.ui` itself
— that would require streamlit/pandas to be installed just to run this
check, and the whole point is to verify the import *statements*, not runtime
behavior.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

UI_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "wdmigrator" / "ui"

# A ui/ module may import wdmigrator.api directly, or any other wdmigrator.ui
# submodule (that's normal intra-package structure) — nothing else under
# wdmigrator/.
ALLOWED_WDMIGRATOR_PREFIXES = ("wdmigrator.api", "wdmigrator.ui")


def _ui_module_files() -> list[pathlib.Path]:
    return sorted(UI_ROOT.rglob("*.py"))


def _wdmigrator_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "wdmigrator" or alias.name.startswith("wdmigrator."):
                    names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "wdmigrator" or node.module.startswith("wdmigrator."):
                names.add(node.module)
    return names


@pytest.mark.parametrize(
    "path", _ui_module_files(), ids=lambda p: str(p.relative_to(UI_ROOT)).replace("\\", "/")
)
def test_ui_module_only_imports_api_or_other_ui_modules(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for module_name in _wdmigrator_imports(tree):
        assert module_name == "wdmigrator" or module_name.startswith(ALLOWED_WDMIGRATOR_PREFIXES), (
            f"{path.relative_to(UI_ROOT)}: imports {module_name!r} directly, "
            "bypassing the wdmigrator.api boundary"
        )


def test_ui_package_actually_has_files_to_check():
    # Guards against this test silently checking nothing if the ui package
    # ever moves or the glob stops matching real files.
    assert len(_ui_module_files()) >= 10


@pytest.mark.parametrize(
    "path", _ui_module_files(), ids=lambda p: str(p.relative_to(UI_ROOT)).replace("\\", "/")
)
def test_no_deprecated_use_container_width(path: pathlib.Path):
    """Streamlit deprecated ``use_container_width`` and says it will remove it.

    ``width="stretch"`` is the replacement on buttons; on dataframes and data
    editors ``width`` already defaults to ``"stretch"``, so the argument just
    goes. Twenty-eight call sites is enough that finding out by way of a
    TypeError on a version bump, in an app whose next step writes to a tenant,
    is not how anyone should learn about it.
    """
    assert "use_container_width" not in path.read_text(encoding="utf-8"), (
        f"{path.relative_to(UI_ROOT)}: use_container_width is deprecated — "
        'use width="stretch" on buttons, or drop it on dataframes/editors'
    )


#: Artifact directories that must never be written to unscoped, because the
#: app is hosted and several consultants share one filesystem. Each holds
#: something private to one person: their saved selections, the destination
#: identifiers they looked up, their error reports.
_SCOPED_DIRS = ("SESSION_DIR", "MAP_DIR", "ERROR_DIR")


@pytest.mark.parametrize(
    "path", _ui_module_files(), ids=lambda p: str(p.relative_to(UI_ROOT)).replace("\\", "/")
)
def test_private_artifact_dirs_are_scoped_per_workspace(path: pathlib.Path):
    """Naming one of these constants is fine; passing it somewhere without
    ``workspace.user_dir`` around it is not.

    Unscoped, the resume picker listed every consultant's saved sessions —
    each labelled with the tenants and usernames it came from — and pruning
    counted files across all of them, so one person's saves deleted another's.
    The failure is silent and only shows up when two people are using the app
    at the same time, which is the case nobody tests by hand.
    """
    source = path.read_text(encoding="utf-8")
    for constant in _SCOPED_DIRS:
        # The module that defines a constant also names it as its own default,
        # which is correct — those modules take an explicit ``directory`` and
        # know nothing about browser sessions. Only callers are checked.
        if f"\n{constant} = " in source:
            continue
        for line in source.splitlines():
            if constant not in line or line.lstrip().startswith("#"):
                continue
            assert "user_dir" in line, (
                f"{path.relative_to(UI_ROOT)}: passes {constant} without "
                "workspace.user_dir() — a hosted app serves several "
                f"consultants from one filesystem.\n    {line.strip()}"
            )
