"""Writing a shareable record of an unexpected failure.

``app.main`` catches everything a step render can raise and turns it into one
redacted sentence. That is the right thing to show — a zeep fault can carry
the request envelope, and the envelope can carry a WS-Security password in
cleartext — but it left the user with a one-line message, no traceback, and
nothing to send to whoever might know what it meant. The redaction that makes
the banner safe is also what makes a file safe, so write the file.

Everything written here goes through :func:`~wdmigrator.secrets.redact` and
:func:`~wdmigrator.secrets.redact_envelope` first. The file is meant to be
forwarded without review, so it has to be safe without review.
"""

from __future__ import annotations

import platform
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from wdmigrator import __version__
from wdmigrator.api import redact, redact_envelope

#: Alongside ``out/cache``, ``out/sessions`` and the run logs. Gitignored.
ERROR_DIR = Path("out") / "errors"

#: Rotated, like sessions. A directory that only ever grows is a directory
#: nobody ever looks in.
KEEP_ERRORS = 20


def _tenant(side) -> str:
    return side.target.tenant if getattr(side, "target", None) is not None else "(none)"


def _context_lines(state, step: str) -> list[str]:
    """Everything worth knowing that is not the traceback.

    Chosen to answer the questions actually asked when one of these turns up:
    which tenants, which step, how much had been selected, and whether a live
    write had happened. Tenant *names* are in here, which is why the folder is
    gitignored — but a bug report with no tenant in it is close to useless.
    """
    return [
        f"wdmigrator     : {__version__}",
        f"python         : {sys.version.split()[0]} on {platform.platform()}",
        f"step           : {step}",
        f"source tenant  : {_tenant(state.source)}",
        f"dest tenant    : {_tenant(state.dest)}",
        f"object kinds   : {', '.join(state.object_kinds) or '(none)'}",
        f"package        : {state.package.name if state.package is not None else '(none)'}",
        f"selected       : {len(state.selected_reports_added)} report(s), "
        f"{len(state.selected_dashboards_added)} dashboard(s), "
        f"{len(state.selected_field_wids)} field(s), "
        f"{len(state.selected_time_calculation_wids)} time calculation(s)",
        f"closure        : {len(state.closure) if state.closure is not None else '(none)'} objects",
        f"plan           : {'built' if state.plan is not None else '(none)'}",
        f"dry run        : {len(state.dry_run_records)} record(s), "
        f"reviewed={bool(state.dry_run_reviewed)}",
        f"live run       : {len(state.execute_records)} record(s)",
    ]


def render_error_log(exc: BaseException, *, step: str, state, secrets=()) -> str:
    """The file body, as text. Split out from writing it so the redaction is
    testable without a filesystem."""
    body = "\n".join(
        [
            "wdmigrator error report",
            f"written_at     : {datetime.now(timezone.utc).isoformat()}",
            *_context_lines(state, step),
            "",
            "This file has been stripped of passwords and of any WS-Security",
            "header found in an embedded SOAP envelope. Tenant names and",
            "usernames are NOT stripped.",
            "",
            "-- traceback ----------------------------------------------------",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]
    )
    # Envelope first: it removes whole <wsse:Security> blocks, which is the
    # case that matters even when the password is also matched by value.
    return redact(redact_envelope(body), secrets)


def write_error_log(
    exc: BaseException, *, step: str, state, secrets=(), directory=None
) -> str:
    """Write the report and return its path, or ``""`` if it could not be written.

    Never raises. This runs inside the handler of last resort, and a failure
    to write a log about an error must not replace the error with a different
    one — the user would then see neither.
    """
    try:
        folder = Path(directory) if directory is not None else ERROR_DIR
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = folder / f"error-{stamp}-{step}.log"
        path.write_text(
            render_error_log(exc, step=step, state=state, secrets=secrets),
            encoding="utf-8",
        )
        for stale in sorted(folder.glob("error-*.log"))[:-KEEP_ERRORS]:
            stale.unlink(missing_ok=True)
        return str(path)
    except Exception:  # noqa: BLE001 - see the docstring
        return ""


__all__ = ["ERROR_DIR", "KEEP_ERRORS", "render_error_log", "write_error_log"]
