"""Integration tests for the §3.3 bulk portal reply.

A request naming more loads than ``bulk_threshold`` used to escalate with "use portal <url>"
so a human could paste the link. It now answers directly with a deterministic reply that
points at the self-service portal.

The property worth protecting is *why* that reply is safe to send unreviewed-by-a-model: it
discloses nothing. No load id, no amount, no date. So it passes the gate on the gate's own
terms rather than by exemption, and it never runs the agent at all.
"""

from __future__ import annotations

import pytest

from payment_bot.clients import (
    ApprovalAction,
    ApprovalDecision,
    MockGmailClient,
    MockSlackClient,
    ScriptedApprovalResolver,
)
from payment_bot.logging import InMemoryAuditSink
from payment_bot.models import InboundEmail
from payment_bot.pipeline import Outcome, PaymentBotPipeline
from payment_bot.sample_data import sample_transport_pro_client, scripted_payment_status_llm

#: Six 7-digit ids — one over the default threshold of five.
_MANY_LOADS = ["2462934", "2462935", "2462936", "2462937", "2462938", "2462939"]


def _bulk_email() -> InboundEmail:
    return InboundEmail(
        message_id="<bulk@carrier.test>",
        thread_id="t-bulk",
        from_email="billing@ideaexpedited.com",
        from_name="Idea Expedited Billing",
        subject="Payment status for several loads",
        body="Could you confirm payment status for " + ", ".join(_MANY_LOADS) + "? Thanks.",
    )


def _pipeline(gmail: MockGmailClient, slack: MockSlackClient, audit: InMemoryAuditSink):
    return PaymentBotPipeline(
        tp=sample_transport_pro_client(),
        gmail=gmail,
        slack=slack,
        llm=scripted_payment_status_llm(),
        approval_resolver=ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)),
        audit_sink=audit,
    )


@pytest.mark.integration
def test_bulk_request_answers_with_the_portal_link() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    result = _pipeline(gmail, slack, audit).process_email(_bulk_email())

    assert result.outcome is Outcome.SENT, result.detail
    assert result.draft is not None
    assert "payment-status-lookup" in result.draft.reply_body
    assert slack.escalations == []


@pytest.mark.integration
def test_bulk_reply_passes_the_gate_without_an_exemption() -> None:
    """Every check must genuinely pass — no bypass, no skipped check."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    result = _pipeline(gmail, slack, audit).process_email(_bulk_email())

    assert result.gate_result is not None
    assert result.gate_result.allowed, result.gate_result.reasons
    assert all(c.passed for c in result.gate_result.checks)
    # Including the bulk check itself, which is what used to stop this email.
    checks = {c.name: c.passed for c in result.gate_result.checks}
    assert checks["bulk"] is True
    assert checks["authorization"] is True


@pytest.mark.integration
def test_bulk_reply_discloses_no_load_data() -> None:
    """The safety property the design rests on. If this breaks, the gate pass is a lie."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    result = _pipeline(gmail, slack, audit).process_email(_bulk_email())

    assert result.draft is not None
    assert result.draft.load_ids == []
    assert result.draft.citations == []
    body = result.draft.reply_body
    for load_id in _MANY_LOADS:
        assert load_id not in body
    assert "$" not in body
    # Not even the count of loads — it reads as machine-generated and adds a number to a
    # body whose gate pass depends on carrying none.
    assert str(len(_MANY_LOADS)) not in body


@pytest.mark.integration
def test_bulk_reply_never_runs_the_agent() -> None:
    """There is nothing to reason about, so no model turn and no Transport Pro reads."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    email = _bulk_email()
    _pipeline(gmail, slack, audit).process_email(email)

    names = [e.tool_name for e in audit.for_correlation(email.message_id)]
    assert names == ["classify_intent", "extract_identifiers", "detect_sensitive_change"]
    assert "submit_draft" not in names
    assert not any(name.startswith("tp_") for name in names)


@pytest.mark.integration
def test_a_sensitive_bulk_request_still_escalates() -> None:
    """Bulk handling must not overtake the fraud check that runs before it."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    email = _bulk_email().model_copy(
        update={
            "body": "Please update our bank account number, then confirm "
            + ", ".join(_MANY_LOADS)
        }
    )
    result = _pipeline(gmail, slack, audit).process_email(email)

    assert result.outcome is Outcome.ESCALATED
    assert gmail.sent == []
