"""Custom dashboard and prompt set migration — offline.

Fixtures here are shaped from a real payload dumped off `commitconsulting_dpt1`
(``Commit - Optimize Reporting Dashboard``, 2026-08-07), not from the schema.
That distinction has mattered repeatedly on this project: a fixture built from
what the WSDL *permits* encodes the shape we assumed, and passes while the real
thing fails. Every reference shape below was observed in that dump.
"""

from __future__ import annotations

import time

import pytest

from wdmigrator.discovery.inventory import (
    DASHBOARD_FLAVOURS,
    DashboardSummary,
    Index,
    PromptFieldSummary,
    PromptSetSummary,
    requires_implementer,
)
from wdmigrator.migrate.ordering import (
    extract_dashboard_refs,
    extract_prompt_field_refs,
    extract_prompt_set_refs,
    extract_report_refs,
)
from wdmigrator.migrate.planner import Action, MigrationPlan
from wdmigrator.migrate.resolver import (
    DASHBOARD_KINDS,
    Node,
    NodeKind,
    node_id_for,
    resolve_closure,
)
from wdmigrator.migrate.writer import (
    WriteError,
    build_dashboard_payload,
    build_prompt_field_payload,
    build_prompt_set_payload,
    build_report_payload,
    is_dashboard_worklet,
    operation_for,
)


def ref(**ids):
    return {"ID": [{"type": t, "_value_1": v} for t, v in ids.items()]}


# ── Fixtures shaped from the live dump ───────────────────────────────────────


def worklet_config(report_id, report_wid, *, order="a"):
    """One Landing_Page_Admin_Configuration, as the tenant actually returns it."""
    return {
        "ID": f"LANDING_PAGE_ADMIN_CONFIGURATION-6-{order}",
        "Order": order,
        "Worklet__All__Reference": ref(WID=report_wid, Custom_Report_ID=report_id),
        "Required": True,
        "Workday-Delivered_Security_Group_Reference": [
            ref(WID="WDSG1", Workday_Security_Group_ID="implementers_wkdyGroup")
        ],
        "Security_Group_Reference": [
            ref(WID="TSG1", Tenant_Security_Group_ID="Report_Administrator")
        ],
        "Worklet_Size_Reference": ref(WID="SIZE1"),
        "Worklet_Title": f"Worklet for {report_id}",
    }


def tabbed_dashboard(wid="DB1", name="Commit - Optimize Reporting Dashboard"):
    return {
        "Custom_Dashboard_with_Tabs_Reference": ref(
            WID=wid, Custom_Landing_Page_Group_ID=name
        ),
        "Custom_Dashboard_with_Tabs_Data": {
            "Name": name,
            "Domains_Reference": [ref(WID="DOMAIN1")],
            "Max_Worklets_Allowed": 6,
            "Announcements_Data": {
                "Announcement_Data": [
                    {
                        "Reference_ID": "ANNOUNCEMENTS-6-487",
                        "Announcement_Title": "Instructions:",
                        "Image_Reference": ref(
                            File_ID="ANNOUNCEMENT_IMAGE-6-136",
                            Image_ID="ANNOUNCEMENT_IMAGE-6-136",
                        ),
                    }
                ]
            },
            "Content_Data": [
                {
                    "ID": "LANDING_PAGE_GROUP_CONFIGURATION-6-664",
                    "Tab_Name": "Custom Report Optimization",
                    "Tab_Data": {
                        "Prompt_Set_Reference": ref(WID="PS1", Prompt_Set_ID="Company"),
                        # Present on the real dashboard alongside the worklets;
                        # deferral must not take it with them.
                        "Custom_Landing_Page_Menu_Data": {
                            "Landing_Page_Menu_Section_Data": [
                                {
                                    "Section_Label": "Additional Reports",
                                    "Order": "a",
                                    "Landing_Page_Menu_Item_Data": [
                                        {"Custom_Report_Reference": ref(
                                            WID="RPT3",
                                            Custom_Report_ID="Calculated Fields Defined",
                                        )}
                                    ],
                                }
                            ]
                        },
                        "Dashboard_Admin_Configuration": [
                            worklet_config("Commit - Report Owner Terminated", "RPT1"),
                            worklet_config("Commit Custom Report Creation Trends", "RPT2",
                                           order="b"),
                        ],
                    },
                }
            ],
        },
    }


def untabbed_dashboard(wid="DB2", name="Cost Center Manager Dashboard"):
    return {
        "Custom_Dashboard_without_Tabs_Reference": ref(
            WID=wid, Custom_Landing_Page_ID=name
        ),
        "Custom_Dashboard_without_Tabs_Data": {
            "Name": name,
            "Domains_Reference": [ref(WID="DOMAIN1")],
            "Worklets_Data": [
                worklet_config("Commit - Report Owner Terminated", "RPT1"),
                {
                    "ID": "LANDING_PAGE_ADMIN_CONFIGURATION-6-9",
                    "Order": "b",
                    # A dashboard shown inside another dashboard.
                    "Landing_Page__All__Reference": ref(
                        WID="DB1", Custom_Landing_Page_Group_ID=(
                            "Commit - Optimize Reporting Dashboard"
                        )
                    ),
                },
            ],
        },
    }


