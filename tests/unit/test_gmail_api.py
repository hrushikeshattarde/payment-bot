"""Unit tests for the Gmail API backend and service-account delegation.

Covers the JWT-bearer token exchange, the three Gmail calls, and — most importantly — that
the diagnostics name the real fix, because domain-wide-delegation failures are otherwise
opaque (``unauthorized_client`` tells you nothing about what to do).

A real RSA key is generated once for the signing test; everything else uses a fake token
source so no crypto or network is involved.
"""

from __future__ import annotations

import base64
import email
import json
from typing import Any

import pytest

from payment_bot.clients.gmail_api import GmailApiClient, build_gmail_api_client
from payment_bot.clients.google_auth import (
    GMAIL_DRAFT_SCOPES,
    ServiceAccountTokenSource,
    load_service_account_info,
)
from payment_bot.clients.http import HttpResponse
from payment_bot.config import Settings
from payment_bot.errors import ClientError
from payment_bot.models import InboundEmail

CARRIER = InboundEmail(
    message_id="<abc123@mail.ideaexpedited.com>",
    thread_id="thread-9911",
    from_email="billing@ideaexpedited.com",
    from_name="Idea Expedited Billing",
    subject="Payment status for load 2462934",
    body="When will we be paid?",
)

REPLY = "Hello,\n\nLoad 2462934 is BILLED. Total pending $4,650.\n\nCircle Delivers Payments"
CC = ("hrushikesh.attarde@circledelivers.com",)

RAW_INBOUND = """\
From: Idea Expedited Billing <billing@ideaexpedited.com>
To: paystatus@circledelivers.com
Subject: Payment status for load 2462934
Message-ID: <abc123@mail.ideaexpedited.com>
Content-Type: text/plain; charset="utf-8"

Could you tell me the payment status for load 2462934?
"""


class FakeHttp:
    """Routes by URL fragment; records every request."""

    def __init__(self, routes: list[tuple[str, int, Any]]) -> None:
        self.routes = routes
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        for fragment, status, payload in self.routes:
            if fragment in url:
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return HttpResponse(status, raw)
        return HttpResponse(404, b'{"error":{"message":"no route"}}')


class FakeTokens:
    """Stands in for a token source without any crypto."""

    def __init__(self, value: str = "ya29.fake") -> None:
        self.value = value
        self.calls = 0

    def token(self) -> str:
        self.calls += 1
        return self.value

    @property
    def subject(self) -> str:
        return "paystatus@circledelivers.com"

    @property
    def client_id(self) -> str:
        return "1234567890"

    @property
    def project_id(self) -> str:
        return "gsheets-python-350615"


def _client(http: FakeHttp, **kwargs: Any) -> GmailApiClient:
    return GmailApiClient(
        FakeTokens(),  # type: ignore[arg-type]
        user="paystatus@circledelivers.com",
        transport=http,
        **kwargs,
    )


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# --- service-account key loading --------------------------------------------
@pytest.mark.unit
def test_key_is_loaded_from_inline_json() -> None:
    info = load_service_account_info(
        inline_json=json.dumps({"client_email": "sa@p.iam.gserviceaccount.com", "private_key": "k"})
    )
    assert info["client_email"] == "sa@p.iam.gserviceaccount.com"


@pytest.mark.unit
def test_key_is_loaded_from_a_file(tmp_path: Any) -> None:
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"client_email": "sa@x.com", "private_key": "k"}), encoding="utf-8")
    assert load_service_account_info(file_path=str(path))["client_email"] == "sa@x.com"


@pytest.mark.unit
def test_no_key_configured_is_an_actionable_error() -> None:
    with pytest.raises(ClientError, match="PAYBOT_GOOGLE_SA_FILE"):
        load_service_account_info()


@pytest.mark.unit
def test_oauth_client_secret_instead_of_a_key_is_rejected_clearly() -> None:
    """A common mix-up: downloading an OAuth client secret rather than an SA key."""

    wrong = json.dumps({"installed": {"client_id": "x", "client_secret": "y"}})
    with pytest.raises(ClientError, match=r"missing \['client_email', 'private_key'\]"):
        load_service_account_info(inline_json=wrong)


@pytest.mark.unit
def test_missing_file_is_reported_with_the_path() -> None:
    with pytest.raises(ClientError, match="not found"):
        load_service_account_info(file_path="/nope/sa.json")


