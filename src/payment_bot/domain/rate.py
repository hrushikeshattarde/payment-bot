"""Carrier-rate computation (PRD §4.1.1 / §3.2).

**Carrier rate = sum of all ``earnings[].amount``.** If deductions exist, each is
reported individually with its reason, and the net = gross - sum(deductions). All
arithmetic is in :class:`~decimal.Decimal` so totals are exact and auditable; the model
never sums money on its own.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from payment_bot.models.transport_pro import Deduction, Earning

_ZERO = Decimal("0")


class EarningLine(BaseModel):
    """One earning line echoed into the reply breakdown."""

    model_config = ConfigDict(frozen=True)

    title: str
    amount: Decimal


class DeductionLine(BaseModel):
    """One deduction line — reason is always populated (falls back to the title)."""

    model_config = ConfigDict(frozen=True)

    title: str
    amount: Decimal
    reason: str


class CarrierRate(BaseModel):
    """Computed rate breakdown for a load."""

    model_config = ConfigDict(frozen=True)

    gross_rate: Decimal
    total_deductions: Decimal
    net_rate: Decimal
    earnings_breakdown: list[EarningLine]
    deductions: list[DeductionLine]


def compute_carrier_rate(
    earnings: list[Earning],
    deductions: list[Deduction] | None = None,
) -> CarrierRate:
    """Compute gross, deductions, and net carrier rate for a load.

    Args:
        earnings: The load's earning lines (carrier rate is their sum).
        deductions: Deduction/adjustment lines, if any.

    Returns:
        A :class:`CarrierRate` with a per-line breakdown ready to echo into the reply.
    """

    deduction_lines = deductions or []

    gross = sum((e.amount for e in earnings), _ZERO)
    # Transport Pro returns deductions as NEGATIVE amounts ("Quick Pay Brokerage": -11.25),
    # so `gross - total` double-negated and *added* the deduction: load 2496737 came back as
    # a net of 236.25 against a gross of 225, overstating what the carrier was owed by twice
    # the deduction. Grounding cannot catch that — the figure genuinely came from a tool.
    #
    # Summing magnitudes makes the result correct whichever sign the API uses, so a later
    # switch to positive deductions cannot silently flip it back.
    total_deductions = sum((abs(d.amount) for d in deduction_lines), _ZERO)

    return CarrierRate(
        gross_rate=gross,
        total_deductions=total_deductions,
        net_rate=gross - total_deductions,
        earnings_breakdown=[EarningLine(title=e.title, amount=e.amount) for e in earnings],
        deductions=[
            DeductionLine(title=d.title, amount=d.amount, reason=d.reason or d.title)
            for d in deduction_lines
        ],
    )
