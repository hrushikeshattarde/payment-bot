"""Google Chat client for the approval space (docs/CHAT_APPROVAL_PLAN.md).

Implements the :class:`~payment_bot.clients.slack.SlackClient` protocol — the pipeline
already posts every approval and escalation through that seam, so pointing it at Google
Chat is a construction-time choice, not pipeline surgery. Three card kinds land in the
one configured space, giving the reviewers a single feed of everything the bot did:

* **Approval cards** — a gate-passing draft with the full reply text and, when the
  client is *interactive*, the three buttons (Approve & send as me / Move to my Gmail
  Drafts / Reject). Non-interactive is shadow mode (plan §10 step 2): same card, no
  buttons, drafts still land in Gmail — visibility with zero behaviour change.
* **Notice cards** — escalations and gate blocks, each carrying the short reason the
  pipeline already produces. These re-run every poll by design, so posting is deduped
  through :class:`~payment_bot.approvals.ChatPostLedger`.

Buttons carry ONLY an entry id and an action name. The reply's content, recipients and
headers come from the pending entry the worker stored — a tampered click payload cannot
change what gets sent (plan §7).

Auth is **app auth** (the service account as the Chat app itself, ``chat.bot`` scope):
no delegation entry, no Admin-console change — the Chat API enabled in the key's project
and the app added to the space is the whole story.

Posting never raises into the pipeline: a failed post is recorded in
:attr:`GoogleChatClient.failed` and the runner falls back to a Gmail draft, so a chat
outage degrades to today's workflow rather than to silence.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime
from typing import Any

from payment_bot.approvals import ChatPostLedger, PendingApproval, entry_id_for
from payment_bot.clients.http import HttpTransport, UrllibTransport
from payment_bot.clients.slack import ApprovalSummary, SlackPost
from payment_bot.logging import get_logger

_log = get_logger("clients.google_chat")

CHAT_API_BASE = "https://chat.googleapis.com/v1"

#: Card body text caps. Chat rejects oversized widgets; the stored entry always keeps
#: the full text — only the card view is trimmed, and it says so.
_BODY_LIMIT = 3600
_REASON_LIMIT = 300

#: Action names the buttons invoke and the callback dispatches on.
ACTION_APPROVE = "approve"
ACTION_MOVE = "move_to_drafts"
ACTION_REJECT = "reject"


def _esc(text: str) -> str:
    """Escape a value for a card's rich-text field.

    Card text takes a small amount of HTML — the tracker's rows are built from ``<a>`` and
    ``<br>`` — so any ``&`` or angle bracket arriving in a subject or an address is markup
    Chat has to parse. A live queue held ``Re: Payment Status Request for … & …``, and one
    unescaped ampersand is enough to make the whole card unrenderable.

    Only the VALUES go through this; the tags this module writes itself must not.
    """

    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _gmail_link(message_id: str) -> str:
    """A link that opens the carrier's email in the CLICKER's own Gmail.

    ``rfc822msgid:`` search rather than a thread URL on purpose: Gmail thread ids are
    per-mailbox, so no single thread URL works for every space member — but every
    member of the group has their own copy of the message under the same RFC 822 id,
    and this search lands each person in their own copy.
    """

    query = urllib.parse.quote(f"rfc822msgid:{message_id.strip('<>')}", safe="")
    return f"https://mail.google.com/mail/u/0/#search/{query}"


def _chat_message_link(chat_message: str) -> str:
    """A link that opens one approval card in the space. ``""`` when it cannot be built.

    The API exposes no permalink field, so this is assembled from the resource name. The
    shape was confirmed against a link copied out of the space itself, because guessing it
    produced a link that resolved nowhere:

    ``spaces/AAQAnvSk2WY/messages/1DtXZn3gulU.HAOPq9FXNMs``
    → ``https://chat.google.com/room/AAQAnvSk2WY/1DtXZn3gulU/HAOPq9FXNMs?cls=10``

    **The dot is a path separator, not part of the id.** A message name ends in
    ``{thread}.{message}`` and the URL wants those as two segments — the earlier version
    passed the whole ``thread.message`` string as one, which is a well-formed URL that opens
    nothing. Anything that does not split into exactly two halves yields no link at all: a
    row with no link is honest, a row with a link that goes nowhere is not.

    ``cls=10`` is carried verbatim from the copied link rather than reasoned about.

    The tracker needs this because the queue is a list of things to DO, and the doing happens
    on the approval card: its Approve button is the only way a reply goes out. A row linking
    only to the email in Gmail told a reviewer what was waiting and then left them scrolling
    the space for the card that could action it.
    """

    parts = chat_message.strip().strip("/").split("/")
    if len(parts) != 4 or parts[0] != "spaces" or parts[2] != "messages":
        return ""
    space, message = parts[1], parts[3]
    thread, _, tail = message.partition(".")
    if not (space and thread and tail):
        return ""
    return f"https://chat.google.com/room/{space}/{thread}/{tail}?cls=10"


def _open_email_button(message_id: str) -> dict[str, Any]:
    return {
        "text": "Open the email in Gmail",
        "onClick": {"openLink": {"url": _gmail_link(message_id)}},
    }


def _action_button(text: str, action: str, entry_id: str, action_url: str) -> dict[str, Any]:
    """One action button, add-ons-runtime correct.

    On the add-ons runtime that console-configured Chat apps run on, ``function`` must
    be **the HTTPS endpoint to call** — a bare verb there sends the click to an
    endpoint literally named "reject", which answers nothing, and the space shows the
    generic red failure (learned live 2026-08-20; the Chat error log's
    ``deploymentFunction: "reject"`` was the giveaway). So the verb rides in the
    parameters, where the callback dispatches on it; ``action_url`` blank falls back
    to the bare name for tests and any legacy-provisioned app.
    """

    return {
        "text": text,
        "onClick": {
            "action": {
                "function": action_url or action,
                "parameters": [
                    {"key": "action", "value": action},
                    {"key": "entry", "value": entry_id},
                ],
            }
        },
    }


def approval_card(
    *,
    entry_id: str,
    from_email: str,
    load_ids: tuple[str, ...],
    to: str,
    cc: tuple[str, ...],
    reply_to: str,
    body: str,
    subject: str = "",
    interactive: bool = False,
    status: str = "",
    message_id: str = "",
    action_url: str = "",
) -> dict[str, Any]:
    """The approval card, shared by the poster and the callback's in-place updates.

    ``status`` non-empty renders the terminal form: status line shown, action buttons
    gone — what a card becomes after a click or an expiry, so the feed doubles as the
    audit trail humans read. ``message_id`` adds an "Open the email in Gmail" link
    button in every form, including terminal ones: finding the conversation is useful
    before acting and after.
    """

    recipients = [
        {"decoratedText": {"topLabel": "To", "text": to or "(unknown)"}},
    ]
    if cc:
        recipients.append({"decoratedText": {"topLabel": "Cc", "text": ", ".join(cc)}})
    if reply_to:
        recipients.append({"decoratedText": {"topLabel": "Reply-To", "text": reply_to}})
    if subject:
        recipients.append({"decoratedText": {"topLabel": "Subject", "text": subject}})

    trimmed = _trim(body, _BODY_LIMIT)
    if trimmed != body:
        trimmed += "\n\n(card view truncated — the send uses the full stored text)"

    sections: list[dict[str, Any]] = [
        {
            "widgets": [
                {"decoratedText": {"topLabel": "From", "text": from_email or "(unknown)"}},
                {
                    "decoratedText": {
                        "topLabel": "Loads",
                        "text": ", ".join(load_ids) or "(none named)",
                    }
                },
                *recipients,
            ]
        },
        {
            "header": "Draft reply",
            "widgets": [{"textParagraph": {"text": trimmed}}],
        },
    ]

    buttons: list[dict[str, Any]] = []
    if interactive and not status:
        buttons = [
            _action_button("Approve & send as me", ACTION_APPROVE, entry_id, action_url),
            _action_button("Move to my Gmail Drafts", ACTION_MOVE, entry_id, action_url),
            _action_button("Reject", ACTION_REJECT, entry_id, action_url),
        ]
    if message_id:
        # A plain link, not an action: it opens in the clicker's Gmail without a round
        # trip to the callback, so it survives every state the card can be in.
        buttons.append(_open_email_button(message_id))

    tail: list[dict[str, Any]] = []
    if status:
        tail.append({"decoratedText": {"topLabel": "Status", "text": status}})
    elif not interactive:
        tail.append(
            {
                "decoratedText": {
                    "topLabel": "Status",
                    "text": "Shadow mode — review and send from Gmail Drafts as usual.",
                }
            }
        )
    if buttons:
        tail.append({"buttonList": {"buttons": buttons}})
    if tail:
        sections.append({"widgets": tail})

    title = "Draft reply — approve to send as yourself" if interactive else "Draft reply"
    return {
        "cardId": f"approval-{entry_id}",
        "card": {
            "header": {"title": title, "subtitle": f"from {from_email}"},
            "sections": sections,
        },
    }


def notice_card(
    *,
    correlation_id: str,
    kind: str,
    severity: str,
    reason: str,
    load_ids: tuple[str, ...],
) -> dict[str, Any]:
    """Escalation / gate-block card: what happened and the short reason why.

    No buttons ever — there is nothing to approve. The card IS the task: this mail sits
    unread and a human must reply from the group mailbox.
    """

    title = "Blocked by the pre-send gate" if kind == "blocked" else "Escalated — no draft"
    widgets: list[dict[str, Any]] = [
        {
            "decoratedText": {
                "topLabel": "Why",
                "text": _trim(reason, _REASON_LIMIT) or "(no reason recorded)",
                "wrapText": True,
            }
        },
        {
            "decoratedText": {
                "topLabel": "Loads",
                "text": ", ".join(load_ids) or "(none named)",
            }
        },
        {
            "decoratedText": {
                "topLabel": "Next",
                "text": "Needs a human reply from the group mailbox — "
                "nothing was drafted or sent.",
            }
        },
    ]
    # The correlation id IS the inbound message id, so the card that says "a human must
    # reply" can also take them straight to the email that needs the reply.
    if correlation_id:
        widgets.append({"buttonList": {"buttons": [_open_email_button(correlation_id)]}})
    return {
        "cardId": f"notice-{entry_id_for(correlation_id)}",
        "card": {
            "header": {"title": title, "subtitle": f"severity: {severity}"},
            "sections": [{"widgets": widgets}],
        },
    }


#: The verb a date chip on the tracker card invokes.
ACTION_QUEUE_FILTER = "queue_filter"

#: The date buckets the tracker's nav bar offers, newest first, keyed by the chip a
#: reviewer clicks. ``all`` is always present and always the default.
#:
#: Days, not hours or carriers, because that is what was asked for and what a
#: non-technical reviewer already thinks in: "what came in yesterday that nobody has
#: answered". The buckets stop at 2 because the sweep expires an approval at three days —
#: there is no such thing as a live entry older than that, so a "week" chip would be an
#: always-empty control.
QUEUE_BUCKETS: tuple[tuple[str, str, int | None], ...] = (
    ("all", "All", None),
    ("today", "Today", 0),
    ("yesterday", "Yesterday", 1),
    ("older", "2 days +", 2),
)


def _bucket_button(key: str, label: str, count: int, selected: bool, action_url: str) -> dict[str, Any]:
    """One date chip. The count rides in the label so the bar reads as a summary too."""

    return {
        "text": f"{'● ' if selected else ''}{label} ({count})",
        "disabled": selected,
        "onClick": {
            "action": {
                # Same add-ons-runtime rule as _action_button: `function` must be the
                # endpoint URL, and the verb rides in the parameters.
                "function": action_url or ACTION_QUEUE_FILTER,
                "parameters": [
                    {"key": "action", "value": ACTION_QUEUE_FILTER},
                    {"key": "bucket", "value": key},
                ],
            }
        },
    }


def _age(hours: float) -> str:
    """How old, in the unit a reader can act on.

    Hours below a day, days above it. Rendering everything in days put "0d old" against every
    row of the Today bucket — true, and useless: it cannot separate a draft queued twenty
    minutes ago from one queued this morning, which is exactly the distinction someone
    triaging today's backlog is looking for.
    """

    if hours < 24:
        return f"{max(hours, 0):.0f}h old"
    return f"{hours / 24:.0f}d old"


#: What the bot knows about whether a queued reply has been dealt with, and how each reads
#: on the card. The colour carries the same meaning as the words, for scanning rather than
#: reading — Chat's card HTML supports ``<font color>`` and little else.
#:
#: ``handled`` is the one worth having. A colleague replying straight from Gmail leaves no
#: trace in the approval store, so the row sat on the board looking outstanding and expired
#: three days later as though nobody had touched it — and anyone working the queue would chase
#: a carrier who had already been answered. ``drafting`` is deliberately NOT that: a draft in
#: the thread means somebody started, not that the carrier heard back.
QUEUE_STATES: dict[str, tuple[str, str]] = {
    "open": ("UNANSWERED", "#A32D2D"),
    "handled": ("ANSWERED IN GMAIL", "#854F0B"),
    "drafting": ("DRAFT IN GMAIL", "#185FA5"),
}


def _state_label(state: str) -> str:
    """The coloured tag for a row, or ``""`` for a state with nothing to say."""

    text, colour = QUEUE_STATES.get(state, QUEUE_STATES["open"])
    return f'<font color="{colour}"><b>{text}</b></font>'


def queue_page(
    entries: list[PendingApproval],
    *,
    now: datetime,
    bucket: str = "all",
    offset: int = 0,
) -> list[PendingApproval]:
    """The entries a card would list, in order, before the size budget trims them.

    Shared with the caller so it can establish per-row state for the rows that will actually
    be shown, rather than for the whole queue: the state check costs a Gmail call each, and
    the board is two hundred deep against a thirty-row card.

    A superset, not the exact page — the budget decides the final cut, and duplicating that
    walk here to save a few calls would be two implementations of one rule.
    """

    aged = sorted(
        (((now - _created(entry, now)).total_seconds() / 3600.0, entry) for entry in entries),
        key=lambda pair: pair[0],
        reverse=True,
    )
    shown = aged if bucket not in {k for k, _, _ in QUEUE_BUCKETS} or bucket == "all" else [
        pair for pair in aged if bucket_of(pair[0]) == bucket
    ]
    start = 0 if offset >= len(shown) else max(offset, 0)
    return [entry for _, entry in shown[start:]]


def _created(entry: PendingApproval, fallback: datetime) -> datetime:
    """When the entry was queued. An unparseable stamp reads as "just now".

    Never as "ancient": a bad timestamp must not hide the row, and must not fabricate an
    expiry that puts it at the top of a queue sorted by urgency.
    """

    try:
        return datetime.fromisoformat(entry.created_at)
    except ValueError:
        return fallback


#: The verb a Prev/Next button on the tracker invokes. Same handler as the date chips; the
#: page rides alongside the bucket in the parameters.
ACTION_QUEUE_PAGE = "queue_page"


def _page_button(label: str, bucket: str, offset: int, action_url: str) -> dict[str, Any]:
    return {
        "text": label,
        "onClick": {
            "action": {
                # Same add-ons rule as every other button here: `function` is the endpoint.
                "function": action_url or ACTION_QUEUE_PAGE,
                "parameters": [
                    {"key": "action", "value": ACTION_QUEUE_PAGE},
                    {"key": "bucket", "value": bucket},
                    {"key": "offset", "value": str(max(offset, 0))},
                ],
            }
        },
    }


def bucket_of(hours_old: float) -> str:
    """Which date bucket an entry of this age belongs to."""

    days = int(hours_old // 24)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return "older"


#: Hard ceiling on rows, and a backstop rather than the real limit — :data:`_CARD_BUDGET`
#: almost always binds first. Kept so a pathological queue of one-character subjects cannot
#: produce a card with hundreds of widgets: Chat allows 100 per section.
_QUEUE_ROWS = 90

#: Bytes of serialised card the tracker will fill before it stops adding rows.
#:
#: 28KB was set from the documented ~32KB message limit and is wrong: Chat refuses these
#: updates well below it. Measured against a live 234-entry queue, from a reviewer clicking
#: each chip in turn — 14.0KB rendered, 28.2KB and 28.3KB both failed with "unable to process
#: your request". So the true ceiling is somewhere in between and the documented figure is not
#: it. 16KB sits just above the largest card confirmed to work, with room for a subject longer
#: than any in the queue.
#:
#: The consequence is paging rather than a shorter list: see ``offset``. A budget this size
#: holds roughly 35 rows, and a single day now routinely runs past a hundred.
#:
#: Budget rather than a row count because rows are not a fixed size — a load list and a long
#: subject can be triple a bare one — so any count safe for the worst case wastes most of the
#: card in the normal one.
_CARD_BUDGET = 16_000

#: Bytes of serialised cards the tracker will put in ONE message, across however many cards
#: that takes.
#:
#: ``cardsV2`` is a list, so the rows can be split across several cards in a single message,
#: and one Chat limit is documented as per-CARD rather than per-message: 100 widgets, with the
#: overflowing section and every section after it silently dropped. Splitting therefore
#: genuinely raises that ceiling.
#:
#: **There are two different size limits here, and the smaller one governs.** Measured:
#:
#: * the API write succeeds at 30KB — the worker's scheduled PATCH of a two-card message
#:   returned 200 twice;
#: * the INTERACTION RESPONSE does not. A chip click hands the cards back inline in the
#:   callback's HTTP response, and at ~30KB Chat answers "Payment Bot is unable to process
#:   your request" while the click itself arrives and parses perfectly.
#:
#: So splitting across cards raised the widget ceiling and bought nothing on bytes: 14.0KB
#: rendered, 28.2KB failed, 30.3KB split across two cards failed too. The bound is on the
#: whole message, not the card, and the click path is the strictest consumer of it.
#:
#: 14KB is therefore the budget — at the only size confirmed to survive a click. Paging
#: covers the remainder. Raising this means removing the inline response from the click path
#: entirely: the callback would PATCH the message through the API, which is already known to
#: accept twice this, and return a bare ack. That is a real option and the numbers above are
#: the case for it; it is not done here because restoring a working card came first.
_MESSAGE_BUDGET = 14_000

#: Widgets per card. Chat's documented ceiling is 100, and going over does not error — it
#: drops that section and all following ones, so a card that looks fine can be missing its
#: tail. 90 keeps a margin under it.
_CARD_WIDGETS = 90


def queue_cards(
    entries: list[PendingApproval],
    *,
    now: datetime,
    expiry_days: int,
    refreshed: str = "",
    rows: int = _QUEUE_ROWS,
    budget: int = _MESSAGE_BUDGET,
    bucket: str = "all",
    offset: int = 0,
    action_url: str = "",
    interactive: bool = False,
    states: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The standing tracker: every drafted reply still waiting for a click.

    One card, rewritten in place each run (see :meth:`GoogleChatClient.upsert_tracker`), so
    the space carries a live queue rather than a trail of stale summaries. Pin it once and it
    behaves like a panel — which is as close to a tab as Chat gets, there being no API for
    adding one.

    **Ordered oldest first, and led by what is about to be lost.** An approval is not a
    backlog item that waits politely: the sweep expires it at ``approval_expiry_days`` and
    the draft is then gone, while the intake query only reaches back ``newer_than`` — so a
    draft that ages out has roughly a day in which it could be redrafted at all, and only if
    the mail is still unread. Measured on the live queue: 86 waiting, 52 of them two days old
    against a three-day expiry. A tracker sorted newest-first would have shown a busy,
    healthy space.

    A row of date chips across the top is the nav bar: All / Today / Yesterday / 2 days +,
    each carrying its own count. Clicking one redraws this same card filtered to that day —
    no command to type, no menu to find, which is the whole point for reviewers who do not
    live in developer tools.

    **The filter is deliberately transient.** One pinned message serves seven reviewers, so a
    chip one person clicks changes what all of them see. Rather than build per-person state,
    the next scheduled refresh re-renders at ``all`` — so a narrowed card heals itself within
    fifteen minutes, and the header always names the filter and the true total so a filtered
    view can never be mistaken for the whole queue.

    Read-only. It reports the queue and changes nothing about expiry or sending.
    """

    cutoff_hours = max(expiry_days, 0) * 24
    aged: list[tuple[float, PendingApproval]] = []
    for entry in entries:
        aged.append(((now - _created(entry, now)).total_seconds() / 3600.0, entry))
    aged.sort(key=lambda pair: pair[0], reverse=True)

    # Chips without an endpoint are dead controls, and they fail in the worst way: the
    # add-ons runtime posts the click to an endpoint named after the verb, so the button
    # looks live, does nothing, and Chat blames itself with "unable to process your
    # request". A static card is strictly better than that, and the warning names the
    # missing setting rather than leaving somebody to find it from the symptom.
    if interactive and not action_url:
        _log.warning("chat_queue_chips_disabled_no_action_url")
        interactive = False

    counts = {key: 0 for key, _, _ in QUEUE_BUCKETS}
    for hours, _ in aged:
        counts["all"] += 1
        counts[bucket_of(hours)] += 1

    selected = bucket if bucket in counts else "all"
    shown = aged if selected == "all" else [p for p in aged if bucket_of(p[0]) == selected]
    # An offset past the end means the queue shrank between the render and the click —
    # entries approved or expired underneath the reviewer. Reset to the top rather than
    # clamping to the last row, which would strand them on a page of one.
    start = 0 if offset >= len(shown) else max(offset, 0)
    page = shown[start:]

    expiring = sum(1 for hours, _ in aged if cutoff_hours and hours >= cutoff_hours - 24)
    widgets: list[dict[str, Any]] = []
    if interactive:
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        _bucket_button(key, label, counts[key], key == selected, action_url)
                        for key, label, _ in QUEUE_BUCKETS
                    ]
                }
            }
        )
    widgets.append(
        {
            "decoratedText": {
                "topLabel": "Waiting on a click",
                "text": (
                    f"<b>{len(aged)}</b> drafted repl{'y' if len(aged) == 1 else 'ies'}"
                    + (
                        f" — <b>{expiring}</b> within 24h of expiring"
                        if expiring
                        else " — none near expiry"
                    )
                    + (
                        ""
                        if selected == "all"
                        else f"<br><b>{len(shown)}</b> from "
                        f"{ {k: la for k, la, _ in QUEUE_BUCKETS}[selected].lower()}"
                    )
                ),
                "wrapText": True,
            }
        }
    )
    if not aged:
        widgets.append(
            {
                "decoratedText": {
                    "topLabel": "Queue",
                    "text": "Empty — every drafted reply has been actioned.",
                }
            }
        )

    if aged and not page:
        widgets.append(
            {
                "decoratedText": {
                    "topLabel": "Nothing in this day",
                    "text": "No unanswered drafts from that day. Tap All to see the rest.",
                    "wrapText": True,
                }
            }
        )

    # Rows are added until the card is nearly full rather than up to a fixed count: see
    # _CARD_BUDGET. `overhead` is everything that is not a row — header, nav bar, lead line
    # — measured rather than estimated, so the budget cannot drift as those change.
    overhead = len(json.dumps(widgets)) + 200  # 200 ≈ header + card scaffolding
    spent = overhead
    listed = 0

    for hours, entry in page[:rows]:
        left = cutoff_hours - hours
        when = (
            "EXPIRED"
            if cutoff_hours and left <= 0
            else (f"{left:.0f}h left" if cutoff_hours else _age(hours))
        )
        loads = ", ".join(entry.load_ids) or "no load id"
        card_link = _chat_message_link(entry.chat_message)
        # The recipient is the link to the CARD, because approving is what a reviewer came
        # here to do. The email stays reachable beside it for the cases where they need to
        # read what was actually asked before deciding. Both are inline anchors rather than
        # buttons: twelve rows of buttons is a card Chat would reject.
        who = _esc(_trim(entry.to, 60))
        primary = f'<a href="{card_link}">{who}</a>' if card_link else who
        secondary = f' · <a href="{_gmail_link(entry.message_id)}">email</a>'
        tag = _state_label((states or {}).get(entry.entry_id, "open"))
        row = {
            "decoratedText": {
                "topLabel": f"{_age(hours)} · {when}",
                "text": (
                    f"{tag} {primary} · {_esc(loads)}{secondary}"
                    f"<br>{_esc(_trim(entry.subject or '(no subject)', 90))}"
                ),
                "wrapText": True,
            }
        }
        cost = len(json.dumps(row))
        # Against the MESSAGE budget: the rows are split across cards below, so a single
        # card's size is no longer what bounds the list.
        if listed and spent + cost > budget:
            break
        widgets.append(row)
        spent += cost
        listed += 1

    # Where this page sits in the bucket, and the buttons to move through it. A day now runs
    # to over a hundred drafts and a card holds about thirty-five, so "84 more" was the whole
    # remainder of the queue reported as a footnote with no way to reach it.
    if listed and len(shown) > listed:
        first, last = start + 1, start + listed
        widgets.append(
            {
                "decoratedText": {
                    "topLabel": "This page",
                    "text": f"Showing <b>{first}-{last}</b> of <b>{len(shown)}</b>, oldest first.",
                    "wrapText": True,
                }
            }
        )
        if interactive:
            buttons = []
            if start > 0:
                buttons.append(
                    _page_button("◀ Newer", selected, max(start - listed, 0), action_url)
                )
            if last < len(shown):
                buttons.append(_page_button("Older ▶", selected, last, action_url))
            if buttons:
                widgets.append({"buttonList": {"buttons": buttons}})

    return _split_cards(widgets, expiry_days=expiry_days, refreshed=refreshed)


