"""Unit tests for the gate-block retry ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from payment_bot.block_ledger import PRUNE_DAYS, BlockLedger

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def test_counts_and_exhaustion() -> None:
    ledger = BlockLedger()
    assert ledger.blocks("<m1>") == 0
    assert ledger.exhausted("<m1>", 2) is False

    assert ledger.record("<m1>", "gate: coverage", now=NOW) == 1
    assert ledger.exhausted("<m1>", 2) is False  # one strike left

    assert ledger.record("<m1>", "gate: coverage", now=NOW) == 2
    assert ledger.exhausted("<m1>", 2) is True
    assert ledger.dirty is True


def test_zero_limit_disables_the_cap() -> None:
    ledger = BlockLedger()
    for _ in range(5):
        ledger.record("<m1>", "r", now=NOW)
    assert ledger.exhausted("<m1>", 0) is False


def test_each_message_id_has_its_own_budget() -> None:
    """A sender's follow-up is a new message and earns fresh attempts."""

    ledger = BlockLedger()
    ledger.record("<original>", "r", now=NOW)
    ledger.record("<original>", "r", now=NOW)
    assert ledger.exhausted("<original>", 2) is True
    assert ledger.exhausted("<follow-up>", 2) is False


def test_json_roundtrip_preserves_counts() -> None:
    ledger = BlockLedger()
    ledger.record("<m1>", "gate: paperwork_request", now=NOW)
    ledger.record("<m1>", "gate: paperwork_request", now=NOW)

    reloaded = BlockLedger.from_json(ledger.to_json(), now=NOW)
    assert reloaded.blocks("<m1>") == 2
    assert reloaded.dirty is False  # nothing changed at load


def test_stale_entries_are_pruned_at_load() -> None:
    """The intake window makes week-old mail unreachable; its state is dead weight."""

    ledger = BlockLedger()
    ledger.record("<old>", "r", now=NOW - timedelta(days=PRUNE_DAYS + 1))
    ledger.record("<fresh>", "r", now=NOW)

    reloaded = BlockLedger.from_json(ledger.to_json(), now=NOW)
    assert reloaded.blocks("<old>") == 0
    assert reloaded.blocks("<fresh>") == 1
    assert reloaded.dirty is True  # the prune must be written back


def test_a_corrupt_ledger_resets_rather_than_raising() -> None:
    """Losing the ledger costs a few duplicate retries; raising would stop every draft."""

    for raw in ("not json", '["a", "list"]', '{"m": {"count": "x"}}'):
        ledger = BlockLedger.from_json(raw, now=NOW)
        assert ledger.blocks("m") == 0
        assert ledger.exhausted("m", 2) is False


def test_empty_input_is_an_ordinary_start() -> None:
    for raw in (None, ""):
        ledger = BlockLedger.from_json(raw, now=NOW)
        assert ledger.entries == {}
        assert ledger.dirty is False
