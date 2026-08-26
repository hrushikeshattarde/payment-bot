"""Pending chat approvals — the state behind CHAT_APPROVAL_PLAN.md §3/§5.

When ``approval_mode=chat``, a gate-passing reply is not saved to the reading mailbox's
Drafts. It becomes a **pending entry** here plus a card in the chat space, and the reply
is later sent by the callback Lambda as whoever clicked Approve. That splits one piece of
state across two writers, so the layout is three object families with exactly one writer
each — the whole point is that the worker and the callback never update the same object:

* ``state/approvals/pending/<id>.json`` — written ONCE by the worker at post time. Holds
  everything a send needs (body, recipients, threading headers), so the callback never
  trusts content from the chat payload — buttons carry only the entry id.
* ``state/approvals/claims/<id>.json`` — conditionally written by the callback
  (``If-None-Match: *``). Exactly one click wins; platform retries and double-clicks
  lose the condition instead of double-sending.
* ``state/approvals/results/<id>.json`` — written by the callback after acting
  (sent / moved / rejected), or by the worker's sweep for ``expired``.

A pending entry with no result is **live**: the worker skips its message *and its
thread* on every re-scan — the replacement for the draft-in-thread duplicate guard,
which only worked while reading and drafting shared a mailbox
(REVIEWER_DRAFTS_DESIGN.md §4.1 explains the breakage; the guard itself stays as a
first check). A lost or unreadable entry degrades exactly as the block ledger does:
worst case one duplicate card, never a wrong send — the claim still gates that.

Entry ids are derived from the inbound message id, so the card the pipeline posts and
the entry the runner writes moments later agree without either knowing about the other.

:class:`ChatPostLedger` rides along here because it answers the same question for the
cards that have no pending entry: escalations and gate blocks re-run every 20 minutes
by design (the mailbox is the state), and without a posted-record each run would repost
the same card. One small JSON object, same contract as :class:`~payment_bot.block_ledger.BlockLedger`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from payment_bot.logging import get_logger

_log = get_logger("approvals")

#: Terminal entries older than this are deleted by the sweep. Comfortably past the
#: ``newer_than:2d`` intake window, matching the block ledger's reasoning.
PRUNE_DAYS = 7

#: Everything the store keeps lives under this prefix — beside the block ledger, inside
#: the one ``state/*`` grant the worker role already has. No new IAM.
S3_PREFIX = "state/approvals"

#: Where the standing tracker card's resource name lives. Beside the three entry families
#: rather than among them: it is one value for the space, not per approval, and the sweep's
#: prune walks the families by prefix and must never see it.
_TRACKER_KEY = f"{S3_PREFIX}/tracker.json"

#: Cached per-row label states, one object for the whole board. Beside the entry families
#: rather than among them, for the same reason as the tracker key: the prune walks those
#: families by prefix and must not see this.
_STATES_KEY = f"{S3_PREFIX}/row_states.json"


def _now() -> datetime:
    return datetime.now(UTC)


def entry_id_for(message_id: str) -> str:
    """Deterministic entry id for an inbound message id.

    Message ids are angle-bracketed addresses with characters S3 keys and Chat button
    parameters are better off without; a hash is stable, safe in both, and lets the
    card (posted by the pipeline) and the entry (written by the runner) name the same
    thing without talking to each other.
    """

    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class PendingApproval:
    """One reply waiting for a human click. The callback sends exactly this, no more."""

    entry_id: str
    message_id: str
    #: Reading-mailbox thread id — what the worker's skip check matches follow-ups on.
    thread_id: str
    to: str
    cc: tuple[str, ...]
    reply_to: str
    subject: str
    body: str
    load_ids: tuple[str, ...]
    #: Chat message resource name (``spaces/…/messages/…``) — how the sweep updates the
    #: card when the entry expires. Blank when the post's name was not captured.
    chat_message: str = ""
    created_at: str = field(default_factory=lambda: _now().isoformat())

    def to_json(self) -> str:
        record = asdict(self)
        record["cc"] = list(self.cc)
        record["load_ids"] = list(self.load_ids)
        return json.dumps(record, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> PendingApproval:
        data = json.loads(raw)
        return cls(
            entry_id=str(data["entry_id"]),
            message_id=str(data["message_id"]),
            thread_id=str(data.get("thread_id") or ""),
            to=str(data["to"]),
            cc=tuple(str(v) for v in data.get("cc") or ()),
            reply_to=str(data.get("reply_to") or ""),
            subject=str(data.get("subject") or ""),
            body=str(data["body"]),
            load_ids=tuple(str(v) for v in data.get("load_ids") or ()),
            chat_message=str(data.get("chat_message") or ""),
            created_at=str(data.get("created_at") or _now().isoformat()),
        )


@runtime_checkable
class ApprovalStore(Protocol):
    """What the worker and the callback need from pending-approval state."""

    def put_pending(self, entry: PendingApproval) -> None:
        """Persist a new pending entry. Write-once; the worker never updates it."""

    def pending(self, entry_id: str) -> PendingApproval | None:
        """One entry by id — what a click resolves — or ``None`` when it never existed
        or was pruned."""

    def live_entries(self) -> list[PendingApproval]:
        """Pending entries with no result yet — the ones the worker must not re-draft."""

    def result(self, entry_id: str) -> dict[str, Any] | None:
        """The recorded outcome for an entry, or ``None`` while it is live."""

    def put_result(self, entry_id: str, result: dict[str, Any]) -> None:
        """Record an entry's terminal outcome (sent / moved / rejected / expired)."""

    def claim(self, entry_id: str, payload: dict[str, Any]) -> bool:
        """Atomically claim an entry for one action. False when someone already has."""

    def release_claim(self, entry_id: str) -> None:
        """Undo a claim whose action failed, so a later click can retry."""

    def row_states(self) -> dict[str, dict[str, str]]:
        """Cached ``{entry_id: {"state": …, "at": …}}`` for the board's labels."""

    def set_row_states(self, states: dict[str, dict[str, str]]) -> None:
        """Replace the cached label states.

        Cached because the two things that need them have opposite budgets. Establishing a
        state costs a Gmail call per row; the worker has fifteen minutes and can afford it,
        while a card click has to answer Chat immediately and cannot. So the worker writes
        what it learns and the click path reads it — a label up to one refresh stale, which
        is the right trade for a label.
        """

    def tracker(self) -> tuple[str, int]:
        """``(chat message name, consecutive edit failures)``. ``("", 0)`` when unposted.

        The failure count is what stops a broken edit from becoming a new card every
        fifteen minutes: the handler retries the same message while the count is low and
        only gives up on it once. See ``lambda_handler._refresh_queue_tracker``.
        """

    def set_tracker(self, name: str, misses: int = 0) -> None:
        """Remember the queue card and its recent edit failures.

        One record for the whole space, not one per entry: the tracker is a single message
        rewritten in place. Kept in the store rather than in the chat client because the
        client is rebuilt every invocation and this has to outlive it.
        """


