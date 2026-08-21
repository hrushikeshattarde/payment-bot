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
from payment_bot.errors import ClientError, ToolError
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
class CarrierPayable(BaseModel):
    """One carrier's payable on the load: their lines, their rate, their remit-to.

    A load with one carrier has one of these and it repeats the top-level fields. A load
    split across legs or re-dispatched has one per carrier, and then the top-level fields
    describe only the FIRST of them — every figure a reply gives has to be taken from the
    entry whose ``carrier_company`` the reply is naming.
    """

    carrier_company: str | None = None
    #: The factor this carrier's payment is remitted to, or ``None`` when it pays them direct.
    factoring_company: str | None = None
    #: The payee as the Settlement Entries screen writes it — "Parasource Inc c/o England
    #: Carrier Services". Quote this when the question is who was paid.
    pay_to: str | None = None
    remit_to_self: bool = True
    #: Gross carrier rate for THIS carrier = sum of its earnings. Never a load-wide total.
    total_payout: Decimal
    earnings: list[Earning] = Field(default_factory=list)
    deductions: list[Deduction] = Field(default_factory=list)
    pickup_date: date | None = None
    delivery_date: date | None = None


class TpLoadSummaryOutput(BaseModel):
    ok: bool = True
    load_id: str
    load_status: str | None = None
    invoice_generated: bool = False  # derived from billing_status == BILLED
    pickup_date: date | None = None
    delivery_date: date | None = None
    carrier_company: str | None = None
    #: The factor this carrier's payment is remitted to, or ``None`` when it pays them direct.
    factoring_company: str | None = None
    #: The payee as the Settlement Entries screen writes it — "Parasource Inc c/o England
    #: Carrier Services", or just the carrier when no factor collects. Copy it verbatim when
    #: the reply has to say who was paid; it is the one place the carrier and the factor
    #: collecting for them are already correctly paired.
    pay_to: str | None = None
    remit_to_self: bool = True
    is_factoring: bool = False
    total_payout: Decimal  # gross carrier rate = sum(earnings); grounding convenience
    earnings: list[Earning]
    deductions: list[Deduction]
    #: One entry per carrier — **populated only when this load has more than one**, so an
    #: ordinary load does not carry its earnings twice. Empty means the fields above are the
    #: whole load.
    carriers: list[CarrierPayable] = Field(default_factory=list)
    #: True when the load has several carriers AFTER scoping. Then every field above describes
    #: the FIRST of them, and answering from those fields reports one carrier's money as
    #: though it were another's — read ``carriers`` and name whose lines you are giving.
    multiple_carriers: bool = False