def _split_cards(
    widgets: list[dict[str, Any]], *, expiry_days: int, refreshed: str
) -> list[dict[str, Any]]:
    """One message's worth of widgets, divided into as many cards as they need.

    Chat caps widgets per CARD at 100 and drops the overflowing section — and every section
    after it — without erroring, so a single card silently loses its tail. Splitting keeps
    each card inside both that cap and :data:`_CARD_BUDGET`, which is the largest single card
    measured to render.

    Only the first card carries the header and the date chips; the rest are plain continuation
    cards, so the message reads as one list rather than as a repeated banner.
    """

    header = {
        "title": "Drafts awaiting approval",
        "subtitle": (
            f"expire after {expiry_days} day{'' if expiry_days == 1 else 's'}"
            + (f" · refreshed {refreshed}" if refreshed else "")
        ),
    }

    cards: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    spent = 0

    def flush() -> None:
        nonlocal current, spent
        if not current:
            return
        index = len(cards)
        card: dict[str, Any] = {
            "cardId": f"approval-queue-tracker-{index}",
            "card": {"sections": [{"widgets": current}]},
        }
        if index == 0:
            card["card"]["header"] = header
        cards.append(card)
        current = []
        spent = 0

    for widget in widgets:
        cost = len(json.dumps(widget))
        if current and (spent + cost > _CARD_BUDGET or len(current) >= _CARD_WIDGETS):
            flush()
        current.append(widget)
        spent += cost
    flush()

    if not cards:  # pragma: no cover - the lead line is always present
        cards = [{"cardId": "approval-queue-tracker-0", "card": {"header": header, "sections": []}}]
    return cards


