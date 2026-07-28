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

from payment_bot.models import InboundEmail


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Record of a reply the bot sent (or would have sent, in the mock)."""

    sent_message_id: str
    thread_id: str
    in_reply_to: str
    to: str
    body: str


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
    """In-memory Gmail: a seeded inbox and a record of everything "sent"."""

    def __init__(self, inbox: list[InboundEmail] | None = None) -> None:
        self._inbox: list[InboundEmail] = list(inbox or [])
        self.sent: list[SentMessage] = []
        self._counter = 0

    def seed(self, email: InboundEmail) -> None:
        self._inbox.append(email)

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
