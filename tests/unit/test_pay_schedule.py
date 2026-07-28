"""Unit tests for the Monday/Thursday scheduled-pay-date rule (§4.1.1).

The calendar is anchored on a single known fact from the PRD worked example:
2026-08-19 is a Wednesday. All parametrised dates below derive from that week.
"""

from __future__ import annotations

from datetime import date

import pytest

from payment_bot.domain import compute_scheduled_pay_date
from payment_bot.models import PayBasis


@pytest.mark.unit
def test_calendar_anchor_is_correct() -> None:
    # Guards the whole test module: if this assumption breaks, everything below is moot.
    assert date(2026, 8, 19).weekday() == 2  # Wednesday


@pytest.mark.unit
@pytest.mark.parametrize(
    ("estimated", "expected_scheduled", "expected_weekday", "expected_rule"),
    [
        (date(2026, 8, 17), date(2026, 8, 17), "Monday", "Mon → Mon (same day)"),
        (date(2026, 8, 18), date(2026, 8, 20), "Tuesday", "Tue → Thu (same week)"),
        (date(2026, 8, 19), date(2026, 8, 20), "Wednesday", "Wed → Thu (same week)"),
        (date(2026, 8, 20), date(2026, 8, 20), "Thursday", "Thu → Thu (same day)"),
        (date(2026, 8, 21), date(2026, 8, 24), "Friday", "Fri → Mon (following week)"),
        (date(2026, 8, 22), date(2026, 8, 24), "Saturday", "Sat → Mon (following week)"),
        (date(2026, 8, 23), date(2026, 8, 24), "Sunday", "Sun → Mon (following week)"),
    ],
)
def test_estimated_date_maps_to_next_payment_day(
    estimated: date,
    expected_scheduled: date,
    expected_weekday: str,
    expected_rule: str,
) -> None:
    result = compute_scheduled_pay_date(estimated_payment_date=estimated)

    assert result.scheduled_pay_date == expected_scheduled
    assert result.basis is PayBasis.ESTIMATED
    assert result.estimated_weekday == expected_weekday
    assert result.rule_applied == expected_rule
    # Every resolved date must itself be a Monday (0) or Thursday (3).
    assert result.scheduled_pay_date.weekday() in (0, 3)


@pytest.mark.unit
def test_prd_worked_example_load_2462934() -> None:
    # §7.4: estimated 2026-08-19 (Wed) → Thursday 2026-08-20.
    result = compute_scheduled_pay_date(estimated_payment_date=date(2026, 8, 19))
    assert result.scheduled_pay_date == date(2026, 8, 20)


@pytest.mark.unit
def test_actual_date_is_reported_directly_and_wins_over_estimated() -> None:
    result = compute_scheduled_pay_date(
        estimated_payment_date=date(2026, 8, 19),  # would compute to 08-20 …
        actual_payment_date=date(2026, 8, 24),  # … but actual wins, no computation
    )
    assert result.basis is PayBasis.ACTUAL
    assert result.scheduled_pay_date == date(2026, 8, 24)
    assert result.rule_applied == "actual date reported directly"


@pytest.mark.unit
def test_missing_both_dates_raises_and_is_never_guessed() -> None:
    with pytest.raises(ValueError, match="cannot schedule"):
        compute_scheduled_pay_date(estimated_payment_date=None, actual_payment_date=None)
