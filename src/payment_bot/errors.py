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
    """An external system (TP / QBO / Gmail / Slack / Bedrock) returned an error."""


class GateBlockedError(PaymentBotError):
    """The deterministic pre-send gate refused to allow a send (PRD §5).

    This is a control-flow signal, not a bug: the orchestrator catches it and routes
    to Slack escalation instead of sending.
    """

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons) if reasons else "pre-send gate blocked")
