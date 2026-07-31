"""Unit tests for carrier-rate computation (§4.1.1 / §3.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from payment_bot.domain import compute_carrier_rate
from payment_bot.models import Deduction, Earning


def _earning(title: str, amount: str) -> Earning:
    return Earning(title=title, amount=Decimal(amount))


@pytest.mark.unit
def test_gross_is_sum_of_earnings_prd_example() -> None:
    # §7.4 load 2462934: 150 + 4500 = 4650, no deductions.
    rate = compute_carrier_rate(
        earnings=[
            _earning("TRUCK ORDER NOT USED", "150"),
            _earning("Brokerage Line Haul", "4500"),
        ],
        deductions=[],
    )
    assert rate.gross_rate == Decimal("4650")
    assert rate.total_deductions == Decimal("0")
    assert rate.net_rate == Decimal("4650")
    assert [(line.title, line.amount) for line in rate.earnings_breakdown] == [
        ("TRUCK ORDER NOT USED", Decimal("150")),
        ("Brokerage Line Haul", Decimal("4500")),
    ]
    assert rate.deductions == []


@pytest.mark.unit
def test_net_subtracts_each_deduction() -> None:
    rate = compute_carrier_rate(
        earnings=[_earning("Line Haul", "5000")],
        deductions=[
            Deduction(title="Advance", amount=Decimal("1000"), reason="Fuel advance"),
            Deduction(title="Claim", amount=Decimal("250"), reason="Damage claim"),
        ],
    )
    assert rate.gross_rate == Decimal("5000")
    assert rate.total_deductions == Decimal("1250")
    assert rate.net_rate == Decimal("3750")
    assert [(d.title, d.amount, d.reason) for d in rate.deductions] == [
        ("Advance", Decimal("1000"), "Fuel advance"),
        ("Claim", Decimal("250"), "Damage claim"),
    ]


@pytest.mark.unit
def test_deduction_reason_falls_back_to_title() -> None:
    rate = compute_carrier_rate(
        earnings=[_earning("Line Haul", "2000")],
        deductions=[Deduction(title="Short Pay", amount=Decimal("100"))],
    )
    assert rate.deductions[0].reason == "Short Pay"


@pytest.mark.unit
def test_none_deductions_is_treated_as_empty() -> None:
    rate = compute_carrier_rate(earnings=[_earning("Line Haul", "2000")], deductions=None)
    assert rate.total_deductions == Decimal("0")
    assert rate.net_rate == Decimal("2000")
    assert rate.deductions == []


@pytest.mark.unit
def test_empty_earnings_gives_zero_gross() -> None:
    rate = compute_carrier_rate(earnings=[], deductions=None)
    assert rate.gross_rate == Decimal("0")
    assert rate.net_rate == Decimal("0")


@pytest.mark.unit
def test_decimal_precision_is_exact() -> None:
    # Guards against float drift: 0.1 + 0.2 must be exactly 0.3 here.
    rate = compute_carrier_rate(
        earnings=[_earning("A", "0.1"), _earning("B", "0.2")],
        deductions=None,
    )
    assert rate.gross_rate == Decimal("0.3")


# --- Deduction sign ----------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("deduction_amount", ["-11.25", "11.25"])
def test_deductions_reduce_the_net_whichever_sign_the_api_uses(deduction_amount: str) -> None:
    """Live load 2496737 returned a net of 236.25 against a gross of 225.

    Transport Pro sends deductions as negatives, so `gross - total` added the deduction
    instead of subtracting it — overstating what the carrier was owed by twice the deduction,
    on every load that has one. The grounding check cannot catch it: the figure really did
    come from a tool.
    """

    rate = compute_carrier_rate(
        earnings=[Earning(title="Brokerage Line Haul", amount=Decimal("225"))],
        deductions=[Deduction(title="Quick Pay Brokerage", amount=Decimal(deduction_amount))],
    )

    assert rate.gross_rate == Decimal("225")
    assert rate.total_deductions == Decimal("11.25")
    assert rate.net_rate == Decimal("213.75")


@pytest.mark.unit
def test_net_never_exceeds_gross_when_deductions_exist() -> None:
    """The property that was violated. A deduction can only ever reduce the net."""

    rate = compute_carrier_rate(
        earnings=[Earning(title="Line Haul", amount=Decimal("1000"))],
        deductions=[
            Deduction(title="Advance", amount=Decimal("-250")),
            Deduction(title="Lumper", amount=Decimal("-75.50")),
        ],
    )

    assert rate.total_deductions == Decimal("325.50")
    assert rate.net_rate == Decimal("674.50")
    assert rate.net_rate < rate.gross_rate
