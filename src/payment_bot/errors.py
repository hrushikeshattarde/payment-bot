"""Exception hierarchy.

A single base (:class:`PaymentBotError`) makes it easy to distinguish "our" errors
from unexpected ones at the process boundary. Tools translate :class:`ToolError`
into the ``{"ok": false, "error": ...}`` envelope required by PRD §4.1.
"""

from __future__ import annotations


class PaymentBotError(Exception):
    """Base class for all application errors."""


class ConfigError(PaymentBotError):
    """Invalid or missing configuration."""


class ToolError(PaymentBotError):
    """A tool failed in an expected, reportable way.

    Raised inside a tool's ``run`` to produce ``{"ok": false, "error": <message>}``
    without crashing the agent loop. Use for validation failures, "not found", and
    upstream-API errors — not for programmer bugs.
    """


class ClientError(PaymentBotError):
    """An external system (Transport Pro / Gmail / Slack / an LLM) returned an error.

    ``status`` carries the HTTP status where there was one, so a caller can tell a missing
    record from a broken upstream without matching on the message text.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LoadCancelledError(ClientError):
    """Transport Pro holds no payable record for this load, which means it was cancelled.

    Not a fault, and the distinction matters at both ends. Transport Pro answers a cancelled
    load's ``payment_information`` with HTTP 400 or an empty array; read as a generic client
    error, that surfaced as ``2476946=ERROR(... HTTP 400)`` inside a line reading "sender not
    authorized for any load" — blaming the sender for a load that no longer exists and
    looking, to whoever read it, like a Transport Pro outage.

    A cancelled load cannot be authorized either, because the authorization context is
    derived from the same payload. So this is a THIRD outcome beside allow and deny, and
    callers must not fold it into either.
    """
