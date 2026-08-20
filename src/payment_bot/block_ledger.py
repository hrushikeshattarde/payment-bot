"""Retry budgets for messages that produced no reviewable reply — the one deliberate
exception to statelessness.

The pipeline keeps no memory across runs on purpose: the mailbox is the state, so a thread
with no draft and no reply is simply re-processed. Two outcomes leave a thread in exactly
that shape, and both therefore re-run on every poll until a human intervenes:

* **Gate-blocked.** The full agent loop runs again and the gate refuses again. Measured live
  (G.H. Factor, load 302618, 2026-08-18/19): one stuck thread re-drafted every 30 minutes for
  a night at roughly a dime per attempt.
* **Escalated.** This module used to claim escalations were free because "they stop before
  any model call". That has not been true since the LLM id filter landed: `_filter_ids` runs
  at intake, ahead of every escalation check, so a sensitive-change or authorization refusal
  now pays a model call each time it is re-scanned. Worse, one escalation reason — `agent
  produced no draft` — is raised *after* the whole agent loop has run, which makes it the
  single most expensive repeat in the system, 12 turns for one load and up to 50 for five.
  Escalations are also the majority outcome: 23 of 37 processed emails in the 2026-08-11 log.

Neither has an upper bound of its own. `is:unread ... newer_than:2d` against a 30-minute
schedule keeps a message fetchable for about 96 runs, so an unbudgeted stuck thread is billed
~96 times before it ages out of the query.

This ledger counts both kinds per **message id** and, once a message has spent ``limit``
attempts of a kind, tells the runner to skip it before any model call. Message id rather than
thread id on purpose: a sender's follow-up is a *new* message and earns a fresh budget —
new inbound deserves new attempts; a re-scan of the same unread mail does not.

**What this trades away.** An escalation caused by a transient failure — Transport Pro
returning 500s, a stale CargoTel cookie — burns attempts on a problem that would have fixed
itself. Once the budget is spent, fixing the cause does *not* bring the mail back: it sits
unread with nothing retrying it, exactly as an exhausted gate block does. That is why the
escalation budget defaults higher than the block budget, and why spending it logs
``escalation_retry_limit_reached`` at WARNING — once, on the run that spends it, which is the
line the EscalationRetriesExhausted metric reads. A spike there right after an outage is the
signal that some threads need re-sending by hand. Do not point that metric at the
``escalation_retries_exhausted`` line beside it: that one fires on every later skip, so it
measures a skip rate rather than a count of stranded threads.

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

#: Gate blocks. Stored under the bare message id, unprefixed, because that is the shape the
#: live ledger in S3 already has — prefixing them would make every existing entry unreadable
#: at the next load, silently resetting every counter in flight.
KIND_BLOCK = "block"

#: Escalations. Prefixed, so the two budgets are independent: a message that escalated twice
#: and is then answered but gate-blocked has spent none of its block budget.
#:
#: The prefix cannot collide with a key, and the reason is worth stating correctly because it
#: is easy to check the wrong identifier: these are keyed on ``InboundEmail.message_id``, the
#: RFC822 ``Message-ID`` header — ``<PH9PR05MB4239...@...outlook.com>`` — not the hex id the
#: Gmail API assigns. A Message-ID is an addr-spec, so a bare colon in the local part is only
#: legal inside a quoted string, and no mail client emits one beginning ``escalation:``.
KIND_ESCALATION = "escalation"


#: Escalations raised AFTER the agent loop ran — in practice the one reason
#: ``agent produced no draft``. Counted apart from :data:`KIND_ESCALATION` because the two
#: cost wildly different amounts to repeat and a budget in attempts cannot tell them apart:
#: a pre-model escalation repeats for one id-filter call, this one repeats the whole loop.
#: Three attempts is cheap insurance against a transient outage on the first and the system's
#: most expensive repeat on the second.
KIND_AGENT_ESCALATION = "agent_escalation"


def _key(kind: str, message_id: str) -> str:
    return message_id if kind == KIND_BLOCK else f"{kind}:{message_id}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class BlockLedger:
    """Attempt counts per (kind, message id), with just enough context to audit them.

    ``kind`` defaults to :data:`KIND_BLOCK` throughout so the gate-block call sites read
    exactly as they did when blocks were the only budget.

    The stored entries keep their original ``first_blocked`` / ``last_blocked`` field names
    for both kinds. They read oddly for an escalation, and they stay: `from_json` prunes on
    ``last_blocked``, so renaming them would drop every entry already in the live ledger.
    """

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

    def blocks(self, message_id: str, *, kind: str = KIND_BLOCK) -> int:
        entry = self.entries.get(_key(kind, message_id))
        return int(entry["count"]) if entry else 0

    def record(
        self, message_id: str, reason: str, *, kind: str = KIND_BLOCK, now: datetime | None = None
    ) -> int:
        """Count one attempt of ``kind`` against ``message_id`` and return the new total."""

        moment = (now or _now()).isoformat()
        entry = self.entries.setdefault(
            _key(kind, message_id), {"count": 0, "first_blocked": moment, "reason": ""}
        )
        entry["count"] = int(entry["count"]) + 1
        entry["last_blocked"] = moment
        # Keep only the latest reason — it is audit context, not history; the log has that.
        entry["reason"] = reason[:500]
        self.dirty = True
        return int(entry["count"])

    def exhausted(self, message_id: str, limit: int, *, kind: str = KIND_BLOCK) -> bool:
        """True when ``message_id`` has spent its budget of ``kind``. ``limit <= 0`` disables."""

        return limit > 0 and self.blocks(message_id, kind=kind) >= limit
