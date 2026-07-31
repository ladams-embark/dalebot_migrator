"""Tests for the UI-facing facade.

`api.py` is meant to be the *only* module a UI (or a future CLI) imports. That
claim is worth almost nothing unless it is checked, so most of this file is
about proving three things: every symbol the UI needs is actually reachable
from here, the re-exports are the real objects rather than typo'd stand-ins,
and the two functions that aren't straight re-exports behave correctly.
"""

import inspect

import pytest

from wdmigrator import api
from wdmigrator.auth import client as auth_client
from wdmigrator.config import targets as config_targets
from wdmigrator.discovery import inventory as discovery_inventory
from wdmigrator.migrate import ordering as migrate_ordering
from wdmigrator.migrate import planner as migrate_planner
from wdmigrator.migrate import resolver as migrate_resolver
from wdmigrator.migrate import writer as migrate_writer
from wdmigrator import safety
from wdmigrator import secrets as wd_secrets

SOURCE = api.target_from_parts("impl-services1.wd12.myworkday.com", "source_tenant")
DEST = api.target_from_parts("impl-services1.wd12.myworkday.com", "dest_tenant")


class TestReExportsAreTheRealThing:
    """Guards against a typo'd re-export silently shadowing the real symbol."""

    @pytest.mark.parametrize(
        "name, original",
        [
            ("TenantTarget", config_targets.TenantTarget),
            ("Environment", config_targets.Environment),
            ("parse_tenant_url", config_targets.parse_tenant_url),
            ("target_from_parts", config_targets.target_from_parts),
            ("Connection", auth_client.Connection),
            ("Credentials", auth_client.Credentials),
            ("Role", auth_client.Role),
            ("make_client", auth_client.make_client),
            ("verify_connection", auth_client.verify_connection),
            ("Index", discovery_inventory.Index),
            ("iter_calculated_field_index", discovery_inventory.iter_calculated_field_index),
            ("iter_report_index", discovery_inventory.iter_report_index),
            ("build_index", discovery_inventory.build_index),
            ("save_index", discovery_inventory.save_index),
            ("load_index", discovery_inventory.load_index),
            ("cache_path", discovery_inventory.cache_path),
            ("lookup_calculated_field", discovery_inventory.lookup_calculated_field),
            ("lookup_report_by_name", discovery_inventory.lookup_report_by_name),
            ("resolve_closure", migrate_resolver.resolve_closure),
            ("Closure", migrate_resolver.Closure),
            ("Node", migrate_resolver.Node),
            ("NodeKind", migrate_resolver.NodeKind),
            ("topological_sort", migrate_ordering.topological_sort),
            ("substitute_wids", migrate_ordering.substitute_wids),
            ("build_plan", migrate_planner.build_plan),
            ("validate_plan", migrate_planner.validate_plan),
            ("iter_check_existence", migrate_planner.iter_check_existence),
            ("Action", migrate_planner.Action),
            ("iter_execute", migrate_writer.iter_execute),
            ("serialize_envelope", migrate_writer.serialize_envelope),
            ("build_owner_reference", migrate_writer.build_owner_reference),
            ("WriteStatus", migrate_writer.WriteStatus),
            ("WriteGuard", safety.WriteGuard),
            ("evaluate_guards", safety.evaluate_guards),
            ("assert_write_allowed", safety.assert_write_allowed),
            ("GuardViolation", safety.GuardViolation),
            ("Secret", wd_secrets.Secret),
            ("redact", wd_secrets.redact),
            ("redact_envelope", wd_secrets.redact_envelope),
        ],
    )
    def test_reexport_is_identical_object(self, name, original):
        assert getattr(api, name) is original


class TestEverythingInAllIsReachable:
    def test_every_declared_export_actually_exists(self):
        missing = [name for name in api.__all__ if not hasattr(api, name)]
        assert missing == []

    def test_all_has_no_duplicates(self):
        assert len(api.__all__) == len(set(api.__all__))

    def test_module_docstring_promises_match_what_is_exported(self):
        """The plan's contract: parse/connect/index/resolve/check/serialize/execute."""
        promised = {
            "parse_tenant_url",
            "connect",
            "iter_calculated_field_index",
            "iter_report_index",
            "resolve",
            "iter_check_existence",
            "serialize_envelope",
            "iter_execute",
        }
        assert promised <= set(api.__all__)


