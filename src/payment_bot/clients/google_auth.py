"""Google service-account authentication with domain-wide delegation (PRD §8.1.2).

A service account has **no mailbox of its own**. To read or draft in
``paystatus@circledelivers.com`` it must *impersonate* that user, which a Workspace
super-admin has to authorise once: Admin console → Security → Access and data control →
API controls → **Domain-wide delegation** → add the service account's numeric client id
together with the exact scopes it may use. Without that entry, every call returns
``unauthorized_client`` no matter how valid the key is.

The flow implemented here is the standard two-legged JWT bearer grant:

1. Build a JWT claiming ``iss`` = the service account, ``scope`` = the scopes, and
   ``sub`` = the user to impersonate (that ``sub`` is what makes it delegation).
2. Sign it with the key's RSA private key.
3. Exchange it at Google's token endpoint for a short-lived access token.
4. Send that token as ``Authorization: Bearer`` on Gmail API calls.

``google-auth`` is used for RSA signing only — the token exchange and every Gmail call go
through our own :class:`HttpTransport`, so this module stays testable with no network and
adds no HTTP stack. Install it with the ``google`` extra.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

from payment_bot.clients.http import HttpTransport, UrllibTransport
from payment_bot.errors import ClientError
from payment_bot.logging import get_logger

_log = get_logger("clients.google_auth")

TOKEN_URI = "https://oauth2.googleapis.com/token"
_JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: Read the mailbox. Sufficient for intake on its own.
SCOPE_GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
#: Create drafts. NOTE: Google provides no draft-only scope — `gmail.compose` also permits
#: sending. See `GMAIL_DRAFT_SCOPES` for what that means for us.
SCOPE_GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"

#: The scopes a draft-only run needs.
#:
#: ⚠ There is deliberately no "create drafts but cannot send" Gmail scope. ``gmail.compose``
#: is the narrowest scope that allows ``drafts.create``, and it *also* allows
#: ``messages.send``. So with the Gmail API backend the credential is technically capable of
#: sending, and the no-send guarantee rests on our code — the client's ``send_reply`` raises,
#: the pipeline never takes the send path, and the resolver never approves. That is a real
#: difference from the IMAP backend, where the app password could not send at all. It is
#: called out in docs/LOCAL_RUN.md rather than glossed over.
GMAIL_DRAFT_SCOPES: tuple[str, ...] = (SCOPE_GMAIL_READONLY, SCOPE_GMAIL_COMPOSE)

#: Read-only: genuinely incapable of sending or drafting. Use when you only want intake.
GMAIL_READONLY_SCOPES: tuple[str, ...] = (SCOPE_GMAIL_READONLY,)

_TOKEN_LIFETIME_SECONDS = 3600
#: Refresh a little early so a long run never presents an expired token.
_EXPIRY_SKEW_SECONDS = 120


def load_service_account_info(
    *,
    file_path: str = "",
    inline_json: str = "",
) -> dict[str, Any]:
    """Load a service-account key from a file path or an inline JSON blob.

    Args:
        file_path: Path to the downloaded ``*.json`` key.
        inline_json: The key's JSON content directly, for environments that inject secrets
            as a single variable rather than a file.

    Raises:
        ClientError: If neither is supplied, or the content is not a usable key.
    """

    raw: str
    if inline_json.strip():
        raw = inline_json.lstrip("﻿")
        source = "inline JSON"
    elif file_path.strip():
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ClientError(f"Google service-account key not found: {path}")
        # utf-8-sig strips a byte-order mark if present and behaves as utf-8 otherwise.
        # Windows editors readily add a BOM, and json.loads rejects it.
        raw = path.read_text(encoding="utf-8-sig")
        source = str(path)
    else:
        raise ClientError(
            "no Google service-account key configured: set PAYBOT_GOOGLE_SA_FILE "
            "(path to the JSON key) or PAYBOT_GOOGLE_SA_JSON (its contents)"
        )

    try:
        info = json.loads(raw)
    except ValueError as exc:
        raise ClientError(f"Google service-account key at {source} is not valid JSON: {exc}") from exc
    if not isinstance(info, dict):
        raise ClientError(f"Google service-account key at {source} is not a JSON object")

    missing = [key for key in ("client_email", "private_key") if not info.get(key)]
    if missing:
        raise ClientError(
            f"Google service-account key at {source} is missing {missing}. "
            "Download the JSON key for the service account (not an OAuth client secret)."
        )
    return info


class ServiceAccountTokenSource:
    """Mints and caches impersonated access tokens for one user and scope set.

    Args:
        info: The parsed service-account key (see :func:`load_service_account_info`).
        subject: The user to impersonate, e.g. ``paystatus@circledelivers.com``.
        scopes: Scopes to request. Must match what the admin authorised for delegation.
        transport: Injectable HTTP seam.
        timeout: Per-request timeout.
        clock: Injectable time source, so token-expiry logic is testable.
    """

    def __init__(
        self,
        info: dict[str, Any],
        subject: str,
        scopes: tuple[str, ...] = GMAIL_DRAFT_SCOPES,
        *,
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
        clock: Any = time.time,
    ) -> None:
        if not subject:
            raise ClientError("an impersonation subject is required for domain-wide delegation")
        if not scopes:
            raise ClientError("at least one scope is required")
        self._info = info
        self._subject = subject
        self._scopes = scopes
        # Honour the key's own token_uri when present; every Google-issued key carries it.
        self._token_uri = str(info.get("token_uri") or TOKEN_URI)
        self._transport: HttpTransport = transport or UrllibTransport()
        self._timeout = timeout
        self._clock = clock
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def subject(self) -> str:
        return self._subject

    @property
    def client_email(self) -> str:
        return str(self._info.get("client_email", ""))

    @property
    def client_id(self) -> str:
        """The numeric client id an admin enters when authorising delegation."""

        return str(self._info.get("client_id", ""))

    @property
    def project_id(self) -> str:
        """The GCP project the key belongs to — where the Gmail API must be enabled."""

        return str(self._info.get("project_id", ""))

    @property
    def scopes(self) -> tuple[str, ...]:
        return self._scopes

    def token(self) -> str:
        """Return a valid access token, refreshing it when needed."""

        now = float(self._clock())
        if self._token is not None and now < self._expires_at - _EXPIRY_SKEW_SECONDS:
            return self._token

        assertion = self._signed_assertion(now)
        body = urllib.parse.urlencode(
            {"grant_type": _JWT_BEARER_GRANT, "assertion": assertion}
        ).encode()
        response = self._transport.request(
            "POST",
            self._token_uri,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=body,
            timeout=self._timeout,
        )

        if not response.ok:
            raise ClientError(self._explain_token_failure(response.status, response.text()))

        data = response.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ClientError("Google token endpoint returned no access_token")

        self._token = str(data["access_token"])
        expires_in = data.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else _TOKEN_LIFETIME_SECONDS
        self._expires_at = now + lifetime
        _log.info(
            "google_token_minted",
            extra={"subject": self._subject, "expires_in": lifetime},
        )
        return self._token

    # -- internals -----------------------------------------------------------
    def _signed_assertion(self, now: float) -> str:
        try:
            from google.auth import jwt as google_jwt
            from google.auth.crypt import RSASigner
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ClientError(
                "google-auth is required for the Gmail API backend. Install the 'google' "
                "extra: pip install -e \".[dev,google]\""
            ) from exc

        issued_at = int(now)
        payload = {
            "iss": self._info["client_email"],
            "scope": " ".join(self._scopes),
            "aud": self._token_uri,
            "iat": issued_at,
            "exp": issued_at + _TOKEN_LIFETIME_SECONDS,
            # `sub` is what turns this into domain-wide delegation: act as this user.
            "sub": self._subject,
        }
        try:
            # google-auth ships no type information, hence the narrow ignores.
            signer = RSASigner.from_service_account_info(self._info)  # type: ignore[no-untyped-call]
            token = google_jwt.encode(signer, payload)  # type: ignore[no-untyped-call]
        except (ValueError, KeyError, TypeError) as exc:
            raise ClientError(
                "cannot sign the service-account JWT: the 'private_key' in the key is not a "
                "usable PEM. Re-download the service account's JSON key from the Google Cloud "
                f"console and do not edit it (escaped newlines matter). Detail: {exc}"
            ) from exc
        return token.decode("ascii") if isinstance(token, bytes) else str(token)

    def _explain_token_failure(self, status: int, body: str) -> str:
        """Turn Google's terse token errors into something actionable."""

        detail = body[:300]
        if "unauthorized_client" in body:
            return (
                f"Google refused the delegated token (HTTP {status}: unauthorized_client). "
                "Domain-wide delegation is not authorised for this service account, or the "
                "scopes do not match exactly. In the Admin console → Security → API controls "
                f"→ Domain-wide delegation, add client id {self.client_id or '(see key)'} "
                f"with scopes: {', '.join(self._scopes)}"
            )
        if "invalid_grant" in body:
            return (
                f"Google refused the delegated token (HTTP {status}: invalid_grant). Usually "
                f"the impersonated user {self._subject!r} does not exist in the domain, or the "
                "machine clock is skewed. Check the address and the system time."
            )
        if "invalid_scope" in body:
            return (
                f"Google rejected the requested scopes (HTTP {status}). Authorise exactly "
                f"these in the Admin console: {', '.join(self._scopes)}"
            )
        return f"Google token request failed (HTTP {status}): {detail}"
