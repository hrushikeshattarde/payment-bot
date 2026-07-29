"""Unit tests for the compute_carrier_rate tool (§4.1.1 / §3.2).

The tool sources earnings/deductions from Transport Pro by load id (not from the model),
so its output is authoritative and grounded.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from payment_bot.clients import LoadFixture, MockTransportProClient
from payment_bot.grounding import GroundingLedger
from payment_bot.models import TransportProLoad
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import ComputeCarrierRate, ComputeCarrierRateInput


@pytest.mark.unit
def test_gross_net_and_grounding_for_2462934(ctx: ToolContext) -> None:
    out = ComputeCarrierRate().run(ComputeCarrierRateInput(load_id="2462934"), ctx)

    assert out.gross_rate == Decimal("4650")
    assert out.total_deductions == Decimal("0")
    assert out.net_rate == Decimal("4650")
    assert [(line.title, line.amount) for line in out.earnings_breakdown] == [
        ("TRUCK ORDER NOT USED", Decimal("150")),
        ("Brokerage Line Haul", Decimal("4500")),
    ]
    assert out.deductions == []
    # Every reported amount is grounded.
    assert {Decimal("150"), Decimal("4500"), Decimal("4650")} <= ctx.ledger.grounded_amounts


@pytest.mark.unit
def test_deductions_are_reported_with_reason_and_net() -> None:
    load = TransportProLoad.model_validate(
        {
            "load_id": 2400002,
            "earnings": [{"title": "Line Haul", "amount": 5000}],
            "deductions": [{"title": "Advance", "amount": 1000, "reason": "Fuel advance"}],
        }
    )
    client = MockTransportProClient({"2400002": LoadFixture(load=load)})
    ctx = ToolContext(tp=client, ledger=GroundingLedger(), correlation_id="t")

    out = ComputeCarrierRate().run(ComputeCarrierRateInput(load_id="2400002"), ctx)

    assert out.gross_rate == Decimal("5000")
    assert out.total_deductions == Decimal("1000")
    assert out.net_rate == Decimal("4000")
    assert out.deductions[0].reason == "Fuel advance"


@pytest.mark.unit
def test_deduction_reason_falls_back_to_title() -> None:
    load = TransportProLoad.model_validate(
        {
            "load_id": 2400003,
            "earnings": [{"title": "Line Haul", "amount": 2000}],
            "deductions": [{"title": "Short Pay", "amount": 50}],  # no explicit reason
        }
    )
    client = MockTransportProClient({"2400003": LoadFixture(load=load)})
    ctx = ToolContext(tp=client, ledger=GroundingLedger(), correlation_id="t")

    out = ComputeCarrierRate().run(ComputeCarrierRateInput(load_id="2400003"), ctx)
    assert out.deductions[0].reason == "Short Pay"
