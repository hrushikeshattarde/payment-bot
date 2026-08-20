"""Unit tests for the Google Chat client and its cards.

What matters here is behavioural: buttons exist only in interactive mode (shadow mode
must not render clickable sends), a failed post never raises into the pipeline and is
recorded for the Gmail-draft fallback, and re-processed escalations dedup through the
ledger instead of reposting every 20 minutes.
"""

from __future__ import annotations

import json
from typing import Any

from payment_bot.approvals import ChatPostLedger, entry_id_for
from payment_bot.clients.google_chat import (
    ACTION_APPROVE,
    GoogleChatClient,
    approval_card,
    notice_card,
)
from payment_bot.clients.http import HttpResponse
from payment_bot.clients.slack import ApprovalSummary

SUMMARY = ApprovalSummary(
    from_="billing@carrier.test",
    intents=("payment_status",),
    load_ids=("2462934",),
    cc=("paystatus@circledelivers.com",),
)


class FakeHttp:
    def __init__(self, status: int = 200, payload: Any = None, *, boom: bool = False) -> None:
        self.status = status
        self.payload = payload if payload is not None else {"name": "spaces/S/messages/M1"}
        self.boom = boom
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        if self.boom:
            raise OSError("network down")
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        return HttpResponse(self.status, json.dumps(self.payload).encode())


class FakeTokens:
    def token(self) -> str:
        return "ya29.chat"


def _client(http: FakeHttp, *, interactive: bool = True, ledger: ChatPostLedger | None = None) -> GoogleChatClient:
    return GoogleChatClient(
        FakeTokens(),
        "spaces/S",
        reply_to="paystatus@circledelivers.com",
        interactive=interactive,
        post_ledger=ledger,
        transport=http,
    )


def _buttons(card: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for section in card["card"]["sections"]:
        for widget in section.get("widgets", []):
            for button in widget.get("buttonList", {}).get("buttons", []):
                texts.append(button["text"])
    return texts


# --- cards ---------------------------------------------------------------------
def test_interactive_card_carries_the_three_buttons_and_only_the_entry_id() -> None:
    card = approval_card(
        entry_id="e1",
        from_email="billing@carrier.test",
        load_ids=("2462934",),
        to="billing@carrier.test",
        cc=("paystatus@circledelivers.com",),
        reply_to="paystatus@circledelivers.com",
        body="Load 2462934 is BILLED.",
        interactive=True,
    )
    assert _buttons(card) == ["Approve & send as me", "Move to my Gmail Drafts", "Reject"]
    # Buttons carry the entry id and nothing else — content comes from the stored entry.
    rendered = json.dumps(card)
    assert '"key": "entry"' in rendered
    assert '"value": "e1"' in rendered
    assert ACTION_APPROVE in rendered


def test_shadow_and_status_cards_have_no_buttons() -> None:
    shadow = approval_card(
        entry_id="e1",
        from_email="a@x",
        load_ids=(),
        to="a@x",
        cc=(),
        reply_to="",
        body="text",
        interactive=False,
    )
    assert _buttons(shadow) == []
    assert "Shadow mode" in json.dumps(shadow)

    terminal = approval_card(
        entry_id="e1",
        from_email="a@x",
        load_ids=(),
        to="a@x",
        cc=(),
        reply_to="",
        body="text",
        interactive=True,
        status="Sent by p@x",
    )
    assert _buttons(terminal) == []
    assert "Sent by p@x" in json.dumps(terminal)


def test_long_bodies_are_trimmed_in_the_card_only() -> None:
    body = "x" * 5000
    card = approval_card(
        entry_id="e1", from_email="a@x", load_ids=(), to="a@x", cc=(), reply_to="",
        body=body, interactive=True,
    )
    rendered = json.dumps(card)
    assert "card view truncated" in rendered
    assert "x" * 4000 not in rendered


def test_notice_card_names_the_kind_and_the_short_reason() -> None:
    blocked = notice_card(
        correlation_id="<m1@x>",
        kind="blocked",
        severity="review",
        reason="pre-send gate blocked: ['tense_consistency: names a past date as future']",
        load_ids=("2462934",),
    )
    assert "Blocked by the pre-send gate" in json.dumps(blocked)
    assert "tense_consistency" in json.dumps(blocked)

    escalated = notice_card(
        correlation_id="<m2@x>",
        kind="escalated",
        severity="security",
        reason="sensitive change ['bank_redirect']",
        load_ids=(),
    )
    assert escalated["card"]["header"]["title"] == "Escalated — no draft"


# --- posting ---------------------------------------------------------------------
def test_post_approval_returns_the_message_name_and_records_it() -> None:
    http = FakeHttp()
    client = _client(http)

    post = client.post_approval("#ignored", SUMMARY, "Reply body", "<m1@x>")

    assert post.slack_ts == "spaces/S/messages/M1"
    assert client.posts["<m1@x>"] == "spaces/S/messages/M1"
    assert client.failed == set()
    sent = json.loads(http.requests[0]["body"])
    assert sent["thread"]["threadKey"] == entry_id_for("<m1@x>")
    assert "cardsV2" in sent


def test_a_failed_post_is_recorded_and_never_raises() -> None:
    client = _client(FakeHttp(boom=True))

    post = client.post_approval("#ignored", SUMMARY, "Reply body", "<m1@x>")

    assert post.slack_ts == ""
    assert "<m1@x>" in client.failed
    assert "<m1@x>" not in client.posts

    http_500 = FakeHttp(status=500, payload={"error": "boom"})
    client2 = _client(http_500)
    assert client2.post_approval("#ignored", SUMMARY, "x", "<m2@x>").slack_ts == ""
    assert "<m2@x>" in client2.failed


def test_escalations_dedup_through_the_ledger() -> None:
    http = FakeHttp()
    ledger = ChatPostLedger()
    client = _client(http, ledger=ledger)
    reason = "pre-send gate blocked: ['grounding']"

    client.post_escalation("#ignored", "review", reason, ("2462934",), "<m1@x>")
    client.post_escalation("#ignored", "review", reason, ("2462934",), "<m1@x>")

    assert len(http.requests) == 1
    assert ledger.posted("blocked", "<m1@x>")
    # A different kind for the same message still posts: block and escalation are
    # different facts about different runs.
    client.post_escalation("#ignored", "review", "no valid load id", (), "<m1@x>")
    assert len(http.requests) == 2


def test_update_status_patches_the_message() -> None:
    http = FakeHttp(payload={})
    client = _client(http)
    card = approval_card(
        entry_id="e1", from_email="a@x", load_ids=(), to="a@x", cc=(), reply_to="",
        body="text", status="EXPIRED",
    )

    assert client.update_status("spaces/S/messages/M1", card) is True
    request = http.requests[0]
    assert request["method"] == "PATCH"
    assert "updateMask=cardsV2" in request["url"]
