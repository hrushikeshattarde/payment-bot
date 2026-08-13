"""A date already gone may not be written as a date still to come.

Live regression, load 302866. The draft read "Payment is scheduled for Friday, August 8,
2026" and was written on August 13 — the day it names had passed five days earlier. Load
303355 in the same period: "scheduled for payment on Friday, August 7, 2026", six days late.

Nothing else could see it, and for a reason worth stating plainly: no part of this system
knew what day it was. `ToolContext` carried clients, settings and a ledger, and not a
calendar. Grounding compares the draft's dates to the ledger and both dates traced cleanly;
`weekday_consistency` compares a weekday to its date and holds no opinion about which side
of today either falls on. The tense is the one part of a payment sentence that is a claim
about *now*, and now was the one fact unavailable.

The direction is deliberately one-way. A past date described as upcoming is a promise of
money that is not coming; a future date described in the past tense is usually just a
scheduling decision already taken ("payment was scheduled for the 21st"), so it is left
alone. A check that fires on a correct reply costs more than the one it catches.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from payment_bot.gate import PreSendGate
from payment_bot.grounding import GroundingLedger, find_tense_mismatches
from payment_bot.models import InboundEmail
from payment_bot.sample_data import sample_transport_pro_client
from payment_bot.tools.base import ToolContext
from payment_bot.tools.submit import SubmitDraftOutput

#: The day the live drafts below were written.
TODAY = date(2026, 8, 13)


def _gate_check(reply_body: str, *, today: date = TODAY) -> tuple[bool, str]:
    """Run the full gate over ``reply_body`` and return the tense check's outcome."""

    ledger = GroundingLedger()
    # Ground every date the drafts below name, so *grounding* cannot be what fails here.
    for value in (date(2026, 8, 8), date(2026, 8, 7), date(2026, 7, 9), date(2026, 8, 20)):
        ledger.record_date(value, "cgt_get_load_status", load_id="302866")
    ledger.record_amount(Decimal("6500.00"), "cgt_get_load_status")
    ctx = ToolContext(
        tp=sample_transport_pro_client(),
        ledger=ledger,
        correlation_id="tense-test",
        today=today,
    )
    result = PreSendGate(allow_factoring=True).evaluate(
        draft=SubmitDraftOutput(
            reply_body=reply_body,
            to="paymentstatus@factoring.saintjohnfactoring.com",
            load_ids=[],
            citations=[],
        ),
        email=InboundEmail(
            message_id="<tense-test>",
            thread_id="t-tense",
            from_email="paymentstatus@factoring.saintjohnfactoring.com",
            subject="Payment Status",
            body="Please provide payment status.",
        ),
        ctx=ctx,
        expected_load_ids=None,
    )
    check = next(c for c in result.checks if c.name == "tense_consistency")
    return check.passed, check.detail


# --- the live drafts --------------------------------------------------------
def test_the_load_302866_draft_is_blocked() -> None:
    """The exact sentence that reached the gate. August 8 was five days gone."""

    passed, detail = _gate_check(
        "Payment is scheduled for Friday, August 8, 2026 on load 302866. We have $6,500.00 "
        "payable on this load."
    )
    assert passed is False
    assert "2026-08-08" in detail
    assert "5 day(s) ago" in detail


def test_the_load_303355_draft_is_blocked() -> None:
    """The other shape: the promise carried by a participle, with no "is" in front of it."""

    passed, detail = _gate_check(
        "We have $6,500.00 payable on load 303355, scheduled for payment on Friday, "
        "August 7, 2026."
    )
    assert passed is False
    assert "2026-08-07" in detail


def test_the_same_draft_in_the_past_tense_passes() -> None:
    """The fix has to be sayable, or the check just blocks every draft about a late load."""

    passed, detail = _gate_check(
        "Load 302866 was scheduled for payment on Friday, August 7, 2026 and is not showing "
        "as paid yet. We have $6,500.00 payable on it and someone will follow up."
    )
    assert passed is True, detail


