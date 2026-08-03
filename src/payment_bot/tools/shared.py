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
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field

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

#: Domains where an address proves nothing about organisation membership.
#:
#: Domain-level contact matching (see ``CheckAuthorization``) must never extend to these:
#: a carrier whose contact on file is ``owner@gmail.com`` does not make every gmail.com
#: sender an authorized party. Not hypothetical — probed on live loads, gmail.com,
#: hotmail.com and bellsouth.net each appear as the *only* contact domain on real carrier
#: records.
_FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "rocketmail.com",
        "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com",
        "me.com", "mac.com", "protonmail.com", "proton.me", "mail.com", "gmx.com",
        "gmx.net", "zoho.com", "comcast.net", "att.net", "verizon.net", "sbcglobal.net",
        "bellsouth.net", "cox.net", "charter.net", "earthlink.net",
    }
)  # fmt: skip

_LOAD_ID_RE = re.compile(r"\b\d{6,7}\b")


def _coerce_id_to_str(value: object) -> object:
    """Accept a numeric id and stringify it.

    Live models pass ``load_id`` as an integer despite the schema saying string — observed
    burning three failed ``compute_scheduled_pay_date`` calls in one run before the model
    stumbled onto quoting it. Pydantic v2 does not coerce int → str on its own, and a type
    mismatch this trivial is not worth an agent iteration.
    """

    return str(value) if isinstance(value, int) else value


#: A load id argument: string, but tolerant of the model sending a bare integer.
LoadIdStr = Annotated[str, BeforeValidator(_coerce_id_to_str)]

#: Labels that mean the following 6-7 digit number is NOT a load id.
#:
#: Carrier mail is full of 6-7 digit numbers that have nothing to do with loads. A live email
#: titled "Payment Status: Load#2433209" escalated as a QuickBooks load because it also said
#: "RTS Financial Service P.O. Box 840267" — the mailing address was read as a load, and one
#: non-Transport-Pro id stops the whole email. MC numbers sit next to carrier names for the
#: same reason.
#:
#: Deliberately excludes "ref"/"reference": factoring templates write the load itself as
#: "Reference#: 2520504".
_NOT_A_LOAD_LABEL_RE = re.compile(
    r"(?:p\.?\s*o\.?\s*box|\bpob\b|\bbox|\bmc\b|\bmc[#-]|\bdot\b|\bsuite\b|\bste\b|\bphone\b"
    r"|\btel\b|\bfax\b|\bext\b|\bzip\b)\W{0,4}$",
    re.IGNORECASE,
)

#: Corporate suffixes that mean the PRECEDING number is a company registration, not a load.
#:
#: Numbered companies are everywhere in trucking, and their registration numbers are load-id
#: shaped. Observed on live mail: "KARNAL FREIGHT SYSTEM O/B 9591699 CANADA INC. (USD)" put a
#: phantom 7-digit "load" on an answerable email — the authorization pre-check then burned a
#: Transport Pro call on it and got HTTP 400 — and "CARRIER 10422126 CANADA INC DBA …" is the
#: same shape. The prefix labels above cannot catch these: the tell sits AFTER the number.
_NOT_A_LOAD_SUFFIX_RE = re.compile(
    r"^\W{0,4}(?:(?:canada|ontario|quebec|alberta|manitoba|saskatchewan|b\.?c\.?|usa)\s+)?"
    r"(?:inc\b|incorporated\b|ltd\b|limited\b|llc\b|corp\b|corporation\b)",
    re.IGNORECASE,
)


#: A URL, to be blanked before id scanning. Numbers inside links are never loads —
#: observed live: iThrive's signature carries ``linkedin.com/company/6425192`` and every
#: email they sent grew a phantom 7-digit "load" that Transport Pro 400'd on.
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _load_ids_in(text: str) -> list[str]:
    """6-7 digit ids in ``text``, skipping ones a nearby label disqualifies."""

    text = _URL_RE.sub(" ", text)
    found: list[str] = []
    for match in _LOAD_ID_RE.finditer(text):
        before = text[max(0, match.start() - 24) : match.start()]
        if _NOT_A_LOAD_LABEL_RE.search(before):
            continue
        after = text[match.end() : match.end() + 24]
        if _NOT_A_LOAD_SUFFIX_RE.search(after):
            continue
        found.append(match.group())
    return found