class TestNoUIDependencies:
    """`api.py` and every module it wraps must stay importable without
    streamlit or pandas — that is what keeps the engine testable and reusable
    from a future CLI, not just a Streamlit app."""

    ENGINE_MODULES = [
        "wdmigrator",
        "wdmigrator.api",
        "wdmigrator.config",
        "wdmigrator.config.targets",
        "wdmigrator.safety",
        "wdmigrator.secrets",
        "wdmigrator.ratelimit",
        "wdmigrator.auth",
        "wdmigrator.auth.client",
        "wdmigrator.discovery",
        "wdmigrator.discovery.inventory",
        "wdmigrator.migrate",
        "wdmigrator.migrate.ordering",
        "wdmigrator.migrate.resolver",
        "wdmigrator.migrate.planner",
        "wdmigrator.migrate.writer",
        "wdmigrator.validation",
    ]

    @pytest.mark.parametrize("module_name", ENGINE_MODULES)
    def test_module_source_does_not_import_streamlit_or_pandas(self, module_name):
        """Parses actual import statements via ast, not a source substring —
        this module's own docstring says 'must not import streamlit or
        pandas', and a naive substring check would flag that sentence."""
        import ast
        import importlib

        module = importlib.import_module(module_name)
        source_file = inspect.getsourcefile(module)
        if source_file is None:
            pytest.skip(f"{module_name} has no source file (namespace package)")

        tree = ast.parse(open(source_file, encoding="utf-8").read())
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

        forbidden = imported_roots & {"streamlit", "pandas"}
        assert not forbidden, f"{module_name} imports {forbidden}"

    def test_importing_the_facade_never_pulls_in_streamlit(self):
        import sys

        # api was already imported by this test module; if streamlit had been
        # pulled in transitively, it would already be in sys.modules.
        assert "streamlit" not in sys.modules
        assert "pandas" not in sys.modules

    def test_engine_modules_import_successfully_even_if_streamlit_is_unavailable(self):
        """Simulates a streamlit-less environment (e.g. a bare CLI install)."""
        import builtins
        import importlib
        import sys

        real_import = builtins.__import__

        def blocking_import(name, *args, **kwargs):
            if name == "streamlit" or name.startswith("streamlit."):
                raise ImportError("streamlit is not installed in this environment")
            return real_import(name, *args, **kwargs)

        removed = {
            name: mod
            for name, mod in list(sys.modules.items())
            if name in self.ENGINE_MODULES
        }
        for name in removed:
            del sys.modules[name]

        builtins.__import__ = blocking_import
        try:
            for name in self.ENGINE_MODULES:
                importlib.import_module(name)
        finally:
            builtins.__import__ = real_import
            for name, mod in removed.items():
                sys.modules[name] = mod


class TestConnect:
    """`connect()` is the one place the facade hides Secret-wrapping."""

    def test_connect_builds_a_working_connection(self, wsdl_path):
        conn = api.connect(
            DEST, "lmcneil", "not-a-real-password",
            role=api.Role.DESTINATION, wsdl_source=wsdl_path,
        )
        assert conn.is_destination()
        assert conn.username == "lmcneil@dest_tenant"

    def test_connect_pins_the_endpoint_to_the_target_not_the_wsdl(self, wsdl_path):
        """Regression guard: a destination client must never inherit the
        bundled WSDL's embedded (source-tenant) address."""
        conn = api.connect(
            DEST, "lmcneil", "pw", role=api.Role.DESTINATION, wsdl_source=wsdl_path
        )
        assert "dest_tenant" in conn.endpoint
        assert "commitconsulting_dpt1" not in conn.endpoint

    def test_connect_defaults_to_source_role(self, wsdl_path):
        conn = api.connect(SOURCE, "u", "pw", wsdl_source=wsdl_path)
        assert not conn.is_destination()

    def test_connect_never_leaks_the_password_in_repr(self, wsdl_path):
        conn = api.connect(
            SOURCE, "u", "super-secret-value", wsdl_source=wsdl_path
        )
        assert "super-secret-value" not in repr(conn)

    def test_connect_does_not_require_importing_secret_directly(self, wsdl_path):
        """The whole point: a UI passes plain strings, never touches Secret."""
        sig = inspect.signature(api.connect)
        assert set(sig.parameters) & {"username", "password"} == {
            "username",
            "password",
        }
        for name in ("username", "password"):
            assert sig.parameters[name].annotation in (str, "str")


