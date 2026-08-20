"""Google Chat interaction callback — the ONE place this system sends email.

Everything else keeps the never-send invariant: the worker's Gmail client raises on
``send_reply``, the pipeline never takes the send path in draft-only, and the resolver
never approves. This Lambda is the deliberate exception the Stage-2 split reserved
("sending moves into a separate callback function with its own role"), and it holds the
identity rule of CHAT_APPROVAL_PLAN.md §1:

**The send is always executed as the person who clicked** — the verified Workspace email
in Google Chat's interaction event, impersonated for that one send via the existing
domain-wide delegation. There is no "send as" parameter anywhere in this module's inputs;
reviewer A cannot cause a send under reviewer B's name, and nobody off the configured
roster can cause a send at all.

Two more rules the code below must keep:

* **Content never comes from the click.** Buttons carry an entry id and an action name;
  body, recipients and headers come from the pending entry the *worker* stored. A
  tampered payload can at most point at a different pending entry — which still sends
  only what the gate passed, from the clicker.
* **Verify before parse.** Google Chat authenticates itself with a bearer ID token
  signed by ``chat@system.gserviceaccount.com`` for the app's project-number audience.
  Nothing touches S3 or Gmail until that token verifies; failures return 401 and one
  log line.

Double-clicks and platform retries are absorbed by the store's conditional claim
(exactly one click wins; the rest see who won). A claim whose action then fails is
released so a later click can retry — a crash cannot wedge an entry shut.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
from typing import Any

from payment_bot.approvals import ApprovalStore, PendingApproval, S3ApprovalStore
from payment_bot.clients.google_auth import (
    GMAIL_DRAFT_SCOPES,
    ServiceAccountTokenSource,
    load_service_account_info,
)
from payment_bot.clients.google_chat import (
    ACTION_APPROVE,
    ACTION_MOVE,
    ACTION_REJECT,
    approval_card,
)
from payment_bot.clients.http import HttpTransport, UrllibTransport
from payment_bot.clients.mime import build_reply
from payment_bot.config import Settings, get_settings
from payment_bot.logging import configure_logging, get_logger
from payment_bot.models import InboundEmail

_log = get_logger("chat_callback")

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

#: Who may sign Chat interaction tokens. Two issuers because Google migrated: the
#: legacy Chat system account, and standard Google OIDC — observed live 2026-08-20,
#: where the real token's key id was NOT in the chat@system cert set and rejection
#: showed as "Payment Bot not responding" in the space.
_CHAT_ISSUERS = frozenset(
    {
        "chat@system.gserviceaccount.com",
        "accounts.google.com",
        "https://accounts.google.com",
    }
)
#: Both signing-key sets, merged at fetch: the legacy Chat account's certs and
#: Google's OIDC federation certs. Same x509 kid→PEM shape either way.
_CHAT_CERT_URLS = (
    "https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com",
    "https://www.googleapis.com/oauth2/v1/certs",
)
#: How long fetched signing certs are reused. Google rotates them slowly; an hour keeps
#: warm invocations to zero cert fetches without trusting a stale key for long.
_CERTS_TTL_SECONDS = 3600


# ---------------------------------------------------------------------------
# Bootstrap — deliberately NOT lambda_handler.bootstrap: importing that module drags in
# the pipeline, the agent loop and boto3 wiring this function never uses. The callback
# needs one secret and the settings object.
# ---------------------------------------------------------------------------
def _resolve_google_secret(environ: dict[str, str] | None = None) -> None:
    """Fetch the service-account key into PAYBOT_GOOGLE_SA_JSON, once per container."""

    env = os.environ if environ is None else environ
    if env.get("PAYBOT_GOOGLE_SA_JSON", "").strip():
        return
    secret_id = env.get("PAYBOT_SECRET_GOOGLE_SA", "").strip()
    if not secret_id:
        return
    import boto3

    value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
    env["PAYBOT_GOOGLE_SA_JSON"] = value


_SETTINGS: Settings | None = None


def _settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        configure_logging(os.environ.get("PAYBOT_LOG_LEVEL", "INFO"))
        _resolve_google_secret()
        get_settings.cache_clear()
        _SETTINGS = get_settings()
        _log.info(
            "chat_callback_cold_start",
            extra={
                "reviewers": len(_SETTINGS.reviewers),
                "audience_configured": bool(_SETTINGS.chat_audience),
            },
        )
    return _SETTINGS


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------
class ChatTokenVerifier:
    """Verifies Google Chat's bearer token against Google's published certs.

    Uses ``google.auth.jwt`` — already a dependency for service-account signing — so
    verification adds no package. The transport is injectable, and tests replace the
    whole verifier; nothing else in the handler knows how verification works.
    """

    def __init__(
        self,
        audience: str,
        *,
        transport: HttpTransport | None = None,
        clock: Any = time.time,
    ) -> None:
        self._audience = audience
        self._transport: HttpTransport = transport or UrllibTransport()
        self._clock = clock
        self._certs: dict[str, str] = {}
        self._certs_fetched_at = 0.0

    def verify(self, token: str, extra_audiences: tuple[str, ...] = ()) -> bool:
        """Signature + expiry via google-auth, then issuer and audience by hand.

        Audience is checked manually (not via ``decode``'s parameter) because Google
        stamps different values depending on the app's configuration era: the Cloud
        project number, or the endpoint URL itself. ``extra_audiences`` lets the
        handler pass its own URL (derived from the request's Host header) so either
        stamp verifies — both are still cryptographically bound to this app. On
        rejection the claimed iss/aud are logged (never the token), so a mismatch is a
        log line naming the fix rather than a silent "not responding" in the space.
        """

        if not token or not self._audience:
            # No audience configured means verification CANNOT succeed. Failing closed
            # here is what makes a half-deployed callback inert rather than open.
            return False
        try:
            from google.auth import jwt as google_jwt

            claims = google_jwt.decode(  # type: ignore[no-untyped-call]
                token, certs=self._fresh_certs(), audience=None
            )
        except Exception as exc:
            _log.warning("chat_token_rejected", extra={"error": str(exc)[:200]})
            return False

        issuer = str(claims.get("iss") or "")
        if issuer not in _CHAT_ISSUERS:
            _log.warning("chat_token_wrong_issuer", extra={"issuer": issuer[:100]})
            return False

        audience = str(claims.get("aud") or "")
        allowed = {self._audience, *(a for a in extra_audiences if a)}
        if audience not in allowed:
            _log.warning(
                "chat_token_wrong_audience",
                extra={"audience": audience[:200], "expected": self._audience},
            )
            return False
        return True

    def _fresh_certs(self) -> dict[str, str]:
        now = float(self._clock())
        if self._certs and now - self._certs_fetched_at < _CERTS_TTL_SECONDS:
            return self._certs
        merged: dict[str, str] = {}
        errors: list[str] = []
        for url in _CHAT_CERT_URLS:
            try:
                response = self._transport.request(
                    "GET", url, headers={"Accept": "application/json"}, timeout=10.0
                )
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}")
                certs = response.json()
                if not isinstance(certs, dict):
                    raise RuntimeError("no keys in response")
                merged.update({str(k): str(v) for k, v in certs.items()})
            except Exception as exc:  # one set degrading must not kill the other
                errors.append(f"{url}: {exc}")
        if not merged:
            raise RuntimeError(f"no signing certs fetched: {'; '.join(errors)[:300]}")
        if errors:
            _log.warning("chat_certs_partial", extra={"errors": "; ".join(errors)[:300]})
        self._certs = merged
        self._certs_fetched_at = now
        return self._certs


_VERIFIER: ChatTokenVerifier | None = None


def _verifier(settings: Settings) -> ChatTokenVerifier:
    global _VERIFIER
    if _VERIFIER is None:
        _VERIFIER = ChatTokenVerifier(settings.chat_audience)
    return _VERIFIER


# ---------------------------------------------------------------------------
# Gmail, as the clicker
# ---------------------------------------------------------------------------
class ClickerGmail:
    """The three Gmail calls a click can need, executed as one verified reviewer.

    Deliberately not :class:`~payment_bot.clients.gmail_api.GmailApiClient`: that
    client's ``send_reply`` raises by design and must keep doing so. The send lives
    here, in the one module whose whole purpose is sending, wrapped so every call is
    logged with whose mailbox it touched and why (the reviewer-mailbox discipline of
    REVIEWER_DRAFTS_DESIGN.md §7 carries over).
    """

    def __init__(
        self,
        info: dict[str, Any],
        clicker: str,
        *,
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._clicker = clicker
        self._transport: HttpTransport = transport or UrllibTransport()
        self._timeout = timeout
        self._tokens = ServiceAccountTokenSource(
            info, subject=clicker, scopes=GMAIL_DRAFT_SCOPES, transport=transport, timeout=timeout
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _user(self) -> str:
        return urllib.parse.quote(self._clicker)

    def resolve_thread(self, message_id: str) -> str:
        """The clicker's own thread id for the carrier's message, or ``""``.

        Reviewers are members of the group, so their mailbox holds its own copy of the
        original — under a *different* thread id than the reading mailbox's (thread ids
        are per-mailbox). ``""`` (no copy found) is fine: the reply still threads at
        the carrier's end via In-Reply-To/References; it merely starts its own
        conversation in the clicker's mailbox view.
        """

        query = urllib.parse.quote(f"rfc822msgid:{message_id.strip('<>')}")
        response = self._transport.request(
            "GET",
            f"{GMAIL_API_BASE}/users/{self._user()}/messages?q={query}&maxResults=1",
            headers=self._headers(),
            timeout=self._timeout,
        )
        if not response.ok:
            _log.warning(
                "clicker_thread_unresolved",
                extra={"clicker": self._clicker, "status": response.status},
            )
            return ""
        data = response.json()
        messages = data.get("messages") if isinstance(data, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            return str(messages[0].get("threadId") or "")
        return ""

    def send(self, raw_rfc822: bytes, thread_id: str) -> str:
        """``messages.send`` as the clicker. Returns the sent message id. Raises on failure."""

        payload: dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(raw_rfc822).decode("ascii")
        }
        if thread_id:
            payload["threadId"] = thread_id
        response = self._transport.request(
            "POST",
            f"{GMAIL_API_BASE}/users/{self._user()}/messages/send",
            headers=self._headers(),
            body=json.dumps(payload).encode("utf-8"),
            timeout=self._timeout,
        )
        if not response.ok:
            raise RuntimeError(f"messages.send failed: HTTP {response.status}: {response.text()[:300]}")
        data = response.json()
        return str(data.get("id") or "") if isinstance(data, dict) else ""

    def create_draft(self, raw_rfc822: bytes, thread_id: str) -> str:
        """``drafts.create`` in the clicker's mailbox (the Move-to-my-Drafts edit path)."""

        message: dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(raw_rfc822).decode("ascii")
        }
        if thread_id:
            message["threadId"] = thread_id
        response = self._transport.request(
            "POST",
            f"{GMAIL_API_BASE}/users/{self._user()}/drafts",
            headers=self._headers(),
            body=json.dumps({"message": message}).encode("utf-8"),
            timeout=self._timeout,
        )
        if not response.ok:
            raise RuntimeError(f"drafts.create failed: HTTP {response.status}: {response.text()[:300]}")
        data = response.json()
        return str(data.get("id") or "") if isinstance(data, dict) else ""