_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
_INVOICE_RE = re.compile(r"invoice\s*(?:no\.?|number|#)?\s*:?\s*(\d{3,})", re.IGNORECASE)
_COMPANY_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&'.\- ]{2,40}?,?\s+(?:Inc|LLC|L\.L\.C\.|Incorporated|Corp|Corporation|Co|Ltd)\b\.?)"
)
_COLUMN_HINT_RE = re.compile(
    r"reference\s*#|p\.?\s*o\.?\s*(?:number|#)|load\s*#|invoice\s*#|pro\s*#", re.IGNORECASE
)


#: Argument descriptions reach the model as JSON Schema, and an undescribed argument is one it
#: has to guess. Measured on live mail: `compute_scheduled_pay_date` failed seven times and
#: `carrier_cross_check` three, each burning an iteration, purely because the expected shape
#: was never stated. Only 3 of 20 arguments carried a description.
_LOAD_ID_FIELD = Field(
    description=(
        "The 6 or 7 digit load id on its own, digits only — e.g. 2462934. Not an MC number, "
        "not an invoice number, no prefix."
    )
)
_SYSTEM_FIELD = Field(
    description=(
        "Which system holds the load, taken from the routing map in the intake message: "
        "'transport_pro' for 7-digit ids, 'quickbooks' for 6-digit."
    )
)


def _sender_domain(sender_email: str) -> str:
    """The registrable domain of an address, lowercased. Empty when there isn't one."""

    return sender_email.rsplit("@", 1)[-1].strip().lower() if "@" in sender_email else ""


#: Tokens generic to the factoring industry's names. An overlap on one of these links
#: nothing: "Apex Capital" and "Alta Capital" share "capital" and are different companies,
#: and answering one about the other's load is exactly the disclosure the authorization
#: check exists to prevent. Kept separate from ``_STOPWORDS`` because the carrier-name
#: match may tolerate these words while a factor-name LINK must not.
_FACTOR_GENERIC_TOKENS = frozenset(
    {
        "factoring", "factors", "factor", "financial", "finance", "funding", "funds",
        "capital", "credit", "bank", "banking", "solutions", "partners", "partner",
        "commercial", "business", "payment", "payments", "national", "american",
        "united", "trust", "advance",
    }
)  # fmt: skip


def _factor_names_match(configured_name: str, on_file: str) -> bool:
    """Does a configured factor entry name the factor recorded on the load?

    The roster is generated from the settlement export's ``payName`` while the load
    carries the remit-to company name, and the two spell the same factor differently —
    "BUSBOT INCORPORATED DBA AXLE" in the export is "Axle Payments" on the load. A strict
    substring test missed those, so a curated, correct domain entry still escalated.

    Accepted links: containment in either direction, or overlap on a distinctive name
    token. Industry-generic tokens (capital, financial, funding…) never link on their
    own. The sender's domain equality stays exact regardless — this only decides which
    roster entries are eligible to vouch for that domain.
    """

    key = configured_name.strip().lower()
    name = on_file.strip().lower()
    if not key or not name:
        return False
    if key in name or name in key:
        return True
    return bool(
        (company_tokens(key) - _FACTOR_GENERIC_TOKENS)
        & (company_tokens(name) - _FACTOR_GENERIC_TOKENS)
    )


def _is_configured_factor_domain(
    factoring_company: str, sender_email: str, ctx: ToolContext
) -> bool:
    """True when the sender's domain is configured for this load's factoring company.

    Matched on the whole domain, never a substring, and the configured factor name must
    match the company recorded on the load (see :func:`_factor_names_match`) — so an
    entry for "rts financial" answers for "RTS Financial Service, Inc" but not for an
    unrelated factor.
    """

    domain = _sender_domain(sender_email)
    if not domain:
        return False
    for configured_name, domains in ctx.settings.factoring_domains.items():
        if not _factor_names_match(configured_name, factoring_company):
            continue
        if any(domain == str(d).strip().lower().lstrip("@") for d in domains):
            return True
    return False


def _roster_entry_for_domain(sender_domain: str, ctx: ToolContext) -> str | None:
    """The roster entry (configured factor name) that owns ``sender_domain``, if any.

    Membership only — no per-load factor comparison. Used by the pre-NOA path, where the
    load has no factor on file to compare against.
    """

    for configured_name, domains in ctx.settings.factoring_domains.items():
        if any(sender_domain == str(d).strip().lower().lstrip("@") for d in domains):
            return configured_name
    return None


def company_tokens(name: str | None) -> set[str]:
    """Distinctive lowercase tokens of a company name (len ≥ 4, minus generic words)."""

    if not name:
        return set()
    tokens = re.split(r"[^a-z0-9]+", name.lower())
    return {t for t in tokens if len(t) >= 4 and t not in _STOPWORDS}


