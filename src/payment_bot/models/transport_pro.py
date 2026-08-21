"""Transport Pro load payload — the authoritative shape from PRD §4.3.0.

This is the *source of truth for grounding*: every amount, date, and status a reply
can contain must trace back to one of these fields (via a tool result). Money is
modelled as :class:`~decimal.Decimal` — never float — so sums are exact and auditable.

The model is lenient about unknown/extra fields because the live API adds fields over
time; it is strict about the types of the fields we actually depend on.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


def _coerce_identifier(value: object) -> object:
    """Accept an identifier that arrives as a JSON number rather than a string.

    Transport Pro returns ``settlement_id`` as an int on some loads (``1301650``) and as a
    string on others. Because the whole payload is parsed as one model, a single int made an
    entire load unreadable — which surfaced as ``authorization: 2508651=ERROR(...)`` and
    blocked the reply, since an unresolvable authorization fails closed. Observed on live
    mail.

    These are opaque identifiers, never arithmetic, so normalising to ``str`` keeps one type
    for every consumer instead of spreading ``str | int`` through the codebase. ``bool`` is
    passed through deliberately so pydantic still rejects it — ``True`` is not an id.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    return value


#: An identifier the API may send as either a string or a number.
IdentifierStr = Annotated[str | None, BeforeValidator(_coerce_identifier)]


#: The ``c/o`` joining a carrier to the factor collecting on its behalf.
#:
#: Transport Pro writes a settlement payee as ``<carrier> c/o <factor>`` — "Parasource Inc
#: c/o England Carrier Services". That is the **mirror image** of CargoTel, which writes
#: ``<factor> C/O <carrier>`` (see ``models.cargotel._CO_SUFFIX_RE``). The separator pattern
#: is deliberately duplicated rather than shared: the two tenants keep the halves in opposite
#: order, and one import would make it far too easy to trim the wrong side of the string.
#:
#: The separator between the C and the O is **required**, not optional. A bare "CO"
#: truncates ordinary company names — "COASTAL CO TRANSPORT" would become "COASTAL" — and
#: "Co" is everywhere in this industry. Whitespace on both sides for the same reason.
_CARE_OF_RE = re.compile(r"\s+c\s*[/.]\s*o\.?\s+", re.IGNORECASE)


def split_care_of(payee: str | None) -> tuple[str | None, str | None]:
    """``(carrier, factor)`` from a Transport Pro pay-to string.

    ``"Parasource Inc c/o England Carrier Services"`` gives
    ``("Parasource Inc", "England Carrier Services")``; a plain name gives
    ``("Victory Transit LLC", None)``.

    Splitting matters for both halves, and for different reasons.

    The **carrier** half has to come out because every name test downstream is built for a
    carrier name on its own. ``_configured_carrier_contact`` demands an exact normalised
    match, and ``carrier_name_matches_sender`` requires every distinctive token of the name
    to appear in the sender's address — the factor's words are in neither, so the joined
    string fails both tests for the carrier it names.

    The **factor** half must come out so it can be kept OUT of the carrier list. A factor is
    not a carrier: disclosure to one is gated on ``allow_factoring`` and on a curated domain
    roster, and quietly filing "England Carrier Services" among a load's carriers would route
    around both.
    """

    if not payee:
        return None, None
    parts = _CARE_OF_RE.split(payee, maxsplit=1)
    carrier = parts[0].strip(" ,.-") or None
    factor = parts[1].strip(" ,.-") if len(parts) > 1 else None
    return carrier, (factor or None)


class _TpModel(BaseModel):
    """Base for Transport Pro payload models: ignore unknown fields, parse leniently."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class RemitTo(_TpModel):
    """Where the carrier's payment is sent. ``send_payment_to='self'`` means no factor."""

    send_payment_to: str | None = None
    company_name: str | None = None

    @property
    def is_factoring(self) -> bool:
        """True when payment is remitted to a third party (factoring company)."""

        if not self.send_payment_to:
            return False
        return self.send_payment_to.strip().lower() != "self"


class AccountInformation(_TpModel):
    """Carrier account block (§4.3.0)."""

    company_name: str | None = None
    dot_number: IdentifierStr = None
    mc_number: IdentifierStr = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    remit_to: RemitTo | None = None


class Earning(_TpModel):
    """One earning line. The carrier rate is the SUM of these amounts (§4.1.1)."""

    title: str
    amount: Decimal
    payment_status: str | None = None
    settlement_id: IdentifierStr = None
    estimated_payment_date: date | None = None
    actual_payment_date: date | None = None
    payment_method: str | None = None
    check_number: IdentifierStr = None

    @property
    def is_paid(self) -> bool:
        """A line is paid once it has an actual payment date."""

        return self.actual_payment_date is not None


