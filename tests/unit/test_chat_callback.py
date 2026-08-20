"""Unit tests for the chat-approval callback — the one sanctioned sender.

The invariants under test are the plan's §1/§5/§7 rules: nothing acts without a
verified token, nobody off the roster acts at all, the send is executed as the clicker
with the stored content (never the payload's), exactly one click wins a claim, and a
failed send releases the claim so the card stays retryable.
"""

from __future__ import annotations

import email
import json
from typing import Any

import pytest

from payment_bot import chat_callback
from payment_bot.approvals import InMemoryApprovalStore, PendingApproval, entry_id_for
from payment_bot.chat_callback import _act, _normalise_event, handler
from payment_bot.config import Settings

REVIEWERS = ("priya@circledelivers.com", "sam@circledelivers.com")


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "reviewers": REVIEWERS,
        "chat_audience": "123456789012",
        "approval_mode": "chat",
        "chat_space": "spaces/S",
    }
    base.update(overrides)
    return Settings(**base)


def _entry(message_id: str = "<m1@carrier.test>") -> PendingApproval:
    return PendingApproval(
        entry_id=entry_id_for(message_id),
        message_id=message_id,
        thread_id="t-reading",
        to="billing@carrier.test",
        cc=("paystatus@circledelivers.com",),
        reply_to="paystatus@circledelivers.com",
        subject="Re: Payment status for load 2462934",
        body="Load 2462934 is BILLED.\n\nCircle Delivers Payments",
        load_ids=("2462934",),
        chat_message="spaces/S/messages/M1",
    )


class FakeGmail:
    """Records what a click did in whose mailbox; the assertions read the raw MIME."""

    def __init__(self, clicker: str, *, fail: bool = False) -> None:
        self.clicker = clicker
        self.fail = fail
        self.sent: list[tuple[bytes, str]] = []
        self.drafted: list[tuple[bytes, str]] = []

    def resolve_thread(self, message_id: str) -> str:
        return "t-clicker-copy"

    def send(self, raw: bytes, thread_id: str) -> str:
        if self.fail:
            raise RuntimeError("gmail 503")
        self.sent.append((raw, thread_id))
        return "sent-1"

    def create_draft(self, raw: bytes, thread_id: str) -> str:
        self.drafted.append((raw, thread_id))
        return "draft-1"


def _store_with_entry() -> tuple[InMemoryApprovalStore, PendingApproval]:
    store = InMemoryApprovalStore()
    entry = _entry()
    store.put_pending(entry)
    return store, entry


# --- the identity rule -------------------------------------------------------------
def test_approve_sends_as_the_clicker_with_the_stored_content() -> None:
    store, entry = _store_with_entry()
    fakes: dict[str, FakeGmail] = {}

    def factory(clicker: str) -> FakeGmail:
        fakes[clicker] = FakeGmail(clicker)
        return fakes[clicker]

    response = _act(
        _settings(), store, entry.entry_id, "approve", "priya@circledelivers.com",
        gmail_factory=factory,
    )

    # The send happened in the CLICKER's mailbox — that, not a header, enforces From.
    assert list(fakes) == ["priya@circledelivers.com"]
    raw, thread_id = fakes["priya@circledelivers.com"].sent[0]
    assert thread_id == "t-clicker-copy"

    message = email.message_from_bytes(raw)
    assert message["From"] == "priya@circledelivers.com"
    assert message["To"] == "billing@carrier.test"
    assert message["Cc"] == "paystatus@circledelivers.com"
    assert message["Reply-To"] == "paystatus@circledelivers.com"
    assert message["In-Reply-To"] == "<m1@carrier.test>"
    assert message["Subject"] == "Re: Payment status for load 2462934"
    assert "Load 2462934 is BILLED." in message.get_payload()

    assert store.result(entry.entry_id)["status"] == "sent"
    body = json.loads(response["body"])
    assert body["actionResponse"]["type"] == "UPDATE_MESSAGE"
    assert "Sent by priya@circledelivers.com" in json.dumps(body)


