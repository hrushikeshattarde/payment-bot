"""Gmail API intake and draft creation via a service account (PRD §4.5 / §8.1.2).

The production-shaped Gmail backend: a service account with domain-wide delegation
impersonates ``paystatus@``, reads mail with ``messages.list`` / ``messages.get``, and saves
replies with ``drafts.create``. This is the path to use when you hold service-account
credentials rather than an account password.

Three endpoints, nothing more:

===========================  ============================================================
Operation                    Gmail API call
===========================  ============================================================
``fetch_new``                ``GET  /gmail/v1/users/{user}/messages?q=…``  then
                             ``GET  /gmail/v1/users/{user}/messages/{id}?format=RAW``
``create_draft``             ``POST /gmail/v1/users/{user}/drafts``
``send_reply``               *(never called — raises)*
===========================  ============================================================

``format=RAW`` returns the original RFC822 bytes, parsed by
:mod:`payment_bot.clients.mime`.

**On the no-send guarantee.** This credential *can* technically send: Google offers no
draft-only scope, and ``gmail.compose`` — the narrowest scope allowing ``drafts.create`` —
also permits ``messages.send``. So the guarantee is enforced by our code rather than by the
credential: :meth:`GmailApiClient.send_reply` raises, the pipeline never takes the send
path, and the approval resolver never approves. Worth knowing, and worth not pretending
otherwise.
"""

from __future__ import annotations

import base64
import email
import json
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any

from payment_bot.clients.gmail import DraftMessage, SentMessage
from payment_bot.clients.google_auth import (
    GMAIL_DRAFT_SCOPES,
    ServiceAccountTokenSource,
    load_service_account_info,
)
from payment_bot.clients.http import HttpTransport, UrllibTransport
from payment_bot.clients.mime import build_reply, parse_inbound_email, reply_subject
from payment_bot.config import Settings, get_settings
from payment_bot.errors import ClientError
from payment_bot.logging import get_logger
from payment_bot.models import InboundEmail, PriorReply

_log = get_logger("clients.gmail_api")

GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1"

#: How many matching MESSAGES to list before thread filtering. Deliberately much larger
#: than any per-run processing limit: with ``mark_seen`` off, unread messages in threads
#: we already answered keep matching the intake query forever, and a listing window sized
#: to the processing limit starves fresh mail behind them (observed live — see fetch_new).
#:
#: Raised from 100 when the intake window widened to 4 days. The pool this lists is every
#: unread message in the window, answered threads included, and it only ever grows within
#: that window because `mark_seen` is off: runs were already listing ~40 and skipping 26 of
#: them as answered. Doubling the window doubles that pool, and starving fresh mail is the
#: one failure this constant exists to prevent.
_LISTING_WINDOW = 250

#: Headers a ``format=metadata`` thread read asks for. From decides ownership; To and Cc tell
#: a reply to the carrier from a note between colleagues; Message-ID locates one message.
_THREAD_HEADERS = ("From", "To", "Cc", "Message-ID")


@dataclass(frozen=True, slots=True)
class _ReplyTarget:
    """The message to answer in one thread, and the reply of ours it follows up on, if any."""

    message_id: str
    #: Gmail id of the colleague's reply that a follow-up answers. ``None`` for a thread
    #: nobody here has written in — the ordinary case.
    prior_reply_id: str | None = None


class SendingDisabledError(ClientError):
    """Raised when something tries to send mail in a draft-only run."""


