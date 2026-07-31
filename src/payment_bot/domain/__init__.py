"""Pure, deterministic business logic (PRD §4.1.1).

Everything in this package is a **pure function of its inputs** — no network, no clock,
no config, no model calls. That is exactly what the PRD requires: pay dates and rate
sums must be computed "in code so results are auditable and grounded; the model never
derives dates or sums on its own." These functions are the single source of truth for
those numbers, and they are the most heavily unit-tested part of the system.
"""

from __future__ import annotations

from payment_bot.domain.documents import (
    REQUIRED_FOR_PAYMENT,
    ClassifiedDocument,
    DocCategory,
    DocumentStatus,
    assess_documents,
    classify,
)
from payment_bot.domain.pay_schedule import ScheduledPayDate, compute_scheduled_pay_date
from payment_bot.domain.rate import CarrierRate, DeductionLine, EarningLine, compute_carrier_rate
from payment_bot.domain.routing import RouteResult, route_load

__all__ = [
    "REQUIRED_FOR_PAYMENT",
    "CarrierRate",
    "ClassifiedDocument",
    "DeductionLine",
    "DocCategory",
    "DocumentStatus",
    "EarningLine",
    "RouteResult",
    "ScheduledPayDate",
    "assess_documents",
    "classify",
    "compute_carrier_rate",
    "compute_scheduled_pay_date",
    "route_load",
]