def test_move_creates_a_draft_in_the_clickers_mailbox_and_sends_nothing() -> None:
    store, entry = _store_with_entry()
    fake = FakeGmail("sam@circledelivers.com")

    _act(
        _settings(), store, entry.entry_id, "move_to_drafts", "sam@circledelivers.com",
        gmail_factory=lambda clicker: fake,
    )

    assert fake.sent == []
    assert len(fake.drafted) == 1
    assert store.result(entry.entry_id)["status"] == "moved"


def test_reject_records_and_touches_no_mailbox() -> None:
    store, entry = _store_with_entry()

    def exploding_factory(clicker: str) -> FakeGmail:  # pragma: no cover - the assertion
        raise AssertionError("reject must not build a Gmail client")

    response = _act(
        _settings(), store, entry.entry_id, "reject", "priya@circledelivers.com",
        gmail_factory=exploding_factory,
    )

    assert store.result(entry.entry_id)["status"] == "rejected"
    assert "Rejected by" in response["body"]


# --- claims and races ----------------------------------------------------------------
def test_second_click_loses_the_claim_and_sends_nothing() -> None:
    store, entry = _store_with_entry()
    store.claim(entry.entry_id, {"by": "priya@circledelivers.com", "action": "approve"})

    fake = FakeGmail("sam@circledelivers.com")
    response = _act(
        _settings(), store, entry.entry_id, "approve", "sam@circledelivers.com",
        gmail_factory=lambda clicker: fake,
    )

    assert fake.sent == []
    assert "already handling" in response["body"]


def test_click_on_a_resolved_entry_reports_who_did_it() -> None:
    store, entry = _store_with_entry()
    store.put_result(entry.entry_id, {"status": "sent", "by": "priya@circledelivers.com", "at": "t"})

    fake = FakeGmail("sam@circledelivers.com")
    response = _act(
        _settings(), store, entry.entry_id, "approve", "sam@circledelivers.com",
        gmail_factory=lambda clicker: fake,
    )

    assert fake.sent == []
    assert "Already sent by priya@circledelivers.com" in response["body"]


def test_failed_send_releases_the_claim_for_a_retry() -> None:
    store, entry = _store_with_entry()

    _act(
        _settings(), store, entry.entry_id, "approve", "priya@circledelivers.com",
        gmail_factory=lambda clicker: FakeGmail(clicker, fail=True),
    )
    assert store.result(entry.entry_id) is None  # not terminal — still live

    # The retry (any reviewer) can now win the claim and send.
    ok = FakeGmail("sam@circledelivers.com")
    _act(
        _settings(), store, entry.entry_id, "approve", "sam@circledelivers.com",
        gmail_factory=lambda clicker: ok,
    )
    assert len(ok.sent) == 1
    assert store.result(entry.entry_id)["status"] == "sent"


def test_missing_entry_is_a_message_not_a_crash() -> None:
    response = _act(
        _settings(), InMemoryApprovalStore(), "nope", "approve", "priya@circledelivers.com",
        gmail_factory=lambda clicker: FakeGmail(clicker),
    )
    assert "entry is gone" in response["body"]


# --- event parsing ---------------------------------------------------------------------
def test_normalise_reads_all_three_event_shapes() -> None:
    """Legacy events in both button styles, and the add-ons schema that has no `type`
    at all — the shape the first live click actually arrived in."""

    legacy_common = {
        "type": "CARD_CLICKED",
        "user": {"email": "P@X.com"},
        "common": {"invokedFunction": "approve", "parameters": {"entry": "e1"}},
    }
    legacy_action = {
        "type": "CARD_CLICKED",
        "user": {"email": "p@x.com"},
        "action": {"actionMethodName": "reject", "parameters": [{"key": "entry", "value": "e2"}]},
    }
    addons_click = {
        "chat": {"user": {"email": "Priya@circledelivers.com"}, "buttonClickedPayload": {}},
        "commonEventObject": {
            "invokedFunction": "move_to_drafts",
            "parameters": {"entry": "e3"},
        },
    }
    addons_message = {
        "chat": {"user": {"email": "p@x.com"}, "messagePayload": {}},
        "commonEventObject": {},
    }

    assert _normalise_event(legacy_common) == ("CARD_CLICKED", "p@x.com", "approve", "e1", False)
    assert _normalise_event(legacy_action) == ("CARD_CLICKED", "p@x.com", "reject", "e2", False)
    assert _normalise_event(addons_click) == (
        "CARD_CLICKED",
        "priya@circledelivers.com",
        "move_to_drafts",
        "e3",
        True,
    )
    assert _normalise_event(addons_message)[0] == "MESSAGE"
    assert _normalise_event({}) == ("", "", "", "", False)


