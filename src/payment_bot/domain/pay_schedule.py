"""Scheduled payment-date rule (PRD §4.1.1).

Carriers are paid **only on Mondays and Thursdays**. The API's
``estimated_payment_date`` (a calendar date, interpreted in EDT) maps to the next
applicable payment day per this table:

    Monday    → Monday    (same day)
    Tuesday   → Thursday  (same week)
    Wednesday → Thursday  (same week)
    Thursday  → Thursday  (same day)
    Friday    → Monday    (following week)
    Saturday  → Monday    (following week)
    Sunday    → Monday    (following week)

If ``actual_payment_date`` is present, it is reported directly and **no computation is
done**. A line with neither date cannot be scheduled and must be reported as
undetermined (the caller turns the raised error into that outcome) — never guessed.

⚠ Open item (§4.1.1): Monday/Thursday are assumed to pay **same-day** when the estimated
date already lands on a payment day. This is pending confirmation from the Payments
owner; if the rule changes to "roll to next payment day", only ``_WEEKDAY_RULE`` below
changes.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict

from payment_bot.models.enums import PayBasis

# weekday() → (offset_days_to_add, human-readable rule). Monday == 0 … Sunday == 6.
_WEEKDAY_RULE: dict[int, tuple[int, str]] = {
    0: (0, "Mon → Mon (same day)"),
    1: (2, "Tue → Thu (same week)"),
    2: (1, "Wed → Thu (same week)"),
    3: (0, "Thu → Thu (same day)"),
    4: (3, "Fri → Mon (following week)"),
    5: (2, "Sat → Mon (following week)"),
    6: (1, "Sun → Mon (following week)"),
}

_WEEKDAY_NAME: dict[int, str] = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}


class ScheduledPayDate(BaseModel):
    """Resolved customer-facing pay date and the rule that produced it."""

    model_config = ConfigDict(frozen=True)

    scheduled_pay_date: date
    basis: PayBasis
    estimated_weekday: str
    rule_applied: str


def compute_scheduled_pay_date(
    estimated_payment_date: date | None,
    actual_payment_date: date | None = None,
) -> ScheduledPayDate:
    """Resolve the date a carrier will actually be paid.

    Args:
        estimated_payment_date: The API's estimated pay date (EDT calendar date), or
            ``None`` if the line has none.
        actual_payment_date: If already paid, the actual date — reported as-is.

    Returns:
        A :class:`ScheduledPayDate`.

    Raises:
        ValueError: If neither date is available. The line is then reported as
            undetermined by the caller; a date is never invented.
    """

    if actual_payment_date is not None:
        return ScheduledPayDate(
            scheduled_pay_date=actual_payment_date,
            basis=PayBasis.ACTUAL,
            estimated_weekday=_WEEKDAY_NAME[actual_payment_date.weekday()],
            rule_applied="actual date reported directly",
        )

    if estimated_payment_date is None:
        raise ValueError("cannot schedule: no estimated_payment_date and no actual_payment_date")

    weekday = estimated_payment_date.weekday()
    offset, rule = _WEEKDAY_RULE[weekday]
    return ScheduledPayDate(
        scheduled_pay_date=estimated_payment_date + timedelta(days=offset),
        basis=PayBasis.ESTIMATED,
        estimated_weekday=_WEEKDAY_NAME[weekday],
        rule_applied=rule,
    )