class Deduction(_TpModel):
    """One deduction/adjustment line — each must be reported with its reason (§4.1.1)."""

    title: str
    amount: Decimal
    reason: str | None = None


class WaypointDate(_TpModel):
    """A waypoint timestamp with its stated timezone (e.g. PDT at pickup, EDT at delivery)."""

    timestamp: datetime
    timezone: str | None = None

    @field_validator("timezone", mode="before")
    @classmethod
    def _normalize_timezone(cls, value: object) -> object:
        """Accept every timezone shape the live API emits.

        ``payment_information`` sends ``false`` when a stop has no timezone on file, and
        other Transport Pro screens send an integer UTC offset (e.g. ``-5``) rather than a
        name. Both are normalised here so a real payload never fails validation over a
        field we only ever echo.
        """

        if value is None or isinstance(value, bool):  # `false` (or `true`) means "unknown"
            return None
        if isinstance(value, int | float):
            return str(value)
        return value


class Waypoint(_TpModel):
    """A pickup or delivery stop."""

    type: str
    city: str | None = None
    state: str | None = None
    date: WaypointDate | None = None


class ShipmentInformation(_TpModel):
    """Shipment block holding the ordered list of waypoints."""

    waypoints: list[Waypoint] = Field(default_factory=list)


class TransportProLoad(_TpModel):
    """One **payable** on a Transport Pro load, as returned by the live endpoint (§4.3.0).

    One record per carrier, not one per load. ``payment_information`` returns an array, and
    the array has an entry for every carrier that has a payable on the load: its own
    ``account_information`` (carrier and ``remit_to``), its own ``earnings``, its own
    ``deductions``, its own waypoints for the leg it ran. A load dispatched once has exactly
    one and the distinction never shows; a load re-dispatched or split across legs has
    several, and reading only the first hides entire carriers and entire payments.

    Live on 2436437, which has three: FOX CARRIERS ($905, remitted to eCapital), Alina
    Transport ($150 TONU, remitted to RTS Financial Service) and Parasource ($5,000 line haul
    paid 06/25 plus a $230 lumper on 08/12, remitted to England Carrier Services). The client
    took ``results[0]``, so for months the bot's entire picture of that load was FOX CARRIERS'
    $905 — which is why Parasource was told nothing and denied.

    Read every payable through ``TransportProClient.get_load_payables``. ``get_load`` returns
    the first and is right only where one carrier is all the caller can act on.
    """

    load_id: int
    billing_status: str | None = None
    account_information: AccountInformation | None = None
    deductions: list[Deduction] = Field(default_factory=list)
    earnings: list[Earning] = Field(default_factory=list)
    shipment_information: ShipmentInformation | None = None

    #: The carrier-facing load number this record was **requested** by — i.e. the 7-digit
    #: id from the email. Transport Pro's ``/voiceai/load/{n}/...`` endpoints take that
    #: number in the path but echo their *internal* record id back in ``load_id`` (e.g.
    #: ``/voiceai/load/2333606`` → ``load_id: 1303298``). The HTTP client sets this so the
    #: reply always quotes the number the carrier asked about, never the internal id.
    #: ``None`` for fixtures, where ``load_id`` already is the carrier-facing number.
    load_number: str | None = None

    # -- convenience accessors -------------------------------------------------
    @property
    def load_id_str(self) -> str:
        """The carrier-facing load id the pipeline routes on and the reply quotes."""

        return self.load_number or str(self.load_id)

    @property
    def internal_record_id(self) -> int:
        """Transport Pro's internal record id — the key for ``/files/search?recordId=``."""

        return self.load_id

    def _waypoint(self, kind: str) -> Waypoint | None:
        if self.shipment_information is None:
            return None
        for wp in self.shipment_information.waypoints:
            if wp.type.strip().lower() == kind:
                return wp
        return None

    @property
    def pickup(self) -> Waypoint | None:
        return self._waypoint("pickup")

    @property
    def delivery(self) -> Waypoint | None:
        return self._waypoint("delivery")

    @property
    def carrier_company(self) -> str | None:
        """The carrier this payable belongs to."""

        return self.account_information.company_name if self.account_information else None

    @property
    def factoring_company(self) -> str | None:
        """The factor this payable is remitted to, or ``None`` when it pays the carrier."""

        remit = self.account_information.remit_to if self.account_information else None
        return remit.company_name if (remit and remit.is_factoring) else None

    @property
    def pay_to(self) -> str | None:
        """The payee as the Settlement Entries screen writes it.

        ``"Parasource Inc c/o England Carrier Services"`` when the payable is factored, the
        carrier's own name when it is not. This is the string the operators read off the
        screen and the one a reply should quote: it answers which carrier and who collected
        for them in one breath, which is exactly what a "paid to the wrong factor" enquiry
        is asking about.
        """

        carrier = self.carrier_company
        if not carrier:
            return None
        factor = self.factoring_company
        return f"{carrier} c/o {factor}" if factor else carrier