class TpGetLoadSummary(Tool):
    """Load Summary: status, dates, and each carrier's authoritative earning/deduction lines."""

    name = "tp_get_load_summary"
    description = (
        "Return a Transport Pro load's status, pickup/delivery dates, and one entry per "
        "carrier, remit-to and pay-to, and every earning line (amount, payment_status, "
        "estimated/actual pay date, method, check number) plus deductions. The earning lines "
        "are the source for pay dates. A load run by several carriers has a payable each: "
        "when multiple_carriers is true the top-level fields describe only the first, and "
        "`carriers` holds one entry per carrier."
    )
    input_model = LoadIdInput

    def run(self, params: BaseModel, ctx: ToolContext) -> TpLoadSummaryOutput:
        """Summarise every payable on the load that this run is allowed to read.

        **A load is not one carrier.** ``payment_information`` returns a payable per carrier,
        and this tool read only the first for as long as it existed. Live on 2436437, which
        has three: the bot's entire picture of the load was FOX CARRIERS' $905, so Parasource's
        $5,000 line haul paid 06/25 and their $230 lumper on 08/12 could not be reported —
        they had never been read, and no amount of prompting could have recovered them.

        Scoped by ``ctx.disclosable_carriers``: when the pipeline has established which
        carrier the sender is, the other carriers' payables are not returned and — this is the
        part that matters — their amounts and dates never enter the grounding ledger, so the
        pre-send gate refuses them as ungrounded. Unset, every payable is returned, which is
        both the old behaviour and the right answer for a single-carrier load.
        """

        assert isinstance(params, LoadIdInput)
        payables = ctx.tp.get_load_payables(params.load_id)
        load_id = payables[0].load_id_str

        in_scope = {name.strip().casefold() for name in ctx.carriers_in_scope(params.load_id)}
        if in_scope:
            scoped = [
                p
                for p in payables
                if p.carrier_company and p.carrier_company.strip().casefold() in in_scope
            ]
            # Never narrow to nothing. A scope that matches no payable means the sender was
            # authorized by something other than a payable — a dispatch contact on a leg that
            # never settled — and withholding the whole load would answer their question with
            # silence. Fall back to the load as a whole rather than to an empty summary.
            payables = scoped or payables

        primary = payables[0]
        carriers: list[CarrierPayable] = []
        for payable in payables:
            rate = compute_carrier_rate(
                earnings=payable.earnings, deductions=payable.deductions
            )
            pickup = _waypoint_date(payable.pickup)
            delivery = _waypoint_date(payable.delivery)

            # --- grounding: every value that may appear in the reply ---------
            ctx.ledger.record_amount(rate.gross_rate, self.name, load_id=load_id)
            for earning in payable.earnings:
                ctx.ledger.record_amount(earning.amount, self.name, load_id=load_id)
                if earning.estimated_payment_date:
                    ctx.ledger.record_date(
                        earning.estimated_payment_date, self.name, load_id=load_id
                    )
                if earning.actual_payment_date:
                    ctx.ledger.record_date(
                        earning.actual_payment_date, self.name, load_id=load_id
                    )
                if earning.payment_status:
                    ctx.ledger.record_text("status", earning.payment_status, self.name, load_id)
                if earning.payment_method:
                    ctx.ledger.record_text("method", earning.payment_method, self.name, load_id)
                if earning.check_number:
                    ctx.ledger.record_text("check_ref", earning.check_number, self.name, load_id)
            for deduction in payable.deductions:
                ctx.ledger.record_amount(deduction.amount, self.name, load_id=load_id)
            if pickup:
                ctx.ledger.record_date(pickup, self.name, load_id=load_id)
            if delivery:
                ctx.ledger.record_date(delivery, self.name, load_id=load_id)
            if payable.carrier_company:
                ctx.ledger.record_text(
                    "carrier", payable.carrier_company, self.name, load_id
                )
            if payable.pay_to:
                ctx.ledger.record_text("carrier", payable.pay_to, self.name, load_id)

            carriers.append(
                CarrierPayable(
                    carrier_company=payable.carrier_company,
                    factoring_company=payable.factoring_company,
                    pay_to=payable.pay_to,
                    remit_to_self=payable.factoring_company is None,
                    total_payout=rate.gross_rate,
                    earnings=list(payable.earnings),
                    deductions=list(payable.deductions),
                    pickup_date=pickup,
                    delivery_date=delivery,
                )
            )

        if primary.billing_status:
            ctx.ledger.record_text("status", primary.billing_status, self.name, load_id)

        head = carriers[0]
        return TpLoadSummaryOutput(
            load_id=load_id,
            load_status=primary.billing_status,
            invoice_generated=(primary.billing_status or "").strip().lower() in _BILLED_STATUSES,
            pickup_date=head.pickup_date,
            delivery_date=head.delivery_date,
            carrier_company=head.carrier_company,
            factoring_company=head.factoring_company,
            pay_to=head.pay_to,
            remit_to_self=head.remit_to_self,
            is_factoring=not head.remit_to_self,
            total_payout=head.total_payout,
            earnings=list(head.earnings),
            deductions=list(head.deductions),
            # Repeating one carrier's lines under both shapes costs tokens on every load and
            # invites the model to wonder which is authoritative. The breakdown appears only
            # where it answers something the fields above cannot.
            carriers=carriers if len(carriers) > 1 else [],
            multiple_carriers=len(carriers) > 1,
        )


# ---------------------------------------------------------------------------
# tp_get_dispatch_history
# ---------------------------------------------------------------------------
class TpDispatchHistoryOutput(BaseModel):
    ok: bool = True
    rows: list[DispatchRow]
    delivered_row: DispatchRow | None = None


