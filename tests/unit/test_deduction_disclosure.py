"""A 6-digit load's reply neither claims it is clean of deductions nor ignores the question.

Live regression, load 317967 (SHADOW FREIGHT LLC / invoice INVCTK1878, 2026-08-14). The
AR contact at Saint John Capital — the factor of record, authorized FACTORING — asked three
things:

    "Can you please state the rate for this load and if there were any fuel advances, no
     claims and no deductions. Also that SJC is set up as factor for this carrier"

The draft answered the rate ($12,000.00) and the factor question, and said nothing whatsoever
about advances, claims or deductions. It *was* blocked — but by ``change_acknowledgment``, on
"set up as the factor" tripping ``_NOA_ACTION_RE``, an unrelated wording collision. Reword
that one clause to "the factor on file" and the draft ships with the money question silently
dropped, green on all fifteen other checks.

Silence is not neutral. A factor asks because it is about to advance funds against the
invoice, so an unanswered question reads as "nothing to report". ``_check_coverage`` cannot
see it — that check counts load ids, not questions.

The opposite failure is worse, so it is checked first and needs no question to have been
asked: on this path ``amount`` is a single payable and the tool returns no line items, which
is why the skill forbids breaking it into a rate plus charges. "No deductions" is therefore
ungrounded *by construction*, the same footing as the paid/unpaid ban — and grounding cannot
catch it, because there is no figure in the sentence to trace.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from payment_bot.gate import PreSendGate
from payment_bot.gate.presend import _deduction_questions
from payment_bot.grounding import GroundingLedger
from payment_bot.models import InboundEmail
from payment_bot.sample_data import sample_transport_pro_client
from payment_bot.tools.base import ToolContext
from payment_bot.tools.submit import SubmitDraftOutput

pytestmark = pytest.mark.unit

CGT_LOAD = "317967"  # 6-digit -> CargoTel
TP_LOAD = "2462934"  # 7-digit -> Transport Pro

#: What the sender actually wrote.
ASKED = (
    "Can you please state the rate for this load and if there were any fuel advances, no "
    "claims and no deductions. Also that SJC is set up as factor for this carrier\n\n"
    # Signature and disclaimer kept in shape, without the real contact details: the point of
    # including them at all is that neither is stripped the way quoted history is, so ordinary
    # words in them must not read as a question. See the must-not-fire cases below.
    "Best Regards,\nAR Department\nSaint John Capital, LLC"
)

#: The draft that reached the gate, with the factor clause reworded so it clears
#: change_acknowledgment — i.e. exactly what would have shipped once that block was fixed.
LIVE_DRAFT = (
    "We have $12,000.00 payable on load 317967 for SHADOW FREIGHT LLC. The load is awaiting "
    "the carrier invoice — please send it to freightpay@circledelivers.com so we can process "
    "payment. SJC is the factor on file for this carrier."
)

#: Responsive and true: names the topic, says this system does not carry it, hands it over.
CORRECTED_DRAFT = (
    "We have $12,000.00 payable on load 317967 for Shadow Freight LLC, and the load is "
    "awaiting the carrier invoice — please send it to freightpay@circledelivers.com so we "
    "can process payment.\n\n"
    "On fuel advances, claims and deductions: we can't confirm those from the payment "
    "record, so someone will check and follow up with you separately."
)


def _email(body: str = ASKED, subject: str = "Rate Verification: SHADOW FREIGHT LLC") -> InboundEmail:
    return InboundEmail(
        message_id="<m>",
        thread_id="t",
        from_email="ar@saintjohncapital.example",
        from_name="AR Department",
        subject=subject,
        body=body,
    )


def _check(
    reply_body: str, load_ids: list[str] | None = None, email: InboundEmail | None = None
) -> tuple[bool, str]:
    result = PreSendGate(allow_factoring=True)._check_deduction_disclosure(
        SubmitDraftOutput(
            reply_body=reply_body,
            to="ar@saintjohncapital.example",
            load_ids=load_ids if load_ids is not None else [CGT_LOAD],
            citations=[],
        ),
        email or _email(),
    )
    return result.passed, result.detail


# --- the live draft ---------------------------------------------------------
def test_the_unanswered_question_is_blocked() -> None:
    passed, detail = _check(LIVE_DRAFT)

    assert passed is False
    assert "does not mention them at all" in detail
    assert CGT_LOAD in detail


def test_the_corrected_wording_passes() -> None:
    """The honest shape has to be sayable or the check blocks every factor reply."""

    passed, detail = _check(CORRECTED_DRAFT)
    assert passed is True, detail


def test_answering_the_question_affirmatively_is_blocked() -> None:
    """The worse failure: a definite answer this path cannot support.

    Reading the sender's own "no claims and no deductions" back to them is the tempting
    reply, and it is a payment assurance sourced from the question rather than the load.
    """

    passed, detail = _check(
        "We have $12,000.00 payable on load 317967. There were no fuel advances, no claims "
        "and no deductions."
    )
    assert passed is False
    assert "cannot support" in detail


def test_a_clean_claim_is_blocked_even_when_nobody_asked() -> None:
    """Arm one does not depend on the email — volunteering it is ungrounded either way."""

    passed, detail = _check(
        "We have $12,000.00 payable on load 317967, payable in full with no deductions.",
        email=_email(body="What is the status of load 317967?", subject="status"),
    )
    assert passed is False
    assert "cannot support" in detail


def test_no_question_and_no_claim_passes() -> None:
    passed, detail = _check(
        "We have $12,000.00 payable on load 317967 for Shadow Freight LLC.",
        email=_email(body="What is the status of load 317967?", subject="status"),
    )
    assert passed is True, detail


def test_a_transport_pro_load_is_not_policed() -> None:
    """TP earning lines carry deductions as real signed figures, so there it is answerable."""

    passed, _ = _check(
        "We have $4,650.00 payable on load 2462934 with no deductions.", [TP_LOAD]
    )
    assert passed is True


def test_the_question_is_read_from_this_message_only() -> None:
    """A deduction word in quoted history is not this sender asking.

    Same rule as the sensitive-change scan: scanning quoted text means one mention keeps
    re-firing for as long as the thread lives.
    """

    quoted = (
        "Any update on 317967?\n\n"
        "On Wed, Aug 13, 2026 at 9:02 AM Circle Delivers wrote:\n"
        "> we can't confirm advances or deductions from the payment record\n"
    )
    passed, detail = _check(LIVE_DRAFT, email=_email(body=quoted, subject="Re: 317967"))
    assert passed is True, detail


# --- the matcher ------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "if there were any fuel advances, no claims and no deductions",
        "please confirm there are no deductions",
        "was anything deducted from this load?",
        "any chargebacks on this one?",
        "did you charge back anything",
        "was this a short pay?",
        "confirm no claims or deductions",
        "we need the rate net of any fuel advance",
        "was there a cash advance on this load",
    ],
)
def test_every_way_of_asking_is_caught(text: str) -> None:
    assert _deduction_questions(text), text


@pytest.mark.parametrize(
    "text",
    [
        # The sign-off that put this lesson in _RATE_SIGNALS in the first place.
        "Thank you in Advance, ACDS TEAM",
        "Thanks in advance for your help",
        # A legal disclaimer is not stripped the way quoted history is, so ordinary words
        # appearing in one must not read as a question.
        "The sender does not accept liability for any errors or omissions in the contents "
        "of this message.",
        "This message contains confidential information and is intended only for the "
        "individual named.",
        "Can you please state the rate for this load?",
        "When will payment be made on invoice 00000938?",
        "Please confirm SJC is set up as factor for this carrier",
        "",
    ],
)
def test_ordinary_mail_is_not_read_as_a_deduction_question(text: str) -> None:
    assert _deduction_questions(text) == [], text


def test_thanks_in_advance_does_not_count_as_answering_it() -> None:
    """The dangerous direction: a lenient answer-detector would pass the silence through."""

    passed, detail = _check(f"{LIVE_DRAFT}\n\nThanks in advance,\nCircle Delivers Payments")
    assert passed is False, detail


# --- the reason this needed a gate check ------------------------------------
def test_the_draft_is_otherwise_green() -> None:
    """With the factor clause reworded, every other check passes on the live draft."""

    ledger = GroundingLedger()
    ledger.record_amount(Decimal("12000.00"), "cgt_get_load_status", load_id=CGT_LOAD)
    ctx = ToolContext(
        tp=sample_transport_pro_client(),
        ledger=ledger,
        correlation_id="deduction-disclosure-test",
        today=date(2026, 8, 14),
    )
    result = PreSendGate(allow_factoring=True).evaluate(
        draft=SubmitDraftOutput(
            reply_body=LIVE_DRAFT, to="a@b.com", load_ids=[], citations=[]
        ),
        email=_email(),
        ctx=ctx,
        expected_load_ids=None,
    )
    for name in ("grounding", "change_acknowledgment", "cargotel_payment_claim",
                 "noa_request", "placeholders", "tense_consistency"):
        check = next(c for c in result.checks if c.name == name)
        assert check.passed is True, f"{name} unexpectedly failed: {check.detail}"

    assert any(c.name == "deduction_disclosure" for c in result.checks)