# ---------------------------------------------------------------------------
# Auxiliary Transport Pro endpoints (§4.3) — separate screens from the load
# payload above. These are the *source* rows a client returns; the tool wrappers
# derive the convenience/summary fields (delivered_row, has_*, matches_load).
# ---------------------------------------------------------------------------
class DispatchRow(_TpModel):
    """One row of the Dispatch History screen (§4.3 ``tp_get_dispatch_history``).

    **This row carries no delivery date.** The screen's Pickup and Delivery cells stack a
    place above a date, and only the place is parsed here; the date beside it belongs to the
    LOAD's waypoints and reaches a reply through ``tp_get_load_summary``, which grounds it.
    Live on load 2478889: the draft said "delivered on August 6, 2026" for a load that
    delivered on June 30, citing this tool — August 6 was :attr:`last_updated`.
    """

    carrier_name: str
    mc_number: IdentifierStr = None
    freight_bill: Decimal | None = None
    dispatch_status: str  # "Delivered" | "Canceled Customer Refused" | ...
    #: Origin **place**, e.g. ``"WHITAKERS, NC"`` — never a date, despite the column header.
    pickup: str | None = None
    #: Destination **place**, e.g. ``"LAREDO, TX"``. Not the delivery date: see the class note.
    delivery: str | None = None
    comment: str | None = None
    #: When the dispatch RECORD was last edited, and the trap on this row: it is the only
    #: date-shaped field here, so it reads as a delivery date and is not one. On 2478889 it
    #: was 2026-08-06 against a 2026-06-30 delivery — five weeks out, and the UI labels the
    #: column "Last Updated" with the editing user's name beside it. Never a delivery,
    #: dispatch or payment date.
    last_updated: str | None = None

    @property
    def is_delivered(self) -> bool:
        return self.dispatch_status.strip().casefold() == "delivered"

    @property
    def is_canceled(self) -> bool:
        return "cancel" in self.dispatch_status.casefold()


class SettlementEntry(_TpModel):
    """One row of the Settlement Entries screen (§4.3 ``tp_get_settlement_entries``)."""

    amount: Decimal
    #: The screen's **Pay To** party, written ``<carrier> c/o <factor>`` when the carrier is
    #: factored — "Parasource Inc c/o England Carrier Services". Read it through
    #: :attr:`paid_carrier` / :attr:`paid_factoring_company` for anything that compares or
    #: matches a name; the raw string is what a reply quotes, because it answers both halves
    #: of the usual question at once — which carrier, and who collected for them.
    carrier_name: str | None = None
    settle_date: date | None = None
    pay_date: date | None = None
    payment_method: str | None = None
    check_or_ref: str | None = None
    # advance | fee | claim | short_pay | addition | settlement | other
    line_type: str | None = None
    description: str | None = None

    @property
    def paid_carrier(self) -> str | None:
        """The carrier half of :attr:`carrier_name` — the party that hauled the load."""

        return split_care_of(self.carrier_name)[0]

    @property
    def paid_factoring_company(self) -> str | None:
        """The factor half of :attr:`carrier_name`, or ``None`` when the row names no factor.

        Who was actually paid on this row, which is not necessarily the factor of record:
        ``remit_to`` holds one name at a time, while a load re-dispatched across carriers
        settles to whoever each carrier was factored to at the time. Load 2436437 carries
        RTS, eCapital and England Carrier Services across its rows.
        """

        return split_care_of(self.carrier_name)[1]


class FileDocument(_TpModel):
    """One document from the File History screen (§4.3 ``tp_get_file_history``)."""

    file_type: str  # "Carrier Invoice" | "Bill of Lading" | "Carrier Rate Agreement" | ...
    #: The API's ``fileTypeId``. Stable per tenant, unlike the display name, so this is what
    #: document classification keys on (see :mod:`payment_bot.domain.documents`).
    file_type_id: int | None = None
    index_date: date | None = None
    upload_date: date | None = None
    indexed_by: str | None = None
    comments: str | None = None


