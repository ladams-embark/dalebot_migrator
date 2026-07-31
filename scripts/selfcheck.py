"""selfcheck.py — prove the dev environment is wired up correctly.

Runs entirely OFFLINE. No .env needed, no tenant contacted, no writes.
Use this as the first thing to run after cloning or changing dependencies:

    python scripts/selfcheck.py
"""

import os
import sys
from pathlib import Path

# Let this script import the sibling prototype in scripts/.
sys.path.insert(0, str(Path(__file__).parent))


def main() -> int:
    ok = True

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "PASS" if condition else "FAIL"
        if not condition:
            ok = False
        print(f"  [{mark}] {label}{f' — {detail}' if detail else ''}")

    print("wdmigrator selfcheck (offline)\n")

    print("Package:")
    import wdmigrator

    check("wdmigrator imports", True, f"v{wdmigrator.__version__}")
    wsdl = wdmigrator.DEFAULT_WSDL_PATH
    check("bundled WSDL present", wsdl.is_file(), f"{wsdl.stat().st_size:,} bytes")

    print("\nSubpackages:")
    for name in ("auth", "discovery", "migrate", "validation"):
        try:
            __import__(f"wdmigrator.{name}")
            check(f"wdmigrator.{name}", True)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            check(f"wdmigrator.{name}", False, repr(exc))

    print("\nDependencies:")
    import zeep

    check("zeep", True, zeep.__version__)
    check("zeep >= 4.3.3 (the verified version)", zeep.__version__ >= "4.3.3", zeep.__version__)
    for mod in ("requests", "lxml.etree", "click", "dotenv", "pytest"):
        try:
            __import__(mod)
            check(mod, True)
        except Exception as exc:  # noqa: BLE001
            check(mod, False, repr(exc))

    print("\nOffline SOAP client (no tenant contacted):")
    from zeep import Client, Settings

    client = Client(wsdl=str(wsdl), settings=Settings(strict=False, xml_huge_tree=True))
    check("client builds from local WSDL", True)

    for op in (
        "Get_Calculated_Fields",
        "Put_Calculated_Field",
        "Get_Tenanted_Report_Definitions",
        "Put_Tenanted_Report_Definition",
    ):
        check(f"operation {op}", hasattr(client.service, op))

    service = next(iter(client.wsdl.services.values()))
    address = next(iter(service.ports.values())).binding_options["address"]
    check("endpoint uses services host", "-services" in address)
    check("endpoint has versioned path", "/Core_Implementation_Service/v47.0" in address)
    print(f"         endpoint: {address}")

    print("\nRead prototype:")
    import get_calculated_field as proto

    check("scripts/get_calculated_field.py imports", True)
    if os.environ.get("WD_SOURCE_SERVICES_HOST") and os.environ.get("WD_SOURCE_TENANT"):
        check(
            "resolves live tenant endpoint (.env present)",
            proto.SERVICE_NAME in proto.WSDL_SOURCE and proto.WSDL_SOURCE.startswith("https://"),
        )
    else:
        check("resolves bundled WSDL (no .env)", Path(proto.WSDL_SOURCE) == wsdl)
    req = proto.build_request("SAMPLE_ID", "Calculated_Field_ID")
    check(
        "Response_Group requests field data",
        req["Response_Group"]["Include_Calculated_Field_Data"] is True,
    )
    check(
        "sends Request_References XOR Request_Criteria",
        "Request_Criteria" not in req,
        "choice honoured",
    )

    print()
    if ok:
        print("All checks passed. Environment is ready.")
        print("Next: `pytest` for the offline suite, or see docs/START_HERE.md.")
    else:
        print("Some checks FAILED — see above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
