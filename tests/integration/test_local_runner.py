"""Tests for the ``payment-bot-local`` entrypoint.

The runner is what you actually invoke, so its wiring is worth pinning down: fetch from the
inbox, draft per message, post to Slack, report — and never send.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.integration.test_draft_only_flow import (
    FakeGroqTransport,
    _groq_payment_status_script,
)
from tests.transport_pro_payloads import full_transport

from payment_bot.clients import (
    GroqLlmClient,
    MockGmailClient,
    NullSlackClient,
    TransportProHttpClient,
)
from payment_bot.config import Settings
from payment_bot.local_runner import _Clients, check_configuration, main, process_inbox
from payment_bot.pipeline import Outcome
from payment_bot.sample_data import (
    PAYMENT_STATUS_DRAFT_BODY,
    sample_payment_status_email,
)

CC = ("hrushikesh.attarde@circledelivers.com",)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "reply_cc": CC,
        "gmail_user": "paystatus@circledelivers.com",
        "slack_approval_channel": "#payments-approvals",
    }
    base.update(overrides)
    return Settings(**base)


def _clients(gmail: Any, slack: Any, *, turns: int = 1) -> _Clients:
    script: list[dict[str, Any]] = []
    for _ in range(turns):
        script.extend(_groq_payment_status_script())
    return _Clients(
        tp_factory=lambda: TransportProHttpClient(
            base_url="https://tp.example.test/api/v1",
            username="u",
            password="p",
            transport=full_transport(),
        ),
        gmail=gmail,
        slack=slack,
        llm=GroqLlmClient(
            api_key="gsk_test",
            transport=FakeGroqTransport(script),
            sleep=lambda _s: None,
        ),
    )


@pytest.mark.integration
def test_run_drafts_the_inbox_and_reports(capsys: pytest.CaptureFixture[str]) -> None:
    gmail = MockGmailClient(inbox=[sample_payment_status_email()])
    slack = NullSlackClient()

    results = process_inbox(_settings(), clients=_clients(gmail, slack))

    assert len(results) == 1
    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert gmail.sent == []
    assert len(slack.approvals) == 1

    out = capsys.readouterr().out
    # The report shows what would go out, and says plainly that it did not.
    assert "DRAFT REPLY (NOT SENT)" in out
    assert "To : billing@ideaexpedited.com" in out
    assert "Cc : hrushikesh.attarde@circledelivers.com" in out
    assert "Thursday, August 20, 2026" in out
    assert "DRAFT READY FOR REVIEW (not sent)" in out
    # The gate result and the tool trail are both visible for review.
    assert "PRE-SEND GATE" in out
    assert "[PASS] grounding" in out
    assert "compute_scheduled_pay_date" in out
    # Citations back the figures.
    assert "scheduled pay date: 2026-08-20" in out


@pytest.mark.integration
def test_draft_is_saved_to_gmail_drafts_with_cc(capsys: pytest.CaptureFixture[str]) -> None:
    """The Slack-free path: the reply lands in Drafts, Cc'd, and nothing is sent."""

    gmail = MockGmailClient(inbox=[sample_payment_status_email()])
    slack = NullSlackClient()

    results = process_inbox(_settings(), clients=_clients(gmail, slack))

    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert gmail.sent == []  # still never sends

    assert len(gmail.drafts) == 1
    draft = gmail.drafts[0]
    assert draft.to == "billing@ideaexpedited.com"
    assert draft.cc == CC
    assert draft.subject == "Re: Payment status for load 2462934"
    assert draft.body == PAYMENT_STATUS_DRAFT_BODY
    assert draft.in_reply_to == "msg-2462934-001"

    out = capsys.readouterr().out
    assert "saved to : [Gmail]/Drafts" in out
    assert "review, then Send" in out


@pytest.mark.integration
def test_slack_is_optional() -> None:
    """With no Slack configured the run still completes and still drafts."""

    gmail = MockGmailClient(inbox=[sample_payment_status_email()])
    slack = NullSlackClient()

    results = process_inbox(_settings(), clients=_clients(gmail, slack))

    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert len(gmail.drafts) == 1
    assert slack.approvals  # recorded in-process, posted nowhere