class TestResolveIsNotAGenerator:
    def test_resolve_returns_a_closure_directly(self):
        import time

        index = discovery_inventory.Index(
            kind="calculated_field", tenant="t", fetched_at=time.time()
        )
        result = api.resolve(index, expected_index_size=0)
        assert isinstance(result, api.Closure)

    def test_resolve_is_not_a_generator_function(self):
        assert not inspect.isgeneratorfunction(api.resolve)

    def test_resolve_matches_resolve_closure_behaviour(self):
        """A thin alias must not silently diverge from what it wraps."""
        import time

        payload = {
            "Calculated_Field_Reference": {
                "ID": [
                    {"type": "WID", "_value_1": "W1"},
                    {"type": "Calculated_Field_ID", "_value_1": "CF_A"},
                ]
            },
            "Calculated_Field_Data": {
                "Calculated_Field_Reference_ID": "CF_A",
                "Name": "Field",
                "Class_Name": "Arithmetic Calculated Field",
            },
        }
        index = discovery_inventory.Index(
            kind="calculated_field", tenant="t", fetched_at=time.time()
        )
        index.summaries["W1"] = discovery_inventory.CalculatedFieldSummary(
            "W1", "CF_A", "Field", "Arithmetic Calculated Field"
        )
        index.payloads["W1"] = payload

        via_api = api.resolve(index, selected_field_wids=["W1"], expected_index_size=1)
        via_direct = api.resolve_closure(
            cf_index=index, selected_field_wids=["W1"], expected_index_size=1
        )
        assert set(via_api.nodes) == set(via_direct.nodes)


class TestGeneratorContract:
    """Everything long-running should yield progress events; resolve is the
    sole, documented exception.

    `iter_calculated_field_index` and friends are thin wrappers that `return`
    a generator built elsewhere (`_iter_index`), so
    `inspect.isgeneratorfunction` — which only looks for `yield` in the
    function's own body — reports False for them even though calling them
    hands back a real generator. The return annotation is the honest signal.
    """

    @pytest.mark.parametrize(
        "fn",
        [
            api.iter_calculated_field_index,
            api.iter_report_index,
            api.iter_check_existence,
            api.iter_execute,
        ],
    )
    def test_long_running_operations_are_declared_as_iterators(self, fn):
        return_annotation = inspect.signature(fn).return_annotation
        assert "Iterator" in str(return_annotation)


class TestEndToEndOfflineSmoke:
    """Wires the facade functions together the way a UI step would, entirely
    against fakes — no network, no writes."""

    def test_resolve_plan_and_validate_with_no_conflicts(self):
        import time

        payload = {
            "Calculated_Field_Reference": {
                "ID": [
                    {"type": "WID", "_value_1": "W1"},
                    {"type": "Calculated_Field_ID", "_value_1": "CF_A"},
                ]
            },
            "Calculated_Field_Data": {
                "Calculated_Field_Reference_ID": "CF_A",
                "Name": "Field",
                "Class_Name": "Arithmetic Calculated Field",
            },
        }
        index = discovery_inventory.Index(
            kind="calculated_field", tenant="t", fetched_at=time.time()
        )
        index.summaries["W1"] = discovery_inventory.CalculatedFieldSummary(
            "W1", "CF_A", "Field", "Arithmetic Calculated Field"
        )
        index.payloads["W1"] = payload

        closure = api.resolve(index, selected_field_wids=["W1"], expected_index_size=1)
        existence = {
            node_id: api.Existence(node_id, api.LookupOutcome.NOT_FOUND)
            for node_id in closure.nodes
        }
        plan = api.build_plan(closure, existence)
        assert plan.counts()["create"] == 1
        assert api.validate_plan(plan) == []
