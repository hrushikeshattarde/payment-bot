"""The bot reckons "today" in Eastern, not in the Lambda's UTC."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from payment_bot.config import Settings
from payment_bot.pipeline import _today_in

pytestmark = pytest.mark.unit


def test_the_zone_is_configurable_and_defaults_to_eastern() -> None:
    """Every date in this domain is Eastern: pay runs, settlement dates, carrier questions."""

    assert Settings().timezone == "America/New_York"


def test_eastern_evening_is_still_today_here_and_tomorrow_in_utc() -> None:
    """The bug, stated as arithmetic.

    `date.today()` is UTC in Lambda. At 21:00 Eastern the UTC date has already rolled, so the
    bot believed it was tomorrow -- `_check_tense_consistency` reads a payment dated today as
    already past, and `compute_scheduled_pay_date` walks the Mon/Thu rule from the wrong day.
    At the :00/:15/:30/:45 cadence roughly a sixth of runs land in that window.
    """

    evening = dt.datetime(2026, 8, 26, 21, 0, tzinfo=ZoneInfo("America/New_York"))

    assert evening.date() == dt.date(2026, 8, 26)
    assert evening.astimezone(dt.UTC).date() == dt.date(2026, 8, 27)


def test_a_real_zone_resolves_rather_than_falling_back() -> None:
    """tzdata is a dependency for this reason.

    ZoneInfo reads the SYSTEM tz database. Windows has none, so every zone raised and fell
    back to the UTC system date silently, while Amazon Linux resolved correctly -- a bug that
    only shows up as a platform difference.
    """

    assert _today_in("America/New_York") == dt.datetime.now(
        ZoneInfo("America/New_York")
    ).date()
    assert _today_in("Pacific/Auckland") == dt.datetime.now(
        ZoneInfo("Pacific/Auckland")
    ).date()


def test_an_unusable_zone_falls_back_instead_of_taking_the_inbox_down() -> None:
    """A misspelt zone is a config slip; refusing to start over one is the worse failure."""

    assert _today_in("Not/AZone") == dt.date.today()
    assert _today_in("") == dt.date.today()
