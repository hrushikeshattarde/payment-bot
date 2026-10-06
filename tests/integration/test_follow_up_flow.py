"""Integration tests: a carrier chasing a reply a colleague already sent.

The Gmail client decides WHICH threads are follow-ups (tests/unit/test_gmail_api.py). These
cover what the pipeline then does with one: answer it with the colleague's reply in the
agent's hands, draft nothing for a follow-up that asks nothing, keep the colleague's figures
out of the sender's stated amounts, and never auto-send.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from payment_bot.agent.skills import build_payment_status_intake
from payment_bot.clients import (
    ApprovalAction,
    ApprovalDecision,
    MockGmailClient,
    MockSlackClient,
    ScriptedApprovalResolver,
)
from payment_bot.clients.google_chat import approval_card
from payment_bot.clients.llm import LlmResponse, ScriptedLlmClient, ToolUseBlock
from payment_bot.config import RolloutPhase, Settings
from payment_bot.logging import InMemoryAuditSink
from payment_bot.models import InboundEmail, PriorReply
from payment_bot.pipeline import Outcome, PaymentBotPipeline
from payment_bot.sample_data import (
    sample_payment_status_email,
    sample_transport_pro_client,
    scripted_payment_status_llm,
)
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import (
    ExtractIdentifiers,
    ExtractIdentifiersInput,
    follow_up_asks,
)

_COLLEAGUE = "angelica.baracao@circledelivers.com"

_COLLEAGUE_REPLY = (
    "Hi,\n\n"
    "Load 2462934 is waiting on the signed POD. Total pending $4,650.00.\n\n"
    "On Fri, Oct 2, 2026 at 9:00 AM Idea Expedited Billing wrote:\n"
    "> Could you tell me the payment status for load 2462934?\n"
)

#: The chase, with the colleague's reply quoted beneath it the way a mail client sends it.
_QUOTED = (
    "\n\nOn Mon, Oct 5, 2026 at 10:15 AM Angelica Baracao wrote:\n"
    "> Load 2462934 is waiting on the signed POD. Total pending $9,999.00.\n"
)


def _follow_up(written: str) -> InboundEmail:
    return sample_payment_status_email().model_copy(
        update={
            "message_id": "msg-2462934-chase",
            "subject": "Re: Payment status for load 2462934",
            "body": written + _QUOTED,
            "prior_reply": PriorReply(
                from_email=_COLLEAGUE,
                from_name="Angelica Baracao",
                sent_at=datetime(2026, 10, 5, 10, 15, tzinfo=timezone(timedelta(hours=-4))),
                body=_COLLEAGUE_REPLY,
            ),
        }
    )


def _reader(kind: str, summary: str = "they want an update") -> ScriptedLlmClient:
    """The follow-up reader's verdict, scripted — the model's side of it is evaluated on
    real mail separately; these tests are about what the pipeline does with each verdict."""

    verdict = LlmResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                tool_use_id="r1",
                name="report_follow_up",
                input={"kind": kind, "summary": summary},
            )
        ],
    )
    return ScriptedLlmClient(responses=[verdict] * 5)


def _pipeline(
    resolver: ScriptedApprovalResolver,
    settings: Settings | None = None,
    kind: str = "status",
) -> tuple[PaymentBotPipeline, MockSlackClient, MockGmailClient, object, InMemoryAuditSink]:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    llm = scripted_payment_status_llm()
    pipeline = PaymentBotPipeline(
        tp=sample_transport_pro_client(),
        gmail=gmail,
        slack=slack,
        llm=llm,
        approval_resolver=resolver,
        audit_sink=audit,
        settings=settings,
        followup_reader_llm=_reader(kind),
    )
    return pipeline, slack, gmail, llm, audit


@pytest.mark.integration
def test_a_chase_is_answered_with_the_colleagues_reply_in_the_agents_hands() -> None:
    pipeline, slack, _, llm, _ = _pipeline(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE))
    )

    result = pipeline.process_email(_follow_up("Any update on this? We sent the POD Monday."))

    assert result.outcome is Outcome.SENT, result.detail
    assert result.follow_up_to == _COLLEAGUE  # what the run summary counts follow-ups by
    prompt = llm.calls[0]["messages"][0].content[0].text  # type: ignore[attr-defined]
    assert "FOLLOW-UP" in prompt
    assert "Angelica Baracao <angelica.baracao@circledelivers.com>" in prompt
    assert "Monday, October 5, 2026" in prompt
    assert "waiting on the signed POD" in prompt
    # What the colleague wrote, not the carrier's mail they quoted beneath it.
    assert "> Could you tell me" not in prompt
    # Reviewers are told what they are approving.
    assert slack.approvals[0]["summary"].follow_up_to == _COLLEAGUE  # type: ignore[attr-defined]


@pytest.mark.integration
def test_a_chase_naming_no_load_finds_it_in_our_reply() -> None:
    """Live: a carrier answered Angelica with no load in subject or body and quoted nothing.

    It escalated as "no valid 6/7-digit load id found" while the load sat in her reply.
    """

    pipeline, _, _, _, audit = _pipeline(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE))
    )
    bare = _follow_up("Any update?").model_copy(
        update={"subject": "Re: Payment", "body": "Any update?"}
    )

    result = pipeline.process_email(bare)

    assert result.outcome is Outcome.SENT, result.detail
    names = [e.tool_name for e in audit.for_correlation(bare.message_id)]
    assert "tp_get_load_summary" in names


@pytest.mark.integration
def test_a_follow_up_that_asks_nothing_drafts_nothing() -> None:
    """A "thanks" after the colleague's answer closes the conversation."""

    pipeline, slack, gmail, llm, audit = _pipeline(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)), kind="thanks"
    )
    email = _follow_up("Thank you, appreciate it.")

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.NO_ACTION
    assert result.follow_up_to == _COLLEAGUE
    assert _COLLEAGUE in result.detail
    assert llm.calls == []  # type: ignore[attr-defined]
    assert audit.for_correlation(email.message_id) == []
    assert slack.approvals == [] and slack.escalations == [] and gmail.sent == []


@pytest.mark.integration
def test_the_same_thanks_without_a_prior_reply_is_unchanged() -> None:
    """Only follow-ups are held to the ask test; first-contact mail is read as before."""

    pipeline, _, _, llm, _ = _pipeline(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE))
    )
    email = _follow_up("Thank you, appreciate it.").model_copy(update={"prior_reply": None})

    result = pipeline.process_email(email)

    assert result.outcome is not Outcome.NO_ACTION
    assert result.follow_up_to == ""
    assert llm.calls  # type: ignore[attr-defined]


@pytest.mark.integration
def test_a_follow_up_is_never_auto_sent() -> None:
    """Phase 2 auto-sends a clean single-load answer — but not into a colleague's thread."""

    pipeline, slack, gmail, _, _ = _pipeline(
        ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.REJECT)),
        settings=Settings(rollout_phase=RolloutPhase.SELECTIVE_AUTOSEND),
    )

    result = pipeline.process_email(_follow_up("Any update?"))

    assert result.outcome is Outcome.REJECTED, result.detail
    assert result.follow_up_to == _COLLEAGUE
    assert len(slack.approvals) == 1
    assert gmail.sent == []


# --- the pieces, directly ------------------------------------------------------
@pytest.mark.unit
def test_the_colleagues_figures_never_become_the_senders_stated_amounts(
    ctx: ToolContext,
) -> None:
    """Recorded as the sender's, the gate would let the draft repeat them unconfirmed."""

    quoted = "> Load 2462934 total pending $9,999.00"
    as_quoted = ExtractIdentifiers().run(
        ExtractIdentifiersInput(body="Any update?", thread_text=quoted), ctx
    )
    assert as_quoted.load_ids == ["2462934"]  # its loads are what the carrier is chasing
    assert as_quoted.stated_rates == []
    assert as_quoted.written_load_ids == []

    # Control: the same line written by the sender IS their stated amount.
    as_written = ExtractIdentifiers().run(ExtractIdentifiersInput(body=quoted[2:]), ctx)
    assert [r.amount for r in as_written.stated_rates] == [9999]


@pytest.mark.unit
def test_a_follow_up_is_split_into_what_was_written_and_what_was_quoted() -> None:
    email = _follow_up("Any update?").model_copy(update={"html": "<p>$9,999.00 2462934</p>"})

    parts = PaymentBotPipeline._identifier_text(email)

    assert parts["body"].strip() == "Any update?"
    assert "$9,999.00" in parts["thread_text"]
    # The HTML repeats the quoted history, so it joins the ids-only side.
    assert parts["html_text"] == ""
    assert email.html_text in parts["thread_text"]
    # So does the colleague's reply, which may be the only place the load is named.
    assert email.prior_reply is not None
    assert email.prior_reply.body in parts["thread_text"]

    ordinary = email.model_copy(update={"prior_reply": None})
    whole = PaymentBotPipeline._identifier_text(ordinary)
    assert whole["body"] == ordinary.body
    assert whole["html_text"] == ordinary.html_text


@pytest.mark.unit
def test_a_follow_up_yields_the_load_but_none_of_our_amounts(ctx: ToolContext) -> None:
    """The colleague's reply ($4,650.00) and the quote of it ($9,999.00) both name the load."""

    parts = PaymentBotPipeline._identifier_text(_follow_up("Any update?"))

    found = ExtractIdentifiers().run(ExtractIdentifiersInput(**parts), ctx)

    assert found.load_ids == ["2462934"]
    assert found.stated_rates == []


@pytest.mark.unit
def test_first_contact_intake_carries_no_follow_up_lines() -> None:
    intake = build_payment_status_intake(sample_payment_status_email(), ["2462934"], {})
    assert "FOLLOW-UP" not in intake


@pytest.mark.unit
@pytest.mark.parametrize(
    "written",
    [
        "Any update?",
        "Thank you! I have attached our voided check. Please provide payment details.",
        "Hello team\nBoth loads were invoiced already.\n\nThank you,\nCrystal",
        "Thanks, can you check again?",
        "Good morning,",  # nothing written: the reader calls it empty
    ],
)
def test_everything_but_a_bare_thank_you_reaches_the_reader(written: str) -> None:
    assert follow_up_asks(_follow_up("").model_copy(update={"body": written}))


@pytest.mark.unit
@pytest.mark.parametrize(
    "written",
    [
        "Thank you!",
        "Received, thank you!",
        "Thank you for the update.",
        "Got it",
        "Received, thank you!\n\nThanks,\nBob",
    ],
)
def test_a_bare_thank_you_never_takes_a_slot(written: str) -> None:
    assert not follow_up_asks(_follow_up("").model_copy(update={"body": written}))


@pytest.mark.unit
def test_the_card_flags_a_follow_up() -> None:
    def labels(card: dict[str, object]) -> list[str]:
        widgets = card["card"]["sections"][0]["widgets"]  # type: ignore[index]
        return [w["decoratedText"]["topLabel"] for w in widgets if "decoratedText" in w]

    common: dict[str, Any] = {
        "entry_id": "e1",
        "from_email": "billing@ideaexpedited.com",
        "load_ids": ("2462934",),
        "to": "billing@ideaexpedited.com",
        "cc": (),
        "reply_to": "",
        "body": "Hello",
    }
    assert "Follow-up" in labels(approval_card(**common, follow_up_to=_COLLEAGUE))
    assert "Follow-up" not in labels(approval_card(**common))
