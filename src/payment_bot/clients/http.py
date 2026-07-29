"""A tiny HTTP seam shared by the real API clients.

Every outbound HTTP client in this package (Transport Pro, Groq, Slack) goes through
:class:`HttpTransport`. That is what lets each of them be unit-tested with recorded
payloads and no network, exactly like the mock clients.

``urllib`` is used rather than ``requests``/``httpx`` so the runtime dependency set stays
at pydantic alone — the package still installs and tests without any SDK.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

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
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(status=int(resp.status), body=resp.read())
        except urllib.error.HTTPError as exc:  # 4xx/5xx are responses, not failures
            return HttpResponse(status=int(exc.code), body=exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ClientError(f"HTTP request to {url} failed: {exc}") from exc


#: Injectable sleep, so retry tests do not actually wait.
SleepFn = Callable[[float], None]


def default_sleep(seconds: float) -> None:
    time.sleep(seconds)