def prompt_set(wid="PS1", name="Company"):
    return {
        "Prompt_Set_Reference": ref(WID=wid, Prompt_Set_ID=name),
        "Prompt_Set_Data": {
            "Name": name,
            "Counter_for_Reference_ID": 6,
            "Field_Category_Reference": ref(
                WID="FC1", External_Field_Category_ID="Uncategorized"
            ),
            "Tenanted_Prompt_Set_Member_Data": [
                {
                    "Reference_for_Webservices": "1",
                    "Label_Override": "Company",
                    "Abstract_External_Parameter_Reference": ref(WID="PARAM1"),
                    "Instance_Reference": [
                        ref(WID="ORG1", Organization_Reference_ID="TOP")
                    ],
                }
            ],
        },
    }


def report(wid="RPT1", name="Commit - Report Owner Terminated"):
    return {
        "Tenanted_Report_Definition_Reference": ref(WID=wid, Custom_Report_ID=name),
        "Tenanted_Report_Definition_Data": {
            "Name": name,
            "Shared": True,
            "Enable_As_Worklet": True,
            "Worklet_Max_Rows": 10,
            "Worklet_Help_Text": "Help",
            "Worklet_Icon_Reference": ref(WID="ICON1"),
            "Worklet_Landing_Page_Reference": ref(WID="DB1"),
            "Restricted_to_Tenanted_Security_Groups_Reference": [ref(WID="TSG1")],
        },
    }


def index(kind, items, summarise):
    idx = Index(kind=kind, tenant="t", fetched_at=0.0)
    for wid, payload in items.items():
        idx.summaries[wid] = summarise(wid, payload)
        idx.payloads[wid] = payload
    return idx


def dashboard_index(*payloads):
    def summarise(wid, payload):
        tabbed = DASHBOARD_FLAVOURS[True]["reference"] in payload
        spec = DASHBOARD_FLAVOURS[tabbed]
        return DashboardSummary(
            wid=wid,
            reference_id=payload[spec["reference"]]["ID"][1]["_value_1"],
            name=payload[spec["data"]]["Name"],
            tabbed=tabbed,
        )

    items = {}
    for payload in payloads:
        tabbed = DASHBOARD_FLAVOURS[True]["reference"] in payload
        spec = DASHBOARD_FLAVOURS[tabbed]
        items[payload[spec["reference"]]["ID"][0]["_value_1"]] = payload
    return index("dashboard", items, summarise)


def prompt_set_index(*payloads):
    return index(
        "prompt_set",
        {p["Prompt_Set_Reference"]["ID"][0]["_value_1"]: p for p in payloads},
        lambda wid, p: PromptSetSummary(
            wid=wid,
            reference_id=p["Prompt_Set_Reference"]["ID"][1]["_value_1"],
            name=p["Prompt_Set_Data"]["Name"],
        ),
    )


def empty_cf_index():
    return Index(kind="calculated_field", tenant="t", fetched_at=0.0)


# ── Extractors ───────────────────────────────────────────────────────────────


class TestExtractors:
    def test_dashboard_reports_are_found_by_the_existing_report_extractor(self):
        """The dashboard->report edge needs no new code. Confirmed live: the
        existing extractor found all 9 reports on the real dashboard.

        Both routes count — a worklet (Worklet__All__Reference) and a menu link
        (Custom_Report_Reference) — and both must migrate first."""
        data = tabbed_dashboard()["Custom_Dashboard_with_Tabs_Data"]
        assert extract_report_refs(data) == {
            "RPT1": "Commit - Report Owner Terminated",       # worklet
            "RPT2": "Commit Custom Report Creation Trends",   # worklet
            "RPT3": "Calculated Fields Defined",              # menu link
        }

    def test_prompt_set_references_are_extracted(self):
        data = tabbed_dashboard()["Custom_Dashboard_with_Tabs_Data"]
        assert extract_prompt_set_refs(data) == {"PS1": "Company"}

    def test_nested_dashboards_are_extracted_with_their_flavour(self):
        data = untabbed_dashboard()["Custom_Dashboard_without_Tabs_Data"]
        assert extract_dashboard_refs(data) == {
            "DB1": ("Commit - Optimize Reporting Dashboard", True)
        }

    def test_a_delivered_landing_page_is_not_treated_as_a_custom_dashboard(self):
        """Landing_Page__All__Reference also names Workday-delivered pages. Those
        carry no custom ID and must pass through, not resolve as a dependency."""
        data = {"Landing_Page__All__Reference": ref(WID="LP1", Landing_Page_ID="Home")}
        assert extract_dashboard_refs(data) == {}

    def test_a_reference_without_a_wid_is_skipped(self):
        assert extract_prompt_set_refs({"X": ref(Prompt_Set_ID="Company")}) == {}


