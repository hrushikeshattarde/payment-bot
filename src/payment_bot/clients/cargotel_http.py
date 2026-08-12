"""Live CargoTel client: fetch ``loadmaint.mcgi`` and parse it.

Implements :class:`~payment_bot.clients.cargotel.CargoTelClient`. Nothing above the client
layer changes: the tools, gate, agent and pipeline work against the same typed model the
mock returns.

Auth is a single browser session cookie (``cgt-browser-session``) held in S3 and refreshed
by a separate login bot — CargoTel has no API and no service credential, so a scraped
session is the only way in. Two consequences this client handles rather than passes on:

1. **A stale cookie does not look like an error.** CargoTel answers ``200`` with the login
   page. Left alone that means every load parses as "no documents on file", and the bot
   tells a queue of carriers their paperwork is missing when it is not — the single worst
   failure available on this path, because it is confident and wrong. So the parser detects
   it and this client raises, which escalates the email instead of answering it.
2. **The cookie is fetched once per client, not once per load.** An email naming three
   loads costs one S3 read and three page fetches.

``boto3`` is imported lazily, the same way :class:`~payment_bot.clients.llm.BedrockLlmClient`
does it, so the package still installs and its tests still run without the AWS SDK.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from payment_bot.clients.cargotel import build_authorization_context
from payment_bot.clients.cargotel_html import parse_carrier_html, parse_load_html
from payment_bot.clients.http import HttpTransport, UrllibTransport
from payment_bot.config import Settings, get_settings
from payment_bot.errors import ClientError
from payment_bot.logging import get_logger
from payment_bot.models import AuthorizationContext
from payment_bot.models.cargotel import CargoTelCarrier, CargoTelLoad

__all__ = [
    "CargoTelHttpClient",
    "CargoTelSettings",
    "CookieSource",
    "S3CookieSource",
    "StaticCookieSource",
    "build_cargotel_client",
]

_log = get_logger("clients.cargotel")

#: Sent on every request. The user-agent is not cosmetic — CargoTel is fronted by rules that
#: treat a default ``Python-urllib`` agent differently, and the referer mirrors what a
#: browser sends when navigating from the menu frame.
_BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}


@runtime_checkable
class CookieSource(Protocol):
    """Supplies the CargoTel session cookie."""

    def cookie(self) -> str:
        """The current ``cgt-browser-session`` value."""


class StaticCookieSource:
    """A cookie supplied directly. For tests, and for a one-off local run."""

    def __init__(self, value: str) -> None:
        self._value = value

    def cookie(self) -> str:
        return self._value


class S3CookieSource:
    """Reads the cookie the login bot maintains in S3.

    The stored object is a browser cookie export — a JSON list of cookie records — so the
    value is dug out rather than read whole. The shape is asserted explicitly: a silent
    ``IndexError`` here would surface much later as an unexplained login page.
    """

    def __init__(self, bucket: str, key: str, profile: str = "") -> None:
        self._bucket = bucket
        self._key = key
        self._profile = profile
        self._cached: str | None = None
        #: Remembered failure, so one broken credential chain costs one round trip rather
        #: than one per load. A credentials or permissions error is never transient within a
        #: single email — observed live as five identical two-second failures in one run,
        #: producing five copies of the same message in the escalation.
        self._error: str | None = None

    def cookie(self) -> str:
        if self._cached is not None:
            return self._cached
        if self._error is not None:
            raise ClientError(self._error)
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - declared in the extra
            raise ClientError(
                'CargoTel needs the AWS SDK for its session cookie: pip install -e ".[aws]"'
            ) from exc

        try:
            session = boto3.Session(profile_name=self._profile) if self._profile else boto3
            response = session.client("s3").get_object(Bucket=self._bucket, Key=self._key)
            payload = json.loads(response["Body"].read().decode("utf-8"))
        except Exception as exc:  # boto3 raises a wide family; all mean "no cookie"
            self._error = _cookie_failure(exc, self._profile)
            # The bucket and key go to the log, not into the message. The message is read in
            # an escalation — once per unauthorized load — where the location is noise and
            # the action is the point; the log is where someone debugging wants the path.
            _log.error(
                "cargotel_cookie_unavailable",
                extra={
                    "bucket": self._bucket,
                    "key": self._key,
                    "profile": self._profile or "(default chain)",
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                },
            )
            raise ClientError(self._error) from exc

        value = _cookie_value(payload)
        if not value:
            raise ClientError(
                f"CargoTel: s3://{self._bucket}/{self._key} holds no usable cookie value"
            )
        self._cached = value
        return value


def _cookie_failure(exc: Exception, profile: str) -> str:
    """Turn a boto3 failure into the one sentence an operator can act on.

    "Could not read the cookie" has several distinct causes with different fixes, and they
    are not interchangeable: a missing credential is the operator's to set, an expired one is
    theirs to refresh, and a missing object means the login bot is not running and there is
    nothing wrong on this side at all. The previous single hint named only one remedy — set
    ``PAYBOT_AWS_PROFILE`` — which is not even how this deployment ended up credentialed
    (a ``[default]`` profile in ``~/.aws/credentials``), so it pointed at the wrong fix while
    sounding certain.

    Classified on the exception name and, for an API error, the S3 error code, read
    defensively so no botocore import is needed here — ``boto3`` is deliberately lazy.
    """

    name = type(exc).__name__
    response = getattr(exc, "response", None)
    code = ""
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")

    if name in {"NoCredentialsError", "PartialCredentialsError"} or code == "InvalidAccessKeyId":
        return (
            "CargoTel session unavailable: no usable AWS credentials, so the session cookie "
            "could not be read. Any one of these fixes it — a [default] profile in "
            "~/.aws/credentials, AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / "
            "AWS_SESSION_TOKEN set in the environment the bot runs in, or PAYBOT_AWS_PROFILE "
            "naming a profile. Note .env is not a credential source — boto3 never reads it."
        )
    if code in {"ExpiredToken", "ExpiredTokenException", "RequestExpired", "InvalidClientTokenId"}:
        return (
            "CargoTel session unavailable: the AWS credentials have expired, so the session "
            "cookie could not be read. Refresh them. Temporary credentials and SSO sessions "
            "both lapse, and boto3 cannot renew ones written to ~/.aws/credentials by hand — "
            "so this recurs on a schedule until a non-expiring credential is used."
        )
    if name == "ProfileNotFound":
        return (
            f"CargoTel session unavailable: AWS profile {profile or '(unset)'!r} does not "
            "exist, so the session cookie could not be read. Check PAYBOT_AWS_PROFILE "
            "against the profiles in ~/.aws/config."
        )
    if code in {"NoSuchKey", "NoSuchBucket", "404"}:
        return (
            "CargoTel session unavailable: the stored session cookie is missing. The login "
            "bot that maintains it has not written it — nothing here needs fixing, that does."
        )
    if code in {"AccessDenied", "403"}:
        return (
            "CargoTel session unavailable: these AWS credentials are valid but cannot read "
            "the stored session cookie — they are missing s3:GetObject on it."
        )
    return (
        f"CargoTel session unavailable: the session cookie could not be read ({name}). "
        "See the cargotel_cookie_unavailable log entry for the location and raw error."
    )


def _cookie_value(payload: Any) -> str | None:
    """Pull the cookie string out of a browser cookie export.

    Accepts the list-of-records shape the login bot writes, a bare mapping, and a plain
    string — cheap tolerance, because the alternative failure is a login page that looks
    like an empty load.
    """

    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, dict):
        payload = [payload]
    if isinstance(payload, list):
        for record in payload:
            if isinstance(record, str) and record.strip():
                return record.strip()
            if isinstance(record, dict):
                value = record.get("value") or record.get("cookie")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


class CargoTelHttpClient:
    """Read-only :class:`CargoTelClient` backed by the CargoTel back-office.

    Args:
        base_url: The ``loadmaint.mcgi`` endpoint.
        client_url: The ``client.mcgi`` endpoint, for carrier records.
        cookies: Where the session cookie comes from.
        transport: Injectable HTTP seam; defaults to :class:`UrllibTransport`.
        timeout: Per-request timeout in seconds.
        cache: Reuse one parsed page per load for this client's lifetime. Several tools read
            the same load in one email run and a single consistent snapshot is what
            grounding wants — build a fresh client per email.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client_url: str,
        cookies: CookieSource,
        transport: HttpTransport | None = None,
        timeout: float = 60.0,
        cache: bool = True,
    ) -> None:
        if not base_url:
            raise ClientError("CargoTel base_url is required")
        self._base = base_url
        #: The carrier client record endpoint — a different page from the load one.
        self._client_base = client_url
        self._cookies = cookies
        self._transport: HttpTransport = transport or UrllibTransport()
        self._timeout = timeout
        self._cache_enabled = cache
        self._loads: dict[str, CargoTelLoad] = {}
        self._carriers: dict[str, CargoTelCarrier] = {}

    def _fetch(self, url: str, what: str) -> str:
        response = self._transport.request(
            "GET",
            url,
            headers={**_BROWSER_HEADERS, "cookie": f"cgt-browser-session={self._cookies.cookie()}"},
            timeout=self._timeout,
        )
        if response.status != 200:
            raise ClientError(f"CargoTel: {what} returned HTTP {response.status}")
        return response.text()

    def get_load(self, load_id: str) -> CargoTelLoad:
        key = _require_load_id(load_id)
        cached = self._loads.get(key)
        if cached is not None:
            return cached

        url = f"{self._base}?{urllib.parse.urlencode({'load_id': key})}"
        # parse_load_html raises on the login page rather than returning an empty load.
        load = parse_load_html(self._fetch(url, f"load {key}"), key)
        if self._cache_enabled:
            self._loads[key] = load
        return load

    def get_carrier(self, client_id: str) -> CargoTelCarrier:
        """The carrier's client record.

        A second fetch, and worth it: it is the only source of contact addresses (so the
        only thing authorization can key off) and it supplies the payment term for loads
        that carry none. Cached per client rather than per load, because several loads in
        one email routinely share a carrier — three of the nine sample loads are the same
        APFAS account.
        """

        key = _require_id(client_id, "client id")
        cached = self._carriers.get(key)
        if cached is not None:
            return cached

        url = f"{self._client_base}?{urllib.parse.urlencode({'id': key})}"
        carrier = parse_carrier_html(self._fetch(url, f"carrier {key}"), key)
        if self._cache_enabled:
            self._carriers[key] = carrier
        return carrier

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        """Who may receive disclosure about this load.

        A load with no carrier assigned yields an empty context rather than an error, so an
        unrecognised sender falls through to DENY and the gate blocks the send — the same
        fail-closed shape the Transport Pro path uses.
        """

        load = self.get_load(load_id)
        carrier: CargoTelCarrier | None = None
        if load.carrier_client_id:
            try:
                carrier = self.get_carrier(load.carrier_client_id)
            except ClientError as exc:
                # An unreadable carrier record must not read as "no authorized parties"
                # without a trace: that is a denial the reviewer would otherwise chase as a
                # roster problem.
                _log.warning(
                    "cargotel_carrier_unreadable",
                    extra={
                        "load_id": load_id,
                        "client_id": load.carrier_client_id,
                        "error": str(exc),
                    },
                )
        return build_authorization_context(load, carrier)