def test_the_action_parameter_beats_the_function_field() -> None:
    """Current cards put the endpoint URL in `function` (the add-ons runtime calls
    whatever is named there), so the verb arrives as a parameter and must win over
    invokedFunction — which is now a URL, not an action."""

    event = {
        "chat": {"user": {"email": "priya@circledelivers.com"}, "buttonClickedPayload": {}},
        "commonEventObject": {
            "invokedFunction": "https://xyz.lambda-url.us-east-1.on.aws/",
            "parameters": {"action": "approve", "entry": "e9"},
        },
    }
    assert _normalise_event(event) == (
        "CARD_CLICKED",
        "priya@circledelivers.com",
        "approve",
        "e9",
        True,
    )


def test_addons_events_get_addons_shaped_responses() -> None:
    """Add-ons-delivered events ignore the legacy reply shape — Chat renders its
    generic failure banner instead — so the response schema must match the event's."""

    store, entry = _store_with_entry()
    response = _act(
        _settings(), store, entry.entry_id, "approve", "priya@circledelivers.com",
        gmail_factory=lambda clicker: FakeGmail(clicker), addons=True,
    )
    body = json.loads(response["body"])
    update = body["hostAppDataAction"]["chatDataAction"]["updateMessageAction"]
    assert "Sent by priya@circledelivers.com" in json.dumps(update["message"]["cardsV2"])
    assert "actionResponse" not in body

    # A plain-text answer (claim already taken) wraps as a created message.
    store.claim(entry.entry_id, {"by": "x"})  # no-op: already resolved, but exercise text path
    text_response = _act(
        _settings(), InMemoryApprovalStore(), "missing", "approve",
        "priya@circledelivers.com",
        gmail_factory=lambda clicker: FakeGmail(clicker), addons=True,
    )
    text_body = json.loads(text_response["body"])
    created = text_body["hostAppDataAction"]["chatDataAction"]["createMessageAction"]
    assert "entry is gone" in created["message"]["text"]


# --- the handler's perimeter -------------------------------------------------------------
class AllowVerifier:
    def verify(self, token: str, extra_audiences: tuple[str, ...] = ()) -> bool:
        return bool(token)


class DenyVerifier:
    def verify(self, token: str, extra_audiences: tuple[str, ...] = ()) -> bool:
        return False


def _event(chat_event: dict[str, Any], *, token: str = "tok") -> dict[str, Any]:
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"Authorization": f"Bearer {token}"} if token else {},
        "body": json.dumps(chat_event),
    }


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_callback, "_SETTINGS", _settings())
    monkeypatch.setattr(chat_callback, "_VERIFIER", AllowVerifier())


def test_handler_rejects_unverified_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_callback, "_SETTINGS", _settings())
    monkeypatch.setattr(chat_callback, "_VERIFIER", DenyVerifier())

    response = handler(_event({"type": "CARD_CLICKED"}))
    assert response["statusCode"] == 401


def test_handler_denies_clickers_off_the_roster(wired: None) -> None:
    response = handler(
        _event(
            {
                "type": "CARD_CLICKED",
                "user": {"email": "intruder@circledelivers.com"},
                "common": {"invokedFunction": "approve", "parameters": {"entry": "e1"}},
            }
        )
    )
    assert response["statusCode"] == 200
    assert "not on the reviewer roster" in response["body"]


