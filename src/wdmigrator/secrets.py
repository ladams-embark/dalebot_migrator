"""Keeping ISU passwords out of places they should never appear.

WS-Security sends the password as **cleartext** inside the SOAP envelope. That
makes a zeep fault, a debug log line, or a rendered traceback a credential
disclosure, because any of them can carry the request envelope. Wrapping the
password in :class:`Secret` means the accidental paths — f-strings, ``repr``,
logging, ``str()`` on a dataclass — print ``***`` instead of the value, and
only an explicit ``.reveal()`` gets the real thing.

This is defence in depth, not a guarantee. The other half is redacting text
that already contains the value; see :func:`redact` and :func:`redact_envelope`.
"""

from __future__ import annotations

import re
from typing import Iterable

#: The WS-Security header block, which carries the password in cleartext.
_WSSE_SECURITY = re.compile(
    r"<(?P<prefix>\w+:)?Security\b.*?</(?P=prefix)?Security>",
    re.DOTALL | re.IGNORECASE,
)

#: Any <...Password...>value</...> element, as a backstop.
_PASSWORD_ELEMENT = re.compile(
    r"(<(?:\w+:)?Password\b[^>]*>)(.*?)(</(?:\w+:)?Password>)",
    re.DOTALL | re.IGNORECASE,
)


class Secret:
    """A string that resists accidental disclosure.

    Deliberately **unhashable**: that stops a Secret being used as a dict key
    or, more importantly, ending up in a Streamlit ``@st.cache_data`` key,
    where it would be retained in a process-global cache shared across
    sessions.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value or ""

    def reveal(self) -> str:
        """The actual secret. Every call site should be easy to grep for."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return "***"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    #: Unhashable on purpose — see the class docstring.
    __hash__ = None  # type: ignore[assignment]


def redact(text: str, secrets: Iterable[Secret | str] = ()) -> str:
    """Replace known secret values in ``text`` with ``***``.

    Use on anything derived from an exception or a SOAP envelope before it is
    logged or rendered. Short values are skipped: redacting a 1-2 character
    password would blank out unrelated characters everywhere and make the
    output useless without meaningfully protecting anything.
    """
    if not text:
        return text

    result = text
    for secret in secrets:
        value = secret.reveal() if isinstance(secret, Secret) else secret
        if value and len(value) >= 3:
            result = result.replace(value, "***")
    return result


def redact_envelope(xml: str) -> str:
    """Strip credentials out of a SOAP envelope so it is safe to display.

    Removes the whole ``<wsse:Security>`` block, then blanks any remaining
    ``<Password>`` element as a backstop in case the header is shaped
    differently than expected.
    """
    if not xml:
        return xml

    cleaned = _WSSE_SECURITY.sub("<!-- wsse:Security removed -->", xml)
    return _PASSWORD_ELEMENT.sub(r"\1***\3", cleaned)


def install_redacting_log_filter(*secrets: Secret | str) -> None:
    """Attach a filter to the root logger that redacts these secrets.

    zeep and urllib3 both log request detail at DEBUG. If someone turns debug
    logging on while chasing a fault, the password would otherwise land in the
    log. Call this once, early, before any client is built.
    """
    import logging

    values = [s.reveal() if isinstance(s, Secret) else s for s in secrets]
    values = [v for v in values if v and len(v) >= 3]
    if not values:
        return

    class _Redactor(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg, values)
            if record.args:
                record.args = tuple(
                    redact(a, values) if isinstance(a, str) else a
                    for a in (
                        record.args
                        if isinstance(record.args, tuple)
                        else (record.args,)
                    )
                )
            return True

    logging.getLogger().addFilter(_Redactor())