#: Spelled-out date forms accepted in addition to ISO.
#:
#: Only formats whose month is a NAME. Every one of these is unambiguous, unlike "07/29/2026"
#: — which could be July 29 or 29 July depending on locale, and where guessing wrong would
#: silently move a payment date. Those stay rejected.
#:
#: This leniency exists because the skill prompt instructs the model to *write* dates as
#: "Thursday, August 20, 2026", and on live mail it then passed that form back as a tool
#: argument. Describing the schema helped but did not settle it: one run made six clean calls
#: and the next failed seven times on the same email. Accepting what the model actually
#: produces removes the failure mode instead of hoping it reads the schema.
_SPELLED_DATE_FORMATS = (
    "%A, %B %d, %Y",  # Thursday, August 20, 2026
    "%A %B %d, %Y",  # Thursday August 20, 2026
    "%B %d, %Y",  # August 20, 2026
    "%b %d, %Y",  # Aug 20, 2026
    "%d %B %Y",  # 20 August 2026
    "%d %b %Y",  # 20 Aug 2026
)


def _parse_pay_date(value: str | None) -> date | None:
    """Parse an API or model-supplied pay date into an EDT calendar date.

    Bare ``YYYY-MM-DD`` values are calendar dates (no shift). Full timestamps are
    converted from their offset into EDT (fixed UTC-4) before the date is taken. Dates with a
    named month are also accepted — see :data:`_SPELLED_DATE_FORMATS`.
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
    except ValueError as iso_error:
        for fmt in _SPELLED_DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        raise ToolError(
            f"invalid date {value!r}: {iso_error}. Use ISO YYYY-MM-DD, exactly as the load "
            "summary returned it."
        ) from iso_error


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
    #: Text extracted from spreadsheet attachments (xlsx/csv). Carriers send statements
    #: whose load ids appear nowhere in the body; this is where they surface. Feeds
    #: identifier extraction only — never the sensitive-change scan.
    attachments_text: str = ""


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
        text = "\n".join(
            p
            for p in (params.subject, params.body, params.thread_text, params.attachments_text)
            if p
        )

        load_ids = _dedupe(_load_ids_in(text))
        invoice_numbers = _dedupe(_INVOICE_RE.findall(text))

        stated_rates: list[StatedRate] = []
        for line in text.splitlines():
            amounts = [_money(m) for m in _MONEY_RE.findall(line)]
            if not amounts:
                continue
            line_ids = _load_ids_in(line)
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
    load_id: LoadIdStr


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
    #: True when the evidence is unambiguous — an explicit request phrase ("update bank",
    #: "void check"), a bank/NOA attachment, an NOA action, a contact change, or a
    #: "confirm the change was made" ask. False when the ONLY evidence is proximity
    #: phrasing (a change word near a payment noun) — the shape of factoring template
    #: boilerplate ("Please ensure remittance is updated to OTR Solutions…"), which per
    #: ESCALATIONS.md §7 may proceed when the email also carries an answerable ask, with
    #: the gate enforcing that the reply never acknowledges the instruction.
    hard: bool = True
    #: Per-category hard WORDING, so each policy switch governs exactly its own language:
    #: ``sensitive_bank_replies`` may draft past ``hard_bank`` (explicit bank/ACH
    #: instructions), ``sensitive_noa_replies`` past ``hard_noa`` (NOA action wording).
    hard_bank: bool = False
    hard_noa: bool = False
    #: True when the email carries an ARTIFACT or identity action rather than language:
    #: a void-check / direct-deposit / NOA attachment, or a contact change. These always
    #: escalate — there is paperwork to file or an identity to re-verify, and a status
    #: reply cannot do either — regardless of any wording policy.
    paperwork: bool = False


# Phrase → flag. NOA/factoring only escalates on an action verb (add/update/attach…),
# so those are handled separately below rather than by bare keyword.
#
# These are split by whether the phrase is self-evidently a *request*. "update bank" is one
# whatever surrounds it. "account number" is not — it appears in every remit-to footer and
# every invoice. Escalating on the bare nouns meant a carrier asking only "what is the rate
# on load X" got refused because the factor's standard payment block sat in the signature.
#: Phrases that are a change request on their own.
_BANK_REQUEST_PHRASES = (
    "bank change", "change bank", "update bank", "new bank", "banking information",
    "update payment info", "change payment", "void check", "voided check",
)  # fmt: skip
#: Payment-detail nouns. Only a request when a change word sits near them.
#:
#: Bare "bank" belongs here rather than in the request list: "update our bank account" must
#: escalate, while "Bank Name: Fifth Third Bank" in a remit-to footer must not. Requiring a
#: nearby change word is what separates those.
_BANK_DETAIL_PHRASES = (
    "routing number", "account number", "ach", "direct deposit", "remittance",
    "payment method", "remit to", "remit-to", "bank", "bank account", "bank details",
)  # fmt: skip
#: Words that turn a payment-detail mention into an instruction aimed at us.
_CHANGE_WORDS = (
    "update", "updated", "change", "changed", "revise", "revised", "switch", "switched",
    "correct", "corrected", "new", "different", "going forward", "effective", "instead",
    "no longer", "replace", "redirect", "moving forward",
    # "…to the account below" / "…as follows" mean details are being supplied in this email,
    # which is the actual shape of a redirect. Kept narrow on purpose: adding a verb like
    # "send" would re-catch ordinary asks such as "send us the remittance advice".
    "below", "as follows", "following",
)  # fmt: skip
#: A change word within ~8 words of a payment detail, split by ORDER, because order is
#: what separates an instruction from boilerplate (§7):
#:
#: * verb first — "please **update our bank** account number" — acts ON the detail; an
#:   instruction aimed at us. Always HARD.
#: * detail first — "remittance **is updated** to OTR Solutions" — passive template
#:   wording describing the sender's own arrangement. SOFT: may proceed when the email
#:   also asks something answerable, with the gate policing the draft.
#:
#: The ``\b`` anchors are load-bearing. Without them "ach" matched inside "e**ach**", so
#: "the status of each load listed below" read as a request to redirect payment by ACH.
_CHANGES_ALT = "|".join(re.escape(w) for w in _CHANGE_WORDS)
_DETAILS_ALT = "|".join(re.escape(d) for d in _BANK_DETAIL_PHRASES)
_BANK_CHANGE_ACTIVE_RE = re.compile(
    rf"\b(?:{_CHANGES_ALT})\b\W(?:\w+\W){{0,8}}?\b(?:{_DETAILS_ALT})\b", re.IGNORECASE
)
_BANK_CHANGE_PASSIVE_RE = re.compile(
    rf"\b(?:{_DETAILS_ALT})\b\W(?:\w+\W){{0,8}}?\b(?:{_CHANGES_ALT})\b", re.IGNORECASE
)
#: Either order — used by the gate's change_acknowledgment check on DRAFT text, where any
#: shape of change wording is disqualifying.
_BANK_CHANGE_REQUEST_RE = re.compile(
    rf"(?:{_BANK_CHANGE_ACTIVE_RE.pattern}|{_BANK_CHANGE_PASSIVE_RE.pattern})", re.IGNORECASE
)
_CONTACT_PHRASES = (
    "change email", "update email", "new email address", "change our email",
    "update contact", "new contact email", "change of email",
)  # fmt: skip

#: "Confirm the change has been made" — asks us to *ratify* a redirect, which is always a
#: hard escalation (§7): "please confirm that the payment remit address has been updated".
#:
#: A CHANGE word inside the clause is required, and only the strong ones (not the
#: positional "below"/"following"). "Confirm payment going to Wex Bank P.O. Box 94565" is
#: a factor VERIFYING its existing remit address — the single most common payment-inquiry
#: template shape — and an earlier version of this pattern (confirm + any payment noun)
#: escalated it on every email WEX ever sent.
_STRONG_CHANGE_WORDS = tuple(
    w for w in _CHANGE_WORDS if w not in ("below", "as follows", "following")
)
_STRONG_CHANGES_ALT = "|".join(re.escape(w) for w in _STRONG_CHANGE_WORDS)
#: Bare nouns are fine here — unlike the proximity scan, this pattern also demands a
#: strong change word in the same clause, so "confirm ... account" alone cannot fire.
_CONFIRM_DETAILS_ALT = (
    r"remit(?:tance)?|bank|account|payment\s+method|noa|notice\s+of\s+assignment"
)
_CONFIRM_CHANGE_RE = re.compile(
    rf"\bconfirm\w*\b\W(?:\w+\W){{0,10}}?"
    rf"(?:\b(?:{_STRONG_CHANGES_ALT})\b\W(?:\w+\W){{0,6}}?\b(?:{_CONFIRM_DETAILS_ALT})\b"
    rf"|\b(?:{_CONFIRM_DETAILS_ALT})\b\W(?:\w+\W){{0,6}}?\b(?:{_STRONG_CHANGES_ALT})\b)",
    re.IGNORECASE,
)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Match ``phrase`` only as whole words.

    Plain substring matching made short phrases catastrophically broad: ``"ach"`` matched
    inside "att**ach**ed", "e**ach**" and "re**ach**", so "please see attached invoice"
    escalated as a suspected bank-change request. Measured on live mail, that single phrase
    accounted for a third of all escalations, four of them with no other signal present.
    """

    return re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE)


