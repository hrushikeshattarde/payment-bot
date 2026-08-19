"""Retry budget for gate-blocked messages — the one deliberate exception to statelessness.

The pipeline keeps no memory across runs on purpose: the mailbox is the state, so a thread
with no draft and no reply is simply re-processed. For escalations that is free — they stop
before any model call. For a gate-blocked draft it is the most expensive failure mode the
system has: the full agent loop runs again, the gate refuses again, and nothing anywhere
records that this already happened. Measured live (G.H. Factor, load 302618, 2026-08-18/19):
one stuck thread re-drafted every 30 minutes for a night at roughly a dime per attempt.

This ledger records gate blocks per **message id** and, once a message has been blocked
``limit`` times, tells the runner to skip its agent loop entirely. Message id rather than
thread id on purpose: a sender's follow-up is a *new* message and earns a fresh budget —
new inbound deserves new attempts; a re-scan of the same unread mail does not.

The store is one small JSON object injected as text, so the Lambda backs it with S3 and
tests with a string. Entries expire after :data:`PRUNE_DAYS`: the intake window
(``newer_than:2d``) makes older mail unreachable anyway, so stale entries are dead weight
that would otherwise grow forever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from payment_bot.logging import get_logger

_log = get_logger("block_ledger")

#: Entries older than this are dropped at load. Comfortably past the intake window, so an
#: email cannot outlive its ledger entry while still being fetchable.
PRUNE_DAYS = 7


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class BlockLedger:
    """Counts of gate blocks per message id, with just enough context to audit them."""

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: True once anything changed, so callers can skip a pointless write-back.
    dirty: bool = False

    @classmethod
    def from_json(cls, raw: str | None, *, now: datetime | None = None) -> BlockLedger:
        """Parse a stored ledger, dropping expired entries and tolerating a corrupt one.

        A ledger that fails to parse is replaced with an empty one rather than raised:
        the cost of losing it is a couple of duplicate retries, while failing the run
        would stop every draft over a bookkeeping file.
        """

        moment = now or _now()
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("ledger root is not an object")
        except (ValueError, TypeError) as exc:
            _log.warning("block_ledger_unreadable_reset", extra={"error": str(exc)})
            return cls(dirty=True)

        cutoff = moment - timedelta(days=PRUNE_DAYS)
        kept: dict[str, dict[str, Any]] = {}
        dropped = 0
        for message_id, entry in data.items():
            try:
                last = datetime.fromisoformat(str(entry["last_blocked"]))
            except (KeyError, TypeError, ValueError):
                dropped += 1
                continue
            if last >= cutoff:
                kept[message_id] = entry
            else:
                dropped += 1
        return cls(entries=kept, dirty=dropped > 0)

    def to_json(self) -> str:
        return json.dumps(self.entries, sort_keys=True)

    def blocks(self, message_id: str) -> int:
        entry = self.entries.get(message_id)
        return int(entry["count"]) if entry else 0

    def record(self, message_id: str, reason: str, *, now: datetime | None = None) -> int:
        """Count one gate block against ``message_id`` and return the new total."""

        moment = (now or _now()).isoformat()
        entry = self.entries.setdefault(
            message_id, {"count": 0, "first_blocked": moment, "reason": ""}
        )
        entry["count"] = int(entry["count"]) + 1
        entry["last_blocked"] = moment
        # Keep only the latest reason — it is audit context, not history; the log has that.
        entry["reason"] = reason[:500]
        self.dirty = True
        return int(entry["count"])

    def exhausted(self, message_id: str, limit: int) -> bool:
        """True when ``message_id`` has spent its retry budget. ``limit <= 0`` disables."""

        return limit > 0 and self.blocks(message_id) >= limit
