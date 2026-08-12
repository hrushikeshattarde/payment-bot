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
Term length                   fixed schedule                  per carrier ("Check Net 30")
Can we answer at all?         a date exists on the line       an invoice has been received
============================  ==============================  =============================

**Carriers on this path are not paid on Mondays and Thursdays.** Confirmed with the
business. Rolling a CargoTel due date onto a payment day would move a date the payment
terms had already settled, so the computed date is returned exactly as the arithmetic gives
it, whatever weekday it lands on.

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

from payment_bot.models.cargotel import CargoTelCarrier, CargoTelLoad


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
    #: Which of :data:`REQUIRED_DOCUMENTS` are not yet on file.
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
        return CargoTelPaymentState(
            state=BillingState.ON_HOLD,
            invoice_received=load.invoice_received,
            payable=payable,
            missing_documents=missing,
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
    days = load.net_days
    if days is None and carrier is not None:
        days = carrier.net_days
    if days is None:
        # Invoice in, but nobody set the terms. Deliberately not defaulted to 30 — see
        # `CargoTelLoad.net_days`.
        return CargoTelPaymentState(
            state=BillingState.INVOICED_NO_TERMS,
            invoice_received=load.invoice_received,
            payable=payable,
            missing_documents=missing,
            note=(
                "the invoice is in but no payment terms are set on this load, so no date "
                "can be computed — state that it is being processed and give no date"
            ),
        )

    assert load.invoice_received is not None  # is_invoiced guarantees this
    expected = load.invoice_received + timedelta(days=days)

    # A paid-looking load that is still missing paperwork is worth flagging rather than
    # smoothing over: billing accepted an invoice while the file list says a document is
    # absent, which usually means it arrived by another route. The date stands; the reply
    # simply should not also claim the paperwork is complete.
    note = None
    if missing:
        note = (
            f"billing has the invoice, but the file list still shows no {', '.join(missing)}"
            " — do not tell the sender their paperwork is complete"
        )

    return CargoTelPaymentState(
        state=BillingState.SCHEDULED,
        expected_payment_date=expected,
        invoice_received=load.invoice_received,
        net_days=days,
        payable=payable,
        missing_documents=missing,
        note=note,
    )