#: Phrases paired with their whole-word patterns, so evidence still names the phrase.
_BANK_REQUEST_PATTERNS = tuple((p, _phrase_pattern(p)) for p in _BANK_REQUEST_PHRASES)
_CONTACT_PATTERNS = tuple((p, _phrase_pattern(p)) for p in _CONTACT_PHRASES)

#: Start of a quoted reply / forwarded block. Everything from here on was written by someone
#: else, earlier — usually us.
_QUOTE_MARKERS = (
    re.compile(r"^\s*>", re.MULTILINE),
    re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-+\s*Original Message\s*-+\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-+\s*Forwarded message\s*-+\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*From:.{0,80}Sent:", re.MULTILINE | re.IGNORECASE | re.DOTALL),
)


def strip_quoted(body: str) -> str:
    """Return only the part of ``body`` the sender wrote in *this* message.

    A change request lives in what someone just wrote, not in the thread they quoted. Two
    live emails escalated on ``"direct deposit"`` that appeared solely inside our own earlier
    reply, quoted back:

        > Settle Date 07/20/2026
        > Amount $427.50
        > Payment Method Direct deposit

    Nobody was requesting anything. Scanning quoted history means every mention of a payment
    detail keeps re-escalating the thread for as long as it stays alive.
    """

    earliest = len(body)
    for marker in _QUOTE_MARKERS:
        found = marker.search(body)
        if found is not None:
            earliest = min(earliest, found.start())
    return body[:earliest]
