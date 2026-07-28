"""Model round-trip tests against the authoritative §4.3.0 payload."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from payment_bot.models import TransportProLoad

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "loads" / "2462934.json"


@pytest.fixture(scope="module")
def load() -> TransportProLoad:
    return TransportProLoad.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))


@pytest.mark.unit
def test_amounts_parse_as_exact_decimal(load: TransportProLoad) -> None:
    amounts = [e.amount for e in load.earnings]
    assert amounts == [Decimal("150"), Decimal("4500")]
    assert all(isinstance(a, Decimal) for a in amounts)


@pytest.mark.unit
def test_estimated_dates_parse(load: TransportProLoad) -> None:
    assert load.earnings[0].estimated_payment_date == date(2026, 8, 19)
    assert load.earnings[0].actual_payment_date is None
    assert not load.earnings[0].is_paid


@pytest.mark.unit
def test_pickup_and_delivery_helpers(load: TransportProLoad) -> None:
    assert load.pickup is not None and load.pickup.city == "Spokane"
    assert load.delivery is not None and load.delivery.city == "Lithia Springs"


@pytest.mark.unit
def test_remit_to_self_is_not_factoring(load: TransportProLoad) -> None:
    assert load.account_information is not None
    remit = load.account_information.remit_to
    assert remit is not None
    assert remit.is_factoring is False


@pytest.mark.unit
def test_unknown_fields_are_ignored() -> None:
    load = TransportProLoad.model_validate(
        {"load_id": 1234567, "some_new_api_field": "ignored", "earnings": []}
    )
    assert load.load_id == 1234567


@pytest.mark.unit
def test_load_id_str_helper(load: TransportProLoad) -> None:
    assert load.load_id_str == "2462934"
