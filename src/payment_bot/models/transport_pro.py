"""Transport Pro load payload — the authoritative shape from PRD §4.3.0.

This is the *source of truth for grounding*: every amount, date, and status a reply
can contain must trace back to one of these fields (via a tool result). Money is
modelled as :class:`~decimal.Decimal` — never float — so sums are exact and auditable.

The model is lenient about unknown/extra fields because the live API adds fields over
time; it is strict about the types of the fields we actually depend on.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


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
    dot_number: str | None = None
    mc_number: str | None = None
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
    settlement_id: str | None = None
    estimated_payment_date: date | None = None
    actual_payment_date: date | None = None
    payment_method: str | None = None
    check_number: str | None = None

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
    """A full Transport Pro load object as returned by the live endpoint (§4.3.0)."""

    load_id: int
    billing_status: str | None = None
    account_information: AccountInformation | None = None
    deductions: list[Deduction] = Field(default_factory=list)
    earnings: list[Earning] = Field(default_factory=list)
    shipment_information: ShipmentInformation | None = None

    # -- convenience accessors -------------------------------------------------
    @property
    def load_id_str(self) -> str:
        """The load id as the string the rest of the pipeline routes on."""

        return str(self.load_id)

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


# ---------------------------------------------------------------------------
# Auxiliary Transport Pro endpoints (§4.3) — separate screens from the load
# payload above. These are the *source* rows a client returns; the tool wrappers
# derive the convenience/summary fields (delivered_row, has_*, matches_load).
# ---------------------------------------------------------------------------
class DispatchRow(_TpModel):
    """One row of the Dispatch History screen (§4.3 ``tp_get_dispatch_history``)."""

    carrier_name: str
    mc_number: str | None = None
    freight_bill: Decimal | None = None
    dispatch_status: str  # "Delivered" | "Canceled Customer Refused" | ...
    pickup: str | None = None
    delivery: str | None = None
    comment: str | None = None
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
    carrier_name: str | None = None
    settle_date: date | None = None
    pay_date: date | None = None
    payment_method: str | None = None
    check_or_ref: str | None = None
    # advance | fee | claim | short_pay | addition | settlement | other
    line_type: str | None = None
    description: str | None = None


class FileDocument(_TpModel):
    """One document from the File History screen (§4.3 ``tp_get_file_history``)."""

    file_type: str  # "Carrier Invoice" | "Bill Of Lading" | "Carrier Rate Agreement" | ...
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
    factoring_company_on_file: str | None = None
    details: str | None = None


class AuthorizationContext(_TpModel):
    """Who is allowed to receive disclosure about a load (source for ``check_authorization``).

    In production this comes from Transport Pro's authorized-parties data for the load;
    the mock supplies it from a fixture. Kept separate from the public tool so the
    matching policy lives in one deterministic place (see ``tools.shared``).
    """

    carrier_company: str | None = None
    authorized_emails: tuple[str, ...] = ()
    factoring_company: str | None = None
    factoring_emails: tuple[str, ...] = ()