def test_handler_denies_off_roster_clicks_in_the_addons_schema_too(wired: None) -> None:
    response = handler(
        _event(
            {
                "chat": {
                    "user": {"email": "intruder@circledelivers.com"},
                    "buttonClickedPayload": {},
                },
                "commonEventObject": {
                    "invokedFunction": "approve",
                    "parameters": {"entry": "e1"},
                },
            }
        )
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    created = body["hostAppDataAction"]["chatDataAction"]["createMessageAction"]
    assert "not on the reviewer roster" in created["message"]["text"]


def test_handler_answers_non_click_events_politely(wired: None) -> None:
    response = handler(_event({"type": "ADDED_TO_SPACE"}))
    assert response["statusCode"] == 200
    assert "approval cards" in response["body"]


# --- token verification, with real crypto ------------------------------------------
# Live failure this pins down (2026-08-20): Chat's real tokens are signed by Google's
# OIDC keys, not the legacy chat@system cert set, and the audience may be the endpoint
# URL instead of the project number. The verifier must accept both eras and reject
# everything else.
import time as _time  # noqa: E402

AUDIENCE = "123456789012"
CALLBACK_URL = "https://xyz.lambda-url.us-east-1.on.aws/"


def _keypair() -> tuple[str, str]:
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _signed_token(private_pem: str, *, kid: str, iss: str, aud: str) -> str:
    google_jwt = pytest.importorskip("google.auth.jwt")
    from google.auth.crypt import RSASigner

    now = int(_time.time())
    signer = RSASigner.from_string(private_pem, kid)
    token = google_jwt.encode(signer, {"iss": iss, "aud": aud, "iat": now, "exp": now + 300})
    return token.decode("ascii") if isinstance(token, bytes) else str(token)


def _verifier_with(certs: dict[str, str]) -> Any:
    from payment_bot.chat_callback import ChatTokenVerifier

    verifier = ChatTokenVerifier(AUDIENCE)
    verifier._certs = certs
    verifier._certs_fetched_at = _time.time()  # cache hit: no network in tests
    return verifier


def test_verifier_accepts_both_issuer_eras_and_both_audiences() -> None:
    private_pem, public_pem = _keypair()
    verifier = _verifier_with({"kid-1": public_pem})

    legacy = _signed_token(
        private_pem, kid="kid-1", iss="chat@system.gserviceaccount.com", aud=AUDIENCE
    )
    assert verifier.verify(legacy) is True

    oidc_url_audience = _signed_token(
        private_pem, kid="kid-1", iss="https://accounts.google.com", aud=CALLBACK_URL
    )
    assert verifier.verify(oidc_url_audience, extra_audiences=(CALLBACK_URL,)) is True
    # The URL audience is only good when the handler vouches for its own URL.
    assert verifier.verify(oidc_url_audience) is False


def test_verifier_rejects_wrong_issuer_audience_and_unknown_key() -> None:
    private_pem, public_pem = _keypair()
    verifier = _verifier_with({"kid-1": public_pem})

    wrong_issuer = _signed_token(
        private_pem, kid="kid-1", iss="attacker@example.com", aud=AUDIENCE
    )
    assert verifier.verify(wrong_issuer) is False

    wrong_audience = _signed_token(
        private_pem, kid="kid-1", iss="accounts.google.com", aud="999999999999"
    )
    assert verifier.verify(wrong_audience) is False

    unknown_kid = _signed_token(
        private_pem, kid="kid-UNKNOWN", iss="accounts.google.com", aud=AUDIENCE
    )
    assert verifier.verify(unknown_kid) is False

    assert verifier.verify("") is False


def test_verifier_without_an_audience_rejects_everything() -> None:
    from payment_bot.chat_callback import ChatTokenVerifier

    private_pem, public_pem = _keypair()
    verifier = ChatTokenVerifier("")
    verifier._certs = {"kid-1": public_pem}
    verifier._certs_fetched_at = _time.time()
    good_shape = _signed_token(
        private_pem, kid="kid-1", iss="accounts.google.com", aud=AUDIENCE
    )
    assert verifier.verify(good_shape) is False
