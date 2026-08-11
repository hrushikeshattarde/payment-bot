"""A weekday named in a reply must be the real weekday of the date beside it.

Live regression, load 2481130. Two drafts in the same run reported the same pay date —
2026-08-11, a Tuesday — and disagreed about the weekday: 2492357 said "Tuesday", 2481130
said "Monday". Both loads were already paid, so `compute_scheduled_pay_date` took its
`actual` branch and returned "Tuesday" for both; the reply cited the tool for a word the
tool never produced.

Nothing else can see it. Grounding compares *dates*: "August 11, 2026" traced to a tool
result and passed, and the weekday adjective was compared against nothing at all, so the
draft cleared all eleven checks and was saved to Drafts as ready to review. A carrier reads
the weekday as the operative fact ("so it went out Monday"), which is what makes a wrong one
worse than an absent one.

The check is arithmetic against the date, so it holds however the weekday was arrived at —
invented, or faithfully copied from a field describing a different date (see
`test_pay_schedule.py` for the estimated-vs-scheduled trap that used to make the second case
reachable).
"""

from __future__ import annotations

from datetime import date

import pytest

from payment_bot.gate import PreSendGate
from payment_bot.grounding import GroundingLedger, find_weekday_mismatches
from payment_bot.models import InboundEmail
from payment_bot.sample_data import sample_transport_pro_client
from payment_bot.tools.base import ToolContext
from payment_bot.tools.submit import SubmitDraftOutput


def _gate_check(reply_body: str) -> tuple[bool, str]:
    """Run the full gate over ``reply_body`` and return the weekday check's outcome."""

    tp = sample_transport_pro_client()
    ledger = GroundingLedger()
    # Ground the date and amount so the *grounding* check cannot be what fails here.
    ledger.record_date(date(2026, 8, 11), "tp_get_load_summary", load_id="2462934")
    ctx = ToolContext(tp=tp, ledger=ledger, correlation_id="weekday-test")
    email = InboundEmail(
        message_id="<weekday-test>",
        thread_id="t-weekday",
        from_email="accounting.receivables@borderlandersinc.com",
        subject="Payment Status Request",
        body="Status on this load please.",
    )
    result = PreSendGate(allow_factoring=True).evaluate(
        draft=SubmitDraftOutput(
            reply_body=reply_body,
            to="accounting.receivables@borderlandersinc.com",
            load_ids=[],
            citations=[],
        ),
        email=email,
        ctx=ctx,
        expected_load_ids=None,
    )
    check = next(c for c in result.checks if c.name == "weekday_consistency")
    return check.passed, check.detail


# --- the live draft ---------------------------------------------------------
def test_the_load_2481130_draft_is_blocked() -> None:
    """The exact sentence that reached Drafts. 2026-08-11 is a Tuesday, not a Monday."""

    passed, detail = _gate_check(
        "Load 2481130 is billed and paid. The $850 brokerage line haul was paid via "
        "direct deposit on Monday, August 11, 2026."
    )
    assert passed is False
    assert "Monday 2026-08-11 is a Tuesday" in detail


def test_the_sibling_draft_from_the_same_run_still_passes() -> None:
    """2492357 named the same date correctly and must not be caught by the fix."""

    passed, _ = _gate_check(
        "Load 2492357 is billed and paid. Both the Detention Pay ($150) and Brokerage "
        "Line Haul ($650) were paid via direct deposit on Tuesday, August 11, 2026."
    )
    assert passed is True


def test_a_grounded_date_does_not_excuse_a_wrong_weekday() -> None:
    """The point of the check: grounding passes on this draft and the gate still blocks."""

    tp = sample_transport_pro_client()
    ledger = GroundingLedger()
    ledger.record_date(date(2026, 8, 11), "compute_scheduled_pay_date", load_id="2462934")
    ctx = ToolContext(tp=tp, ledger=ledger, correlation_id="weekday-test")
    result = PreSendGate(allow_factoring=True).evaluate(
        draft=SubmitDraftOutput(
            reply_body="Paid on Monday, August 11, 2026.",
            to="a@b.com",
            load_ids=[],
            citations=[],
        ),
        email=InboundEmail(
            message_id="<m>", thread_id="t", from_email="a@b.com", subject="s", body="b"
        ),
        ctx=ctx,
        expected_load_ids=None,
    )
    assert next(c for c in result.checks if c.name == "grounding").passed is True
    assert next(c for c in result.checks if c.name == "weekday_consistency").passed is False
    assert result.allowed is False


# --- the extractor ---------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "paid on Monday, August 11, 2026",
        "paid on Monday August 11, 2026",  # no comma after the weekday
        "paid on Mon, Aug 11, 2026",  # abbreviated
        "paid on Mon. Aug. 11, 2026",  # abbreviated with periods
        "paid on Monday, 2026-08-11",  # ISO after the weekday
        "scheduled for Monday, August 11th, 2026",  # ordinal suffix
    ],
)
def test_wrong_weekday_is_caught_in_every_form_a_reply_might_use(text: str) -> None:
    found = find_weekday_mismatches(text)
    assert len(found) == 1
    assert found[0].value == date(2026, 8, 11)
    assert found[0].correct == "Tuesday"


@pytest.mark.parametrize(
    "text",
    [
        "paid on Tuesday, August 11, 2026",
        "paid on Tue, Aug 11, 2026",
        "paid on Tuesday, 2026-08-11",
        "paid on August 11, 2026",  # no weekday claimed at all
        "we pay on Monday, and August work is billed later",  # not a date
        "payment runs Monday and Thursday each week",  # no date follows
        "",
    ],
)
def test_correct_or_absent_weekdays_are_left_alone(text: str) -> None:
    assert find_weekday_mismatches(text) == []


def test_every_weekday_name_is_recognised() -> None:
    """A gap in the name table would silently stop checking that weekday."""

    # 2026-08-11 is a Tuesday, so every other weekday name attached to it is a mismatch.
    for name in ("Monday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        found = find_weekday_mismatches(f"paid on {name}, August 11, 2026")
        assert len(found) == 1, f"{name} was not recognised as a weekday"
        assert found[0].correct == "Tuesday"
    assert find_weekday_mismatches("paid on Tuesday, August 11, 2026") == []


def test_several_wrong_dates_are_all_reported() -> None:
    """A multi-line reply must not stop at the first mismatch."""

    found = find_weekday_mismatches(
        "Detention was paid Monday, August 11, 2026 and line haul Sunday, August 6, 2026."
    )
    assert {(m.value, m.correct) for m in found} == {
        (date(2026, 8, 11), "Tuesday"),
        (date(2026, 8, 6), "Thursday"),
    }


def test_the_same_mismatch_repeated_is_reported_once() -> None:
    """Keeps the blocked-gate detail readable when a reply restates a date."""

    found = find_weekday_mismatches(
        "Paid Monday, August 11, 2026. To confirm: Monday, August 11, 2026."
    )
    assert len(found) == 1


def test_an_impossible_date_is_ignored_rather_than_raising() -> None:
    """February 30th has no weekday to be wrong about; the gate must not crash on it."""

    assert find_weekday_mismatches("paid on Monday, February 30, 2026") == []
