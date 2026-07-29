"""Unit tests for the extract_identifiers tool (§4.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import (
    ExtractIdentifiers,
    ExtractIdentifiersInput,
    ExtractIdentifiersOutput,
)


def _run(ctx: ToolContext, **kw: str) -> ExtractIdentifiersOutput:
    out = ExtractIdentifiers().run(ExtractIdentifiersInput(**kw), ctx)
    assert isinstance(out, ExtractIdentifiersOutput)
    return out


@pytest.mark.unit
def test_extracts_7_and_6_digit_load_ids(ctx: ToolContext) -> None:
    out = _run(ctx, subject="Loads 2462934 and 123456", body="please advise")
    assert out.load_ids == ["2462934", "123456"]


@pytest.mark.unit
def test_ignores_short_and_long_numbers(ctx: ToolContext) -> None:
    # 5-digit, 8-digit, and a 10-digit phone number are not load ids.
    out = _run(ctx, body="ref 12345, po 12345678, call 5551234567")
    assert out.load_ids == []


@pytest.mark.unit
def test_captures_stated_rate_with_load_on_same_line(ctx: ToolContext) -> None:
    out = _run(ctx, body="Load 2499505 rate is $9,300 per the rate con")
    assert out.stated_rates[0].amount == Decimal("9300")
    assert out.stated_rates[0].load_id == "2499505"


@pytest.mark.unit
def test_captures_sender_invoice_number(ctx: ToolContext) -> None:
    out = _run(ctx, body="Our Invoice #4540 covers load 2462934")
    assert "4540" in out.sender_invoice_numbers


@pytest.mark.unit
def test_detects_column_hints(ctx: ToolContext) -> None:
    out = _run(ctx, body="See Reference# and P.O. Number columns")
    assert out.column_hints  # at least one hint detected


@pytest.mark.unit
def test_dedupes_repeated_load_ids(ctx: ToolContext) -> None:
    out = _run(ctx, subject="2462934", body="2462934 again 2462934")
    assert out.load_ids == ["2462934"]
