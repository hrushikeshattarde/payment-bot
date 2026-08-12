"""CargoTel load shapes (6-digit loads), scraped from ``loadmaint.mcgi``.

The CargoTel counterpart of :mod:`payment_bot.models.transport_pro`. Same job — be the
source of truth for grounding, money as :class:`~decimal.Decimal` — but the data arrives as
an HTML page rather than JSON, so :mod:`payment_bot.clients.cargotel_html` does the
narrowing and this module holds only what survived it.

Where each field comes from, verified against real pages for loads 296993, 296006 and
295089:

=========================  ==========================================================
Field                      Source in the page
=========================  ==========================================================
``status`` / ``status_date``  the banner, e.g. "Delivered 07/02/2026"
``carrier_name``           the Carrier field on the Carrier tab
``ap_terms``               ``select[name=loadmaint_form__APTerms]``, e.g. "Check Net 30"
``ap_invoice_number``      the A/P block's ``Invoice:`` cell
``invoice_received``       the A/P block's ``Invoice Received:`` cell
``payable``                ``input[name=loadmaint_form__ye_olde_payable]``
``pay_hold``               ``loadmaint_form__fs_ldmnt_audit_carrier_pay_hold``
``documents``              the Print Docs menu
=========================  ==========================================================

The one non-obvious part is ``documents``. "Print Docs" is a menu of things CargoTel can
*produce*, not a list of what is on file, so most of its entries appear on every load and
prove nothing. Two kinds of entry do carry information, and
:class:`CargoTelDocument` keeps them distinguishable:

* ``type=AttachDoc_*`` with a count — a genuinely uploaded file
  (``AttachDoc_Invoice (2)`` = two carrier invoices attached);
* a conditional generated type — ``Pdfgenbol05`` ("BOL 05 Dealer") is only offered once
  that BOL has been uploaded, which is why its mere presence is the BOL signal.

Verified across the three sample loads: 296993 and 296006 carry ``AttachDoc_Invoice`` and
``Pdfgenbol05``; 295089 carries neither, has no AP terms, and is on pay hold.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

#: ``type=`` values in the Print Docs menu that are rendered on demand from load data and
#: therefore say nothing about what paperwork arrived. Listed explicitly rather than
#: inferred, so a new conditional type shows up as unknown instead of being assumed inert.
ALWAYS_AVAILABLE_TYPES = frozenset(
    {
        "ImageBol",
        "StdBol",
        "TripSheet",
        "Manifest",
        "CarrierAgreementAlt",
        "Manifest_StdBol",
        "PreInvoiceBolAlt",
    }
)

#: The generated type that is only offered once the BOL 05 has been uploaded. Confirmed by
#: the business, and consistent with the samples: present on both invoiced loads, absent on
#: the one still waiting for paperwork.
BOL05_TYPE = "Pdfgenbol05"

#: The attachment type holding the carrier's own invoice.
CARRIER_INVOICE_TYPE = "AttachDoc_Invoice"


class CargoTelDocument(BaseModel):
    """One entry in the Print Docs menu."""

    model_config = ConfigDict(frozen=True)

    #: The visible label, e.g. "Invoice Attached Doc (2)" or "BOL 05 Dealer".
    label: str
    #: The ``type=`` query parameter, which is the stable identifier — labels are prose and
    #: vary ("Invoice" vs "Inspection Image"), the type does not.
    doc_type: str
    #: How many files are attached, for ``AttachDoc_*`` entries. ``None`` for a generated
    #: template, which is not the same as zero: nothing was uploaded *and* nothing was
    #: counted.
    attached_count: int | None = None

    @property
    def is_attachment(self) -> bool:
        """True when this entry represents a real uploaded file."""

        return self.doc_type.startswith("AttachDoc_")


class CargoTelCarrier(BaseModel):
    """A carrier's client record — ``client.mcgi?id=<client_id>``.

    Reached from a load via :attr:`CargoTelLoad.carrier_client_id`. Holds the two things the
    load page does not: who the carrier's contacts are, and what terms they are on when the
    load itself carries none.
    """

    model_config = ConfigDict(frozen=True)

    client_id: str
    name: str | None = None
    #: The payee of record, written ``ST JOHN C/O <carrier>`` on this tenant. This is the
    #: string that reaches QuickBooks as the bill's vendor — confirmed for North Trucking,
    #: whose record reads ``ST JOHN C/O NORTH TRUCKING LLC`` and whose QuickBooks vendor is
    #: byte-identical. Empty when the carrier is not factored (APFAS Group).
    factoring_name: str | None = None
    #: Carrier-level payment terms, the fallback when a load has none of its own. Note the
    #: payment method varies — "Check Net 30" and "ACH Net 30" both occur — so only the
    #: number is ever read out of it.
    ap_terms: str | None = None
    #: Every contact address on the record, lowercased and de-duplicated. The authorization
    #: source for this path: a load page names the carrier but carries no address.
    #:
    #: Frequently free-mail. Measured across four carriers, two have **only** a Gmail
    #: address — so the matching rule must allow an exact address while refusing to match on
    #: the domain, or authorizing one of them would authorize every Gmail user.
    emails: tuple[str, ...] = ()

    @property
    def is_factored(self) -> bool:
        return bool(self.factoring_name)

    @property
    def factoring_company(self) -> str | None:
        """Just the factor, with the ``C/O <carrier>`` suffix removed.

        **Use this for any authorization decision, never** :attr:`factoring_name`.

        CargoTel writes the payee as ``<factor> C/O <carrier>`` — "ST JOHN C/O NORTH TRUCKING
        LLC". The carrier's own name is therefore part of the string, and feeding the whole
        thing to a name-similarity test lets the *carrier's* words match an unrelated factor
        in the roster. Measured against the live 271-entry roster, that is not theoretical:

        * ``ST JOHN C/O NORTH TRUCKING LLC`` matched ``posshel erzkontor north american``
          on the token "north";
        * ``SAINT JOHN CAPITAL C/O EME AUTO TRANSPORT`` matched ``auto freight factoring``
          on "auto".

        Either would have authorized one factoring company to receive another's payment
        details — precisely the disclosure the authorization check exists to prevent.

        Trimming to the left of ``C/O`` keeps the genuine match: "ST JOHN" still links to
        ``loves's solutions llc dba saint john capital, llc`` on "john", and the full
        "SAINT JOHN CAPITAL" links on both "saint" and "john".
        """

        if not self.factoring_name:
            return None
        factor = _CO_SUFFIX_RE.split(self.factoring_name, maxsplit=1)[0].strip(" ,.-")
        return factor or None

    @property
    def net_days(self) -> int | None:
        """The N in the carrier's terms, or ``None``."""

        return net_days_in(self.ap_terms)


