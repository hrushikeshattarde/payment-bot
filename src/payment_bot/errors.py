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
    """An external system (Transport Pro / Gmail / Slack / an LLM) returned an error."""
