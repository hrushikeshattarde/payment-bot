"""Integration tests: a follow-up is answered by what it ASKS, and never twice the same way.

Replays the live failure. RTS Financial asked about load 2493116; Camil's (bot-drafted) reply
said pending, no pay date. Jake then wrote "over 90 days old — can you fast track payment and
provide a status?" and got the same status plus "we are not able to expedite", then "we
will have to recourse this" and got the same status a third time.

Now: an ask to act, a dispute, or a second chase goes to a person — a code-authored handoff
copying the colleagues — and a chase after a handoff sends nothing more. Every decision is
recorded per thread.
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
from payment_bot.config import Settings
from payment_bot.followups import (
    ACTION_HANDOFF,
    ACTION_NOTICE,
    ACTION_STATUS_UPDATE,
    FollowUpRecord,
    InMemoryFollowUpStore,
    S3FollowUpStore,
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
from payment_bot.tools.shared import FollowUpAsk, follow_up_asks, read_follow_up
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
            sent_at=datetime(2026, 10, 5, 16, 31, tzinfo=timezone(timedelta(hours=-4))),
            body=_CAMIL_REPLY,
            colleagues=(_CAMIL,),
        ),
    )


def _pipeline(
    store: InMemoryFollowUpStore,
    handoff_cc: tuple[str, ...] = (f"Billing Lead <{_BILLING}>",),
) -> tuple[PaymentBotPipeline, MockSlackClient, Any]:
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
        ),
        followup_store=store,
    )
    return pipeline, slack, llm


# --- the RTS thread, replayed ----------------------------------------------------
@pytest.mark.integration
def test_fast_track_on_a_90_day_invoice_is_handed_off_not_restated() -> None:
    store = InMemoryFollowUpStore()
    pipeline, slack, llm = _pipeline(store)

    result = pipeline.process_email(_jake(_FAST_TRACK))

    assert result.outcome is Outcome.AWAITING_REVIEW, result.detail
    assert result.follow_up_action == ACTION_HANDOFF
    assert llm.calls == [], "no agent run: nothing it can look up answers 'fast track'"
    draft = result.draft
    assert draft is not None
    # Answers the ask, copies the people who can act, and states nothing about the load.
    assert "passed your request to our team" in draft.reply_body
    assert "copied" in draft.reply_body
    assert draft.extra_cc == [_CAMIL, _BILLING]
    assert "$" not in draft.reply_body and "2493116" not in draft.reply_body
    assert draft.load_ids == []
    # The reviewer sees why.
    summary: ApprovalSummary = slack.approvals[0]["summary"]  # type: ignore[assignment]
    assert "fast track" in summary.handoff and "over 90 days" in summary.handoff
    assert _BILLING in summary.cc
    # And it is on the thread's record, reply text included.
    [entry] = store.history(_THREAD)
    assert (entry.ask, entry.action, entry.outcome) == ("action", "handoff", "awaiting_review")
    assert entry.cc == (_CAMIL, _BILLING)
    assert entry.reply == draft.reply_body


@pytest.mark.integration
def test_recourse_after_the_handoff_sends_nothing_more() -> None:
    """Telling Jake again who has it is the repeat being fixed. The colleagues get a card."""

    store = InMemoryFollowUpStore()
    pipeline, slack, llm = _pipeline(store)
    pipeline.process_email(_jake(_FAST_TRACK))

    result = pipeline.process_email(_jake(_RECOURSE, "<chase2@rtsfinancial.com>"))

    assert result.outcome is Outcome.ESCALATED
    assert result.follow_up_action == ACTION_NOTICE
    assert result.draft is None
    assert "handed off" in result.detail and "recourse" in result.detail
    assert _CAMIL in result.detail
    assert len(slack.approvals) == 1, "only the handoff's card — no second draft"
    assert len(slack.escalations) == 1
    assert llm.calls == []
    assert [e.action for e in store.history(_THREAD)] == [ACTION_HANDOFF, ACTION_NOTICE]


@pytest.mark.integration
def test_recourse_as_the_first_chase_is_handed_off_too() -> None:
    store = InMemoryFollowUpStore()
    pipeline, _, _ = _pipeline(store)

    result = pipeline.process_email(_jake(_RECOURSE))

    assert result.follow_up_action == ACTION_HANDOFF
    assert result.draft is not None
    assert "passed your message to our team" in result.draft.reply_body


# --- one status answer per thread ---------------------------------------------------
@pytest.mark.integration
def test_a_first_status_chase_is_still_answered_from_the_records() -> None:
    store = InMemoryFollowUpStore()
    pipeline, _, llm = _pipeline(store)
    email = sample_payment_status_email().model_copy(
        update={
            "body": "Any update on this?",
            "prior_reply": PriorReply(from_email=_CAMIL, body="Pending.", colleagues=(_CAMIL,)),
        }
    )

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.AWAITING_REVIEW, result.detail
    assert result.follow_up_action == ACTION_STATUS_UPDATE
    assert llm.calls
    [entry] = store.history(email.thread_id)
    assert entry.action == ACTION_STATUS_UPDATE
    assert entry.reply == PAYMENT_STATUS_DRAFT_BODY
    assert entry.loads == ("2462934",)


@pytest.mark.integration
def test_a_second_status_chase_goes_to_a_person() -> None:
    """Re-reading unchanged records would only produce the same reply again."""

    store = InMemoryFollowUpStore()
    email = sample_payment_status_email().model_copy(
        update={
            "message_id": "<second@x>",
            "body": "Any update on this?",
            "prior_reply": PriorReply(from_email=_CAMIL, body="Pending.", colleagues=(_CAMIL,)),
        }
    )
    store.record(
        email.thread_id,
        FollowUpRecord(
            message_id="<first@x>", ask="status", action=ACTION_STATUS_UPDATE,
            outcome="sent",
        ),
    )
    pipeline, slack, llm = _pipeline(store)

    result = pipeline.process_email(email)

    assert result.follow_up_action == ACTION_HANDOFF
    assert llm.calls == []
    assert "chased again" in slack.approvals[0]["summary"].handoff  # type: ignore[attr-defined]


@pytest.mark.integration
def test_a_handoff_with_nobody_to_copy_escalates_instead() -> None:
    """"They are copied" must be true, or the reply cannot be sent."""

    store = InMemoryFollowUpStore()
    pipeline, slack, _ = _pipeline(store, handoff_cc=())
    email = _jake(_FAST_TRACK).model_copy(
        update={"prior_reply": PriorReply(from_email=_CAMIL, body=_CAMIL_REPLY)}
    )

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.ESCALATED
    assert "nobody to copy" in result.detail and "FollowupHandoffCc" in result.detail
    assert slack.approvals == []


@pytest.mark.integration
def test_a_bank_change_in_a_follow_up_still_escalates_first() -> None:
    store = InMemoryFollowUpStore()
    pipeline, slack, _ = _pipeline(store)

    result = pipeline.process_email(
        _jake("Please expedite, and update our bank account to the new routing number.")
    )

    assert result.outcome is Outcome.ESCALATED
    assert result.detail.startswith("sensitive change")
    assert slack.approvals == []


# --- reading the ask ---------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("written", "ask"),
    [
        (_FAST_TRACK, FollowUpAsk.ACTION),
        (_RECOURSE, FollowUpAsk.ESCALATION),
        ("Can this be expedited?", FollowUpAsk.ACTION),
        ("This is 120+ days past due, please advise", FollowUpAsk.ACTION),
        ("We dispute the short pay on this load.", FollowUpAsk.ESCALATION),
        ("Any update on this?", FollowUpAsk.STATUS),
        ("URGENT - any update please", FollowUpAsk.STATUS),
        ("Idea Expedited here - any news?", FollowUpAsk.STATUS),
        ("Thank you!", FollowUpAsk.NONE),
    ],
)
def test_what_a_follow_up_asks(written: str, ask: FollowUpAsk) -> None:
    assert read_follow_up(written).ask is ask


@pytest.mark.unit
def test_the_evidence_names_what_decided_it() -> None:
    assert read_follow_up(_FAST_TRACK).evidence == ("fast track", "over 90 days")


@pytest.mark.unit
def test_a_bare_expedite_request_is_not_dropped_as_asking_nothing() -> None:
    """The Gmail client drops follow-ups that ask nothing; "please expedite" asks plenty."""

    email = _jake("Please expedite.")
    assert follow_up_asks(email)


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
    store.record(_THREAD, FollowUpRecord("<a>", "action", ACTION_HANDOFF, "escalated"))
    # The same message re-processed (escalations re-run every poll) updates, never appends.
    store.record(_THREAD, FollowUpRecord("<a>", "action", ACTION_HANDOFF, "awaiting_review"))
    store.record(_THREAD, FollowUpRecord("<b>", "escalation", ACTION_NOTICE, "escalated"))

    history = store.history(_THREAD)
    assert [(e.message_id, e.outcome) for e in history] == [
        ("<a>", "awaiting_review"),
        ("<b>", "escalated"),
    ]
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
