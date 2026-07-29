"""Integration tests: the whole payment_status pipeline, driven by a scripted model.

These exercise intake → agent loop → pre-send gate → approval → send end-to-end with mock
clients, asserting the audited tool sequence, the gate decision, and the side effects
(what was sent / escalated). No network, fully deterministic.
"""

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
    PAYMENT_STATUS_DRAFT_BODY,
    SAMPLE_SENDER_EMAIL,
    sample_payment_status_email,
    sample_transport_pro_client,
    scripted_payment_status_llm,
)

_EXPECTED_TOOL_ORDER = [
    "classify_intent",
    "extract_identifiers",
    "detect_sensitive_change",
    "tp_get_load_summary",
    "compute_scheduled_pay_date",
    "compute_scheduled_pay_date",
    "tp_get_dispatch_history",
    "carrier_cross_check",
    "tp_get_settlement_entries",
    "check_authorization",
    "submit_draft",
]


def _build(resolver: object, gmail: MockGmailClient, slack: MockSlackClient, audit: InMemoryAuditSink, settings: Settings | None = None) -> PaymentBotPipeline:
    return PaymentBotPipeline(
        tp=sample_transport_pro_client(),
        gmail=gmail,
        slack=slack,
        llm=scripted_payment_status_llm(),
        approval_resolver=resolver,  # type: ignore[arg-type]
        audit_sink=audit,
        settings=settings,
    )


@pytest.mark.integration
def test_happy_path_runs_tools_passes_gate_and_sends() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)), gmail, slack, audit
    )

    email = sample_payment_status_email()
    result = pipeline.process_email(email)

    # Outcome + side effects
    assert result.outcome is Outcome.SENT, result.detail
    assert result.gate_result is not None and result.gate_result.allowed
    assert len(gmail.sent) == 1
    assert gmail.sent[0].body == PAYMENT_STATUS_DRAFT_BODY
    assert gmail.sent[0].to == SAMPLE_SENDER_EMAIL
    assert len(slack.approvals) == 1
    assert slack.escalations == []

    # Audited tool sequence (intake + agent), in order (§8.1)
    names = [e.tool_name for e in audit.for_correlation(email.message_id)]
    assert names == _EXPECTED_TOOL_ORDER
    assert all(e.ok for e in audit.for_correlation(email.message_id))


@pytest.mark.integration
def test_rejection_does_not_send() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.REJECT)), gmail, slack, audit
    )

    result = pipeline.process_email(sample_payment_status_email())

    assert result.outcome is Outcome.REJECTED
    assert gmail.sent == []
    assert len(slack.approvals) == 1  # it was posted for review, then rejected


@pytest.mark.integration
def test_human_edit_is_re_gated_then_sent() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    edited_body = PAYMENT_STATUS_DRAFT_BODY + "\n\nThank you for your patience."
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.EDIT, edited_text=edited_body)),
        gmail,
        slack,
        audit,
    )

    result = pipeline.process_email(sample_payment_status_email())

    assert result.outcome is Outcome.SENT
    assert len(gmail.sent) == 1
    assert gmail.sent[0].body == edited_body


@pytest.mark.integration
def test_bank_change_escalates_before_agent_runs() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(AutoApproveResolver(), gmail, slack, audit)

    fraud = InboundEmail(
        message_id="msg-fraud-1",
        thread_id="t",
        from_email=SAMPLE_SENDER_EMAIL,
        subject="Please update our bank account for load 2462934",
        body="We changed banks — update the routing number and account number on file.",
    )
    result = pipeline.process_email(fraud)

    assert result.outcome is Outcome.ESCALATED
    assert gmail.sent == []
    assert len(slack.escalations) == 1
    # The agent never ran: no Transport Pro lookups in the audit trail.
    names = [e.tool_name for e in audit.for_correlation("msg-fraud-1")]
    assert "tp_get_load_summary" not in names
    assert names == ["classify_intent", "extract_identifiers", "detect_sensitive_change"]


@pytest.mark.integration
def test_phase2_single_load_auto_sends_without_approval() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    settings = Settings(rollout_phase=RolloutPhase.SELECTIVE_AUTOSEND)
    # Resolver must never be consulted in auto-send; use a rejecter to prove it isn't.
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.REJECT)),
        gmail,
        slack,
        audit,
        settings=settings,
    )

    result = pipeline.process_email(sample_payment_status_email())

    assert result.outcome is Outcome.SENT
    assert len(gmail.sent) == 1
    assert slack.approvals == []  # auto-sent, no human approval posted
