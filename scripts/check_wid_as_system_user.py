"""
check_wid_as_system_user.py
------------------------------
One-off: does a given WID resolve as an Integration_System_User on the
DESTINATION tenant? Used to confirm the account previously identified as
"wd-support" by WID actually is one, and to read back its WorkdayUserName.
Read-only.

Run it:
  python scripts/check_wid_as_system_user.py <wid>
"""

import os
import sys

from dotenv import load_dotenv
from zeep.helpers import serialize_object

from wdmigrator import api

load_dotenv()


def main(wid: str) -> None:
    target = api.target_from_parts(
        os.environ["WD_DEST_SERVICES_HOST"], os.environ["WD_DEST_TENANT"]
    )
    conn = api.connect(
        target,
        os.environ["WD_DEST_ISU_USERNAME"],
        os.environ["WD_DEST_ISU_PASSWORD"],
        role=api.Role.DESTINATION,
    )

    resp = conn.service.Get_Integration_System_Users(
        Request_References={
            "Integration_System_Reference": [{"ID": [{"type": "WID", "_value_1": wid}]}]
        }
    )
    data = serialize_object(resp)
    print(data)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/check_wid_as_system_user.py <wid>")
        raise SystemExit(1)
    main(sys.argv[1])