def _reply_mime(entry: PendingApproval, from_address: str) -> bytes:
    """The outbound message, built by the same builder every draft goes through.

    Reconstructing a minimal :class:`InboundEmail` keeps one MIME path in the codebase:
    To from the entry, threading headers from the original message id, Reply-To back at
    the group. From is the clicker — and Gmail stamps the authenticated sender at send
    time anyway, so the mailbox executing the call is what enforces it, not this header.
    """

    source = InboundEmail(
        message_id=entry.message_id,
        thread_id=entry.thread_id,
        from_email=entry.to,
        subject=entry.subject,
    )
    mime = build_reply(
        source,
        entry.body,
        from_address=from_address,
        cc=entry.cc,
        subject=entry.subject,
        reply_to=entry.reply_to,
    )
    return mime.as_bytes()


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------
def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """Function URL target for Google Chat interaction events."""

    settings = _settings()
    event = event or {}

    method = str(
        ((event.get("requestContext") or {}).get("http") or {}).get("method") or ""
    ).upper()
    if method and method != "POST":
        return _http(405, {"text": "POST only"})

    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
    bearer = headers.get("authorization", "")
    token = bearer[len("Bearer ") :] if bearer.startswith("Bearer ") else ""
    # Google may stamp the token's audience as the endpoint URL rather than the project
    # number, depending on the app configuration; the URL is derivable from the request
    # itself, so both known-good audiences are offered to the verifier.
    host = headers.get("host", "").strip()
    own_urls = (f"https://{host}/", f"https://{host}") if host else ()
    if not _verifier(settings).verify(token, extra_audiences=own_urls):
        return _http(401, {"text": "unverified"})

    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")
    try:
        chat_event = json.loads(body)
        if not isinstance(chat_event, dict):
            raise ValueError("event is not an object")
    except ValueError:
        return _http(400, {"text": "unreadable event"})

    kind, clicker, action, entry_id, addons = _normalise_event(chat_event)
    # The shape log exists because the first live click cost a debugging session: the
    # event arrived in the add-ons schema (no `type`, payload under `chat.*`) and the
    # handler silently answered "nothing to act on". Never guess the schema again.
    _log.info(
        "chat_event_received",
        extra={
            "schema": "addons" if addons else "legacy",
            "kind": kind or "(none)",
            "action": action or "(none)",
            "event": _sanitised(chat_event),
        },
    )

    if kind != "CARD_CLICKED":
        # Added-to-space / plain messages / removals — nothing to act on; answer
        # politely so adding the app to the space shows something sane.
        return _respond_text(
            "Payment bot: approval cards are posted here by the worker; "
            "the buttons on each card are the interface.",
            addons,
        )

    if not action or not entry_id:
        return _respond_text("That button carried no action — repost the card.", addons)

    roster = {r.strip().lower() for r in settings.reviewers if r.strip()}
    if clicker not in roster:
        _log.warning(
            "approval_denied_not_reviewer", extra={"clicker": clicker, "entry_id": entry_id}
        )
        return _respond_text(
            f"Sorry — {clicker or 'this account'} is not on the reviewer roster, "
            "so it cannot act on approvals.",
            addons,
        )

    bucket = os.environ.get("PAYBOT_ROSTER_BUCKET", "").strip()
    if not bucket:
        return _respond_text("Misconfigured: no state bucket. Nothing was done.", addons)
    store = S3ApprovalStore(bucket)

    return _act(settings, store, entry_id, action, clicker, addons=addons)


