"""The local draft-only pipeline, end to end.

Groq drives the loop (over a fake HTTP transport replaying a real tool-call sequence),
Transport Pro serves live-shaped payloads, and the gate-passing draft is posted to Slack —
with **nothing sent**. These tests exist to lock down that last property: the whole point of
draft-only mode is that no code path reaches an outbound email.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.transport_pro_payloads import full_transport

from payment_bot.clients import (
    ApprovalAction,
    DeferredApprovalResolver,
    GmailApiClient,
    GroqLlmClient,
    MockGmailClient,
    MockSlackClient,
    ServiceAccountTokenSource,
    TransportProHttpClient,
)
from payment_bot.clients.http import HttpResponse
from payment_bot.config import RolloutPhase, Settings
from payment_bot.logging import InMemoryAuditSink
from payment_bot.pipeline import Outcome, PaymentBotPipeline
from payment_bot.sample_data import (
    PAYMENT_STATUS_DRAFT_BODY,
    SAMPLE_SENDER_EMAIL,
    sample_payment_status_email,
)

CC = ("hrushikesh.attarde@circledelivers.com",)


def _tool_turn(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def _groq_payment_status_script() -> list[dict[str, Any]]:
    """The §7.4 payment-status sequence for load 2462934, as Groq would emit it."""

    lid = "2462934"
    return [
        _tool_turn("c1", "tp_get_load_summary", {"load_id": lid}),
        # The client reports pay dates in the app's calendar: raw 2026-08-19 → 2026-08-20.
        _tool_turn("c2", "compute_scheduled_pay_date", {"estimated_payment_date": "2026-08-20", "load_id": lid}),
        _tool_turn("c3", "tp_get_dispatch_history", {"load_id": lid}),
        _tool_turn("c4", "carrier_cross_check", {"load_id": lid, "system": "transport_pro"}),
        _tool_turn("c5", "tp_get_settlement_entries", {"load_id": lid}),
        _tool_turn(
            "c6",
            "check_authorization",
            {
                "sender_email": SAMPLE_SENDER_EMAIL,
                "sender_name": "Idea Expedited Billing",
                "load_id": lid,
                "system": "transport_pro",
            },
        ),
        _tool_turn(
            "c7",
            "submit_draft",
            {
                "reply_body": PAYMENT_STATUS_DRAFT_BODY,
                "to": SAMPLE_SENDER_EMAIL,
                "load_ids": [lid],
                "citations": [
                    {"fact": "total pending", "value": "$4,650", "source_tool": "tp_get_load_summary"},
                    {
                        "fact": "scheduled pay date",
                        "value": "2026-08-20",
                        "source_tool": "compute_scheduled_pay_date",
                    },
                ],
            },
        ),
    ]


class FakeGroqTransport:
    """Replays a queued list of Groq completions."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls = 0

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        self.calls += 1
        if not self.script:
            raise AssertionError("Groq script exhausted — the loop asked for another turn")
        return HttpResponse(200, json.dumps(self.script.pop(0)).encode())


def _draft_only_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "draft_only": True,
        "reply_cc": CC,
        "slack_approval_channel": "#payments-approvals",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _pipeline(
    settings: Settings,
    gmail: Any,
    slack: MockSlackClient,
    audit: InMemoryAuditSink,
) -> PaymentBotPipeline:
    return PaymentBotPipeline(
        tp=TransportProHttpClient(
            base_url="https://tp.example.test/api/v1",
            username="apiuser",
            password="secret",
            transport=full_transport(),
        ),
        gmail=gmail,
        slack=slack,
        llm=GroqLlmClient(
            api_key="gsk_test",
            transport=FakeGroqTransport(_groq_payment_status_script()),
            sleep=lambda _s: None,
        ),
        approval_resolver=DeferredApprovalResolver(),
        settings=settings,
        audit_sink=audit,
    )


