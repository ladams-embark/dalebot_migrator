"""The implementer gate is an account *type*, and it has to be found early.

Dashboards, prompt sets, prompt fields and time calculations are readable only
by an implementer account. No security domain grant substitutes. Before this,
nothing asked the question until a user had picked Dashboards on Scope, waited
out a sweep on Select, and been told on the far side that their connection
could never have done it. Connect now probes, and Scope spends the answer.
"""

from __future__ import annotations

import pytest

from wdmigrator.api import Capabilities
from wdmigrator.discovery.inventory import probe_capabilities
from wdmigrator.ui.state import WizardState
from wdmigrator.ui.steps import scope as scope_step


class _Limiter:
    def wait(self) -> None:
        pass


class _Service:
    def __init__(self, error: Exception | None):
        self._error = error

    def Get_Custom_Dashboards_without_Tabs(self, **_kwargs):
        if self._error is not None:
            raise self._error
        return {"Response_Data": {}}


class _Connection:
    """Enough of a ``Connection`` for the probe: a service, a limiter, redact."""

    def __init__(self, error: Exception | None = None):
        self.service = _Service(error)
        self.limiter = _Limiter()

    def redact(self, text: str) -> str:
        return text


class TestProbe:
    def test_a_clean_get_means_implementer(self):
        caps = probe_capabilities(_Connection())
        assert caps.implementer is True
        assert caps.known
        assert caps.label == "Implementer"

    def test_the_implementer_fault_is_recognised_as_a_denial(self):
        """The live fault text, which ``requires_implementer`` matches on."""
        caps = probe_capabilities(
            _Connection(
                RuntimeError(
                    "Processing error occurred. The task submitted is not "
                    "authorized."
                )
            )
        )
        assert caps.implementer is False
        assert caps.label == "Standard ISU"
        assert "cannot read dashboards" in caps.detail

    def test_any_other_fault_leaves_the_answer_unknown(self):
        """An outage is not an entitlement finding. ``None`` must never be
        collapsed to ``False`` — that would hide a working account's kinds."""
        caps = probe_capabilities(_Connection(RuntimeError("HTTP 503 upstream")))
        assert caps.implementer is None
        assert not caps.known
        assert "503" in caps.detail


def _state(source: bool | None, dest: bool | None, **kwargs) -> WizardState:
    state = WizardState(step="scope", **kwargs)
    for side, value in ((state.source, source), (state.dest, dest)):
        side.capabilities = (
            None if value == "unprobed" else Capabilities(implementer=value, detail="d")
        )
    return state


class TestVerdict:
    def test_two_implementers_is_available_and_silent(self):
        available, note = scope_step._implementer_verdict(_state(True, True))
        assert available
        assert note == ""

    @pytest.mark.parametrize(
        "source, dest, named",
        [
            (False, True, "Source"),
            (True, False, "Destination"),
            (False, False, "Source and Destination"),
        ],
    )
    def test_either_side_failing_blocks_and_the_note_names_which(self, source, dest, named):
        """The source has to read the object and the destination has to write
        it. One working side is no use on its own, and a user cannot fix what
        the app will not name."""
        available, note = scope_step._implementer_verdict(_state(source, dest))
        assert not available
        assert note.startswith(named)
        assert "Reports and calculated fields still work" in note

    def test_an_unknown_side_warns_but_never_blocks(self):
        available, note = scope_step._implementer_verdict(_state(None, True))
        assert available, "an inconclusive probe must not take kinds away"
        assert "Source could not be checked" in note

    def test_an_unprobed_side_warns_but_never_blocks(self):
        """No connection attempt yet, or a session restored without one."""
        available, note = scope_step._implementer_verdict(_state("unprobed", True))
        assert available
        assert "Source could not be checked" in note

    def test_a_denial_outranks_an_unknown(self):
        available, note = scope_step._implementer_verdict(_state(None, False))
        assert not available
        assert note.startswith("Destination")


class TestGate:
    def test_a_gated_kind_left_in_scope_blocks_continue(self):
        """``render`` unticks these, but a state restored from disk or set by a
        loaded package never went through ``render``."""
        state = _state(True, False, object_kinds=["dashboards", "reports"])
        blockers = scope_step.gate(state)
        assert blockers
        assert blockers[0].title == "Dashboards need an implementer account"
        assert "untick" in blockers[0].remedy

    def test_both_gated_kinds_are_named_together(self):
        state = _state(False, False, object_kinds=["dashboards", "time_calculations"])
        assert (
            scope_step.gate(state)[0].title
            == "Dashboards and Time calculations need an implementer account"
        )

    def test_ungated_kinds_pass_with_a_standard_isu(self):
        state = _state(False, False, object_kinds=["reports", "calculated_fields"])
        assert scope_step.gate(state) == []

    def test_an_unknown_probe_does_not_gate_anything(self):
        state = _state(None, None, object_kinds=["dashboards"])
        assert scope_step.gate(state) == []
