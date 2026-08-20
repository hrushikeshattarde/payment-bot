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


# --- Chat-approval mode (docs/CHAT_APPROVAL_PLAN.md) -------------------------
class _FakeChatHttp:
    """Chat API stand-in: succeeds with a message name, or refuses everything."""

    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom
        self.posts: list[bytes | None] = []
        self._counter = 0

    def request(self, method: str, url: str, *, headers: Any, body: Any = None, timeout: float = 30.0) -> Any:
        from payment_bot.clients.http import HttpResponse

        if self.boom:
            raise OSError("chat is down")
        self.posts.append(body)
        self._counter += 1
        return HttpResponse(200, f'{{"name": "spaces/TEST/messages/M{self._counter}"}}'.encode())


class _FakeChatTokens:
    def token(self) -> str:
        return "ya29.chat"


def _chat_clients(gmail: Any, *, boom: bool = False, turns: int = 1) -> tuple[_Clients, Any]:
    from payment_bot.clients.google_chat import GoogleChatClient

    chat = GoogleChatClient(
        _FakeChatTokens(),
        "spaces/TEST",
        reply_to="paystatus@circledelivers.com",
        interactive=True,
        transport=_FakeChatHttp(boom=boom),
    )
    return _clients(gmail, chat, turns=turns), chat


def _chat_settings(**overrides: Any) -> Settings:
    return _settings(
        approval_mode="chat",
        chat_space="spaces/TEST",
        reviewers=("priya@circledelivers.com",),
        reply_to="paystatus@circledelivers.com",
        **overrides,
    )


@pytest.mark.integration
def test_chat_mode_stores_a_pending_entry_instead_of_a_gmail_draft() -> None:
    from payment_bot.approvals import InMemoryApprovalStore, entry_id_for

    gmail = MockGmailClient(inbox=[sample_payment_status_email()])
    clients, chat = _chat_clients(gmail)
    store = InMemoryApprovalStore()

    results = process_inbox(_chat_settings(), clients=clients, approval_store=store)

    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert gmail.drafts == []  # the card is the review surface now
    assert gmail.sent == []

    email_in = sample_payment_status_email()
    entry = store.pending(entry_id_for(email_in.message_id))
    assert entry is not None
    assert entry.to == email_in.from_email
    assert entry.body == PAYMENT_STATUS_DRAFT_BODY
    assert entry.reply_to == "paystatus@circledelivers.com"
    assert entry.chat_message == chat.posts[email_in.message_id]


@pytest.mark.integration
def test_chat_outage_falls_back_to_a_gmail_draft(capsys: pytest.CaptureFixture[str]) -> None:
    """Plan §3: a chat failure degrades to today's workflow, never to silence."""

    from payment_bot.approvals import InMemoryApprovalStore

    gmail = MockGmailClient(inbox=[sample_payment_status_email()])
    clients, _chat = _chat_clients(gmail, boom=True)
    store = InMemoryApprovalStore()

    results = process_inbox(_chat_settings(), clients=clients, approval_store=store)

    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert store.live_entries() == []  # no card → no entry
    assert len(gmail.drafts) == 1  # the fallback
    assert "falling back to a Gmail draft" in capsys.readouterr().out


@pytest.mark.integration
def test_a_live_pending_entry_skips_the_message_and_its_thread() -> None:
    """The duplicate guard the draft-in-thread check can no longer provide: a message
    (or a follow-up in its thread) with a live card must not re-run the agent loop."""

    from payment_bot.approvals import InMemoryApprovalStore, PendingApproval, entry_id_for

    email_in = sample_payment_status_email()
    follow_up = email_in.model_copy(update={"message_id": "msg-follow-up"})
    gmail = MockGmailClient(inbox=[email_in, follow_up])
    clients, _chat = _chat_clients(gmail)
    store = InMemoryApprovalStore()
    store.put_pending(
        PendingApproval(
            entry_id=entry_id_for(email_in.message_id),
            message_id=email_in.message_id,
            thread_id=email_in.thread_id,
            to=email_in.from_email,
            cc=(),
            reply_to="",
            subject="Re: x",
            body="pending",
            load_ids=(),
        )
    )

    results = process_inbox(_chat_settings(), clients=clients, approval_store=store)

    # Both skipped: the original by message id, the follow-up by thread id.
    assert results == []
    assert gmail.drafts == []


@pytest.mark.integration
def test_without_a_store_chat_mode_keeps_drafting_to_gmail() -> None:
    """Chat mode cannot be half-on: no store (local runs) means Gmail drafts as ever."""

    gmail = MockGmailClient(inbox=[sample_payment_status_email()])
    clients, _chat = _chat_clients(gmail)

    results = process_inbox(_chat_settings(), clients=clients, approval_store=None)

    assert results[0].outcome is Outcome.AWAITING_REVIEW
    assert len(gmail.drafts) == 1


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