def _require_id(value: str, label: str) -> str:
    """Validate an id before it reaches a URL. CargoTel ids are numeric."""

    key = (value or "").strip()
    if not key.isdigit():
        raise ClientError(f"CargoTel: {value!r} is not a valid {label}")
    return key


def _require_load_id(load_id: str) -> str:
    """Validate the id before it reaches a URL.

    Load ids come from email text, so this is where untrusted input would otherwise meet a
    request. Every id on this path is a 6-digit number (§4.1), which makes a digits-only
    check both accurate and the tightest possible guard.
    """

    key = (load_id or "").strip()
    if not key.isdigit():
        raise ClientError(f"CargoTel: {load_id!r} is not a numeric load id")
    return key


@dataclass(frozen=True, slots=True)
class CargoTelSettings:
    """The configuration a :class:`CargoTelHttpClient` needs."""

    base_url: str
    client_url: str
    cookie_bucket: str
    cookie_key: str
    timeout: float = 60.0
    #: Named AWS profile for the cookie read. Blank = default chain (correct in Lambda).
    aws_profile: str = ""

    def build_client(self, transport: HttpTransport | None = None) -> CargoTelHttpClient:
        return CargoTelHttpClient(
            base_url=self.base_url,
            client_url=self.client_url,
            cookies=S3CookieSource(self.cookie_bucket, self.cookie_key, self.aws_profile),
            transport=transport,
            timeout=self.timeout,
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> CargoTelSettings:
        """Read ``PAYBOT_CARGOTEL_*`` configuration.

        Raises:
            ClientError: If CargoTel is not fully configured. Failing at start-up is
                deliberate: a half-configured client must never reach the point of
                answering a carrier.
        """

        resolved = settings or get_settings()
        if not resolved.cargotel_configured:
            raise ClientError(
                "CargoTel is not configured: set PAYBOT_CARGOTEL_BASE_URL, "
                "PAYBOT_CARGOTEL_COOKIE_BUCKET and PAYBOT_CARGOTEL_COOKIE_KEY"
            )
        return cls(
            base_url=resolved.cargotel_base_url,
            client_url=resolved.cargotel_client_url,
            cookie_bucket=resolved.cargotel_cookie_bucket,
            cookie_key=resolved.cargotel_cookie_key,
            timeout=resolved.cargotel_timeout_seconds,
            aws_profile=resolved.aws_profile,
        )


def build_cargotel_client(
    settings: Settings | None = None,
    transport: HttpTransport | None = None,
) -> CargoTelHttpClient:
    """Build a live CargoTel client. One **per email**, so each run gets one snapshot."""

    return CargoTelSettings.from_settings(settings).build_client(transport=transport)
