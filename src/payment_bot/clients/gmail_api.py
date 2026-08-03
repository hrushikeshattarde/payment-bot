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
from email.utils import parseaddr
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
from payment_bot.models import InboundEmail

_log = get_logger("clients.gmail_api")

GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1"

#: How many matching MESSAGES to list before thread filtering. Deliberately much larger
#: than any per-run processing limit: with ``mark_seen`` off, unread messages in threads
#: we already answered keep matching the intake query forever, and a listing window sized
#: to the processing limit starves fresh mail behind them (observed live — see fetch_new).
_LISTING_WINDOW = 100


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
    ) -> None:
        self._tokens = token_source
        self._user = user or token_source.subject
        self._query = query
        self._limit = max(1, limit)
        self._mark_read = mark_read
        self._transport: HttpTransport = transport or UrllibTransport()
        self._timeout = timeout
        #: The monitored group address (``paystatus@…``). Google Groups rewrites the From
        #: of DMARC-strict external senders to this address ("teamamy via Payment Status
        #: <paystatus@…>"), so mail *from* it is a carrier arriving through the group —
        #: never a colleague. Without this, every unanswered email from such a sender was
        #: skipped as "already answered by us" and the sender was invisible to the bot.
        self._group = group_address.strip().lower()

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
        ids: list[str] = []
        skipped = 0
        for message_id, thread_id in matched:
            if not thread_id:
                ids.append(message_id)
                continue
            if thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)
            target = self._thread_reply_target(thread_id)
            if target is None:
                skipped += 1
                continue
            ids.append(target)

        if skipped:
            _log.info(
                "gmail_api_threads_skipped",
                extra={"skipped": skipped, "reason": "already answered or already drafted"},
            )
        if len(ids) > self._limit:
            _log.warning(
                "gmail_api_backlog",
                extra={"actionable": len(ids), "processing": self._limit},
            )
            ids = ids[: self._limit]
        if not ids:
            _log.info("gmail_api_no_actionable_threads", extra={"query": query})
            return []

        emails: list[InboundEmail] = []
        for message_id in ids:
            record = self._get(
                f"/users/{self._quoted_user()}/messages/{urllib.parse.quote(message_id)}",
                {"format": "RAW"},
            )
            raw = record.get("raw")
            if not isinstance(raw, str):
                _log.warning("gmail_api_message_without_raw", extra={"id": message_id})
                continue
            try:
                decoded = base64.urlsafe_b64decode(_pad_base64(raw))
            except (ValueError, TypeError) as exc:
                _log.warning("gmail_api_undecodable_message", extra={"id": message_id, "error": str(exc)})
                continue
            emails.append(
                parse_inbound_email(
                    email.message_from_bytes(decoded),
                    thread_id=str(record.get("threadId") or "") or None,
                    labels=[str(label) for label in (record.get("labelIds") or [])],
                    group_address=self._group or None,
                )
            )

        _log.info("gmail_api_fetched", extra={"count": len(emails), "query": query})
        return emails

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

    def _thread_reply_target(self, thread_id: str) -> str | None:
        """The id of the message to answer in this thread, or ``None`` if none needs it.

        Three reasons a thread needs nothing:

        * **A draft already exists in it.** Gmail keeps drafts in the thread, so this is what
          stops a re-run adding a second draft to the same conversation.
        * **The newest message is ours.** Somebody on our side has already replied — a
          colleague answering by hand, or an earlier run that was sent.
        * **The newest message is a reply we sent.** Same as above; ownership is decided by
          the sender's domain, not by the mailbox being impersonated, because group mail
          arrives from colleagues on the same domain.

        Otherwise the answer is the thread's newest message, which may be *newer* than the one
        the query matched — a carrier who followed up twice should get one reply to the latest.

        A metadata-only thread read; it fetches no bodies.
        """

        thread = self._get(
            f"/users/{self._quoted_user()}/threads/{urllib.parse.quote(thread_id)}",
            {"format": "metadata", "metadataHeaders": "From"},
        )
        messages = [m for m in (thread.get("messages") or []) if isinstance(m, dict)]
        if not messages:
            return None

        newest: dict[str, Any] | None = None
        newest_at = -1
        for message in messages:
            if "DRAFT" in (message.get("labelIds") or []):
                return None
            try:
                stamp = int(message.get("internalDate") or 0)
            except (TypeError, ValueError):
                stamp = 0
            if stamp >= newest_at:
                newest_at, newest = stamp, message
        if newest is None:
            return None

        if self._is_ours(_header_value(newest, "From")):
            return None
        return str(newest.get("id") or "") or None

    def _is_ours(self, from_header: str) -> bool:
        """True when a message was sent by someone on our side — a colleague or ourselves.

        The monitored group address itself is the exception: DMARC-strict external senders
        arrive with From rewritten to exactly that address, so it marks a carrier coming
        *through* the group, not a reply going out. Verified live: an OTR Solutions rate
        verification read "teamamy via Payment Status <paystatus@…>" and was skipped as
        already-answered until this carve-out existed.
        """

        _, address = parseaddr(from_header)
        normalized = address.lower()
        if self._group and normalized == self._group:
            return False
        domain = self._user.rsplit("@", 1)[-1].lower()
        return bool(domain) and normalized.endswith(f"@{domain}")

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{GMAIL_API_ROOT}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
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


def _pad_base64(value: str) -> str:
    """Restore the ``=`` padding Gmail omits from base64url payloads."""

    return value + "=" * (-len(value) % 4)


def build_gmail_api_client(
    settings: Settings | None = None,
    transport: HttpTransport | None = None,
) -> GmailApiClient:
    """Build a :class:`GmailApiClient` from ``PAYBOT_GOOGLE_*`` / ``PAYBOT_GMAIL_*`` config."""

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
    )