# ── Closure resolution ───────────────────────────────────────────────────────


class TestClosure:
    def resolve(self, dashboard, **kwargs):
        spec = DASHBOARD_FLAVOURS[
            DASHBOARD_FLAVOURS[True]["reference"] in dashboard
        ]
        wid = dashboard[spec["reference"]]["ID"][0]["_value_1"]
        return resolve_closure(
            cf_index=empty_cf_index(),
            allow_partial_index=True,
            selected_dashboards={wid: dashboard},
            **kwargs,
        )

    def test_a_dashboard_pulls_in_its_prompt_set(self):
        closure = self.resolve(
            tabbed_dashboard(), prompt_set_index=prompt_set_index(prompt_set())
        )
        assert node_id_for(NodeKind.PROMPT_SET, "PS1") in closure.nodes

    def test_a_dashboard_pulls_in_a_nested_dashboard(self):
        closure = self.resolve(
            untabbed_dashboard(), dashboard_index=dashboard_index(tabbed_dashboard())
        )
        assert node_id_for(NodeKind.DASHBOARD_TABBED, "DB1") in closure.nodes

    def test_worklet_reports_are_pulled_in_through_the_report_loader(self):
        closure = self.resolve(
            tabbed_dashboard(), report_loader=lambda wid: report(wid)
        )
        assert node_id_for(NodeKind.REPORT, "RPT1") in closure.nodes
        assert node_id_for(NodeKind.REPORT, "RPT2") in closure.nodes

    def test_the_dashboard_depends_on_its_reports(self):
        closure = self.resolve(
            tabbed_dashboard(), report_loader=lambda wid: report(wid)
        )
        dashboard = closure.nodes[node_id_for(NodeKind.DASHBOARD_TABBED, "DB1")]
        assert node_id_for(NodeKind.REPORT, "RPT1") in dashboard.depends_on

    def test_a_missing_prompt_set_is_recorded_not_silently_dropped(self):
        closure = self.resolve(tabbed_dashboard(), prompt_set_index=prompt_set_index())
        assert closure.unresolved_prompt_set_ids == {"Company"}

    def test_a_missing_nested_dashboard_is_recorded(self):
        closure = self.resolve(
            untabbed_dashboard(), dashboard_index=dashboard_index()
        )
        assert closure.unresolved_dashboard_ids == {
            "Commit - Optimize Reporting Dashboard"
        }

    def test_prompt_sets_are_skipped_without_an_index(self):
        """Not passing an index means "do not resolve these", not "fail"."""
        closure = self.resolve(tabbed_dashboard())
        assert closure.unresolved_prompt_set_ids == set()
        assert not any(n.kind is NodeKind.PROMPT_SET for n in closure.nodes.values())

    def test_the_flavour_comes_from_the_payload_not_the_caller(self):
        closure = self.resolve(untabbed_dashboard())
        node = closure.nodes[node_id_for(NodeKind.DASHBOARD, "DB2")]
        assert node.kind is NodeKind.DASHBOARD
        assert node.reference_id == "Cost Center Manager Dashboard"


# ── Payload building ─────────────────────────────────────────────────────────


def dashboard_node(payload, *, tabbed=True, wid="DB1"):
    spec = DASHBOARD_FLAVOURS[tabbed]
    return Node(
        node_id=node_id_for(DASHBOARD_KINDS[tabbed], wid),
        kind=DASHBOARD_KINDS[tabbed],
        source_wid=wid,
        reference_id=payload[spec["reference"]]["ID"][1]["_value_1"],
        name=payload[spec["data"]]["Name"],
        payload=payload,
    )


