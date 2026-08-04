"""One reply must not disclose loads belonging to two different carriers.

Live regression. An RTS enquiry titled "RAD LOGISTICS ONE LLC | 1669695" asked about loads
2478316 and 2463787 in a table. `1669695` was RTS's own account reference in the subject and
it collided with a real load — SKYWAY TRUCK LINE INC's, from 2024. Both loads are factored
to RTS, so authorization passed correctly and every figure was grounded; the draft still
reported Skyway's $3,200 payment in a reply about RAD Logistics, reading as though RAD had
been paid.

No other check can see this: authorization is per load and was satisfied, grounding only
proves a figure came from a tool, coverage only proves loads are addressed. The tell is that
the loads do not share a carrier.
"""

from __future__ import annotations

import pytest

from payment_bot.clients import MockTransportProClient
from payment_bot.errors import ClientError
from payment_bot.gate import PreSendGate
from payment_bot.grounding import GroundingLedger
from payment_bot.models import InboundEmail
from payment_bot.sample_data import sample_transport_pro_client
from payment_bot.tools.base import ToolContext
from payment_bot.tools.submit import SubmitDraftOutput


class _TwoCarrierTp:
    """Two loads, two carriers — the shape that produced the live bad draft.

    Delegates to the populated sample client; a bare MockTransportProClient has no
    fixtures, so every lookup would raise and the check would find nothing to compare.
    """

    def __init__(self, second_carrier: str = "SKYWAY TRUCK LINE INC") -> None:
        self._inner = sample_transport_pro_client()
        self._second_carrier = second_carrier

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    def get_authorization_context(self, load_id: str):  # type: ignore[no-untyped-def]
        base = self._inner.get_authorization_context("2462934")
        if load_id == "1669695":
            return base.model_copy(update={"carrier_company": self._second_carrier})
        return base


def _draft(load_ids: list[str]) -> SubmitDraftOutput:
    return SubmitDraftOutput(
        reply_body=" ".join(f"Load {lid} is billed." for lid in load_ids),
        to="adenslow@rtsfinancial.com",
        load_ids=load_ids,
        citations=[],
    )


def _ctx(tp: object) -> ToolContext:
    return ToolContext(tp=tp, ledger=GroundingLedger(), correlation_id="carrier-test")  # type: ignore[arg-type]


@pytest.mark.unit
def test_two_carriers_in_one_reply_is_blocked(sample_email: InboundEmail) -> None:
    gate = PreSendGate()
    result = gate.evaluate(
        draft=_draft(["2462934", "1669695"]),
        email=sample_email,
        ctx=_ctx(_TwoCarrierTp()),
    )
    checks = {c.name: c for c in result.checks}
    assert checks["carrier_consistency"].passed is False
    assert not result.allowed
    assert "different carriers" in checks["carrier_consistency"].detail


@pytest.mark.unit
def test_one_carrier_across_several_loads_passes(sample_email: InboundEmail) -> None:
    """The normal multi-load case must be unaffected."""

    result = PreSendGate().evaluate(
        draft=_draft(["2462934", "1669695"]),
        email=sample_email,
        ctx=_ctx(_TwoCarrierTp(second_carrier="Idea Expedited, Inc")),
    )
    checks = {c.name: c for c in result.checks}
    assert checks["carrier_consistency"].passed is True


@pytest.mark.unit
def test_capitalisation_differences_are_not_a_mismatch(sample_email: InboundEmail) -> None:
    """The API returns the same company with inconsistent case; that is not two carriers."""

    result = PreSendGate().evaluate(
        draft=_draft(["2462934", "1669695"]),
        email=sample_email,
        ctx=_ctx(_TwoCarrierTp(second_carrier="IDEA EXPEDITED, INC")),
    )
    checks = {c.name: c for c in result.checks}
    assert checks["carrier_consistency"].passed is True, checks["carrier_consistency"].detail


@pytest.mark.unit
def test_an_unresolvable_carrier_is_skipped_not_failed(sample_email: InboundEmail) -> None:
    """An unreachable Transport Pro must not turn every draft into a block."""

    class _Failing(MockTransportProClient):
        def get_authorization_context(self, load_id: str):  # type: ignore[no-untyped-def]
            raise ClientError("Transport Pro GET /voiceai/load/x failed (HTTP 400)")

    result = PreSendGate().evaluate(
        draft=_draft(["2462934"]), email=sample_email, ctx=_ctx(_Failing())
    )
    checks = {c.name: c for c in result.checks}
    assert checks["carrier_consistency"].passed is True
    assert "no carrier to compare" in checks["carrier_consistency"].detail
