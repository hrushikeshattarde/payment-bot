"""The escalation retry budget: an escalated thread stops being re-billed.

Escalations were the one no-reply outcome with no cap. They leave the thread with no draft
and no reply, so the Gmail thread-skip never fires on them, and ``newer_than:2d`` against a
30-minute schedule keeps the same message fetchable for ~96 runs. Every one of those runs
re-derived the identical verdict and paid for it.

Two shapes are covered, because they cost wildly different amounts:

* **Escalation before the model** — an unauthorized sender. Cheap per attempt, but it is the
  majority outcome (23 of 37 processed emails in the 2026-08-11 log), and it is no longer
  free: ``_filter_ids`` runs ahead of every escalation check, so any email carrying two or
  more id candidates pays a model call before reaching the refusal.
* **Escalation after the model** — the agent never calls ``submit_draft``. The whole agent
  loop has already run, nudges included, which makes this the most expensive repeat in the
  system and the one the gate-block ledger never covered.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.transport_pro_payloads import full_transport

from payment_bot.block_ledger import (
    KIND_AGENT_ESCALATION,
    KIND_BLOCK,
    KIND_ESCALATION,
    BlockLedger,
)
from payment_bot.clients import (
    LlmResponse,
    MockGmailClient,
    NullSlackClient,
    ScriptedLlmClient,
    TextBlock,
    TransportProHttpClient,
)
from payment_bot.config import Settings
from payment_bot.local_runner import _Clients, process_inbox
from payment_bot.models import InboundEmail
from payment_bot.pipeline import Outcome
from payment_bot.sample_data import sample_payment_status_email

pytestmark = pytest.mark.integration

#: Nobody on load 2462934's record, so ``check_authorization`` denies every load and the
#: pipeline escalates before selecting a skill.
STRANGER = "nobody@unknown-carrier.example"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "gmail_user": "paystatus@circledelivers.com",
        "slack_approval_channel": "#payments-approvals",
    }
    base.update(overrides)
    return Settings(**base)


def _email(from_email: str) -> InboundEmail:
    return sample_payment_status_email().model_copy(update={"from_email": from_email})


def _clients(email: InboundEmail, llm: ScriptedLlmClient) -> _Clients:
    """Fresh clients per simulated run, the way the Lambda builds them."""

    return _Clients(
        tp_factory=lambda: TransportProHttpClient(
            base_url="https://tp.example.test/api/v1",
            username="u",
            password="p",
            transport=full_transport(),
        ),
        gmail=MockGmailClient(inbox=[email]),
        slack=NullSlackClient(),
        llm=llm,
    )


def test_escalated_three_times_then_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    """Three attempts at an unauthorized sender, then the runs stop paying for the thread."""

    settings = _settings()  # escalation_retry_limit defaults to 3
    ledger = BlockLedger()
    email = _email(STRANGER)

    for expected in (1, 2, 3):
        llm = ScriptedLlmClient([])
        results = process_inbox(settings, clients=_clients(email, llm), block_ledger=ledger)
        assert [r.outcome for r in results] == [Outcome.ESCALATED]
        assert "not authorized" in results[0].detail
        assert ledger.blocks(email.message_id, kind=KIND_ESCALATION) == expected
        # This email names one load, so the id filter is below MIN_CANDIDATES and never runs.
        # The refusal is reached with no model call at all — which is what makes the *cost* of
        # this shape the re-scan rate rather than any single attempt.
        assert llm.calls == []
    assert ledger.dirty is True

    # Run 4: budget spent. Nothing is processed, and the mail stays unread and a human's.
    llm = ScriptedLlmClient([])
    results = process_inbox(settings, clients=_clients(email, llm), block_ledger=ledger)
    assert results == []
    assert llm.calls == []
    assert "retry budget spent" in capsys.readouterr().out


def test_the_agent_produced_no_draft_shape_is_capped() -> None:
    """The expensive repeat: the full loop runs, then escalates.

    The model answers in prose instead of calling ``submit_draft``, so the loop nudges twice
    and gives up — three model turns burned per attempt, on top of the intake tool calls, for
    an outcome the gate-block ledger never counted because no draft was ever produced.

    Counted under its own kind and its own, tighter budget. Priced in attempts alongside the
    pre-model escalations it cost three of the whole agent loop to reach the same cap that a
    refusal reaches for three id-filter calls.
    """

    settings = _settings()
    ledger = BlockLedger()
    email = _email(sample_payment_status_email().from_email)  # authorized: the loop runs

    def prose_llm() -> ScriptedLlmClient:
        return ScriptedLlmClient(
            [LlmResponse(stop_reason="end_turn", content=[TextBlock("Payment is scheduled.")])] * 3
        )

    spend: list[int] = []
    for expected in (1, 2):
        llm = prose_llm()
        results = process_inbox(settings, clients=_clients(email, llm), block_ledger=ledger)
        assert [r.outcome for r in results] == [Outcome.ESCALATED]
        assert "agent produced no draft" in results[0].detail
        assert ledger.blocks(email.message_id, kind=KIND_AGENT_ESCALATION) == expected
        spend.append(len(llm.calls))

    # Each attempt really did drive the model — the nudges included — which is the cost the
    # cap removes from every run after the second.
    assert spend == [3, 3]

    # The general escalation budget is untouched: this shape never spent any of it.
    assert ledger.blocks(email.message_id, kind=KIND_ESCALATION) == 0

    llm = prose_llm()
    assert process_inbox(settings, clients=_clients(email, llm), block_ledger=ledger) == []
    assert llm.calls == []


def test_the_expensive_escalation_stops_a_full_loop_sooner_than_the_cheap_one() -> None:
    """The point of splitting the budgets, stated as the number that matters.

    Same email, same failure, one fewer agent loop paid for. Under a single budget of 3 this
    shape cost three loops; the cheap escalations that budget was sized for still get three.
    """

    settings = _settings()
    ledger = BlockLedger()
    email = _email(sample_payment_status_email().from_email)

    loops = 0
    for _ in range(5):
        llm = ScriptedLlmClient(
            [LlmResponse(stop_reason="end_turn", content=[TextBlock("Payment is scheduled.")])] * 3
        )
        process_inbox(settings, clients=_clients(email, llm), block_ledger=ledger)
        if llm.calls:
            loops += 1

    assert loops == 2, "five polls must buy two agent loops, not three and not five"


def test_the_two_budgets_are_independent() -> None:
    """A spent escalation budget must not consume the gate-block budget, or vice versa.

    They are stored under different keys for this reason: an escalation and a gate block are
    different failures, and a message that escalated three times and is later answered — a
    roster entry added, say — deserves its full allowance of drafting attempts.
    """

    ledger = BlockLedger()
    for _ in range(3):
        ledger.record("msg-1", "escalated", kind=KIND_ESCALATION)

    assert ledger.blocks("msg-1", kind=KIND_ESCALATION) == 3
    assert ledger.blocks("msg-1", kind=KIND_BLOCK) == 0
    assert ledger.exhausted("msg-1", 3, kind=KIND_ESCALATION) is True
    assert ledger.exhausted("msg-1", 2, kind=KIND_BLOCK) is False


def test_zero_limit_keeps_todays_behaviour() -> None:
    settings = _settings(escalation_retry_limit=0)
    ledger = BlockLedger()
    email = _email(STRANGER)
    for _ in range(5):
        ledger.record(email.message_id, "r", kind=KIND_ESCALATION)

    results = process_inbox(
        settings, clients=_clients(email, ScriptedLlmClient([])), block_ledger=ledger
    )
    assert [r.outcome for r in results] == [Outcome.ESCALATED]


def test_no_ledger_means_no_suppression() -> None:
    """Local runs pass no ledger and must behave exactly as before this feature."""

    email = _email(STRANGER)
    for _ in range(4):
        results = process_inbox(_settings(), clients=_clients(email, ScriptedLlmClient([])))
        assert [r.outcome for r in results] == [Outcome.ESCALATED]


def test_a_ledger_survives_a_round_trip_with_both_kinds() -> None:
    """The Lambda persists this as JSON in S3 between runs, so both kinds must reload.

    The block entries keep their unprefixed keys deliberately — that is the shape already in
    the live bucket, and prefixing them would drop every counter in flight at the next load.
    """

    ledger = BlockLedger()
    ledger.record("msg-1", "gate said no", kind=KIND_BLOCK)
    ledger.record("msg-1", "not authorized", kind=KIND_ESCALATION)
    ledger.record("msg-2", "not authorized", kind=KIND_ESCALATION)

    reloaded = BlockLedger.from_json(ledger.to_json())

    assert reloaded.blocks("msg-1", kind=KIND_BLOCK) == 1
    assert reloaded.blocks("msg-1", kind=KIND_ESCALATION) == 1
    assert reloaded.blocks("msg-2", kind=KIND_ESCALATION) == 1
    assert reloaded.blocks("msg-2", kind=KIND_BLOCK) == 0
    # Unprefixed for blocks, so an entry written before this feature still reads back.
    assert "msg-1" in reloaded.entries
    assert f"{KIND_ESCALATION}:msg-1" in reloaded.entries


# ---------------------------------------------------------------------------
# The id filter moved behind authorization. What that buys, and what it costs.
# ---------------------------------------------------------------------------
def _multi_id_email(from_email: str) -> InboundEmail:
    """Two candidates, so the filter clears MIN_CANDIDATES and would have been called."""

    return sample_payment_status_email().model_copy(
        update={
            "from_email": from_email,
            "body": "Payment status for loads 2462934 and 2499505 please.",
        }
    )


def test_a_refused_email_still_pays_for_the_id_filter_on_purpose() -> None:
    """The cost saving here was taken back, deliberately, and this records why.

    The filter briefly ran AFTER authorization so a refused email — the majority outcome —
    paid no model call. What that bought was worse than what it saved: with the filter behind
    authorization, phantom candidates reach Transport Pro and get named in the escalation as
    loads the sender asked about. A WEX collections table put the carrier's motor-carrier
    number 1133075 beside load 2480506 and the refusal listed both; an OTR rate confirmation
    did the same with an MC and a DOT number.

    Knowing WHICH load and WHICH carrier an email is about is worth more than the call, and
    MIN_CANDIDATES keeps single-id mail free either way.
    """

    settings = _settings(llm_id_filter="enforce")
    llm = ScriptedLlmClient([])
    email = _multi_id_email(STRANGER)

    results = process_inbox(settings, clients=_clients(email, llm))

    assert [r.outcome for r in results] == [Outcome.ESCALATED]
    assert "not authorized" in results[0].detail
    # One call, and it failed closed on an exhausted script — the point is that it was made.
    assert len(llm.calls) == 1


def test_the_filter_is_still_called_when_the_email_is_answerable() -> None:
    """Moving it must not mean losing it: an authorized email still gets filtered."""

    settings = _settings(llm_id_filter="shadow")
    # The loop is never reached — the filter's own call is the first, and it is enough to
    # prove the filter ran. A response it cannot parse falls back to the regex, by design.
    llm = ScriptedLlmClient(
        [LlmResponse(stop_reason="end_turn", content=[TextBlock("not a tool call")])] * 6
    )
    email = _multi_id_email(sample_payment_status_email().from_email)

    process_inbox(settings, clients=_clients(email, llm))

    assert llm.calls, "an answerable email must still be filtered"


def test_a_table_of_reference_numbers_is_filtered_before_it_can_trip_the_portal() -> None:
    """The cost of moving the filter, paid back where it would otherwise have shown.

    A WEX-shaped collections table writes MC numbers, an account number and an invoice number
    beside the one real load. Six of those in the BODY clear the bulk threshold, so with the
    filter deferred to after authorization the count that decided the portal fallback was the
    unfiltered one — and a one-load question got a portal link. Caught by probe, not by the
    suite, which is why it is pinned here.
    """

    from payment_bot.clients import ToolUseBlock

    rows = [
        {"value": "1601899", "kind": "mc_number", "why": "under the Mot Car column"},
        {"value": "761291", "kind": "mc_number", "why": "second Mot Car column"},
        {"value": "1229475", "kind": "account", "why": "the Account cell"},
        {"value": "2869463", "kind": "invoice", "why": "the Invoice cell"},
        {"value": "9132447", "kind": "reference", "why": "a billing reference"},
        {"value": "2462934", "kind": "load", "why": "under the Load column"},
    ]
    verdict = LlmResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                tool_use_id="t1",
                name="report_identifier_kinds",
                input={"identifiers": rows},
            )
        ],
    )
    email = sample_payment_status_email().model_copy(
        update={
            "subject": "statement",
            "body": (
                "Carrier | Mot Car | Account | Invoice | Load | Age\n"
                "1601899 761291 1229475 2869463 9132447 2462934\n"
            ),
        }
    )
    llm = ScriptedLlmClient([verdict])

    results = process_inbox(
        _settings(llm_id_filter="enforce"), clients=_clients(email, llm)
    )

    body = results[0].draft.reply_body if results and results[0].draft else ""
    assert "payment-status-lookup" not in body, "six reference numbers are not six loads"
    assert llm.calls, "the filter must run when it is what decides the portal fallback"
