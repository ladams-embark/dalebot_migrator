"""One-off: verify the newly-provided wd501 services host actually serves a
real WSDL for the commitconsulting tenant. Read-only."""

import requests

url = "https://impl-services1.wd501.myworkday.com/ccx/service/commitconsulting/Core_Implementation_Service/v46.0?wsdl"
r = requests.get(url, timeout=15)
body = r.text[:200].lstrip()
looks_like_wsdl = body.startswith("<?xml") and "wsdl" in body.lower()
print(f"HTTP {r.status_code}, {len(r.content)} bytes, looks_like_wsdl={looks_like_wsdl}")
print(f"body[:150]: {r.text[:150]!r}")
