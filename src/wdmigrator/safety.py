"""The gate between a plan and a write.

This module exists because ``Core_Implementation_Service`` has **no delete
operation**. Anything this tool writes into a destination tenant must be
removed by hand, object by object, in the Workday UI. There is no rollback, so
the cost of a wrong write is far higher than the cost of a refused one, and
every rule here is biased accordingly.

Two design decisions worth stating up front, because they look overcautious
until the failure mode is considered:

**Same source and destination tenant is an unconditional block.** Not a
warning, not a checkbox — live execution is unreachable. An override that
exists is an override that eventually gets clicked, and the failure it permits
is silently corrupting the tenant you were migrating *from*, with no undo.
Dry runs against a same-tenant configuration stay allowed, so the whole flow
can still be exercised end to end.

**Anything not positively identified as an implementation or sandbox tenant is
treated as production.** :class:`~wdmigrator.config.Environment.UNKNOWN` is not
a neutral middle value; it is refused like production.

The guard is enforced *in the engine*, on every object, not once at the start
and not in the UI. UI-only enforcement is not enforcement — a bug in a step
transition, a stale session, or a future CLI would all bypass it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

from wdmigrator.config.targets import TenantTarget

#: Process-environment escape hatch for writing to a non-implementation tenant.
#: Deliberately an env var rather than a UI control: it cannot be clicked
#: through by accident, it does not exist on a hosted deployment unless someone
#: sets it on purpose, and it leaves a trace in how the app was launched.
ALLOW_NON_IMPL_ENV_VAR = "WDMIGRATOR_ALLOW_NON_IMPL"


class GuardViolation(RuntimeError):
    """A write was attempted that the safety rules forbid."""

    def __init__(self, guards: list["Guard"]) -> None:
        self.guards = guards
        detail = "; ".join(f"{g.title} — {g.remedy}" for g in guards)
        super().__init__(f"Write blocked by {len(guards)} guard(s): {detail}")


class Level(str, Enum):
    """BLOCK stops a live run outright. WARN is surfaced but does not stop it."""

    BLOCK = "block"
    WARN = "warn"


@dataclass(frozen=True)
class Guard:
    """One safety finding, phrased so the UI can render it without rewording."""

    id: str
    level: Level
    title: str
    detail: str
    remedy: str


@dataclass(frozen=True)
class WriteGuard:
    """Everything needed to decide whether a live write may proceed.

    ``dry_run`` defaults to True. A caller must opt *in* to writing, and the
    default value of this field is the last line of defence if some code path
    forgets to set it.
    """

    source: TenantTarget
    dest: TenantTarget
    dry_run: bool = True
    #: Hash of the reviewed plan. Live execution is only permitted against a
    #: plan the user actually reviewed and dry-ran; this ties the two together
    #: so an edited plan cannot ride an earlier approval.
    plan_hash: str = ""
    #: The destination tenant name, retyped by the user as confirmation.
    confirmed_tenant_name: str = ""
    #: Set when the user has run and viewed a dry run for ``plan_hash``.
    dry_run_reviewed: bool = False
    #: Connection tests currently green for both sides.
    source_verified: bool = False
    dest_verified: bool = False
    #: ISU usernames, compared only to spot a copy-paste mistake.
    source_username: str = ""
    dest_username: str = ""
    warnings_acknowledged: frozenset[str] = field(default_factory=frozenset)


def non_impl_override_enabled() -> bool:
    """Whether the process environment permits writing to a non-impl tenant."""
    return os.environ.get(ALLOW_NON_IMPL_ENV_VAR, "").strip() == "1"


def evaluate_guards(guard: WriteGuard) -> list[Guard]:
    """Return every guard finding for this configuration.

    Always returns the full list rather than short-circuiting, so the UI can
    show a user everything they need to fix at once instead of revealing
    problems one refused click at a time.

    A dry run only evaluates the rules that keep a *dry* run honest (the plan
    must exist); the write-specific rules are skipped, which is what lets a
    same-tenant setup exercise the whole flow.
    """
    guards: list[Guard] = []

    same_tenant = guard.source.identity() == guard.dest.identity()

    if same_tenant:
        # Reported at BLOCK even in dry-run mode so it is visible from the
        # first screen, not sprung on the user at the execute step.
        guards.append(
            Guard(
                id="same_tenant",
                level=Level.BLOCK,
                title="Source and destination are the same tenant",
                detail=(
                    f"Both sides point at {guard.dest.label}. A live migration "
                    "would write into the tenant you are migrating from, and "
                    "this service has no delete operation to undo it."
                ),
                remedy=(
                    "Point the destination at a different tenant. Dry runs are "
                    "still available in this configuration."
                ),
            )
        )

    if guard.dry_run:
        # Everything below governs writes only.
        return guards

    if not guard.dest.environment.is_safe_write_target:
        if not non_impl_override_enabled():
            guards.append(
                Guard(
                    id="unsafe_destination_environment",
                    level=Level.BLOCK,
                    title=(
                        f"Destination is classified {guard.dest.environment.value}"
                    ),
                    detail=(
                        f"{guard.dest.label} is not a recognised implementation "
                        "or sandbox tenant. Unrecognised environments are "
                        "treated as production."
                    ),
                    remedy=(
                        f"Use an implementation or sandbox tenant, or set "
                        f"{ALLOW_NON_IMPL_ENV_VAR}=1 in the environment if you "
                        "are certain this is correct."
                    ),
                )
            )
        elif guard.confirmed_tenant_name != guard.dest.tenant:
            guards.append(
                Guard(
                    id="unconfirmed_tenant_name",
                    level=Level.BLOCK,
                    title="Destination tenant name not confirmed",
                    detail=(
                        f"{ALLOW_NON_IMPL_ENV_VAR} is set, so writing to "
                        f"{guard.dest.environment.value} is permitted, but the "
                        "tenant name must be retyped exactly to proceed."
                    ),
                    remedy=f"Type the destination tenant name: {guard.dest.tenant}",
                )
            )

    if not guard.source_verified:
        guards.append(
            Guard(
                id="source_unverified",
                level=Level.BLOCK,
                title="Source connection not verified",
                detail="No successful connection test for the source tenant.",
                remedy="Run the source connection test.",
            )
        )

    if not guard.dest_verified:
        guards.append(
            Guard(
                id="dest_unverified",
                level=Level.BLOCK,
                title="Destination connection not verified",
                detail="No successful connection test for the destination tenant.",
                remedy="Run the destination connection test.",
            )
        )

    if not guard.plan_hash:
        guards.append(
            Guard(
                id="no_plan",
                level=Level.BLOCK,
                title="No reviewed plan",
                detail="Live execution requires a resolved, reviewed migration plan.",
                remedy="Resolve dependencies and complete conflict review.",
            )
        )
    elif not guard.dry_run_reviewed:
        guards.append(
            Guard(
                id="dry_run_not_reviewed",
                level=Level.BLOCK,
                title="Dry run not completed for this plan",
                detail=(
                    "This exact plan has not been dry-run and reviewed. Editing "
                    "the selection or conflict decisions invalidates a previous "
                    "dry run."
                ),
                remedy="Run a dry run and review the output before going live.",
            )
        )

    if (
        guard.source_username
        and guard.source_username.lower() == guard.dest_username.lower()
    ):
        guards.append(
            Guard(
                id="same_isu_username",
                level=Level.WARN,
                title="Same ISU username on both tenants",
                detail=(
                    f"Both sides authenticate as {guard.source_username!r}. That "
                    "is legitimate across two tenants, but often means one side "
                    "was copy-pasted and not updated."
                ),
                remedy="Confirm the destination credentials are the intended ones.",
            )
        )

    return guards


def blocking_guards(guard: WriteGuard) -> list[Guard]:
    """Only the findings that stop a live run."""
    return [g for g in evaluate_guards(guard) if g.level is Level.BLOCK]


def assert_write_allowed(guard: WriteGuard) -> None:
    """Raise :class:`GuardViolation` unless a live write may proceed.

    Call this immediately before *every* write, not once per run. Between two
    objects a session can be edited, a token can expire, and a plan can be
    swapped; re-checking per object costs nothing and closes all of that.

    A ``dry_run=True`` guard never reaches a write, so this raises for it —
    reaching this function at all in dry-run mode means a caller has confused
    the two paths.
    """
    if guard.dry_run:
        raise GuardViolation(
            [
                Guard(
                    id="dry_run_write_attempt",
                    level=Level.BLOCK,
                    title="Write attempted during a dry run",
                    detail=(
                        "assert_write_allowed was reached with dry_run=True. A "
                        "dry run must not call a write path at all."
                    ),
                    remedy="This is a bug in the caller — report it.",
                )
            ]
        )

    blocking = blocking_guards(guard)
    if blocking:
        raise GuardViolation(blocking)