class TestDashboardPayload:
    def data(self, **kwargs):
        node = dashboard_node(tabbed_dashboard())
        built = build_dashboard_payload(node, {}, action=Action.CREATE, **kwargs)
        return built["Custom_Dashboard_with_Tabs_Data"]

    def test_tenanted_security_groups_are_stripped(self):
        text = repr(self.data())
        assert "Report_Administrator" not in text

    def test_delivered_security_groups_are_stripped_too(self):
        """Kept at first, on the reasoning that a delivered business ID resolves
        anywhere. Disproved live: the dashboard write failed with 'references
        one or more invalid metadata security groups' over
        implementers_wkdyGroup, and no destination dashboard references any."""
        assert "implementers_wkdyGroup" not in repr(self.data())

    def test_announcements_are_stripped(self):
        assert "Announcements_Data" not in self.data()

    def test_the_worklet_report_reference_survives(self):
        assert "Commit - Report Owner Terminated" in repr(self.data())

    def test_create_sets_add_only(self):
        """The only Put in this tool with a server-side create-only guard."""
        node = dashboard_node(tabbed_dashboard())
        assert build_dashboard_payload(node, {}, action=Action.CREATE)["Add_Only"] is True

    def test_update_carries_the_destination_wid_and_no_add_only(self):
        node = dashboard_node(tabbed_dashboard())
        payload = build_dashboard_payload(
            node, {}, action=Action.UPDATE, dest_wid="DEST1"
        )
        assert payload["Custom_Dashboard_with_Tabs_Reference"]["ID"][0]["_value_1"] == "DEST1"
        assert "Add_Only" not in payload

    def test_update_without_a_destination_wid_is_refused(self):
        node = dashboard_node(tabbed_dashboard())
        with pytest.raises(Exception, match="without the destination"):
            build_dashboard_payload(node, {}, action=Action.UPDATE)

    def test_wids_are_remapped(self):
        node = dashboard_node(tabbed_dashboard())
        data = build_dashboard_payload(
            node, {"RPT1": "DEST_RPT1"}, action=Action.CREATE
        )["Custom_Dashboard_with_Tabs_Data"]
        assert "DEST_RPT1" in repr(data)
        assert "'RPT1'" not in repr(data)

    def test_the_untabbed_flavour_uses_its_own_keys(self):
        node = dashboard_node(untabbed_dashboard(), tabbed=False, wid="DB2")
        payload = build_dashboard_payload(node, {}, action=Action.CREATE)
        assert "Custom_Dashboard_without_Tabs_Data" in payload

    def test_the_source_payload_is_not_mutated(self):
        payload = tabbed_dashboard()
        build_dashboard_payload(dashboard_node(payload), {}, action=Action.CREATE)
        assert "Announcements_Data" in payload["Custom_Dashboard_with_Tabs_Data"]


class TestPromptSetPayload:
    def test_instance_defaults_are_left_alone(self):
        """Unlike a report filter's comparison value: most prompt defaults are
        delivered instances that resolve fine, and the ones that do not belong
        in the reference-decision table rather than being silently dropped."""
        node = Node(
            node_id="prompt_set:PS1", kind=NodeKind.PROMPT_SET, source_wid="PS1",
            reference_id="Company", name="Company", payload=prompt_set(),
        )
        data = build_prompt_set_payload(node, {}, action=Action.CREATE)[
            "Prompt_Set_Data"
        ]
        assert "Organization_Reference_ID" in repr(data)


class TestOperationRouting:
    @pytest.mark.parametrize(
        "kind,expected",
        [
            (NodeKind.DASHBOARD, "Put_Custom_Dashboard_without_Tabs"),
            (NodeKind.DASHBOARD_TABBED, "Put_Custom_Dashboard_with_Tabs"),
            (NodeKind.PROMPT_SET, "Put_Prompt_Set"),
        ],
    )
    def test_each_kind_routes_to_its_own_operation(self, kind, expected):
        node = Node(node_id="x", kind=kind, source_wid="W", reference_id="R",
                    name="n", payload={})
        assert operation_for(node) == expected


# ── The worklet split ────────────────────────────────────────────────────────


