"""The full pipeline driven against the **live-shaped** Transport Pro API.

Same scripted model and same assertions as the mocked payment-status flow, but every
Transport Pro read goes through :class:`TransportProHttpClient` over a fake HTTP transport
replaying real Postman-collection payloads. This is what proves the integration end to
end: array-wrapped payload, token auth, the internal-vs-carrier-facing load id, and a
pre-send gate that still passes on data shaped the way the real API returns it.
"""

from __future__ import annotations

import pytest
from tests.transport_pro_payloads import FakeTransport, full_transport

from payment_bot.clients import (
    ApprovalAction,
    ApprovalDecision,
    MockGmailClient,
    MockSlackClient,
    ScriptedApprovalResolver,
    TransportProHttpClient,
)
from payment_bot.logging import InMemoryAuditSink
from payment_bot.pipeline import Outcome, PaymentBotPipeline
from payment_bot.sample_data import (
    PAYMENT_STATUS_DRAFT_BODY,
    RATE_VERIFICATION_DRAFT_BODY,
    SAMPLE_SENDER_EMAIL,
    sample_payment_status_email,
    sample_rate_verification_email,
    scripted_payment_status_llm,
    scripted_rate_verification_llm,
)


def _live_shaped_tp() -> tuple[TransportProHttpClient, FakeTransport]:
    transport = full_transport()
    client = TransportProHttpClient(
        base_url="https://tp.example.test/api/v1",
        username="apiuser",
        password="secret",
        transport=transport,
    )
    return client, transport


@pytest.mark.integration
def test_payment_status_passes_the_gate_on_live_api_shapes() -> None:
    tp, transport = _live_shaped_tp()
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = PaymentBotPipeline(
        tp=tp,
        gmail=gmail,
        slack=slack,
        llm=scripted_payment_status_llm(),
        approval_resolver=ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)),
        audit_sink=audit,
    )

    email = sample_payment_status_email()
    result = pipeline.process_email(email)

    assert result.outcome is Outcome.SENT, result.detail
    assert result.gate_result is not None and result.gate_result.allowed
    assert [c.passed for c in result.gate_result.checks] == [True] * 6
    assert len(gmail.sent) == 1
    assert gmail.sent[0].body == PAYMENT_STATUS_DRAFT_BODY
    assert gmail.sent[0].to == SAMPLE_SENDER_EMAIL

    # The reply is about the number the carrier quoted, not Transport Pro's internal id.
    summary = next(
        e for e in audit.for_correlation(email.message_id) if e.tool_name == "tp_get_load_summary"
    )
    assert summary.response["load_id"] == "2462934"
    assert summary.response["invoice_generated"] is True
    assert summary.response["total_payout"] == "4650"

    # It really went over HTTP: one login, then the load and dispatch reads.
    assert transport.auth_calls == 1
    assert any("voiceai/load/2462934/payment_information" in u for u in transport.data_urls())
    assert any("dispatch/search?loadId=2462934" in u for u in transport.data_urls())


@pytest.mark.integration
def test_rate_verification_passes_the_gate_on_live_api_shapes() -> None:
    tp, transport = _live_shaped_tp()
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = PaymentBotPipeline(
        tp=tp,
        gmail=gmail,
        slack=slack,
        llm=scripted_rate_verification_llm(),
        approval_resolver=ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)),
        audit_sink=audit,
    )

    result = pipeline.process_email(sample_rate_verification_email())

    assert result.outcome is Outcome.SENT, result.detail
    assert result.gate_result is not None and result.gate_result.allowed
    assert gmail.sent[0].body == RATE_VERIFICATION_DRAFT_BODY

    entries = audit.for_correlation("msg-2462934-rate-001")
    rate = next(e for e in entries if e.tool_name == "compute_carrier_rate")
    assert rate.response["gross_rate"] == "4650"
    assert rate.response["net_rate"] == "4650"
    assert rate.response["deductions"] == []

    # NOA/factoring is derived read-only from remit_to + file history.
    noa = next(e for e in entries if e.tool_name == "tp_get_noa_factoring")
    assert noa.response["noa_on_file"] is False
    assert noa.response["factoring_company_on_file"] is None

    # File history is fetched with the carrier-facing load number.
    assert any("recordId=2462934" in u for u in transport.data_urls())


@pytest.mark.integration
def test_unknown_load_fails_closed_at_intake_and_never_sends() -> None:
    """A load the API does not have must fail closed, not be answered with a guess.

    Every Transport Pro read returns the ``{"ok": false, "error": …}`` envelope, so the
    intake authorization pre-check cannot resolve the sender and escalates before the model
    is ever invoked — no draft exists to hallucinate. The gate's own defense in depth for a
    draft that *does* state unverifiable figures is exercised directly in
    ``tests/unit/test_presend_gate.py`` (unauthorized sender, ungrounded amount/date).
    """

    transport = FakeTransport({"payment_information": []})
    tp = TransportProHttpClient(
        base_url="https://tp.example.test/api/v1",
        username="apiuser",
        password="secret",
        transport=transport,
    )
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = PaymentBotPipeline(
        tp=tp,
        gmail=gmail,
        slack=slack,
        llm=scripted_payment_status_llm(),
        approval_resolver=ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)),
        audit_sink=audit,
    )

    email = sample_payment_status_email()
    result = pipeline.process_email(email)

    assert result.outcome is Outcome.ESCALATED
    assert "not authorized for any load" in result.detail
    assert "ERROR" in result.detail  # authorization unresolved → fail closed
    assert result.draft is None
    assert gmail.sent == []
    assert len(slack.escalations) == 1
    assert slack.approvals == []

    # The model never ran: the trail stops at the intake pre-check.
    names = [e.tool_name for e in audit.for_correlation(email.message_id)]
    assert names == [
        "classify_intent",
        "extract_identifiers",
        "detect_sensitive_change",
        "check_authorization",
    ]
