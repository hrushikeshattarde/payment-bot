"""Unit tests for the pre-send gate (§5) — one test per failure mode plus the happy path."""

from __future__ import annotations

import pytest

from payment_bot.gate import PreSendGate
from payment_bot.models import EmailAttachment, InboundEmail
from payment_bot.tools.base import ToolContext
from payment_bot.tools.submit import SubmitDraftOutput

# A well-grounded, authorized draft for load 2462934 (values match grounded_ctx).
_GOOD_BODY = (
    "Load 2462934 is BILLED. Both earning lines are Pending, totaling $4,650 "
    "($4,500 Brokerage Line Haul + $150 Truck Order Not Used). "
    "Scheduled payment date: Thursday, August 20, 2026."
)


def _draft(body: str = _GOOD_BODY, load_ids: list[str] | None = None) -> SubmitDraftOutput:
    return SubmitDraftOutput(
        reply_body=body,
        to="billing@ideaexpedited.com",
        load_ids=load_ids if load_ids is not None else ["2462934"],
        citations=[],
    )


def _checks(result: object) -> dict[str, bool]:
    return {c.name: c.passed for c in result.checks}  # type: ignore[attr-defined]


@pytest.mark.unit
def test_happy_path_allows_send(grounded_ctx: ToolContext, sample_email: InboundEmail) -> None:
    result = PreSendGate().evaluate(draft=_draft(), email=sample_email, ctx=grounded_ctx)
    assert result.allowed, result.reasons
    assert all(_checks(result).values())


@pytest.mark.unit
def test_unauthorized_sender_is_blocked(grounded_ctx: ToolContext) -> None:
    stranger = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="scammer@gmail.com",
        subject="Payment status for 2462934",
        body="status?",
    )
    result = PreSendGate().evaluate(draft=_draft(), email=stranger, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["authorization"] is False


@pytest.mark.unit
def test_bank_change_is_blocked(grounded_ctx: ToolContext) -> None:
    fraud = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="billing@ideaexpedited.com",
        subject="Update banking information",
        body="Please update our bank account number and routing number for load 2462934.",
    )
    result = PreSendGate().evaluate(draft=_draft(), email=fraud, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["sensitive_change"] is False


@pytest.mark.unit
def test_noa_attachment_is_blocked(grounded_ctx: ToolContext) -> None:
    with_noa = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="billing@ideaexpedited.com",
        subject="Please add our factoring NOA",
        body="Attached is our notice of assignment to set up factoring.",
        attachments=[EmailAttachment(filename="ACME_NOA.pdf", mime_type="application/pdf")],
    )
    result = PreSendGate().evaluate(draft=_draft(), email=with_noa, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["sensitive_change"] is False


@pytest.mark.unit
def test_ungrounded_amount_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    body = _GOOD_BODY + " An extra fee of $9,999 applies."
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["grounding"] is False
    assert any("9999" in r for r in result.reasons)


@pytest.mark.unit
def test_ungrounded_date_is_blocked(grounded_ctx: ToolContext, sample_email: InboundEmail) -> None:
    body = "Load 2462934 will be paid on September 1, 2026."
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["grounding"] is False
    assert any("2026-09-01" in r for r in result.reasons)


@pytest.mark.unit
def test_invalid_length_load_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    result = PreSendGate().evaluate(
        draft=_draft(body="Load 12345 status.", load_ids=["12345"]),
        email=sample_email,
        ctx=grounded_ctx,
    )
    assert not result.allowed
    assert _checks(result)["length_routing"] is False


@pytest.mark.unit
def test_bulk_over_threshold_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    many = [f"246293{n}" for n in range(6)]  # 6 valid 7-digit ids > default threshold 5
    result = PreSendGate().evaluate(
        draft=_draft(body="Multiple loads.", load_ids=many),
        email=sample_email,
        ctx=grounded_ctx,
    )
    assert not result.allowed
    assert _checks(result)["bulk"] is False


@pytest.mark.unit
def test_factoring_blocked_by_default_but_allowed_by_policy(
    grounded_ctx: ToolContext, tp_client: object
) -> None:
    # Register a factoring sender against the load, then send from the factor.
    from payment_bot.models import AuthorizationContext
    from payment_bot.sample_data import build_load_2462934_fixture

    fixture = build_load_2462934_fixture()
    factored = fixture.__class__(
        load=fixture.load,
        dispatch=fixture.dispatch,
        settlement=fixture.settlement,
        files=fixture.files,
        authorization=AuthorizationContext(
            carrier_company="Idea Expedited, Inc",
            authorized_emails=(),
            factoring_company="England Carrier Services",
            factoring_emails=("ar@englandcarrier.com",),
        ),
    )
    tp_client.add(factored)  # type: ignore[attr-defined]
    factor_email = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="ar@englandcarrier.com",
        subject="Payment status 2462934",
        body="status?",
    )

    blocked = PreSendGate(allow_factoring=False).evaluate(
        draft=_draft(), email=factor_email, ctx=grounded_ctx
    )
    assert not blocked.allowed
    assert _checks(blocked)["authorization"] is False

    allowed = PreSendGate(allow_factoring=True).evaluate(
        draft=_draft(), email=factor_email, ctx=grounded_ctx
    )
    assert allowed.allowed, allowed.reasons
