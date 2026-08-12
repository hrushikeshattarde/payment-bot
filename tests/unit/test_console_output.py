"""``configure_console_output`` — the guard that keeps a finished run from dying at its report.

The bug this covers is specific and was live on Windows: the demo's report is drawn with
``─`` (U+2500) and ``→`` (U+2192), a Windows console encodes as cp1252, and neither character
survives that. The pipeline completed, then ``print`` raised ``UnicodeEncodeError`` — so a
successful run looked like a crash.

These tests drive a real cp1252 stream rather than asserting the call was made, because the
thing worth pinning is that writing the report *succeeds*, not that a particular API was
touched.
"""

from __future__ import annotations

import io
import sys

import pytest

from payment_bot.logging import configure_console_output

pytestmark = pytest.mark.unit

#: Every non-cp1252 character the console reports are built from.
#:
#: ``─``/``→`` come from ``runner._render``; ``⚠``/``✗`` from the local runner's check output.
#: Kept together so adding a glyph to a report without widening this test is unlikely.
REPORT_GLYPHS = "──── OUTCOME ── sent-id : abc → carrier@example.com ⚠ ✗"


def _cp1252_stream() -> io.TextIOWrapper:
    """A stdout as a legacy Windows console presents it."""

    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def test_the_report_glyphs_really_do_break_a_cp1252_stream() -> None:
    """Establishes the premise. If this stops failing, the rest of the file proves nothing."""

    stream = _cp1252_stream()

    with pytest.raises(UnicodeEncodeError):
        stream.write(REPORT_GLYPHS)
        stream.flush()


def test_after_configuring_the_report_glyphs_write_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    configure_console_output()
    sys.stdout.write(REPORT_GLYPHS)
    sys.stdout.flush()

    assert stream.encoding == "utf-8"


def test_both_streams_are_reconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON log formatter uses ensure_ascii=False, so stderr is exposed as well."""

    out, err = _cp1252_stream(), _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    configure_console_output()

    assert out.encoding == "utf-8"
    assert err.encoding == "utf-8"


def test_a_stream_without_reconfigure_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest's own capture objects have no ``reconfigure``; neither does a plain StringIO."""

    plain = io.StringIO()
    monkeypatch.setattr(sys, "stdout", plain)

    configure_console_output()  # must not raise

    sys.stdout.write(REPORT_GLYPHS)
    assert "→" in plain.getvalue()


def test_a_closed_stream_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconfiguring is a courtesy; a detached stream is the caller's problem, not a crash."""

    stream = _cp1252_stream()
    stream.close()
    monkeypatch.setattr(sys, "stdout", stream)

    configure_console_output()  # must not raise


def test_it_is_safe_to_call_more_than_once(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    configure_console_output()
    configure_console_output()

    sys.stdout.write(REPORT_GLYPHS)
    assert stream.encoding == "utf-8"