#: An NOA/factoring *action* — verb near the noun. Both sides are word-bounded, and the
#: verbs spell out their inflections rather than substring-matching them: without the
#: boundaries, "Al**add**in Factoring" — a real factor's signature — matched (`add` inside
#: the name, `Factor` within 30 chars), so every email that company ever sent escalated as
#: an NOA change. Same bug class as "ach" inside "attached", fixed the same way.
_NOA_ACTION_RE = re.compile(
    # "assignment" is deliberately NOT a verb form: it is the noun in "notice of
    # assignment", and including it made the phrase "notice of assignment or factoring"
    # match itself (assignment → verb, factoring → noun) in a draft that was *reporting*
    # nothing is on file.
    r"\b(?:add(?:ed|ing)?|attach(?:ed|ing|ment)?|updat(?:e|ed|ing)|chang(?:e|ed|ing)"
    r"|set\s*up|setup|assign(?:ed|ing)?|register(?:ed|ing)?|remov(?:e|ed|ing)"
    r"|releas(?:e|ed|ing))\b\D{0,30}"
    r"\b(?:noa|notice\s+of\s+assignment|factor(?:ing|s)?)\b",
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
        # Only what the sender wrote in this message, never the quoted thread below it.
        written = f"{params.subject}\n{strip_quoted(params.body)}"
        haystack = written.lower()
        flags: list[SensitiveFlag] = []
        evidence: list[str] = []
        hard_bank = False
        hard_noa = False

        for phrase, pattern in _BANK_REQUEST_PATTERNS:
            if pattern.search(haystack):
                _add(flags, SensitiveFlag.BANK_CHANGE)
                evidence.append(f"bank: matched {phrase!r}")
                hard_bank = True  # a request phrase is self-evidently an instruction

        # A payment-detail noun alone is not a request — a change word must sit near it.
        # Verb-first ("update our bank…") is an instruction aimed at us: HARD. Detail-first
        # ("remittance is updated to X") is passive template boilerplate: the one SOFT
        # signal.
        for match in _BANK_CHANGE_ACTIVE_RE.finditer(written):
            _add(flags, SensitiveFlag.BANK_CHANGE)
            evidence.append(f"bank: change instructed — {' '.join(match.group(0).split())!r}")
            hard_bank = True
        for match in _BANK_CHANGE_PASSIVE_RE.finditer(written):
            _add(flags, SensitiveFlag.BANK_CHANGE)
            evidence.append(f"bank: change requested — {' '.join(match.group(0).split())!r}")

        # Asking us to *ratify* a change is hard, whatever shape the wording takes.
        if _CONFIRM_CHANGE_RE.search(written):
            _add(flags, SensitiveFlag.BANK_CHANGE)
            evidence.append("bank: asks to confirm a change was made")
            hard_bank = True

        paperwork = False
        for match in _NOA_ACTION_RE.finditer(written):
            _add(flags, SensitiveFlag.NOA_SETUP_CHANGE)
            evidence.append(f"noa_setup: matched {match.group(0).strip()!r}")
            hard_noa = True

        for phrase, pattern in _CONTACT_PATTERNS:
            if pattern.search(haystack):
                _add(flags, SensitiveFlag.EMAIL_CONTACT_CHANGE)
                evidence.append(f"contact: matched {phrase!r}")
                paperwork = True

        for att in params.attachments_metadata:
            lower = att.filename.lower()
            if any(k in lower for k in ("voidcheck", "void_check", "void-check", "directdeposit")):
                _add(flags, SensitiveFlag.BANK_CHANGE)
                evidence.append(f"bank: attachment {att.filename!r}")
                paperwork = True
            if "noa" in lower or "assignment" in lower:
                _add(flags, SensitiveFlag.NOA_SETUP_CHANGE)
                evidence.append(f"noa_setup: attachment {att.filename!r}")
                paperwork = True

        if not flags:
            flags.append(SensitiveFlag.NONE)
            action = SensitiveAction.CONTINUE
        else:
            action = SensitiveAction.ESCALATE

        return DetectSensitiveChangeOutput(
            flags=flags,
            evidence=evidence,
            action=action,
            hard=hard_bank or hard_noa or paperwork,
            hard_bank=hard_bank,
            hard_noa=hard_noa,
            paperwork=paperwork,
        )


# ---------------------------------------------------------------------------
# check_authorization
# ---------------------------------------------------------------------------
class CheckAuthorizationInput(BaseModel):
    sender_email: str = Field(
        description="The sender's email address exactly as it appeared in the From header."
    )
    sender_name: str | None = Field(
        default=None, description="The sender's display name from the From header, if any."
    )
    load_id: LoadIdStr = _LOAD_ID_FIELD
    system: System = _SYSTEM_FIELD


class CheckAuthorizationOutput(BaseModel):
    ok: bool = True
    decision: AuthDecision
    #: The policy-resolved verdict the agent acts on: true when this sender may receive
    #: disclosure about this load — ALLOW, or FACTORING when configuration permits
    #: answering the factoring company on file. The skill prompts key off this field, not
    #: ``decision``, so the model never has to know the deployment's factoring policy.
    #: Observed before this existed: a factoring sender the pipeline had authorized was
    #: refused by the model ("unable to provide rate details due to authorization
    #: restrictions") because the prompt said only ALLOW counts.
    authorized: bool = False
    #: True when the sender is a roster-verified factoring company asking about a load
    #: that shows NO factor on file — the standard pre-funding flow where a factor
    #: verifies the rate BEFORE its NOA reaches us. The reply must then request the NOA
    #: and billing paperwork be emailed to the documents address.
    pre_noa: bool = False
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
                authorized=True,
                matched_party=auth.carrier_company,
                reason="sender is an explicitly authorized contact for this load",
            )
        if sender in {e.lower() for e in auth.factoring_emails}:
            return CheckAuthorizationOutput(
                decision=AuthDecision.FACTORING,
                authorized=ctx.settings.allow_factoring,
                matched_party=auth.factoring_company,
                reason="sender is the factoring company on file",
            )

        # Same organisation as an explicitly authorized contact. Carriers write from several
        # mailboxes at one domain — accounting@ asks about the load whose dispatch@ is on
        # file — and requiring the exact address denied them (observed live: load 2480109,
        # contact on file at sky-expressllc.com, payment question from accounting@ there).
        # The domain must match exactly, and free-mail providers are excluded: an address at
        # a shared provider proves nothing about who the sender works for.
        sender_domain = _sender_domain(sender)
        if sender_domain and sender_domain not in _FREE_MAIL_DOMAINS:
            contact_domains = {_sender_domain(e) for e in auth.authorized_emails}
            if sender_domain in contact_domains:
                return CheckAuthorizationOutput(
                    decision=AuthDecision.ALLOW,
                    authorized=True,
                    matched_party=auth.carrier_company,
                    reason="sender's domain matches an authorized contact's domain on this load",
                )

        # A factoring sender the operator has explicitly vouched for. Two independent
        # conditions: this load really is factored, and the sender's exact registrable domain
        # is configured for *that* factor — so RTS cannot be answered about an OTR-factored
        # load. This is the only path that yields FACTORING, because it is the only one that
        # is safe to switch on via `allow_factoring`.
        if auth.factoring_company and _is_configured_factor_domain(
            auth.factoring_company, sender, ctx
        ):
            return CheckAuthorizationOutput(
                decision=AuthDecision.FACTORING,
                authorized=ctx.settings.allow_factoring,
                matched_party=auth.factoring_company,
                reason="sender domain is configured for the factoring company on this load",
            )

        # Pre-NOA (policy): a roster-verified factor asking about a load that shows NO
        # factor on file — the standard pre-funding flow, where the factor verifies the
        # rate BEFORE its NOA reaches us and Transport Pro still says remit-to self. The
        # reply answers the rate question and requests the NOA + billing paperwork. A load
        # already factored to a DIFFERENT company never reaches this branch (the check
        # above owns that case), so one factor is still never told about another's load.
        if (
            ctx.settings.factoring_prenoa_replies
            and not auth.factoring_company
            and sender_domain
            and sender_domain not in _FREE_MAIL_DOMAINS
        ):
            roster_name = _roster_entry_for_domain(sender_domain, ctx)
            if roster_name is not None:
                return CheckAuthorizationOutput(
                    decision=AuthDecision.FACTORING,
                    authorized=ctx.settings.allow_factoring,
                    pre_noa=True,
                    matched_party=roster_name,
                    reason=(
                        "roster-verified factoring company; no factor on file for this "
                        "load — the reply should request the NOA and billing paperwork"
                    ),
                )

        carrier_toks = company_tokens(auth.carrier_company)
        if any(tok in domain for tok in carrier_toks):
            return CheckAuthorizationOutput(
                decision=AuthDecision.ALLOW,
                authorized=True,
                matched_party=auth.carrier_company,
                reason="sender domain matches the carrier company on the load",
            )

        # Name-only resemblance to the factor is NOT authorization. It used to return
        # FACTORING, which meant any domain containing "finance" would have been disclosed to
        # the moment `allow_factoring` was enabled. Say so in the reason so a human reviewing
        # the escalation can add the domain to PAYBOT_FACTORING_DOMAINS if it is genuine.
        factor_toks = company_tokens(auth.factoring_company)
        if factor_toks and any(tok in domain for tok in factor_toks):
            return CheckAuthorizationOutput(
                decision=AuthDecision.DENY,
                matched_party=None,
                reason=(
                    f"sender resembles the factoring company on file "
                    f"({auth.factoring_company!r}) but its domain is not configured; add it "
                    "to PAYBOT_FACTORING_DOMAINS if it is genuine"
                ),
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
    load_id: LoadIdStr = _LOAD_ID_FIELD
    system: System = _SYSTEM_FIELD


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
    """Dates in, ISO only.

    The descriptions are load-bearing, not documentation. This tool accepts ISO dates and
    nothing else, and the skill prompt separately tells the model to *write* dates as
    "Thursday, August 20, 2026" — so on live mail it passed that form as an argument and the
    tool rejected it seven times in a row, burning most of the iteration budget before
    stumbling onto a working call. The schema reaches the model; say the format in it.
    """

    estimated_payment_date: str | None = Field(
        default=None,
        description=(
            "ISO date, YYYY-MM-DD (e.g. 2026-07-29). Copy it verbatim from the earning line "
            "returned by tp_get_load_summary. Never a human-readable date."
        ),
    )
    actual_payment_date: str | None = Field(
        default=None,
        description="ISO date, YYYY-MM-DD, when the line is already paid. Same format rule.",
    )
    tz: str = Field(
        default="EDT", description="Timezone for the calendar date. Leave as the default."
    )
    load_id: LoadIdStr | None = Field(
        default=None, description="The load id this earning line belongs to."
    )


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
        "Given an earning line's estimated (and optional actual) payment date as ISO "
        "YYYY-MM-DD, return the carrier-facing pay date via the Monday/Thursday rule. Pass "
        "the dates exactly as tp_get_load_summary returned them. Never guess dates yourself; "
        "always call this."
    )
    input_model = ComputeScheduledPayDateInput

    def run(self, params: BaseModel, ctx: ToolContext) -> ComputeScheduledPayDateOutput:
        assert isinstance(params, ComputeScheduledPayDateInput)
        estimated = _parse_pay_date(params.estimated_payment_date)
        actual = _parse_pay_date(params.actual_payment_date)

        # Input dates must already be grounded (§5). This tool computes from model-supplied
        # arguments and records its result in the ledger — which made an invented input the
        # one way a fabrication could be laundered into "grounded". Observed live on load
        # 2458141: Transport Pro said estimated 2026-08-23 and not paid; the model passed a
        # fabricated actual date of 2026-06-13, and the draft's "Saturday, June 13, 2026"
        # sailed through the gate's date check while its invented amounts were blocked.
        # A legitimate call always passes this check, because tp_get_load_summary grounds
        # every earning line's estimated and actual date as it reads the load.
        for label, value in (
            ("estimated_payment_date", estimated),
            ("actual_payment_date", actual),
        ):
            if value is not None and value not in ctx.ledger.grounded_dates:
                raise ToolError(
                    f"{label} {value.isoformat()} does not match any date a tool returned "
                    "in this run. Call tp_get_load_summary for this load first and copy the "
                    "earning line's dates verbatim — never supply a date of your own."
                )
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
    #: True when the intent came from an actual keyword match, False when it is the
    #: names-a-load fallback. The §7 sensitive-change narrowing keys off this: template
    #: boilerplate may proceed only when the email *asked something answerable in words* —
    #: a bare change instruction with a load number attached must still escalate.
    keyword_grounded: bool = False


