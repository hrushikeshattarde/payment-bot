"""A tiny HTTP seam shared by the real API clients.

Every outbound HTTP client in this package (Transport Pro, Groq, Slack) goes through
:class:`HttpTransport`. That is what lets each of them be unit-tested with recorded
payloads and no network, exactly like the mock clients.

``urllib`` is used rather than ``requests``/``httpx`` so the runtime dependency set stays
at pydantic alone — the package still installs and tests without any SDK.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message as _Headers
from typing import Any, Protocol, runtime_checkable

from payment_bot import __version__
from payment_bot.errors import ClientError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A minimal HTTP response: status plus raw body."""

    status: int
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        """Decode the body as JSON, or raise :class:`ClientError`."""

        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except ValueError as exc:
            preview = self.body[:200].decode("utf-8", "replace")
            raise ClientError(f"expected JSON, got {preview!r}: {exc}") from exc

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


#: Headers added to every request unless the caller supplies its own value.
#:
#: ``User-Agent`` is not cosmetic. urllib's default is ``Python-urllib/3.x``, which
#: Cloudflare's managed bot rules reject outright — Groq answers such requests with
#: ``HTTP 403 error 1010 browser_signature_banned`` before the API is ever reached.
#:
#: ``Accept-Encoding`` is declared explicitly so the encodings we advertise are exactly the
#: ones :func:`_decompress` can undo. Some gateways (Transport Pro's included) gzip a
#: response whether or not the client asked, so decompression is needed either way.
_DEFAULT_HEADERS = {
    "User-Agent": f"payment-bot/{__version__}",
    "Accept-Encoding": "gzip, deflate",
}


def _decompress(raw: bytes, content_encoding: str) -> bytes:
    """Undo ``Content-Encoding`` on a response body.

    urllib, unlike ``requests``/``httpx``, hands back the compressed bytes as-is. Without
    this, a gzipped payload reaches :meth:`HttpResponse.json` as binary and surfaces as a
    confusing "expected JSON, got '\\x1f\\x8b...'" error.
    """

    token = content_encoding.strip().lower()
    if not raw or token in ("", "identity"):
        return raw
    try:
        if token == "gzip":
            return gzip.decompress(raw)
        if token == "deflate":
            # RFC 1950 (zlib-wrapped) is correct, but some servers send bare RFC 1951.
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error, EOFError) as exc:
        raise ClientError(f"cannot decode {token}-encoded response body: {exc}") from exc
    # An encoding we never advertised. Pass it through rather than guess.
    return raw


def _content_encoding(headers: _Headers | None) -> str:
    return str(headers.get("Content-Encoding", "")) if headers is not None else ""


@runtime_checkable
class HttpTransport(Protocol):
    """Performs one HTTP request. Injectable so tests need no network."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse: ...


class UrllibTransport:
    """:class:`HttpTransport` on the standard library.

    HTTP error statuses are returned like any other response, so callers can handle 401
    refresh / 404 / 429 themselves. Only genuine transport failures raise.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        # URLs are built from configuration (https API roots), never from model output.
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        # Defaults go on last and never override an explicit header from the caller.
        supplied = {key.lower() for key in headers}
        for key, value in _DEFAULT_HEADERS.items():
            if key.lower() not in supplied:
                req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return HttpResponse(
                    status=int(resp.status),
                    body=_decompress(raw, _content_encoding(resp.headers)),
                )
        except urllib.error.HTTPError as exc:  # 4xx/5xx are responses, not failures
            raw = exc.read()
            try:
                decoded = _decompress(raw, _content_encoding(exc.headers))
            except ClientError:
                # An error body we cannot decompress is still worth surfacing verbatim —
                # the status code is the useful part and callers put it in the message.
                decoded = raw
            return HttpResponse(status=int(exc.code), body=decoded)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ClientError(f"HTTP request to {url} failed: {exc}") from exc


#: Injectable sleep, so retry tests do not actually wait.
SleepFn = Callable[[float], None]


def default_sleep(seconds: float) -> None:
    time.sleep(seconds)
