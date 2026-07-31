"""Unit tests for :class:`UrllibTransport`, the shared outbound HTTP seam.

Two real-world failures motivate these, both invisible until a live call is made because
every other test injects a fake transport and never exercises this class:

* **Compressed bodies.** ``urllib`` hands back the raw ``Content-Encoding`` bytes, unlike
  ``requests``/``httpx``. Transport Pro's gateway gzips whether or not the client asked, so
  a gzipped payload reached ``HttpResponse.json`` as binary and failed with
  ``expected JSON, got '\\x1f\\x8b...'``.
* **The default user agent.** urllib announces itself as ``Python-urllib/3.x``, which
  Cloudflare's managed bot rules reject — Groq answered with ``403 error 1010
  browser_signature_banned`` before the request reached the API.

``urlopen`` is monkeypatched, so nothing here touches the network.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
import zlib
from email.message import Message
from types import TracebackType
from typing import Any

import pytest

from payment_bot.clients.http import UrllibTransport
from payment_bot.errors import ClientError


class FakeResponse:
    """The subset of ``http.client.HTTPResponse`` that the transport touches."""

    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def _capture(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeResponse | urllib.error.HTTPError,
) -> list[urllib.request.Request]:
    """Patch ``urlopen`` to return ``response`` and record the requests it was given."""

    seen: list[urllib.request.Request] = []

    def fake_urlopen(req: urllib.request.Request, timeout: float = 30.0) -> Any:
        seen.append(req)
        if isinstance(response, urllib.error.HTTPError):
            raise response
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def _http_error(status: int, body: bytes, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError("https://api.example.com", status, "err", message, None)  # type: ignore[arg-type]


# --- Compressed bodies ------------------------------------------------------
@pytest.mark.unit
def test_gzipped_body_is_decompressed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"loadId": "2462934", "status": "BILLED"}
    _capture(
        monkeypatch,
        FakeResponse(200, gzip.compress(json.dumps(payload).encode()), {"Content-Encoding": "gzip"}),
    )

    response = UrllibTransport().request("GET", "https://api.example.com/load", headers={})

    assert response.json() == payload


@pytest.mark.unit
def test_zlib_wrapped_deflate_body_is_decompressed(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(
        monkeypatch,
        FakeResponse(200, zlib.compress(b'{"ok":true}'), {"Content-Encoding": "deflate"}),
    )

    response = UrllibTransport().request("GET", "https://api.example.com/x", headers={})

    assert response.json() == {"ok": True}


@pytest.mark.unit
def test_bare_deflate_body_is_decompressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some servers send RFC 1951 with no zlib wrapper; both must work."""

    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = compressor.compress(b'{"ok":true}') + compressor.flush()
    _capture(monkeypatch, FakeResponse(200, raw, {"Content-Encoding": "deflate"}))

    response = UrllibTransport().request("GET", "https://api.example.com/x", headers={})

    assert response.json() == {"ok": True}


@pytest.mark.unit
@pytest.mark.parametrize("encoding", ["", "identity"])
def test_uncompressed_body_passes_through(monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    headers = {"Content-Encoding": encoding} if encoding else {}
    _capture(monkeypatch, FakeResponse(200, b'{"ok":true}', headers))

    response = UrllibTransport().request("GET", "https://api.example.com/x", headers={})

    assert response.json() == {"ok": True}


@pytest.mark.unit
def test_unknown_encoding_is_passed_through_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """We only advertise gzip/deflate. Anything else is returned as-is rather than mangled."""

    _capture(monkeypatch, FakeResponse(200, b"\x01\x02\x03", {"Content-Encoding": "br"}))

    response = UrllibTransport().request("GET", "https://api.example.com/x", headers={})

    assert response.body == b"\x01\x02\x03"


@pytest.mark.unit
def test_corrupt_gzip_on_success_raises_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, FakeResponse(200, b"not actually gzip", {"Content-Encoding": "gzip"}))

    with pytest.raises(ClientError, match="cannot decode gzip"):
        UrllibTransport().request("GET", "https://api.example.com/x", headers={})


@pytest.mark.unit
def test_gzipped_error_body_is_decompressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx/5xx bodies carry the actionable detail, and gateways compress them too."""

    error = _http_error(401, b"", {"Content-Encoding": "gzip"})
    monkeypatch.setattr(error, "read", lambda: gzip.compress(b'{"error":"bad token"}'))
    _capture(monkeypatch, error)

    response = UrllibTransport().request("GET", "https://api.example.com/x", headers={})

    assert response.status == 401
    assert response.json() == {"error": "bad token"}


@pytest.mark.unit
def test_undecodable_error_body_is_surfaced_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt error body must not mask the status code the caller needs to report."""

    error = _http_error(503, b"", {"Content-Encoding": "gzip"})
    monkeypatch.setattr(error, "read", lambda: b"<html>gateway down</html>")
    _capture(monkeypatch, error)

    response = UrllibTransport().request("GET", "https://api.example.com/x", headers={})

    assert response.status == 503
    assert b"gateway down" in response.body


# --- Default headers --------------------------------------------------------
@pytest.mark.unit
def test_default_user_agent_is_not_python_urllib(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, FakeResponse(200, b"{}"))

    UrllibTransport().request("GET", "https://api.example.com/x", headers={})

    agent = seen[0].get_header("User-agent") or ""
    assert agent.startswith("payment-bot/")
    assert "urllib" not in agent.lower()


@pytest.mark.unit
def test_accept_encoding_matches_what_we_can_decompress(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, FakeResponse(200, b"{}"))

    UrllibTransport().request("GET", "https://api.example.com/x", headers={})

    assert seen[0].get_header("Accept-encoding") == "gzip, deflate"


@pytest.mark.unit
@pytest.mark.parametrize("supplied", ["User-Agent", "user-agent", "USER-AGENT"])
def test_caller_user_agent_wins_whatever_the_casing(
    monkeypatch: pytest.MonkeyPatch, supplied: str
) -> None:
    """Defaults must never duplicate or override a header the client set deliberately."""

    seen = _capture(monkeypatch, FakeResponse(200, b"{}"))

    UrllibTransport().request("GET", "https://api.example.com/x", headers={supplied: "custom/9"})

    assert seen[0].get_header("User-agent") == "custom/9"


@pytest.mark.unit
def test_caller_headers_are_still_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, FakeResponse(200, b"{}"))

    UrllibTransport().request(
        "POST",
        "https://api.example.com/x",
        headers={"Authorization": "Bearer t", "Content-Type": "application/json"},
        body=b"{}",
    )

    assert seen[0].get_header("Authorization") == "Bearer t"
    assert seen[0].get_header("Content-type") == "application/json"
    assert seen[0].get_method() == "POST"