def _act(
    settings: Settings,
    store: ApprovalStore,
    entry_id: str,
    action: str,
    clicker: str,
    *,
    gmail_factory: Any = None,
    addons: bool = False,
) -> dict[str, Any]:
    """Claim the entry and perform one action. Split from :func:`handler` so tests can
    drive it with an in-memory store and a fake Gmail without forging HTTP events."""

    existing = store.result(entry_id)
    if existing is not None:
        status = str(existing.get("status") or "done")
        by = str(existing.get("by") or "someone")
        return _update_card_response(
            store, entry_id, f"Already {status} by {by} — nothing further to do.",
            addons=addons,
        )

    raw = store.pending(entry_id)
    if raw is None:
        return _respond_text(
            "This card's entry is gone (expired and pruned, or never stored). "
            "The mail needs a human reply from the group mailbox.",
            addons,
        )

    now = _now_iso()
    if not store.claim(entry_id, {"by": clicker, "action": action, "at": now}):
        return _respond_text("Someone is already handling this one — check the card.", addons)

    try:
        if action == ACTION_REJECT:
            store.put_result(entry_id, {"status": "rejected", "by": clicker, "at": now})
            _log.info("approval_rejected", extra={"entry_id": entry_id, "by": clicker})
            return _update_card_response(
                store, entry_id, f"Rejected by {clicker} — needs a human reply.",
                entry=raw, addons=addons,
            )

        factory = gmail_factory or _default_gmail_factory(settings)
        gmail: ClickerGmail = factory(clicker)
        thread_id = gmail.resolve_thread(raw.message_id)
        mime = _reply_mime(raw, clicker)

        if action == ACTION_MOVE:
            draft_id = gmail.create_draft(mime, thread_id)
            store.put_result(
                entry_id,
                {"status": "moved", "by": clicker, "at": now, "draft_id": draft_id},
            )
            _log.info("approval_moved", extra={"entry_id": entry_id, "by": clicker})
            return _update_card_response(
                store,
                entry_id,
                f"With {clicker} in Gmail Drafts — edit there and send from Gmail.",
                entry=raw,
                addons=addons,
            )

        if action == ACTION_APPROVE:
            sent_id = gmail.send(mime, thread_id)
            store.put_result(
                entry_id,
                {"status": "sent", "by": clicker, "at": now, "gmail_id": sent_id},
            )
            _log.info(
                "approval_sent",
                extra={"entry_id": entry_id, "by": clicker, "gmail_id": sent_id},
            )
            return _update_card_response(
                store, entry_id, f"Sent by {clicker} · {now} · from {clicker}",
                entry=raw, addons=addons,
            )

        store.release_claim(entry_id)
        return _respond_text(f"Unknown action {action!r} — nothing was done.", addons)
    except Exception as exc:
        # Release so a later click can retry: a claim without a result would otherwise
        # wedge the entry shut behind a transient Gmail failure.
        store.release_claim(entry_id)
        _log.warning(
            "approval_send_failed",
            extra={"entry_id": entry_id, "by": clicker, "action": action, "error": str(exc)},
        )
        return _respond_text(
            f"That failed ({str(exc)[:200]}) — the card is still live, try again.", addons
        )


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------
def _default_gmail_factory(settings: Settings) -> Any:
    info = load_service_account_info(
        file_path=settings.google_sa_file,
        inline_json=settings.google_sa_json.get_secret_value(),
    )
    return lambda clicker: ClickerGmail(info, clicker, timeout=settings.google_timeout_seconds)


