"""The standing "drafts awaiting approval" card.

Google Chat has no API for adding a tab to a space, so the tracker is the nearest thing it
does allow: one message the app rewrites in place every run, pinned once by a human. The
card is a view — it reports the queue and changes nothing about expiry or sending.

Why it is ordered by age rather than arrival: an approval nobody clicks is not a backlog
item that waits politely. The sweep expires it at ``approval_expiry_days`` and the draft is
then gone, while the intake query only reaches back ``newer_than``, so an aged-out draft has
about a day in which it could be redrafted at all — and only if the mail is still unread.
Measured on the live queue the day this was written: 86 waiting, 52 of them two days old
against a three-day expiry. Newest-first would have rendered that as a busy, healthy space.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from payment_bot.approvals import InMemoryApprovalStore, PendingApproval
from payment_bot.clients.google_chat import queue_card

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _entry(entry_id: str, *, hours_old: float, to: str, loads: tuple[str, ...] = ()) -> PendingApproval:
    return PendingApproval(
        entry_id=entry_id,
        message_id=f"<{entry_id}@mail>",
        thread_id="t",
        to=to,
        cc=(),
        reply_to="paystatus@circledelivers.com",
        subject=f"Payment status for {', '.join(loads) or 'a load'}",
        body="…",
        load_ids=loads,
        created_at=(NOW - timedelta(hours=hours_old)).isoformat(),
    )


def _texts(card: dict) -> list[str]:
    return [
        w["decoratedText"]["text"]
        for w in card["card"]["sections"][0]["widgets"]
        if "decoratedText" in w
    ]


def _labels(card: dict) -> list[str]:
    return [
        w["decoratedText"].get("topLabel", "")
        for w in card["card"]["sections"][0]["widgets"]
        if "decoratedText" in w
    ]


@pytest.mark.unit
def test_the_oldest_drafts_come_first() -> None:
    """The queue is read for what is about to be lost, not for what just arrived."""

    card = queue_card(
        [
            _entry("new", hours_old=1, to="new@carrier.com", loads=("2500001",)),
            _entry("old", hours_old=68, to="old@carrier.com", loads=("2400001",)),
            _entry("mid", hours_old=30, to="mid@carrier.com", loads=("2450001",)),
        ],
        now=NOW,
        expiry_days=3,
    )

    body = "\n".join(_texts(card))
    assert body.index("old@carrier.com") < body.index("mid@carrier.com")
    assert body.index("mid@carrier.com") < body.index("new@carrier.com")


@pytest.mark.unit
def test_the_count_leads_and_names_what_is_nearly_expired() -> None:
    entries = [_entry(f"e{i}", hours_old=60, to=f"a{i}@c.com") for i in range(4)]
    entries += [_entry("fresh", hours_old=2, to="fresh@c.com")]

    card = queue_card(entries, now=NOW, expiry_days=3)
    lead = _texts(card)[0]

    assert "<b>5</b> drafted replies" in lead
    # 60h old against a 72h expiry is inside the last 24 hours; the 2h one is not.
    assert "<b>4</b> within 24h of expiring" in lead


@pytest.mark.unit
def test_hours_remaining_are_shown_per_row_and_expiry_is_named() -> None:
    card = queue_card(
        [_entry("e", hours_old=60, to="a@c.com", loads=("2469115",))],
        now=NOW,
        expiry_days=3,
    )

    assert "12h left" in _labels(card)[1]
    assert "2d old" in _labels(card)[1]
    assert "2469115" in _texts(card)[1]


@pytest.mark.unit
def test_an_overdue_entry_reads_as_expired_not_as_negative_hours() -> None:
    card = queue_card([_entry("e", hours_old=100, to="a@c.com")], now=NOW, expiry_days=3)

    assert "EXPIRED" in _labels(card)[1]
    assert "-" not in _labels(card)[1].replace("·", "")


@pytest.mark.unit
def test_a_long_queue_is_capped_and_says_how_many_it_left_out() -> None:
    """Chat rejects an oversized card, so the cap is real — but it must never read as a
    shorter queue than there is. The count line is always the true total."""

    entries = [_entry(f"e{i}", hours_old=50 - i, to=f"a{i}@c.com") for i in range(30)]
    card = queue_card(entries, now=NOW, expiry_days=3, rows=12)

    assert "<b>30</b> drafted replies" in _texts(card)[0]
    assert "18 more" in _texts(card)[-1]
    # lead + 12 rows + the "not listed" line
    assert len(_texts(card)) == 14


@pytest.mark.unit
def test_an_empty_queue_says_so_rather_than_rendering_a_bare_header() -> None:
    card = queue_card([], now=NOW, expiry_days=3)

    assert "<b>0</b> drafted replies" in _texts(card)[0]
    assert "none near expiry" in _texts(card)[0]
    assert "Empty" in _texts(card)[1]


@pytest.mark.unit
def test_an_unparseable_timestamp_is_treated_as_new_and_never_invents_urgency() -> None:
    """A bad stamp must not hide the entry, and must not fake an expiry either."""

    broken = replace(_entry("e", hours_old=1, to="a@c.com"), created_at="not-a-date")

    card = queue_card([broken], now=NOW, expiry_days=3)

    assert "a@c.com" in "\n".join(_texts(card))
    assert "none near expiry" in _texts(card)[0]


@pytest.mark.unit
def test_each_row_links_to_the_clickers_own_copy_of_the_email() -> None:
    """rfc822msgid search, not a thread url — Gmail thread ids are per-mailbox."""

    card = queue_card([_entry("e", hours_old=5, to="a@c.com")], now=NOW, expiry_days=3)

    assert "rfc822msgid" in _texts(card)[1]
    assert "mail.google.com" in _texts(card)[1]


@pytest.mark.unit
def test_the_store_remembers_one_card_for_the_space() -> None:
    """Editing in place is what keeps the pin, so the name has to outlive the invocation."""

    store = InMemoryApprovalStore()
    assert store.tracker_message() == ""

    store.set_tracker_message("spaces/AAQAnvSk2WY/messages/abc")
    assert store.tracker_message() == "spaces/AAQAnvSk2WY/messages/abc"


@pytest.mark.unit
def test_upsert_edits_the_existing_card_and_only_posts_when_there_is_none() -> None:
    from payment_bot.clients.google_chat import GoogleChatClient
    from payment_bot.clients.http import HttpResponse

    class Transport:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def request(self, method: str, url: str, **kwargs: object) -> HttpResponse:
            self.calls.append(method)
            return HttpResponse(200, b'{"name": "spaces/S/messages/new"}')

    class Tokens:
        def token(self) -> str:
            return "t"

    transport = Transport()
    chat = GoogleChatClient(Tokens(), "spaces/S", transport=transport)  # type: ignore[arg-type]
    card = queue_card([], now=NOW, expiry_days=3)

    # No stored name: one POST, and the new name comes back to be stored.
    assert chat.upsert_tracker(card, "") == "spaces/S/messages/new"
    assert transport.calls == ["POST"]

    # Stored name: PATCH only, and the same name is kept — so the pin survives.
    assert chat.upsert_tracker(card, "spaces/S/messages/kept") == "spaces/S/messages/kept"
    assert transport.calls == ["POST", "PATCH"]


@pytest.mark.unit
def test_a_failed_edit_falls_back_to_posting_a_fresh_card() -> None:
    """A deleted tracker must not leave the space with no queue view at all."""

    from payment_bot.clients.google_chat import GoogleChatClient
    from payment_bot.clients.http import HttpResponse

    class Transport:
        def request(self, method: str, url: str, **kwargs: object) -> HttpResponse:
            if method == "PATCH":
                return HttpResponse(404, b'{"error": "not found"}')
            return HttpResponse(200, b'{"name": "spaces/S/messages/reposted"}')

    class Tokens:
        def token(self) -> str:
            return "t"

    chat = GoogleChatClient(Tokens(), "spaces/S", transport=Transport())  # type: ignore[arg-type]
    card = queue_card([], now=NOW, expiry_days=3)

    assert chat.upsert_tracker(card, "spaces/S/messages/gone") == "spaces/S/messages/reposted"
