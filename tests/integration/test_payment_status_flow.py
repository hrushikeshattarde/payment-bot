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
    "check_authorization",  # intake pre-check; the gate re-runs it on the draft
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
def test_unauthorized_sender_escalates_before_the_agent_runs() -> None:
    """A sender authorized for no load stops at intake: the model is never invoked.

    Before the pre-check, this email would burn a full agent run and then be blocked by
    the gate's authorization check — same terminal outcome, 11-12 LLM requests later.
    """

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(AutoApproveResolver(), gmail, slack, audit)

    stranger = sample_payment_status_email().model_copy(
        update={"message_id": "msg-stranger-1", "from_email": "billing@totally-unrelated.com"}
    )
    result = pipeline.process_email(stranger)

    assert result.outcome is Outcome.ESCALATED
    assert "not authorized for any load" in result.detail
    assert gmail.sent == []
    assert len(slack.escalations) == 1
    # Intake stopped at the authorization pre-check; no model, no Transport Pro tools.
    names = [e.tool_name for e in audit.for_correlation("msg-stranger-1")]
    assert names == [
        "classify_intent",
        "extract_identifiers",
        "detect_sensitive_change",
        "check_authorization",
    ]


@pytest.mark.integration
def test_a_stray_6_digit_number_does_not_block_a_valid_load() -> None:
    """A mixed email proceeds with its 7-digit loads instead of escalating whole.

    Observed live: "Re: 2476340 - Need payment status" carried a stray '107430' in the
    body, and one non-Transport-Pro id used to stop the whole email.
    """

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)), gmail, slack, audit
    )
    mixed = sample_payment_status_email().model_copy(
        update={
            "message_id": "msg-mixed-1",
            "body": "Need payment status for load 2462934. Our reference 107430.",
        }
    )

    result = pipeline.process_email(mixed)

    assert result.outcome is Outcome.SENT, result.detail
    assert "non-Transport-Pro" not in (result.detail or "")
    assert slack.escalations == []


@pytest.mark.integration
def test_an_unresolvable_load_is_dropped_not_fatal() -> None:
    """A phantom id the API errors on must not reach the agent or block the email.

    Observed live: an email naming a real load plus an id Transport Pro 400s on made the
    agent burn all 12 iterations retrying the phantom, producing no draft.
    """

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)), gmail, slack, audit
    )
    phantom = sample_payment_status_email().model_copy(
        update={
            "message_id": "msg-phantom-1",
            "body": "Payment status for 2462934 and 9999998 please.",
        }
    )

    result = pipeline.process_email(phantom)

    assert result.outcome is Outcome.SENT, result.detail
    assert slack.escalations == []
    # The intake pre-check tried both loads; the phantom errored and was dropped.
    intake_auth = [
        e
        for e in audit.for_correlation("msg-phantom-1")
        if e.tool_name == "check_authorization" and e.request.get("load_id") == "9999998"
    ]
    assert intake_auth and not intake_auth[0].ok


@pytest.mark.integration
def test_loads_found_only_in_an_attachment_are_answered() -> None:
    """A statement email whose load ids live in the spreadsheet, not the body."""

    from payment_bot.models import EmailAttachment

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)), gmail, slack, audit
    )
    statement = sample_payment_status_email().model_copy(
        update={
            "message_id": "msg-att-1",
            "body": "Hello, please see the attached statement.",
            "attachments": [
                EmailAttachment(
                    filename="statement.xlsx",
                    mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    extracted_text="Load #\tAmount\n2462934\t4650.00",
                )
            ],
        }
    )

    result = pipeline.process_email(statement)

    assert result.outcome is Outcome.SENT, result.detail
    assert slack.escalations == []


