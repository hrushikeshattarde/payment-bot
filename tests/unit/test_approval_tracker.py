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
from payment_bot.clients.google_chat import queue_cards
from payment_bot.lambda_handler import _refresh_queue_tracker

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


def _widgets(cards: list[dict]) -> list[dict]:
    """Every widget across the message, in order. The tracker spans several cards once the
    queue outgrows one — see _split_cards — and a reader of the message cannot tell where a
    card boundary fell, so neither should these assertions."""

    return [w for c in cards for w in c["card"]["sections"][0]["widgets"]]


def _texts(cards: list[dict]) -> list[str]:
    return [w["decoratedText"]["text"] for w in _widgets(cards) if "decoratedText" in w]


def _labels(cards: list[dict]) -> list[str]:
    return [
        w["decoratedText"].get("topLabel", "")
        for w in _widgets(cards)
        if "decoratedText" in w
    ]


@pytest.mark.unit
def test_the_oldest_drafts_come_first() -> None:
    """The queue is read for what is about to be lost, not for what just arrived."""

    card = queue_cards(
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

    card = queue_cards(entries, now=NOW, expiry_days=3)
    lead = _texts(card)[0]

    assert "<b>5</b> drafted replies" in lead
    # 60h old against a 72h expiry is inside the last 24 hours; the 2h one is not.
    assert "<b>4</b> within 24h of expiring" in lead


@pytest.mark.unit
def test_hours_remaining_are_shown_per_row_and_expiry_is_named() -> None:
    card = queue_cards(
        [_entry("e", hours_old=60, to="a@c.com", loads=("2469115",))],
        now=NOW,
        expiry_days=3,
    )

    assert "12h left" in _labels(card)[1]
    assert "2d old" in _labels(card)[1]
    assert "2469115" in _texts(card)[1]


@pytest.mark.unit
def test_an_overdue_entry_reads_as_expired_not_as_negative_hours() -> None:
    card = queue_cards([_entry("e", hours_old=100, to="a@c.com")], now=NOW, expiry_days=3)

    assert "EXPIRED" in _labels(card)[1]
    assert "-" not in _labels(card)[1].replace("·", "")


@pytest.mark.unit
def test_a_capped_card_says_where_the_page_sits_in_the_whole() -> None:
    """Chat rejects an oversized card, so the cap is real — but it must never read as a
    shorter queue than there is. The count line is always the true total, and the page line
    says which slice of it this is."""

    entries = [_entry(f"e{i}", hours_old=50 - i, to=f"a{i}@c.com") for i in range(30)]
    card = queue_cards(entries, now=NOW, expiry_days=3, rows=12)

    assert "<b>30</b> drafted replies" in _texts(card)[0]
    assert "Showing <b>1-12</b> of <b>30</b>" in _texts(card)[-1]
    # lead + 12 rows + the page line
    assert len(_texts(card)) == 14
    assert len(card) == 1, "twelve rows fit in one card"


@pytest.mark.unit
def test_an_empty_queue_says_so_rather_than_rendering_a_bare_header() -> None:
    card = queue_cards([], now=NOW, expiry_days=3)

    assert "<b>0</b> drafted replies" in _texts(card)[0]
    assert "none near expiry" in _texts(card)[0]
    assert "Empty" in _texts(card)[1]


@pytest.mark.unit
def test_an_unparseable_timestamp_is_treated_as_new_and_never_invents_urgency() -> None:
    """A bad stamp must not hide the entry, and must not fake an expiry either."""

    broken = replace(_entry("e", hours_old=1, to="a@c.com"), created_at="not-a-date")

    card = queue_cards([broken], now=NOW, expiry_days=3)

    assert "a@c.com" in "\n".join(_texts(card))
    assert "none near expiry" in _texts(card)[0]


@pytest.mark.unit
def test_each_row_links_to_the_clickers_own_copy_of_the_email() -> None:
    """rfc822msgid search, not a thread url — Gmail thread ids are per-mailbox."""

    card = queue_cards([_entry("e", hours_old=5, to="a@c.com")], now=NOW, expiry_days=3)

    assert "rfc822msgid" in _texts(card)[1]
    assert "mail.google.com" in _texts(card)[1]


@pytest.mark.unit
def test_the_store_remembers_the_card_and_its_failed_edits() -> None:
    """Editing in place is what keeps the pin, so the name has to outlive the invocation —
    and so does the failure count that decides when to stop trusting it."""

    store = InMemoryApprovalStore()
    assert store.tracker() == ("", 0)

    store.set_tracker("spaces/AAQAnvSk2WY/messages/abc")
    assert store.tracker() == ("spaces/AAQAnvSk2WY/messages/abc", 0)

    store.set_tracker("spaces/AAQAnvSk2WY/messages/abc", 2)
    assert store.tracker() == ("spaces/AAQAnvSk2WY/messages/abc", 2)


@pytest.mark.unit
def test_patch_reports_the_status_so_failures_can_be_told_apart() -> None:
    """A 503 is worth retrying on the same message; a 403 eventually is not. A bool cannot
    distinguish them, which is why the primitive returns the status."""

    card = queue_cards([], now=NOW, expiry_days=3)
    for status in (200, 403, 503):
        chat, transport = _chat(status)
        assert chat.patch_card("spaces/S/messages/m", card) == status
        # The sweep's bool contract is unchanged: only 200 counts as updated.
        assert chat.update_status("spaces/S/messages/m", card) is (status == 200)
        assert transport.calls == ["PATCH", "PATCH"]

    chat, transport = _chat(200)
    assert chat.patch_card("", card) == 0
    assert transport.calls == []


@pytest.mark.unit
def test_a_failed_edit_does_not_post_a_second_tracker() -> None:
    """The regression. Live on 2026-08-24: the PATCH came back 403, the handler treated that
    as "the card is gone" and posted a fresh one, and the space acquired a second tracker
    fifteen minutes after the first. A failed edit is not evidence the message is gone —
    Chat returns 503 for an outage and 403 for both a deleted and a foreign message — so the
    name is kept and retried instead."""

    store = InMemoryApprovalStore()
    store.set_tracker("spaces/S/messages/first")
    chat, transport = _chat(403)

    _refresh_queue_tracker(store, chat, _settings())

    assert transport.calls == ["PATCH"], "a failed edit must never POST"
    assert store.tracker() == ("spaces/S/messages/first", 1)


@pytest.mark.unit
def test_a_successful_edit_clears_the_failure_count() -> None:
    store = InMemoryApprovalStore()
    store.set_tracker("spaces/S/messages/first", 3)
    chat, transport = _chat(200)

    _refresh_queue_tracker(store, chat, _settings())

    assert transport.calls == ["PATCH"]
    assert store.tracker() == ("spaces/S/messages/first", 0)


@pytest.mark.unit
def test_the_card_is_abandoned_only_after_repeated_refusals() -> None:
    """Four failures is an hour at this cadence: long enough that an outage costs nothing,
    short enough that a genuinely deleted card is replaced the same morning. Abandoning
    clears the name so the NEXT run posts — never the same run, or one bad edit would still
    produce a card immediately."""

    store = InMemoryApprovalStore()
    store.set_tracker("spaces/S/messages/first", 3)
    chat, transport = _chat(403)

    _refresh_queue_tracker(store, chat, _settings())

    assert transport.calls == ["PATCH"], "abandoning must not post in the same run"
    assert store.tracker() == ("", 0)

    # Only now, with no name held, does a run post a fresh card.
    chat2, transport2 = _chat(200, post_name="spaces/S/messages/second")
    _refresh_queue_tracker(store, chat2, _settings())
    assert transport2.calls == ["POST"]
    assert store.tracker() == ("spaces/S/messages/second", 0)


@pytest.mark.unit
def test_the_first_ever_refresh_posts_and_remembers_the_name() -> None:
    store = InMemoryApprovalStore()
    chat, transport = _chat(200, post_name="spaces/S/messages/new")

    _refresh_queue_tracker(store, chat, _settings())

    assert transport.calls == ["POST"]
    assert store.tracker() == ("spaces/S/messages/new", 0)


# --- the date nav bar -------------------------------------------------------
#
# Asked for in these words: "make it easy for the users. They are not from technical
# background so like a navigation bar where they can select unanswered emails date wise."
#
# So: chips, not a slash command. A command you have to know to type is the worst possible
# affordance for someone who does not live in developer tools, and App Home would move the
# queue out of the space the team already works in.


def _chat(status: int, post_name: str = "spaces/S/messages/new"):
    """A GoogleChatClient over a transport that records methods and answers one status."""

    from payment_bot.clients.google_chat import GoogleChatClient
    from payment_bot.clients.http import HttpResponse

    class Transport:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def request(self, method: str, url: str, **kwargs: object) -> HttpResponse:
            self.calls.append(method)
            if method == "POST":
                return HttpResponse(status, json.dumps({"name": post_name}).encode())
            return HttpResponse(status, b"{}")

    class Tokens:
        def token(self) -> str:
            return "t"

    transport = Transport()
    return GoogleChatClient(Tokens(), "spaces/S", transport=transport), transport  # type: ignore[arg-type]


def _settings():
    from payment_bot.config import Settings

    return Settings(_env_file=None)


def _buttons(cards: list[dict]) -> list[dict]:
    """The date chips — the FIRST button list in the message."""

    for w in _widgets(cards):
        if "buttonList" in w:
            return w["buttonList"]["buttons"]
    return []


def _page_buttons(cards: list[dict]) -> list[dict]:
    """The Newer/Older buttons — the LAST button list, below the rows."""

    lists = [w for w in _widgets(cards) if "buttonList" in w]
    return lists[-1]["buttonList"]["buttons"] if len(lists) > 1 else []


@pytest.mark.unit
def test_the_nav_bar_offers_every_day_with_its_own_count() -> None:
    entries = [
        _entry("a", hours_old=2, to="today1@c.com"),
        _entry("b", hours_old=5, to="today2@c.com"),
        _entry("c", hours_old=30, to="yesterday@c.com"),
        _entry("d", hours_old=60, to="older1@c.com"),
        _entry("e", hours_old=70, to="older2@c.com"),
    ]

    labels = [b["text"] for b in _buttons(queue_cards(entries, now=NOW, expiry_days=3, action_url="https://cb/", interactive=True))]

    assert labels == ["● All (5)", "Today (2)", "Yesterday (1)", "2 days + (2)"]


@pytest.mark.unit
def test_selecting_a_day_shows_only_that_day_and_says_so() -> None:
    entries = [
        _entry("a", hours_old=2, to="today@c.com"),
        _entry("c", hours_old=30, to="yesterday@c.com"),
    ]

    card = queue_cards(entries, now=NOW, expiry_days=3, bucket="yesterday", action_url="https://cb/", interactive=True)
    body = "\n".join(_texts(card))

    assert "yesterday@c.com" in body
    assert "today@c.com" not in body
    # The true total still leads, so a filtered card can never read as the whole queue.
    assert "<b>2</b> drafted replies" in _texts(card)[0]
    assert "<b>1</b> from yesterday" in _texts(card)[0]


@pytest.mark.unit
def test_the_selected_chip_is_marked_and_not_clickable_again() -> None:
    card = queue_cards(
        [_entry("a", hours_old=30, to="a@c.com")],
        now=NOW,
        expiry_days=3,
        bucket="yesterday",
        action_url="https://cb/", interactive=True,
    )
    chips = {b["text"].lstrip("● "): b for b in _buttons(card)}

    assert chips["Yesterday (1)"]["disabled"] is True
    assert chips["All (1)"]["disabled"] is False


@pytest.mark.unit
def test_an_empty_day_says_so_rather_than_looking_like_an_empty_queue() -> None:
    """The trap this avoids: 'Today (0)' rendering as though nothing is outstanding."""

    card = queue_cards(
        [_entry("a", hours_old=60, to="old@c.com")],
        now=NOW,
        expiry_days=3,
        bucket="today",
        action_url="https://cb/", interactive=True,
    )

    assert "No unanswered drafts from that day" in "\n".join(_texts(card))
    assert "<b>1</b> drafted reply" in _texts(card)[0]


@pytest.mark.unit
def test_a_chip_carries_the_callback_url_not_a_bare_verb() -> None:
    """The add-ons runtime sends the click to whatever `function` names, so a bare verb
    reaches an endpoint literally called "queue_filter" — the live lesson behind
    _action_button's comment."""

    card = queue_cards(
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

    card = queue_cards([_entry("a", hours_old=2, to="a@c.com")], now=NOW, expiry_days=3)

    assert _buttons(card) == []


@pytest.mark.unit
def test_an_unknown_bucket_falls_back_to_all_rather_than_showing_nothing() -> None:
    card = queue_cards(
        [_entry("a", hours_old=2, to="a@c.com")], now=NOW, expiry_days=3, bucket="last-tuesday"
    )

    assert "a@c.com" in "\n".join(_texts(card))
    assert "Showing" not in _texts(card)[0]


@pytest.mark.unit
def test_a_chip_click_reads_the_queue_fresh_and_redraws_in_place() -> None:
    """A reviewer may tap a card minutes old; showing them a stale list under a filter they
    just chose is what would make the nav bar untrustworthy."""

    from payment_bot.chat_callback import _queue_response
    from payment_bot.config import Settings

    # _queue_response reads the real clock, so these ages must be relative to it — anchoring
    # them to NOW made the test pass on the day it was written and silently mis-bucket after.
    real_now = datetime.now(UTC)
    store = InMemoryApprovalStore()
    for entry_id, hours, to in (("fresh", 1, "fresh@c.com"), ("old", 60, "old@c.com")):
        store.put_pending(
            replace(
                _entry(entry_id, hours_old=0, to=to),
                created_at=(real_now - timedelta(hours=hours)).isoformat(),
            )
        )

    body = json.loads(_queue_response(store, Settings(_env_file=None), "older", False)["body"])

    assert body["actionResponse"]["type"] == "UPDATE_MESSAGE"
    rendered = json.dumps(body["cardsV2"][0])
    assert "old@c.com" in rendered
    assert "fresh@c.com" not in rendered


@pytest.mark.unit
def test_a_click_parameter_is_read_whether_it_is_a_map_or_a_list() -> None:
    """The live bug. Chat sends a parameter block as a mapping in some payloads and as a
    list of key/value objects in others. Handling only the mapping failed silently in the
    worst way: the verb still parsed, the click still dispatched, and the bucket came back
    empty — so every chip rendered `all` and the nav bar looked inert."""

    from payment_bot.chat_callback import _click_param

    as_map = {"commonEventObject": {"parameters": {"action": "queue_filter", "bucket": "today"}}}
    as_list = {
        "commonEventObject": {
            "parameters": [
                {"key": "action", "value": "queue_filter"},
                {"key": "bucket", "value": "yesterday"},
            ]
        }
    }
    legacy = {
        "action": {
            "parameters": [
                {"key": "action", "value": "queue_filter"},
                {"key": "bucket", "value": "older"},
            ]
        }
    }

    assert _click_param(as_map, "bucket") == ("today", "commonEventObject")
    assert _click_param(as_list, "bucket") == ("yesterday", "commonEventObject")
    assert _click_param(legacy, "bucket") == ("older", "action")
    assert _click_param(as_map, "nothing") == ("", "none")
    assert _click_param({}, "bucket") == ("", "none")


@pytest.mark.unit
def test_the_echoed_card_is_dropped_so_the_click_payload_survives_the_log_cap() -> None:
    """The reason the bug took two rounds to find. A click echoes the whole rendered card
    back, which alone exceeded the 2000-char cap — and since the dump is key-sorted,
    `commonEventObject` sorted last and was the first thing lost. The one field needed to
    debug a click was the one field never logged."""

    from payment_bot.chat_callback import _sanitised

    event = {
        "chat": {
            "buttonClickedPayload": {"message": {"cardsV2": [{"card": {"x": "y" * 5000}}]}},
            "user": {"email": "a@b.com"},
        },
        "commonEventObject": {"parameters": {"bucket": "yesterday"}},
    }

    dumped = _sanitised(event)

    assert "echoed card dropped" in dumped
    assert "yyyy" not in dumped
    # The whole point: the click parameters are still in there.
    assert "yesterday" in dumped


@pytest.mark.unit
def test_the_tracker_is_posted_unthreaded_so_it_can_be_pinned() -> None:
    """Pinning applies to a message, so a threaded tracker is unpinnable — and a FIXED
    threadKey is worse than useless: with REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD every repost
    after the first lands as a reply inside the old thread.

    This shipped briefly, chasing a 403 that PATCH kept returning on tracker messages. The
    403 was simply a card someone had deleted — Chat reports deleted and forbidden alike.
    Approval cards can use that option safely because each carries a UNIQUE key per email;
    a standing card reusing one key cannot. Pinned as a test so the mistake is not repeated.
    """

    import json as _json

    from payment_bot.clients.http import HttpResponse

    chat, transport = _chat(200)
    captured: dict[str, object] = {}

    def request(method: str, url: str, **kwargs: object):
        captured["url"] = url
        captured["body"] = _json.loads(kwargs["body"])  # type: ignore[arg-type]
        transport.calls.append(method)
        return HttpResponse(200, b'{"name": "spaces/S/messages/t.t"}')

    chat._transport.request = request  # type: ignore[assignment,method-assign]
    chat.post_card(queue_cards([], now=NOW, expiry_days=3), fallback_text="x")

    assert captured["url"] == "https://chat.googleapis.com/v1/spaces/S/messages"
    assert "messageReplyOption" not in str(captured["url"])
    assert "thread" not in captured["body"]  # type: ignore[operator]


@pytest.mark.unit
def test_a_deleted_card_is_replaced_by_the_miss_counter_not_by_a_special_case() -> None:
    """The live cause of both 403s: a reviewer deleted the card. That needs no new code —
    four refused edits abandons the name and the next run posts fresh."""

    store = InMemoryApprovalStore()
    store.set_tracker("spaces/S/messages/deleted")

    for expected_misses in (1, 2, 3):
        chat, transport = _chat(403)
        _refresh_queue_tracker(store, chat, _settings())
        assert transport.calls == ["PATCH"]
        assert store.tracker() == ("spaces/S/messages/deleted", expected_misses)

    chat, transport = _chat(403)
    _refresh_queue_tracker(store, chat, _settings())
    assert store.tracker() == ("", 0)

    chat, transport = _chat(200, post_name="spaces/S/messages/replacement")
    _refresh_queue_tracker(store, chat, _settings())
    assert transport.calls == ["POST"]
    assert store.tracker() == ("spaces/S/messages/replacement", 0)


@pytest.mark.unit
def test_each_row_links_to_the_approval_card_so_a_reviewer_can_act() -> None:
    """The queue is a list of things to do, and the doing happens on the approval card —
    its Approve button is the only way a reply goes out. A row that linked only to Gmail
    told a reviewer what was waiting and left them scrolling the space for the card."""

    from payment_bot.clients.google_chat import _chat_message_link

    entry = replace(
        _entry("e", hours_old=5, to="ar@carrier.com", loads=("2469115",)),
        chat_message="spaces/AAQAnvSk2WY/messages/p7dH0UiVeRc.wbi1j0I5tdo",
    )
    row = _texts(queue_cards([entry], now=NOW, expiry_days=3))[1]

    assert (
        "https://chat.google.com/room/AAQAnvSk2WY/p7dH0UiVeRc/wbi1j0I5tdo?cls=10" in row
    )
    # The email stays reachable beside it, for reading what was actually asked.
    assert "rfc822msgid" in row
    assert ">email</a>" in row

    # The shape was confirmed against a link copied out of the space. The dot in a message
    # name is the thread/message boundary and the URL wants two segments — passing
    # `thread.message` as one is a well-formed URL that opens nothing, which is what shipped
    # first and what a reviewer reported as "it does not take me to that card".
    assert (
        _chat_message_link("spaces/AAQAnvSk2WY/messages/1DtXZn3gulU.HAOPq9FXNMs")
        == "https://chat.google.com/room/AAQAnvSk2WY/1DtXZn3gulU/HAOPq9FXNMs?cls=10"
    )


@pytest.mark.unit
def test_a_row_with_no_card_still_renders_and_still_links_the_email() -> None:
    """`chat_message` is blank when the post failed or the entry predates the field. That
    must cost the recipient's link, not the whole row."""

    row = _texts(queue_cards([_entry("e", hours_old=5, to="ar@carrier.com")], now=NOW, expiry_days=3))[1]

    assert "ar@carrier.com" in row
    assert "chat.google.com" not in row
    assert ">email</a>" in row

    from payment_bot.clients.google_chat import _chat_message_link

    # A name that does not split into thread and message yields no link at all. A row with
    # no link is honest; a row whose link resolves nowhere is not.
    for junk in ("", "nonsense", "spaces/S", "spaces/S/threads/T.M", "spaces/S/messages/NoDot"):
        assert _chat_message_link(junk) == ""


@pytest.mark.unit
def test_ages_under_a_day_are_shown_in_hours() -> None:
    """Every row of the Today bucket read "0d old" — true, and useless. It cannot separate a
    draft queued twenty minutes ago from one queued this morning, which is the distinction
    someone triaging today's backlog is actually looking for."""

    from payment_bot.clients.google_chat import _age

    assert _age(0.4) == "0h old"
    assert _age(9) == "9h old"
    assert _age(23.6) == "24h old"
    assert _age(24) == "1d old"
    assert _age(60) == "2d old"

    card = queue_cards([_entry("e", hours_old=9, to="a@c.com")], now=NOW, expiry_days=3)
    assert "9h old" in _labels(card)[1]
    assert "0d old" not in _labels(card)[1]


@pytest.mark.unit
def test_chips_are_omitted_when_there_is_no_endpoint_to_send_them_to() -> None:
    """The live failure, and the reason it was so hard to see. PAYBOT_CHAT_ACTION_URL was
    set on the worker but not on the callback, so a worker-rendered card had working chips
    and the callback's redraw of it did not. The first click succeeded, the redraw came back
    dead, and every click after it produced Chat's generic "unable to process your request"
    with nothing in any log — because those clicks went to an endpoint named `queue_filter`
    and never reached us.

    A static card is strictly better than a button that looks live and isn't."""

    entries = [_entry("e", hours_old=5, to="a@c.com")]

    with_url = queue_cards(entries, now=NOW, expiry_days=3, action_url="https://cb/", interactive=True)
    assert [b["text"] for b in _buttons(with_url)], "chips expected when the URL is present"

    without_url = queue_cards(entries, now=NOW, expiry_days=3, action_url="", interactive=True)
    assert _buttons(without_url) == []
    # The queue itself still renders — losing the filter must not lose the list.
    assert "a@c.com" in "\n".join(_texts(without_url))


@pytest.mark.unit
def test_the_redrawn_card_points_its_chips_back_at_the_callback() -> None:
    """The chips must carry the callback's own URL, and it cannot come from configuration:
    the Function URL resource depends on the function, so feeding the URL back into the
    function's environment is a circular dependency CloudFormation refuses. It is derived
    from the request's Host header instead — the same value the audience check already reads.
    """

    from payment_bot.chat_callback import _queue_response
    from payment_bot.config import Settings

    store = InMemoryApprovalStore()
    store.put_pending(_entry("e", hours_old=5, to="a@c.com"))

    body = json.loads(
        _queue_response(
            store, Settings(_env_file=None), "all", False, action_url="https://own.example/"
        )["body"]
    )
    rendered = json.dumps(body["cardsV2"][0])

    assert '"function": "https://own.example/"' in rendered
    assert '"function": "queue_filter"' not in rendered


@pytest.mark.unit
def test_subjects_and_addresses_are_escaped_for_the_card() -> None:
    """Card text takes a little HTML — the rows are built from <a> and <br> — so an
    ampersand arriving in a subject is markup Chat has to parse, and one unescaped `&` makes
    the whole card unrenderable. Live in the queue: a subject reading "… Request for … & …".
    """

    entry = replace(
        _entry("e", hours_old=5, to="a&b@carrier.com", loads=("2469115",)),
        subject="Re: Payment Status for A & B <urgent>",
    )
    row = _texts(queue_cards([entry], now=NOW, expiry_days=3))[1]

    assert "&amp;" in row
    assert "&lt;urgent&gt;" in row
    # Our own markup survives untouched.
    assert "<br>" in row
    assert "<a href=" in row
    # And no raw ampersand is left outside an entity.
    import re as _re

    assert not _re.search(r"&(?!amp;|lt;|gt;|quot;|#)", row)


@pytest.mark.unit
def test_a_whole_bucket_is_listed_when_it_fits() -> None:
    """The row cap was 12 against a card allowance of ~32KB, which twelve rows fill to 6KB.
    Five sixths of the space went unused, so a 96-entry queue showed twelve rows and "84
    more" — and every date bucket was in fact small enough to list completely."""

    entries = [_entry(f"e{i}", hours_old=30 + i * 0.1, to=f"a{i}@carrier.com") for i in range(40)]
    card = queue_cards(entries, now=NOW, expiry_days=3)
    rows = [t for t in _labels(card) if "old" in t]

    assert len(rows) == 40
    assert "Not listed" not in _labels(card)


@pytest.mark.unit
def test_rows_stop_at_the_byte_budget_not_at_a_row_count() -> None:
    """Rows are not a fixed size — a long subject and a load list can be triple a bare row —
    so any count safe for the worst case wastes most of the card in the normal one. Chat
    rejects an oversized card outright, so the budget is a real limit."""

    fat = [
        replace(
            _entry(f"e{i}", hours_old=60 - i * 0.01, to=f"someone.with.a.long.address{i}@carrier.example.com"),
            subject="Re: " + "Payment status request for a great many loads " * 3,
        )
        for i in range(90)
    ]
    card = queue_cards(fat, now=NOW, expiry_days=3, budget=12_000)

    assert len(json.dumps(card)) < 16_000, "budget must bound the message"
    listed = len([t for t in _labels(card) if "old" in t])
    assert 0 < listed < 90
    assert "of <b>90</b>" in _texts(card)[-1]


@pytest.mark.unit
def test_one_row_is_always_listed_even_if_it_alone_exceeds_the_budget() -> None:
    """A single enormous entry must not render an empty queue. Better an oversized card Chat
    might refuse than a card that silently says nothing is waiting."""

    huge = replace(_entry("e", hours_old=5, to="a@c.com"), subject="x" * 400)
    card = queue_cards([huge], now=NOW, expiry_days=3, budget=1)

    assert len([t for t in _labels(card) if "old" in t]) == 1


@pytest.mark.unit
def test_a_bucket_larger_than_one_card_is_paged_not_truncated() -> None:
    """A day now runs past a hundred drafts and a card holds about thirty-five, so the old
    "84 more, all newer than those above" reported the bulk of the queue as a footnote with
    no way to reach it."""

    entries = [_entry(f"e{i}", hours_old=70 - i * 0.2, to=f"a{i}@carrier.example.com") for i in range(120)]

    first = queue_cards(entries, now=NOW, expiry_days=3, action_url="https://cb/", interactive=True)
    page_line = next(t for t in _texts(first) if "Showing" in t)
    assert "of <b>120</b>" in page_line
    assert "Showing <b>1-" in page_line

    labels = [b["text"] for b in _page_buttons(first)]
    assert "Older ▶" in labels
    assert "◀ Newer" not in labels, "no Newer on the first page"

    second = queue_cards(
        entries, now=NOW, expiry_days=3, offset=30, action_url="https://cb/", interactive=True
    )
    assert "Showing <b>31-" in next(t for t in _texts(second) if "Showing" in t)
    assert "◀ Newer" in [b["text"] for b in _page_buttons(second)]


@pytest.mark.unit
def test_every_page_stays_under_the_size_chat_actually_accepts() -> None:
    """The measurement that set the budget: on a live 234-entry queue a reviewer clicked each
    chip in turn — 14.0KB rendered, 28.2KB and 28.3KB both failed with "unable to process
    your request". 28KB came from the documented ~32KB message limit, which is not the real
    ceiling for these updates."""

    entries = [
        replace(
            _entry(f"e{i}", hours_old=70 - i * 0.2, to=f"someone.long{i}@carrier.example.com"),
            subject="Re: Payment Status - A CARRIER NAME INC MC#1234567 INVDHV0458 Load#2506698",
        )
        for i in range(240)
    ]

    for offset in (0, 35, 70, 200):
        card = queue_cards(
            entries, now=NOW, expiry_days=3, offset=offset,
            action_url="https://cb/", interactive=True,
        )
        assert len(json.dumps(card)) < 34_000, f"offset {offset} too large"
        for one in card:
            assert len(json.dumps(one)) < 17_000, "each card must stay renderable on its own"


@pytest.mark.unit
def test_an_offset_past_the_end_returns_to_the_top() -> None:
    """The queue moves under the reviewer — entries get approved and expired between the
    render and the click. Landing them on a stranded page of one is worse than starting over.
    """

    entries = [_entry(f"e{i}", hours_old=60 - i, to=f"a{i}@c.com") for i in range(5)]
    card = queue_cards(entries, now=NOW, expiry_days=3, offset=500)

    rows = [t for t in _labels(card) if "old" in t]
    assert len(rows) == 5
    assert not [t for t in _texts(card) if "Showing" in t], "all five fit; no page line"


@pytest.mark.unit
def test_page_buttons_carry_the_bucket_and_the_offset() -> None:
    """Paging inside a filtered day must not silently drop back to the unfiltered queue."""

    entries = [_entry(f"e{i}", hours_old=30 + i * 0.1, to=f"a{i}@carrier.example.com") for i in range(80)]
    # Budget forced small so the bucket definitely spans more than one page.
    card = queue_cards(
        entries, now=NOW, expiry_days=3, bucket="yesterday", budget=6_000,
        action_url="https://cb/", interactive=True,
    )
    older = next(b for b in _page_buttons(card) if b["text"] == "Older ▶")
    params = {p["key"]: p["value"] for p in older["onClick"]["action"]["parameters"]}

    assert params["action"] == "queue_page"
    assert params["bucket"] == "yesterday"
    assert int(params["offset"]) > 0
    assert older["onClick"]["action"]["function"] == "https://cb/"


@pytest.mark.unit
def test_the_message_is_split_across_cards_once_it_outgrows_one() -> None:
    """cardsV2 is a list, and Chat's 100-widget cap is documented as per CARD rather than per
    message — with the overflowing section and every section after it dropped silently, so a
    single card loses its tail without erroring. Splitting keeps each card inside the largest
    size measured to render (16KB) while the message carries roughly twice one card's worth.
    """

    entries = [
        replace(
            _entry(f"e{i}", hours_old=70 - i * 0.2, to=f"someone.long{i}@carrier.example.com"),
            subject="Re: Payment Status - A CARRIER NAME INC MC#1234567 INVDHV0458 Load#2506698",
        )
        for i in range(200)
    ]
    # Budget raised past the default: the shipped default is 14KB, which is what a click
    # response survives, and one card holds that. The splitting itself still has to be right
    # — the widget cap it exists for is per card, so a larger budget must never produce one
    # oversized card that silently drops its tail.
    cards = queue_cards(
        entries, now=NOW, expiry_days=3, budget=30_000,
        action_url="https://cb/", interactive=True,
    )

    assert len(cards) > 1, "a 200-entry queue must span more than one card"
    for one in cards:
        assert len(json.dumps(one)) <= 17_000
        assert len(one["card"]["sections"][0]["widgets"]) <= 90

    # Only the first card carries the header and the chips, so the message reads as one list.
    assert "header" in cards[0]["card"]
    assert all("header" not in c["card"] for c in cards[1:])
    assert len(_buttons(cards)) == 4

    # Card ids must differ or Chat treats them as one card being redefined.
    assert len({c["cardId"] for c in cards}) == len(cards)


@pytest.mark.unit
def test_a_split_message_still_lists_more_than_a_single_card_could() -> None:
    entries = [_entry(f"e{i}", hours_old=70 - i * 0.2, to=f"a{i}@carrier.example.com") for i in range(200)]

    one_card = queue_cards(entries, now=NOW, expiry_days=3, budget=16_000)
    split = queue_cards(entries, now=NOW, expiry_days=3, budget=30_000)

    rows_of = lambda cs: len([t for t in _labels(cs) if "old" in t])  # noqa: E731
    assert len(split) > len(one_card), "the larger budget must actually span more cards"
    assert rows_of(split) > rows_of(one_card)


@pytest.mark.unit
def test_the_default_budget_is_what_a_click_response_survives() -> None:
    """Two different limits govern this card, and the smaller one wins.

    Measured on a live 235-entry queue: the worker's API PATCH of a 30.3KB two-card message
    returned 200 twice, so the API write is fine at that size. The same payload handed back
    inline from a chip click was refused — "Payment Bot is unable to process your request" —
    while the click itself arrived and parsed correctly. Splitting raised the widget ceiling
    and bought nothing on bytes: 14.0KB rendered, 28.2KB failed, 30.3KB across two cards
    failed too.

    So the default has to sit at the size a CLICK survives, not the size the API accepts.
    """

    from payment_bot.clients.google_chat import _MESSAGE_BUDGET

    assert _MESSAGE_BUDGET <= 14_000

    entries = [
        replace(
            _entry(f"e{i}", hours_old=70 - i * 0.2, to=f"someone.long{i}@carrier.example.com"),
            subject="Re: Payment Status - A CARRIER NAME INC MC#1234567 INVDHV0458 Load#2506698",
        )
        for i in range(240)
    ]
    for bucket in ("all", "today", "yesterday", "older"):
        for offset in (0, 30, 90):
            cards = queue_cards(
                entries, now=NOW, expiry_days=3, bucket=bucket, offset=offset,
                action_url="https://cb/", interactive=True,
            )
            assert len(json.dumps(cards)) <= 15_000, f"{bucket}@{offset} would be refused"


# --- answered / unanswered labels -------------------------------------------
#
# A colleague replying straight from Gmail leaves no trace in the approval store. The row sat
# on the board looking outstanding, expired at three days as though nobody had touched it, and
# meanwhile anyone working the queue would chase a carrier who had already been answered.


@pytest.mark.unit
def test_every_row_carries_a_coloured_state_tag() -> None:
    from payment_bot.clients.google_chat import QUEUE_STATES

    entries = [
        _entry("open", hours_old=10, to="open@c.com"),
        _entry("done", hours_old=20, to="done@c.com"),
        _entry("draft", hours_old=30, to="draft@c.com"),
    ]
    cards = queue_cards(
        entries, now=NOW, expiry_days=3,
        states={"done": "handled", "draft": "drafting"},
    )
    body = "\n".join(_texts(cards))

    for state, (text, colour) in QUEUE_STATES.items():
        assert text in body, state
        assert colour in body, state
    # The colour is inside a font tag Chat actually renders, not loose text.
    assert '<font color="#A32D2D"><b>UNANSWERED</b></font>' in body


@pytest.mark.unit
def test_a_row_with_no_known_state_reads_as_unanswered() -> None:
    """Failing open is the only safe default: a thread we could not read must look like work
    still owed, never like work already done."""

    cards = queue_cards([_entry("e", hours_old=5, to="a@c.com")], now=NOW, expiry_days=3)

    assert "UNANSWERED" in _texts(cards)[1]


@pytest.mark.unit
def test_only_the_rows_about_to_be_shown_are_checked() -> None:
    """One Gmail call per row against a two-hundred-deep queue and a thirty-row card: checking
    all of them would spend the invocation on rows nobody is about to see."""

    from payment_bot.clients.google_chat import queue_page

    entries = [_entry(f"e{i}", hours_old=70 - i * 0.2, to=f"a{i}@c.com") for i in range(200)]
    page = queue_page(entries, now=NOW)

    assert [e.entry_id for e in page[:3]] == ["e0", "e1", "e2"], "oldest first"
    assert len(page) == 200, "queue_page orders and filters; the budget does the trimming"

    only_today = queue_page(entries, now=NOW, bucket="today")
    assert all(e.entry_id not in {"e0", "e1"} for e in only_today)

    resumed = queue_page(entries, now=NOW, offset=50)
    assert resumed[0].entry_id == "e50"


@pytest.mark.unit
def test_an_answered_thread_is_retired_and_an_open_one_is_not() -> None:
    from payment_bot.lambda_handler import _queue_states

    class Gmail:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def thread_state(self, thread_id: str) -> str:
            self.asked.append(thread_id)
            return {"t-done": "handled", "t-draft": "drafting"}.get(thread_id, "open")

    store = InMemoryApprovalStore()
    entries = []
    for entry_id, thread in (("open", "t-open"), ("done", "t-done"), ("draft", "t-draft")):
        entry = replace(_entry(entry_id, hours_old=10, to=f"{entry_id}@c.com"), thread_id=thread)
        store.put_pending(entry)
        entries.append(entry)

    gmail = Gmail()
    states = _queue_states(store, gmail, entries, _settings())

    assert states == {"open": "open", "done": "handled", "draft": "drafting"}
    assert gmail.asked == ["t-open", "t-done", "t-draft"]

    # Only the answered one leaves the queue.
    assert store.result("done") == {
        "status": "answered_elsewhere",
        "at": store.result("done")["at"],
        "by": "gmail-thread",
        "thread_id": "t-done",
    }
    assert store.result("open") is None
    assert store.result("draft") is None, "a draft is not an answer"
    assert {e.entry_id for e in store.live_entries()} == {"open", "draft"}


@pytest.mark.unit
def test_a_gmail_failure_never_retires_a_row() -> None:
    """The expensive mistake would be recording a carrier as answered because a read failed."""

    from payment_bot.lambda_handler import _queue_states

    class Broken:
        def thread_state(self, thread_id: str) -> str:
            raise RuntimeError("gmail is unwell")

    store = InMemoryApprovalStore()
    entry = replace(_entry("e", hours_old=10, to="a@c.com"), thread_id="t")
    store.put_pending(entry)

    states = _queue_states(store, Broken(), [entry], _settings())

    assert states == {}
    assert store.result("e") is None
    assert [e.entry_id for e in store.live_entries()] == ["e"]


@pytest.mark.unit
def test_an_entry_with_no_thread_id_is_left_alone() -> None:
    from payment_bot.lambda_handler import _queue_states

    class Gmail:
        def thread_state(self, thread_id: str) -> str:
            raise AssertionError("must not be asked without a thread id")

    store = InMemoryApprovalStore()
    entry = replace(_entry("e", hours_old=10, to="a@c.com"), thread_id="")
    store.put_pending(entry)

    assert _queue_states(store, Gmail(), [entry], _settings()) == {}


@pytest.mark.unit
def test_a_chip_click_labels_rows_from_the_cache() -> None:
    """The reported bug. Labels were wired into the worker's refresh only, so every row a
    chip click rendered came back UNANSWERED — a 19h-old row whose thread a colleague had
    already answered still read as outstanding, because reaching it meant clicking Today.

    The click path cannot ask Gmail: that is an API call per row against a response Chat
    expects immediately. So it reads the cache the worker keeps.
    """

    from payment_bot.chat_callback import _queue_response
    from payment_bot.config import Settings

    real_now = datetime.now(UTC)
    store = InMemoryApprovalStore()
    for entry_id, hours in (("done", 19), ("open", 20)):
        store.put_pending(
            replace(
                _entry(entry_id, hours_old=0, to=f"{entry_id}@carrier.com"),
                created_at=(real_now - timedelta(hours=hours)).isoformat(),
            )
        )
    store.set_row_states({"done": {"state": "handled", "at": real_now.isoformat()}})

    body = json.loads(_queue_response(store, Settings(_env_file=None), "all", False)["body"])
    rendered = json.dumps(body["cardsV2"])

    assert "ANSWERED IN GMAIL" in rendered
    assert "UNANSWERED" in rendered
    # And each label lands on the right row.
    rows = [w["decoratedText"]["text"] for w in _widgets(body["cardsV2"]) if "decoratedText" in w]
    done_row = next(r for r in rows if "done@carrier.com" in r)
    open_row = next(r for r in rows if "open@carrier.com" in r)
    assert "ANSWERED IN GMAIL" in done_row
    assert "UNANSWERED" in open_row


@pytest.mark.unit
def test_a_fresh_cached_state_is_not_rechecked() -> None:
    """The check budget has to go to rows nothing is known about, or a queue this size spends
    every refresh re-establishing what it already knew."""

    from payment_bot.lambda_handler import _queue_states

    class Gmail:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def thread_state(self, thread_id: str) -> str:
            self.asked.append(thread_id)
            return "open"

    now = datetime.now(UTC)
    store = InMemoryApprovalStore()
    entries = []
    for entry_id in ("fresh", "stale", "unknown"):
        entry = replace(
            _entry(entry_id, hours_old=10, to=f"{entry_id}@c.com"), thread_id=f"t-{entry_id}"
        )
        store.put_pending(entry)
        entries.append(entry)
    store.set_row_states(
        {
            "fresh": {"state": "drafting", "at": now.isoformat()},
            "stale": {"state": "open", "at": (now - timedelta(hours=4)).isoformat()},
        }
    )

    gmail = Gmail()
    states = _queue_states(store, gmail, entries, _settings())

    assert gmail.asked == ["t-stale", "t-unknown"], "the fresh one is trusted"
    assert states["fresh"] == "drafting", "and its cached label is kept"


@pytest.mark.unit
def test_the_cache_forgets_rows_that_have_left_the_queue() -> None:
    """Otherwise it grows for the life of the deployment."""

    from payment_bot.lambda_handler import _queue_states

    now = datetime.now(UTC)
    store = InMemoryApprovalStore()
    entry = replace(_entry("live", hours_old=10, to="a@c.com"), thread_id="t")
    store.put_pending(entry)
    store.set_row_states(
        {
            "live": {"state": "open", "at": now.isoformat()},
            "long-gone": {"state": "handled", "at": now.isoformat()},
        }
    )

    _queue_states(store, None, [entry], _settings())

    assert set(store.row_states()) == {"live"}


@pytest.mark.unit
def test_the_check_budget_is_shared_across_the_days_not_eaten_by_the_oldest() -> None:
    """The flaw a screenshot of page two exposed.

    Candidates were the head of every bucket INCLUDING "all", concatenated. "all" came first
    and is the same oldest-first ordering, so its forty oldest rows consumed the whole per-run
    budget and Today got five checks — a reviewer reading Today saw unlabelled rows for hours
    while the same oldest page was re-established every quarter hour.

    "all" is the union of the other three, so its head is already covered by whichever day
    those rows fall in. Interleaving the days means a budget that runs out leaves every day
    partly covered rather than one day fully and the rest not at all.
    """

    from payment_bot.clients.google_chat import QUEUE_BUCKETS, bucket_of, queue_page
    from payment_bot.lambda_handler import _STATE_ROWS_PER_BUCKET

    now = datetime.now(UTC)
    entries = []
    for bucket_hours, count in ((6, 50), (30, 50), (60, 50)):
        for i in range(count):
            entries.append(
                replace(
                    _entry(f"b{bucket_hours}-{i}", hours_old=0, to=f"b{bucket_hours}-{i}@c.com"),
                    created_at=(now - timedelta(hours=bucket_hours, minutes=i)).isoformat(),
                )
            )

    pages = [
        queue_page(entries, now=now, bucket=key)[:_STATE_ROWS_PER_BUCKET]
        for key, _, _ in QUEUE_BUCKETS
        if key != "all"
    ]
    candidates, seen = [], set()
    for row in range(_STATE_ROWS_PER_BUCKET):
        for page in pages:
            if row < len(page) and page[row].entry_id not in seen:
                seen.add(page[row].entry_id)
                candidates.append(page[row])

    # Whatever the budget, the first slice of candidates touches all three days.
    for budget in (9, 30, 60):
        covered = {
            bucket_of((now - datetime.fromisoformat(e.created_at)).total_seconds() / 3600)
            for e in candidates[:budget]
        }
        assert covered == {"today", "yesterday", "older"}, f"budget {budget} starved a day"