@pytest.mark.integration
def test_groq_drives_the_loop_and_the_draft_is_posted_not_sent() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    result = _pipeline(_draft_only_settings(), gmail, slack, audit).process_email(
        sample_payment_status_email()
    )

    # The terminal state is "awaiting review", never "sent".
    assert result.outcome is Outcome.AWAITING_REVIEW, result.detail
    assert result.detail == "draft ready for review; nothing sent"
    assert gmail.sent == []

    # The gate still ran in full and passed.
    assert result.gate_result is not None and result.gate_result.allowed
    assert [c.passed for c in result.gate_result.checks] == [True] * 6

    # The draft reached Slack, with the reviewer on Cc.
    assert len(slack.approvals) == 1
    approval = slack.approvals[0]
    assert approval["channel"] == "#payments-approvals"
    assert approval["draft_reply"] == PAYMENT_STATUS_DRAFT_BODY
    assert approval["summary"].cc == CC
    assert approval["summary"].load_ids == ("2462934",)
    assert slack.escalations == []

    # And it really was Groq's tool sequence that got us there.
    names = [e.tool_name for e in audit.for_correlation("msg-2462934-001")]
    assert names == [
        "classify_intent",
        "extract_identifiers",
        "detect_sensitive_change",
        "check_authorization",  # intake pre-check; the gate re-runs it on the draft
        "tp_get_load_summary",
        "compute_scheduled_pay_date",
        "tp_get_dispatch_history",
        "carrier_cross_check",
        "tp_get_settlement_entries",
        "check_authorization",
        "submit_draft",
    ]


@pytest.mark.integration
def test_draft_only_overrides_phase_2_auto_send() -> None:
    """Even misconfigured into Phase 2, a draft-only run must not send."""

    settings = _draft_only_settings(rollout_phase=RolloutPhase.SELECTIVE_AUTOSEND)
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()

    result = _pipeline(settings, gmail, slack, audit).process_email(sample_payment_status_email())

    assert result.outcome is Outcome.AWAITING_REVIEW
    assert gmail.sent == []
    assert len(slack.approvals) == 1


def _real_gmail_client() -> GmailApiClient:
    """The actual Gmail client the runner builds, with no network behind it."""

    tokens = ServiceAccountTokenSource(
        {"client_email": "sa@x", "private_key": "k", "client_id": "1"},
        subject="paystatus@circledelivers.com",
        transport=_NeverCalled(),
    )
    return GmailApiClient(tokens, transport=_NeverCalled())


class _NeverCalled:
    """A transport that fails loudly if anything actually tries to use the network."""

    def request(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no HTTP call should be needed to prove sending is refused")


@pytest.mark.integration
def test_gmail_client_refusing_to_send_is_a_second_independent_guard() -> None:
    """With draft_only off *and* Phase 2, the Gmail client still refuses to send.

    ``send_reply`` raises before any HTTP is attempted, so this holds even though the
    service-account credential's ``gmail.compose`` scope would technically permit sending.
    """

    settings = _draft_only_settings(
        draft_only=False, rollout_phase=RolloutPhase.SELECTIVE_AUTOSEND
    )
    slack, audit = MockSlackClient(), InMemoryAuditSink()

    result = _pipeline(settings, _real_gmail_client(), slack, audit).process_email(
        sample_payment_status_email()
    )

    # The send attempt raised, the pipeline failed closed and escalated instead.
    assert result.outcome is Outcome.ESCALATED
    assert "sending is disabled" in result.detail
    assert len(slack.escalations) == 1


@pytest.mark.integration
def test_deferred_resolver_never_approves() -> None:
    decision = DeferredApprovalResolver().resolve("msg-1", "any draft")
    assert decision.action is ApprovalAction.DEFER
    assert decision.edited_text is None


@pytest.mark.integration
def test_sensitive_change_escalates_before_groq_is_called() -> None:
    """The intake guard still runs first: no model tokens spent on a fraud attempt."""

    from payment_bot.models import InboundEmail

    settings = _draft_only_settings()
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    groq_transport = FakeGroqTransport(_groq_payment_status_script())
    pipeline = PaymentBotPipeline(
        tp=TransportProHttpClient(
            base_url="https://tp.example.test/api/v1",
            username="u",
            password="p",
            transport=full_transport(),
        ),
        gmail=gmail,
        slack=slack,
        llm=GroqLlmClient(api_key="gsk_test", transport=groq_transport, sleep=lambda _s: None),
        approval_resolver=DeferredApprovalResolver(),
        settings=settings,
        audit_sink=audit,
    )

    result = pipeline.process_email(
        InboundEmail(
            message_id="msg-fraud",
            thread_id="t",
            from_email=SAMPLE_SENDER_EMAIL,
            subject="Update banking information for load 2462934",
            body="Please change our bank account number and routing number.",
        )
    )

    assert result.outcome is Outcome.ESCALATED
    assert groq_transport.calls == 0  # the model was never consulted
    assert gmail.sent == []
    assert len(slack.escalations) == 1
