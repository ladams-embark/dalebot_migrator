"""
resolve_wd_support_account.py
-------------------------------
One-off diagnostic: find which System_User lookup operation actually
resolves the "wd-support" account on the DESTINATION tenant, and print its
WID. Tries Integration System User first (most likely for a shared
support/utility account), then falls through to Employee and Contingent
Worker System User lookups. Read-only.

Run it:
  python scripts/resolve_wd_support_account.py [username]
"""

import os
import sys

from dotenv import load_dotenv
from zeep.helpers import serialize_object

from wdmigrator import api

load_dotenv()

USERNAME_DEFAULT = "wd-support"


def _try(label, fn):
    print(f"\n--- {label} ---")
    try:
        result = fn()
        print(result if isinstance(result, str) else repr(result)[:2000])
        return result
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}")
        return None


def main(username: str) -> None:
    target = api.target_from_parts(
        os.environ["WD_DEST_SERVICES_HOST"], os.environ["WD_DEST_TENANT"]
    )
    conn = api.connect(
        target,
        os.environ["WD_DEST_ISU_USERNAME"],
        os.environ["WD_DEST_ISU_PASSWORD"],
        role=api.Role.DESTINATION,
    )

    ref = {"ID": [{"type": "WorkdayUserName", "_value_1": username}]}

    def integration():
        resp = conn.service.Get_Integration_System_Users(
            Integration_System_Request_Criteria={"System_User_Reference": ref}
        )
        return serialize_object(resp)

    # Employee_System_User_Request_CriteriaType and
    # Contingent_Worker_System_User_Request_CriteriaType are both EMPTY types
    # in the WSDL (no filterable fields) — only Request_References works for
    # those two, which needs a WID/System_User_ID already in hand. Only
    # Integration_System_Request_Criteria.System_User_Reference accepts an
    # arbitrary ID type (including WorkdayUserName), so that's the only
    # operation that can resolve a username to a WID from scratch.
    _try("Get_Integration_System_Users", integration)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else USERNAME_DEFAULT)
