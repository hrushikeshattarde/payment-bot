"""Integration tests for the rate_verification pipeline (§3.2, §8.5)."""

from __future__ import annotations

import pytest

from payment_bot.clients import (
    ApprovalAction,
    ApprovalDecision,
    AutoApproveResolver,
    MockGmailClient,
    MockSlackClient,
    ScriptedApprovalResolver,
)
from payment_bot.config import RolloutPhase, Settings
from payment_bot.logging import InMemoryAuditSink
from payment_bot.models import InboundEmail
from payment_bot.pipeline import Outcome, PaymentBotPipeline
from payment_bot.sample_data import (
    RATE_VERIFICATION_DRAFT_BODY,
    SAMPLE_SENDER_EMAIL,
    sample_rate_verification_email,
    sample_transport_pro_client,
    scripted_rate_verification_llm,
)

_EXPECTED_TOOL_ORDER = [
    "classify_intent",
    "extract_identifiers",
    "detect_sensitive_change",
    "check_authorization",  # intake pre-check; the gate re-runs it on the draft
    "tp_get_load_summary",
    "compute_carrier_rate",
    "tp_get_dispatch_history",
    "carrier_cross_check",
    "tp_get_settlement_entries",
    "tp_get_noa_factoring",
    "tp_get_file_history",
    "check_authorization",
    "submit_draft",
]


def _build(
    resolver: object,
    gmail: MockGmailClient,
    slack: MockSlackClient,
    audit: InMemoryAuditSink,
    settings: Settings | None = None,
) -> PaymentBotPipeline:
    return PaymentBotPipeline(
        tp=sample_transport_pro_client(),
        gmail=gmail,
        slack=slack,
        llm=scripted_rate_verification_llm(),
        approval_resolver=resolver,  # type: ignore[arg-type]
        audit_sink=audit,
        settings=settings,
    )


@pytest.mark.integration
def test_rate_match_runs_full_sequence_and_sends() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)), gmail, slack, audit
    )

    email = sample_rate_verification_email()
    result = pipeline.process_email(email)

    assert result.outcome is Outcome.SENT, result.detail
    assert result.gate_result is not None and result.gate_result.allowed
    assert len(gmail.sent) == 1
    assert gmail.sent[0].body == RATE_VERIFICATION_DRAFT_BODY
    assert gmail.sent[0].to == SAMPLE_SENDER_EMAIL

    names = [e.tool_name for e in audit.for_correlation(email.message_id)]
    assert names == _EXPECTED_TOOL_ORDER
    # It was routed to the rate_verification skill, and posted for approval (Phase 1).
    assert slack.approvals[0]["summary"].intents == ("rate_verification",)


@pytest.mark.integration
def test_noa_setup_request_escalates_before_agent() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(AutoApproveResolver(), gmail, slack, audit)

    noa_email = InboundEmail(
        message_id="msg-noa-1",
        thread_id="t",
        from_email=SAMPLE_SENDER_EMAIL,
        subject="Rate Verification - Load 2462934",
        body="Please add our NOA and set up factoring, then verify the rate shows $4,650.",
    )
    result = pipeline.process_email(noa_email)

    assert result.outcome is Outcome.ESCALATED
    assert gmail.sent == []
    assert len(slack.escalations) == 1
    names = [e.tool_name for e in audit.for_correlation("msg-noa-1")]
    assert "tp_get_load_summary" not in names  # agent never ran


@pytest.mark.integration
def test_rate_verification_never_auto_sends_even_in_phase2() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    settings = Settings(rollout_phase=RolloutPhase.SELECTIVE_AUTOSEND)
    # If rate ever auto-sent, this rejecter would be bypassed and a message would go out.
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.REJECT)),
        gmail,
        slack,
        audit,
        settings=settings,
    )

    result = pipeline.process_email(sample_rate_verification_email())

    assert result.outcome is Outcome.REJECTED  # approval was required, then rejected
    assert gmail.sent == []
    assert len(slack.approvals) == 1  # it WAS posted for human approval, not auto-sent
