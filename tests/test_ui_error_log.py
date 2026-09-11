"""An unexpected failure should leave something you can forward.

``app.main`` wraps every step render and turns anything raised into one
redacted sentence. Redacting is right — a zeep fault can carry the request
envelope and the envelope can carry a WS-Security password in cleartext — but
it left the user holding a single line with no traceback and nothing to send
to whoever might recognise it.

The same redaction that makes the banner safe makes a file safe. These tests
are about the "safe" half: the file is written to be forwarded without anyone
reading it first, so nothing in it may depend on someone reading it first.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from wdmigrator.ui import errors
from wdmigrator.ui.state import WizardState

ROOT = pathlib.Path(__file__).resolve().parents[1]

PASSWORD = "s3cr3t-tenant-password"

_ENVELOPE_FAULT = f"""Server raised fault: 'invalid'. Request was:
<soapenv:Envelope>
 <soapenv:Header>
  <wsse:Security>
   <wsse:UsernameToken>
    <wsse:Username>isu@tenant</wsse:Username>
    <wsse:Password>{PASSWORD}</wsse:Password>
   </wsse:UsernameToken>
  </wsse:Security>
 </soapenv:Header>
</soapenv:Envelope>"""


def _state() -> WizardState:
    from wdmigrator.config.targets import target_from_parts

    state = WizardState(step="select", object_kinds=["reports"])
    state.source.target = target_from_parts("impl-services1.wd12.myworkday.com", "src_tenant")
    state.dest.target = target_from_parts("impl-services1.wd12.myworkday.com", "dst_tenant")
    state.source.password = PASSWORD
    state.dest.password = PASSWORD
    state.selected_reports_added = {"W0": {}, "W1": {}}
    return state


def _raised(message: str) -> Exception:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


class TestRedaction:
    def test_a_password_matched_by_value_never_reaches_the_file(self):
        body = errors.render_error_log(
            _raised(f"auth failed for {PASSWORD}"),
            step="connect",
            state=_state(),
            secrets=(PASSWORD, PASSWORD),
        )
        assert PASSWORD not in body
        assert "***" in body

    def test_a_wsse_header_is_removed_whole(self):
        """Not only the password value: the header block goes, so a password
        this session does not happen to know about cannot survive in it."""
        body = errors.render_error_log(
            _raised(_ENVELOPE_FAULT), step="select", state=_state(), secrets=()
        )
        assert PASSWORD not in body
        assert "wsse:Security removed" in body

    def test_the_file_says_what_it_does_and_does_not_strip(self):
        """Tenant names and usernames survive on purpose — a bug report
        without them is close to useless — so the file has to say so rather
        than let a reader assume it is fully anonymised."""
        body = errors.render_error_log(
            _raised("boom"), step="select", state=_state(), secrets=()
        )
        assert "stripped of passwords" in body
        assert "usernames are NOT stripped" in body


class TestContent:
    def _body(self) -> str:
        return errors.render_error_log(
            _raised("boom"), step="select", state=_state(), secrets=()
        )

    def test_the_traceback_is_there(self):
        body = self._body()
        assert "Traceback" in body
        assert "RuntimeError: boom" in body

    def test_both_tenants_and_the_step_are_named(self):
        body = self._body()
        assert "src_tenant" in body
        assert "dst_tenant" in body
        assert "step           : select" in body

    def test_how_far_the_run_had_got_is_recorded(self):
        body = self._body()
        assert "2 report(s)" in body
        assert "live run       : 0 record(s)" in body


class TestWriting:
    def test_the_path_is_returned_and_the_file_exists(self, tmp_path):
        path = errors.write_error_log(
            _raised("boom"), step="select", state=_state(), directory=tmp_path
        )
        assert path
        assert pathlib.Path(path).read_text(encoding="utf-8")

    def test_old_logs_are_rotated(self, tmp_path):
        for i in range(errors.KEEP_ERRORS + 5):
            (tmp_path / f"error-2026010{i:03d}-x.log").write_text("old", encoding="utf-8")
        errors.write_error_log(
            _raised("boom"), step="select", state=_state(), directory=tmp_path
        )
        assert len(list(tmp_path.glob("error-*.log"))) == errors.KEEP_ERRORS

    def test_a_failure_to_write_never_replaces_the_original_error(self, tmp_path):
        """This runs inside the handler of last resort. Raising here would
        swap the user's actual problem for a filesystem one and show neither."""
        blocked = tmp_path / "a-file-not-a-directory"
        blocked.write_text("x", encoding="utf-8")
        assert errors.write_error_log(
            _raised("boom"), step="select", state=_state(), directory=blocked
        ) == ""


class TestTheBannerPointsAtIt:
    def test_a_failing_step_writes_a_log_and_names_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(errors, "ERROR_DIR", tmp_path)

        from wdmigrator.ui.state import STATE_KEY
        from wdmigrator.ui.steps import select as select_step

        def _explode(_state):
            raise RuntimeError(f"kaboom {PASSWORD}")

        monkeypatch.setattr(select_step, "render", _explode)

        at = AppTest.from_file(str(ROOT / "streamlit_app.py"))
        at.session_state[STATE_KEY] = _state()
        at.run(timeout=20)

        assert not at.exception, "the handler of last resort must still catch it"
        rendered = " ".join(str(m.value) for m in at.markdown)
        assert "Unexpected error in the Select step" in rendered
        assert PASSWORD not in rendered
        written = list(tmp_path.glob("error-*.log"))
        assert len(written) == 1
        assert written[0].name in rendered
        assert "safe to send on" in rendered
