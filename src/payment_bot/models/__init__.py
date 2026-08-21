"""Typed domain models.

These are the data shapes exchanged between layers. External-facing models (the
Transport Pro payload) are deliberately lenient about unknown fields — upstream APIs
add fields over time — while our own tool I/O models are strict.
"""

from __future__ import annotations

from payment_bot.models.cargotel import CargoTelDocument, CargoTelLoad
from payment_bot.models.email import EmailAttachment, InboundEmail
from payment_bot.models.enums import (
    AuthDecision,
    Intent,
    PayBasis,
    SensitiveAction,
    SensitiveFlag,
    System,
)
from payment_bot.models.transport_pro import (
    AccountInformation,
    AuthorizationContext,
    Deduction,
    DispatchRow,
    Earning,
    FileDocument,
    NoaFactoring,
    RemitTo,
    SettlementEntry,
    ShipmentInformation,
    TransportProLoad,
    Waypoint,
    WaypointDate,
    split_care_of,
)

__all__ = [
    "AccountInformation",
    "AuthDecision",
    "AuthorizationContext",
    "CargoTelDocument",
    "CargoTelLoad",
    "Deduction",
    "DispatchRow",
    "Earning",
    "EmailAttachment",
    "FileDocument",
    "InboundEmail",
    "Intent",
    "NoaFactoring",
    "PayBasis",
    "RemitTo",
    "SensitiveAction",
    "SensitiveFlag",
    "SettlementEntry",
    "ShipmentInformation",
    "System",
    "TransportProLoad",
    "Waypoint",
    "WaypointDate",
    "split_care_of",
]
