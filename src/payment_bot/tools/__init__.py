"""Tool layer: the typed capabilities the agent can call, and the registry factory.

``build_default_registry`` wires up every tool this slice implements. ``PAYMENT_STATUS_TOOLS``
is the subset advertised to the model for the ``payment_status`` skill — the registry
holds all tools, but each skill exposes only the ones its playbook (§3.1) permits.
"""

from __future__ import annotations

from payment_bot.logging import AuditSink
from payment_bot.tools.base import Tool, ToolContext, ToolOutcome, ToolRegistry
from payment_bot.tools.cargotel import CgtGetLoadStatus
from payment_bot.tools.shared import (
    CarrierCrossCheck,
    CheckAuthorization,
    ClassifyIntent,
    ComputeCarrierRate,
    ComputeScheduledPayDate,
    DetectSensitiveChange,
    ExtractIdentifiers,
    RouteLoad,
)
from payment_bot.tools.submit import Citation, SubmitDraft
from payment_bot.tools.transport_pro import (
    TpGetDispatchHistory,
    TpGetFileHistory,
    TpGetLoadSummary,
    TpGetNoaFactoring,
    TpGetSettlementEntries,
)

#: Tools the agent may call while running the payment_status skill (§3.1 / §6 matrix).
#: The safety/intake tools (classify, extract, route, detect_sensitive_change) run
#: deterministically in the pipeline *before* the agent, so they are not advertised here.
PAYMENT_STATUS_TOOLS: tuple[str, ...] = (
    TpGetLoadSummary.name,
    TpGetDispatchHistory.name,
    TpGetSettlementEntries.name,
    TpGetFileHistory.name,
    TpGetNoaFactoring.name,
    ComputeScheduledPayDate.name,
    CarrierCrossCheck.name,
    CheckAuthorization.name,
    SubmitDraft.name,
)

#: Tools the agent may call while running the rate_verification skill (§3.2 / §6 matrix).
RATE_VERIFICATION_TOOLS: tuple[str, ...] = (
    TpGetLoadSummary.name,
    ComputeCarrierRate.name,
    TpGetDispatchHistory.name,
    TpGetSettlementEntries.name,
    TpGetNoaFactoring.name,
    TpGetFileHistory.name,
    CarrierCrossCheck.name,
    CheckAuthorization.name,
    SubmitDraft.name,
)


#: Tools for the CargoTel (6-digit) payment_status skill.
#:
#: Three, against nine on the Transport Pro side, because CargoTel is one page: the load,
#: its documents, its terms and the computed date all arrive from a single read. There is no
#: dispatch history, settlement table or file-history endpoint to advertise, and
#: ``compute_scheduled_pay_date`` is deliberately absent — the Monday/Thursday rule does not
#: apply here, and offering the tool would invite the model to apply it anyway.
CARGOTEL_PAYMENT_STATUS_TOOLS: tuple[str, ...] = (
    CgtGetLoadStatus.name,
    CheckAuthorization.name,
    SubmitDraft.name,
)


def build_default_registry(audit_sink: AuditSink | None = None) -> ToolRegistry:
    """Return a registry with every implemented tool registered."""

    registry = ToolRegistry(audit_sink=audit_sink)
    registry.register_all(
        [
            # shared
            ClassifyIntent(),
            ExtractIdentifiers(),
            RouteLoad(),
            DetectSensitiveChange(),
            CheckAuthorization(),
            CarrierCrossCheck(),
            ComputeScheduledPayDate(),
            ComputeCarrierRate(),
            # cargotel
            CgtGetLoadStatus(),
            # transport pro
            TpGetLoadSummary(),
            TpGetDispatchHistory(),
            TpGetSettlementEntries(),
            TpGetFileHistory(),
            TpGetNoaFactoring(),
            # terminal
            SubmitDraft(),
        ]
    )
    return registry


__all__ = [
    "CARGOTEL_PAYMENT_STATUS_TOOLS",
    "PAYMENT_STATUS_TOOLS",
    "RATE_VERIFICATION_TOOLS",
    "Citation",
    "Tool",
    "ToolContext",
    "ToolOutcome",
    "ToolRegistry",
    "build_default_registry",
]