def _normalise_event(chat_event: dict[str, Any]) -> tuple[str, str, str, str, bool]:
    """(kind, clicker_email, action, entry_id, addons) across BOTH event schemas.

    Google Chat delivers two shapes depending on how the app is provisioned. The
    legacy shape carries a top-level ``type`` and the click under ``common`` /
    ``action``. Apps configured through the current console land on the **add-ons
    infrastructure** (the ``gcp-sa-gsuiteaddons`` service agent in the config page is
    the tell) and deliver a top-level ``chat`` object with per-kind payload keys and
    the invoked function under ``commonEventObject`` — with no ``type`` at all, which
    is how the first live click slipped through as "nothing to act on". Read both;
    trust neither for anything beyond a verb, an id, and the verified user's address.
    """

    if isinstance(chat_event.get("chat"), dict):
        chat = chat_event["chat"]
        if "buttonClickedPayload" in chat:
            kind = "CARD_CLICKED"
        elif "messagePayload" in chat or "appCommandPayload" in chat:
            kind = "MESSAGE"
        elif "addedToSpacePayload" in chat:
            kind = "ADDED_TO_SPACE"
        else:
            kind = ""
        user = chat.get("user") if isinstance(chat.get("user"), dict) else {}
        clicker = str(user.get("email") or "").strip().lower()
        common = chat_event.get("commonEventObject") or {}
        parameters = common.get("parameters") if isinstance(common.get("parameters"), dict) else {}
        # The verb rides in the parameters: on the add-ons runtime the `function` field
        # holds the endpoint URL, so invokedFunction is the URL, not the action. The
        # fallback keeps cards posted before that fix clickable.
        action = str(parameters.get("action") or common.get("invokedFunction") or "")
        entry = str(parameters.get("entry") or "")
        return kind, clicker, action, entry, True

    kind = str(chat_event.get("type") or "")
    user = chat_event.get("user") if isinstance(chat_event.get("user"), dict) else {}
    clicker = str(user.get("email") or "").strip().lower()

    common = chat_event.get("common") or {}
    action_obj = chat_event.get("action") or {}
    values: dict[str, str] = {}
    parameters = common.get("parameters")
    if isinstance(parameters, dict):
        values = {str(k): str(v) for k, v in parameters.items()}
    for item in action_obj.get("parameters") or []:
        if isinstance(item, dict) and item.get("key") is not None:
            values.setdefault(str(item["key"]), str(item.get("value") or ""))
    # Parameters first for the same reason as the add-ons branch: `function` carries
    # the endpoint URL on current cards, so it stopped naming the verb.
    action = str(
        values.get("action")
        or common.get("invokedFunction")
        or action_obj.get("actionMethodName")
        or action_obj.get("function")
        or ""
    )
    entry = values.get("entry", "")
    return kind, clicker, action, entry, False


