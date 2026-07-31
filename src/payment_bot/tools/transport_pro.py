"""Transport Pro tools (7-digit loads, §4.3).

Each tool normalises the live payload (§4.3.0) or an auxiliary screen into the reply-
ready shape, and **records the facts it exposes into the grounding ledger** so the
pre-send gate can later verify the draft. Money is summed only via the domain layer.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from payment_bot.domain import compute_carrier_rate
from payment_bot.domain.documents import DocCategory, assess_documents
from payment_bot.models import Deduction, DispatchRow, Earning, SettlementEntry
from payment_bot.tools.base import Tool, ToolContext
from payment_bot.tools.shared import LoadIdStr

_BILLED_STATUSES = frozenset({"billed"})


#: Shared by every Transport Pro read. The description reaches the model as JSON Schema, and
#: an undescribed argument is one the model guesses at — on live mail that cost three to seven
#: failed calls per tool before it hit a working shape.
LOAD_ID_FIELD = Field(
    description=(
        "The 6 or 7 digit load id on its own, digits only — e.g. 2462934. Not an MC number, "
        "not an invoice number, no prefix."
    )
)


class LoadIdInput(BaseModel):
    load_id: LoadIdStr = LOAD_ID_FIELD


# ---------------------------------------------------------------------------
# tp_get_load_summary
# ---------------------------------------------------------------------------
class TpLoadSummaryOutput(BaseModel):
    ok: bool = True
    load_id: str
    load_status: str | None = None
    invoice_generated: bool = False  # derived from billing_status == BILLED
    pickup_date: date | None = None
    delivery_date: date | None = None
    carrier_company: str | None = None
    remit_to_self: bool = True
    is_factoring: bool = False
    total_payout: Decimal  # gross carrier rate = sum(earnings); grounding convenience
    earnings: list[Earning]
    deductions: list[Deduction]


class TpGetLoadSummary(Tool):
    """Load Summary: status, dates, carrier, and the authoritative earning/deduction lines."""

    name = "tp_get_load_summary"
    description = (
        "Return a Transport Pro load's status, pickup/delivery dates, carrier, remit-to, "
        "and every earning line (amount, payment_status, estimated/actual pay date, method, "
        "check number) plus deductions. The earning lines are the source for pay dates."
    )
    input_model = LoadIdInput

    def run(self, params: BaseModel, ctx: ToolContext) -> TpLoadSummaryOutput:
        assert isinstance(params, LoadIdInput)
        load = ctx.tp.get_load(params.load_id)
        load_id = load.load_id_str

        rate = compute_carrier_rate(earnings=load.earnings, deductions=load.deductions)
        pickup_date = _waypoint_date(load.pickup)
        delivery_date = _waypoint_date(load.delivery)
        remit = load.account_information.remit_to if load.account_information else None

        # --- grounding: every value that may appear in the reply -------------
        ctx.ledger.record_amount(rate.gross_rate, self.name, load_id=load_id)
        for earning in load.earnings:
            ctx.ledger.record_amount(earning.amount, self.name, load_id=load_id)
            if earning.estimated_payment_date:
                ctx.ledger.record_date(earning.estimated_payment_date, self.name, load_id=load_id)
            if earning.actual_payment_date:
                ctx.ledger.record_date(earning.actual_payment_date, self.name, load_id=load_id)
            if earning.payment_status:
                ctx.ledger.record_text("status", earning.payment_status, self.name, load_id)
            if earning.payment_method:
                ctx.ledger.record_text("method", earning.payment_method, self.name, load_id)
            if earning.check_number:
                ctx.ledger.record_text("check_ref", earning.check_number, self.name, load_id)
        for deduction in load.deductions:
            ctx.ledger.record_amount(deduction.amount, self.name, load_id=load_id)
        if pickup_date:
            ctx.ledger.record_date(pickup_date, self.name, load_id=load_id)
        if delivery_date:
            ctx.ledger.record_date(delivery_date, self.name, load_id=load_id)
        if load.billing_status:
            ctx.ledger.record_text("status", load.billing_status, self.name, load_id)

        return TpLoadSummaryOutput(
            load_id=load_id,
            load_status=load.billing_status,
            invoice_generated=(load.billing_status or "").strip().lower() in _BILLED_STATUSES,
            pickup_date=pickup_date,
            delivery_date=delivery_date,
            carrier_company=(
                load.account_information.company_name if load.account_information else None
            ),
            remit_to_self=not (remit.is_factoring if remit else False),
            is_factoring=remit.is_factoring if remit else False,
            total_payout=rate.gross_rate,
            earnings=list(load.earnings),
            deductions=list(load.deductions),
        )


# ---------------------------------------------------------------------------
# tp_get_dispatch_history
# ---------------------------------------------------------------------------
class TpDispatchHistoryOutput(BaseModel):
    ok: bool = True
    rows: list[DispatchRow]
    delivered_row: DispatchRow | None = None


class TpGetDispatchHistory(Tool):
    """Dispatch history — use the Delivered row only for carrier + rate; ignore canceled."""

    name = "tp_get_dispatch_history"
    description = (
        "Return dispatch rows for a load. Use only the Delivered row for carrier and rate; "
        "canceled rows must be ignored."
    )
    input_model = LoadIdInput

    def run(self, params: BaseModel, ctx: ToolContext) -> TpDispatchHistoryOutput:
        assert isinstance(params, LoadIdInput)
        rows = ctx.tp.get_dispatch_history(params.load_id)
        delivered = next((r for r in rows if r.is_delivered and not r.is_canceled), None)
        if delivered and delivered.freight_bill is not None:
            ctx.ledger.record_amount(delivered.freight_bill, self.name, load_id=params.load_id)
        return TpDispatchHistoryOutput(rows=list(rows), delivered_row=delivered)


# ---------------------------------------------------------------------------
# tp_get_settlement_entries
# ---------------------------------------------------------------------------
class TpSettlementEntriesOutput(BaseModel):
    ok: bool = True
    entries: list[SettlementEntry]
    empty: bool


class TpGetSettlementEntries(Tool):
    """Settlement entries (advances, fees, claims, short pays, payments)."""

    name = "tp_get_settlement_entries"
    description = (
        "Return settlement entries for a load (advances, fees, claims, short pays, "
        "payments). Empty means the load has not settled yet."
    )
    input_model = LoadIdInput

    def run(self, params: BaseModel, ctx: ToolContext) -> TpSettlementEntriesOutput:
        assert isinstance(params, LoadIdInput)
        entries = ctx.tp.get_settlement_entries(params.load_id)
        for entry in entries:
            ctx.ledger.record_amount(entry.amount, self.name, load_id=params.load_id)
            if entry.pay_date:
                ctx.ledger.record_date(entry.pay_date, self.name, load_id=params.load_id)
            if entry.check_or_ref:
                ctx.ledger.record_text("check_ref", entry.check_or_ref, self.name, params.load_id)
        return TpSettlementEntriesOutput(entries=list(entries), empty=not entries)


# ---------------------------------------------------------------------------
# tp_get_file_history
# ---------------------------------------------------------------------------
class CategoryCount(BaseModel):
    category: str
    count: int
    latest: date | None = None


class TpFileHistoryOutput(BaseModel):
    ok: bool = True
    load_id: str
    document_count: int = 0
    #: The question this tool exists to answer: required paperwork not on file.
    missing_documents: list[str] = Field(default_factory=list)
    all_required_present: bool = True
    #: One row per document category, so a load with four rate agreements reads as one line.
    on_file: list[CategoryCount] = Field(default_factory=list)
    has_carrier_invoice: bool = False
    has_bol_or_pod: bool = False
    has_rate_agreement: bool = False
    has_cancel_confirmation: bool = False


class TpGetFileHistory(Tool):
    """File history, reduced to *what is missing* (§4.3).

    A busy load carries a dozen rows — several copies of the rate agreement, two invoices,
    a billing packet. Handing all of that to the model invites it to eyeball the list and
    guess. Instead this classifies each file by its ``fileTypeId`` and returns the answer
    directly: which required documents are absent.
    """

    name = "tp_get_file_history"
    description = (
        "Return which required documents are MISSING for a load (carrier invoice, "
        "proof of delivery/BOL, rate agreement), plus a per-category count of what is on "
        "file and whether a CANCEL LOAD confirmation exists. Read `missing_documents` — "
        "do not infer it yourself. A cancel confirmation escalates."
    )
    input_model = LoadIdInput

    def run(self, params: BaseModel, ctx: ToolContext) -> TpFileHistoryOutput:
        assert isinstance(params, LoadIdInput)
        load_id = params.load_id.strip()
        docs = ctx.tp.get_file_history(load_id)

        status, _classified = assess_documents(
            ((d.file_type, d.file_type_id, d.upload_date or d.index_date, d.comments) for d in docs),
            load_id=load_id,
        )
        present = set(status.present)

        # Ground the document categories so the reply may name them (§5).
        for category in status.present:
            ctx.ledger.record_text("document", category.value, self.name, load_id)

        return TpFileHistoryOutput(
            load_id=load_id,
            document_count=status.document_count,
            missing_documents=[c.value for c in status.missing],
            all_required_present=status.is_complete,
            on_file=[
                CategoryCount(category=s.category.value, count=s.count, latest=s.latest)
                for s in status.by_category
            ],
            has_carrier_invoice=DocCategory.CARRIER_INVOICE in present,
            has_bol_or_pod=DocCategory.PROOF_OF_DELIVERY in present,
            has_rate_agreement=DocCategory.RATE_AGREEMENT in present,
            has_cancel_confirmation=status.has_cancel_confirmation,
        )


# ---------------------------------------------------------------------------
# tp_get_noa_factoring
# ---------------------------------------------------------------------------
class TpNoaFactoringOutput(BaseModel):
    ok: bool = True
    noa_on_file: bool = False
    factoring_company_on_file: str | None = None
    details: str | None = None


class TpGetNoaFactoring(Tool):
    """Read-only NOA / factoring status for a load (§4.3).

    Reporting is read-only. Any request to *add/attach/update* an NOA or change factoring
    setup is caught upstream by ``detect_sensitive_change`` and escalates — this tool never
    modifies anything.
    """

    name = "tp_get_noa_factoring"
    description = (
        "Return read-only NOA / factoring status for a load: whether a notice of assignment "
        "is on file and the factoring company name, if any. Read-only."
    )
    input_model = LoadIdInput

    def run(self, params: BaseModel, ctx: ToolContext) -> TpNoaFactoringOutput:
        assert isinstance(params, LoadIdInput)
        noa = ctx.tp.get_noa_factoring(params.load_id)
        if noa.factoring_company_on_file:
            ctx.ledger.record_text(
                "factoring", noa.factoring_company_on_file, self.name, load_id=params.load_id
            )
        return TpNoaFactoringOutput(
            noa_on_file=noa.noa_on_file,
            factoring_company_on_file=noa.factoring_company_on_file,
            details=noa.details,
        )


def _waypoint_date(waypoint: object) -> date | None:
    date_obj = getattr(waypoint, "date", None)
    timestamp = getattr(date_obj, "timestamp", None)
    return timestamp.date() if timestamp is not None else None
