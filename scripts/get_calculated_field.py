"""
get_calculated_field.py
-----------------------
Calls the Workday 'Get_Calculated_Fields' SOAP operation on the
Report_Metadata web service.

All specifics below are CONFIRMED against the local WSDL
(report_metadata_wsdl.xml, Report_MetadataService, v47.0):
  - Service ...... Report_Metadata   (NOT Report_Builder)
  - Version ...... v47.0             (fixed in the schema)
  - Operation .... Get_Calculated_Fields (plural)
  - Reference .... Calculated_Field_Reference with ID type
                   "WID" or "Calculated_Field_ID"
  - Request shape (Get_Calculated_Fields_RequestType):
        [choice] Request_References | Request_Criteria
        Response_Filter (Page, Count, As_Of_* dates)
        Response_Group  (Include_Reference, Include_Calculated_Field_Data)
    Request_References and Request_Criteria are a CHOICE — send at most one.
    Request_Criteria is an empty type, so it carries no usable filters.
    Response_Group.Include_Calculated_Field_Data must be True to get the
    actual calculated-field definitions (the point of a migration export).

This script READS from the source tenant only. It performs no writes.

Run it:
  python scripts/get_calculated_field.py                 # all fields, page 1
  python scripts/get_calculated_field.py <id> [id_type]   # one field

Environment variables (never hardcode — see .env.example):
  WD_SOURCE_SERVICES_HOST — e.g. impl-services1.wd12.myworkday.com
  WD_SOURCE_TENANT        — e.g. commitconsulting_dpt1
  WD_SOURCE_ISU_USERNAME  — Integration System User (usually ISU_name@tenant)
  WD_SOURCE_ISU_PASSWORD  — ISU password
  WD_WWS_VERSION          — defaults to v47.0 (matches the WSDL)
  WD_WSDL_PATH            — optional; defaults to the WSDL bundled with the
                            wdmigrator package (src/wdmigrator/assets/). Set to
                            a live "...?wsdl" URL to fetch from the tenant.
"""

import os

from dotenv import load_dotenv
from zeep import Client, Settings
from zeep.transports import Transport
from requests import Session
from requests.auth import HTTPBasicAuth

from wdmigrator import DEFAULT_WSDL_PATH

load_dotenv()


# ── Config from environment ──────────────────────────────────────────────────

SERVICE_NAME = "Report_Metadata"                       # confirmed from WSDL
API_VERSION  = os.environ.get("WD_WWS_VERSION", "v47.0")  # WSDL fixes this at v47.0

# Build the client from the local WSDL by default (no tenant round-trip needed
# just to construct the client). The WSDL embeds the service address, so actual
# operation calls still go to the tenant endpoint over HTTPS.
WSDL_SOURCE = os.environ.get("WD_WSDL_PATH", str(DEFAULT_WSDL_PATH))


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "See workday-migrator/.env.example."
        )
    return val


# ── Build authenticated zeep client ──────────────────────────────────────────

def build_client() -> Client:
    """Return a zeep SOAP client for Report_Metadata, authenticated with the
    SOURCE tenant's ISU credentials. The WSDL is loaded from WSDL_SOURCE."""
    isu_user = _require_env("WD_SOURCE_ISU_USERNAME")
    isu_pass = _require_env("WD_SOURCE_ISU_PASSWORD")

    session = Session()
    session.auth = HTTPBasicAuth(isu_user, isu_pass)

    settings = Settings(strict=False, xml_huge_tree=True)
    transport = Transport(session=session)
    return Client(wsdl=WSDL_SOURCE, settings=settings, transport=transport)


# ── Request builder ──────────────────────────────────────────────────────────

def build_request(calculated_field_id: str | None = None,
                  id_type: str = "WID",
                  page: int = 1,
                  count: int = 100) -> dict:
    """
    Build the Get_Calculated_Fields request per Get_Calculated_Fields_RequestType.

    - calculated_field_id: if given, filter to one field via Request_References.
      id_type must be "WID" or "Calculated_Field_ID" (the only valid types).
    - if None, no reference/criteria is sent and all fields are returned,
      paged by Response_Filter.

    Response_Group.Include_Calculated_Field_Data is True so the response
    carries the actual field definitions (needed for migration).
    """
    if id_type not in ("WID", "Calculated_Field_ID"):
        raise ValueError('id_type must be "WID" or "Calculated_Field_ID"')

    request: dict = {
        # Request_References and Request_Criteria are a CHOICE — set at most one.
        "Response_Filter": {
            "Page": page,
            "Count": count,
        },
        "Response_Group": {
            "Include_Reference": True,
            "Include_Calculated_Field_Data": True,
        },
    }

    if calculated_field_id:
        request["Request_References"] = {
            "Calculated_Field_Reference": [
                {"ID": [{"type": id_type, "_value_1": calculated_field_id}]}
            ]
        }

    return request


# ── Main call ────────────────────────────────────────────────────────────────

def get_calculated_fields(calculated_field_id: str | None = None,
                          id_type: str = "WID"):
    """Call Get_Calculated_Fields and return the zeep response.
    Raises on HTTP error or SOAP fault."""
    client = build_client()
    operation = client.service.Get_Calculated_Fields
    request_body = build_request(calculated_field_id, id_type)

    print(f"[INFO] WSDL source: {WSDL_SOURCE}")
    print(f"[INFO] Service: {SERVICE_NAME}  Version: {API_VERSION}")
    print("[INFO] Operation: Get_Calculated_Fields")
    if calculated_field_id:
        print(f"[INFO] Filtering by {id_type}: {calculated_field_id}")
    else:
        print("[INFO] No filter — fetching all (page 1, count 100)")

    return operation(**request_body)


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import pprint

    field_id = sys.argv[1] if len(sys.argv) > 1 else None
    ref_type = sys.argv[2] if len(sys.argv) > 2 else "WID"

    try:
        result = get_calculated_fields(field_id, ref_type)
        pprint.pprint(result)
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        raise
