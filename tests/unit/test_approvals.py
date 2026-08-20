"""Unit tests for the pending-approval store and the chat-post ledger.

The S3 store is exercised against a small fake S3 client because its two contracts are
behavioural, not cosmetic: the conditional claim must admit exactly one caller (the
whole double-send defence), and the sweep must expire live entries without touching
ones a human already resolved.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any

from payment_bot.approvals import (
    ChatPostLedger,
    InMemoryApprovalStore,
    PendingApproval,
    S3ApprovalStore,
    entry_id_for,
)


def _entry(entry_id: str = "", message_id: str = "<m1@x>", **overrides: Any) -> PendingApproval:
    values: dict[str, Any] = {
        "entry_id": entry_id or entry_id_for(message_id),
        "message_id": message_id,
        "thread_id": "t-1",
        "to": "billing@carrier.test",
        "cc": ("paystatus@circledelivers.com",),
        "reply_to": "paystatus@circledelivers.com",
        "subject": "Re: Payment status for load 2462934",
        "body": "Load 2462934 is BILLED.",
        "load_ids": ("2462934",),
        "chat_message": "spaces/S/messages/M1",
    }
    values.update(overrides)
    return PendingApproval(**values)


# --- entry ids ----------------------------------------------------------------
def test_entry_id_is_deterministic_and_key_safe() -> None:
    a = entry_id_for("<abc123@mail.example.com>")
    assert a == entry_id_for("<abc123@mail.example.com>")
    assert a != entry_id_for("<other@mail.example.com>")
    # Safe in S3 keys and Chat button parameters: hex only.
    assert len(a) == 32
    assert all(c in "0123456789abcdef" for c in a)


def test_pending_approval_roundtrips_through_json() -> None:
    entry = _entry()
    again = PendingApproval.from_json(entry.to_json())
    assert again == entry
    assert isinstance(again.cc, tuple)
    assert isinstance(again.load_ids, tuple)


# --- in-memory store ------------------------------------------------------------
def test_live_entries_exclude_resolved_ones() -> None:
    store = InMemoryApprovalStore()
    store.put_pending(_entry(message_id="<a@x>"))
    store.put_pending(_entry(message_id="<b@x>"))
    store.put_result(entry_id_for("<a@x>"), {"status": "sent", "by": "p@x", "at": "now"})

    live = store.live_entries()
    assert [e.message_id for e in live] == ["<b@x>"]


def test_claim_admits_exactly_one() -> None:
    store = InMemoryApprovalStore()
    assert store.claim("e1", {"by": "a@x"}) is True
    assert store.claim("e1", {"by": "b@x"}) is False
    store.release_claim("e1")
    assert store.claim("e1", {"by": "b@x"}) is True


# --- S3 store -------------------------------------------------------------------
class FakeS3:
    """Just enough S3: objects in a dict, IfNoneMatch honoured, paging exercised."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> dict[str, Any]:  # noqa: N803 - boto3 kwargs
        if kwargs.get("IfNoneMatch") == "*" and Key in self.objects:
            exc = Exception("precondition")
            exc.response = {"Error": {"Code": "PreconditionFailed"}}  # type: ignore[attr-defined]
            raise exc
        self.objects[Key] = Body
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3 kwargs
        if Key not in self.objects:
            exc = Exception("missing")
            exc.response = {"Error": {"Code": "NoSuchKey"}}  # type: ignore[attr-defined]
            raise exc
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3 kwargs
        self.objects.pop(Key, None)
        return {}

    def list_objects_v2(self, *, Bucket: str, Prefix: str, **kwargs: Any) -> dict[str, Any]:  # noqa: N803 - boto3 kwargs
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


def test_s3_store_roundtrip_and_live_filtering() -> None:
    store = S3ApprovalStore("bucket", client=FakeS3())
    store.put_pending(_entry(message_id="<a@x>"))
    store.put_pending(_entry(message_id="<b@x>"))
    store.put_result(
        entry_id_for("<a@x>"),
        {"status": "rejected", "by": "p@x", "at": datetime.now(UTC).isoformat()},
    )

    assert store.pending(entry_id_for("<b@x>")) == _entry(message_id="<b@x>")
    assert [e.message_id for e in store.live_entries()] == ["<b@x>"]
    assert store.result(entry_id_for("<a@x>"))["status"] == "rejected"


def test_s3_claim_is_conditional() -> None:
    store = S3ApprovalStore("bucket", client=FakeS3())
    assert store.claim("e1", {"by": "a@x"}) is True
    assert store.claim("e1", {"by": "b@x"}) is False
    store.release_claim("e1")
    assert store.claim("e1", {"by": "b@x"}) is True


def test_sweep_expires_stale_and_spares_fresh_and_resolved() -> None:
    fake = FakeS3()
    store = S3ApprovalStore("bucket", client=fake)
    now = datetime.now(UTC)
    old = (now - timedelta(days=4)).isoformat()

    store.put_pending(_entry(message_id="<stale@x>", created_at=old))
    store.put_pending(_entry(message_id="<fresh@x>", created_at=now.isoformat()))
    store.put_pending(_entry(message_id="<done@x>", created_at=old))
    store.put_result(
        entry_id_for("<done@x>"), {"status": "sent", "by": "p@x", "at": now.isoformat()}
    )

    expired = store.sweep(expiry_days=3, now=now)

    assert [e.message_id for e in expired] == ["<stale@x>"]
    assert store.result(entry_id_for("<stale@x>"))["status"] == "expired"
    # Fresh stays live; the already-sent one is untouched.
    assert [e.message_id for e in store.live_entries()] == ["<fresh@x>"]


def test_sweep_prunes_long_terminal_entries() -> None:
    fake = FakeS3()
    store = S3ApprovalStore("bucket", client=fake)
    now = datetime.now(UTC)
    long_ago = (now - timedelta(days=10)).isoformat()

    store.put_pending(_entry(message_id="<gone@x>", created_at=long_ago))
    store.put_result(entry_id_for("<gone@x>"), {"status": "sent", "by": "p@x", "at": long_ago})
    store.claim(entry_id_for("<gone@x>"), {"by": "p@x"})

    store.sweep(expiry_days=3, now=now)

    assert fake.objects == {}


# --- chat post ledger -------------------------------------------------------------
def test_chat_post_ledger_roundtrip_and_kinds_are_independent() -> None:
    ledger = ChatPostLedger()
    ledger.record("escalated", "<m1@x>")
    assert ledger.posted("escalated", "<m1@x>")
    assert not ledger.posted("blocked", "<m1@x>")

    again = ChatPostLedger.from_json(ledger.to_json())
    assert again.posted("escalated", "<m1@x>")


def test_chat_post_ledger_prunes_old_entries_and_survives_corruption() -> None:
    now = datetime.now(UTC)
    ledger = ChatPostLedger()
    ledger.record("escalated", "<old@x>", now=now - timedelta(days=8))
    ledger.record("escalated", "<new@x>", now=now)

    reloaded = ChatPostLedger.from_json(ledger.to_json(), now=now)
    assert not reloaded.posted("escalated", "<old@x>")
    assert reloaded.posted("escalated", "<new@x>")
    assert reloaded.dirty  # dropped something → worth writing back

    broken = ChatPostLedger.from_json("{not json", now=now)
    assert broken.entries == {}
    assert broken.dirty