class TestWorkletHandling:
    def report_node(self, required_by=()):
        return Node(
            node_id="report:RPT1", kind=NodeKind.REPORT, source_wid="RPT1",
            reference_id="Commit - Report Owner Terminated",
            name="Commit - Report Owner Terminated", payload=report(),
            required_by=frozenset(required_by),
        )

    def test_a_standalone_report_still_lands_unplaced(self):
        data = build_report_payload(
            self.report_node(), {}, action=Action.CREATE,
            keep_worklet=is_dashboard_worklet(self.report_node()),
        )["Tenanted_Report_Definition_Data"]
        assert data["Enable_As_Worklet"] is False
        assert "Worklet_Max_Rows" not in data

    def test_a_dashboard_worklet_keeps_its_worklet_configuration(self):
        """Clearing this would migrate the dashboard with a hole where the
        worklet should be — a report reaches a dashboard only as a worklet."""
        node = self.report_node(required_by=["dashboard_tabbed:DB1"])
        assert is_dashboard_worklet(node)
        data = build_report_payload(
            node, {}, action=Action.CREATE, keep_worklet=True
        )["Tenanted_Report_Definition_Data"]
        assert data["Enable_As_Worklet"] is True
        assert data["Worklet_Max_Rows"] == 10

    def test_a_dashboard_worklet_must_be_shared(self):
        """Confirmed live by elimination: with Shared=False every worklet was
        rejected as 'not valid for the assigned dashboard', even alone; the
        same payload with Shared=True succeeded immediately."""
        node = self.report_node(required_by=["dashboard_tabbed:DB1"])
        data = build_report_payload(
            node, {}, action=Action.CREATE, keep_worklet=True
        )["Tenanted_Report_Definition_Data"]
        assert data["Shared"] is True

    def test_a_worklet_still_inherits_no_security_groups(self):
        """Sharing the report is not the same as copying who it was shared
        WITH. The Restricted_to_* references are the tenant-specific part and
        are what actually made reports unmigratable."""
        node = self.report_node(required_by=["dashboard_tabbed:DB1"])
        data = build_report_payload(
            node, {}, action=Action.CREATE, keep_worklet=True
        )["Tenanted_Report_Definition_Data"]
        assert "Restricted_to_Tenanted_Security_Groups_Reference" not in data

    def test_a_standalone_report_is_still_unshared(self):
        node = self.report_node()
        data = build_report_payload(
            node, {}, action=Action.CREATE, keep_worklet=False
        )["Tenanted_Report_Definition_Data"]
        assert data["Shared"] is False
        assert "Restricted_to_Tenanted_Security_Groups_Reference" not in data

    def worklet_node_with_landing_page(self, *, stable=True):
        payload = report()
        ids = {"WID": "DB1_SOURCE"}
        if stable:
            ids["Custom_Landing_Page_Group_ID"] = (
                "Commit - Optimize Reporting Dashboard"
            )
        payload["Tenanted_Report_Definition_Data"][
            "Worklet_Landing_Page_Reference"
        ] = [ref(**ids)]
        return Node(
            node_id="report:RPT1", kind=NodeKind.REPORT, source_wid="RPT1",
            reference_id="R", name="n", payload=payload,
            required_by=frozenset(["dashboard_tabbed:DB1"]),
        )

    def test_the_association_is_dropped_before_the_dashboard_exists(self):
        """First pass. Referencing the dashboard by its stable business ID does
        NOT work as a forward reference — confirmed live, Workday rejects
        'Commit - Optimize Reporting Dashboard' as an invalid
        Custom_Landing_Page_Group_ID while the dashboard is absent."""
        data = build_report_payload(
            self.worklet_node_with_landing_page(), {}, action=Action.CREATE,
            keep_worklet=True,
        )["Tenanted_Report_Definition_Data"]
        assert "Worklet_Landing_Page_Reference" not in data

    def test_the_association_is_written_once_the_dashboard_wid_is_known(self):
        """Second pass, after the dashboard has been created in this run."""
        data = build_report_payload(
            self.worklet_node_with_landing_page(),
            {"DB1_SOURCE": "DB1_DEST"}, action=Action.CREATE, keep_worklet=True,
        )["Tenanted_Report_Definition_Data"]
        entries = data["Worklet_Landing_Page_Reference"][0]["ID"]
        assert entries == [{"type": "WID", "_value_1": "DB1_DEST"}]

    def test_the_source_dashboard_wid_is_never_written(self):
        for wid_map in ({}, {"DB1_SOURCE": "DB1_DEST"}):
            data = build_report_payload(
                self.worklet_node_with_landing_page(), wid_map,
                action=Action.CREATE, keep_worklet=True,
            )["Tenanted_Report_Definition_Data"]
            assert "DB1_SOURCE" not in repr(data)

    def test_a_non_worklet_report_still_drops_the_landing_page(self):
        node = self.worklet_node_with_landing_page()
        data = build_report_payload(
            node, {}, action=Action.CREATE, keep_worklet=False
        )["Tenanted_Report_Definition_Data"]
        assert "Worklet_Landing_Page_Reference" not in data

    def test_a_subreport_dependency_does_not_count_as_a_worklet(self):
        assert not is_dashboard_worklet(self.report_node(required_by=["report:R9"]))

    @pytest.mark.parametrize(
        "kind", [NodeKind.CALCULATED_FIELD, NodeKind.PROMPT_SET, NodeKind.DASHBOARD]
    )
    def test_only_reports_can_be_worklets(self, kind):
        """A dashboard depends on calculated fields it uses in runtime prompts,
        and on its prompt set. None of those are worklets, and answering True
        for them makes the function useless anywhere but its one call site."""
        node = Node(
            node_id=f"{kind.value}:X", kind=kind, source_wid="X", reference_id="R",
            name="n", payload={}, required_by=frozenset(["dashboard_tabbed:DB1"]),
        )
        assert not is_dashboard_worklet(node)

    def test_a_dashboard_and_its_worklet_report_do_not_form_a_cycle(self):
        """The report names the dashboard back through
        Worklet_Landing_Page_Reference. The writer strips it, so the resolver
        must not treat it as an edge — confirmed live, doing so made the real
        dashboard unschedulable."""
        from wdmigrator.migrate.ordering import topological_sort

        closure = resolve_closure(
            cf_index=empty_cf_index(),
            allow_partial_index=True,
            selected_dashboards={"DB1": tabbed_dashboard()},
            report_loader=lambda wid: report(wid),
            dashboard_index=dashboard_index(tabbed_dashboard()),
        )
        ordered = topological_sort(closure.nodes)
        names = [n.node_id for n in ordered]
        assert names.index("report:RPT1") < names.index("dashboard_tabbed:DB1")


