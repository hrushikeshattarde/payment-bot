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


# ---------------------------------------------------------------------------
# Length does NOT partition the two systems, and that must not be "fixed" by
# adding a Transport Pro fallback. See payment_bot.domain.routing for the
# measurements; this pins the decision so the fallback cannot be added quietly.
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize("load_id", ["246558", "318354", "316040", "318410", "999998", "111111"])
def test_a_six_digit_id_routes_to_cargotel_even_though_transport_pro_has_it(
    load_id: str,
) -> None:
    """Every id here is a REAL Transport Pro load, verified against the live API.

    All of them settled in 2018-19, while the CargoTel loads sharing two of the numbers were
    delivered 2026-08-10 and still unpaid — so the live question is always CargoTel's, and
    routing there is correct. Six-digit Transport Pro loads are also common rather than rare
    (999998, 111111, 222222, 555555 are all distinct real loads), which is precisely why a
    fallback would surface coincidences: a sender's own reference number resolving to a
    stranger's archived load, whose payment history we would then be answering about.
    """

    assert route_load(load_id).system is System.QUICKBOOKS


@pytest.mark.unit
def test_the_two_systems_never_share_a_length_bucket() -> None:
    """Routing stays a pure function of length — no lookup, no client, no ambiguity.

    If this ever needs to consult a system to decide, that is a different design and the
    disclosure argument in the module docstring has to be re-made first.
    """

    import inspect

    from payment_bot.domain import routing

    source = inspect.getsource(routing.route_load)
    for forbidden in ("tp", "cargotel", "client", "get_load", "await"):
        assert forbidden not in source, f"route_load must not consult a system ({forbidden!r})"