class GoogleChatClient:
    """Posts approval and notice cards into one space; never raises into the pipeline.

    Args:
        token_source: App-auth token source (``CHAT_APP_SCOPES``). Anything with a
            ``token() -> str`` method fits, which is what tests inject.
        space: Chat space resource name, ``spaces/XXXX``.
        reply_to: Shown on approval cards so reviewers see the exact headers a send
            will carry.
        interactive: Render buttons. Only the Lambda in ``approval_mode=chat`` sets
            this — everything else posts view-only cards.
        post_ledger: Cross-run dedup for notice cards (escalations re-run every poll).
            ``None`` (local runs) posts every time, mirroring the block ledger's
            "no ledger, no memory" convention.
    """

    def __init__(
        self,
        token_source: Any,
        space: str,
        *,
        reply_to: str = "",
        interactive: bool = False,
        action_url: str = "",
        post_ledger: ChatPostLedger | None = None,
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not space:
            raise ValueError("GoogleChatClient needs a space (spaces/…)")
        self._tokens = token_source
        self._space = space.strip().strip("/")
        self._reply_to = reply_to
        self._interactive = interactive
        self._action_url = action_url.strip()
        self._ledger = post_ledger
        self._transport: HttpTransport = transport or UrllibTransport()
        self._timeout = timeout
        #: Correlation id → posted message resource name. The runner stores this on the
        #: pending entry so the sweep can update the card at expiry.
        self.posts: dict[str, str] = {}
        #: Correlation ids whose card could not be posted — the runner's signal to fall
        #: back to a Gmail draft rather than leave the reply with no review surface.
        self.failed: set[str] = set()

    @property
    def interactive(self) -> bool:
        return self._interactive

    # -- SlackClient protocol --------------------------------------------------
    def post_approval(
        self,
        channel: str,
        summary: ApprovalSummary,
        draft_reply: str,
        correlation_id: str,
    ) -> SlackPost:
        """Post the approval card. ``channel`` is ignored — the space is the channel."""

        if self._ledger is not None and self._ledger.posted("approval", correlation_id):
            return SlackPost(slack_ts=self.posts.get(correlation_id, ""), channel=self._space)

        entry_id = entry_id_for(correlation_id)
        card = approval_card(
            entry_id=entry_id,
            from_email=summary.from_,
            load_ids=summary.load_ids,
            to=summary.from_,
            cc=summary.cc,
            reply_to=self._reply_to,
            body=draft_reply,
            interactive=self._interactive,
            message_id=correlation_id,
            action_url=self._action_url,
        )
        name = self._post_card(
            card,
            thread_key=entry_id,
            fallback_text=f"Draft reply for {summary.from_} (loads {', '.join(summary.load_ids)})",
            correlation_id=correlation_id,
            kind="approval",
        )
        return SlackPost(slack_ts=name, channel=self._space)

    def post_escalation(
        self,
        channel: str,
        severity: str,
        reason: str,
        load_ids: tuple[str, ...],
        correlation_id: str,
    ) -> SlackPost:
        """Post a notice card with the short reason. ``channel`` is ignored (one space).

        A gate block arrives here too — ``_escalate`` routes both — recognisable by the
        reason prefix the pipeline writes, and titled as a block so reviewers can tell
        "the bot refused its own draft" from "the bot never drafted".
        """

        kind = "blocked" if reason.startswith("pre-send gate blocked") else "escalated"
        if self._ledger is not None and self._ledger.posted(kind, correlation_id):
            return SlackPost(slack_ts="", channel=self._space)

        card = notice_card(
            correlation_id=correlation_id,
            kind=kind,
            severity=severity,
            reason=reason,
            load_ids=load_ids,
        )
        name = self._post_card(
            card,
            thread_key=entry_id_for(correlation_id),
            fallback_text=f"{kind}: {_trim(reason, _REASON_LIMIT)}",
            correlation_id=correlation_id,
            kind=kind,
        )
        return SlackPost(slack_ts=name, channel=self._space)

    # -- card updates ------------------------------------------------------------
    def patch_card(self, message_name: str, cards: list[dict[str, Any]]) -> int:
        """Replace a posted card in place. Returns the HTTP status; 0 if it never went out.

        The status, not a bool, because the caller's next move depends on WHICH failure.
        A 5xx is worth retrying on the same message; a 403/404 means Chat will not let us
        touch it again — the API returns "permission denied … or the resource doesn't exist"
        for both a foreign message and a deleted one, so they are indistinguishable here.
        """

        if not message_name:
            return 0
        try:
            response = self._transport.request(
                "PATCH",
                f"{CHAT_API_BASE}/{urllib.parse.quote(message_name)}?updateMask=cardsV2",
                headers=self._headers(),
                body=json.dumps({"cardsV2": cards}).encode("utf-8"),
                timeout=self._timeout,
            )
            if not response.ok:
                _log.warning(
                    "chat_card_update_failed",
                    extra={
                        "chat_message": message_name,
                        "error": f"HTTP {response.status}: {response.text()[:200]}",
                    },
                )
            return int(response.status)
        except Exception as exc:
            _log.warning(
                "chat_card_update_failed",
                extra={"chat_message": message_name, "error": str(exc)},
            )
            return 0

    def update_status(self, message_name: str, card: dict[str, Any]) -> bool:
        """Replace a posted card in place (expiry sweeps). True on success.

        Single-card convenience: an approval card is always one card, so the sweep's contract
        is unchanged by the tracker learning to span several.
        """

        return self.patch_card(message_name, [card]) == 200

    def post_card(self, cards: list[dict[str, Any]], *, fallback_text: str) -> str:
        """Post one standalone card and return its resource name, or ``""`` on failure.

        **Unthreaded, and it must stay that way.** A pin applies to a message, so a tracker
        posted into a thread is not pinnable anywhere a reviewer would find it.

        This briefly went out with a fixed ``threadKey`` and
        ``messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD``, chasing a 403 that
        PATCH kept returning on tracker messages while the identical PATCH succeeded on 55
        of 58 approval cards. The 403 turned out to be simpler than that: the cards had been
        deleted by hand, and Chat reports a deleted message and a forbidden one with the same
        "permission denied … or the resource doesn't exist". Copying the approval cards'
        creation path fixed nothing and broke something — they use a UNIQUE key per email, so
        they always start a new thread, whereas one fixed key means every repost after the
        first lands as a reply inside the old thread. Deleted cards are already handled, by
        the miss counter in ``lambda_handler._refresh_queue_tracker``.
        """

        try:
            response = self._transport.request(
                "POST",
                f"{CHAT_API_BASE}/{urllib.parse.quote(self._space)}/messages",
                headers=self._headers(),
                body=json.dumps({"text": fallback_text, "cardsV2": cards}).encode("utf-8"),
                timeout=self._timeout,
            )
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status}: {response.text()[:200]}")
            data = response.json()
            name = str(data.get("name") or "") if isinstance(data, dict) else ""
        except Exception as exc:
            _log.warning("chat_tracker_post_failed", extra={"error": str(exc)})
            return ""
        _log.info("chat_tracker_posted", extra={"chat_message": name})
        return name

    # -- internals ---------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post_card(
        self,
        card: dict[str, Any],
        *,
        thread_key: str,
        fallback_text: str,
        correlation_id: str,
        kind: str,
    ) -> str:
        """POST one card; record success in the ledger, failure in ``self.failed``.

        Returns the message resource name, or ``""`` on failure — the empty name is the
        protocol's existing "posted nowhere" shape (NullSlackClient returns it too).
        """

        payload = {
            # threadKey groups the approval card and any notice about the same email;
            # in an unthreaded space the option makes it a plain message instead of an
            # error, so this is safe whichever way the space was created.
            "thread": {"threadKey": thread_key},
            "text": fallback_text,
            "cardsV2": [card],
        }
        url = (
            f"{CHAT_API_BASE}/{urllib.parse.quote(self._space)}/messages"
            "?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        )
        try:
            response = self._transport.request(
                "POST",
                url,
                headers=self._headers(),
                body=json.dumps(payload).encode("utf-8"),
                timeout=self._timeout,
            )
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status}: {response.text()[:300]}")
            data = response.json()
            name = str(data.get("name") or "") if isinstance(data, dict) else ""
        except Exception as exc:
            # Never raises into the pipeline: an exception here would be misfiled as a
            # pipeline crash and escalate the email over a chat outage. The runner sees
            # `failed` and falls back to a Gmail draft instead.
            self.failed.add(correlation_id)
            _log.warning(
                "chat_post_failed",
                extra={"correlation_id": correlation_id, "kind": kind, "error": str(exc)},
            )
            return ""

        self.posts[correlation_id] = name
        if self._ledger is not None:
            self._ledger.record(kind, correlation_id)
        # "chat_message", not "message": the latter is a reserved LogRecord attribute and
        # overwriting it raises at log time once logging is configured.
        _log.info(
            "chat_card_posted",
            extra={"correlation_id": correlation_id, "kind": kind, "chat_message": name},
        )
        return name


def build_google_chat_client(
    settings: Any,
    *,
    interactive: bool,
    post_ledger: ChatPostLedger | None = None,
    transport: HttpTransport | None = None,
) -> GoogleChatClient:
    """Construct the client from settings; import-light so local runs without the
    ``google`` extra never pay for it unless a space is configured."""

    from payment_bot.clients.google_auth import (
        CHAT_APP_SCOPES,
        ServiceAccountTokenSource,
        load_service_account_info,
    )

    info = load_service_account_info(
        file_path=settings.google_sa_file,
        inline_json=settings.google_sa_json.get_secret_value(),
    )
    tokens = ServiceAccountTokenSource(
        info,
        subject="",
        scopes=CHAT_APP_SCOPES,
        app_auth=True,
        transport=transport,
        timeout=settings.google_timeout_seconds,
    )
    return GoogleChatClient(
        tokens,
        settings.chat_space,
        reply_to=settings.reply_to,
        interactive=interactive,
        action_url=settings.chat_action_url,
        post_ledger=post_ledger,
        transport=transport,
        timeout=settings.google_timeout_seconds,
    )