@pytest.mark.unit
def test_key_file_with_a_utf8_bom_is_accepted(tmp_path: Any) -> None:
    """Windows editors add a BOM, and json.loads rejects it."""

    path = tmp_path / "sa.json"
    path.write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"client_email": "sa@x", "private_key": "k"}).encode()
    )
    assert load_service_account_info(file_path=str(path))["client_email"] == "sa@x"


@pytest.mark.unit
def test_inline_key_with_a_bom_is_accepted() -> None:
    blob = "﻿" + json.dumps({"client_email": "sa@x", "private_key": "k"})
    assert load_service_account_info(inline_json=blob)["client_email"] == "sa@x"


# --- the delegated token exchange -------------------------------------------
@pytest.mark.unit
def test_token_request_uses_the_jwt_bearer_grant_with_impersonation() -> None:
    """The signed assertion must carry `sub` — that is what makes it delegation."""

    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    http = FakeHttp([("oauth2.googleapis.com/token", 200, {"access_token": "ya29.x", "expires_in": 3600})])
    source = ServiceAccountTokenSource(
        {"client_email": "sa@p.iam.gserviceaccount.com", "private_key": pem, "client_id": "999"},
        subject="paystatus@circledelivers.com",
        transport=http,
        clock=lambda: 1_000_000.0,
    )

    assert source.token() == "ya29.x"

    body = (http.requests[0]["body"] or b"").decode()
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in body
    assertion = body.split("assertion=")[1]
    import urllib.parse

    claims = json.loads(
        base64.urlsafe_b64decode(
            _pad(urllib.parse.unquote(assertion).split(".")[1])
        ).decode()
    )
    assert claims["iss"] == "sa@p.iam.gserviceaccount.com"
    assert claims["sub"] == "paystatus@circledelivers.com"  # ← delegation
    assert claims["scope"] == " ".join(GMAIL_DRAFT_SCOPES)
    assert claims["aud"] == "https://oauth2.googleapis.com/token"


def _pad(value: str) -> str:
    return value + "=" * (-len(value) % 4)


@pytest.mark.unit
def test_token_is_cached_until_it_nears_expiry() -> None:
    http = FakeHttp([("token", 200, {"access_token": "t1", "expires_in": 3600})])
    now = [1_000_000.0]
    source = ServiceAccountTokenSource(
        {"client_email": "sa@x", "private_key": "k"},
        subject="paystatus@circledelivers.com",
        transport=http,
        clock=lambda: now[0],
    )
    # Bypass signing: this test is about the cache, not the crypto.
    source._signed_assertion = lambda _now: "fake.jwt.assertion"  # type: ignore[method-assign]
    source._token = "cached"
    source._expires_at = now[0] + 3600

    assert source.token() == "cached"
    assert http.requests == []  # no exchange needed

    now[0] += 3550  # inside the refresh skew
    assert source.token() == "t1"
    assert len(http.requests) == 1


@pytest.mark.unit
def test_unauthorized_client_explains_domain_wide_delegation() -> None:
    """The single most likely failure — the message must say exactly what to do."""

    http = FakeHttp([("token", 401, {"error": "unauthorized_client"})])
    source = ServiceAccountTokenSource(
        {"client_email": "sa@x", "private_key": "k", "client_id": "1234567890"},
        subject="paystatus@circledelivers.com",
        transport=http,
        clock=lambda: 1.0,
    )
    source._token = None
    with pytest.raises(ClientError) as excinfo:
        source._token = None
        source._expires_at = 0.0
        # Bypass signing; only the failure branch is under test.
        source._signed_assertion = lambda _now: "fake.jwt.assertion"  # type: ignore[method-assign]
        source.token()

    message = str(excinfo.value)
    assert "Domain-wide delegation" in message
    assert "1234567890" in message  # the client id an admin must enter
    assert "gmail.compose" in message  # the scopes they must authorise


@pytest.mark.unit
def test_invalid_grant_points_at_the_user_or_the_clock() -> None:
    http = FakeHttp([("token", 400, {"error": "invalid_grant"})])
    source = ServiceAccountTokenSource(
        {"client_email": "sa@x", "private_key": "k"},
        subject="typo@circledelivers.com",
        transport=http,
        clock=lambda: 1.0,
    )
    source._signed_assertion = lambda _now: "fake.jwt"  # type: ignore[method-assign]
    with pytest.raises(ClientError, match=r"does not exist in the domain"):
        source.token()


@pytest.mark.unit
def test_subject_is_required() -> None:
    with pytest.raises(ClientError, match="impersonation subject"):
        ServiceAccountTokenSource({"client_email": "a", "private_key": "k"}, subject="")