def test_the_draft_was_correct_on_the_day_it_named() -> None:
    """Same words, judged from before the date: nothing wrong with it then."""

    passed, _ = _gate_check(
        "Payment is scheduled for Friday, August 8, 2026 on load 302866.",
        today=date(2026, 8, 1),
    )
    assert passed is True


def test_a_grounded_correctly_named_date_does_not_excuse_the_tense() -> None:
    """The point of the check: the two checks either side of it pass on this draft."""

    ledger = GroundingLedger()
    ledger.record_date(date(2026, 8, 8), "cgt_get_load_status", load_id="302866")
    ctx = ToolContext(
        tp=sample_transport_pro_client(),
        ledger=ledger,
        correlation_id="tense-test",
        today=TODAY,
    )
    result = PreSendGate(allow_factoring=True).evaluate(
        # Saturday IS the weekday of 2026-08-08 — the weekday check has nothing to say here.
        draft=SubmitDraftOutput(
            reply_body="Payment is scheduled for Saturday, August 8, 2026.",
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
    assert next(c for c in result.checks if c.name == "weekday_consistency").passed is True
    assert next(c for c in result.checks if c.name == "tense_consistency").passed is False
    assert result.allowed is False


# --- the extractor ----------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "payment is scheduled for August 8, 2026",
        "payment is currently scheduled for August 8, 2026",
        "payment is still scheduled for Saturday, August 8, 2026",
        "payment will be issued on August 8, 2026",
        "we will pay this on August 8, 2026",
        "the check goes out on August 8, 2026",
        "funds are expected on August 8, 2026",
        "this is due on August 8, 2026",
        "scheduled for payment on August 8, 2026",
        "payment is set for 2026-08-08",  # ISO, same claim
    ],
)
def test_a_promise_about_a_passed_date_is_caught(text: str) -> None:
    found = find_tense_mismatches(text, TODAY)
    assert len(found) == 1, text
    assert found[0].value == date(2026, 8, 8)
    assert found[0].days_past == 5


@pytest.mark.parametrize(
    "text",
    [
        "payment was scheduled for August 8, 2026",
        "payment had been scheduled for August 8, 2026",
        "this was paid on August 8, 2026",
        "the load delivered on August 8, 2026",
        "we received your invoice on August 8, 2026",
        "your invoice was billed on August 8, 2026",
        "the payment went out on August 8, 2026",
        "on August 8, 2026 the load was still open",  # no cue before the date at all
        "payment is scheduled for August 20, 2026",  # still ahead of today
        "payment is scheduled for August 13, 2026",  # today itself is not past
        "",
    ],
)
def test_correct_or_unclaimed_tenses_are_left_alone(text: str) -> None:
    assert find_tense_mismatches(text, TODAY) == []


def test_a_past_clause_does_not_shelter_a_future_one_beside_it() -> None:
    """Both tenses in one sentence. The "and" is where one clause's verb stops applying."""

    found = find_tense_mismatches(
        "Your invoice was received on July 9, 2026 and payment is scheduled for "
        "August 8, 2026.",
        TODAY,
    )
    assert [m.value for m in found] == [date(2026, 8, 8)]


def test_several_late_promises_are_all_reported() -> None:
    found = find_tense_mismatches(
        "Load 302866 is scheduled for August 8, 2026. Load 303355 is due on August 7, 2026.",
        TODAY,
    )
    assert {m.value for m in found} == {date(2026, 8, 8), date(2026, 8, 7)}


def test_the_same_late_promise_repeated_is_reported_once() -> None:
    """Keeps the blocked-gate detail readable when a reply restates a date."""

    found = find_tense_mismatches(
        "Payment is scheduled for August 8, 2026. To confirm, it is due on August 8, 2026.",
        TODAY,
    )
    assert len(found) == 1


def test_the_reported_phrase_is_the_wording_to_fix() -> None:
    """The detail line has to point at the words, not just the date."""

    found = find_tense_mismatches("Payment is scheduled for August 8, 2026.", TODAY)
    assert found[0].phrase.lower() == "is scheduled"
