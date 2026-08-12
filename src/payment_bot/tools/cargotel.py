"""CargoTel tools (6-digit loads).

One tool, because CargoTel is one page. Everything a payment-status reply needs — status,
documents, terms, the invoice-received date and the computed payment date — comes off
``loadmaint.mcgi`` plus the carrier record it points at, so splitting it into several tools
would mean several round trips for facts that arrive together.

Like the Transport Pro tools, this **records every fact it exposes into the grounding
ledger**, so the pre-send gate can verify the draft afterwards. The date in particular:
it is computed by :mod:`payment_bot.domain.cargotel`, and a computed value that never
reaches the ledger is a value the gate will block.

Rate verification is not implemented here. A CargoTel load has a single payable amount and
no line-item breakdown on this page, so there is nothing to itemise into gross, deductions
and net the way ``compute_carrier_rate`` does — the charge detail lives on the Accounting
tab, which is not wired. Advertising a rate tool that could only restate one number would
invite exactly the vague reply the rate skill exists to avoid.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from payment_bot.clients.cargotel import CargoTelClient
from payment_bot.domain.cargotel import BillingState, resolve_payment
from payment_bot.errors import ToolError
from payment_bot.models.cargotel import CargoTelCarrier
from payment_bot.tools.base import Tool, ToolContext
from payment_bot.tools.shared import LoadIdStr

CGT_LOAD_ID_FIELD = Field(
    description=(
        "The 6 digit load id on its own, digits only - e.g. 296006. Not the invoice number, "
        "no prefix."
    )
)


class CgtLoadIdInput(BaseModel):
    load_id: LoadIdStr = CGT_LOAD_ID_FIELD


class CgtLoadStatusOutput(BaseModel):
    ok: bool = True
    load_id: str
    carrier_company: str | None = None
    #: Delivered / In-Route / Cancelled, and the date on the banner.
    load_status: str | None = None
    delivered_date: date | None = None

    #: open / awaiting paperwork / awaiting billing / scheduled / on hold. Read this rather
    #: than inferring a state from the other fields.
    billing_state: BillingState
    #: The date to quote. Present only when a term could be applied to a received invoice —
    #: never estimated. Report it exactly; do not move it to a Monday or Thursday.
    expected_payment_date: date | None = None
    #: What the term was counted from, so the reply can explain the date if asked.
    invoice_received: date | None = None
    payment_terms: str | None = None
    amount: Decimal | None = None
    invoice_number: str | None = None

    #: Required documents not on file: "BOL 05" and/or "carrier invoice".
    missing_documents: list[str] = Field(default_factory=list)
    has_bol05: bool = False
    carrier_invoice_count: int = 0
    #: Anything the reply must not paper over — a hold, a settled-looking load with no
    #: payment record, or paperwork that is with us rather than with the sender. When
    #: present, follow it.
    note: str | None = None


class CgtGetLoadStatus(Tool):
    """The whole billing picture for one 6-digit load."""

    name = "cgt_get_load_status"
    description = (
        "Return a 6-digit load's billing state, the expected payment date when one can be "
        "given, the amount, the payment terms, and which required documents (BOL 05, "
        "carrier invoice) are missing. Read `billing_state` and `note` rather than inferring "
        "anything. The date is already final - never adjust it to a Monday or Thursday."
    )
    input_model = CgtLoadIdInput

    def run(self, params: BaseModel, ctx: ToolContext) -> CgtLoadStatusOutput:
        assert isinstance(params, CgtLoadIdInput)
        client = _client(ctx)
        load = client.get_load(params.load_id)

        # The carrier record supplies the payment term when the load carries none — four of
        # nine real loads were in that state. Its absence must not fail the read: the load
        # still has a status worth reporting.
        carrier: CargoTelCarrier | None = None
        if load.carrier_client_id:
            try:
                carrier = client.get_carrier(load.carrier_client_id)
            except Exception:
                carrier = None

        state = resolve_payment(load, carrier)
        load_id = load.load_id

        # --- grounding: every value the reply may quote ----------------------
        if load.payable is not None:
            ctx.ledger.record_amount(load.payable, self.name, load_id=load_id)
        if load.status_date:
            ctx.ledger.record_date(load.status_date, self.name, load_id=load_id)
        if state.invoice_received:
            ctx.ledger.record_date(state.invoice_received, self.name, load_id=load_id)
        if state.expected_payment_date:
            ctx.ledger.record_date(
                state.expected_payment_date,
                self.name,
                load_id=load_id,
                kind="scheduled_pay_date",
            )
        ctx.ledger.record_text("status", state.state.value, self.name, load_id)
        carrier_name = (carrier.name if carrier else None) or load.carrier_name
        if carrier_name:
            ctx.ledger.record_text("carrier", carrier_name, self.name, load_id)
        if load.ap_invoice_number:
            ctx.ledger.record_text("check_ref", load.ap_invoice_number, self.name, load_id)

        return CgtLoadStatusOutput(
            load_id=load_id,
            carrier_company=carrier_name,
            load_status=load.status,
            delivered_date=load.status_date,
            billing_state=state.state,
            expected_payment_date=state.expected_payment_date,
            invoice_received=state.invoice_received,
            payment_terms=load.ap_terms or (carrier.ap_terms if carrier else None),
            amount=load.payable,
            invoice_number=load.ap_invoice_number,
            missing_documents=list(state.missing_documents),
            has_bol05=load.has_bol05,
            carrier_invoice_count=load.carrier_invoice_count,
            note=state.note,
        )


def _client(ctx: ToolContext) -> CargoTelClient:
    """The CargoTel client for this run, or a terminal error.

    Reaching a ``cgt_*`` tool without one is a wiring mistake, not a data problem, so the
    message says so instead of looking like a missing load — and tells the model not to
    retry, since no retry can help.
    """

    if ctx.cargotel is None:
        raise ToolError(
            "CargoTel is not wired for this run, so 6-digit loads cannot be looked up. "
            "Do NOT retry this tool."
        )
    return ctx.cargotel
