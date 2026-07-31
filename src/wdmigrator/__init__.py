"""wdmigrator — Workday tenant configuration migration tool.

Migrates calculated fields and custom report definitions from a SOURCE Workday
tenant to a DESTINATION tenant via the Core_Implementation_Service SOAP web
service. (Report_Metadata exposes the same operations but is rejected live on
this tenant regardless of domain security — see docs/WSDL_NOTES.md.)

Build order (see docs/START_HERE.md):
    auth.client -> discovery.inventory -> migrate.ordering
    -> migrate.writer -> validation.verify -> cli
"""

from pathlib import Path

__version__ = "0.1.0"

#: Directory holding bundled, non-code assets.
ASSETS_DIR = Path(__file__).parent / "assets"

#: Local copy of the tenant WSDL (Core_Implementation_Service, v47.0). Point
#: zeep at this to construct a client OFFLINE — no tenant round-trip needed
#: just to build the client. The WSDL embeds the service address, so real
#: operation calls still go to the tenant over HTTPS.
DEFAULT_WSDL_PATH = ASSETS_DIR / "core_implementation_service_wsdl.xml"

__all__ = ["__version__", "ASSETS_DIR", "DEFAULT_WSDL_PATH"]
