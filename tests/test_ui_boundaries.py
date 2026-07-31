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