class TpGetDispatchHistory(Tool):
    """Dispatch history — use the Delivered row only for carrier + rate; ignore canceled.

    Carries NO dates a reply may quote. See :class:`~payment_bot.models.DispatchRow`: the
    screen's Pickup/Delivery cells stack a place above a date and only the place is parsed,
    while ``last_updated`` is a record stamp. The delivery date comes from
    ``tp_get_load_summary``, which is also the only tool that grounds it.
    """

    name = "tp_get_dispatch_history"
    description = (
        "Return dispatch rows for a load: carrier, MC, dispatch status and freight bill. Use "
        "only the Delivered row for carrier and rate; canceled rows must be ignored. This "
        "tool returns NO usable date. `pickup` and `delivery` are PLACES ('LAREDO, TX'), not "
        "dates, and `last_updated` is when the record was last edited — never a delivery, "
        "dispatch or payment date. For the delivery date call `tp_get_load_summary`."
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
    #: True when the rows span more than one pay-to. Each row's ``carrier_name`` says whose it
    #: is — "Parasource Inc c/o England Carrier Services" — and a reply that reports a figure
    #: without naming that party is attributing one carrier's payment to whoever asked.
    multiple_payees: bool = False


class TpGetSettlementEntries(Tool):
    """Settlement entries (advances, fees, claims, short pays, payments)."""

    name = "tp_get_settlement_entries"
    description = (
        "Return settlement entries for a load (advances, fees, claims, short pays, "
        "payments), each with the pay-to it was settled against. Empty means the load has "
        "not settled yet."
    )
    input_model = LoadIdInput

    def run(self, params: BaseModel, ctx: ToolContext) -> TpSettlementEntriesOutput:
        """Every settled line on the load, with who was paid on each.

        Rows come from all of the load's payables, so a load settled per leg reports every
        leg. This is what was missing from the draft on 2436437: Parasource's $5,000 line haul
        paid 06/25 and their $230 lumper on 08/12 are rows on the third payable, and the
        client only ever read the first.

        Scoped by ``ctx.disclosable_carriers`` for the same reason ``tp_get_load_summary`` is:
        an out-of-scope carrier's settlement is not this sender's to see, and keeping its
        amounts out of the ledger lets the gate enforce that rather than merely asking.
        """

        assert isinstance(params, LoadIdInput)
        entries = list(ctx.tp.get_settlement_entries(params.load_id))

        in_scope = {name.strip().casefold() for name in ctx.carriers_in_scope(params.load_id)}
        if in_scope:
            scoped = [
                e
                for e in entries
                if e.paid_carrier and e.paid_carrier.strip().casefold() in in_scope
            ]
            # Same fallback as the summary, and for the same reason: a scope that matches no
            # row must not turn "here is your settlement" into "there is none".
            entries = scoped or entries

        for entry in entries:
            ctx.ledger.record_amount(entry.amount, self.name, load_id=params.load_id)
            if entry.pay_date:
                ctx.ledger.record_date(entry.pay_date, self.name, load_id=params.load_id)
            if entry.check_or_ref:
                ctx.ledger.record_text("check_ref", entry.check_or_ref, self.name, params.load_id)
            if entry.carrier_name:
                ctx.ledger.record_text("carrier", entry.carrier_name, self.name, params.load_id)
        payees = {e.carrier_name.strip().casefold() for e in entries if e.carrier_name}
        return TpSettlementEntriesOutput(
            entries=entries, empty=not entries, multiple_payees=len(payees) > 1
        )


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
    #: Which document carried the phrase, so a reviewer can actually find it. The phrase lives
    #: in a comment on an ordinary document type, so the file list alone never reveals it.
    cancel_confirmation_sources: list[str] = Field(default_factory=list)
    #: True when a cancel confirmation exists BUT the load went on to deliver under a carrier
    #: whose dispatch is not canceled — i.e. the cancellation belongs to a superseded leg.
    #:
    #: ``has_cancel_confirmation`` is load-level and the document names only the load, so on a
    #: re-dispatched load it cannot say whose cancellation it was. Live on 2534597: Nesh Trans
    #: canceled, N S Express delivered, and a factor asking about N S Express was told the load
    #: was under review for a cancellation belonging to the carrier that never ran it.
    cancel_confirmation_superseded: bool = False
    #: Carriers whose dispatch is canceled, and the carrier that delivered.
    canceled_carriers: list[str] = Field(default_factory=list)
    delivered_carrier: str | None = None


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
        "do not infer it yourself. A driver_upload row is a driver-app photo, not the "
        "signed BOL: it never satisfies proof of delivery. A cancel confirmation escalates, "
        "UNLESS `cancel_confirmation_superseded` is true: the load was then re-dispatched and "
        "delivered, and the cancellation belongs to `canceled_carriers` — a different carrier "
        "from `delivered_carrier`, whose leg ran normally."
    )
    input_model = LoadIdInput

    def run(self, params: BaseModel, ctx: ToolContext) -> TpFileHistoryOutput:
        assert isinstance(params, LoadIdInput)
        load_id = params.load_id.strip()
        docs = ctx.tp.get_file_history(load_id)

        # The required set comes from configuration (PAYBOT_REQUIRED_DOCUMENTS), so which
        # documents drafts chase is a config edit. This call is also the seam for the
        # authoritative missing-documents source: when the GET /load/missing_documents
        # request shape is settled (docs/MISSING_DOCUMENTS_CACHE.md), swap the derivation
        # here — the output shape, and therefore every draft, stays identical.
        try:
            required = tuple(DocCategory(v) for v in ctx.settings.required_documents)
        except ValueError as exc:
            raise ToolError(
                f"PAYBOT_REQUIRED_DOCUMENTS contains an unknown document category: {exc}. "
                f"Valid values: {[c.value for c in DocCategory]}"
            ) from exc
        status, _classified = assess_documents(
            ((d.file_type, d.file_type_id, d.upload_date or d.index_date, d.comments) for d in docs),
            load_id=load_id,
            required=required,
        )
        present = set(status.present)

        # Ground the document categories so the reply may name them (§5).
        for category in status.present:
            ctx.ledger.record_text("document", category.value, self.name, load_id)

        # A cancel confirmation names only the load, so on its own it cannot say WHOSE
        # dispatch was canceled. The dispatch rows can, and this tool is where both are
        # reachable — the model must not be left to join them, because it would have to call
        # a second tool it has no reason to call and then reason about supersession.
        superseded = False
        canceled_carriers: list[str] = []
        delivered_carrier: str | None = None
        if status.has_cancel_confirmation:
            rows: list[DispatchRow] = []
            try:
                rows = list(ctx.tp.get_dispatch_history(load_id))
            except (ClientError, ToolError):
                # Unreadable dispatch history must not fail the document read, and must not
                # claim supersession either: `superseded` stays False, so the hold stands.
                rows = []
            canceled_carriers = [r.carrier_name for r in rows if r.is_canceled]
            delivered = next((r for r in rows if r.is_delivered and not r.is_canceled), None)
            delivered_carrier = delivered.carrier_name if delivered else None
            # Delivered under a carrier whose own dispatch is not canceled means the load was
            # re-dispatched and ran. Requiring a canceled row too keeps this from firing on a
            # load whose cancel document is the only sign of a cancellation nobody recorded.
            superseded = delivered is not None and bool(canceled_carriers)

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
            cancel_confirmation_sources=list(status.cancel_confirmation_sources),
            cancel_confirmation_superseded=superseded,
            canceled_carriers=canceled_carriers,
            delivered_carrier=delivered_carrier,
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
        """Where this load's payment goes, for the carrier the sender is entitled to.

        Scoped by ``ctx.disclosable_carriers`` like the summary and settlement reads, and here
        the scoping fixes a wrong answer as much as an over-broad one. A load can be factored
        per leg — 2436437 remits FOX CARRIERS' leg to eCapital, Alina's to RTS and
        Parasource's to England Carrier Services — and the single value was the first
        payable's, so Parasource asking where their payment goes would have been told
        eCapital: another carrier's factor, reported as theirs.
        """

        assert isinstance(params, LoadIdInput)
        noa = ctx.tp.get_noa_factoring(params.load_id)

        in_scope = {name.strip().casefold() for name in ctx.carriers_in_scope(params.load_id)}
        scoped = [
            (carrier, factor)
            for carrier, factor in noa.by_carrier
            if not in_scope or carrier.strip().casefold() in in_scope
        ]
        company: str | None = noa.factoring_company_on_file
        details: str | None = noa.details
        # `by_carrier` is empty for a fixture that predates it, and a scope may name a carrier
        # with no payable — those keep the client's own summary rather than falling silent.
        if noa.by_carrier and scoped:
            factors = list(dict.fromkeys(f for _, f in scoped if f))
            company = "; ".join(factors) if factors else None
            parts = [
                f"remit-to is {f} (not self)"
                if len(scoped) == 1
                else f"{c}: remit-to is {f} (not self)"
                for c, f in scoped
                if f
            ]
            if noa.document_evidence:
                parts.append(noa.document_evidence)
            details = "; ".join(parts) or (
                "Remit-to self; no factoring document on file for this load."
            )

        if company:
            ctx.ledger.record_text("factoring", company, self.name, load_id=params.load_id)
        return TpNoaFactoringOutput(
            noa_on_file=noa.noa_on_file,
            factoring_company_on_file=company,
            details=details,
        )


def _waypoint_date(waypoint: object) -> date | None:
    date_obj = getattr(waypoint, "date", None)
    timestamp = getattr(date_obj, "timestamp", None)
    return timestamp.date() if timestamp is not None else None
