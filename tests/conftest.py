"""Shared pytest fixtures.

The ``grounded_ctx`` fixture reproduces the ledger state the agent would have created by
the time it drafts: it runs the load-summary and pay-date tools for load 2462934, so
$150 / $4,500 / $4,650 and 2026-08-20 are all grounded. Gate tests then vary the draft.

``isolate_settings`` is autouse and matters just as much: it keeps the suite from reading a
developer's real ``.env``. Without it, a filled-in ``.env`` makes ``Settings()`` fully
configured, and tests that expect an unconfigured system instead reach for the *live*
mailbox — running ``pytest`` would attempt a real IMAP login. Tests must never depend on,
or touch, whatever credentials happen to be on the machine.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from payment_bot.clients import MockTransportProClient
from payment_bot.config import Settings, get_settings
from payment_bot.grounding import GroundingLedger
from payment_bot.models import InboundEmail
from payment_bot.sample_data import (
    sample_payment_status_email,
    sample_transport_pro_client,
)
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import (
    ComputeScheduledPayDate,
    ComputeScheduledPayDateInput,
)
from payment_bot.tools.transport_pro import LoadIdInput, TpGetLoadSummary


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make every test hermetic with respect to configuration.

    Drops any ``PAYBOT_*`` environment variables and stops ``Settings`` from loading a
    ``.env`` file, so the suite behaves identically on a machine with real credentials and
    on a fresh checkout. Also clears the cached settings singleton on both sides.
    """

    for key in [k for k in os.environ if k.startswith("PAYBOT_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)  # type: ignore[typeddict-item]

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tp_client() -> MockTransportProClient:
    return sample_transport_pro_client()


@pytest.fixture
def ledger() -> GroundingLedger:
    return GroundingLedger()


@pytest.fixture
def ctx(tp_client: MockTransportProClient, ledger: GroundingLedger) -> ToolContext:
    return ToolContext(
        tp=tp_client,
        ledger=ledger,
        correlation_id="test-corr",
        settings=get_settings(),
    )


@pytest.fixture
def sample_email() -> InboundEmail:
    return sample_payment_status_email()


@pytest.fixture
def grounded_ctx(ctx: ToolContext) -> ToolContext:
    """A context whose ledger has been populated for load 2462934."""

    TpGetLoadSummary().run(LoadIdInput(load_id="2462934"), ctx)
    ComputeScheduledPayDate().run(
        ComputeScheduledPayDateInput(estimated_payment_date="2026-08-19", load_id="2462934"),
        ctx,
    )
    return ctx