class InMemoryApprovalStore:
    """Dict-backed store for tests and local experiments. Same contract, no S3."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, dict[str, Any]] = {}
        self._tracker: tuple[str, int] = ("", 0)
        self._row_states: dict[str, dict[str, str]] = {}

    def put_pending(self, entry: PendingApproval) -> None:
        self._pending[entry.entry_id] = entry

    def pending(self, entry_id: str) -> PendingApproval | None:
        return self._pending.get(entry_id)

    def live_entries(self) -> list[PendingApproval]:
        return [e for eid, e in self._pending.items() if eid not in self.results]

    def result(self, entry_id: str) -> dict[str, Any] | None:
        return self.results.get(entry_id)

    def put_result(self, entry_id: str, result: dict[str, Any]) -> None:
        self.results[entry_id] = result

    def claim(self, entry_id: str, payload: dict[str, Any]) -> bool:
        if entry_id in self.claims:
            return False
        self.claims[entry_id] = payload
        return True

    def release_claim(self, entry_id: str) -> None:
        self.claims.pop(entry_id, None)

    def row_states(self) -> dict[str, dict[str, str]]:
        return dict(self._row_states)

    def set_row_states(self, states: dict[str, dict[str, str]]) -> None:
        self._row_states = dict(states)

    def tracker(self) -> tuple[str, int]:
        return self._tracker

    def set_tracker(self, name: str, misses: int = 0) -> None:
        self._tracker = (name, misses)


class S3ApprovalStore:
    """The deployed store: three key families under ``state/approvals/`` (module doc).

    boto3 is imported per call site, like the Lambda handler's other S3 uses — the
    module stays importable in environments without it (local tests use the in-memory
    store). Read failures on individual entries degrade to "skip that entry" rather
    than failing the run; the entry is bookkeeping, and the claim protocol still
    prevents any wrong send.
    """

    def __init__(self, bucket: str, *, client: Any | None = None) -> None:
        if not bucket:
            raise ValueError("S3ApprovalStore needs a bucket")
        self._bucket = bucket
        self._client = client

    # -- keys ------------------------------------------------------------------
    @staticmethod
    def _pending_key(entry_id: str) -> str:
        return f"{S3_PREFIX}/pending/{entry_id}.json"

    @staticmethod
    def _result_key(entry_id: str) -> str:
        return f"{S3_PREFIX}/results/{entry_id}.json"

    @staticmethod
    def _claim_key(entry_id: str) -> str:
        return f"{S3_PREFIX}/claims/{entry_id}.json"

    def _s3(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - Lambda always has it
                raise RuntimeError("boto3 is required for S3ApprovalStore") from exc
            self._client = boto3.client("s3")
        return self._client

    # -- ApprovalStore ---------------------------------------------------------
    def put_pending(self, entry: PendingApproval) -> None:
        self._s3().put_object(
            Bucket=self._bucket,
            Key=self._pending_key(entry.entry_id),
            Body=entry.to_json().encode("utf-8"),
            ContentType="application/json",
        )

    def pending(self, entry_id: str) -> PendingApproval | None:
        raw = self._get(self._pending_key(entry_id))
        if raw is None:
            return None
        try:
            return PendingApproval.from_json(raw)
        except (ValueError, KeyError, TypeError) as exc:
            _log.warning(
                "approval_entry_unreadable", extra={"entry_id": entry_id, "error": str(exc)}
            )
            return None

    def live_entries(self) -> list[PendingApproval]:
        pending_ids = self._list_ids("pending")
        if not pending_ids:
            return []
        result_ids = set(self._list_ids("results"))
        entries: list[PendingApproval] = []
        for entry_id in pending_ids:
            if entry_id in result_ids:
                continue
            raw = self._get(self._pending_key(entry_id))
            if raw is None:
                continue
            try:
                entries.append(PendingApproval.from_json(raw))
            except (ValueError, KeyError, TypeError) as exc:
                _log.warning(
                    "approval_entry_unreadable", extra={"entry_id": entry_id, "error": str(exc)}
                )
        return entries

    def result(self, entry_id: str) -> dict[str, Any] | None:
        raw = self._get(self._result_key(entry_id))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except ValueError:
            return None

    def put_result(self, entry_id: str, result: dict[str, Any]) -> None:
        self._s3().put_object(
            Bucket=self._bucket,
            Key=self._result_key(entry_id),
            Body=json.dumps(result, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )

    def claim(self, entry_id: str, payload: dict[str, Any]) -> bool:
        client = self._s3()
        try:
            client.put_object(
                Bucket=self._bucket,
                Key=self._claim_key(entry_id),
                Body=json.dumps(payload, sort_keys=True).encode("utf-8"),
                ContentType="application/json",
                # S3 conditional write: exactly one caller creates the object. This is
                # the entire double-send defence, so it must be the atomic kind, not a
                # read-then-write.
                IfNoneMatch="*",
            )
            return True
        except Exception as exc:
            if _is_precondition_failure(exc):
                return False
            raise

    def row_states(self) -> dict[str, dict[str, str]]:
        """Cached label states. An unreadable cache reads as empty — every row UNANSWERED,
        which is the safe direction: work still owed, never work already done."""

        raw = self._get(_STATES_KEY)
        if not raw:
            return {}
        try:
            record = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(record, dict):
            return {}
        return {
            str(k): {"state": str(v.get("state") or "open"), "at": str(v.get("at") or "")}
            for k, v in record.items()
            if isinstance(v, dict)
        }

    def set_row_states(self, states: dict[str, dict[str, str]]) -> None:
        self._s3().put_object(
            Bucket=self._bucket,
            Key=_STATES_KEY,
            Body=json.dumps(states, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
        )

    def tracker(self) -> tuple[str, int]:
        """The queue card's name and its consecutive edit failures.

        A read failure returns ``("", 0)``, which makes the next refresh post a fresh card.
        That is the right way to fail: a duplicate tracker is untidy, an unreadable one is a
        queue nobody can see.
        """

        raw = self._get(_TRACKER_KEY)
        if not raw:
            return "", 0
        try:
            record = json.loads(raw)
            return str(record.get("chat_message") or ""), int(record.get("misses") or 0)
        except (ValueError, AttributeError, TypeError):
            return "", 0

    def set_tracker(self, name: str, misses: int = 0) -> None:
        self._s3().put_object(
            Bucket=self._bucket,
            Key=_TRACKER_KEY,
            Body=json.dumps({"chat_message": name, "misses": misses}).encode("utf-8"),
            ContentType="application/json",
        )

    def release_claim(self, entry_id: str) -> None:
        try:
            self._s3().delete_object(Bucket=self._bucket, Key=self._claim_key(entry_id))
        except Exception as exc:
            # A stuck claim on a failed send blocks retries until it is removed, so it
            # is worth a warning — but never worth failing the response over.
            _log.warning(
                "approval_claim_release_failed", extra={"entry_id": entry_id, "error": str(exc)}
            )

    # -- sweep -----------------------------------------------------------------
    def sweep(
        self, *, expiry_days: int, now: datetime | None = None
    ) -> list[PendingApproval]:
        """Expire stale live entries; prune terminal ones past :data:`PRUNE_DAYS`.

        Returns the entries expired *this* sweep so the caller can update their cards.
        Runs inside the existing worker invocation — this is deliberately not its own
        schedule (CHAT_APPROVAL_PLAN.md §8).
        """

        moment = now or _now()
        expired: list[PendingApproval] = []
        cutoff = moment - timedelta(days=expiry_days)
        for entry in self.live_entries():
            try:
                created = datetime.fromisoformat(entry.created_at)
            except ValueError:
                created = moment
            if created <= cutoff:
                self.put_result(
                    entry.entry_id,
                    {"status": "expired", "at": moment.isoformat(), "by": "sweep"},
                )
                expired.append(entry)

        prune_cutoff = moment - timedelta(days=PRUNE_DAYS)
        for entry_id in self._list_ids("results"):
            result = self.result(entry_id) or {}
            try:
                at = datetime.fromisoformat(str(result.get("at")))
            except (TypeError, ValueError):
                continue
            if at <= prune_cutoff:
                for key in (
                    self._pending_key(entry_id),
                    self._claim_key(entry_id),
                    self._result_key(entry_id),
                ):
                    with contextlib.suppress(Exception):  # pragma: no cover - best-effort
                        self._s3().delete_object(Bucket=self._bucket, Key=key)
        return expired

    # -- S3 plumbing -------------------------------------------------------------
    def _list_ids(self, family: str) -> list[str]:
        prefix = f"{S3_PREFIX}/{family}/"
        ids: list[str] = []
        token: str | None = None
        client = self._s3()
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "")
                if key.endswith(".json"):
                    ids.append(key[len(prefix) : -len(".json")])
            if not page.get("IsTruncated"):
                return ids
            token = page.get("NextContinuationToken")

    def _get(self, key: str) -> str | None:
        client = self._s3()
        try:
            raw: str = (
                client.get_object(Bucket=self._bucket, Key=key)["Body"].read().decode("utf-8")
            )
            return raw
        except Exception as exc:
            if _is_missing_key(exc):
                return None
            _log.warning("approval_object_unreadable", extra={"key": key, "error": str(exc)})
            return None


def _is_precondition_failure(exc: Exception) -> bool:
    """True for S3's 412 on a lost conditional write, without importing botocore."""

    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
        if code in {"PreconditionFailed", "412"}:
            return True
    return "PreconditionFailed" in str(exc) or "412" in str(exc)


