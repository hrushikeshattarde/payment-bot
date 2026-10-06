"""Integration tests: a follow-up is answered by what it WANTS, and never with a repeat.

The follow-up reader (``payment_bot.followup_reader``) decides what a chase wants; its
accuracy is measured separately against 80 hand-labelled real chases. These tests script
its verdict and check what the pipeline does with each one:

* a status or payment-details ask is answered from the records — but only if the draft says
  something the last reply did not;
* everything else is handed to the colleagues, copied on a code-authored reply;
* a chase after a handoff, or after a code-authored reply the carrier already has, sends
  nothing more;
* a thank-you, or nothing at all, drafts nothing.

Replays the live failure that started this: RTS Financial, load 2493116, asked us to fast
track a 90-day-old invoice and then raised recourse, and was sent the same status each time.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from payment_bot.clients import (
    ApprovalSummary,
    DeferredApprovalResolver,
    MockGmailClient,
    MockSlackClient,
)
from payment_bot.clients.llm import (
    LlmResponse,
    ScriptedLlmClient,
    TextBlock,
    ToolUseBlock,
)
from payment_bot.config import Settings
from payment_bot.followup_reader import FollowUpKind, FollowUpRoute, read_follow_up_with_model
from payment_bot.followups import (
    ACTION_HANDOFF,
    ACTION_NOTICE,
    ACTION_STATUS_UPDATE,
    FollowUpRecord,
    InMemoryFollowUpStore,
    S3FollowUpStore,
    new_facts,
)
from payment_bot.gate.presend import PreSendGate
from payment_bot.logging import InMemoryAuditSink
from payment_bot.models import InboundEmail, PriorReply
from payment_bot.pipeline import Outcome, PaymentBotPipeline
from payment_bot.sample_data import (
    PAYMENT_STATUS_DRAFT_BODY,
    sample_payment_status_email,
    sample_transport_pro_client,
    scripted_payment_status_llm,
)
from payment_bot.tools.submit import SubmitDraftOutput

_CAMIL = "camil.meniano@circledelivers.com"
_BILLING = "billing.lead@circledelivers.com"
_THREAD = "1a10ddd512995d06"

_CAMIL_REPLY = (
    "Hi Jake,\n\n"
    "For load 2493116 (Aka Cargo Inc), the $3,100.00 line haul is currently in a pending "
    "status and has not yet been paid - no payment date is scheduled at this time.\n\n"
    "Circle Delivers Payments"
)
_FAST_TRACK = (
    "Ok but this invoice is over 90 days old. Can you please fast track payment and "
    "provide a status?"
)
_RECOURSE = (
    "Unfortunately, then we will have to recourse this from our client. If you can "
    "expedite it, please do."
)


def _verdict(kind: str, summary: str) -> LlmResponse:
    return LlmResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                tool_use_id="r1", name="report_follow_up", input={"kind": kind, "summary": summary}
            )
        ],
    )


def _reader(kind: str, summary: str = "they want this looked at") -> ScriptedLlmClient:
    return ScriptedLlmClient(responses=[_verdict(kind, summary)] * 5)


def _jake(written: str, message_id: str = "<chase1@rtsfinancial.com>") -> InboundEmail:
    return InboundEmail(
        message_id=message_id,
        thread_id=_THREAD,
        from_email="jwimpey@rtsfinancial.com",
        from_name="Jake Wimpey",
        subject="Re: Payment Status Inquiry",
        body=f"{written}\n\nOn Mon, Oct 5, 2026 at 4:31 PM Camil wrote:\n> {_CAMIL_REPLY}",
        prior_reply=PriorReply(
            from_email=_CAMIL,
            from_name="Camil Meniano",
            sent_at=datetime(2026, 10, 5, 16, 31, tzinfo=timezone(timedelta(hours=-4))),
            body=_CAMIL_REPLY,
            colleagues=(_CAMIL,),
        ),
    )


def _status_chase(prior_body: str, message_id: str = "<status@x>") -> InboundEmail:
    return sample_payment_status_email().model_copy(
        update={
            "message_id": message_id,
            "body": "Any update on this?",
            "prior_reply": PriorReply(from_email=_CAMIL, body=prior_body, colleagues=(_CAMIL,)),
        }
    )


def _pipeline(
    store: InMemoryFollowUpStore,
    reader: ScriptedLlmClient,
    handoff_cc: tuple[str, ...] = (f"Billing Lead <{_BILLING}>",),
    **settings: Any,
) -> tuple[PaymentBotPipeline, MockSlackClient, ScriptedLlmClient]:
    llm = scripted_payment_status_llm()
    slack = MockSlackClient()
    pipeline = PaymentBotPipeline(
        tp=sample_transport_pro_client(),
        gmail=MockGmailClient(),
        slack=slack,
        llm=llm,
        approval_resolver=DeferredApprovalResolver(),
        audit_sink=InMemoryAuditSink(),
        settings=Settings(
            followup_handoff_cc=handoff_cc,
            reply_cc=("paystatus@circledelivers.com",),
            **settings,
        ),
        followup_store=store,
        followup_reader_llm=reader,
    )
    return pipeline, slack, llm


def _summary(slack: MockSlackClient) -> ApprovalSummary:
    summary = slack.approvals[0]["summary"]
    assert isinstance(summary, ApprovalSummary)
    return summary


# --- the RTS thread, replayed ----------------------------------------------------
@pytest.mark.integration
def test_pressure_is_handed_off_not_restated() -> None:
    store = InMemoryFollowUpStore()
    pipeline, slack, llm = _pipeline(
        store, _reader("pressure", "Wants the 90-day-old invoice fast-tracked and a status.")
    )

    result = pipeline.process_email(_jake(_FAST_TRACK))

    assert result.outcome is Outcome.AWAITING_REVIEW, result.detail
    assert result.follow_up_action == ACTION_HANDOFF
    assert llm.calls == [], "no agent run: nothing in the records answers 'fast track'"
    draft = result.draft
    assert draft is not None
    # Answers the ask, copies the people who can act, and states nothing about the load.
    assert "passed your request to our team" in draft.reply_body
    assert "copied" in draft.reply_body
    assert draft.extra_cc == [_CAMIL, _BILLING]
    assert "$" not in draft.reply_body and "2493116" not in draft.reply_body
    # The reviewer sees why, and what was said last.
    summary = _summary(slack)
    assert "press for payment" in summary.handoff and "fast-tracked" in summary.handoff
    assert "Last reply (Camil Meniano on Mon Oct 5)" in summary.follow_up_note
    assert _BILLING in summary.cc
    # And it is on the thread's record, the reader's verdict and the reply text included.
    [entry] = store.history(_THREAD)
    assert (entry.ask, entry.action, entry.outcome) == ("pressure", "handoff", "awaiting_review")
    assert entry.summary.startswith("Wants the 90-day-old invoice")
    assert entry.reply == draft.reply_body


@pytest.mark.integration
def test_a_chase_after_the_handoff_sends_nothing_more() -> None:
    """Telling Jake again who has it would be the repeat. The colleagues get a card."""

    store = InMemoryFollowUpStore()
    pipeline, slack, llm = _pipeline(store, _reader("pressure", "Threatens recourse."))
    pipeline.process_email(_jake(_FAST_TRACK))

    result = pipeline.process_email(_jake(_RECOURSE, "<chase2@rtsfinancial.com>"))

    assert result.outcome is Outcome.ESCALATED
    assert result.follow_up_action == ACTION_NOTICE
    assert result.draft is None
    assert "handed off" in result.detail and "Threatens recourse" in result.detail
    assert _CAMIL in result.detail
    assert len(slack.approvals) == 1, "only the handoff's card — no second draft"
    assert llm.calls == []
    assert [e.action for e in store.history(_THREAD)] == [ACTION_HANDOFF, ACTION_NOTICE]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("kind", "line"),
    [
        ("pressure", "I have passed your request to our team to review."),
        ("not_received", "I have passed this to our team to look into."),
        ("dispute", "I have passed your message to our team to review."),
        ("process_question", "I have passed your question to our team."),
        ("new_info", "I have passed this to our team."),
    ],
)
def test_every_kind_a_person_must_handle_is_handed_off(kind: str, line: str) -> None:
    pipeline, _, llm = _pipeline(InMemoryFollowUpStore(), _reader(kind))

    result = pipeline.process_email(_jake("whatever they wrote"))

    assert result.follow_up_action == ACTION_HANDOFF
    assert result.draft is not None and line in result.draft.reply_body
    assert llm.calls == []


# --- answered from the records, only when it is news ----------------------------------
@pytest.mark.integration
def test_a_status_chase_with_news_is_drafted_with_the_comparison_on_the_card() -> None:
    store = InMemoryFollowUpStore()
    pipeline, slack, llm = _pipeline(store, _reader("status", "Wants an update."))
    email = _status_chase("Hi, this one is still pending - we will update you.")

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.AWAITING_REVIEW, result.detail
    assert result.follow_up_action == ACTION_STATUS_UPDATE
    assert llm.calls
    note = _summary(slack).follow_up_note
    assert "They ask for a status update: Wants an update." in note
    assert "New in this draft:" in note and "date 8/20" in note
    [entry] = store.history(email.thread_id)
    assert entry.action == ACTION_STATUS_UPDATE and entry.reply == PAYMENT_STATUS_DRAFT_BODY


@pytest.mark.integration
def test_a_status_chase_with_nothing_new_is_not_drafted() -> None:
    """The RTS failure in its general form: the records have not moved since our reply."""

    store = InMemoryFollowUpStore()
    pipeline, slack, llm = _pipeline(store, _reader("status", "Wants an update."))

    result = pipeline.process_email(_status_chase(PAYMENT_STATUS_DRAFT_BODY))

    assert result.outcome is Outcome.ESCALATED
    assert result.follow_up_action == ACTION_NOTICE
    assert "unchanged" in result.detail and "would only repeat it" in result.detail
    assert result.after_agent  # priced as the agent run it was, by the retry budget
    assert slack.approvals == []
    assert llm.calls, "the records were re-read; only the repeat was withheld"


@pytest.mark.integration
def test_a_payment_details_ask_tells_the_agent_what_to_give() -> None:
    pipeline, _, llm = _pipeline(
        InMemoryFollowUpStore(), _reader("payment_proof", "Wants the check number.")
    )

    pipeline.process_email(_status_chase("Pending."))

    intake = llm.calls[0]["messages"][0].content[0].text
    assert "What they want now: Wants the check number." in intake
    assert "They are asking for payment details" in intake
    assert "cannot attach a remittance document" in intake


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["thanks", "empty"])
def test_thanks_or_nothing_drafts_nothing(kind: str) -> None:
    pipeline, slack, llm = _pipeline(InMemoryFollowUpStore(), _reader(kind))

    result = pipeline.process_email(_jake("Thank you, Jake"))

    assert result.outcome is Outcome.NO_ACTION
    assert llm.calls == [] and slack.approvals == [] and slack.escalations == []


# --- the reader's failures, and its memory ---------------------------------------------
@pytest.mark.integration
def test_an_unreadable_verdict_goes_to_a_person() -> None:
    garbled = ScriptedLlmClient(
        responses=[LlmResponse(stop_reason="end_turn", content=[TextBlock("no idea")])]
    )
    pipeline, slack, llm = _pipeline(InMemoryFollowUpStore(), garbled)

    result = pipeline.process_email(_jake(_FAST_TRACK))

    assert result.follow_up_action == ACTION_HANDOFF
    assert "could not be read automatically" in _summary(slack).handoff
    assert llm.calls == []


@pytest.mark.integration
def test_a_re_run_reuses_the_verdict_instead_of_asking_again() -> None:
    """An escalated follow-up is re-processed every poll until its budget is spent."""

    store = InMemoryFollowUpStore()
    reader = _reader("pressure", "Presses for payment.")
    pipeline, _, _ = _pipeline(store, reader, handoff_cc=())
    email = _jake(_FAST_TRACK).model_copy(
        update={"prior_reply": PriorReply(from_email=_CAMIL, body=_CAMIL_REPLY)}
    )

    first = pipeline.process_email(email)
    second = pipeline.process_email(email)

    assert first.outcome is second.outcome is Outcome.ESCALATED
    assert len(reader.calls) == 1


@pytest.mark.integration
def test_with_no_owner_named_a_handoff_is_a_card_and_no_email() -> None:
    """ "They will follow up with you directly" is a promise only a named owner can keep."""

    store = InMemoryFollowUpStore()
    pipeline, slack, llm = _pipeline(
        store, _reader("pressure", "Wants the invoice fast-tracked."), handoff_cc=()
    )

    result = pipeline.process_email(_jake(_FAST_TRACK))

    assert result.outcome is Outcome.ESCALATED
    assert result.follow_up_action == ACTION_HANDOFF
    assert result.draft is None and slack.approvals == []
    assert len(slack.escalations) == 1
    assert "needs a person" in result.detail and "Wants the invoice fast-tracked" in result.detail
    assert "Last reply (Camil Meniano on Mon Oct 5)" in result.detail
    assert _CAMIL in result.detail and "No email sent" in result.detail
    assert llm.calls == []
    assert store.history(_THREAD)[0].action == ACTION_HANDOFF


@pytest.mark.integration
def test_the_card_says_how_many_times_they_have_chased() -> None:
    store = InMemoryFollowUpStore()
    pipeline, _, _ = _pipeline(store, _reader("pressure", "Still waiting."), handoff_cc=())

    pipeline.process_email(_jake(_FAST_TRACK))
    second = pipeline.process_email(_jake(_RECOURSE, "<chase2@rtsfinancial.com>"))

    assert "Follow-up #2 in this thread" in second.detail


@pytest.mark.integration
def test_a_bank_change_in_a_follow_up_still_escalates_first() -> None:
    pipeline, slack, _ = _pipeline(InMemoryFollowUpStore(), _reader("new_info"))

    result = pipeline.process_email(
        _jake("Please update our bank account to the new routing number.")
    )

    assert result.outcome is Outcome.ESCALATED
    assert result.detail.startswith("sensitive change")
    assert slack.approvals == []


# --- a code-authored reply is never sent twice in one thread ---------------------------
_REFERRAL = (
    "Thank you for reaching out. The load you asked about is handled directly by:\n\n"
    "- Ashley Wolf (ashley.wolf@circledelivers.com)\n\n"
    "They are copied on this email and will have the most up-to-date information for you.\n\n"
    "Circle Delivers Payments"
)


def _cargotel_chase(prior_body: str) -> tuple[PaymentBotPipeline, MockSlackClient, InboundEmail]:
    pipeline, slack, _ = _pipeline(
        InMemoryFollowUpStore(),
        _reader("status"),
        cargotel_referral_contacts=("Ashley Wolf <ashley.wolf@circledelivers.com>",),
    )
    email = InboundEmail(
        message_id="<cgt-chase@x>",
        thread_id="t-cgt",
        from_email="billing@carrier.example.com",
        subject="Re: load 301230",
        body="Any update on load 301230?",
        prior_reply=PriorReply(from_email=_CAMIL, body=prior_body, colleagues=(_CAMIL,)),
    )
    return pipeline, slack, email


@pytest.mark.integration
def test_the_cargotel_referral_is_never_sent_twice_in_one_thread() -> None:
    """Live: a carrier chasing a 6-digit load was drafted the referral it already had."""

    pipeline, slack, email = _cargotel_chase(_REFERRAL)

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.ESCALATED
    assert result.follow_up_action == ACTION_NOTICE
    assert "repeat the cargotel_referral reply" in result.detail
    assert "ashley.wolf@circledelivers.com" in result.detail
    assert slack.approvals == []


@pytest.mark.integration
def test_the_referral_still_goes_out_when_the_carrier_never_had_it() -> None:
    pipeline, slack, email = _cargotel_chase("Let me check on this and get back to you.")

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.AWAITING_REVIEW, result.detail
    assert result.draft is not None and "handled directly by" in result.draft.reply_body
    assert len(slack.approvals) == 1


# --- "does this draft say anything new?" --------------------------------------------------
_RTS_DRAFT = (
    "Hi Jake, the $3,100 line haul for load 2493116 (Aka Cargo Inc) is still showing as "
    "pending with no payment date scheduled at this time - the load has not yet been billed."
)
_PORTAL = "The payment status for these loads are listed on our website - https://example.com/"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("draft", "prior", "expected"),
    [
        (_RTS_DRAFT, _CAMIL_REPLY, []),  # the live repeat
        (
            "The $3,100 line haul is now scheduled for Thursday, October 8, 2026.",
            _CAMIL_REPLY,
            ["date 10/8"],
        ),
        (
            "The $3,100 line haul was paid by check number 800154 on 10/02/2026.",
            _CAMIL_REPLY,
            ["check #800154", "date 10/2", "paid by check"],
        ),
        # Figures after a reply that gave none ARE news.
        ("Load 2426838 ($1,800) is pending with no payment date yet.", _PORTAL, ["$1800.00"]),
        # A colleague's "10/16" and the draft's "October 16" are the same date.
        (
            "Load 2533198 is scheduled for payment on Friday, October 16, 2026.",
            "Good Morning, This load will be paid on 10/16.",
            [],
        ),
    ],
)
def test_what_counts_as_news(draft: str, prior: str, expected: list[str]) -> None:
    assert new_facts(draft, prior) == expected


# --- the reader's answer ---------------------------------------------------------------
@pytest.mark.unit
def test_the_reader_is_shown_both_sides_and_returns_the_verdict() -> None:
    llm = ScriptedLlmClient(responses=[_verdict("dispute", "Says they invoiced already.")])

    read = read_follow_up_with_model(
        llm, subject="Re: loads", our_reply="We have no invoice.", their_message="Invoiced."
    )

    assert read is not None
    assert (read.kind, read.route) == (FollowUpKind.DISPUTE, FollowUpRoute.HANDOFF)
    prompt = llm.calls[0]["messages"][0].content[0].text
    assert "OUR LAST REPLY:\nWe have no invoice." in prompt
    assert "THEIR NEW MESSAGE:\nInvoiced." in prompt


@pytest.mark.unit
def test_the_reader_accepts_json_text_from_providers_without_tool_calls() -> None:
    llm = ScriptedLlmClient(
        responses=[
            LlmResponse(
                stop_reason="end_turn",
                content=[TextBlock('Here: {"kind": "thanks", "summary": "Says thanks."}')],
            )
        ]
    )
    read = read_follow_up_with_model(llm, subject="", our_reply="x", their_message="Thanks")
    assert read is not None and read.kind is FollowUpKind.THANKS


@pytest.mark.unit
def test_an_invented_kind_or_a_failed_call_is_no_verdict() -> None:
    invented = ScriptedLlmClient(responses=[_verdict("urgent_vip", "?")])
    assert read_follow_up_with_model(invented, subject="", our_reply="", their_message="") is None

    exhausted = ScriptedLlmClient(responses=[])  # raises on the call
    assert read_follow_up_with_model(exhausted, subject="", our_reply="", their_message="") is None


# --- the gate ---------------------------------------------------------------------
def _draft(body: str) -> SubmitDraftOutput:
    return SubmitDraftOutput(reply_body=body, to="x@y.com", load_ids=[], citations=[])


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "This is sitting in our billing queue and we are not able to expedite the timeline "
        "beyond that process.",
        "We will expedite this for you.",
        "Unfortunately this cannot be expedited.",
        "We can fast-track the payment.",
    ],
)
def test_the_gate_blocks_a_promise_or_refusal_to_expedite(body: str) -> None:
    check = PreSendGate()._check_action_commitments(_draft(body))
    assert not check.passed
    assert check.name == "action_commitments"


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        PAYMENT_STATUS_DRAFT_BODY,  # names the carrier Idea Expedited, Inc
        "Payment will be issued to Rush Trucking on Thursday, October 8, 2026.",
        "Thank you for following up. I have passed your request to our team to review.",
    ],
)
def test_the_gate_does_not_mistake_a_carrier_name_for_a_promise(body: str) -> None:
    assert PreSendGate()._check_action_commitments(_draft(body)).passed


# --- the S3 record -------------------------------------------------------------------
class _NoSuchKeyError(Exception):
    """What botocore raises for a missing object, as far as the store can tell."""

    def __init__(self) -> None:
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise _NoSuchKeyError

        class _Body:
            def __init__(self, raw: bytes) -> None:
                self._raw = raw

            def read(self) -> bytes:
                return self._raw

        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        self.objects[Key] = Body


@pytest.mark.unit
def test_the_s3_record_round_trips_one_entry_per_message() -> None:
    s3 = _FakeS3()
    store = S3FollowUpStore("bucket", client=s3)

    assert store.history(_THREAD) == []  # a thread with no record yet
    store.record(_THREAD, FollowUpRecord("<a>", "pressure", ACTION_HANDOFF, "escalated"))
    # The same message re-processed (escalations re-run every poll) updates, never appends.
    store.record(
        _THREAD,
        FollowUpRecord("<a>", "pressure", ACTION_HANDOFF, "awaiting_review", summary="Fast track"),
    )
    store.record(_THREAD, FollowUpRecord("<b>", "pressure", ACTION_NOTICE, "escalated"))

    history = store.history(_THREAD)
    assert [(e.message_id, e.outcome) for e in history] == [
        ("<a>", "awaiting_review"),
        ("<b>", "escalated"),
    ]
    assert history[0].summary == "Fast track"
    saved = json.loads(s3.objects[f"state/followups/{_THREAD}.json"])
    assert saved["thread_id"] == _THREAD


@pytest.mark.unit
def test_a_thread_id_unsafe_for_a_key_is_hashed() -> None:
    s3 = _FakeS3()
    store = S3FollowUpStore("bucket", client=s3)

    store.record("<root@mail.example.com>", FollowUpRecord("<a>", "status", "x", "y"))

    [key] = s3.objects
    assert key.startswith("state/followups/") and "@" not in key
    assert store.history("<root@mail.example.com>")[0].message_id == "<a>"


@pytest.mark.unit
def test_an_unreadable_record_reads_as_empty() -> None:
    s3 = _FakeS3()
    s3.objects[f"state/followups/{_THREAD}.json"] = b"not json"
    assert S3FollowUpStore("bucket", client=s3).history(_THREAD) == []