class CargoTelLoad(BaseModel):
    """A CargoTel load, reduced to what a payment-status or rate reply can use."""

    model_config = ConfigDict(frozen=True)

    load_id: str
    business_unit: str | None = None
    #: The carrier's client-record id, from the load page's carrier panel. The handle for
    #: :class:`CargoTelCarrier`; ``None`` when the load has no carrier assigned.
    carrier_client_id: str | None = None
    #: "Delivered", "In-Route", "Cancelled"… as shown in the banner.
    status: str | None = None
    #: The date attached to that status — the delivery date on a delivered load.
    status_date: date | None = None
    carrier_name: str | None = None

    #: Payment terms for this load, e.g. "Check Net 30". Absent on loads where nobody has
    #: set them yet, which is itself meaningful — see :attr:`is_invoiced`.
    ap_terms: str | None = None
    #: The A/P invoice reference, ``<load id>-<carrier's own invoice number>``. This is the
    #: same string that reaches QuickBooks as the bill's ``DocNumber``.
    ap_invoice_number: str | None = None
    #: When billing recorded the carrier's invoice as received. **The anchor the payment
    #: term counts from** — not the delivery date. See :mod:`payment_bot.domain.cargotel`.
    invoice_received: date | None = None
    payable: Decimal | None = None
    #: The audit hold that stops a load being paid regardless of paperwork.
    pay_hold: bool = False

    documents: tuple[CargoTelDocument, ...] = ()

    @property
    def carries_no_order(self) -> bool:
        """True when the page rendered the load form with nothing loaded into it.

        CargoTel's third way of saying "that is not a load", after "Invalid Order ID" and the
        login page — and the quietest. An id it does not have can return HTTP 200 with a
        179KB load form carrying no order: not the login page, no "Invalid Order ID" anywhere,
        so both existing guards pass and it parses into a load whose every field is empty.

        Live on a Neon Freight collections email. Its "Ref No" column held ``246558`` — the
        sender's own reference — which is six digits, so it routed here, and the blank form
        that came back was reported as *"the carrier record for this load lists no contact
        address, so the sender cannot be verified; add one in CargoTel"*. There was no record
        to add a contact to. Same misdiagnosis the :func:`~payment_bot.clients.cargotel_html.
        is_invalid_order` split exists to prevent: a routine not-a-load reported as a data gap
        someone then goes looking for.

        Every field here, rather than a single tell, because one alone is not decisive.
        Measured against real loads: all of them carry a business unit, a carrier client id, a
        status, a status date, a carrier name and terms. Both non-loads carry none of the six.
        ``payable`` is deliberately excluded — ``318354`` came back with $420.00 and nothing
        else, so a figure on its own does not make a page an order.
        """

        return not any(
            (
                self.business_unit,
                self.carrier_client_id,
                self.status,
                self.status_date,
                self.carrier_name,
                self.ap_terms,
            )
        )

    # -- document questions ---------------------------------------------------
    @property
    def carrier_invoice_count(self) -> int:
        """How many carrier invoices are attached. Zero when none are."""

        for doc in self.documents:
            if doc.doc_type == CARRIER_INVOICE_TYPE:
                return doc.attached_count or 0
        return 0

    @property
    def has_carrier_invoice(self) -> bool:
        return self.carrier_invoice_count > 0

    @property
    def has_bol05(self) -> bool:
        """True when the BOL 05 has been uploaded.

        Presence of the menu entry is the signal — CargoTel only offers that generated
        document once the underlying BOL exists. Unlike the attachment entries there is no
        count to read, so this is boolean by nature.
        """

        return any(doc.doc_type == BOL05_TYPE for doc in self.documents)

    @property
    def attachments(self) -> tuple[CargoTelDocument, ...]:
        return tuple(doc for doc in self.documents if doc.is_attachment)

    # -- billing state --------------------------------------------------------
    @property
    def is_invoiced(self) -> bool:
        """True when billing has accepted the carrier's invoice.

        Keyed on ``invoice_received`` rather than on the attachment, because the two answer
        different questions: a file can be attached days before billing processes it (on
        load 296006 the invoice was attached on 07/13 while the received date reads 07/07).
        The received date is what the payment term counts from, so it is what decides
        whether a date can be quoted at all.
        """

        return self.invoice_received is not None

    @property
    def net_days(self) -> int | None:
        """The N in "Net 30", from :attr:`ap_terms`. ``None`` when terms are unset or
        unrecognised — never defaulted to 30.

        Defaulting would be the tempting shortcut and it is the wrong one: terms genuinely
        vary by carrier, and a load with no terms set (load 295089) is a load nobody has
        finished setting up. Quoting a confident date off an assumed term would be inventing
        a payment promise.
        """

        return net_days_in(self.ap_terms)


#: The ``C/O`` that separates the factor from the carrier it is collecting for.
#:
#: The separator between the C and the O is **required**, not optional. Allowing a bare "CO"
#: truncates ordinary company names — "COASTAL CO TRANSPORT" became "COASTAL" — and "Co" is
#: everywhere in this industry. Whitespace on both sides for the same reason.
_CO_SUFFIX_RE = re.compile(r"\s+c\s*[/.]\s*o\.?\s+", re.IGNORECASE)


#: "Check Net 30" → 30. Tolerant of the payment method sharing the field, because it does:
#: the live values are "Check Net 30", not "Net 30".
_NET_TERMS_RE = re.compile(r"\bnet\s*(\d{1,3})\b", re.IGNORECASE)


def net_days_in(terms: str | None) -> int | None:
    """Extract the day count from a terms string, or ``None`` if there isn't one.

    "Due On Receipt" deliberately returns ``None`` rather than 0. Zero would flow through
    the arithmetic and produce "payable today", which reads as a promise; ``None`` routes to
    the same "no date can be given" path as unset terms, and a human decides.
    """

    if not terms:
        return None
    match = _NET_TERMS_RE.search(terms)
    return int(match.group(1)) if match else None
