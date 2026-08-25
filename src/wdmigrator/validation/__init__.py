"""Post-migration verification: read the destination back and compare.

Every migration this project has actually trusted was verified by hand-written
scripts against the destination, not by trusting the writer's own success bit
(see HANDOFF: "0 failed" while two of three dashboards were empty shells).
:mod:`~wdmigrator.validation.verify` formalises that read-back so the answer
is programmatic per-run rather than bespoke per-migration.
"""

from wdmigrator.validation.verify import (
    VerifyFinding,
    VerifyProgress,
    VerifyRecord,
    VerifyStatus,
    iter_verify,
    summarise,
    verify_record,
)

__all__ = [
    "VerifyFinding",
    "VerifyProgress",
    "VerifyRecord",
    "VerifyStatus",
    "iter_verify",
    "summarise",
    "verify_record",
]
