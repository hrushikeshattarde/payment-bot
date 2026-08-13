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
from payment_bot.logging import get_logger
from payment_bot.models import (
    AuthDecision,
    Intent,
    SensitiveAction,
    SensitiveFlag,
    System,
)
from payment_bot.tools.base import Tool, ToolContext

_log = get_logger("tools.shared")

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
#:
#: ``account``/``acct``/``aba``/``routing`` were added after a live block: Engaged Finance's
#: verification email carries its full remittance block, and "Account #2657147" — their ACH
#: account number — was read as a load. Transport Pro 400'd on it, the gate's authorization
#: check failed on the unresolvable id, and the draft told the factoring company "I could not
#: locate load 2657147, please confirm the number" about their own bank account. Every
#: factoring template states remit details this way, so it is a recurring shape, and a
#: coincidental collision with a real load id would be far worse than a 400 — that is exactly
#: how an unrelated carrier's load reached a draft on the RTS enquiry.
#:
#: ``settlement``/``check`` are the collision case realised. Measured across 60 live messages:
#: "Circle Logistics, Inc - Settlement 1311088" from bngtransportation.com, where 1311088 is a
#: real load id belonging to Power Transport, LLC — an unrelated carrier. A settlement number
#: is never a load number here (settlements are their own endpoint), so reading one as a load
#: risks disclosing a third party's load, which is the more serious failure than escalating.
#: The bank labels carry an optional "no."/"number" filler, because remittance blocks write
#: "Account No. 2657147" as readily as "Account #2657147". That filler is NOT a label in its
#: own right and must never become one: "Load No. 2523916" is a load, and suppressing a bare
#: "no." would discard the very ids this tool exists to find.
#:
#: ``chk``/``ck``/``cheque`` are the abbreviations of a label already here, and their absence
#: cost an escalation: an RTS stop-payment notice opened "PLACE STOP PAYMENT ON CHK 787147",
#: and because six digits route to CargoTel the email was refused as spanning two systems —
#: over a check number. Spelled-out "check 787147" had been suppressed since the settlement
#: case above. Payment mail abbreviates by default, so a label list that only knows the long
#: form knows half of it. The same lesson as the factoring acronyms in ``company_acronym``.
#:
#: Still deliberately excludes ``inv``, for the reason ``ref`` is excluded: carriers write
#: "INV 2462934" meaning the load itself, so suppressing it would discard real ids. A check
#: number is different in kind — nobody labels a load "CHK".
_NOT_A_LOAD_LABEL_RE = re.compile(
    r"(?:"
    r"(?:p\.?\s*o\.?\s*box|\bpob\b|\bbox|\bmc\b|\bmc[#-]|\bdot\b|\bsuite\b|\bste\b|\bphone\b"
    r"|\btel\b|\bfax\b|\bext\b|\bzip\b)"
    r"|(?:\bacct\b|\baccount\b|\baba\b|\brouting\b|\bsettlement\b"
    r"|\bcheck\b|\bchecks\b|\bchk\b|\bck\b|\bcheque\b)"
    r"(?:\W{0,3}(?:no|nbr|num|number)\b)?"
    r")\W{0,4}$",
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
        # A load id is a sequence number and never carries a leading zero; a zero-padded
        # one is a reference number formatted to a fixed width. Observed live on a WEX
        # collections table whose Invoice cell read "IN-001208": the 001208 routed to
        # CargoTel while the load beside it was 7-digit, and an answerable single-system
        # email was refused as spanning both.
        #
        # This is deliberately NOT a rule about the "IN-" prefix. Suppressing digits after
        # any letter-hyphen would also drop "INV-2462934", and `inv` must keep meaning the
        # load itself — carriers write it that way, which is exactly why `inv` was left out
        # of _NOT_A_LOAD_LABEL_RE. Padding is the orthogonal signal: no real id has it, and
        # a sender writing "INV 2462934" is unaffected.
        if match.group().startswith("0"):
            continue
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


#: Words a sender uses for OUR load, as a label immediately before the number.
#:
#: ``reference`` belongs here rather than among the sender's own references: factoring
#: templates write the load itself as "Reference#: 2520504", which is why ``ref`` has always
#: been excluded from :data:`_NOT_A_LOAD_LABEL_RE`.
_LOAD_LABEL_RE = re.compile(
    r"\b(?:load|order|pro|trip|reference|ref)\b\W{0,3}(?:no|nbr|num|number)?\W{0,3}(\d{6,7})\b",
    re.IGNORECASE,
)


def _prefer_labelled_loads_across_systems(load_ids: list[str], text: str) -> list[str]:
    """When ids disagree about system, keep the ones the sender CALLED a load.

    Third instance of one shape. A VIP Logistics enquiry read "Load #2513318 / VIP
    #282775-0-A": the first is a Transport Pro load, the second is the sender's own reference,
    six digits, so it routed to CargoTel and the email was refused as spanning both systems.
    OperFi's "Load #: 2485194" beside "OperFi Invoice #: 318354" is the same sentence with
    different nouns, and RTS's "CHK 787147" was the same thing with a label
    :data:`_NOT_A_LOAD_LABEL_RE` now knows.

    Neither existing guard reaches this one. The label is a company abbreviation, so no fixed
    list can hold it — every carrier and factor has its own — and nothing captured the number
    as an invoice, so :func:`_drop_stray_sender_invoice_ids` had no candidate.

    So this works from positive evidence instead: the sender wrote "Load #" in front of one
    number and something else in front of the other. Preferring the labelled one needs no
    knowledge of what the other label meant.

    Deliberately cannot fire on a single-system email, and cannot fire unless a load label is
    actually present. A genuine two-system email where neither id is labelled still escalates,
    which is the case a human must see. Suppressing the *suffix* instead — ``282775-0-A`` — was
    the other candidate and is wrong: a load id is legitimately followed by a hyphen and more,
    as in CargoTel's own ``296006-INVDKD0098``.
    """

    if len(load_ids) < 2:
        return load_ids

    systems = {lid: domain_route_load(lid).system for lid in load_ids}
    if len(set(systems.values())) < 2:
        return load_ids

    labelled = {m.group(1) for m in _LOAD_LABEL_RE.finditer(text)} & set(load_ids)
    if not labelled:
        return load_ids

    keep_systems = {systems[lid] for lid in labelled}
    if len(keep_systems) != 1:
        return load_ids

    wanted = next(iter(keep_systems))
    kept = [lid for lid in load_ids if lid in labelled or systems[lid] is wanted]
    dropped = [lid for lid in load_ids if lid not in kept]
    if dropped:
        _log.info(
            "unlabelled_cross_system_id_dropped",
            extra={"dropped": dropped, "kept": kept, "labelled_system": wanted.value},
        )
    return kept


#: A sender stating outright how long our load ids are — "(7 DIGIT LOAD#S)", "6-digit loads".
#:
#: Factors label our account in their own system and put that label in the subject and in
#: the table, which is where this comes from. It has to say *load*: "7 digit" beside
#: anything else is a coincidence, and this must never fire on one.
_DECLARED_ID_LENGTH_RE = re.compile(r"\b([67])[\s-]*digit[\s-]*load", re.IGNORECASE)


def _prefer_declared_id_length_across_systems(load_ids: list[str], text: str) -> list[str]:
    """When ids disagree about system, keep the length the sender says our loads are.

    Fourth instance of the shape :func:`_prefer_labelled_loads_across_systems` documents, and
    the first where the label sits in a **column header** rather than beside the number. A WEX
    collections table arrived as ``Carrier | Mot Car | Account | Mot Car | Invoice | Load |
    Age | Balance`` over ``FFS Brothers LLC | 1601899 | CIRCLE LOGISTICS, INC (IN) (7 DIGIT
    LOAD#S) | 761291 | IN-001208 | 2481841 | …``. Four ids, one real load.

    The label guard cannot reach these: HTML flattening leaves 40-odd characters between
    "Mot Car" and the number under it, and the proximity window is 24 — widening it would
    start attaching labels to whatever happens to precede a number two cells later, which is
    worse than the escalation.

    So this uses the other positive evidence the same email carries: the sender has written
    down what length our load ids are, twice. Preferring that length needs no knowledge of
    what a "Mot Car" is.

    Same safety envelope as the labelled guard, for the same reasons. It cannot fire on a
    single-system email, cannot fire without an explicit declaration, and cannot fire when
    the declaration would leave nothing — so a genuine two-system email still escalates,
    which is the case a human must see. It only ever *filters*; it can never introduce an id.
    """

    if len(load_ids) < 2:
        return load_ids

    systems = {lid: domain_route_load(lid).system for lid in load_ids}
    if len(set(systems.values())) < 2:
        return load_ids

    declared = {int(m.group(1)) for m in _DECLARED_ID_LENGTH_RE.finditer(text)}
    if len(declared) != 1:  # nothing said, or the email contradicts itself
        return load_ids

    wanted = next(iter(declared))
    # An id the sender explicitly CALLED a load outranks a length declared once in an
    # account name, so it is never dropped here. The two can genuinely disagree — "Load
    # #296006" under a "(7 DIGIT LOAD#S)" account — and when they do, the specific
    # statement about that number beats the general one about the account.
    labelled = {m.group(1) for m in _LOAD_LABEL_RE.finditer(text)}
    kept = [lid for lid in load_ids if len(lid) == wanted or lid in labelled]
    dropped = [lid for lid in load_ids if lid not in kept]
    if not kept or not dropped:
        return load_ids
    # If protecting the labelled ids leaves the disagreement intact, the declaration did not
    # resolve anything and the email still needs a human.
    if len({domain_route_load(lid).system for lid in kept}) > 1:
        return load_ids

    _log.info(
        "declared_length_cross_system_id_dropped",
        extra={"dropped": dropped, "kept": kept, "declared_digits": wanted},
    )
    return kept


def _drop_stray_sender_invoice_ids(load_ids: list[str], invoice_numbers: list[str]) -> list[str]:
    """Drop an id that is only the sender's own invoice number, pulled into another system.

    Live on an OperFi second-request email. It named "Load #: 2485194" — a Transport Pro
    load carried by Mays Transport and factored to Operation Finance, which is the sender —
    beside "OperFi Invoice #: 318354". Six digits routes to CargoTel, where 318354 happens
    to hit a record with no carrier and no factor, so the run escalated as "email spans both
    systems" and an answerable question from the load's own factor went unanswered. Third
    instance of this shape: RTS's account reference collided with a real Skyway load, and a
    "Past Due Invoices" email's 405445 was reported as an expired CargoTel cookie.

    The extractor already knew — 318354 came back in ``sender_invoice_numbers`` as well.
    Nothing consumed it.

    Deliberately narrow. ``invoice`` is **not** a label in :data:`_NOT_A_LOAD_LABEL_RE`,
    because carriers say "Invoice 2462934" meaning a real Transport Pro load; suppressing the
    bare word would refuse real questions, and a false negative is worse than an escalation.
    So this fires only when all three hold:

    * the ids span more than one system, so there is a disagreement to resolve at all;
    * at least one id is *not* a sender invoice number, giving an anchor;
    * those anchored ids agree on a single system.

    Only then is a sender-invoice id belonging to a *different* system a stray. A
    single-system email is never touched, and neither is an email whose only id is an invoice
    number — with no anchor there is nothing to contradict it, so "Invoice 2462934" survives.
    """

    invoice_set = set(invoice_numbers)
    if len(load_ids) < 2 or not invoice_set:
        return load_ids

    systems = {lid: domain_route_load(lid).system for lid in load_ids}
    if len(set(systems.values())) < 2:
        return load_ids

    anchored = {systems[lid] for lid in load_ids if lid not in invoice_set}
    if len(anchored) != 1:
        return load_ids

    anchor = next(iter(anchored))
    kept = [lid for lid in load_ids if lid not in invoice_set or systems[lid] is anchor]
    dropped = [lid for lid in load_ids if lid not in kept]
    if dropped:
        _log.info(
            "sender_invoice_id_dropped",
            extra={"dropped": dropped, "kept": kept, "anchor_system": anchor.value},
        )
    return kept
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


#: Punctuation that spells one company two ways: "G.H." / "GH", "Love's" / "Loves",
#: "XFactors Financial, Inc." / "XFactors Financial Inc".
#: The second apostrophe is U+2019, the curly form Outlook and Word substitute for a typed
#: one. Both are needed: "Love's Solutions" arrives spelled either way depending on the
#: sender's client. RUF001 flags it as visually ambiguous, which is precisely why it is
#: called out here rather than removed.
_NAME_PUNCT_RE = re.compile(r"[.,'’\-/&()]+")  # noqa: RUF001


def _normalize_company_name(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    Exists because an initialism written with full stops on the load and without them in
    the settlement export defeated the containment test entirely. Observed live: the load
    for 2444099 records "G.H. Factor LLC" while the export says "GH Factor LLC", so the
    roster key was ``gh factor llc`` and could not be linked — even though the entry, and
    the sender's ``ghfactor.net`` domain, were both exactly right. The only word the two
    spellings share is "factor", which is industry-generic and deliberately cannot link on
    its own, and "gh" is too short to be a name token. Normalised, the two strings are
    identical. Initialisms with stops are common in this industry (J.D. Factors, T.B.S.
    Factoring), so each one cost an escalation against a correct roster entry.
    """

    return " ".join(_NAME_PUNCT_RE.sub("", name.lower()).split())


#: Roster-key prefix meaning "this key matches ONLY this factor name, exactly".
#:
#: The escape hatch for a company whose one distinctive word is shared with an unrelated
#: company. "G Squared Funding, LLC" reduces to the single token ``squared`` once the
#: generic ones are dropped — so an ordinary key for it also matches "DB Squared, Inc.",
#: eleven carriers under a different factor, and would let G Squared's domain vouch for
#: their loads. Neither name can be spelled to avoid the other; the roster README's usual
#: advice ("use a new key") does not reach this, because the collision is in the matching
#: rather than in the keys.
#:
#: Use it only when the collision is real and named in the entry's evidence note. A plain
#: key remains right for almost everything: the fuzzy match exists because the export and
#: the load spell the same factor differently, and exactness costs that.
_EXACT_FACTOR_PREFIX = "exact:"


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

    Both names are punctuation-normalised first (see :func:`_normalize_company_name`).
    That widens nothing about which words may link — the generic-token rule is untouched
    — it only stops one spelling of the same name reading as a different company.
    """

    if configured_name.lower().startswith(_EXACT_FACTOR_PREFIX):
        wanted = _normalize_company_name(configured_name[len(_EXACT_FACTOR_PREFIX) :])
        return bool(wanted) and wanted == _normalize_company_name(on_file)

    key = _normalize_company_name(configured_name)
    name = _normalize_company_name(on_file)
    if not key or not name:
        return False
    if key in name or name in key:
        return True
    return bool(
        (company_tokens(key) - _FACTOR_GENERIC_TOKENS)
        & (company_tokens(name) - _FACTOR_GENERIC_TOKENS)
    )


def _is_configured_carrier_contact(
    carrier_company: str | None, sender_email: str, ctx: ToolContext
) -> bool:
    """True when the sender is an address configured for this load's carrier.

    The carrier-side counterpart of :func:`_is_configured_factor_domain`, and narrower in
    both directions on purpose.

    Matching is on the **whole address**, never the domain, because carriers are routinely
    on free mail — a domain grant here would authorise every Gmail user for that carrier.
    And the carrier name must match exactly once normalised, with none of the token overlap
    :func:`_factor_names_match` allows: a loose match would let one carrier's configured
    address answer for another's loads, which is precisely what this must never do.

    Reached only after the back office's own contact list has been consulted and missed, so
    it adds addresses and can never remove one.
    """

    sender = sender_email.strip().lower()
    if not (carrier_company and sender):
        return False
    wanted = _normalize_company_name(carrier_company)
    if not wanted:
        return False
    for name, addresses in ctx.settings.carrier_contacts.items():
        if _normalize_company_name(name) != wanted:
            continue
        if sender in {str(a).strip().lower() for a in addresses}:
            return True
    return False


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


#: Legal-entity suffixes, dropped when building an acronym. Only these — an industry word
#: like "group" or "capital" IS part of how a company abbreviates itself ("American Factoring
#: Group" is AFG, not AF), which is why this cannot reuse ``_STOPWORDS``.
_LEGAL_SUFFIXES = frozenset(
    {
        "inc", "incorporated", "llc", "l", "c", "ltd", "limited", "corp", "corporation",
        "co", "company", "lp", "llp", "plc", "pllc", "gmbh", "nv", "bv", "sa",
    }
)  # fmt: skip


def _domain_without_tld(sender_email: str) -> str:
    """The sender's domain labels except the last, concatenated. ``""`` when there are none.

    Exists so a three-letter acronym cannot be satisfied by the TLD: a factor named
    "National Equipment Transport" abbreviates to "net", which would otherwise resemble every
    ``.net`` address on earth. Subdomains are kept — ``bwc.fleetsmarts.net`` gives
    ``bwcfleetsmarts`` — because a per-tenant subdomain is exactly where an acronym shows up.
    """

    labels = [label for label in _sender_domain(sender_email).split(".") if label]
    if not labels:
        return ""
    return "".join(labels[:-1] if len(labels) > 1 else labels)


def company_acronym(name: str | None) -> str:
    """Initials of a company name, or ``""`` when too short to be distinctive.

    Factors abbreviate themselves in their domains, and the initials are then the *only*
    resemblance to the name on the load. "American Factoring Group, LLC" sends from
    ``afgfactor.com``: no token of the name appears in that domain, so the resemblance hint
    stayed silent and the escalation read "sender does not match any authorized party" — as
    if a stranger had written in, rather than a known factor whose domain simply was not
    configured yet. Whoever reviewed it was sent looking for the wrong thing.

    Three characters minimum, because two are meaningless: "Operation Finance" gives "of",
    which appears in a large share of all domains. Names that abbreviate to fewer than three
    are left to the token test, which already covers them — "Aladdin Financial" is "af", but
    ``aladdin`` matches ``aladdincap.com`` directly.
    """

    if not name:
        return ""
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w and w not in _LEGAL_SUFFIXES]
    acronym = "".join(w[0] for w in words)
    return acronym if len(acronym) >= 3 else ""


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


#: Strings a model reaches for when a field has no value. All mean "absent".
#:
#: A JSON-emitting model asked for an optional date on a load that has none does not omit
#: the field — it fills it in, and ``"null"`` is the overwhelming favourite. Rejecting that
#: as an invalid date is a dead end: the error says "use ISO YYYY-MM-DD", but there IS no
#: date to supply, so the model has nothing to correct and simply repeats itself. Observed
#: live on load 2443422 — six byte-identical calls passing ``"null"`` for both dates, half
#: the iteration budget gone, and the run ended at max_iterations with no draft on an
#: otherwise trivial status request. Same reasoning as :func:`_coerce_id_to_str`: a type
#: mismatch this trivial is not worth an agent iteration, let alone six.
_ABSENT_DATE_SENTINELS = frozenset({"null", "none", "nil", "n/a", "na", "undefined", "-", "--"})


def _parse_pay_date(value: str | None) -> date | None:
    """Parse an API or model-supplied pay date into an EDT calendar date.

    Bare ``YYYY-MM-DD`` values are calendar dates (no shift). Full timestamps are
    converted from their offset into EDT (fixed UTC-4) before the date is taken. Dates with a
    named month are also accepted — see :data:`_SPELLED_DATE_FORMATS`. A null-ish placeholder
    is treated as absent rather than invalid — see :data:`_ABSENT_DATE_SENTINELS`.
    """

    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.lower() in _ABSENT_DATE_SENTINELS:
        return None
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
    #: Visible text of the HTML part (:attr:`~payment_bot.models.InboundEmail.html_text`).
    #: A sender's plain-text alternative need not match their HTML: portal collections mail
    #: puts its invoice table in the HTML only, so this is where the load id lives. Unlike
    #: ``attachments_text`` this DOES also feed the sensitive-change scan — a bank
    #: instruction present only in the HTML would otherwise never be seen.
    html_text: str = ""


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
            for p in (
                params.subject,
                params.body,
                params.thread_text,
                params.attachments_text,
                params.html_text,
            )
            if p
        )

        load_ids = _dedupe(_load_ids_in(text))
        invoice_numbers = _dedupe(_INVOICE_RE.findall(text))
        load_ids = _drop_stray_sender_invoice_ids(load_ids, invoice_numbers)
        load_ids = _prefer_labelled_loads_across_systems(load_ids, text)
        # After the label guard, not before: an id the sender actually called a load is
        # stronger evidence than a length they declared once in an account name.
        load_ids = _prefer_declared_id_length_across_systems(load_ids, text)

        stated_rates: list[StatedRate] = []
        for line in text.splitlines():
            amounts = [_money(m) for m in _MONEY_RE.findall(line)]
            if not amounts:
                continue
            # Deduped: an invoice table routinely prints the same number under both an
            # "Invoice No" and a "Load No" column, and counting one id twice left the amount
            # bound to nothing. Two *different* ids on a line stays ambiguous — that is the
            # case this guard is for. Only helps a row that arrives on ONE line, i.e. from a
            # text part; an HTML table puts each cell on its own line, so its amounts bind to
            # no load and would need row-aware parsing to fix.
            line_ids = _dedupe(_load_ids_in(line))
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
    #: Visible text of the HTML part. Scanned alongside the body because a plain-text
    #: alternative may omit what the HTML says — the mirror of the missing-invoice-table bug,
    #: and the dangerous direction of it: a bank instruction the text part drops would pass
    #: unseen. Erring toward more text can only ever escalate more, never less.
    html_text: str = ""
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
    #: True when the email carries an ARTIFACT, an identity action, or an operation on money
    #: already sent, rather than language about future money: a void-check / direct-deposit
    #: attachment, a contact change, or a stop-payment request. These always escalate — there
    #: is paperwork to file, an identity to re-verify, or a payment to halt, and a status reply
    #: cannot do any of them — regardless of any wording policy.
    paperwork: bool = False
    #: True when an NOA / notice-of-assignment file is attached. Split from ``paperwork``
    #: because pre-funding factors routinely attach their NOA to a routine rate
    #: verification — it is part of their standard packet, not a change instruction.
    #: ``noa_attachment_replies`` may draft past it; the attachment itself still needs a
    #: human to verify and file either way.
    noa_attachment: bool = False


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
#: "banking" is listed separately from "bank" because the word boundaries that stopped "ach"
#: matching inside "each" also stop "bank" matching inside "banking" — and "our banking
#: details have changed" is one of the commonest ways a redirect is announced. Missed live:
#: a TRU Funding rate-verification email whose body read "our banking and address details
#: have recently changed", followed by a full account and routing number, scored no flags at
#: all and would have been answered with nobody alerted.
_BANK_DETAIL_PHRASES = (
    "routing number", "account number", "ach", "direct deposit", "remittance",
    "payment method", "remit to", "remit-to", "bank", "banking", "bank account",
    "bank details", "banking details",
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
#: An account or routing number actually supplied in the message — "Account Number:
#: 4941701385", "Routing Number: 121000248".
#:
#: This is what separates a redirect from the boilerplate the §7 narrowing deliberately lets
#: through. A factor's standing signature carries the numbers but no change verb. Template
#: boilerplate carries the change verb but names a *company* ("remittance is updated to OTR
#: Solutions"), not an account. **Both together — "our banking details have changed" plus
#: fresh credentials — is the payment-redirect shape**, and it is treated as hard evidence
#: however passive the grammar, because that combination is not something a routine
#: signature block produces.
_SUPPLIED_ACCOUNT_RE = re.compile(
    r"\b(?:routing|account|acct)\s*(?:number|no\.?|#)?\s*[:#-]?\s*\d{6,17}\b",
    re.IGNORECASE,
)

#: A request to halt a payment already issued. Treated as ``paperwork`` — always escalating,
#: whatever the wording policies say — rather than as bank-change language.
#:
#: The wording switches rest on one argument: the bot cannot move money, so answering the
#: status question past a remittance instruction is safe because the instruction still waits
#: for a human. A stop payment is the case where that argument does not hold. It is not a
#: change to where future money goes; it is an operation on money already gone, and the reply
#: closing the thread is what loses it. ``void check`` is already a hard bank phrase for the
#: same reason — this is the same operation named the way a payer names it.
#:
#: Live on an RTS notice opening "PLACE STOP PAYMENT ON CHK 787147 - PAID TO CARRIER ON 7.27":
#: a check had been issued to the carrier on a factored load and RTS wanted it stopped. The
#: detector scored no flags at all, so with both wording switches on the email would have been
#: answered on payment status with the stop-payment request unmentioned and unactioned.
#: Grouped, so it can be embedded after a negation prefix without the alternation escaping the
#: prefix's scope — ``prefix A|B`` would mean ``(prefix A)|(B)``.
_STOP_PAYMENT_BODY = (
    r"(?:\bstop(?:\s|-)*(?:payment|pay|the\s+(?:check|cheque|payment)|ach|wire|deposit)\b"
    r"|\bpayment\s+stop\b)"
)
_STOP_PAYMENT_RE = re.compile(_STOP_PAYMENT_BODY, re.IGNORECASE)

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


#: A change word under a direct negation — a PROHIBITION, not an instruction.
#:
#: Factoring companies append anti-fraud disclaimers to every email, and the standard wording
#: contains the exact shape the proximity scan looks for. Far West Capital's footer reads "Do
#: not change payment instructions on wires or ACH without calling the person you are paying",
#: which matched ``_BANK_CHANGE_ACTIVE_RE`` five times over the quoted chain and escalated a
#: two-line TONU status question at severity=security. The scan cannot see that the sentence
#: forbids the change rather than requesting it.
#:
#: At most ONE word may sit between the negation and the change word. "do not change" and
#: "never update" are prohibitions; "please do not hesitate to update our remittance details"
#: is a real request whose "do not" belongs to "hesitate", and two intervening words keep it
#: out of this pattern.
#:
#: Negation only, deliberately. A fraud-warning CONTEXT ("fraud", "phishing", "compromise"
#: nearby) looks like an equally good signal and is not: "due to recent fraud we need to
#: update our ACH details" is both a genuine instruction and the classic fraud pretext, so
#: suppressing on those words would blind the check to the very emails it exists for.
#: Shared by every negation guard, so a prohibition recognised for one signal is recognised
#: for all of them. Kept as a string rather than a compiled pattern because it is a prefix,
#: not a pattern in its own right.
_NEGATION_PREFIX = (
    r"\b(?:do(?:es)?\s+not|do\s*n[o']t|never|can\s*not|can'?t|won'?t|will\s+not"
    r"|must\s+not|should\s+not|shall\s+not|no\s+need\s+to)\s+(?:\w+\s+){0,1}?"
)

_NEGATED_CHANGE_RE = re.compile(
    rf"{_NEGATION_PREFIX}\b(?:{_CHANGES_ALT})\b",
    re.IGNORECASE,
)

#: "Do not stop payment" is a prohibition, and ``_NEGATED_CHANGE_RE`` cannot see it: "stop" is
#: not a change word, and adding it there would also loosen the bank proximity scan, where
#: "stop" near a payment noun is not a change request at all. So the halt check carries its own
#: guard, built from the same prefix so the two cannot drift apart.
_NEGATED_STOP_PAYMENT_RE = re.compile(
    _NEGATION_PREFIX + _STOP_PAYMENT_BODY,
    re.IGNORECASE,
)


def _negated_change_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of every negated change phrase in ``text``."""

    return [m.span() for m in _NEGATED_CHANGE_RE.finditer(text)]


def _within_negation(span: tuple[int, int], negated: list[tuple[int, int]]) -> bool:
    """True when ``span`` overlaps a negated change phrase, so it states a prohibition."""

    start, end = span
    return any(neg_start < end and start < neg_end for neg_start, neg_end in negated)


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
        # The HTML part gets the same quote-stripping: it is the same message in another
        # format, so its quoted history is just as much not-this-sender's-words.
        written = f"{params.subject}\n{strip_quoted(params.body)}"
        if params.html_text:
            written = f"{written}\n{strip_quoted(params.html_text)}"
        haystack = written.lower()
        flags: list[SensitiveFlag] = []
        evidence: list[str] = []
        hard_bank = False
        hard_noa = False

        # Negation is resolved once, up front: a prohibition is a prohibition whether it is
        # spotted by the explicit phrase list or by the proximity scans below. Far West
        # Capital's "Do not change payment instructions on wires or ACH" matches the phrase
        # 'change payment' as well as the verb-first scan, so narrowing only the scans left
        # the footer escalating anyway.
        #
        # Matched against `written` rather than `haystack`: the phrase patterns are already
        # IGNORECASE, and using the un-lowercased text keeps match offsets aligned with the
        # negated spans.
        negated = _negated_change_spans(written)
        for phrase, pattern in _BANK_REQUEST_PATTERNS:
            hits = [m for m in pattern.finditer(written) if not _within_negation(m.span(), negated)]
            if hits:
                _add(flags, SensitiveFlag.BANK_CHANGE)
                evidence.append(f"bank: matched {phrase!r}")
                hard_bank = True  # a request phrase is self-evidently an instruction

        # A payment-detail noun alone is not a request — a change word must sit near it.
        # Verb-first ("update our bank…") is an instruction aimed at us: HARD. Detail-first
        # ("remittance is updated to X") is passive template boilerplate: the one SOFT
        # signal.
        #
        # A negated change word states a prohibition, not a request, and every anti-fraud
        # footer contains one. Skipping those spans is what keeps a disclaimer from reading
        # as an instruction — see `_NEGATED_CHANGE_RE`. Only these proximity scans are
        # narrowed: the explicit `_BANK_REQUEST_PATTERNS` phrases above stay as they are, and
        # the gate's change_acknowledgment check on our own DRAFT text stays strict, because
        # there any shape of change wording is disqualifying whatever its grammar.
        for match in _BANK_CHANGE_ACTIVE_RE.finditer(written):
            if _within_negation(match.span(), negated):
                continue
            _add(flags, SensitiveFlag.BANK_CHANGE)
            evidence.append(f"bank: change instructed — {' '.join(match.group(0).split())!r}")
            hard_bank = True
        # A change announcement plus supplied credentials is a redirect, whatever the
        # grammar. Detail-first wording is normally SOFT (§7: factoring templates all say
        # "remittance is updated to X"), but those templates name a company — they do not
        # hand over an account and routing number. See `_SUPPLIED_ACCOUNT_RE`.
        credentials_supplied = _SUPPLIED_ACCOUNT_RE.search(written)
        for match in _BANK_CHANGE_PASSIVE_RE.finditer(written):
            if _within_negation(match.span(), negated):
                continue
            _add(flags, SensitiveFlag.BANK_CHANGE)
            if credentials_supplied:
                hard_bank = True
                evidence.append(
                    f"bank: change announced with new account details — "
                    f"{' '.join(match.group(0).split())!r} + "
                    f"{' '.join(credentials_supplied.group(0).split())!r}"
                )
            else:
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

        # Its own negation spans, not the bank scan's — see _NEGATED_STOP_PAYMENT_RE.
        halt_negated = [m.span() for m in _NEGATED_STOP_PAYMENT_RE.finditer(written)]
        for match in _STOP_PAYMENT_RE.finditer(written):
            if _within_negation(match.span(), halt_negated):
                continue
            _add(flags, SensitiveFlag.BANK_CHANGE)
            evidence.append(
                f"bank: payment halt requested — {' '.join(match.group(0).split())!r}"
            )
            paperwork = True
            break

        noa_attachment = False
        for att in params.attachments_metadata:
            lower = att.filename.lower()
            if any(k in lower for k in ("voidcheck", "void_check", "void-check", "directdeposit")):
                _add(flags, SensitiveFlag.BANK_CHANGE)
                evidence.append(f"bank: attachment {att.filename!r}")
                paperwork = True
            if "noa" in lower or "assignment" in lower:
                _add(flags, SensitiveFlag.NOA_SETUP_CHANGE)
                evidence.append(f"noa_setup: attachment {att.filename!r}")
                noa_attachment = True

        if not flags:
            flags.append(SensitiveFlag.NONE)
            action = SensitiveAction.CONTINUE
        else:
            action = SensitiveAction.ESCALATE

        return DetectSensitiveChangeOutput(
            flags=flags,
            evidence=evidence,
            action=action,
            hard=hard_bank or hard_noa or paperwork or noa_attachment,
            hard_bank=hard_bank,
            hard_noa=hard_noa,
            paperwork=paperwork,
            noa_attachment=noa_attachment,
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
        if params.system is System.QUICKBOOKS:
            return self._decide_cargotel(params, ctx)
        if params.system is not System.TRANSPORT_PRO:
            raise ToolError(
                f"authorization source not wired for system {params.system.value!r}"
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
        if _is_configured_carrier_contact(auth.carrier_company, sender, ctx):
            return CheckAuthorizationOutput(
                decision=AuthDecision.ALLOW,
                authorized=True,
                matched_party=auth.carrier_company,
                reason=(
                    "sender is a configured contact for this load's carrier "
                    "(PAYBOT_CARRIER_CONTACTS, not the back office record)"
                ),
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
        # Two ways a domain can resemble the name: a whole distinctive word of it, or its
        # initials. The acronym is searched in the domain WITHOUT its TLD, so a three-letter
        # acronym cannot be satisfied by "com" or "net". Both routes only ever change the
        # WORDING of a denial — this branch returns DENY either way — so a hint that fires
        # too eagerly costs a reviewer one wasted glance, never a disclosure.
        factor_toks = company_tokens(auth.factoring_company)
        acronym = company_acronym(auth.factoring_company)
        if (factor_toks and any(tok in domain for tok in factor_toks)) or (
            acronym and acronym in _domain_without_tld(sender)
        ):
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

    def _decide_cargotel(
        self, params: CheckAuthorizationInput, ctx: ToolContext
    ) -> CheckAuthorizationOutput:
        """Authorize a 6-digit load against its carrier's CargoTel client record.

        ``System.QUICKBOOKS`` is the routing label for 6-digit ids (§4.1); CargoTel is the
        system that actually holds them, and QuickBooks receives them downstream as bills.

        Three ways in, and one deliberately missing:

        1. the sender's address is a contact on the carrier's record;
        2. the sender's registrable domain matches a contact's, free-mail excluded;
        3. the sender is the load's factoring company and its domain is configured in
           ``factoring_domains`` — the same roster and the same rule the Transport Pro path
           uses, so Saint John Capital is entered once and serves both systems.

        Missing: any match on the carrier or factor *name* resembling the sender's domain.
        Measured on real records, that test reduces "SAINT JOHN CAPITAL" to tokens a domain
        like ``saintjohn-imports.com`` would satisfy, and here it would frequently be the
        only signal. Email or exact domain, or DENY.

        The free-mail exclusion is doing more work on this path than on the Transport Pro
        one: of four real carrier records, two list **only** a Gmail address. Those senders
        are authorizable on their exact address and nothing else — matching the domain would
        authorize every Gmail user alive.
        """

        if not ctx.settings.cargotel_replies:
            return CheckAuthorizationOutput(
                decision=AuthDecision.DENY,
                matched_party=None,
                reason=(
                    "CargoTel replies are disabled; set PAYBOT_CARGOTEL_REPLIES=true to "
                    "answer 6-digit loads"
                ),
            )
        if ctx.cargotel is None:
            # A wiring failure, not a denial. Raising keeps it out of the DENY bucket, where
            # it would read as "this sender is not authorized" and send a reviewer hunting
            # for a roster entry that was never the problem.
            raise ToolError(
                "CargoTel is not wired for this run, so authorization for a 6-digit load "
                "cannot be resolved"
            )

        auth = ctx.cargotel.get_authorization_context(params.load_id)
        sender = params.sender_email.strip().lower()
        sender_domain = _sender_domain(sender)
        contacts = {e.lower() for e in auth.authorized_emails}

        if sender in contacts:
            return CheckAuthorizationOutput(
                decision=AuthDecision.ALLOW,
                authorized=True,
                matched_party=auth.carrier_company,
                reason="sender is a contact on this carrier's record",
            )

        if _is_configured_carrier_contact(auth.carrier_company, sender, ctx):
            return CheckAuthorizationOutput(
                decision=AuthDecision.ALLOW,
                authorized=True,
                matched_party=auth.carrier_company,
                reason=(
                    "sender is a configured contact for this load's carrier "
                    "(PAYBOT_CARRIER_CONTACTS, not the back office record)"
                ),
            )

        if (
            sender_domain
            and sender_domain not in _FREE_MAIL_DOMAINS
            and sender_domain in {_sender_domain(e) for e in contacts}
        ):
            return CheckAuthorizationOutput(
                decision=AuthDecision.ALLOW,
                authorized=True,
                matched_party=auth.carrier_company,
                reason="sender's domain matches a contact on this carrier's record",
            )

        # The factor of record. Most carriers on this tenant are factored, so this is the
        # common case for payment enquiries rather than an edge one.
        if auth.factoring_company and _is_configured_factor_domain(
            auth.factoring_company, sender, ctx
        ):
            return CheckAuthorizationOutput(
                decision=AuthDecision.FACTORING,
                authorized=ctx.settings.allow_factoring,
                matched_party=auth.factoring_company,
                reason="sender domain is configured for the factoring company on this load",
            )

        if not contacts:
            return CheckAuthorizationOutput(
                decision=AuthDecision.DENY,
                matched_party=None,
                reason=(
                    f"the carrier record for {auth.carrier_company or 'this load'} lists no "
                    "contact address, so the sender cannot be verified; add one in CargoTel"
                ),
            )
        if auth.factoring_company:
            return CheckAuthorizationOutput(
                decision=AuthDecision.DENY,
                matched_party=None,
                reason=(
                    f"sender is neither a contact on the carrier's record nor a configured "
                    f"domain for the factor on file ({auth.factoring_company!r}); add it to "
                    "PAYBOT_FACTORING_DOMAINS if it is genuine"
                ),
            )
        return CheckAuthorizationOutput(
            decision=AuthDecision.DENY,
            matched_party=None,
            reason="sender is not a contact on this carrier's record",
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
            "returned by tp_get_load_summary. Never a human-readable date. If the line has no "
            "estimated date, OMIT this field — do not send the string 'null'."
        ),
    )
    actual_payment_date: str | None = Field(
        default=None,
        description=(
            "ISO date, YYYY-MM-DD, when the line is already paid. Same format rule. Omit it "
            "entirely when the line is unpaid; do not send the string 'null'."
        ),
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
    #: The pay date written exactly as the reply must render it — copy it verbatim.
    #:
    #: This replaced ``estimated_weekday``, which named the weekday of the *estimated* date
    #: the caller passed in while ``scheduled_pay_date`` carried the Monday/Thursday-shifted
    #: result. For five of the seven weekdays those disagree, so a reply that paired the two
    #: fields wrote e.g. "Friday, August 10, 2026" for a Monday. Nothing is left to pair:
    #: there is one date and one rendering of it.
    scheduled_pay_date_display: str
    #: True when :attr:`scheduled_pay_date` has already passed. Companion to the display
    #: string: that one settles how the date is *spelled*, this one how it is *spoken*. A
    #: reply saying payment "is scheduled for" a date already gone reads as a promise still
    #: to come, and neither the ledger nor the weekday check can see it — both compare the
    #: date, and the date is right. On the ESTIMATED basis this being true means the pay day
    #: passed with the line still unpaid; it is never licence to report the line as paid.
    scheduled_pay_date_is_past: bool = False
    basis: str
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
            if estimated is None and actual is None:
                # Terminal, not a call to fix: the load genuinely carries no payment date, so
                # no retry can succeed. Say that outright, because the bare "cannot schedule"
                # message reads like bad input and invites the model to try again with a
                # different date format — which is how an unschedulable line burns iterations.
                raise ToolError(
                    "this line has no estimated or actual payment date on file, so no pay "
                    "date can be computed. Do NOT call this tool again for this line: "
                    "report the line as pending and not yet scheduled for payment, and "
                    "state no date."
                ) from exc
            raise ToolError(str(exc)) from exc

        ctx.ledger.record_date(
            result.scheduled_pay_date,
            self.name,
            load_id=params.load_id,
            kind="scheduled_pay_date",
        )
        return ComputeScheduledPayDateOutput(
            scheduled_pay_date=result.scheduled_pay_date,
            scheduled_pay_date_display=result.display,
            scheduled_pay_date_is_past=result.scheduled_pay_date < ctx.today,
            basis=result.basis.value,
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