@pytest.mark.integration
def test_dry_run_writes_no_draft(capsys: pytest.CaptureFixture[str]) -> None:
    gmail = MockGmailClient(inbox=[sample_payment_status_email()])

    results = process_inbox(_settings(), dry_run=True, clients=_clients(gmail, NullSlackClient()))

    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert gmail.drafts == []
    assert "no draft written" in capsys.readouterr().out


@pytest.mark.integration
def test_no_draft_is_saved_when_the_gate_blocks() -> None:
    """Only a gate-passed reply becomes a draft — an escalation leaves Drafts untouched."""

    from payment_bot.models import InboundEmail

    gmail = MockGmailClient(
        inbox=[
            InboundEmail(
                message_id="msg-fraud",
                thread_id="t",
                from_email="billing@ideaexpedited.com",
                subject="Update banking information for load 2462934",
                body="Please change our bank account number and routing number.",
            )
        ]
    )
    results = process_inbox(_settings(), clients=_clients(gmail, NullSlackClient()))

    assert results[0].outcome is Outcome.ESCALATED
    assert gmail.drafts == []
    assert gmail.sent == []


@pytest.mark.integration
def test_draft_creation_can_be_turned_off() -> None:
    gmail = MockGmailClient(inbox=[sample_payment_status_email()])
    settings = _settings(gmail_create_draft=False)

    results = process_inbox(settings, clients=_clients(gmail, NullSlackClient()))

    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert gmail.drafts == []


@pytest.mark.integration
def test_limit_caps_the_batch() -> None:
    inbox = [
        sample_payment_status_email().model_copy(update={"message_id": f"msg-{n}"})
        for n in range(3)
    ]
    gmail = MockGmailClient(inbox=inbox)
    slack = NullSlackClient()

    results = process_inbox(_settings(), limit=1, clients=_clients(gmail, slack))

    assert len(results) == 1
    assert len(slack.approvals) == 1


@pytest.mark.integration
def test_empty_inbox_is_reported_not_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    results = process_inbox(_settings(), clients=_clients(MockGmailClient(), NullSlackClient()))

    assert results == []
    assert "Nothing to do" in capsys.readouterr().out


@pytest.mark.integration
def test_draft_only_is_forced_even_if_configuration_says_otherwise() -> None:
    """The runner must not depend on PAYBOT_DRAFT_ONLY being set correctly."""

    from payment_bot.config import RolloutPhase

    gmail = MockGmailClient(inbox=[sample_payment_status_email()])
    slack = NullSlackClient()
    reckless = _settings(draft_only=False, rollout_phase=RolloutPhase.SELECTIVE_AUTOSEND)

    results = process_inbox(reckless, clients=_clients(gmail, slack))

    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert gmail.sent == []


@pytest.mark.integration
def test_the_draft_reaches_slack_when_one_is_configured() -> None:
    """Slack is optional, but when present the draft is mirrored to it as well."""

    gmail = MockGmailClient(inbox=[sample_payment_status_email()])
    slack = NullSlackClient()

    results = process_inbox(_settings(), clients=_clients(gmail, slack))

    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert len(slack.approvals) == 1
    assert len(gmail.drafts) == 1
    assert gmail.drafts[0].body == PAYMENT_STATUS_DRAFT_BODY


# --- CLI --------------------------------------------------------------------
@pytest.mark.integration
def test_check_reports_missing_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_configuration(Settings()) == 1
    out = capsys.readouterr().out
    assert "MISSING CONFIGURATION" in out
    assert "PAYBOT_GROQ_API_KEY" in out
    assert "DRAFT ONLY" in out


@pytest.mark.integration
def test_cli_refuses_to_start_unconfigured(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--limit", "1"]) == 1
    assert "missing configuration" in capsys.readouterr().out