#: Phrases that mean "check our rate against yours".
#:
#: Bare "advance" used to be here, and it classified a *sign-off* as a rate request: "Thank
#: you in Advance, ACDS TEAM" scored rate_verification at 0.9 confidence, so the bot asked a
#: carrier chasing payment to supply a rate. "fees" and "claim" were the same shape — words
#: that appear in ordinary payment chatter. Phrases only, and matched as whole words.
_RATE_SIGNALS = (
    "rate verification", "verify the rate", "verify rate", "confirm the rate", "confirm rate",
    "rate con", "rate confirmation", "rate agreement", "advance payment", "payment advance",
    "cash advance", "deduction", "deductions", "chargeback", "charge back", "short pay",
    "short-pay", "shortpay", "confirm noa", "notice of assignment", "factoring",
)  # fmt: skip
_PAYMENT_SIGNALS = (
    "payment status", "when will i be paid", "when do i get paid", "get paid", "estimated payment",
    "estimated pay", "pay date", "payment date", "settle date", "settlement date",
    "missing payment", "haven't been paid", "have not been paid", "not been paid",
    "still waiting on payment", "when is payment",
)  # fmt: skip
_PAPERWORK_SIGNALS = ("pod", "bol", "proof of delivery", "bill of lading", "paperwork")