# --- fetch ------------------------------------------------------------------
def _thread(*messages: dict[str, Any]) -> dict[str, Any]:
    """A `format=metadata` thread response."""

    return {"messages": list(messages)}


def _thread_message(
    message_id: str,
    from_address: str,
    internal_date: str = "1000",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "internalDate": internal_date,
        "labelIds": labels if labels is not None else ["INBOX"],
        "payload": {"headers": [{"name": "From", "value": from_address}]},
    }


_CARRIER = "billing@ideaexpedited.com"
_COLLEAGUE = "angelica.baracao@circledelivers.com"


@pytest.mark.unit
def test_fetch_lists_then_gets_raw_and_parses() -> None:
    http = FakeHttp(
        [
            ("/messages?", 200, {"messages": [{"id": "m1", "threadId": "t1"}]}),
            ("/threads/t1", 200, _thread(_thread_message("m1", _CARRIER))),
            (
                "/messages/m1",
                200,
                {"id": "m1", "threadId": "t1", "labelIds": ["UNREAD", "INBOX"], "raw": _b64(RAW_INBOUND)},
            ),
        ]
    )
    emails = _client(http).fetch_new()

    assert len(emails) == 1
    inbound = emails[0]
    assert inbound.from_email == "billing@ideaexpedited.com"
    assert inbound.subject == "Payment status for load 2462934"
    assert "2462934" in inbound.body
    # The API gives a real thread id, unlike IMAP.
    assert inbound.thread_id == "t1"
    assert inbound.labels == ["UNREAD", "INBOX"]

    assert "q=is%3Aunread" in http.requests[0]["url"]
    assert "/threads/t1" in http.requests[1]["url"]
    assert "format=RAW" in http.requests[2]["url"]
    assert http.requests[0]["headers"]["Authorization"] == "Bearer ya29.fake"


# --- thread awareness -------------------------------------------------------
# A Gmail query matches messages, not conversations. Answering per message meant replying to
# threads a colleague had already handled, and adding a second draft to a thread on every
# re-run (mark_seen is off by design, so the same mail keeps matching).
@pytest.mark.unit
def test_thread_whose_newest_message_is_ours_is_skipped() -> None:
    """A colleague already replied — the carrier is waiting on nothing."""

    http = FakeHttp(
        [
            ("/messages?", 200, {"messages": [{"id": "m1", "threadId": "t1"}]}),
            (
                "/threads/t1",
                200,
                _thread(
                    _thread_message("m1", _CARRIER, "1000"),
                    _thread_message("m2", _COLLEAGUE, "2000"),
                ),
            ),
        ]
    )
    assert _client(http).fetch_new() == []
    # It never fetched a body.
    assert not any("format=RAW" in r["url"] for r in http.requests)


@pytest.mark.unit
def test_thread_that_already_has_a_draft_is_skipped() -> None:
    """This is what stops a re-run stacking duplicate drafts on one conversation."""

    http = FakeHttp(
        [
            ("/messages?", 200, {"messages": [{"id": "m1", "threadId": "t1"}]}),
            (
                "/threads/t1",
                200,
                _thread(
                    _thread_message("m1", _CARRIER, "1000"),
                    _thread_message("d1", _COLLEAGUE, "1500", labels=["DRAFT"]),
                ),
            ),
        ]
    )
    assert _client(http).fetch_new() == []


@pytest.mark.unit
def test_the_newest_carrier_message_is_answered_not_the_matched_one() -> None:
    """A carrier who chased twice gets one reply, to their latest message."""

    http = FakeHttp(
        [
            ("/messages?", 200, {"messages": [{"id": "m1", "threadId": "t1"}]}),
            (
                "/threads/t1",
                200,
                _thread(
                    _thread_message("m1", _CARRIER, "1000"),
                    _thread_message("m9", _CARRIER, "9000"),
                ),
            ),
            (
                "/messages/m9",
                200,
                {"id": "m9", "threadId": "t1", "labelIds": ["UNREAD"], "raw": _b64(RAW_INBOUND)},
            ),
        ]
    )
    assert len(_client(http).fetch_new()) == 1
    assert any("/messages/m9" in r["url"] for r in http.requests)


