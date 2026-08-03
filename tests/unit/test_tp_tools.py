"""Unit tests for the Transport Pro tool wrappers (§4.3) and their grounding."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from payment_bot.clients import LoadFixture, MockTransportProClient
from payment_bot.grounding import GroundingLedger
from payment_bot.models import SettlementEntry, TransportProLoad
from payment_bot.tools.base import ToolContext
from payment_bot.tools.transport_pro import (
    LoadIdInput,
    TpGetDispatchHistory,
    TpGetFileHistory,
    TpGetLoadSummary,
    TpGetNoaFactoring,
    TpGetSettlementEntries,
)


@pytest.mark.unit
def test_load_summary_normalizes_and_grounds(ctx: ToolContext) -> None:
    out = TpGetLoadSummary().run(LoadIdInput(load_id="2462934"), ctx)

    assert out.load_status == "BILLED"
    assert out.total_payout == Decimal("4650")
    assert len(out.earnings) == 2
    assert out.remit_to_self is True
    assert out.is_factoring is False
    assert out.pickup_date == date(2026, 6, 23)
    assert out.delivery_date == date(2026, 6, 29)

    # Grounding: every amount and the estimated date are recorded.
    assert {Decimal("150"), Decimal("4500"), Decimal("4650")} <= ctx.ledger.grounded_amounts
    assert date(2026, 8, 19) in ctx.ledger.grounded_dates


@pytest.mark.unit
def test_dispatch_history_picks_delivered_row(ctx: ToolContext) -> None:
    out = TpGetDispatchHistory().run(LoadIdInput(load_id="2462934"), ctx)

    assert out.delivered_row is not None
    assert out.delivered_row.carrier_name == "Idea Expedited, Inc"
    assert Decimal("4650") in ctx.ledger.grounded_amounts


@pytest.mark.unit
def test_settlement_empty_flag(ctx: ToolContext) -> None:
    out = TpGetSettlementEntries().run(LoadIdInput(load_id="2462934"), ctx)
    assert out.empty is True
    assert out.entries == []


@pytest.mark.unit
def test_settlement_with_entries_grounds_amounts() -> None:
    load = TransportProLoad.model_validate({"load_id": 2400001, "earnings": []})
    client = MockTransportProClient(
        {
            "2400001": LoadFixture(
                load=load,
                settlement=[
                    SettlementEntry(
                        amount=Decimal("2900"),
                        carrier_name="Extra Trans Inc",
                        pay_date=date(2026, 8, 20),
                        check_or_ref="10231",
                        line_type="settlement",
                    )
                ],
            )
        }
    )
    ctx = ToolContext(tp=client, ledger=GroundingLedger(), correlation_id="t")

    out = TpGetSettlementEntries().run(LoadIdInput(load_id="2400001"), ctx)

    assert out.empty is False
    assert Decimal("2900") in ctx.ledger.grounded_amounts
    assert date(2026, 8, 20) in ctx.ledger.grounded_dates


@pytest.mark.unit
def test_file_history_reports_a_complete_load_as_complete(ctx: ToolContext) -> None:
    """The sample load has an invoice and a BOL, so only the rate agreement is missing."""

    out = TpGetFileHistory().run(LoadIdInput(load_id="2462934"), ctx)

    assert out.has_carrier_invoice is True
    assert out.has_bol_or_pod is True
    assert out.has_cancel_confirmation is False
    assert out.document_count == 2
    assert out.missing_documents == ["rate_agreement"]
    assert out.all_required_present is False
    assert {c.category for c in out.on_file} == {"carrier_invoice", "proof_of_delivery"}


@pytest.mark.unit
def test_load_summary_invoice_generated_from_billed_status(ctx: ToolContext) -> None:
    out = TpGetLoadSummary().run(LoadIdInput(load_id="2462934"), ctx)
    assert out.invoice_generated is True  # billing_status == BILLED


@pytest.mark.unit
def test_noa_factoring_read_only(ctx: ToolContext) -> None:
    out = TpGetNoaFactoring().run(LoadIdInput(load_id="2462934"), ctx)
    assert out.noa_on_file is False
    assert out.factoring_company_on_file is None
    assert out.details is not None


@pytest.mark.unit
def test_required_documents_come_from_configuration(ctx: ToolContext) -> None:
    """PAYBOT_REQUIRED_DOCUMENTS decides which documents drafts chase — a config edit,
    and the single seam for the future GET /load/missing_documents source."""

    from payment_bot.config import Settings
    from payment_bot.grounding import GroundingLedger
    from payment_bot.tools.base import ToolContext as Ctx
    from payment_bot.tools.transport_pro import LoadIdInput, TpGetFileHistory

    narrow = Ctx(
        tp=ctx.tp,
        ledger=GroundingLedger(),
        correlation_id="t",
        settings=Settings(required_documents=("carrier_invoice",)),
    )
    out = TpGetFileHistory().run(LoadIdInput(load_id="2462934"), narrow)
    # The sample load is missing its rate agreement, but with only the carrier invoice
    # required, nothing on the narrowed list is missing.
    assert "rate_agreement" not in out.missing_documents


@pytest.mark.unit
def test_an_unknown_required_document_fails_loudly(ctx: ToolContext) -> None:
    from payment_bot.config import Settings
    from payment_bot.errors import ToolError
    from payment_bot.grounding import GroundingLedger
    from payment_bot.tools.base import ToolContext as Ctx
    from payment_bot.tools.transport_pro import LoadIdInput, TpGetFileHistory

    bad = Ctx(
        tp=ctx.tp,
        ledger=GroundingLedger(),
        correlation_id="t",
        settings=Settings(required_documents=("notarized_selfie",)),
    )
    with pytest.raises(ToolError, match="unknown document category"):
        TpGetFileHistory().run(LoadIdInput(load_id="2462934"), bad)