class TestReportTags:
    """A report tag blocked the first real live migration at object 18 of 25."""

    def tagged_report(self):
        payload = report()
        payload["Tenanted_Report_Definition_Data"]["Report_Tag_Reference"] = [
            ref(
                WID="d07f2203d8fc1001b64bf07d1d130000",
                Custom_Report_Tag_ID="Commit - Reporting Optimization Report-NDc3",
            )
        ]
        return payload

    def build(self, **kwargs):
        payload = self.tagged_report()
        node = Node(
            node_id="report:RPT1", kind=NodeKind.REPORT, source_wid="RPT1",
            reference_id="R", name="n", payload=payload,
        )
        return build_report_payload(node, {}, action=Action.CREATE, **kwargs)[
            "Tenanted_Report_Definition_Data"
        ]

    def test_report_tags_are_stripped(self):
        assert "Report_Tag_Reference" not in self.build()

    def test_stripped_for_dashboard_worklets_too(self):
        assert "Report_Tag_Reference" not in self.build(keep_worklet=True)

    def test_the_source_payload_is_not_mutated(self):
        payload = self.tagged_report()
        node = Node(
            node_id="report:RPT1", kind=NodeKind.REPORT, source_wid="RPT1",
            reference_id="R", name="n", payload=payload,
        )
        build_report_payload(node, {}, action=Action.CREATE)
        assert "Report_Tag_Reference" in payload["Tenanted_Report_Definition_Data"]


class TestInlineChildReferences:
    """A matrix measure blocked the composite at object 24 of 25, live."""

    def composite(self, wid="d07f2203d8fc1001b86ccee64da00000"):
        payload = report(wid="CMP1", name="Custom Report Exceptions by Owner")
        payload["Tenanted_Report_Definition_Data"]["Tenanted_Composite_Report_Data"] = {
            "Composite_Report_Region_Data": [
                {"Tenanted_Composite_Data_Column_Data": [
                    {"Tenanted_Composite_Sub-Report_Data": {
                        "Matrix_Measure__All__Reference": ref(
                            WID=wid, Matrix_Measure_Reference_ID="MATRIX_MEASURE-6-4022"
                        )
                    }}
                ]}
            ]
        }
        return payload

    def build(self, wid_map, **kwargs):
        payload = self.composite(**kwargs)
        node = Node(
            node_id="report:CMP1", kind=NodeKind.REPORT, source_wid="CMP1",
            reference_id="R", name="Custom Report Exceptions by Owner",
            payload=payload,
        )
        data = build_report_payload(node, wid_map, action=Action.CREATE)[
            "Tenanted_Report_Definition_Data"
        ]
        return data["Tenanted_Composite_Report_Data"][
            "Composite_Report_Region_Data"
        ][0]["Tenanted_Composite_Data_Column_Data"][0][
            "Tenanted_Composite_Sub-Report_Data"
        ]["Matrix_Measure__All__Reference"]["ID"]

    def test_an_unmapped_wid_is_dropped(self):
        """It is a dead source WID, not a delivered object passing through —
        the measure exists in the destination under the same business ID."""
        entries = self.build({})
        assert [e["type"] for e in entries] == ["Matrix_Measure_Reference_ID"]

    def test_the_business_id_is_preserved(self):
        entries = self.build({})
        assert entries[0]["_value_1"] == "MATRIX_MEASURE-6-4022"

    def test_a_mapped_wid_is_kept(self):
        """A WID the migration actually created resolves directly; dropping it
        would force an unnecessary lookup and lose precision."""
        entries = self.build({"SRC": "DESTWID"}, wid="SRC")
        assert {e["type"] for e in entries} == {
            "WID", "Matrix_Measure_Reference_ID"
        }
        assert [e["_value_1"] for e in entries if e["type"] == "WID"] == ["DESTWID"]

    def test_a_reference_without_a_business_id_is_left_alone(self):
        """Nothing else addresses it, so dropping the WID would lose the
        reference entirely — worse than failing loudly."""
        payload = self.composite()
        block = payload["Tenanted_Report_Definition_Data"][
            "Tenanted_Composite_Report_Data"]["Composite_Report_Region_Data"][0][
            "Tenanted_Composite_Data_Column_Data"][0][
            "Tenanted_Composite_Sub-Report_Data"]
        block["Matrix_Measure__All__Reference"] = ref(WID="ONLYWID")
        node = Node(
            node_id="report:CMP1", kind=NodeKind.REPORT, source_wid="CMP1",
            reference_id="R", name="n", payload=payload,
        )
        data = build_report_payload(node, {}, action=Action.CREATE)[
            "Tenanted_Report_Definition_Data"
        ]
        entries = data["Tenanted_Composite_Report_Data"][
            "Composite_Report_Region_Data"][0][
            "Tenanted_Composite_Data_Column_Data"][0][
            "Tenanted_Composite_Sub-Report_Data"][
            "Matrix_Measure__All__Reference"]["ID"]
        assert [e["type"] for e in entries] == ["WID"]


