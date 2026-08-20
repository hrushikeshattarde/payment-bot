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
from typing import Any

from payment_bot.approvals import ChatPostLedger, entry_id_for
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


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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
) -> dict[str, Any]:
    """The approval card, shared by the poster and the callback's in-place updates.

    ``status`` non-empty renders the terminal form: status line shown, buttons gone —
    what a card becomes after a click or an expiry, so the feed doubles as the audit
    trail humans read.
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

    if status:
        sections.append({"widgets": [{"decoratedText": {"topLabel": "Status", "text": status}}]})
    elif interactive:
        parameters = [{"key": "entry", "value": entry_id}]
        sections.append(
            {
                "widgets": [
                    {
                        "buttonList": {
                            "buttons": [
                                {
                                    "text": "Approve & send as me",
                                    "onClick": {
                                        "action": {
                                            "function": ACTION_APPROVE,
                                            "parameters": parameters,
                                        }
                                    },
                                },
                                {
                                    "text": "Move to my Gmail Drafts",
                                    "onClick": {
                                        "action": {
                                            "function": ACTION_MOVE,
                                            "parameters": parameters,
                                        }
                                    },
                                },
                                {
                                    "text": "Reject",
                                    "onClick": {
                                        "action": {
                                            "function": ACTION_REJECT,
                                            "parameters": parameters,
                                        }
                                    },
                                },
                            ]
                        }
                    }
                ]
            }
        )
    else:
        sections.append(
            {
                "widgets": [
                    {
                        "decoratedText": {
                            "topLabel": "Status",
                            "text": "Shadow mode — review and send from Gmail Drafts as usual.",
                        }
                    }
                ]
            }
        )

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
    return {
        "cardId": f"notice-{entry_id_for(correlation_id)}",
        "card": {
            "header": {"title": title, "subtitle": f"severity: {severity}"},
            "sections": [
                {
                    "widgets": [
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
                }
            ],
        },
    }


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
    def update_status(self, message_name: str, card: dict[str, Any]) -> bool:
        """Replace a posted card in place (expiry sweeps). True on success."""

        if not message_name:
            return False
        try:
            response = self._transport.request(
                "PATCH",
                f"{CHAT_API_BASE}/{urllib.parse.quote(message_name)}?updateMask=cardsV2",
                headers=self._headers(),
                body=json.dumps({"cardsV2": [card]}).encode("utf-8"),
                timeout=self._timeout,
            )
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status}: {response.text()[:200]}")
            return True
        except Exception as exc:
            _log.warning(
                "chat_card_update_failed",
                extra={"chat_message": message_name, "error": str(exc)},
            )
            return False

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
        post_ledger=post_ledger,
        transport=transport,
        timeout=settings.google_timeout_seconds,
    )
