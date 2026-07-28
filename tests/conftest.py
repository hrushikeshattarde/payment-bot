"""Shared pytest fixtures.

The ``grounded_ctx`` fixture reproduces the ledger state the agent would have created by
the time it drafts: it runs the load-summary and pay-date tools for load 2462934, so
$150 / $4,500 / $4,650 and 2026-08-20 are all grounded. Gate tests then vary the draft.
"""

from __future__ import annotations

import pytest

from payment_bot.clients import MockTransportProClient
from payment_bot.config import get_settings
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