@pytest.mark.unit
def test_several_messages_in_one_thread_produce_one_reply() -> None:
    """Two matches, one conversation, one draft — not two."""

    http = FakeHttp(
        [
            (
                "/messages?",
                200,
                {"messages": [{"id": "m2", "threadId": "t1"}, {"id": "m1", "threadId": "t1"}]},
            ),
            ("/threads/t1", 200, _thread(_thread_message("m2", _CARRIER, "2000"))),
            (
                "/messages/m2",
                200,
                {"id": "m2", "threadId": "t1", "labelIds": ["UNREAD"], "raw": _b64(RAW_INBOUND)},
            ),
        ]
    )
    assert len(_client(http).fetch_new()) == 1
    assert sum(1 for r in http.requests if "/threads/" in r["url"]) == 1


@pytest.mark.unit
def test_ownership_is_by_domain_not_by_the_impersonated_mailbox() -> None:
    """Group mail arrives from colleagues, so any sender on our domain counts as answered."""

    client = _client(FakeHttp([]))
    assert client._is_ours("Angelica <angelica.baracao@circledelivers.com>") is True
    assert client._is_ours("paystatus@circledelivers.com") is True
    assert client._is_ours(f"Carrier <{_CARRIER}>") is False
    # A lookalike domain must not read as ours.
    assert client._is_ours("x@notcircledelivers.com") is False


@pytest.mark.unit
def test_empty_listing_returns_no_emails() -> None:
    http = FakeHttp([("/messages?", 200, {"resultSizeEstimate": 0})])
    assert _client(http).fetch_new() == []


@pytest.mark.unit
def test_since_adds_a_gmail_after_term() -> None:
    http = FakeHttp([("/messages?", 200, {})])
    _client(http).fetch_new(since="2026/07/01")
    assert "after%3A2026%2F07%2F01" in http.requests[0]["url"]


@pytest.mark.unit
def test_limit_is_passed_as_max_results() -> None:
    http = FakeHttp([("/messages?", 200, {})])
    _client(http, limit=3).fetch_new()
    assert "maxResults=3" in http.requests[0]["url"]


# --- draft ------------------------------------------------------------------
@pytest.mark.unit
def test_draft_is_created_with_cc_and_thread() -> None:
    http = FakeHttp([("/drafts", 200, {"id": "r-99", "message": {"id": "m-99"}})])
    draft = _client(http).create_draft(CARRIER, REPLY, CC)

    request = json.loads(http.requests[0]["body"] or b"{}")
    assert request["message"]["threadId"] == "thread-9911"

    mime = email.message_from_bytes(
        base64.urlsafe_b64decode(_pad(request["message"]["raw"]))
    )
    assert mime["From"] == "paystatus@circledelivers.com"
    assert mime["To"] == "billing@ideaexpedited.com"
    assert mime["Cc"] == "hrushikesh.attarde@circledelivers.com"
    assert mime["Subject"] == "Re: Payment status for load 2462934"
    assert mime["In-Reply-To"] == "<abc123@mail.ideaexpedited.com>"
    payload = mime.get_payload(decode=True)
    assert isinstance(payload, bytes) and payload.decode().strip() == REPLY.strip()

    assert draft.to == "billing@ideaexpedited.com"
    assert draft.cc == CC
    assert "r-99" in draft.folder


@pytest.mark.unit
def test_draft_endpoint_is_drafts_not_send() -> None:
    """The whole safety claim for this backend: we only ever call drafts.create."""

    http = FakeHttp([("/drafts", 200, {"id": "r-1"})])
    _client(http).create_draft(CARRIER, REPLY, CC)

    urls = [r["url"] for r in http.requests]
    assert all(url.endswith("/drafts") for url in urls)
    assert not any("send" in url for url in urls)


@pytest.mark.unit
def test_send_reply_raises() -> None:
    from payment_bot.clients.gmail_api import SendingDisabledError

    with pytest.raises(SendingDisabledError, match="draft-only"):
        _client(FakeHttp([])).send_reply("t", "m", "body", "x@y.com")


# --- error diagnostics ------------------------------------------------------
@pytest.mark.unit
def test_insufficient_scope_names_the_scopes_to_authorise() -> None:
    http = FakeHttp([("/drafts", 403, {"error": {"message": "Request had insufficient authentication scopes"}})])
    with pytest.raises(ClientError) as excinfo:
        _client(http).create_draft(CARRIER, REPLY, CC)
    assert "gmail.compose" in str(excinfo.value)


@pytest.mark.unit
def test_401_points_at_delegation_and_the_mailbox() -> None:
    http = FakeHttp([("/messages?", 401, {"error": {"message": "Invalid Credentials"}})])
    with pytest.raises(ClientError) as excinfo:
        _client(http).fetch_new()
    message = str(excinfo.value)
    assert "domain-wide delegation" in message
    assert "1234567890" in message


