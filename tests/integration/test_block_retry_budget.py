"""The gate-block retry budget: two strikes, then the run stops paying for the thread.

A gate-blocked email saves no draft, so the stateless re-scan re-runs the full agent loop
every run until a human intervenes — measured live at ~48 retries a night for one thread
(G.H. Factor, load 302618). With a ledger wired in, the third run skips the loop entirely:
the mail stays unread and a human's, it just stops being re-billed.

The blocked flow here is the ordinary payment-status script whose final draft mentions a
tool name — the ``tool_mentions`` check fails deterministically, which is the cheapest way
to manufacture a real BLOCKED outcome end to end.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.integration.test_draft_only_flow import (
    SAMPLE_SENDER_EMAIL,
    FakeGroqTransport,
    _groq_payment_status_script,
    _tool_turn,
)
from tests.transport_pro_payloads import full_transport

from payment_bot.block_ledger import BlockLedger
from payment_bot.clients import (
    GroqLlmClient,
    MockGmailClient,
    NullSlackClient,
    TransportProHttpClient,
)
from payment_bot.config import Settings
from payment_bot.local_runner import _Clients, process_inbox
from payment_bot.pipeline import Outcome
from payment_bot.sample_data import PAYMENT_STATUS_DRAFT_BODY, sample_payment_status_email

pytestmark = pytest.mark.integration

CC = ("hrushikesh.attarde@circledelivers.com",)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "reply_cc": CC,
        "gmail_user": "paystatus@circledelivers.com",
        "slack_approval_channel": "#payments-approvals",
    }
    base.update(overrides)
    return Settings(**base)


def _blocked_script() -> list[dict[str, Any]]:
    """The payment-status sequence, ending in a draft the gate must refuse."""

    script = _groq_payment_status_script()
    script[-1] = _tool_turn(
        "c7",
        "submit_draft",
        {
            # An amount no tool ever reported: the grounding check fails deterministically.
            # (Tool names won't do — submit_draft strips those before the gate looks.)
            "reply_body": PAYMENT_STATUS_DRAFT_BODY + "\nA processing fee of $123.45 applies.",
            "to": SAMPLE_SENDER_EMAIL,
            "load_ids": ["2462934"],
            "citations": [
                {"fact": "total pending", "value": "$4,650", "source_tool": "tp_get_load_summary"},
                {
                    "fact": "scheduled pay date",
                    "value": "2026-08-20",
                    "source_tool": "compute_scheduled_pay_date",
                },
            ],
        },
    )
    return script


def _clients() -> _Clients:
    """Fresh clients per simulated run, the way the Lambda builds them."""

    return _Clients(
        tp_factory=lambda: TransportProHttpClient(
            base_url="https://tp.example.test/api/v1",
            username="u",
            password="p",
            transport=full_transport(),
        ),
        gmail=MockGmailClient(inbox=[sample_payment_status_email()]),
        slack=NullSlackClient(),
        llm=GroqLlmClient(
            api_key="gsk_test",
            transport=FakeGroqTransport(_blocked_script()),
            sleep=lambda _s: None,
        ),
    )


def test_blocked_twice_then_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings()  # gate_block_retry_limit defaults to 2
    ledger = BlockLedger()
    message_id = sample_payment_status_email().message_id

    # Runs 1 and 2: the full loop runs, the gate blocks, the ledger counts.
    for expected in (1, 2):
        results = process_inbox(settings, clients=_clients(), block_ledger=ledger)
        assert [r.outcome for r in results] == [Outcome.BLOCKED]
        assert ledger.blocks(message_id) == expected
    assert ledger.dirty is True

    # Run 3: the budget is spent — no processing, no model turns, the mail stays a human's.
    results = process_inbox(settings, clients=_clients(), block_ledger=ledger)
    assert results == []
    out = capsys.readouterr().out
    assert "retry budget spent" in out


def test_zero_limit_keeps_todays_behaviour() -> None:
    settings = _settings(gate_block_retry_limit=0)
    ledger = BlockLedger()
    for _ in range(3):
        ledger.record(sample_payment_status_email().message_id, "r")

    results = process_inbox(settings, clients=_clients(), block_ledger=ledger)
    assert [r.outcome for r in results] == [Outcome.BLOCKED]


def test_no_ledger_means_no_suppression() -> None:
    """Local runs pass no ledger and must behave exactly as before this feature."""

    results = process_inbox(_settings(), clients=_clients())
    assert [r.outcome for r in results] == [Outcome.BLOCKED]
