"""Unit tests for load routing by ID length (§4.1)."""

from __future__ import annotations

import pytest

from payment_bot.domain import route_load
from payment_bot.models import System


@pytest.mark.unit
@pytest.mark.parametrize(
    ("load_id", "expected_system", "expected_length"),
    [
        ("2462934", System.TRANSPORT_PRO, 7),  # 7-digit → TP
        ("2484035", System.TRANSPORT_PRO, 7),
        ("123456", System.QUICKBOOKS, 6),  # 6-digit → QBO
        ("12345", System.INVALID, 5),  # too short
        ("12345678", System.INVALID, 8),  # too long
        ("", System.INVALID, 0),  # empty
        ("246293a", System.INVALID, 7),  # right length, non-numeric → invalid
        ("24-6293", System.INVALID, 7),  # punctuation is not a digit
    ],
)
def test_route_load(load_id: str, expected_system: System, expected_length: int) -> None:
    result = route_load(load_id)
    assert result.system is expected_system
    assert result.length == expected_length


@pytest.mark.unit
def test_route_load_strips_surrounding_whitespace() -> None:
    result = route_load("  2462934  ")
    assert result.system is System.TRANSPORT_PRO
    assert result.length == 7
