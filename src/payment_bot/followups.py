"""What the bot did with each follow-up, per thread — ``state/followups/<thread>.json``.

A follow-up is a carrier writing again in a thread someone on our side already answered
(``Settings.followup_replies``). Deciding what to do with one needs memory a single email
does not carry: has this thread already had an automated status answer, and has it already
been handed to a person? Without it, live on an RTS Financial thread about load 2493116, the
bot restated the same "pending, no pay date" three times in eighteen hours to a factor who
had stopped asking for the status and started asking us to expedite.

One small JSON object per thread, in the config bucket under the worker's existing
``state/*`` grant — no new IAM. It doubles as the audit trail of every automated follow-up:
what was asked, what the bot did, who was copied, and the reply text itself.

Same tolerance contract as the other ledgers: an unreadable record reads as empty and a
failed write is logged, never raised. The cost of losing it is one extra status answer
before the cap applies again, not a wrong send — every follow-up draft is still approved by
a person.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from payment_bot.approvals import _is_missing_key
from payment_bot.logging import get_logger

_log = get_logger("followups")

S3_PREFIX = "state/followups"

#: The re-read of the records that answered the follow-up.
ACTION_STATUS_UPDATE = "status_update"
#: A code-authored reply handing the thread to the colleagues, who are copied on it.
ACTION_HANDOFF = "handoff"
#: A chase after a handoff: no email, a card for the people who have it.
ACTION_NOTICE = "notice"

#: Automated status answers one thread may receive. One, because the second is the repeat:
#: if the first did not satisfy the carrier, re-reading unchanged records will not either,
#: and a person has to look.
MAX_STATUS_UPDATES = 1

#: Outcomes that put a draft in front of a reviewer. A blocked or escalated attempt drafted
#: nothing the carrier could have received, so it does not count toward the cap.
_DRAFTED = frozenset({"awaiting_review", "sent", "rejected"})

#: Entries kept per thread. A thread past this many automated follow-ups is long since a
#: person's; the oldest go first.
_MAX_ENTRIES = 50


@dataclass(frozen=True, slots=True)
class FollowUpRecord:
    """One follow-up and what the bot did with it."""

    message_id: str
    ask: str
    action: str
    outcome: str
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    after_reply_from: str = ""
    evidence: tuple[str, ...] = ()
    loads: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    #: The drafted reply, when there was one — the "proper track" of what went out.
    reply: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        record = asdict(self)
        for key in ("evidence", "loads", "cc"):
            record[key] = list(record[key])
        return record

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FollowUpRecord:
        return cls(
            message_id=str(data["message_id"]),
            ask=str(data.get("ask") or ""),
            action=str(data.get("action") or ""),
            outcome=str(data.get("outcome") or ""),
            at=str(data.get("at") or ""),
            after_reply_from=str(data.get("after_reply_from") or ""),
            evidence=tuple(str(v) for v in data.get("evidence") or ()),
            loads=tuple(str(v) for v in data.get("loads") or ()),
            cc=tuple(str(v) for v in data.get("cc") or ()),
            reply=str(data.get("reply") or ""),
            detail=str(data.get("detail") or ""),
        )

    @property
    def drafted(self) -> bool:
        return self.outcome in _DRAFTED


def status_updates(history: list[FollowUpRecord]) -> int:
    """Automated status answers this thread has already been given."""

    return sum(1 for r in history if r.action == ACTION_STATUS_UPDATE and r.drafted)


def handed_off(history: list[FollowUpRecord]) -> bool:
    """True once a handoff reply has been drafted for this thread."""

    return any(r.action == ACTION_HANDOFF and r.drafted for r in history)


@runtime_checkable
class FollowUpStore(Protocol):
    """Per-thread follow-up history."""

    def history(self, thread_id: str) -> list[FollowUpRecord]:
        """Oldest first. Empty for a thread with no record, or one that cannot be read."""
        ...

    def record(self, thread_id: str, entry: FollowUpRecord) -> None:
        """Append ``entry``, replacing any earlier entry for the same message.

        Replacing is what makes re-processing safe: an escalation re-runs every poll until
        its retry budget is spent, and each run must update its one entry, not add another.
        """
        ...


def _merge(entries: list[FollowUpRecord], entry: FollowUpRecord) -> list[FollowUpRecord]:
    kept = [e for e in entries if e.message_id != entry.message_id]
    kept.append(entry)
    return kept[-_MAX_ENTRIES:]


class InMemoryFollowUpStore:
    """For tests and local runs, where the history need only last one run."""

    def __init__(self) -> None:
        self._threads: dict[str, list[FollowUpRecord]] = {}

    def history(self, thread_id: str) -> list[FollowUpRecord]:
        return list(self._threads.get(thread_id, []))

    def record(self, thread_id: str, entry: FollowUpRecord) -> None:
        self._threads[thread_id] = _merge(self._threads.get(thread_id, []), entry)


_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")


def _key(thread_id: str) -> str:
    """Gmail thread ids are hex and go in as-is; anything else is hashed into a safe key."""

    name = (
        thread_id
        if _SAFE_ID_RE.fullmatch(thread_id)
        else hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]
    )
    return f"{S3_PREFIX}/{name}.json"


class S3FollowUpStore:
    """The deployed store: one JSON object per thread in the config bucket.

    boto3 is imported per call, like :class:`~payment_bot.approvals.S3ApprovalStore`, so the
    module stays importable without it.
    """

    def __init__(self, bucket: str, *, client: Any | None = None) -> None:
        if not bucket:
            raise ValueError("S3FollowUpStore needs a bucket")
        self._bucket = bucket
        self._client = client

    def _s3(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - Lambda always has it
                raise RuntimeError("boto3 is required for S3FollowUpStore") from exc
            self._client = boto3.client("s3")
        return self._client

    def history(self, thread_id: str) -> list[FollowUpRecord]:
        key = _key(thread_id)
        try:
            raw = self._s3().get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception as exc:
            if not _is_missing_key(exc):
                _log.warning("followup_record_unreadable", extra={"key": key, "error": str(exc)})
            return []
        try:
            data = json.loads(raw.decode("utf-8"))
            return [FollowUpRecord.from_dict(e) for e in data.get("entries") or []]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            _log.warning("followup_record_unreadable", extra={"key": key, "error": str(exc)})
            return []

    def record(self, thread_id: str, entry: FollowUpRecord) -> None:
        entries = _merge(self.history(thread_id), entry)
        body = {"thread_id": thread_id, "entries": [e.to_dict() for e in entries]}
        self._s3().put_object(
            Bucket=self._bucket,
            Key=_key(thread_id),
            Body=json.dumps(body, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )
