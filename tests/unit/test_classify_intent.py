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