class TestWorkletDeferral:
    """The dashboard and its worklet reports are mutually dependent; both ends
    are validated at write time, so the dashboard is written twice."""

    def test_worklets_are_held_back_from_the_tabbed_shell(self):
        from wdmigrator.migrate.writer import _defer_dashboard_worklets

        payload = build_dashboard_payload(
            dashboard_node(tabbed_dashboard()), {}, action=Action.CREATE
        )
        full = _defer_dashboard_worklets(payload, "Custom_Dashboard_with_Tabs_Data")
        assert full is not None
        shell_tab = payload["Custom_Dashboard_with_Tabs_Data"]["Content_Data"][0]
        assert "Dashboard_Admin_Configuration" not in shell_tab["Tab_Data"]

    def test_the_deferred_copy_keeps_the_worklets(self):
        from wdmigrator.migrate.writer import _defer_dashboard_worklets

        payload = build_dashboard_payload(
            dashboard_node(tabbed_dashboard()), {}, action=Action.CREATE
        )
        full = _defer_dashboard_worklets(payload, "Custom_Dashboard_with_Tabs_Data")
        tab = full["Custom_Dashboard_with_Tabs_Data"]["Content_Data"][0]
        assert len(tab["Tab_Data"]["Dashboard_Admin_Configuration"]) == 2

    def test_the_shell_keeps_tabs_menus_and_prompts(self):
        """Only the worklets are deferred — everything else must survive, or the
        shell is not a faithful first write."""
        from wdmigrator.migrate.writer import _defer_dashboard_worklets

        payload = build_dashboard_payload(
            dashboard_node(tabbed_dashboard()), {}, action=Action.CREATE
        )
        _defer_dashboard_worklets(payload, "Custom_Dashboard_with_Tabs_Data")
        tab = payload["Custom_Dashboard_with_Tabs_Data"]["Content_Data"][0]
        assert tab["Tab_Name"] == "Custom Report Optimization"
        assert "Custom_Landing_Page_Menu_Data" in tab["Tab_Data"]
        assert "Prompt_Set_Reference" in tab["Tab_Data"]

    def test_untabbed_worklets_are_deferred_too(self):
        from wdmigrator.migrate.writer import _defer_dashboard_worklets

        payload = build_dashboard_payload(
            dashboard_node(untabbed_dashboard(), tabbed=False, wid="DB2"), {},
            action=Action.CREATE,
        )
        full = _defer_dashboard_worklets(
            payload, "Custom_Dashboard_without_Tabs_Data"
        )
        assert full is not None
        assert "Worklets_Data" not in payload["Custom_Dashboard_without_Tabs_Data"]

    def test_a_dashboard_with_no_worklets_defers_nothing(self):
        """No second write means no shell left behind if something later fails."""
        from wdmigrator.migrate.writer import _defer_dashboard_worklets

        payload = {"Custom_Dashboard_with_Tabs_Data": {"Name": "Empty"}}
        assert _defer_dashboard_worklets(
            payload, "Custom_Dashboard_with_Tabs_Data"
        ) is None

    def test_worklet_reports_are_found_from_the_reverse_edges(self):
        from wdmigrator.migrate.writer import _worklet_reports_for

        dashboard = dashboard_node(tabbed_dashboard())
        worklet = Node(
            node_id="report:RPT1", kind=NodeKind.REPORT, source_wid="RPT1",
            reference_id="R", name="worklet", payload=report(),
            required_by=frozenset([dashboard.node_id]),
        )
        other = Node(
            node_id="report:RPT9", kind=NodeKind.REPORT, source_wid="RPT9",
            reference_id="R9", name="unrelated", payload=report(),
            required_by=frozenset(["report:RPT1"]),
        )
        plan = MigrationPlan(ordered_nodes=[worklet, other, dashboard])
        assert _worklet_reports_for(dashboard, plan) == [worklet]


class TestImplementerDetection:
    def test_the_authorization_fault_is_recognised(self):
        assert requires_implementer(
            "Processing error occurred. The task submitted is not authorized."
        )

    def test_other_faults_are_not(self):
        """Notably the Report_Metadata entitlement fault, which is a different
        problem with a different fix and must not be mislabelled."""
        assert not requires_implementer(
            "The web service or version is invalid for the requested operation"
        )
        assert not requires_implementer("invalid username or password")
        assert not requires_implementer(None)


# ── Prompt fields (tenanted external parameters) ─────────────────────────────


def prompt_field(wid="PARAM1", reference_id="DateOE Open Date", name="OE Open Date"):
    return {
        "Prompt_Field_Reference": ref(WID=wid, TenantedExternalParameter=reference_id),
        "Prompt_Field_Data": {
            "Name": name,
            "Field_Type_Reference": ref(WID="FIELDTYPE1"),
            "Business_Object_Reference": ref(WID="BO1"),
            "Currency_Code_Reference": None,
        },
    }


def prompt_field_index(*payloads):
    index = Index(kind="prompt_field", tenant="t", fetched_at=time.time())
    for p in payloads:
        wid = p["Prompt_Field_Reference"]["ID"][0]["_value_1"]
        index.summaries[wid] = PromptFieldSummary(
            wid=wid,
            reference_id=p["Prompt_Field_Reference"]["ID"][1]["_value_1"],
            name=p["Prompt_Field_Data"]["Name"],
        )
        index.payloads[wid] = p
    return index


