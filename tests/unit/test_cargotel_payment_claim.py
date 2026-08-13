"""A 6-digit load's reply may not say whether it was paid, in either direction.

Live regression, load 298891. Partners Funding asked when Vio Line's invoice would pay and
the draft answered: "This load was scheduled for payment on Wednesday, August 12, 2026, but
is not yet showing as paid." Tense correct, weekday correct, every figure traced to
`cgt_get_load_status`, and all thirteen gate checks green.

CargoTel had reported no payment state at all, because it has none to report. `BillingState`
has five members and none of them is paid; `CgtLoadStatusOutput` carries no check number, no
payment date and no method — that detail lives on the Accounting tab, which is not wired. So
a passed pay date is not evidence of payment and its absence is not evidence against. The
system does not say.

The clause came from the skill prompt, whose tense rule ended "say it is not showing as paid
yet" — written to stop the model claiming payment, and instructing an equally ungrounded
claim in the opposite direction. That is the argument for a gate check rather than a prompt
fix alone: the prompt was the thing that was wrong.

Grounding cannot see it and is not meant to. It compares amounts and dates; this is status
prose, checked by nothing. Same shape as the weekday and tense checks one step further out —
those police the words beside a date, this one polices a claim with no date in it at all.

The negative direction is what earns the code. Claiming payment invites a "no you didn't";
claiming NON-payment to a factor chasing money invites a duplicate-payment request or a
dispute, and reads as authoritative because we are the payer.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from payment_bot.gate import PreSendGate
from payment_bot.gate.presend import _cargotel_payment_claims
from payment_bot.grounding import GroundingLedger
from payment_bot.models import InboundEmail
from payment_bot.sample_data import sample_transport_pro_client
from payment_bot.tools.base import ToolContext
from payment_bot.tools.submit import SubmitDraftOutput

CGT_LOAD = "298891"  # 6-digit -> CargoTel
TP_LOAD = "2462934"  # 7-digit -> Transport Pro

#: The exact sentence that reached Gmail Drafts on 2026-08-13.
LIVE_DRAFT = (
    "We have $2,100.00 payable on load 298891 for Vio Lines. This load was scheduled for "
    "payment on Wednesday, August 12, 2026, but is not yet showing as paid."
)

#: The same facts without the claim the system cannot support.
CORRECTED_DRAFT = (
    "We have $2,100.00 payable on load 298891 for Vio Lines. It was scheduled for payment "
    "on Wednesday, August 12, 2026, and someone will confirm where it stands."
)


def _check(reply_body: str, load_ids: list[str]) -> tuple[bool, str]:
    """Run the payment-claim check directly, with no clients to wire."""

    result = PreSendGate(allow_factoring=True)._check_cargotel_payment_claim(
        SubmitDraftOutput(reply_body=reply_body, to="a@b.com", load_ids=load_ids, citations=[])
    )
    return result.passed, result.detail


# --- the live draft ---------------------------------------------------------
def test_the_load_298891_draft_is_blocked() -> None:
    passed, detail = _check(LIVE_DRAFT, [CGT_LOAD])

    assert passed is False
    assert "paid/unpaid" in detail
    assert CGT_LOAD in detail


def test_the_corrected_wording_passes() -> None:
    """The fix has to be sayable, or the check just blocks every late-load reply."""

    passed, detail = _check(CORRECTED_DRAFT, [CGT_LOAD])
    assert passed is True, detail


def test_the_same_sentence_is_fine_on_a_transport_pro_load() -> None:
    """The asymmetry is the whole point, and it is a property of the systems.

    Transport Pro earning lines carry payment_status, actual_payment_date and check_number,
    so "not showing as paid" is a reading there. Scoping this check to 6-digit loads is what
    keeps a true statement sayable on the path that can support it.
    """

    passed, _ = _check(LIVE_DRAFT.replace(CGT_LOAD, TP_LOAD), [TP_LOAD])
    assert passed is True


def test_the_other_checks_pass_on_the_live_draft() -> None:
    """The point of adding it: everything else was already satisfied by that sentence."""

    ledger = GroundingLedger()
    ledger.record_amount(Decimal("2100.00"), "cgt_get_load_status", load_id=CGT_LOAD)
    ledger.record_date(date(2026, 8, 12), "cgt_get_load_status", load_id=CGT_LOAD)
    ctx = ToolContext(
        tp=sample_transport_pro_client(),
        ledger=ledger,
        correlation_id="payment-claim-test",
        today=date(2026, 8, 13),
    )
    draft = SubmitDraftOutput(
        reply_body=LIVE_DRAFT, to="a@b.com", load_ids=[], citations=[]
    )
    gate = PreSendGate(allow_factoring=True)
    result = gate.evaluate(
        draft=draft,
        email=InboundEmail(
            message_id="<m>", thread_id="t", from_email="a@b.com", subject="s", body="b"
        ),
        ctx=ctx,
        expected_load_ids=None,
    )
    for name in ("grounding", "weekday_consistency", "tense_consistency"):
        check = next(c for c in result.checks if c.name == name)
        assert check.passed is True, f"{name} unexpectedly failed: {check.detail}"

    # And the new one is wired into evaluate() at all.
    assert any(c.name == "cargotel_payment_claim" for c in result.checks)


# --- the matcher ------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "but is not yet showing as paid",
        "this load has been paid",
        "the load is unpaid at this time",
        "it was not paid on the scheduled date",
        "the check was mailed last week",
        "payment went out on Wednesday",
        "funds were released on the 12th",
        "the remittance was issued Friday",
    ],
)
def test_every_way_of_characterising_payment_is_caught(text: str) -> None:
    assert _cargotel_payment_claims(text), text


@pytest.mark.parametrize(
    "text",
    [
        # Every line here is required vocabulary in a real CargoTel reply. A check that
        # fires on one of these is worse than the bug it was added for.
        "We have $2,100.00 payable on load 298891 for Vio Line Inc.",
        "It was scheduled for payment on Wednesday, August 12, 2026.",
        "The payment terms on this load are Check Net 30.",
        "Please send the BOL 05 to freightpay@circledelivers.com.",
        "We have $2,000 payable on load 296006, awaiting your invoice.",
        "Your invoice and BOL are in our system and we're processing the load for payment.",
        "This load is under review and someone will follow up.",
        "The amount is $2,000.00 and payment is expected on Thursday, August 6, 2026.",
        # Future tense claims nothing about what has happened.
        "We'll confirm when payment goes out.",
        "",
    ],
)
def test_the_vocabulary_a_real_reply_needs_is_left_alone(text: str) -> None:
    assert _cargotel_payment_claims(text) == []


def test_payable_is_not_paid() -> None:
    """The word the whole check rests on not catching — it appears in nearly every reply."""

    assert _cargotel_payment_claims("we have $2,000 payable on this load") == []
    assert _cargotel_payment_claims("we have $2,000 paid on this load") == ["paid/unpaid"]


def test_a_draft_naming_no_six_digit_load_is_not_policed() -> None:
    """Code-authored replies (the bulk portal draft) name no load and must not be caught."""

    passed, detail = _check("You can check all of these here: https://portal.example", [])
    assert passed is True
    assert "no 6-digit load" in detail
