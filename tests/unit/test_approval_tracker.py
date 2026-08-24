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

    card = queue_card([], now=NOW, expiry_days=3)
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

    labels = [b["text"] for b in _buttons(queue_card(entries, now=NOW, expiry_days=3, action_url="https://cb/", interactive=True))]

    assert labels == ["● All (5)", "Today (2)", "Yesterday (1)", "2 days + (2)"]


@pytest.mark.unit
def test_selecting_a_day_shows_only_that_day_and_says_so() -> None:
    entries = [
        _entry("a", hours_old=2, to="today@c.com"),
        _entry("c", hours_old=30, to="yesterday@c.com"),
    ]

    card = queue_card(entries, now=NOW, expiry_days=3, bucket="yesterday", action_url="https://cb/", interactive=True)
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
        action_url="https://cb/", interactive=True,
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
        action_url="https://cb/", interactive=True,
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

    from payment_bot.chat_callback import _queue_response
    from payment_bot.config import Settings

    store = InMemoryApprovalStore()
    store.put_pending(_entry("fresh", hours_old=1, to="fresh@c.com"))
    store.put_pending(_entry("old", hours_old=60, to="old@c.com"))

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
    chat.post_card(queue_card([], now=NOW, expiry_days=3), fallback_text="x")

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
    row = _texts(queue_card([entry], now=NOW, expiry_days=3))[1]

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

    row = _texts(queue_card([_entry("e", hours_old=5, to="ar@carrier.com")], now=NOW, expiry_days=3))[1]

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

    card = queue_card([_entry("e", hours_old=9, to="a@c.com")], now=NOW, expiry_days=3)
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

    with_url = queue_card(entries, now=NOW, expiry_days=3, action_url="https://cb/", interactive=True)
    assert [b["text"] for b in _buttons(with_url)], "chips expected when the URL is present"

    without_url = queue_card(entries, now=NOW, expiry_days=3, action_url="", interactive=True)
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
    row = _texts(queue_card([entry], now=NOW, expiry_days=3))[1]

    assert "&amp;" in row
    assert "&lt;urgent&gt;" in row
    # Our own markup survives untouched.
    assert "<br>" in row
    assert "<a href=" in row
    # And no raw ampersand is left outside an entity.
    import re as _re

    assert not _re.search(r"&(?!amp;|lt;|gt;|quot;|#)", row)