class NoaFactoring(_TpModel):
    """Read-only NOA / factoring status for a load (§4.3 ``tp_get_noa_factoring``).

    Reporting this is read-only; any request to *add/update* an NOA or change factoring
    setup is a sensitive change and escalates via ``detect_sensitive_change`` (§4.2).
    """

    noa_on_file: bool = False
    #: Every factor of record on the load, joined for display. Read :attr:`by_carrier` to
    #: report only the carrier the sender is entitled to.
    factoring_company_on_file: str | None = None
    details: str | None = None
    #: ``(carrier, factor-or-None)`` per payable, so a caller can answer "where does MY
    #: payment go" without naming the other carriers' factors.
    #:
    #: This exists because the single value was payable[0]'s, which on a multi-carrier load
    #: is simply the wrong answer rather than a partial one: asked where their payment goes,
    #: Parasource on load 2436437 would have been told eCapital — FOX CARRIERS' factor.
    by_carrier: tuple[tuple[str, str | None], ...] = ()
    #: Factoring/assignment documents indexed on the load, if any. Load-level: the file
    #: history names the load, never the leg, so this cannot be attributed to one carrier
    #: and is reported to any authorized sender as evidence that an NOA is on file.
    document_evidence: str | None = None


def _dedupe_carriers(names: Iterable[str]) -> tuple[str, ...]:
    """Carrier names in first-seen order, one per name, matched case-insensitively."""

    seen: dict[str, str] = {}
    for name in names:
        cleaned = name.strip()
        if cleaned:
            seen.setdefault(cleaned.casefold(), cleaned)
    return tuple(seen.values())


