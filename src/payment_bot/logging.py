"""Structured logging and the tool-call audit sink.

Two concerns live here:

1. **App logging** — JSON lines to stderr, so CloudWatch (or any log aggregator) can
   parse them without a regex. :func:`configure_logging` is idempotent.

2. **Audit trail** — PRD §8.1 requires that *every* tool call and its result be
   logged "for audit and grounding checks". :class:`AuditSink` is the seam:
   :class:`InMemoryAuditSink` serves tests and the local runners; production would add
   a DynamoDB/S3-backed sink (§8.1.1) behind the same protocol.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_CONFIGURED = False

# Standard LogRecord attributes we must not treat as user-supplied "extra" fields.
_RESERVED_LOGRECORD_KEYS = frozenset(
    logging.makeLogRecord({}).__dict__.keys() | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """Render each log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Merge structured `extra=...` fields passed at the call site.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOGRECORD_KEYS and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger. Safe to call more than once."""

    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(level.upper())
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger (namespaced under ``payment_bot``)."""

    return logging.getLogger(f"payment_bot.{name}" if name != "payment_bot" else name)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One immutable audit entry for a single tool invocation."""

    correlation_id: str
    tool_name: str
    request: dict[str, Any]
    response: dict[str, Any]
    ok: bool
    duration_ms: float


@runtime_checkable
class AuditSink(Protocol):
    """Where tool-call audit records go. Implementations must be side-effect safe."""

    def record(self, entry: AuditRecord) -> None: ...


@dataclass(slots=True)
class InMemoryAuditSink:
    """Collects audit records in a list. Used by tests and the local demo."""

    entries: list[AuditRecord] = field(default_factory=list)

    def record(self, entry: AuditRecord) -> None:
        self.entries.append(entry)

    def for_correlation(self, correlation_id: str) -> list[AuditRecord]:
        """Return all entries for one email/run, in call order."""

        return [e for e in self.entries if e.correlation_id == correlation_id]

