"""CargoTel payment rules (6-digit loads) — deliberately **not** the Transport Pro rules.

Pure functions, same contract as the rest of :mod:`payment_bot.domain`: no network, no
clock, no config. A separate module rather than branches inside
:mod:`payment_bot.domain.pay_schedule`, because the two systems answer the same carrier
question by different rules and mixing them in one function is how one system's rule
silently starts applying to the other's loads.

Where the rules differ
----------------------

============================  ==============================  =============================
Question                      Transport Pro (7-digit)         CargoTel (6-digit)
============================  ==============================  =============================
Pay date                      ``estimated_payment_date``      ``Invoice Received`` + N, from
                              through the Mon/Thu table       the load's own AP terms
Mon/Thu rule                  applies                         **does not apply**
Anchor date                   the load's estimated date       the date billing recorded the
                                                              carrier's invoice
Term length                   fixed schedule                  per carrier ("Check Net 30",
                                                              "Check 2 Day QuickPay")
Which days count              n/a                             Net: calendar. QuickPay:
                                                              business days
Can we answer at all?         a date exists on the line       an invoice has been received
============================  ==============================  =============================

**Carriers on this path are not paid on Mondays and Thursdays.** Confirmed with the
business. Rolling a CargoTel due date onto a payment day would move a date the payment
terms had already settled, so the computed date is returned exactly as the arithmetic gives
it, whatever weekday it lands on.

**Two kinds of term, and they count different days.** The ``ap_terms`` dropdown is a closed
list of six: ``{2 Day QuickPay, 7 Day QuickPay, Net 30}`` crossed with ``{ACH, Check}``.
Both kinds are anchored on the A/P *Invoice Received* date — confirmed with the business on
2026-08-13, and not the A/R customer invoice date, which is a different cell on the same
page and was a week later on load 298891. What differs is only the counting: Net runs
calendar days, QuickPay runs **business** days. See :func:`add_term_days`. Weekends are
skipped for QuickPay, holidays are not; a stale hard-coded holiday calendar would produce
wrong dates silently, and a date a day early across Thanksgiving is the smaller, knowable
error. The weekend rule matters here in a way it never did for Net: under calendar counting
every Thursday and Friday invoice on a two-day term promised payment at the weekend.

The anchor is the other thing worth stating twice. It is **not** the delivery date. Checked
against QuickBooks, which receives these loads as bills: load 296006 delivered 07/02/2026,
invoice received 07/07/2026, and the bill fell due 08/06/2026 — that is 07/07 + 30, not
07/02 + 30. Anchoring on delivery would have been five days wrong on a single sample.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from payment_bot.models.cargotel import CargoTelCarrier, CargoTelLoad, PaymentTerm


def add_term_days(start: date, term: PaymentTerm) -> date:
    """Apply a payment term to its anchor date.

    Net counts calendar days; QuickPay counts business days. Both count from the same place
    — the A/P *Invoice Received* date, confirmed with the business on 2026-08-13 — so this
    is the only thing that differs between the two, and keeping it in one function is what
    stops a caller applying the wrong kind by reaching for a bare day count.

    Weekends are skipped for QuickPay, holidays are not. A hard-coded federal-holiday
    calendar needs extending every year, and a stale one produces wrong dates silently,
    which is worse than a bounded and understood error: a QuickPay date crossing
    Thanksgiving or July 4th can be a day early.

    The result is still never shifted onto a "payment day" — that rule belongs to Transport
    Pro. A Net 30 date landing on a Sunday is returned as a Sunday, exactly as before.
    """

    if not term.business_days:
        return start + timedelta(days=term.days)

    current = start
    remaining = term.days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Mon-Fri
            remaining -= 1
    return current


class BillingState(StrEnum):
    """How far a load has got toward being payable."""

    #: Invoice received and terms known — a date can be quoted.
    SCHEDULED = "scheduled"
    #: Invoice received but no usable terms, so no date can be computed.
    INVOICED_NO_TERMS = "invoiced_no_terms"
    #: Paperwork is still outstanding. ``missing_documents`` says what.
    AWAITING_PAPERWORK = "awaiting_paperwork"
    #: Every document is on file but billing has not recorded the invoice as received, so
    #: there is still no date to quote.
    #:
    #: Kept distinct from :attr:`AWAITING_PAPERWORK` because the two need opposite replies
    #: and the difference is the carrier's. Here the invoice is already attached and the
    #: delay is entirely ours; telling that sender "we have not received your invoice" is
    #: both false and the kind of thing that generates an angry second email. Observed on
    #: real loads 275957 and 295894 — full document set, no received date.
    AWAITING_BILLING = "awaiting_billing"
    #: An audit hold stops payment regardless of paperwork — always a human's call.
    ON_HOLD = "on_hold"


#: The two documents billing needs before a load can be invoiced. Named here rather than
#: taken from ``REQUIRED_FOR_PAYMENT`` because that tuple is the Transport Pro file-history
#: vocabulary; this path has its own, much smaller one, and conflating them would make a
#: change to one silently move the other.
REQUIRED_DOCUMENTS: tuple[str, ...] = ("BOL 05", "carrier invoice")


class CargoTelPaymentState(BaseModel):
    """What a CargoTel load says about when its carrier gets paid."""

    model_config = ConfigDict(frozen=True)

    state: BillingState
    #: ``Invoice Received`` + N. ``None`` whenever a date cannot be justified — no invoice,
    #: no terms, or a hold. Never a guess.
    expected_payment_date: date | None = None
    #: The anchor the term was counted from, so a reply can explain the date if asked and
    #: the gate has it grounded.
    invoice_received: date | None = None
    #: The N that was applied.
    net_days: int | None = None
    payable: Decimal | None = None
    #: Which of :data:`REQUIRED_DOCUMENTS` the SENDER still has to send — populated only in
    #: :attr:`BillingState.AWAITING_PAPERWORK`, and empty in every other state.
    #:
    #: It used to carry the raw file-list gap in every state, and that is what produced the
    #: A & J and load-301230 replies: an invoice or BOL that reached billing by email is never
    #: in the Print Docs menu, so the gap persists on ``invoiced_no_terms`` and ``scheduled``
    #: too — and the skill keys "name what is in missing_documents and ask the sender to send
    #: it" off this field. The model was reading it correctly; the field was answering a
    #: different question than the one it was being asked. The gap itself is not lost: it goes
    #: into :attr:`note` via :func:`_file_list_gap_note`, which says plainly that it is not
    #: what is holding payment.
    missing_documents: tuple[str, ...] = ()
    #: Plain-language note on anything the reply must not paper over.
    note: str | None = None


def missing_documents(load: CargoTelLoad) -> tuple[str, ...]:
    """Which of the two billing documents are absent.

    Only these two gate invoicing, per the business. Everything else in the Print Docs menu
    is either generated on demand or incidental (inspection images, the carrier agreement),
    and reporting those as "missing" would send carriers chasing paperwork nobody wants.
    """

    missing: list[str] = []
    if not load.has_bol05:
        missing.append("BOL 05")
    if not load.has_carrier_invoice:
        missing.append("carrier invoice")
    return tuple(missing)


def _file_list_gap_note(missing: tuple[str, ...]) -> str:
    """The note for a load billing has invoiced while the file list still shows a gap.

    Shared by :attr:`BillingState.SCHEDULED` and :attr:`BillingState.INVOICED_NO_TERMS`
    because the gap means the same thing in both: the document arrived by some route other
    than the Print Docs menu, and payment is not waiting on it. Says so explicitly rather
    than only forbidding the "complete" claim — a note that lists a missing document without
    saying who owns it is how the ask got written in the first place.
    """

    return (
        f"billing has the invoice, but the file list still shows no {', '.join(missing)} — do "
        "not tell the sender their paperwork is complete, and do NOT ask them for it: it "
        "arrived by another route and is not what is holding this payment"
    )


def resolve_payment(
    load: CargoTelLoad, carrier: CargoTelCarrier | None = None
) -> CargoTelPaymentState:
    """Derive the carrier-facing payment state of a CargoTel load.

    Args:
        load: The parsed load page.
        carrier: The carrier's client record, when it has been fetched. Supplies the payment
            term for loads that carry none of their own — four of nine sample loads were in
            that state, and their carrier records all had terms. Without it those loads are
            answerable only as "being processed, no date".

    The order of the checks is the policy. A hold outranks everything, because a load can
    have every document and a clean term and still not be payable. Paperwork outranks terms,
    because "we are waiting on your invoice" is actionable while "terms are unset" is our
    problem, not the carrier's.
    """

    missing = missing_documents(load)
    payable = load.payable

    if load.pay_hold:
        # No missing_documents: a hold is not answered by paperwork, and naming a document
        # here invites a reply that blames the sender for a hold they cannot clear.
        return CargoTelPaymentState(
            state=BillingState.ON_HOLD,
            invoice_received=load.invoice_received,
            payable=payable,
            note=(
                "this load is on an accounting hold, so no payment date can be given and "
                "the reply must not imply one — it needs a human"
            ),
        )

    if not load.is_invoiced:
        if missing:
            return CargoTelPaymentState(
                state=BillingState.AWAITING_PAPERWORK,
                payable=payable,
                missing_documents=missing,
                note=(
                    "no payment date exists yet because paperwork is outstanding; still "
                    f"needed from the sender: {', '.join(missing)}"
                ),
            )
        return CargoTelPaymentState(
            state=BillingState.AWAITING_BILLING,
            payable=payable,
            note=(
                "every required document is on file but billing has not yet recorded the "
                "invoice, so no payment date can be given. Do NOT ask the sender for "
                "paperwork — they have sent it; say it is with us and being processed"
            ),
        )

    # The load's own terms win; the carrier record is the fallback, not an override. A load
    # set to different terms from its carrier's default was set that way deliberately.
    term = load.payment_term
    if term is None and carrier is not None:
        term = carrier.payment_term
    days = term.days if term else None
    if term is None:
        # Invoice in, but nobody set the terms. Deliberately not defaulted to 30 — see
        # `CargoTelLoad.net_days`.
        note = (
            "the invoice is in but no payment terms are set on this load, so no date "
            "can be computed — state that it is being processed and give no date"
        )
        if missing:
            # The A & J shape (loads 291174/291180/291117): invoice recorded 07/14, no
            # attachment in the menu, no terms. This branch used to pass `missing` through
            # with a note that never mentioned it, so the only thing the model saw about the
            # document was a field that reads as a chore for the sender.
            note = f"{note}. Also: {_file_list_gap_note(missing)}"
        return CargoTelPaymentState(
            state=BillingState.INVOICED_NO_TERMS,
            invoice_received=load.invoice_received,
            payable=payable,
            note=note,
        )

    assert load.invoice_received is not None  # is_invoiced guarantees this
    expected = add_term_days(load.invoice_received, term)

    # A paid-looking load that is still missing paperwork is worth flagging rather than
    # smoothing over: billing accepted an invoice while the file list says a document is
    # absent, which usually means it arrived by another route. The date stands; the reply
    # simply should not also claim the paperwork is complete — nor chase it. Load 301230
    # was scheduled for a date already past, with no BOL 05 in the menu, and the draft told
    # the factor to send one: a document we generate ourselves, for a payment it was not
    # holding, in answer to a question about status.
    return CargoTelPaymentState(
        state=BillingState.SCHEDULED,
        expected_payment_date=expected,
        invoice_received=load.invoice_received,
        net_days=days,
        payable=payable,
        note=_file_list_gap_note(missing) if missing else None,
    )