def _matches_any(text: str, phrases: tuple[str, ...]) -> bool:
    """True when any phrase appears as whole words.

    Substring matching is what let "advance" fire from inside a sign-off. Word boundaries are
    cheap and remove a whole class of misreads.
    """

    return any(re.search(rf"\b{re.escape(phrase)}\b", text) for phrase in phrases)


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
        # What the sender wrote in this message. Quoted history describes an older ask, and a
        # signature is not a request — both used to drive routing.
        written = f"{params.email_subject}\n{strip_quoted(params.email_body)}"
        text = written.lower()

        has_rate = _matches_any(text, _RATE_SIGNALS)
        has_payment = _matches_any(text, _PAYMENT_SIGNALS)

        intents: list[Intent] = []
        if has_payment:
            intents.append(Intent.PAYMENT_STATUS)
        if has_rate:
            intents.append(Intent.RATE_VERIFICATION)
        if not intents and _LOAD_ID_RE.search(written):
            # Names a load but says nothing recognisable — "can anybody update this for me".
            # This inbox exists to answer payment status, so that is the reading, and a human
            # reviews the draft regardless. Escalating a plain question helps nobody.
            intents.append(Intent.PAYMENT_STATUS)
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
        return ClassifyIntentOutput(
            intents=intents,
            confidence=confidence,
            secondary_asks=secondary,
            keyword_grounded=has_rate or has_payment,
        )


# ---------------------------------------------------------------------------
# compute_carrier_rate
# ---------------------------------------------------------------------------
class ComputeCarrierRateInput(BaseModel):
    load_id: LoadIdStr = _LOAD_ID_FIELD


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