def _is_missing_key(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
        return code in {"NoSuchKey", "404"}
    return "NoSuchKey" in str(exc)


@dataclass
class ChatPostLedger:
    """Which cards have already been posted, per ``kind:message_id``.

    Exists for the cards with no pending entry to dedup on: escalations and gate
    blocks are re-processed every run by design, and each would otherwise repost.
    Same tolerance contract as the block ledger — unreadable resets to empty, and the
    cost of losing it is a duplicate card, not a wrong send.
    """

    entries: dict[str, str] = field(default_factory=dict)
    dirty: bool = False

    @classmethod
    def from_json(cls, raw: str | None, *, now: datetime | None = None) -> ChatPostLedger:
        moment = now or _now()
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("ledger root is not an object")
        except (ValueError, TypeError) as exc:
            _log.warning("chat_post_ledger_unreadable_reset", extra={"error": str(exc)})
            return cls(dirty=True)

        cutoff = moment - timedelta(days=PRUNE_DAYS)
        kept: dict[str, str] = {}
        dropped = 0
        for key, stamp in data.items():
            try:
                at = datetime.fromisoformat(str(stamp))
            except (TypeError, ValueError):
                dropped += 1
                continue
            if at >= cutoff:
                kept[str(key)] = str(stamp)
            else:
                dropped += 1
        return cls(entries=kept, dirty=dropped > 0)

    def to_json(self) -> str:
        return json.dumps(self.entries, sort_keys=True)

    @staticmethod
    def key(kind: str, message_id: str) -> str:
        return f"{kind}:{message_id}"

    def posted(self, kind: str, message_id: str) -> bool:
        return self.key(kind, message_id) in self.entries

    def record(self, kind: str, message_id: str, *, now: datetime | None = None) -> None:
        self.entries[self.key(kind, message_id)] = (now or _now()).isoformat()
        self.dirty = True
