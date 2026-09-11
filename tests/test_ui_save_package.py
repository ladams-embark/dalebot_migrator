"""The wizard can now produce the packages it has always been able to load.

Connect has a picker that loads a stored package and skips the whole source
half of the wizard. Nothing in the product could make one: ``save_package``
and ``package_from_closure`` sat in ``api.py`` with no caller anywhere
outside their own tests, so the only route to a package was to write a
script. Resolve is where the closure is finished, which makes it the place
to offer the capture.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.api import Closure, Node, NodeKind, load_package
from wdmigrator.config.targets import target_from_parts
from wdmigrator.migrate.resolver import node_id_for
from wdmigrator.ui.state import STATE_KEY, WizardState
from wdmigrator.ui.steps import resolve as resolve_step

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _StubConnection:
    pass


def _closure() -> Closure:
    closure = Closure()
    node = Node(
        node_id=node_id_for(NodeKind.REPORT, "W0"),
        kind=NodeKind.REPORT,
        source_wid="W0",
        reference_id=None,
        name="Headcount",
        payload={"Tenanted_Report_Definition_Data": {"Name": "Headcount"}},
        selected=True,
    )
    closure.nodes[node.node_id] = node
    return closure


def _plan_app(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(resolve_step, "default_packages_dir", lambda: tmp_path)
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
    state = WizardState(step="plan", closure=_closure(), **kwargs)
    state.source.connection = _StubConnection()
    state.dest.connection = _StubConnection()
    state.source.target = target_from_parts("impl-services1.wd12.myworkday.com", "src_tenant")
    at.session_state[STATE_KEY] = state
    at.run(timeout=20)
    return at


def _save(at, name: str, description: str = "") -> AppTest:
    at.text_input(key="pkg_save_name").set_value(name)
    at.text_area(key="pkg_save_description").set_value(description)
    return at.button(key="pkg_save").click().run(timeout=20)


def _text(at) -> str:
    return " ".join(str(w.value) for w in at.markdown)


class TestSaving:
    def test_a_resolved_closure_can_be_captured(self, tmp_path, monkeypatch):
        at = _plan_app(tmp_path, monkeypatch)
        assert not at.exception
        at = _save(at, "Admin Reports", "The headcount bundle.")
        assert not at.exception

        written = list(tmp_path.glob("*.json"))
        assert len(written) == 1
        package = load_package(written[0])
        assert package.name == "Admin Reports"
        assert package.description == "The headcount bundle."
        assert package.node_count == 1

    def test_the_captured_package_names_the_source_it_came_from(self, tmp_path, monkeypatch):
        """A package is replayed against arbitrary destinations later, and
        ``build_guard`` uses the captured tenant as the run's source identity —
        it is how a package-loaded run still gets caught by the same-tenant
        check in ``safety.py``."""
        at = _save(_plan_app(tmp_path, monkeypatch), "src-capture")
        package = load_package(next(iter(tmp_path.glob("*.json"))))
        assert package.source_tenant == "src_tenant"
        assert package.source_services_host == "impl-services1.wd12.myworkday.com"

    def test_the_payloads_survive_the_round_trip(self, tmp_path, monkeypatch):
        """A package with node ids but no payloads would load cleanly and
        then fail at write time, which is the worst place to find out."""
        at = _save(_plan_app(tmp_path, monkeypatch), "payloads")
        package = load_package(next(iter(tmp_path.glob("*.json"))))
        node = next(iter(package.closure.nodes.values()))
        assert node.payload["Tenanted_Report_Definition_Data"]["Name"] == "Headcount"

    def test_the_filename_is_derived_safely_from_the_name(self):
        assert resolve_step._package_filename("Admin Reports") == "admin-reports.json"
        assert resolve_step._package_filename("  a/b\\c  ") == "a-b-c.json"
        assert resolve_step._package_filename("R&D — 2026") == "r-d-2026.json"


class TestRefusals:
    def test_an_unnamed_package_is_refused(self, tmp_path, monkeypatch):
        at = _save(_plan_app(tmp_path, monkeypatch), "   ")
        assert not at.exception
        assert "A package needs a name" in _text(at)
        assert list(tmp_path.glob("*.json")) == []

    def test_an_existing_package_is_never_overwritten(self, tmp_path, monkeypatch):
        """Packages carry no version, so replacing one would swap what a
        colleague's picker entry means with nothing to show it changed."""
        at = _save(_plan_app(tmp_path, monkeypatch), "dupe")
        assert not at.exception
        before = (tmp_path / "dupe.json").read_text(encoding="utf-8")

        at = _save(_plan_app(tmp_path, monkeypatch), "dupe")
        assert "already exists" in _text(at)
        assert (tmp_path / "dupe.json").read_text(encoding="utf-8") == before

    def test_a_loaded_package_offers_no_re_save(self, tmp_path, monkeypatch):
        """Nothing to capture — the closure came out of a file already."""
        from wdmigrator.api import package_from_closure

        at = _plan_app(
            tmp_path,
            monkeypatch,
            package=package_from_closure(
                _closure(), name="loaded", description="", source_tenant="src_tenant"
            ),
        )
        assert not at.exception
        assert not [b for b in at.button if b.key == "pkg_save"]
