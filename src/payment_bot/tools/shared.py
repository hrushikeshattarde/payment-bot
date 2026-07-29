"""Shared tools (§4.2).

These wrap the deterministic domain logic and the cross-cutting intake/safety checks.
The purely computational ones (``route_load``, ``compute_scheduled_pay_date``) delegate
to :mod:`payment_bot.domain` so there is exactly one implementation of each rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field

from payment_bot.domain import compute_carrier_rate as domain_carrier_rate
from payment_bot.domain import compute_scheduled_pay_date as domain_scheduled_pay_date
from payment_bot.domain import route_load as domain_route_load
from payment_bot.errors import ToolError
from payment_bot.models import (
    AuthDecision,
    Intent,
    SensitiveAction,
    SensitiveFlag,
    System,
)
from payment_bot.tools.base import Tool, ToolContext

# Company-name tokens too generic to prove identity by themselves.
_STOPWORDS = frozenset(
    {
        "inc", "llc", "corp", "co", "ltd", "incorporated", "company", "corporation",
        "transport", "transportation", "trucking", "logistics", "carrier", "carriers",
        "express", "freight", "services", "service", "group", "the", "and",
    }
)  # fmt: skip

_LOAD_ID_RE = re.compile(r"\b\d{6,7}\b")
_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
_INVOICE_RE = re.compile(r"invoice\s*(?:no\.?|number|#)?\s*:?\s*(\d{3,})", re.IGNORECASE)
_COMPANY_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&'.\- ]{2,40}?,?\s+(?:Inc|LLC|L\.L\.C\.|Incorporated|Corp|Corporation|Co|Ltd)\b\.?)"
)
_COLUMN_HINT_RE = re.compile(
    r"reference\s*#|p\.?\s*o\.?\s*(?:number|#)|load\s*#|invoice\s*#|pro\s*#", re.IGNORECASE
)


def company_tokens(name: str | None) -> set[str]:
    """Distinctive lowercase tokens of a company name (len ≥ 4, minus generic words)."""

    if not name:
        return set()
    tokens = re.split(r"[^a-z0-9]+", name.lower())
    return {t for t in tokens if len(t) >= 4 and t not in _STOPWORDS}


def _parse_pay_date(value: str | None) -> date | None:
    """Parse an API pay date into an EDT calendar date.

    Bare ``YYYY-MM-DD`` values are calendar dates (no shift). Full timestamps are
    converted from their offset into EDT (fixed UTC-4) before the date is taken.
    """

    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        if "T" in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return (dt.astimezone(UTC) - timedelta(hours=4)).date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(f"invalid date {value!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# extract_identifiers
# ---------------------------------------------------------------------------
class StatedRate(BaseModel):
    load_id: str | None = None
    amount: Decimal


class ExtractIdentifiersInput(BaseModel):
    subject: str = ""
    body: str = ""
    thread_text: str = ""


class ExtractIdentifiersOutput(BaseModel):
    ok: bool = True
    load_ids: list[str]
    stated_rates: list[StatedRate]
    carrier_names: list[str]
    factoring_company: str | None = None
    sender_invoice_numbers: list[str]
    column_hints: list[str]


class ExtractIdentifiers(Tool):
    """Parse subject/body/thread for load ids, stated rates, carriers, factoring (§4.2)."""

    name = "extract_identifiers"
    description = (
        "Extract 6/7-digit load/invoice ids, stated rate amounts, carrier names, "
        "factoring company, sender invoice numbers, and column hints from an email."
    )
    input_model = ExtractIdentifiersInput

    def run(self, params: BaseModel, ctx: ToolContext) -> ExtractIdentifiersOutput:
        assert isinstance(params, ExtractIdentifiersInput)
        text = "\n".join(p for p in (params.subject, params.body, params.thread_text) if p)

        load_ids = _dedupe(_LOAD_ID_RE.findall(text))
        invoice_numbers = _dedupe(_INVOICE_RE.findall(text))

        stated_rates: list[StatedRate] = []
        for line in text.splitlines():
            amounts = [_money(m) for m in _MONEY_RE.findall(line)]
            if not amounts:
                continue
            line_ids = _LOAD_ID_RE.findall(line)
            load_ref = line_ids[0] if len(line_ids) == 1 else None
            stated_rates.extend(StatedRate(load_id=load_ref, amount=a) for a in amounts)

        carrier_names = _dedupe(m.strip().rstrip(".") for m in _COMPANY_RE.findall(text))

        factoring_company: str | None = None
        if re.search(r"factor", text, re.IGNORECASE):
            for sentence in re.split(r"[.\n]", text):
                if "factor" in sentence.lower():
                    match = _COMPANY_RE.search(sentence)
                    if match:
                        factoring_company = match.group(1).strip().rstrip(".")
                        break

        column_hints = _dedupe(m.strip() for m in _COLUMN_HINT_RE.findall(text))

        # Ground the sender's own stated amounts so the reply may quote them back
        # (attributed to the sender) without tripping the pre-send gate.
        for rate in stated_rates:
            ctx.ledger.record_amount(rate.amount, self.name, load_id=rate.load_id)

        return ExtractIdentifiersOutput(
            load_ids=load_ids,
            stated_rates=stated_rates,
            carrier_names=carrier_names,
            factoring_company=factoring_company,
            sender_invoice_numbers=invoice_numbers,
            column_hints=column_hints,
        )


# ---------------------------------------------------------------------------
# route_load
# ---------------------------------------------------------------------------
class RouteLoadInput(BaseModel):
    load_id: str


class RouteLoadOutput(BaseModel):
    ok: bool = True
    system: System
    length: int


class RouteLoad(Tool):
    """Route a load id to its owning system by length (§4.1)."""

    name = "route_load"
    description = "Route a load id: 7 digits → Transport Pro, 6 → QuickBooks, else invalid."
    input_model = RouteLoadInput

    def run(self, params: BaseModel, ctx: ToolContext) -> RouteLoadOutput:
        assert isinstance(params, RouteLoadInput)
        result = domain_route_load(params.load_id)
        return RouteLoadOutput(system=result.system, length=result.length)


# ---------------------------------------------------------------------------
# detect_sensitive_change
# ---------------------------------------------------------------------------
class AttachmentMeta(BaseModel):
    filename: str
    mime_type: str | None = None


class DetectSensitiveChangeInput(BaseModel):
    subject: str = ""
    body: str = ""
    attachments_metadata: list[AttachmentMeta] = Field(default_factory=list)


class DetectSensitiveChangeOutput(BaseModel):
    ok: bool = True
    flags: list[SensitiveFlag]
    evidence: list[str]
    action: SensitiveAction


# Phrase → flag. NOA/factoring only escalates on an action verb (add/update/attach…),
# so those are handled separately below rather than by bare keyword.
_BANK_PHRASES = (
    "bank change", "change bank", "update bank", "new bank", "banking information",
    "routing number", "account number", "ach", "direct deposit", "void check",
    "voided check", "remittance", "update payment info", "change payment",
)  # fmt: skip
_CONTACT_PHRASES = (
    "change email", "update email", "new email address", "change our email",
    "update contact", "new contact email", "change of email",
)  # fmt: skip
_NOA_ACTION_RE = re.compile(
    r"(add|attach|update|change|set\s*up|setup|assign|register|remove|release)\D{0,30}"
    r"(noa|notice of assignment|factor)",
    re.IGNORECASE,
)


class DetectSensitiveChange(Tool):
    """Detect bank / NOA-setup / contact-change signals that force escalation (§4.2)."""

    name = "detect_sensitive_change"
    description = (
        "Scan an email + attachment metadata for sensitive changes (bank, NOA/factoring "
        "setup, contact email). Any hit means escalate — never auto-answer."
    )
    input_model = DetectSensitiveChangeInput

    def run(self, params: BaseModel, ctx: ToolContext) -> DetectSensitiveChangeOutput:
        assert isinstance(params, DetectSensitiveChangeInput)
        haystack = f"{params.subject}\n{params.body}".lower()
        flags: list[SensitiveFlag] = []
        evidence: list[str] = []

        for phrase in _BANK_PHRASES:
            if phrase in haystack:
                _add(flags, SensitiveFlag.BANK_CHANGE)
                evidence.append(f"bank: matched {phrase!r}")

        for match in _NOA_ACTION_RE.finditer(f"{params.subject}\n{params.body}"):
            _add(flags, SensitiveFlag.NOA_SETUP_CHANGE)
            evidence.append(f"noa_setup: matched {match.group(0).strip()!r}")

        for phrase in _CONTACT_PHRASES:
            if phrase in haystack:
                _add(flags, SensitiveFlag.EMAIL_CONTACT_CHANGE)
                evidence.append(f"contact: matched {phrase!r}")

        for att in params.attachments_metadata:
            lower = att.filename.lower()
            if any(k in lower for k in ("voidcheck", "void_check", "void-check", "directdeposit")):
                _add(flags, SensitiveFlag.BANK_CHANGE)
                evidence.append(f"bank: attachment {att.filename!r}")
            if "noa" in lower or "assignment" in lower:
                _add(flags, SensitiveFlag.NOA_SETUP_CHANGE)
                evidence.append(f"noa_setup: attachment {att.filename!r}")

        if not flags:
            flags.append(SensitiveFlag.NONE)
            action = SensitiveAction.CONTINUE
        else:
            action = SensitiveAction.ESCALATE

        return DetectSensitiveChangeOutput(flags=flags, evidence=evidence, action=action)


# ---------------------------------------------------------------------------
# check_authorization
# ---------------------------------------------------------------------------
class CheckAuthorizationInput(BaseModel):
    sender_email: str
    sender_name: str | None = None
    load_id: str
    system: System


class CheckAuthorizationOutput(BaseModel):
    ok: bool = True
    decision: AuthDecision
    matched_party: str | None = None
    reason: str


class CheckAuthorization(Tool):
    """Decide whether a sender may receive disclosure about a load (§4.2)."""

    name = "check_authorization"
    description = (
        "Return ALLOW / DENY / FACTORING for a sender against a load's authorized parties."
    )
    input_model = CheckAuthorizationInput

    def run(self, params: BaseModel, ctx: ToolContext) -> CheckAuthorizationOutput:
        assert isinstance(params, CheckAuthorizationInput)
        if params.system is not System.TRANSPORT_PRO:
            raise ToolError(
                f"authorization source not wired for system {params.system.value!r} "
                "(this slice covers Transport Pro / 7-digit only)"
            )

        auth = ctx.tp.get_authorization_context(params.load_id)
        sender = params.sender_email.strip().lower()
        domain = sender.split("@")[-1].replace(".", "")

        if sender in {e.lower() for e in auth.authorized_emails}:
            return CheckAuthorizationOutput(
                decision=AuthDecision.ALLOW,
                matched_party=auth.carrier_company,
                reason="sender is an explicitly authorized contact for this load",
            )
        if sender in {e.lower() for e in auth.factoring_emails}:
            return CheckAuthorizationOutput(
                decision=AuthDecision.FACTORING,
                matched_party=auth.factoring_company,
                reason="sender is the factoring company on file",
            )

        carrier_toks = company_tokens(auth.carrier_company)
        if any(tok in domain for tok in carrier_toks):
            return CheckAuthorizationOutput(
                decision=AuthDecision.ALLOW,
                matched_party=auth.carrier_company,
                reason="sender domain matches the carrier company on the load",
            )

        factor_toks = company_tokens(auth.factoring_company)
        if factor_toks and any(tok in domain for tok in factor_toks):
            return CheckAuthorizationOutput(
                decision=AuthDecision.FACTORING,
                matched_party=auth.factoring_company,
                reason="sender domain matches the factoring company on file",
            )

        return CheckAuthorizationOutput(
            decision=AuthDecision.DENY,
            matched_party=None,
            reason="sender does not match any authorized party for this load",
        )


# ---------------------------------------------------------------------------
# carrier_cross_check
# ---------------------------------------------------------------------------
class CarrierCrossCheckInput(BaseModel):
    load_id: str
    system: System


class CarrierCrossCheckOutput(BaseModel):
    ok: bool
    delivered_carrier: str | None = None
    settlement_carrier: str | None = None
    payout_amount: Decimal | None = None
    issues: list[str]


class CarrierCrossCheck(Tool):
    """Cross-check delivered carrier vs settlement carrier; ignore canceled rows (§4.2)."""

    name = "carrier_cross_check"
    description = (
        "Corroborate the paying carrier across dispatch (Delivered row only) and "
        "settlement. Flags mismatches, empty settlement, and ignored canceled rows."
    )
    input_model = CarrierCrossCheckInput

    def run(self, params: BaseModel, ctx: ToolContext) -> CarrierCrossCheckOutput:
        assert isinstance(params, CarrierCrossCheckInput)
        if params.system is not System.TRANSPORT_PRO:
            raise ToolError("carrier_cross_check is Transport Pro only in this slice")

        dispatch = ctx.tp.get_dispatch_history(params.load_id)
        settlement = ctx.tp.get_settlement_entries(params.load_id)
        issues: list[str] = []

        delivered = next((r for r in dispatch if r.is_delivered and not r.is_canceled), None)
        if any(r.is_canceled for r in dispatch):
            issues.append("canceled_row_ignored")

        delivered_carrier = delivered.carrier_name if delivered else None
        payout = delivered.freight_bill if delivered else None

        settlement_carrier = next((e.carrier_name for e in settlement if e.carrier_name), None)
        if not settlement:
            issues.append("settlement_empty")

        mismatch = bool(
            delivered_carrier
            and settlement_carrier
            and delivered_carrier.strip().casefold() != settlement_carrier.strip().casefold()
        )
        if mismatch:
            issues.append("mismatch")

        if delivered_carrier:
            ctx.ledger.record_text(
                "carrier", delivered_carrier, self.name, load_id=params.load_id
            )
        if payout is not None:
            ctx.ledger.record_amount(payout, self.name, load_id=params.load_id)

        return CarrierCrossCheckOutput(
            ok=not mismatch,
            delivered_carrier=delivered_carrier,
            settlement_carrier=settlement_carrier,
            payout_amount=payout,
            issues=issues,
        )


# ---------------------------------------------------------------------------
# compute_scheduled_pay_date
# ---------------------------------------------------------------------------
class ComputeScheduledPayDateInput(BaseModel):
    estimated_payment_date: str | None = None
    actual_payment_date: str | None = None
    tz: str = "EDT"
    load_id: str | None = None


class ComputeScheduledPayDateOutput(BaseModel):
    ok: bool = True
    scheduled_pay_date: date
    basis: str
    estimated_weekday: str
    rule_applied: str


class ComputeScheduledPayDate(Tool):
    """Resolve the Monday/Thursday scheduled pay date deterministically (§4.1.1)."""

    name = "compute_scheduled_pay_date"
    description = (
        "Given an earning line's estimated (and optional actual) payment date, return the "
        "carrier-facing pay date via the Monday/Thursday rule. Never guess dates yourself; "
        "always call this."
    )
    input_model = ComputeScheduledPayDateInput

    def run(self, params: BaseModel, ctx: ToolContext) -> ComputeScheduledPayDateOutput:
        assert isinstance(params, ComputeScheduledPayDateInput)
        estimated = _parse_pay_date(params.estimated_payment_date)
        actual = _parse_pay_date(params.actual_payment_date)
        try:
            result = domain_scheduled_pay_date(
                estimated_payment_date=estimated, actual_payment_date=actual
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        ctx.ledger.record_date(
            result.scheduled_pay_date,
            self.name,
            load_id=params.load_id,
            kind="scheduled_pay_date",
        )
        return ComputeScheduledPayDateOutput(
            scheduled_pay_date=result.scheduled_pay_date,
            basis=result.basis.value,
            estimated_weekday=result.estimated_weekday,
            rule_applied=result.rule_applied,
        )


# ---------------------------------------------------------------------------
# classify_intent
# ---------------------------------------------------------------------------
class ClassifyIntentInput(BaseModel):
    email_subject: str = ""
    email_body: str = ""
    thread_text: str = ""


class ClassifyIntentOutput(BaseModel):
    ok: bool = True
    intents: list[Intent]
    confidence: float
    secondary_asks: list[str]


_RATE_SIGNALS = (
    "rate verification", "verify the rate", "verify rate", "confirm the rate", "confirm rate",
    "rate con", "rate confirmation", "rate agreement", "advance", "advances", "deduction",
    "deductions", "chargeback", "charge back", "short pay", "short-pay", "shortpay", "fees",
    "claim", "claims", "confirm noa", "notice of assignment", "factoring",
)  # fmt: skip
_PAYMENT_SIGNALS = (
    "payment status", "when will i be paid", "when do i get paid", "get paid", "estimated payment",
    "estimated pay", "pay date", "payment date", "settle date", "settlement date",
    "missing payment", "haven't been paid", "have not been paid", "not been paid",
    "still waiting on payment", "when is payment",
)  # fmt: skip
_PAPERWORK_SIGNALS = ("pod", "bol", "proof of delivery", "bill of lading", "paperwork")


class ClassifyIntent(Tool):
    """Deterministic keyword classifier for email intent (§4.2).

    Keyword-based on purpose: routing must be auditable and cheap. A production build may
    add a cheap-model classifier (§8.1.1) behind the same output shape for fuzzier mail.
    """

    name = "classify_intent"
    description = (
        "Classify an email as payment_status and/or rate_verification (or uncertain), with "
        "a confidence and any secondary asks."
    )
    input_model = ClassifyIntentInput

    def run(self, params: BaseModel, ctx: ToolContext) -> ClassifyIntentOutput:
        assert isinstance(params, ClassifyIntentInput)
        text = f"{params.email_subject}\n{params.email_body}\n{params.thread_text}".lower()

        has_rate = any(sig in text for sig in _RATE_SIGNALS)
        has_payment = any(sig in text for sig in _PAYMENT_SIGNALS)

        intents: list[Intent] = []
        if has_payment:
            intents.append(Intent.PAYMENT_STATUS)
        if has_rate:
            intents.append(Intent.RATE_VERIFICATION)
        if not intents:
            intents.append(Intent.UNCERTAIN)

        secondary: list[str] = []
        if _NOA_ACTION_RE.search(f"{params.email_subject}\n{params.email_body}"):
            secondary.append("factoring_setup")
        if any(sig in text for sig in _PAPERWORK_SIGNALS):
            secondary.append("paperwork_receipt")

        confidence = 0.9 if len(intents) == 1 and intents[0] is not Intent.UNCERTAIN else (
            0.6 if has_rate and has_payment else 0.3
        )
        return ClassifyIntentOutput(intents=intents, confidence=confidence, secondary_asks=secondary)


# ---------------------------------------------------------------------------
# compute_carrier_rate
# ---------------------------------------------------------------------------
class ComputeCarrierRateInput(BaseModel):
    load_id: str


class RateLine(BaseModel):
    title: str
    amount: Decimal


class RateDeductionLine(BaseModel):
    title: str
    amount: Decimal
    reason: str


class ComputeCarrierRateOutput(BaseModel):
    ok: bool = True
    load_id: str
    gross_rate: Decimal
    total_deductions: Decimal
    net_rate: Decimal
    earnings_breakdown: list[RateLine]
    deductions: list[RateDeductionLine]


class ComputeCarrierRate(Tool):
    """Deterministic carrier rate = sum(earnings) - sum(deductions) for a load (§4.1.1).

    This tool sources the earning and deduction lines from Transport Pro **by load id** —
    it does not accept model-supplied numbers. That is a deliberate strengthening of the
    §4.2 contract: it guarantees the rate is computed from authoritative data, so nothing
    the model relays can distort the sum (grounding integrity, §5).
    """

    name = "compute_carrier_rate"
    description = (
        "Compute a load's carrier rate deterministically: gross = sum of earnings, minus "
        "each deduction (reported with its reason), giving the net. Sourced from Transport "
        "Pro by load id; never pass your own numbers."
    )
    input_model = ComputeCarrierRateInput

    def run(self, params: BaseModel, ctx: ToolContext) -> ComputeCarrierRateOutput:
        assert isinstance(params, ComputeCarrierRateInput)
        load = ctx.tp.get_load(params.load_id)
        load_id = load.load_id_str
        rate = domain_carrier_rate(earnings=load.earnings, deductions=load.deductions)

        ctx.ledger.record_amount(rate.gross_rate, self.name, load_id=load_id)
        ctx.ledger.record_amount(rate.net_rate, self.name, load_id=load_id)
        for line in rate.earnings_breakdown:
            ctx.ledger.record_amount(line.amount, self.name, load_id=load_id)
        for ded in rate.deductions:
            ctx.ledger.record_amount(ded.amount, self.name, load_id=load_id)

        return ComputeCarrierRateOutput(
            load_id=load_id,
            gross_rate=rate.gross_rate,
            total_deductions=rate.total_deductions,
            net_rate=rate.net_rate,
            earnings_breakdown=[RateLine(title=e.title, amount=e.amount) for e in rate.earnings_breakdown],
            deductions=[
                RateDeductionLine(title=d.title, amount=d.amount, reason=d.reason)
                for d in rate.deductions
            ],
        )


# --- small helpers ----------------------------------------------------------
def _dedupe(items: Iterable[object]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        key = str(item).strip()
        if key and key not in seen:
            seen[key] = None
    return list(seen)


def _money(token: str) -> Decimal:
    return Decimal(token.replace("$", "").replace(",", "").strip())


def _add(flags: list[SensitiveFlag], flag: SensitiveFlag) -> None:
    if flag not in flags:
        flags.append(flag)