class GmailApiClient:
    """Fetch + draft access to one mailbox through the Gmail API.

    Args:
        token_source: Supplies impersonated access tokens.
        user: Mailbox to operate on. Defaults to the impersonated subject.
        query: Gmail search query for intake, e.g. ``is:unread``. Gmail's own search
            syntax, not IMAP's.
        limit: Newest-N cap per fetch.
        mark_read: Remove the ``UNREAD`` label after fetching. Off while iterating, so the
            same mail can be reprocessed. Requires a scope permitting modify.
        transport: Injectable HTTP seam.
        follow_ups: Answer a carrier's follow-up to a colleague's reply instead of skipping
            every thread our side has written in (``Settings.followup_replies``).
    """

    def __init__(
        self,
        token_source: ServiceAccountTokenSource,
        *,
        user: str = "",
        query: str = "is:unread",
        limit: int = 10,
        mark_read: bool = False,
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
        group_address: str = "",
        group_members: tuple[str, ...] = (),
        reply_to: str = "",
        follow_ups: bool = False,
        follow_up_asks: Callable[[InboundEmail], bool] | None = None,
    ) -> None:
        self._tokens = token_source
        self._user = user or token_source.subject
        self._query = query
        self._limit = max(1, limit)
        self._mark_read = mark_read
        self._transport: HttpTransport = transport or UrllibTransport()
        self._timeout = timeout
        #: Reply-To on every draft this client writes — the group address, so a plain
        #: Reply returns to the monitored mailbox whoever's name the reply carries.
        self._reply_to = reply_to.strip()
        #: Members of the monitored group whose addresses are NOT on our own domain. The
        #: domain rule already covers colleagues; this exists for a member who sits outside
        #: it. Lowercased once here so `_is_ours` is a plain set lookup.
        self._group_members = frozenset(m.strip().lower() for m in group_members if m.strip())
        #: The monitored group address (``paystatus@…``). Google Groups rewrites the From
        #: of DMARC-strict external senders to this address ("teamamy via Payment Status
        #: <paystatus@…>"), so mail *from* it is a carrier arriving through the group —
        #: never a colleague. Without this, every unanswered email from such a sender was
        #: skipped as "already answered by us" and the sender was invisible to the bot.
        self._group = group_address.strip().lower()
        #: Answer a carrier's follow-up to a colleague's reply (``Settings.followup_replies``)
        #: rather than leaving the whole thread to that colleague. Which threads qualify is
        #: decided in :meth:`_followed_up_reply`.
        self._follow_ups = follow_ups
        #: Whether a follow-up asks for anything (``tools.shared.follow_up_asks``, injected by
        #: the factory — the tools layer imports this one). One that does not is dropped here,
        #: before it takes a processing slot: the mailbox stays unread, so a closing "thanks"
        #: would otherwise re-take a slot every run and starve fresh mail behind it.
        self._follow_up_asks = follow_up_asks

    @property
    def user(self) -> str:
        """The mailbox this client reads and drafts in."""

        return self._user

    # -- diagnostics ---------------------------------------------------------
    def verify_access(self) -> dict[str, Any]:
        """Confirm impersonation works, via the cheapest read-only call there is.

        ``users.getProfile`` needs only ``gmail.readonly`` and touches no message, so it
        separates *"delegation is working"* from *"the search matched nothing"* — two very
        different problems that a plain fetch would conflate.

        Returns the profile: ``emailAddress``, ``messagesTotal``, ``threadsTotal``.
        """

        return self._get(f"/users/{self._quoted_user()}/profile")

    # -- GmailClient / DraftingGmailClient -----------------------------------
    def fetch_new(self, since: str | None = None) -> list[InboundEmail]:
        """Return the messages in matching threads that still need a reply.

        A Gmail query matches individual *messages*, which is the wrong unit of work here. On
        live mail that meant answering conversations a colleague had already handled, and
        producing a second draft for a thread that already had one — every re-run added
        another, because ``mark_seen`` is off by design.

        So the listing is collapsed to one candidate per thread and each is checked against
        the thread itself. See :meth:`_thread_reply_target`.

        Args:
            since: Optional ``YYYY/MM/DD`` date, added as a Gmail ``after:`` term.
        """

        query = self._query
        if since:
            query = f"{query} after:{since}".strip()

        # List WIDE, filter, then apply the processing limit. The limit used to cap the
        # listing itself, and because ``mark_seen`` is off, unread messages in threads we
        # already answered accumulate and keep matching the query forever — observed live:
        # ten newer vendor replies in answered threads filled the entire window and a fresh
        # carrier email an hour old was never fetched, on every run. ``self._limit`` now
        # means what it says: how many emails one run may PROCESS.
        listing = self._get(
            f"/users/{self._quoted_user()}/messages",
            {"q": query, "maxResults": str(_LISTING_WINDOW)},
        )
        matched = [
            (str(item["id"]), str(item.get("threadId") or ""))
            for item in (listing.get("messages") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        if not matched:
            _log.info("gmail_api_no_matches", extra={"query": query})
            return []

        # Gmail lists newest first, so the first sighting of a thread is its newest match.
        seen_threads: set[str] = set()
        targets: list[_ReplyTarget] = []
        skipped = 0
        for message_id, thread_id in matched:
            if not thread_id:
                targets.append(_ReplyTarget(message_id))
                continue
            if thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)
            target = self._thread_reply_target(thread_id)
            if target is None:
                skipped += 1
                continue
            targets.append(target)

        if skipped:
            _log.info(
                "gmail_api_threads_skipped",
                extra={"skipped": skipped, "reason": "already answered or already drafted"},
            )
        if len(targets) > self._limit:
            _log.warning(
                "gmail_api_backlog",
                extra={"actionable": len(targets), "processing": self._limit},
            )
        if not targets:
            _log.info("gmail_api_no_actionable_threads", extra={"query": query})
            return []

        # Filled to the limit rather than cut to it first: a follow-up that asks for nothing
        # is only known as one once its body is read, and it must not cost fresh mail a slot.
        emails: list[InboundEmail] = []
        for target in targets:
            if len(emails) >= self._limit:
                break
            inbound = self._fetch_message(target.message_id)
            if inbound is None:
                continue
            if target.prior_reply_id is not None:
                if self._follow_up_asks is not None and not self._follow_up_asks(inbound):
                    _log.info(
                        "gmail_api_follow_up_asks_nothing",
                        extra={"id": target.message_id, "thread_id": inbound.thread_id},
                    )
                    continue
                prior = self._prior_reply(target.prior_reply_id)
                if prior is None:
                    # Without the reply it follows up on, the draft would answer blind and
                    # could contradict it. Leave the thread with whoever answered, as before.
                    _log.warning(
                        "gmail_api_follow_up_without_prior_reply",
                        extra={"id": target.message_id, "prior": target.prior_reply_id},
                    )
                    continue
                inbound = inbound.model_copy(update={"prior_reply": prior})
            emails.append(inbound)

        _log.info("gmail_api_fetched", extra={"count": len(emails), "query": query})
        return emails

    def search(self, query: str, limit: int = 500) -> list[InboundEmail]:
        """Every message matching ``query``, oldest handling rules deliberately not applied.

        The read-only counterpart of :meth:`fetch_new`, for offline analysis rather than for
        deciding what to answer. Everything :meth:`fetch_new` does to pick *work* is wrong
        here and is left out: no collapsing to one message per thread, and no skipping of
        threads a colleague already answered — for contact discovery those threads are the
        best evidence there is, since a human replying to an address is a human accepting it.

        Paginates, because the point is history rather than a window. ``limit`` caps messages
        fetched, and each one costs a request.
        """

        emails: list[InboundEmail] = []
        page_token: str | None = None
        while len(emails) < limit:
            params = {"q": query, "maxResults": str(min(_LISTING_WINDOW, limit - len(emails)))}
            if page_token:
                params["pageToken"] = page_token
            listing = self._get(f"/users/{self._quoted_user()}/messages", params)
            items = [
                str(item["id"])
                for item in (listing.get("messages") or [])
                if isinstance(item, dict) and item.get("id")
            ]
            for message_id in items:
                if len(emails) >= limit:
                    break
                parsed = self._fetch_message(message_id)
                if parsed is not None:
                    emails.append(parsed)
            page_token = listing.get("nextPageToken")
            if not page_token or not items:
                break

        _log.info("gmail_api_searched", extra={"count": len(emails), "query": query})
        return emails

    def _fetch_message(self, message_id: str) -> InboundEmail | None:
        """One message by id, or ``None`` when it cannot be decoded."""

        fetched = self._fetch_raw(message_id)
        if fetched is None:
            return None
        record, mime = fetched
        return parse_inbound_email(
            mime,
            thread_id=str(record.get("threadId") or "") or None,
            labels=[str(label) for label in (record.get("labelIds") or [])],
            group_address=self._group or None,
        )

    def _prior_reply(self, message_id: str) -> PriorReply | None:
        """The reply of ours that a follow-up answers, or ``None`` when it cannot be read."""

        fetched = self._fetch_raw(message_id)
        if fetched is None:
            return None
        record, mime = fetched
        parsed = parse_inbound_email(mime, thread_id=str(record.get("threadId") or "") or None)
        try:
            sent_at = parsedate_to_datetime(str(mime.get("Date") or ""))
        except (TypeError, ValueError, IndexError):
            sent_at = None
        return PriorReply(
            from_email=parsed.from_email,
            from_name=parsed.from_name,
            sent_at=sent_at,
            body=parsed.body,
        )

    def _fetch_raw(self, message_id: str) -> tuple[dict[str, Any], Message] | None:
        """The ``format=RAW`` record and its parsed MIME, or ``None`` when undecodable."""

        record = self._get(
            f"/users/{self._quoted_user()}/messages/{urllib.parse.quote(message_id)}",
            {"format": "RAW"},
        )
        raw = record.get("raw")
        if not isinstance(raw, str):
            _log.warning("gmail_api_message_without_raw", extra={"id": message_id})
            return None
        try:
            decoded = base64.urlsafe_b64decode(_pad_base64(raw))
        except (ValueError, TypeError) as exc:
            _log.warning(
                "gmail_api_undecodable_message", extra={"id": message_id, "error": str(exc)}
            )
            return None
        return record, email.message_from_bytes(decoded)

    def create_draft(
        self,
        email_message: InboundEmail,
        body: str,
        cc: tuple[str, ...] = (),
    ) -> DraftMessage:
        """Save a reply as a Gmail draft. **Never sends.**

        ``drafts.create`` stores the message; delivery would require ``drafts.send`` or
        ``messages.send``, neither of which this codebase calls. Passing ``threadId`` keeps
        the draft in the carrier's existing conversation.
        """

        subject = reply_subject(email_message.subject)
        mime = build_reply(
            email_message,
            body,
            from_address=self._user,
            cc=cc,
            subject=subject,
            reply_to=self._reply_to,
        )
        payload: dict[str, Any] = {
            "message": {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")}
        }
        if email_message.thread_id:
            payload["message"]["threadId"] = email_message.thread_id

        created = self._post(f"/users/{self._quoted_user()}/drafts", payload)
        draft_id = str(created.get("id") or "")
        _log.info(
            "gmail_api_draft_created",
            extra={"draft_id": draft_id, "to": email_message.from_email, "cc": list(cc)},
        )
        return DraftMessage(
            folder=f"Drafts (id {draft_id})" if draft_id else "Drafts",
            to=email_message.from_email,
            cc=cc,
            subject=subject,
            body=body,
            in_reply_to=email_message.message_id or None,
        )

    def send_reply(
        self,
        thread_id: str,
        message_id_in_reply_to: str,
        body: str,
        to: str,
    ) -> SentMessage:
        """Always raises — this client never sends, by policy.

        The credential's ``gmail.compose`` scope would permit ``messages.send``; we simply do
        not implement it. Drafts go to the mailbox for a human to send.
        """

        raise SendingDisabledError(
            "sending is disabled: GmailApiClient is draft-only. The reply is saved in "
            "Drafts — review it and press Send yourself."
        )

    # -- HTTP ----------------------------------------------------------------
    def _quoted_user(self) -> str:
        return urllib.parse.quote(self._user)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.token()}",
            "Accept": "application/json",
        }

    def thread_state(self, thread_id: str, after_message_id: str = "") -> str:
        """``"handled"``, ``"drafting"`` or ``"open"`` for one thread.

        The tracker's labels come from here. ``fetch_new`` already collapses all three into
        "skip it" via :meth:`_thread_reply_target`, which is right for intake and too coarse
        for a board: the reason a row can be ignored is exactly what a reviewer needs to see.

        **The two are kept apart because only one of them means the carrier heard back.** A
        reply from our side is an answer and retires the row. A draft sitting in the thread
        means a colleague started and may never finish, so that row stays in the queue — and
        retiring it would be how a carrier ends up with no reply at all and no record that one
        was owed.

        Ownership is decided by :meth:`_is_ours`, so a colleague on our domain and a
        configured group member both count, and the group address itself does not — that is a
        carrier arriving through the group, not us writing out.

        Any read failure answers ``"open"``: an unreadable thread must never retire a row.

        ``after_message_id`` (the row's own RFC ``Message-ID``) limits the question to what
        happened AFTER that message. A follow-up's row lives in a thread a colleague already
        answered once — that earlier answer is the reason the follow-up exists, and counting
        it retired the row the moment it was posted. Not found in the thread, the whole
        thread is read, which is the behaviour before follow-ups existed.
        """

        try:
            thread = self._get(
                f"/users/{self._quoted_user()}/threads/{urllib.parse.quote(thread_id)}",
                {"format": "metadata", "metadataHeaders": list(_THREAD_HEADERS)},
            )
        except Exception as exc:
            _log.info("gmail_thread_state_unavailable", extra={"thread_id": thread_id, "error": str(exc)})
            return "open"

        messages = _chronological(thread)
        if after_message_id:
            wanted = after_message_id.strip()
            for position, message in enumerate(messages):
                if _header_value(message, "Message-ID").strip() == wanted:
                    messages = messages[position + 1 :]
                    break
        drafting = False
        for message in messages:
            if "DRAFT" in (message.get("labelIds") or []):
                drafting = True
                continue
            if self._is_ours(_header_value(message, "From")):
                return "handled"
        return "drafting" if drafting else "open"

    def _thread_reply_target(self, thread_id: str) -> _ReplyTarget | None:
        """The message to answer in this thread, or ``None`` if none needs it.

        Reasons a thread needs nothing:

        * **A draft already exists in it.** Gmail keeps drafts in the thread, so this is what
          stops a re-run adding a second draft to the same conversation.
        * **Anyone on our side has written in it, at any point.** A colleague who started the
          thread or replied anywhere in it owns that conversation; ownership is decided by the
          sender's domain and the configured group membership, not by the mailbox being
          impersonated, because group mail arrives from colleagues on the same domain.

        That last rule used to test only the NEWEST message, which missed the common shape: a
        colleague emails a carrier with the group Cc'd, the carrier replies, and the newest
        message is then the carrier's — so the bot drafted into a conversation a human was
        already handling. Most `to:paystatus` matches are colleague mail of exactly this kind.

        Otherwise the answer is the thread's newest message, which may be *newer* than the one
        the query matched — a carrier who followed up twice should get one reply to the latest.

        **The one exception to ownership** is a carrier's follow-up to our answer, and only
        with ``follow_ups`` on: the newest message is then answered too, carrying the id of the
        reply it follows up on. See :meth:`_followed_up_reply` for the exact shape.

        A metadata-only thread read; it fetches no bodies.
        """

        thread = self._get(
            f"/users/{self._quoted_user()}/threads/{urllib.parse.quote(thread_id)}",
            {"format": "metadata", "metadataHeaders": list(_THREAD_HEADERS)},
        )
        messages = _chronological(thread)
        if not messages:
            return None
        if any("DRAFT" in (message.get("labelIds") or []) for message in messages):
            return None

        newest_id = str(messages[-1].get("id") or "")
        ours = [m for m in messages if self._is_ours(_header_value(m, "From"))]
        if not ours:
            return _ReplyTarget(newest_id) if newest_id else None

        # Someone on our side has written in it, so a human has it — unless this is a
        # carrier chasing the answer they were given.
        prior = self._followed_up_reply(messages) if self._follow_ups else None
        if prior is None or not newest_id:
            _log.info(
                "gmail_api_thread_owned_by_us",
                extra={"thread_id": thread_id, "from": _header_value(ours[0], "From")[:80]},
            )
            return None
        _log.info(
            "gmail_api_follow_up",
            extra={
                "thread_id": thread_id,
                "answering": newest_id,
                "after_reply_from": _header_value(prior, "From")[:80],
            },
        )
        return _ReplyTarget(newest_id, prior_reply_id=str(prior.get("id") or "") or None)

    def _followed_up_reply(self, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Our reply that the thread's newest message follows up on, or ``None``.

        All four must hold, and each one is a way to get this wrong if it is dropped:

        * **The carrier started the thread.** A thread a colleague started is outbound mail
          with the group Cc'd — chasing paperwork, say — and the carrier's reply belongs to
          that conversation, not to the payments queue. Most colleague mail is this shape.
        * **Our latest message went to someone outside.** Colleagues discuss a carrier's email
          by replying to the group alone; that is a note, not an answer, and the carrier has
          not heard back. It stays with the humans already discussing it.
        * **Nothing of ours came after it** — true by construction, since it is our latest.
        * **The newest message is not ours**, i.e. the carrier wrote after that reply. Our
          side having spoken last means the carrier is waiting on nothing.

        ``messages`` must be chronological and draft-free (the caller ensures both).
        """

        if self._is_ours(_header_value(messages[0], "From")):
            return None
        last_ours = max(
            (i for i, m in enumerate(messages) if self._is_ours(_header_value(m, "From"))),
            default=None,
        )
        if last_ours is None or last_ours == len(messages) - 1:
            return None
        reply = messages[last_ours]
        return reply if self._addressed_outside(reply) else None

    def _addressed_outside(self, message: dict[str, Any]) -> bool:
        """True when ``message`` went to anyone beyond our side and the group mailbox."""

        recipients = getaddresses(
            [_header_value(message, "To"), _header_value(message, "Cc")]
        )
        for _, address in recipients:
            normalized = address.strip().lower()
            if not normalized or normalized == self._group:
                continue
            if not self._is_ours(normalized):
                return True
        return False

    def _is_ours(self, from_header: str) -> bool:
        """True when a message was sent by someone on our side — a colleague or ourselves.

        Two signals, either sufficient:

        * the sender's domain is our own (covers every colleague without a list to maintain);
        * the sender is a configured member of the monitored group, which catches a member
          whose address is NOT on our domain — a shared mailbox, a contractor, or an alias.

        The monitored group address itself is the exception to both: DMARC-strict external
        senders arrive with From rewritten to exactly that address, so it marks a carrier
        coming *through* the group, not a reply going out. Verified live: an OTR Solutions
        rate verification read "teamamy via Payment Status <paystatus@…>" and was skipped as
        already-answered until this carve-out existed.
        """

        _, address = parseaddr(from_header)
        normalized = address.lower()
        if self._group and normalized == self._group:
            return False
        if normalized and normalized in self._group_members:
            return True
        domain = self._user.rsplit("@", 1)[-1].lower()
        return bool(domain) and normalized.endswith(f"@{domain}")

    def _get(
        self, path: str, params: Mapping[str, str | Sequence[str]] | None = None
    ) -> dict[str, Any]:
        url = f"{GMAIL_API_ROOT}{path}"
        if params:
            # doseq: Gmail takes a repeated parameter (metadataHeaders) once per value.
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        response = self._transport.request(
            "GET", url, headers=self._headers(), timeout=self._timeout
        )
        return self._parse(response.status, response.body, path)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._transport.request(
            "POST",
            f"{GMAIL_API_ROOT}{path}",
            headers={**self._headers(), "Content-Type": "application/json"},
            body=json.dumps(payload).encode(),
            timeout=self._timeout,
        )
        return self._parse(response.status, response.body, path)

    def _parse(self, status: int, body: bytes, path: str) -> dict[str, Any]:
        text = body.decode("utf-8", "replace")
        if status >= 400:
            raise ClientError(self._explain(status, text, path))
        try:
            data = json.loads(text) if text else {}
        except ValueError as exc:
            raise ClientError(f"Gmail API {path} returned non-JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ClientError(f"Gmail API {path} returned a non-object response")
        return data

    def _explain(self, status: int, body: str, path: str) -> str:
        """Turn Gmail's errors into something that names the fix."""

        lowered = body.lower()
        if status in {403, 404} and (
            "accessnotconfigured" in lowered
            or "has not been used in project" in lowered
            or "gmail api has not been used" in lowered
        ):
            project = self._tokens.project_id or "<your project>"
            return (
                f"The Gmail API is not enabled in Google Cloud project {project!r} "
                f"(HTTP {status}). This is common when the service account was created for "
                "another API (Sheets, Drive…). Enable it at "
                f"https://console.cloud.google.com/apis/library/gmail.googleapis.com?project={project} "
                "then retry — it takes a minute to propagate."
            )
        if status == 403 and "insufficient" in lowered:
            return (
                f"Gmail API {path} refused: insufficient scope (HTTP 403). The delegation "
                f"entry must include {', '.join(GMAIL_DRAFT_SCOPES)}. Adding a scope in the "
                "Admin console requires no new key, but tokens must be re-minted."
            )
        if status in {401, 403}:
            return (
                f"Gmail API {path} refused (HTTP {status}). Check that domain-wide delegation "
                f"is authorised for client id {self._tokens.client_id or '(see key)'} and that "
                f"{self._user!r} is a real mailbox in the domain. Detail: {body[:200]}"
            )
        # Google spells this FAILED_PRECONDITION or failedPrecondition depending on the field.
        if status == 400 and "failedprecondition" in lowered.replace("_", ""):
            return (
                f"Gmail API {path}: failed precondition (HTTP 400). Usually {self._user!r} has "
                "no Gmail mailbox — the account exists but Gmail is not enabled for it."
            )
        if status == 404:
            return f"Gmail API {path}: not found (HTTP 404). Is {self._user!r} the right mailbox?"
        if status == 429:
            return f"Gmail API {path}: rate limited (HTTP 429). Lower PAYBOT_GMAIL_FETCH_LIMIT."
        return f"Gmail API {path} failed (HTTP {status}): {body[:250]}"


def _header_value(message: dict[str, Any], name: str) -> str:
    """Read one header out of a ``format=metadata`` message, case-insensitively."""

    headers = ((message.get("payload") or {}).get("headers")) or []
    wanted = name.lower()
    for header in headers:
        if isinstance(header, dict) and str(header.get("name", "")).lower() == wanted:
            return str(header.get("value") or "")
    return ""


def _chronological(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """A thread's messages oldest first, by Gmail's ``internalDate``.

    Stable, so messages sharing a timestamp keep the API's order and the later of them is
    still the newest — what the per-message loop this replaced did with ties.
    """

    def stamp(message: dict[str, Any]) -> int:
        try:
            return int(message.get("internalDate") or 0)
        except (TypeError, ValueError):
            return 0

    messages = [m for m in (thread.get("messages") or []) if isinstance(m, dict)]
    return sorted(messages, key=stamp)


def _pad_base64(value: str) -> str:
    """Restore the ``=`` padding Gmail omits from base64url payloads."""

    return value + "=" * (-len(value) % 4)


def build_gmail_api_client(
    settings: Settings | None = None,
    transport: HttpTransport | None = None,
) -> GmailApiClient:
    """Build a :class:`GmailApiClient` from ``PAYBOT_GOOGLE_*`` / ``PAYBOT_GMAIL_*`` config."""

    # Imported here, not at module level: the tools layer imports the clients package, so a
    # top-level import would be circular.
    from payment_bot.tools.shared import follow_up_asks

    resolved = settings or get_settings()
    info = load_service_account_info(
        file_path=resolved.google_sa_file,
        inline_json=resolved.google_sa_json.get_secret_value(),
    )
    subject = resolved.gmail_user or resolved.mailbox
    scopes = GMAIL_DRAFT_SCOPES if resolved.gmail_create_draft else (GMAIL_DRAFT_SCOPES[0],)
    tokens = ServiceAccountTokenSource(
        info,
        subject=subject,
        scopes=scopes,
        transport=transport,
        timeout=resolved.google_timeout_seconds,
    )
    return GmailApiClient(
        tokens,
        user=subject,
        query=resolved.gmail_query,
        limit=resolved.gmail_fetch_limit,
        mark_read=resolved.gmail_mark_seen,
        transport=transport,
        timeout=resolved.google_timeout_seconds,
        # The group whose From-rewritten mail must not read as "ours" (DMARC senders).
        group_address=resolved.mailbox,
        group_members=resolved.gmail_group_members,
        reply_to=resolved.reply_to,
        follow_ups=resolved.followup_replies,
        follow_up_asks=follow_up_asks,
    )