class AuthorizationContext(_TpModel):
    """Who is allowed to receive disclosure about a load (source for ``check_authorization``).

    In production this comes from Transport Pro's authorized-parties data for the load;
    the mock supplies it from a fixture. Kept separate from the public tool so the
    matching policy lives in one deterministic place (see ``tools.shared``).

    **Every party field here is a tuple, because a load is not one carrier.** A load
    re-dispatched after a cancellation, or split across legs, has one *payable* per carrier
    — its own ``account_information``, its own ``remit_to``, its own earnings — and
    ``payment_information`` returns all of them. Reading only the first is what denied
    Parasource their own load; see :attr:`carrier_companies`.

    Unlike its siblings this model **forbids** unknown fields. Everything else here parses a
    live payload, where a field we have never heard of must be ignored rather than fatal; this
    one is only ever built in our own code, and it fails CLOSED — an unrecognised keyword would
    leave the carrier list empty and DENY every sender on the load, silently, which is the one
    failure mode that looks exactly like working correctly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Every carrier this load has been dispatched to or settled with — **not one name**.
    #:
    #: A load re-dispatched after a cancellation or a fall-off carries several. Load 2436437
    #: has five in its dispatch history (Hazemo, Victory Transit, FOX CARRIERS, Parasource,
    #: Alina), and this used to be a single string taken from
    #: ``account_information.company_name``. Whichever carrier that field happened to hold,
    #: the other four were unknown to authorization: Parasource — the carrier that actually
    #: hauled it and was paid $5,000 on 06/25 — asked about their own load from
    #: ``parasourceinc.com`` and was DENIED, which then dropped the load from the answer set
    #: entirely, so the settlement row showing that payment was never even read.
    #:
    #: Ordering is the load's own: the payables in API order, then the dispatch rows.
    #: Nothing downstream may rely on it — every test iterates.
    #:
    #: **Carriers only.** The factor half of a ``<carrier> c/o <factor>`` pay-to belongs in
    #: :attr:`factoring_companies`; disclosure to a factor is gated on ``allow_factoring``
    #: and a curated domain roster, and a factor listed here would be matched as a carrier
    #: and bypass both.
    carrier_companies: tuple[str, ...] = ()
    authorized_emails: tuple[str, ...] = ()
    #: ``(carrier, email)`` for every carrier-side contact address on the load, lowercased.
    #:
    #: The same addresses as :attr:`authorized_emails`, keeping the carrier each one belongs
    #: to. Whether a sender is on the load is answered by the flat tuple; **whose leg they are
    #: on** is only answerable here, and on a multi-carrier load that is the difference between
    #: a right answer and a confidently wrong one.
    #:
    #: Live on load 2469115, a four-carrier cross-border relay: ``dispatch@zmile.io`` is on
    #: the load, so Zmile's payroll asking about it was authorized on their domain — and then
    #: answered from the first payable, which is Aralo Express's $2,580 remitted to RTS
    #: Financial. Zmile's own $5,750 sits on the third. Being on the load is not being on
    #: every leg of it, and the dispatch payload has known which leg all along.
    #:
    #: An address with no carrier beside it (a dispatch row with contacts but no carrier
    #: block) appears in :attr:`authorized_emails` only: it still authorizes, it just cannot
    #: narrow.
    carrier_contacts: tuple[tuple[str, str], ...] = ()
    #: ``(carrier, factor-or-None)`` for each payable on the load, in the API's order.
    #:
    #: The PAIRING is what this field is for, and no two parallel tuples could carry it. A
    #: factor is the factor of record for the *leg it was assigned*, not for the load: on
    #: 2436437, eCapital collects for FOX CARRIERS, RTS for Alina Transport, England Carrier
    #: Services for Parasource. Answering England about the load has to mean answering them
    #: about Parasource's $5,000 and nothing else — which is only knowable from the pair.
    #:
    #: A carrier appears here only if it has a payable. Carriers that were dispatched and
    #: never settled are in :attr:`carrier_companies` and not here, which is right: there is
    #: no money to attribute to them.
    payable_parties: tuple[tuple[str, str | None], ...] = ()
    factoring_emails: tuple[str, ...] = ()
    #: Whether a Notice of Assignment / factoring agreement is already indexed on the load.
    #:
    #: Distinct from :attr:`factoring_companies`, and the distinction is the whole point. The
    #: company name comes from ``remit_to`` — a field somebody has to key in — while this comes
    #: from the file history. On load 2530268 the NOA was indexed three times over and the
    #: remit field was still empty, so the pre-NOA branch read "their NOA has not reached us"
    #: and the reply asked a factor to send a document sitting on the load. `pre_noa` is about
    #: whether the NOA is ON FILE; only this field answers that.
    noa_on_file: bool = False

    @property
    def carrier_label(self) -> str | None:
        """Every carrier on the load as one string, for a reason line. Never for matching.

        Used where a match is real but not attributable to one carrier — an explicitly
        authorized address is on the load, not on a leg of it, so naming the first carrier
        would be a guess dressed up as a fact.
        """

        return "; ".join(self.carrier_companies) or None

    @property
    def factoring_companies(self) -> tuple[str, ...]:
        """Every factor of record on the load, de-duplicated, in payable order.

        One per payable, because ``remit_to`` is per payable. Reading only the first payable's
        factor is what measured England Carrier Services — the factor Parasource had actually
        assigned load 2436437 to — against eCapital's roster entry, and denied it.

        Every name here is a factor **of record**, so all of them are eligible for the
        FACTORING decision on exactly the terms the single value had: gated on
        ``allow_factoring`` and on the sender's own domain being rostered for that factor.
        Nothing becomes authorized merely by having been paid.
        """

        seen: dict[str, str] = {}
        for _, factor in self.payable_parties:
            if factor:
                seen.setdefault(factor.casefold(), factor)
        return tuple(seen.values())

    @property
    def factor_label(self) -> str | None:
        """Every factor of record on the load as one string. The twin of :attr:`carrier_label`."""

        return "; ".join(self.factoring_companies) or None

    def carriers_for_contact(self, sender_email: str) -> tuple[str, ...]:
        """The carriers this exact address is a contact for. Empty when it is not attributable."""

        wanted = sender_email.strip().lower()
        return _dedupe_carriers(
            carrier for carrier, email in self.carrier_contacts if email.lower() == wanted
        )

    def carriers_at_domain(self, domain: str) -> tuple[str, ...]:
        """The carriers whose contact addresses sit at ``domain``.

        The domain form of :meth:`carriers_for_contact`, for the sender who writes from
        ``payroll@`` when ``dispatch@`` is the address on file. Several carriers can share a
        domain in principle; all of them are returned, and the caller narrows to their union.
        """

        wanted = domain.strip().lower()
        if not wanted:
            return ()
        return _dedupe_carriers(
            carrier
            for carrier, email in self.carrier_contacts
            if email.rsplit("@", 1)[-1].strip().lower() == wanted
        )

    def carriers_factored_to(self, factor: str) -> tuple[str, ...]:
        """The carriers whose payable remits to ``factor``, matched exactly and casefolded.

        The narrowing behind a FACTORING decision: the factor asked about the load, and the
        part of the load that is theirs is the leg (or legs) they collect for.
        """

        wanted = factor.casefold()
        return tuple(
            carrier
            for carrier, on_file in self.payable_parties
            if on_file and on_file.casefold() == wanted
        )
