"""Unit tests for the classify_intent tool (§4.2)."""

from __future__ import annotations

import pytest

from payment_bot.models import Intent
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import ClassifyIntent, ClassifyIntentInput, ClassifyIntentOutput


def _run(ctx: ToolContext, **kw: str) -> ClassifyIntentOutput:
    out = ClassifyIntent().run(ClassifyIntentInput(**kw), ctx)
    assert isinstance(out, ClassifyIntentOutput)
    return out


@pytest.mark.unit
def test_rate_verification_subject(ctx: ToolContext) -> None:
    out = _run(ctx, email_subject="Rate Verification - Load 2462934", email_body="verify the rate")
    assert Intent.RATE_VERIFICATION in out.intents
    assert Intent.PAYMENT_STATUS not in out.intents


@pytest.mark.unit
def test_payment_status(ctx: ToolContext) -> None:
    out = _run(ctx, email_subject="Payment status", email_body="when will I be paid for 2462934?")
    assert out.intents == [Intent.PAYMENT_STATUS]
    assert out.confidence == pytest.approx(0.9)


@pytest.mark.unit
def test_both_intents_lowers_confidence(ctx: ToolContext) -> None:
    out = _run(ctx, email_body="verify the rate and tell me the payment status / pay date")
    assert Intent.PAYMENT_STATUS in out.intents
    assert Intent.RATE_VERIFICATION in out.intents
    assert out.confidence == pytest.approx(0.6)


@pytest.mark.unit
def test_unclear_is_uncertain(ctx: ToolContext) -> None:
    out = _run(ctx, email_body="Hello, quick question about my load.")
    assert out.intents == [Intent.UNCERTAIN]
    assert out.confidence == pytest.approx(0.3)


@pytest.mark.unit
def test_secondary_asks(ctx: ToolContext) -> None:
    out = _run(ctx, email_body="Please add our NOA and confirm the POD is on file.")
    assert "factoring_setup" in out.secondary_asks
    assert "paperwork_receipt" in out.secondary_asks


# --- Misreads found on live mail ---------------------------------------------
#: Verbatim from a carrier chasing payment on load 2496737. The only "rate" signal in it is
#: the word "Advance" inside the sign-off — which scored rate_verification at 0.9 confidence,
#: so the bot replied asking him to supply a rate he had never mentioned.
_SIGNOFF_BODY = (
    "Hello\nCan anybody update this for me please\n\n"
    "Thank you in Advance,\nACDS TEAM\n"
    "Build a team so strong, no one can point out the leader\n734-799-1488"
)


@pytest.mark.unit
def test_a_signoff_is_not_a_rate_request(ctx: ToolContext) -> None:
    out = _run(ctx, email_subject="Load 2496737 2nd request", email_body=_SIGNOFF_BODY)
    assert Intent.RATE_VERIFICATION not in out.intents
    assert out.intents == [Intent.PAYMENT_STATUS]


@pytest.mark.unit
def test_a_vague_ask_naming_a_load_reads_as_payment_status(ctx: ToolContext) -> None:
    """Carriers rarely write "payment status" — they write "any update on this?"."""

    out = _run(ctx, email_subject="Load 2496737", email_body="Any update on this one?")
    assert out.intents == [Intent.PAYMENT_STATUS]


@pytest.mark.unit
def test_a_vague_ask_with_no_load_stays_uncertain(ctx: ToolContext) -> None:
    """Defaulting needs something to act on; with no load there is nothing to look up."""

    out = _run(ctx, email_subject="Question", email_body="Can you help me with something?")
    assert out.intents == [Intent.UNCERTAIN]


@pytest.mark.unit
def test_explicit_rate_wording_routes_to_rate_verification_without_a_figure(
    ctx: ToolContext,
) -> None:
    """Asking us to verify a rate is a rate request even with no amount quoted.

    They want us to state ours. Requiring a figure here was an over-correction — the fix for
    the sign-off misread is narrower signals, not a stricter rule.
    """

    out = _run(
        ctx,
        email_subject="Rate confirmation for load 2496737",
        email_body="Please verify the rate on this load.",
    )
    assert Intent.RATE_VERIFICATION in out.intents


@pytest.mark.unit
def test_rate_verification_survives_when_an_amount_is_quoted(ctx: ToolContext) -> None:
    """The real thing must still route to rate_verification."""

    out = _run(
        ctx,
        email_subject="Rate verification - load 2496737",
        email_body="We show $225.00 on our rate confirmation. Please verify the rate.",
    )
    assert Intent.RATE_VERIFICATION in out.intents


#: Verbatim shape of a factoring onboarding packet (FirstLine Funding, load 2545383). Every
#: question is answerable, but none matched a signal phrase — "Rate is correct at $550?" is
#: not "confirm the rate" — so keyword_grounded came back False, and change evidence plus an
#: ungrounded ask escalates in every configuration. The email never got its answers.
_VERIFICATION_PACKET_BODY = (
    "We have a new carrier HUNT LOGISTICS coming on with us. We need to confirm the following:\n\n"
    "Did this carrier deliver load 2545383 without issue ?\n\n"
    "Rate is correct at $ 550 ?\n\n"
    "Any advances taken?\n\n"
    "Do you have any other factor on file for this carrier?\n\n"
    "Is the paperwork attached sufficient to pay?\n\n"
    "I have attached the NOA. Please confirm this has been updated in your system.\n"
)


@pytest.mark.unit
def test_a_verification_packet_grounds_its_questions(ctx: ToolContext) -> None:
    out = _run(
        ctx,
        email_subject="PLEASE CONFIRM - LOAD #2545383 HUNT LOGISTICS",
        email_body=_VERIFICATION_PACKET_BODY,
    )
    assert out.keyword_grounded is True
    assert Intent.RATE_VERIFICATION in out.intents
    assert "factoring_setup" in out.secondary_asks


@pytest.mark.unit
def test_question_form_signals_do_not_revive_the_signoff_misread(ctx: ToolContext) -> None:
    """"any advances" must not bring back what bare "advance" did — see _SIGNOFF_BODY."""

    out = _run(ctx, email_subject="Load 2496737 2nd request", email_body=_SIGNOFF_BODY)
    assert Intent.RATE_VERIFICATION not in out.intents
    assert out.keyword_grounded is False


@pytest.mark.unit
def test_quoted_history_does_not_decide_intent(ctx: ToolContext) -> None:
    """An older ask in the quoted thread must not override what was just written."""

    out = _run(
        ctx,
        email_subject="Re: load 2496737",
        email_body=(
            "Any update?\n\n"
            "On Mon, Jul 20, 2026 someone wrote:\n"
            "> Please verify the rate, we show $225.00 on our rate confirmation.\n"
        ),
    )
    assert out.intents == [Intent.PAYMENT_STATUS]
