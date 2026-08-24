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

import json
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


# --- the date nav bar -------------------------------------------------------
#
# Asked for in these words: "make it easy for the users. They are not from technical
# background so like a navigation bar where they can select unanswered emails date wise."
#
# So: chips, not a slash command. A command you have to know to type is the worst possible
# affordance for someone who does not live in developer tools, and App Home would move the
# queue out of the space the team already works in.


def _buttons(card: dict) -> list[dict]:
    for w in card["card"]["sections"][0]["widgets"]:
        if "buttonList" in w:
            return w["buttonList"]["buttons"]
    return []


@pytest.mark.unit
def test_the_nav_bar_offers_every_day_with_its_own_count() -> None:
    entries = [
        _entry("a", hours_old=2, to="today1@c.com"),
        _entry("b", hours_old=5, to="today2@c.com"),
        _entry("c", hours_old=30, to="yesterday@c.com"),
        _entry("d", hours_old=60, to="older1@c.com"),
        _entry("e", hours_old=70, to="older2@c.com"),
    ]

    labels = [b["text"] for b in _buttons(queue_card(entries, now=NOW, expiry_days=3, interactive=True))]

    assert labels == ["● All (5)", "Today (2)", "Yesterday (1)", "2 days + (2)"]


@pytest.mark.unit
def test_selecting_a_day_shows_only_that_day_and_says_so() -> None:
    entries = [
        _entry("a", hours_old=2, to="today@c.com"),
        _entry("c", hours_old=30, to="yesterday@c.com"),
    ]

    card = queue_card(entries, now=NOW, expiry_days=3, bucket="yesterday", interactive=True)
    body = "\n".join(_texts(card))

    assert "yesterday@c.com" in body
    assert "today@c.com" not in body
    # The true total still leads, so a filtered card can never read as the whole queue.
    assert "<b>2</b> drafted replies" in _texts(card)[0]
    assert "Showing <b>1</b> from yesterday" in _texts(card)[0]


@pytest.mark.unit
def test_the_selected_chip_is_marked_and_not_clickable_again() -> None:
    card = queue_card(
        [_entry("a", hours_old=30, to="a@c.com")],
        now=NOW,
        expiry_days=3,
        bucket="yesterday",
        interactive=True,
    )
    chips = {b["text"].lstrip("● "): b for b in _buttons(card)}

    assert chips["Yesterday (1)"]["disabled"] is True
    assert chips["All (1)"]["disabled"] is False


@pytest.mark.unit
def test_an_empty_day_says_so_rather_than_looking_like_an_empty_queue() -> None:
    """The trap this avoids: 'Today (0)' rendering as though nothing is outstanding."""

    card = queue_card(
        [_entry("a", hours_old=60, to="old@c.com")],
        now=NOW,
        expiry_days=3,
        bucket="today",
        interactive=True,
    )

    assert "No unanswered drafts from that day" in "\n".join(_texts(card))
    assert "<b>1</b> drafted reply" in _texts(card)[0]


@pytest.mark.unit
def test_a_chip_carries_the_callback_url_not_a_bare_verb() -> None:
    """The add-ons runtime sends the click to whatever `function` names, so a bare verb
    reaches an endpoint literally called "queue_filter" — the live lesson behind
    _action_button's comment."""

    card = queue_card(
        [_entry("a", hours_old=2, to="a@c.com")],
        now=NOW,
        expiry_days=3,
        action_url="https://cb.example.test/",
        interactive=True,
    )
    chip = _buttons(card)[1]

    assert chip["onClick"]["action"]["function"] == "https://cb.example.test/"
    params = {p["key"]: p["value"] for p in chip["onClick"]["action"]["parameters"]}
    assert params == {"action": "queue_filter", "bucket": "today"}


@pytest.mark.unit
def test_a_non_interactive_tracker_has_no_chips_at_all() -> None:
    """Shadow mode posts the same card without controls, like the approval card does."""

    card = queue_card([_entry("a", hours_old=2, to="a@c.com")], now=NOW, expiry_days=3)

    assert _buttons(card) == []


@pytest.mark.unit
def test_an_unknown_bucket_falls_back_to_all_rather_than_showing_nothing() -> None:
    card = queue_card(
        [_entry("a", hours_old=2, to="a@c.com")], now=NOW, expiry_days=3, bucket="last-tuesday"
    )

    assert "a@c.com" in "\n".join(_texts(card))
    assert "Showing" not in _texts(card)[0]


@pytest.mark.unit
def test_a_chip_click_reads_the_queue_fresh_and_redraws_in_place() -> None:
    """A reviewer may tap a card minutes old; showing them a stale list under a filter they
    just chose is what would make the nav bar untrustworthy."""

    from payment_bot.chat_callback import _click_param, _queue_response
    from payment_bot.config import Settings

    store = InMemoryApprovalStore()
    store.put_pending(_entry("fresh", hours_old=1, to="fresh@c.com"))
    store.put_pending(_entry("old", hours_old=60, to="old@c.com"))

    event = {
        "commonEventObject": {"parameters": {"action": "queue_filter", "bucket": "older"}}
    }
    assert _click_param(event, "bucket") == "older"

    response = _queue_response(store, Settings(_env_file=None), "older", False)
    body = json.loads(response["body"])

    assert body["actionResponse"]["type"] == "UPDATE_MESSAGE"
    rendered = json.dumps(body["cardsV2"][0])
    assert "old@c.com" in rendered
    assert "fresh@c.com" not in rendered


@pytest.mark.unit
def test_a_legacy_schema_click_carries_its_parameters_too() -> None:
    from payment_bot.chat_callback import _click_param

    legacy = {
        "action": {
            "parameters": [{"key": "action", "value": "queue_filter"}, {"key": "bucket", "value": "today"}]
        }
    }

    assert _click_param(legacy, "bucket") == "today"
    assert _click_param(legacy, "nothing") == ""