@pytest.mark.integration
def test_bulk_attachment_loads_get_the_portal_reply() -> None:
    """More attachment loads than the threshold → the self-service portal link."""

    from payment_bot.models import EmailAttachment

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)), gmail, slack, audit
    )
    rows = "\n".join(f"25200{n:02d}" for n in range(1, 8))  # seven 7-digit ids
    bulk = sample_payment_status_email().model_copy(
        update={
            "message_id": "msg-att-bulk-1",
            "body": "Payment status for the attached loads please.",
            "attachments": [
                EmailAttachment(
                    filename="loads.xlsx",
                    mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    extracted_text=rows,
                )
            ],
        }
    )

    result = pipeline.process_email(bulk)

    assert result.outcome is Outcome.SENT, result.detail
    assert "payment-status-lookup" in gmail.sent[0].body  # the portal link
    assert slack.escalations == []


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


# --- Combined intent ---------------------------------------------------------
# "Confirm the rate and tell me when I get paid" used to be refused outright: §3.5 merging is
# unwired, so _select_skill returned None. On live mail that was 5 of 20 emails, all of them
# answerable. It now answers the more specific question rather than nothing.
@pytest.mark.integration
def test_combined_intent_without_a_figure_answers_payment_status() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)), gmail, slack, audit
    )
    email = sample_payment_status_email()
    combined = email.model_copy(
        update={
            "subject": "Load 2462934 - verify the rate and payment status",
            "body": "Please confirm the rate and let me know the pay date for load 2462934.",
        }
    )

    result = pipeline.process_email(combined)

    assert result.outcome is not Outcome.ESCALATED, result.detail
    names = [e.tool_name for e in audit.for_correlation(combined.message_id)]
    # The payment_status procedure, not a refusal.
    assert "compute_scheduled_pay_date" in names


@pytest.mark.integration
def test_combined_intent_is_no_longer_escalated_as_unanswerable() -> None:
    """The specific detail string that used to come back."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)), gmail, slack, audit
    )
    combined = sample_payment_status_email().model_copy(
        update={"body": "Verify the rate and give me the payment status for load 2462934."}
    )

    result = pipeline.process_email(combined)

    assert "intent not answerable" not in (result.detail or "")


@pytest.mark.integration
def test_bank_wording_drafts_when_the_policy_allows_it() -> None:
    """PAYBOT_SENSITIVE_BANK_REPLIES: an authorized sender's status question is answered
    even when the email also asks to switch payment method. The instruction itself is
    ignored (prompt rule) and can never be acknowledged (gate check #9)."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    settings = Settings(sensitive_bank_replies=True)
    pipeline = _build(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)),
        gmail,
        slack,
        audit,
        settings=settings,
    )
    email = sample_payment_status_email().model_copy(
        update={
            "message_id": "msg-achswitch-1",
            "body": (
                "Payment status for load 2462934 please. Also, can we please be switched "
                "to ACH pay and not checks? USPS is extremely slow."
            ),
        }
    )

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.SENT, result.detail
    assert slack.escalations == []


@pytest.mark.integration
def test_bank_wording_still_escalates_by_default() -> None:
    """The strict default is unchanged: same email, no policy switch, security escalation."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _build(AutoApproveResolver(), gmail, slack, audit)
    email = sample_payment_status_email().model_copy(
        update={
            "message_id": "msg-achswitch-2",
            "body": (
                "Payment status for load 2462934 please. Also, can we please be switched "
                "to ACH pay and not checks?"
            ),
        }
    )

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.ESCALATED
    assert "sensitive change" in result.detail


@pytest.mark.integration
def test_noa_attachment_still_escalates_despite_the_bank_policy() -> None:
    """Paperwork is not wording: an NOA attachment escalates whatever the policy says."""

    from payment_bot.models import EmailAttachment

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    settings = Settings(sensitive_bank_replies=True)
    pipeline = _build(AutoApproveResolver(), gmail, slack, audit, settings=settings)
    email = sample_payment_status_email().model_copy(
        update={
            "message_id": "msg-noa-policy-1",
            "body": "Payment status for load 2462934 please.",
            "attachments": [
                EmailAttachment(filename="ACME_NOA.pdf", mime_type="application/pdf")
            ],
        }
    )

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.ESCALATED
    assert "noa_setup_change" in result.detail