@pytest.mark.unit
def test_gmail_api_not_enabled_names_the_project_and_the_enable_url() -> None:
    """The likeliest failure when the service account was made for another API."""

    http = FakeHttp(
        [
            (
                "/profile",
                403,
                {
                    "error": {
                        "status": "PERMISSION_DENIED",
                        "message": (
                            "Gmail API has not been used in project gsheets-python-350615 "
                            "before or it is disabled."
                        ),
                    }
                },
            )
        ]
    )
    with pytest.raises(ClientError) as excinfo:
        _client(http).verify_access()

    message = str(excinfo.value)
    assert "not enabled" in message
    assert "gsheets-python-350615" in message
    assert "console.cloud.google.com/apis/library/gmail.googleapis.com" in message


@pytest.mark.unit
def test_verify_access_returns_the_impersonated_profile() -> None:
    """The cheap read-only probe that separates delegation from an empty search."""

    http = FakeHttp(
        [("/profile", 200, {"emailAddress": "paystatus@circledelivers.com", "messagesTotal": 412})]
    )
    profile = _client(http).verify_access()

    assert profile["emailAddress"] == "paystatus@circledelivers.com"
    assert profile["messagesTotal"] == 412
    assert http.requests[0]["url"].endswith("/profile")


@pytest.mark.unit
def test_mailbox_without_gmail_enabled_is_explained() -> None:
    http = FakeHttp([("/profile", 400, {"error": {"status": "FAILED_PRECONDITION"}})])
    with pytest.raises(ClientError, match="no Gmail mailbox"):
        _client(http).verify_access()


@pytest.mark.unit
def test_token_uri_from_the_key_is_honoured() -> None:
    """Google-issued keys carry their own token_uri; use it rather than a hard-coded one."""

    http = FakeHttp([("oauth2.googleapis.com/token", 200, {"access_token": "t"})])
    source = ServiceAccountTokenSource(
        {
            "client_email": "gsheets-python@gsheets-python-350615.iam.gserviceaccount.com",
            "private_key": "k",
            "client_id": "101455662173429172836",
            "project_id": "gsheets-python-350615",
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        subject="paystatus@circledelivers.com",
        transport=http,
        clock=lambda: 1.0,
    )
    source._signed_assertion = lambda _now: "fake.jwt"  # type: ignore[method-assign]

    assert source.token() == "t"
    assert source.project_id == "gsheets-python-350615"
    assert source.client_id == "101455662173429172836"
    assert http.requests[0]["url"] == "https://oauth2.googleapis.com/token"


@pytest.mark.unit
def test_404_questions_the_mailbox_address() -> None:
    http = FakeHttp([("/messages?", 404, {"error": {"message": "Not Found"}})])
    with pytest.raises(ClientError, match="right mailbox"):
        _client(http).fetch_new()


@pytest.mark.unit
def test_rate_limit_suggests_lowering_the_fetch_limit() -> None:
    http = FakeHttp([("/messages?", 429, {"error": {"message": "Too many requests"}})])
    with pytest.raises(ClientError, match="PAYBOT_GMAIL_FETCH_LIMIT"):
        _client(http).fetch_new()


# --- configuration ----------------------------------------------------------
@pytest.mark.unit
def test_a_key_plus_the_default_mailbox_is_configured() -> None:
    settings = Settings(google_sa_file="/tmp/sa.json")
    assert settings.google_sa_configured is True
    # gmail_user falls back to `mailbox`, which has a default.
    assert settings.gmail_configured is True


@pytest.mark.unit
def test_inline_json_also_counts_as_configured() -> None:
    from pydantic import SecretStr

    settings = Settings(google_sa_json=SecretStr('{"client_email":"a","private_key":"k"}'))
    assert settings.gmail_configured is True


@pytest.mark.unit
def test_nothing_configured_is_not_configured() -> None:
    settings = Settings()
    assert settings.google_sa_configured is False
    assert settings.gmail_configured is False


@pytest.mark.unit
def test_factory_builds_from_settings(tmp_path: Any) -> None:
    key = tmp_path / "sa.json"
    key.write_text(
        json.dumps({"client_email": "sa@x", "private_key": "k", "client_id": "7"}), encoding="utf-8"
    )
    settings = Settings(
        google_sa_file=str(key),
        gmail_user="paystatus@circledelivers.com",
        gmail_query="is:unread newer_than:2d",
    )
    client = build_gmail_api_client(settings, transport=FakeHttp([]))
    assert client.user == "paystatus@circledelivers.com"