def prompt_set_with_custom_parameter(wid="PS1", name="Company"):
    payload = prompt_set(wid=wid, name=name)
    payload["Prompt_Set_Data"]["Tenanted_Prompt_Set_Member_Data"] = [
        {
            "Reference_for_Webservices": "1",
            "Abstract_External_Parameter_Reference": ref(
                WID="PARAM1", TenantedExternalParameter="DateOE Open Date"
            ),
        }
    ]
    return payload


class TestPromptFieldReferences:
    """A prompt set cannot be written before its parameters exist.

    Confirmed live 2026-08-12: ``Put_Prompt_Set`` failed with ``Invalid ID
    value ... for type = 'WID'`` naming an
    ``Abstract_External_Parameter_Reference``, 69 objects into a migration.
    """

    def test_a_custom_parameter_is_extracted(self):
        data = {
            "Abstract_External_Parameter_Reference": ref(
                WID="PARAM1", TenantedExternalParameter="DateOE Open Date"
            )
        }
        assert extract_prompt_field_refs(data) == {"PARAM1": "DateOE Open Date"}

    def test_a_delivered_parameter_is_ignored(self):
        """The 'Commit - HR Dashboard' prompt set's five members are all like
        this: a bare WID, absent from Get_Prompt_Fields on *both* tenants, and
        identical across tenants. Resolving them would invent a dependency that
        cannot be satisfied."""
        assert extract_prompt_field_refs(
            {"Abstract_External_Parameter_Reference": ref(WID="DELIVERED1")}
        ) == {}


class TestPromptFieldClosure:
    def resolve(self, **kwargs):
        return resolve_closure(
            cf_index=empty_cf_index(),
            allow_partial_index=True,
            selected_dashboards={"DB1": tabbed_dashboard()},
            **kwargs,
        )

    def test_a_prompt_set_pulls_in_its_custom_parameter(self):
        closure = self.resolve(
            prompt_set_index=prompt_set_index(prompt_set_with_custom_parameter()),
            prompt_field_index=prompt_field_index(prompt_field()),
        )
        assert node_id_for(NodeKind.PROMPT_FIELD, "PARAM1") in closure.nodes

    def test_the_prompt_set_depends_on_the_parameter(self):
        """Ordering is the whole point — the parameter has to be written first."""
        closure = self.resolve(
            prompt_set_index=prompt_set_index(prompt_set_with_custom_parameter()),
            prompt_field_index=prompt_field_index(prompt_field()),
        )
        prompt_set_node = closure.nodes[node_id_for(NodeKind.PROMPT_SET, "PS1")]
        assert node_id_for(NodeKind.PROMPT_FIELD, "PARAM1") in prompt_set_node.depends_on

    def test_a_delivered_parameter_creates_no_node(self):
        closure = self.resolve(
            prompt_set_index=prompt_set_index(prompt_set()),
            prompt_field_index=prompt_field_index(prompt_field()),
        )
        assert not [
            n for n in closure.nodes.values() if n.kind is NodeKind.PROMPT_FIELD
        ]

    def test_a_missing_parameter_is_recorded_not_silently_dropped(self):
        closure = self.resolve(
            prompt_set_index=prompt_set_index(prompt_set_with_custom_parameter()),
            prompt_field_index=prompt_field_index(),
        )
        assert closure.unresolved_prompt_field_ids == {"DateOE Open Date"}

    def test_no_prompt_field_index_means_no_prompt_field_resolution(self):
        """Opt-in, like prompt sets and dashboards — a caller that passes
        nothing behaves exactly as before."""
        closure = self.resolve(
            prompt_set_index=prompt_set_index(prompt_set_with_custom_parameter())
        )
        assert closure.unresolved_prompt_field_ids == set()
        assert not [
            n for n in closure.nodes.values() if n.kind is NodeKind.PROMPT_FIELD
        ]


class TestPromptFieldPayload:
    def node(self):
        payload = prompt_field()
        return Node(
            node_id=node_id_for(NodeKind.PROMPT_FIELD, "PARAM1"),
            kind=NodeKind.PROMPT_FIELD,
            source_wid="PARAM1",
            reference_id="DateOE Open Date",
            name="OE Open Date",
            payload=payload,
        )

    def test_create_omits_the_reference(self):
        built = build_prompt_field_payload(self.node(), {}, action=Action.CREATE)
        assert "Prompt_Field_Reference" not in built
        assert built["Prompt_Field_Data"]["Name"] == "OE Open Date"

    def test_update_without_a_destination_wid_is_refused(self):
        with pytest.raises(WriteError):
            build_prompt_field_payload(self.node(), {}, action=Action.UPDATE)

    def test_the_put_operation_is_put_prompt_field(self):
        assert operation_for(self.node()) == "Put_Prompt_Field"