def _sanitised(chat_event: dict[str, Any]) -> str:
    """The event for the shape log: authorization material dropped, size capped.

    The event is the user's own data landing in their own log group, but tokens must
    never be written anywhere — ``authorizationEventObject`` carries them in the
    add-ons schema, and anything token-like is stripped defensively.
    """

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: "(redacted)"
                if "token" in k.lower() or "authorization" in k.lower()
                else scrub(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    try:
        return json.dumps(scrub(chat_event), sort_keys=True)[:2000]
    except (TypeError, ValueError):  # pragma: no cover - json-parsed input is dumpable
        return "(unserialisable)"


def _respond_text(text: str, addons: bool) -> dict[str, Any]:
    """A new message into the space, in whichever response schema the event demands.

    Add-ons-delivered events ignore the legacy ``{"text": …}`` reply outright — the
    space renders Google's generic failure banner instead — so the response schema
    must always match the event schema.
    """

    if addons:
        return _http(
            200,
            {
                "hostAppDataAction": {
                    "chatDataAction": {"createMessageAction": {"message": {"text": text}}}
                }
            },
        )
    return _http(200, {"text": text})


def _update_card_response(
    store: ApprovalStore,
    entry_id: str,
    status: str,
    entry: PendingApproval | None = None,
    *,
    addons: bool = False,
) -> dict[str, Any]:
    """Rewrite the clicked card in place: it becomes its own terminal record."""

    entry = entry or store.pending(entry_id)
    if entry is None:
        return _respond_text(status, addons)
    card = approval_card(
        entry_id=entry.entry_id,
        from_email=entry.to,
        load_ids=entry.load_ids,
        to=entry.to,
        cc=entry.cc,
        reply_to=entry.reply_to,
        subject=entry.subject,
        body=entry.body,
        status=status,
        message_id=entry.message_id,
    )
    if addons:
        return _http(
            200,
            {
                "hostAppDataAction": {
                    "chatDataAction": {
                        "updateMessageAction": {"message": {"cardsV2": [card]}}
                    }
                }
            },
        )
    return _http(
        200,
        {"actionResponse": {"type": "UPDATE_MESSAGE"}, "cardsV2": [card]},
    )


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _http(status: int, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(message),
    }
