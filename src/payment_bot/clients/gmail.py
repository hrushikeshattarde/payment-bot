"""Gmail client (§4.5).

Only two operations matter for this slice: fetch new mail and send a reply. **Sending
is the one irreversible side effect in the whole system**, so the protocol makes it a
single, explicit method that the pipeline calls *only after* the pre-send gate and (in
Phase 1) human approval — never the agent directly.

Production uses a Gmail API service account with domain-wide delegation (§8.1.2). The
mock records sent messages so tests can assert exactly what would have gone out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from payment_bot.clients.mime import reply_subject
from payment_bot.models import InboundEmail


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Record of a reply the bot sent (or would have sent, in the mock)."""

    sent_message_id: str
    thread_id: str
    in_reply_to: str
    to: str
    body: str


@dataclass(frozen=True, slots=True)
class DraftMessage:
    """A reply saved to the Drafts folder — reviewable, and **not sent**.

    Creating a draft is the safe half of "reply": the message exists in the mailbox for a
    human to read, edit, and send, but nothing has left the building. It is deliberately a
    separate operation from :meth:`GmailClient.send_reply`.
    """

    folder: str
    to: str
    cc: tuple[str, ...]
    subject: str
    body: str
    in_reply_to: str | None = None


@runtime_checkable
class DraftingGmailClient(Protocol):
    """Read access plus draft creation — everything a draft-only run needs.

    Kept separate from :class:`GmailClient` because it deliberately does **not** include
    ``send_reply``: a client can satisfy this protocol while being incapable of sending.
    """

    def fetch_new(self, since: str | None = None) -> list[InboundEmail]: ...

    def create_draft(
        self,
        email: InboundEmail,
        body: str,
        cc: tuple[str, ...] = (),
    ) -> DraftMessage:
        """Save a reply to ``email`` in the Drafts folder. Never sends."""
        ...


@runtime_checkable
class GmailClient(Protocol):
    """Read + send access to the payments mailbox."""

    def fetch_new(self, since: str | None = None) -> list[InboundEmail]:
        """Return unprocessed inbound messages (§4.5 ``gmail_fetch_new``)."""

    def send_reply(
        self,
        thread_id: str,
        message_id_in_reply_to: str,
        body: str,
        to: str,
    ) -> SentMessage:
        """Send a threaded reply. Callable ONLY after the pre-send gate + approval."""


class MockGmailClient:
    """In-memory Gmail: a seeded inbox, plus records of drafts and anything "sent"."""

    def __init__(self, inbox: list[InboundEmail] | None = None) -> None:
        self._inbox: list[InboundEmail] = list(inbox or [])
        self.sent: list[SentMessage] = []
        self.drafts: list[DraftMessage] = []
        self._counter = 0

    def seed(self, email: InboundEmail) -> None:
        self._inbox.append(email)

    def create_draft(
        self,
        email: InboundEmail,
        body: str,
        cc: tuple[str, ...] = (),
    ) -> DraftMessage:
        draft = DraftMessage(
            folder="[Gmail]/Drafts",
            to=email.from_email,
            cc=cc,
            subject=reply_subject(email.subject),
            body=body,
            in_reply_to=email.message_id,
        )
        self.drafts.append(draft)
        return draft

    def fetch_new(self, since: str | None = None) -> list[InboundEmail]:
        return list(self._inbox)

    def send_reply(
        self,
        thread_id: str,
        message_id_in_reply_to: str,
        body: str,
        to: str,
    ) -> SentMessage:
        self._counter += 1
        message = SentMessage(
            sent_message_id=f"mock-sent-{self._counter}",
            thread_id=thread_id,
            in_reply_to=message_id_in_reply_to,
            to=to,
            body=body,
        )
        self.sent.append(message)
        return message
