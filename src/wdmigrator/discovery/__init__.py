"""Read-side inventory of tenant configuration.

There is no usable server-side search — calculated fields have no filter
criteria at all, and report search is exact-match — so discovery means building
a local index and searching that. See `inventory.py` for the details.
"""

from wdmigrator.discovery.inventory import (
    PAGE_SIZE,
    CalculatedFieldSummary,
    Index,
    IndexProgress,
    LookupOutcome,
    LookupResult,
    ReportSummary,
    build_index,
    cache_path,
    classify_fault,
    find_report_by_exact_name,
    ids_of,
    iter_calculated_field_index,
    iter_report_index,
    load_index,
    lookup_calculated_field,
    lookup_calculated_measure,
    lookup_report,
    lookup_report_by_name,
    save_index,
)

__all__ = [
    "PAGE_SIZE",
    "CalculatedFieldSummary",
    "Index",
    "IndexProgress",
    "LookupOutcome",
    "LookupResult",
    "ReportSummary",
    "build_index",
    "cache_path",
    "classify_fault",
    "find_report_by_exact_name",
    "ids_of",
    "iter_calculated_field_index",
    "iter_report_index",
    "load_index",
    "lookup_calculated_field",
    "lookup_calculated_measure",
    "lookup_report",
    "lookup_report_by_name",
    "save_index",
]
