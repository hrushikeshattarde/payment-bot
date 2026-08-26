"""The pre-send gate (PRD §5) — the non-negotiable code that runs before any send.

The gate is **not a skill and not a model**. It re-derives the safety-critical facts
itself (it re-runs authorization and sensitive-change detection as the source of truth,
rather than trusting what the agent reported) and checks the draft against the grounding
ledger. If any check fails, the send is blocked and the run escalates — it is never
bypassed, in any rollout phase (§8.5).

Checks (all must pass):

1. **Authorization** — every disclosed load is ALLOW (FACTORING only if policy allows).
2. **Fraud / sensitive change** — no bank / NOA-setup / contact-change signal.
3. **Grounding** — every amount and date in the draft traces to the ledger.
4. **Placeholders** — the draft contains no unfilled template markers.
5. **Length routing** — every disclosed load is a valid 6/7-digit id.
6. **Bulk** — the disclosed-load count is within the portal-fallback threshold.
7. **Tool mentions** — the reply body names no internal tool.
8. **Coverage** — the draft addresses every load the agent was asked to answer.
9. **Carrier consistency** — a CARRIER's reply never mixes two carriers' loads, catching an
   identifier that is authorized and grounded but simply is not this sender's load. A
   factoring sender is exempt: one factor legitimately spans several carriers.
10. **Change acknowledgment** — the reply never confirms or acts on a remittance/bank/NOA
    instruction (the §7 compensating control behind the boilerplate narrowing).
11. **NOA request** — the reply asks the sender for an NOA only when the intake's pre-NOA
    instruction said to; a draft must never invent a paperwork chore.
12. **Weekday consistency** — every weekday named in the reply is the real weekday of the
    date beside it. Grounding checks dates, not the adjectives attached to them, so a
    correct-and-grounded date carrying a wrong weekday passed all eleven checks above.
13. **Tense consistency** — no date that has already passed is written as though it were
    still ahead. Same blind spot one step further out: checks 3 and 12 settle that a date
    is real and correctly named, and neither of them knows what today is.
14. **CargoTel payment claim** — a 6-digit load's reply never says whether it was paid,
    either way. The blind spot widened once more: checks 3, 12 and 13 all police *dates*,
    and none of them looks at a claim about payment state — which on this path no tool
    reports, so the words can only have come from the model or its prompt.
15. **Paperwork request** — a 6-digit load's reply asks the sender for a document only when
    that load is genuinely ``awaiting_paperwork``. Check 14 polices whether a load was
    *paid*; this polices whose *court the ball is in*, which is a different claim and was
    likewise checked by nothing.
16. **Deduction disclosure** — a 6-digit load's reply never asserts a load is clear of
    advances, deductions or claims, and never answers a sender who asked about them with
    silence. The CargoTel payable is a single figure with no line items, so "no deductions"
    is ungrounded by construction — and a factor asks that question precisely because it is
    about to advance money, which makes an unanswered one read as "none".
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from payment_bot.domain import route_load
from payment_bot.domain.cargotel import BillingState, resolve_payment
from payment_bot.errors import ClientError, ToolError
from payment_bot.grounding import (
    extract_date_tokens,
    extract_money_tokens,
    find_tense_mismatches,
    find_weekday_mismatches,
)
from payment_bot.logging import get_logger
from payment_bot.models import AuthDecision, InboundEmail, SensitiveFlag, System
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import (
    _BANK_CHANGE_REQUEST_RE as _BANK_CHANGE_REQUEST_RE_GATE,
)
from payment_bot.tools.shared import (
    _BANK_REQUEST_PATTERNS as _BANK_REQUEST_PATTERNS_GATE,
)
from payment_bot.tools.shared import (
    _NOA_ACTION_RE as _NOA_ACTION_RE_GATE,
)
from payment_bot.tools.shared import (
    AttachmentMeta,
    CheckAuthorization,
    CheckAuthorizationInput,
    DetectSensitiveChange,
    DetectSensitiveChangeInput,
    strip_quoted,
)
from payment_bot.tools.submit import TOOL_NAMES, SubmitDraftOutput

_log = get_logger("gate")

#: Markers that mean the model emitted the reply *template* instead of a finished reply.
#:
#: This exists because the grounding check cannot catch it. Grounding compares the amounts
#: and dates in a draft against the ledger, so a draft that states no parseable figure at
#: all — "our carrier rate is $XXX" — has nothing to verify and passes vacuously. Observed
#: on live mail: a draft reading "$XXX … MATCHES/MISMATCHES … Yes/Not yet" passed all five
#: original checks and was reported as ready for review.
#:
#: Deliberately narrow. A gate check that fires on a legitimate reply is worse than useless,
#: so these match either the model's own stand-in text (``XXX``) or verbatim instruction
#: fragments from the reply template in ``agent/skills.py`` — never general prose.
_PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # The model's stand-in when it fails to substitute a figure: "$XXX", "XXX".
    ("XXX", re.compile(r"\bX{3,}\b")),
    # Unresolved alternations echoed from the template's own wording.
    ("MATCHES/MISMATCHES", re.compile(r"matches\s*/\s*mismatches", re.IGNORECASE)),
    ("Yes/Not yet", re.compile(r"\byes\s*/\s*not\s+yet\b", re.IGNORECASE)),
    ("Yes/No", re.compile(r"\byes\s*/\s*no\b", re.IGNORECASE)),
    ("TBD", re.compile(r"\bTBD\b")),
    # Angle and brace placeholders. Both require a letter first and forbid "@", so an email
    # address in angle brackets is not mistaken for a placeholder.
    ("<placeholder>", re.compile(r"<[A-Za-z][A-Za-z _-]{1,38}>")),
    ("{placeholder}", re.compile(r"\{\{[^{}\n]{1,40}\}\}|\{[A-Za-z][A-Za-z _.-]{0,38}\}")),
)


def _placeholder_hits(text: str) -> set[str]:
    """Labels of every placeholder marker present in ``text``."""

    return {label for label, pattern in _PLACEHOLDER_PATTERNS if pattern.search(text)}


#: A draft asking the sender to supply an NOA — request verb near the noun, either order.
#:
#: This must catch requests only, never reports: "no NOA is on file" and "WEX Fleet One is
#: on file as the factoring company" are statements the rate skill is required to make.
#: "email"/"send" (the verbs the prompts prescribe for paperwork asks) plus their nearby
#: NOA noun is the request shape. Observed live: a payment-status draft told a carrier
#: whose factor and NOA were already on file to "please email your Notice of Assignment
#: and billing paperwork" — the model invented the ask; no tool and no intake said so.
_NOA_REQUEST_RE = re.compile(
    r"\b(?:email|send|provide|submit|forward|resend)\b\W(?:\w+\W){0,8}?"
    r"\b(?:noa|notice\s+of\s+assignment)\b"
    r"|\b(?:noa|notice\s+of\s+assignment)\b\W(?:\w+\W){0,8}?"
    r"\b(?:email|send|provide|submit|forward|resend)\b",
    re.IGNORECASE,
)


#: The sender asking us to CONFIRM where their money goes.
#:
#: Distinct from :data:`_CONFIRM_CHANGE_RE` on the intake side, which wants a change word
#: near a payment noun. This shape has no change word in it at all — live on an RTS
#: statement: "confirm all payments will be made to RTS Financial Service P.O. Box 840267".
#: Nothing is being changed; we are being asked to affirm a remit address, which §7 forbids
#: a reply from doing just as firmly.
#:
#: "confirm the payment status" is the commonest sentence in this inbox and must never match,
#: which is why a direction verb is required after the noun rather than the noun alone.
_REMIT_CONFIRMATION_REQUEST_RE = re.compile(
    r"\bconfirm\w*\b\W(?:\w+\W){0,8}?"
    r"(?:\b(?:payments?|funds|checks?|remittances?)\b\W(?:\w+\W){0,4}?"
    r"\b(?:made|sent|remitted|paid|issued|directed|mailed|mailing|payable|go|going)\b"
    r"|\bremit(?:tance)?\s*(?:to|address)\b)",
    re.IGNORECASE,
)

#: A reply asserting where money will GO. Direction, never timing.
#:
#: "payment will be issued on Thursday" is the answer this inbox exists to give and must not
#: match; "payment will be directed accordingly" is the same sentence pointed at a payee and
#: must. The difference is entirely in what follows the verb, so a destination is required:
#: ``to <someone>``, or an anaphor like ``accordingly`` that points back at whatever the
#: sender just asked for. That anaphor is the live case — the draft never repeated the
#: address, it agreed to it.
_PAYMENT_DIRECTION_RE = re.compile(
    r"\b(?:payments?|funds|remittances?|checks?)\b\W(?:\w+\W){0,4}?"
    r"\b(?:will|shall)\b\W(?:\w+\W){0,3}?"
    r"\b(?:made|sent|remitted|paid|issued|directed|mailed|mailing|payable|go)\b\W(?:\w+\W){0,2}?"
    r"(?:\bto\b|\baccordingly\b|\bas\s+(?:requested|instructed|directed|indicated)\b)",
    re.IGNORECASE,
)


def asks_to_confirm_payment_direction(email: InboundEmail) -> bool:
    """Has the sender asked us to affirm where their payments go?

    The inbound half of :func:`_confirms_payment_direction`, lifted out so the intake can
    read it too. The gate blocking a reply that agrees to a remit address is the control; a
    reply that never writes the sentence is the better outcome, and only the deterministic
    detector can tell the model which kind of email it is looking at.

    Reads subject and body with quoted text stripped, exactly as the gate does — so the two
    can never disagree about whether the sender asked.
    """

    asked = "\n".join(
        part
        for part in (email.subject, strip_quoted(email.body), strip_quoted(email.html_text))
        if part
    )
    return bool(_REMIT_CONFIRMATION_REQUEST_RE.search(asked))


def _confirms_payment_direction(
    draft_body: str, email: InboundEmail, *, change_announced: bool = False
) -> str | None:
    """The draft agreeing to where payment goes, when the sender asked it to.

    The third arm of :meth:`PreSendGate._check_change_acknowledgment`, and the one the other
    two could not reach. Both of those hunt a change VERB near a payment noun. An RTS
    statement asked us to "confirm all payments will be made to RTS Financial Service P.O.
    Box 840267", and the draft answered "payment will be directed accordingly" — no change
    verb anywhere, so bank phrases, change wording and NOA action all came back empty and the
    reply confirmed a remit address on the way past.

    Armed by the INBOUND, which is what keeps it narrow: a payment-status reply may say
    whatever it likes about direction on a thread where nobody asked. Only when the sender
    has asked us to affirm where their money goes does agreeing to it become the §7 problem.
    """

    requested = asks_to_confirm_payment_direction(email)
    if not (requested or change_announced):
        return None
    match = _PAYMENT_DIRECTION_RE.search(draft_body)
    if not match:
        return None
    # Name the trigger that actually armed, not whichever one was written first. Reported as
    # "answering a confirmation request" regardless, the message sent a reviewer looking for
    # the word "confirm" in an RTS email that never used it — the arm had fired on
    # detect_sensitive_change's own bank signal ("below remittance") instead.
    trigger = (
        "answering a confirmation request"
        if requested
        else "on mail already flagged as a payment-detail change"
    )
    return f"{' '.join(match.group(0).split())} {trigger}"


def _noa_action_problems(body: str, noa_request_expected: bool) -> list[str]:
    """NOA actions in a draft, minus the one the intake told it to write.

    ``_NOA_ACTION_RE`` was written to read the SENDER's mail, where a change verb near the
    NOA noun means they are asking us to set one up. The gate reuses it on our own reply,
    and the pattern has no notion of who is asking whom. On the pre-NOA flow that misreads
    the sanctioned ask as an acknowledgment: a draft for load 2546075 said "To get this set
    up, please email the NOA and all billing paperwork" and was blocked for "NOA action 'set
    up, please email the NOA'" in the same run where ``noa_request`` passed it as "NOA
    request present, per the intake". Two checks, one sentence, opposite verdicts.

    Scoped by SPAN OVERLAP rather than by suppressing the arm outright. Only the stretch of
    text that IS the sanctioned request is forgiven; "we have set up your NOA" elsewhere in
    the same draft does not overlap it and still blocks, which is the shape this check
    exists for. And only when the intake actually asked for one — an unsanctioned request
    fails ``noa_request`` anyway, and leaving this arm to fail too keeps the reason honest.
    """

    sanctioned: list[tuple[int, int]] = (
        [m.span() for m in _NOA_REQUEST_RE.finditer(body)] if noa_request_expected else []
    )
    problems: list[str] = []
    for match in _NOA_ACTION_RE_GATE.finditer(body):
        start, end = match.span()
        if any(max(start, s) < min(end, e) for s, e in sanctioned):
            continue
        problems.append(f"NOA action {match.group(0).strip()!r}")
    return problems


#: Wording that characterises whether a 6-digit load has been paid — in either direction.
#:
#: On the CargoTel path this is unanswerable, not merely unverified: ``BillingState`` has
#: five members and none of them is paid, and ``CgtLoadStatusOutput`` carries no check
#: number, no payment date and no method (that detail is on the Accounting tab, which is
#: not wired). A passed pay date is not evidence of payment and its absence is not evidence
#: against — the system does not say. So the word is ungrounded *by construction* here,
#: which is what makes a flat ban on it safe rather than blunt.
#:
#: The first pattern is deliberately the whole of the paid-word family and nothing near it.
#: "payable", "payment", "pay date" and ``freightpay@circledelivers.com`` are all required
#: vocabulary in these replies and none contains ``paid``, so the word boundary does the
#: separating on its own. Observed live on load 298891: "was scheduled for payment on
#: Wednesday, August 12, 2026, but is not yet showing as paid" — thirteen checks passed and
#: the one clause a factor would act on was sourced from the prompt, not the load.
#:
#: The second closes the paraphrase that says the same thing without the word. Kept tight:
#: a payment noun within a few words of a *completed*-action verb. Future forms are left
#: alone deliberately — "we'll confirm when payment goes out" claims nothing.
_CGT_PAYMENT_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("paid/unpaid", re.compile(r"\b(?:un)?paid\b", re.IGNORECASE)),
    (
        "completed-payment claim",
        re.compile(
            r"\b(?:payment|check|funds|remittance)\b(?:\W+\w+){0,4}?\W+"
            r"\b(?:went\s+out|gone\s+out|issued|released|mailed|remitted|disbursed|cleared)\b",
            re.IGNORECASE,
        ),
    ),
)


def _cargotel_payment_claims(text: str) -> list[str]:
    """Labels of every payment characterisation present in ``text``."""

    return [label for label, pattern in _CGT_PAYMENT_CLAIM_PATTERNS if pattern.search(text)]


#: The documents a reply can put back on the sender. ``paperwork``/``documents`` are here
#: because the ask is just as actionable when it names no specific file.
_PAPERWORK_NOUN = (
    r"(?:carrier\s+)?invoices?|bills?\s+of\s+lading|bol\s*0?5|bols?"
    r"|paperwork|documents?|documentation"
)

#: Wording that puts a document back on the sender — "send us X", "awaiting your X", "we have
#: not received X", "X is still outstanding".
#:
#: The gap between trigger and noun is ``[^.!?\n]`` rather than a word count, so a match can
#: never straddle a sentence boundary. That is what keeps "there's nothing further we need
#: from you. We don't have a payment date yet" from reading as a request for a payment date:
#: with a word-window the two sentences join up, and the check would fire on the very wording
#: it exists to make sayable.
_PAPERWORK_REQUEST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "asks the sender to send it",
        re.compile(
            rf"\b(?:send|resend|forward|email|provide|submit|upload|attach)\b"
            rf"[^.!?\n]{{0,40}}?\b(?:{_PAPERWORK_NOUN})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "says we are awaiting it",
        re.compile(
            rf"\b(?:awaiting|await|waiting\s+(?:on|for))\b"
            rf"[^.!?\n]{{0,40}}?\b(?:{_PAPERWORK_NOUN})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "says we have not received it",
        re.compile(
            rf"\b(?:not|never|haven't|havent|don't|dont)\b[^.!?\n]{{0,20}}?"
            rf"\b(?:received|receive|have|got)\b[^.!?\n]{{0,30}}?\b(?:{_PAPERWORK_NOUN})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "calls it outstanding",
        re.compile(
            rf"\b(?:need|needs|needed|require|requires|required|missing|outstanding)\b"
            rf"[^.!?\n]{{0,40}}?\b(?:{_PAPERWORK_NOUN})\b",
            re.IGNORECASE,
        ),
    ),
    # The same claim with the noun first — "your BOL is still outstanding", "the carrier
    # invoice is missing from our file". Only the predicate words go here: a bare "need"
    # in this direction would fire on "nothing further we need from you".
    (
        "says it is outstanding",
        re.compile(
            rf"\b(?:{_PAPERWORK_NOUN})\b[^.!?\n]{{0,30}}?"
            rf"\b(?:missing|outstanding|still\s+needed|still\s+required|not\s+on\s+file)\b",
            re.IGNORECASE,
        ),
    ),
)


#: Wording that CLEARS paperwork rather than requesting it. Two live shapes betrayed the
#: request patterns above (G.H. Factor, load 302618, 2026-08-19): a NEGATED claim — "no
#: paperwork is outstanding" contains "paperwork … outstanding" — and an adjectival
#: predicate — "all required documents are on file" contains "required … documents". Both
#: sentences hand the sender nothing; they assert the opposite, and the factor had asked
#: "confirm you have all the documents needed", so the reply could not avoid the
#: vocabulary. A request match sitting ENTIRELY inside one of these spans is an assurance
#: and is discarded. Containment rather than overlap, on purpose: "please send all
#: outstanding documents so everything is on file" starts its match at "send", before any
#: clearing span opens, and must keep blocking. Same lesson the intake scan's
#: ``_NEGATED_CHANGE_RE`` learned from the Far West footer: negation flips meaning, and a
#: shape-matcher cannot see it without help.
_PAPERWORK_CLEARED_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "no paperwork is outstanding", "none of the documents are missing"
    re.compile(
        rf"\b(?:no|none|nothing)\b[^.!?\n]{{0,30}}?\b(?:{_PAPERWORK_NOUN})\b"
        rf"[^.!?\n]{{0,30}}?\b(?:outstanding|missing|needed|required|due)\b",
        re.IGNORECASE,
    ),
    # negation first, noun trailing or absent — "nothing outstanding on the documents"
    re.compile(
        rf"\b(?:no|none|nothing)\b[^.!?\n]{{0,30}}?"
        rf"\b(?:outstanding|missing|needed|required)\b"
        rf"(?:[^.!?\n]{{0,40}}?\b(?:{_PAPERWORK_NOUN})\b)?",
        re.IGNORECASE,
    ),
    # "all required documents are on file", "every document has been received"
    re.compile(
        rf"\b(?:all|every)\b[^.!?\n]{{0,50}}?\b(?:{_PAPERWORK_NOUN})\b"
        rf"[^.!?\n]{{0,40}}?\b(?:on\s+file|received|in\s+order|complete"
        rf"|accounted\s+for|in\s+our\s+system)\b",
        re.IGNORECASE,
    ),
)


def _paperwork_cleared_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of every phrase asserting paperwork is complete or not needed."""

    return [m.span() for p in _PAPERWORK_CLEARED_PATTERNS for m in p.finditer(text)]


def _paperwork_requests(text: str) -> list[str]:
    """Labels of every way ``text`` puts a document back on the sender.

    A match wholly inside a cleared span is an assurance wearing request vocabulary —
    "no paperwork is outstanding" — and does not count.
    """

    cleared = _paperwork_cleared_spans(text)
    labels: list[str] = []
    for label, pattern in _PAPERWORK_REQUEST_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if not any(start >= c_start and end <= c_end for c_start, c_end in cleared):
                labels.append(label)
                break
    return labels


#: Phrases that mean a sender is asking whether the payable is net of anything.
#:
#: Phrases, never bare words, and the reason is recorded in ``_RATE_SIGNALS``: bare "advance"
#: scored "Thank you in Advance, ACDS TEAM" as a rate request at 0.9 confidence, and "claim"
#: was the same shape. A signature or a legal disclaimer is not stripped from a sender's own
#: text the way quoted history is, so a single ordinary word here would fire on boilerplate.
#: "claims" is therefore admitted only alongside a deduction word — "no claims and no
#: deductions" is the real phrasing and it carries its own corroboration.
_DEDUCTION_QUESTION_SIGNALS: tuple[str, ...] = (
    "fuel advance", "fuel advances", "advance payment", "payment advance", "cash advance",
    "advances or deductions", "advances and deductions", "advances, claims",
    "deduction", "deductions", "deducted",
    "chargeback", "chargebacks", "charge back", "charge backs",
    "short pay", "short-pay", "shortpay", "shortpays",
    "claims or deductions", "claims and deductions", "claims, deductions",
)  # fmt: skip

#: The vocabulary a reply uses when it *does* address the question, either way.
#:
#: "in advance" is removed before this is applied — a reply signing off "thanks in advance"
#: must not count as having addressed deductions. That direction of error is the dangerous
#: one here: it would mark the check satisfied and let the silence through.
_DEDUCTION_TOPIC_RE = re.compile(
    r"\b(?:advances?|deductions?|deducted|chargebacks?|charge\s?backs?|claims?"
    r"|short\s?pays?|offsets?)\b",
    re.IGNORECASE,
)

_IN_ADVANCE_RE = re.compile(r"\bin\s+advance\b", re.IGNORECASE)

#: A reply asserting the load is clean. Ungrounded on the CargoTel path by construction:
#: ``amount`` is one payable and the tool returns no line items, so neither the presence nor
#: the absence of a deduction is reported. "We have no record of deductions" is included
#: deliberately — no record is what this path always shows, so offering it as an answer
#: dresses a structural blind spot up as a finding.
_DEDUCTION_CLEAR_CLAIM_RE = re.compile(
    r"\b(?:no|not\s+any|none|without|free\s+of|clear\s+of|zero)\b[^.!?\n]{0,30}?"
    r"\b(?:advances?|deductions?|chargebacks?|claims?|short\s?pays?|offsets?)\b",
    re.IGNORECASE,
)


def _deduction_questions(text: str) -> list[str]:
    """Every advance/deduction phrase the sender used, matched as whole words."""

    lowered = text.lower()
    return [
        phrase
        for phrase in _DEDUCTION_QUESTION_SIGNALS
        if re.search(rf"\b{re.escape(phrase)}\b", lowered)
    ]


class GateCheck(BaseModel):
    """Outcome of one named gate check."""

    name: str
    passed: bool
    detail: str


class GateResult(BaseModel):
    """Aggregate gate decision. ``allowed`` is true only if every check passed."""

    allowed: bool
    checks: list[GateCheck]

    @property
    def reasons(self) -> list[str]:
        """Human-readable reasons for every failed check."""

        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]


class PreSendGate:
    """Evaluates a draft against the §5 checks. Stateless and deterministic."""

    def __init__(self, *, allow_factoring: bool = False) -> None:
        self._allow_factoring = allow_factoring
        self._check_auth = CheckAuthorization()
        self._detect_sensitive = DetectSensitiveChange()

    def evaluate(
        self,
        *,
        draft: SubmitDraftOutput,
        email: InboundEmail,
        ctx: ToolContext,
        expected_load_ids: tuple[str, ...] | None = None,
        noa_request_expected: bool = False,
        withheld_loads: tuple[str, ...] = (),
    ) -> GateResult:
        """Run all checks.

        Args:
            expected_load_ids: The loads the agent was asked to answer, when a specific
                set exists. ``None`` for code-authored replies (the bulk portal draft),
                which deliberately name no load.
            noa_request_expected: True when the intake instructed the agent to ask the
                sender for an NOA (the pre-NOA flow). Only then may the draft request one.
        """

        checks = [
            self._check_length_routing(draft),
            self._check_bulk(draft, ctx),
            self._check_authorization(draft, email, ctx),
            self._check_sensitive_change(email, ctx),
            self._check_placeholders(draft),
            self._check_grounding(draft, ctx),
            self._check_sender_amount_attribution(draft, ctx),
            self._check_weekday_consistency(draft),
            self._check_tense_consistency(draft, ctx),
            self._check_cargotel_payment_claim(draft),
            self._check_paperwork_request(draft, ctx),
            self._check_deduction_disclosure(draft, email),
            self._check_tool_mentions(draft),
            self._check_coverage(draft, expected_load_ids),
            self._check_withheld_acknowledged(draft, withheld_loads),
            self._check_carrier_consistency(draft, email, ctx),
            self._check_change_acknowledgment(
                draft, email, noa_request_expected, change_announced=self._change_announced(email, ctx)
            ),
            self._check_noa_request(draft, noa_request_expected),
        ]
        allowed = all(c.passed for c in checks)
        result = GateResult(allowed=allowed, checks=checks)
        if not allowed:
            _log.warning(
                "gate_blocked",
                extra={"correlation_id": ctx.correlation_id, "reasons": result.reasons},
            )
        return result

    # --- individual checks --------------------------------------------------
    def _check_length_routing(self, draft: SubmitDraftOutput) -> GateCheck:
        invalid = [lid for lid in draft.load_ids if route_load(lid).system is System.INVALID]
        if invalid:
            return GateCheck(
                name="length_routing",
                passed=False,
                detail=f"disclosed load ids are not valid 6/7-digit: {invalid}",
            )
        return GateCheck(
            name="length_routing", passed=True, detail="all disclosed loads are valid 6/7-digit"
        )

    def _check_bulk(self, draft: SubmitDraftOutput, ctx: ToolContext) -> GateCheck:
        count = len(draft.load_ids)
        threshold = ctx.settings.bulk_threshold
        if count > threshold:
            return GateCheck(
                name="bulk",
                passed=False,
                detail=f"{count} loads exceeds threshold {threshold}; use the portal reply",
            )
        return GateCheck(name="bulk", passed=True, detail=f"{count} loads within threshold")

    def _check_authorization(
        self, draft: SubmitDraftOutput, email: InboundEmail, ctx: ToolContext
    ) -> GateCheck:
        if not draft.load_ids:
            return GateCheck(
                name="authorization", passed=True, detail="no loads disclosed; nothing to authorize"
            )
        denied: list[str] = []
        for load_id in draft.load_ids:
            system = route_load(load_id).system
            try:
                outcome = self._check_auth.run(
                    CheckAuthorizationInput(
                        sender_email=email.from_email,
                        sender_name=email.from_name,
                        load_id=load_id,
                        system=system,
                    ),
                    ctx,
                )
            except (ToolError, ClientError) as exc:
                # Cannot resolve authorization → fail closed (never disclose).
                denied.append(f"{load_id}=ERROR({exc})")
                continue
            decision = outcome.decision
            allowed = decision is AuthDecision.ALLOW or (
                decision is AuthDecision.FACTORING and self._allow_factoring
            )
            if not allowed:
                # Carry the tool's reason. "2462934=DENY" alone tells a reviewer nothing;
                # the reason distinguishes an unknown stranger from a factoring company whose
                # domain simply has not been configured yet, and names the fix.
                detail = f" ({outcome.reason})" if outcome.reason else ""
                denied.append(f"{load_id}={decision.value}{detail}")
        if denied:
            return GateCheck(
                name="authorization",
                passed=False,
                detail=f"sender not authorized for: {denied}",
            )
        return GateCheck(
            name="authorization", passed=True, detail="sender authorized for all disclosed loads"
        )

    def _check_sensitive_change(self, email: InboundEmail, ctx: ToolContext) -> GateCheck:
        outcome = self._detect_sensitive.run(
            DetectSensitiveChangeInput(
                subject=email.subject,
                body=email.body,
                # Must match what the pipeline scanned, or the gate — which re-derives this
                # as the source of truth rather than trusting the agent — would see LESS
                # than the intake did and wave through what intake had flagged.
                html_text=email.html_text,
                attachments_metadata=[
                    AttachmentMeta(filename=a.filename, mime_type=a.mime_type)
                    for a in email.attachments
                ],
            ),
            ctx,
        )
        flags = [f for f in outcome.flags if f is not SensitiveFlag.NONE]
        blocked = (
            outcome.paperwork
            or (outcome.hard_bank and not ctx.settings.sensitive_bank_replies)
            or (outcome.hard_noa and not ctx.settings.sensitive_noa_replies)
            or (outcome.noa_attachment and not ctx.settings.noa_attachment_replies)
        )
        if flags and blocked:
            return GateCheck(
                name="sensitive_change",
                passed=False,
                detail=f"sensitive change detected: {[f.value for f in flags]}",
            )
        if flags:
            # Boilerplate (§7) or bank wording admitted by the sensitive_bank_replies
            # policy: what the gate enforces instead is that the DRAFT acknowledges
            # nothing — see change_acknowledgment, which runs on every draft.
            return GateCheck(
                name="sensitive_change",
                passed=True,
                detail="change wording present; draft checked for acknowledgment instead",
            )
        return GateCheck(
            name="sensitive_change", passed=True, detail="no bank/NOA/contact change detected"
        )

    def _change_announced(self, email: InboundEmail, ctx: ToolContext) -> bool:
        """Did the inbound ANNOUNCE a payment-detail change, as opposed to asking about one?

        Reuses the same scan :meth:`_check_sensitive_change` runs, for the one bit
        :func:`_confirms_payment_direction` needs. Cheap — it is regex over the body — and
        re-derived here rather than threaded through, so the two can never disagree.
        """

        outcome = self._detect_sensitive.run(
            DetectSensitiveChangeInput(
                subject=email.subject,
                body=email.body,
                html_text=email.html_text,
                attachments_metadata=[
                    AttachmentMeta(filename=a.filename, mime_type=a.mime_type)
                    for a in email.attachments
                ],
            ),
            ctx,
        )
        return bool(outcome.hard_bank or outcome.hard_noa)

    def _check_change_acknowledgment(
        self,
        draft: SubmitDraftOutput,
        email: InboundEmail,
        noa_request_expected: bool = False,
        *,
        change_announced: bool = False,
    ) -> GateCheck:
        """The reply must never acknowledge or act on a remittance/bank/NOA instruction.

        This is the §7 compensating control that makes the boilerplate narrowing safe: the
        bot cannot change remittance, so the only real risk was a reply that *reads as if
        it did*. The draft body is scanned with the same patterns the email scan uses — a
        change word near a payment noun, an explicit request phrase, an NOA action.

        ``noa_request_expected`` is threaded through for :func:`_noa_action_problems`, which
        is where reusing the email's patterns on a reply stops being free.
        """

        body = draft.reply_body
        problems: list[str] = []
        for phrase, pattern in _BANK_REQUEST_PATTERNS_GATE:
            if pattern.search(body.lower()):
                problems.append(f"bank phrase {phrase!r}")
        match = _BANK_CHANGE_REQUEST_RE_GATE.search(body)
        if match:
            problems.append(f"change wording {' '.join(match.group(0).split())!r}")
        problems.extend(_noa_action_problems(body, noa_request_expected))
        agreed = _confirms_payment_direction(body, email, change_announced=change_announced)
        if agreed:
            problems.append(f"payment direction {agreed!r}")
        if problems:
            return GateCheck(
                name="change_acknowledgment",
                passed=False,
                detail=f"draft acknowledges a payment-change instruction: {problems}",
            )
        return GateCheck(
            name="change_acknowledgment",
            passed=True,
            detail="reply acknowledges no remittance/bank/NOA instruction",
        )

    def _check_noa_request(self, draft: SubmitDraftOutput, expected: bool) -> GateCheck:
        """The draft may ask the sender for an NOA only when the intake said to.

        The pre-NOA flow is the one legitimate source of that ask, and the pipeline knows
        when it fired. Observed live: a draft told a carrier whose factor and NOA were
        already on file to email their Notice of Assignment — inventing paperwork chores
        for senders erodes exactly the trust these replies exist to build.
        """

        match = _NOA_REQUEST_RE.search(draft.reply_body)
        if match and not expected:
            return GateCheck(
                name="noa_request",
                passed=False,
                detail=(
                    "draft asks the sender for an NOA but the intake did not instruct it "
                    f"— {' '.join(match.group(0).split())!r}"
                ),
            )
        if match:
            return GateCheck(
                name="noa_request", passed=True, detail="NOA request present, per the intake"
            )
        return GateCheck(name="noa_request", passed=True, detail="no NOA request in the reply")

    def _check_placeholders(self, draft: SubmitDraftOutput) -> GateCheck:
        """Block a draft that was never filled in.

        Citations are scanned as well as the body. A citation reading ``XXX`` never reaches
        the carrier — only the body is emailed — but it is direct evidence the model was
        emitting the template rather than reporting tool results, which makes the whole
        draft untrustworthy. Failing closed on it is the point of the gate.
        """

        body = _placeholder_hits(draft.reply_body)
        citations: set[str] = set()
        for citation in draft.citations:
            citations |= _placeholder_hits(f"{citation.fact} {citation.value}")

        if not body and not citations:
            return GateCheck(
                name="placeholders", passed=True, detail="no unfilled template markers"
            )

        problems: list[str] = []
        if body:
            problems.append(f"reply body {sorted(body)}")
        if citations:
            problems.append(f"citations {sorted(citations)}")
        return GateCheck(
            name="placeholders",
            passed=False,
            detail=f"draft was never filled in: {'; '.join(problems)}",
        )

    def _check_tool_mentions(self, draft: SubmitDraftOutput) -> GateCheck:
        """No internal tool name in the carrier-facing text.

        ``submit_draft`` already strips these mechanically, so through the normal path
        this cannot fail — it guards the other routes to the gate (human edits, future
        code-authored drafts) and any future tool the sanitizer misses. Observed live
        before the sanitizer existed: "[tp_get_load_summary]" in a reply body.
        """

        mentioned = sorted(name for name in TOOL_NAMES if name in draft.reply_body)
        if mentioned:
            return GateCheck(
                name="tool_mentions",
                passed=False,
                detail=f"reply body names internal tools: {mentioned}",
            )
        return GateCheck(name="tool_mentions", passed=True, detail="no tool names in the reply")

    def _check_withheld_acknowledged(
        self, draft: SubmitDraftOutput, withheld_loads: tuple[str, ...]
    ) -> GateCheck:
        """A load the sender named and this reply will not cover must be named as not covered.

        `_withheld_line` puts that instruction in the intake, and an instruction is all it
        was — nothing checked the draft obeyed it. Live on a Trans 99 statement listing loads
        2494074 and 2543747: the second was denied, the intake said to say so, and the reply
        answered the first and never mentioned the second. The sender had no way to tell.

        `_check_coverage` cannot catch this. It polices the loads the agent was ASKED to
        answer, and a withheld load is by definition not one of them — it was removed before
        the skill was chosen.

        Presence only. The reply must contain the id; what it may say ABOUT it is the
        intake's business, and disclosure beyond that is what the other checks police.
        """

        missing = [lid for lid in withheld_loads if lid not in draft.reply_body]
        if missing:
            return GateCheck(
                name="withheld_acknowledged",
                passed=False,
                detail=(
                    "the sender named these loads and the reply neither covers nor "
                    f"mentions them: {', '.join(missing)}"
                ),
            )
        return GateCheck(
            name="withheld_acknowledged",
            passed=True,
            detail=(
                f"withheld load(s) named in the reply: {', '.join(withheld_loads)}"
                if withheld_loads
                else "no withheld loads to acknowledge"
            ),
        )

    def _check_coverage(
        self, draft: SubmitDraftOutput, expected_load_ids: tuple[str, ...] | None
    ) -> GateCheck:
        """Every load the agent was asked about must be addressed in the reply.

        Observed live: a carrier asked about two loads and the draft silently answered
        one — a reviewer skimming the draft would not know the second was dropped. A load
        counts as addressed when it appears in ``load_ids`` (disclosed) or is named in the
        body (e.g. a hold reply: "load 2520677 is under review").

        Loads the pipeline could not look up at all are deliberately NOT required here.
        Failing on them would escalate every email that happens to contain a stray 7-digit
        number, which `test_an_unresolvable_load_is_dropped_not_fatal` exists to prevent.
        They are instead named in the intake message so the reply can say it could not
        locate them — see `build_payment_status_intake`.
        """

        if expected_load_ids is None:
            return GateCheck(
                name="coverage", passed=True, detail="code-authored reply; no expected loads"
            )
        missing = [
            lid
            for lid in expected_load_ids
            if lid not in draft.load_ids and lid not in draft.reply_body
        ]
        if missing:
            return GateCheck(
                name="coverage",
                passed=False,
                detail=f"draft does not address load(s) {missing}",
            )
        return GateCheck(
            name="coverage", passed=True, detail="every requested load is addressed"
        )

    def _check_carrier_consistency(
        self, draft: SubmitDraftOutput, email: InboundEmail, ctx: ToolContext
    ) -> GateCheck:
        """A CARRIER's reply must not span two carriers' loads.

        A carrier asking about a load that is not theirs is nearly always an accident of
        identifier extraction, and materially misleading even when every fact is grounded and
        every disclosure authorized. Observed live: an RTS enquiry titled "RAD LOGISTICS ONE
        LLC | 1669695" reported SKYWAY TRUCK LINE INC's 2024 payment in a reply about RAD
        Logistics, because ``1669695`` was an account reference that collided with a real
        load.

        **A factoring sender is exempt, and must be.** A factor's aging report legitimately
        spans several of its carriers in one email — that is the normal shape of its work, not
        an accident. Observed live, and the reason this exemption exists: Engaged Finance
        asked about loads 2523916 (Forever13 Azorie Reynolds Trucking) and 2526677 (AAA
        Expedited Services), both factored to Engaged Financial, and the first version of this
        check blocked the draft. Factors are a large share of inbound, so an unscoped
        same-carrier rule would block a great deal of legitimate mail.

        That narrowing does mean this check no longer catches the RTS case, where RTS was the
        factor on both loads. The durable fix for that was always the extraction side — see
        ``_NOT_A_LOAD_LABEL_RE``, which now suppresses account-number labels. This check
        remains the backstop for a carrier sender.

        A load whose carrier cannot be resolved is skipped rather than failed — an
        unreachable Transport Pro must not turn every draft into a block. Comparison is on a
        casefolded name, because the API returns inconsistent capitalisation for the same
        company ("Rad Logistics One Llc" vs "RAD LOGISTICS ONE LLC").

        **A load can belong to several carriers**, so the test is whether the disclosed loads
        SHARE one, not whether each reports the same single name. A load re-dispatched across
        legs has a payable per carrier (see ``TransportProLoad``); on such a load the old
        one-name comparison read whichever carrier came first in the array, so two loads the
        same carrier genuinely ran could be reported as a mix — and two loads with nothing in
        common could pass if their first entries happened to agree. Intersecting the sets is
        exactly the old behaviour wherever every load has one carrier, which is nearly always.
        """

        for load_id in draft.load_ids:
            try:
                outcome = self._check_auth.run(
                    CheckAuthorizationInput(
                        sender_email=email.from_email,
                        sender_name=email.from_name,
                        load_id=load_id,
                        system=route_load(load_id).system,
                    ),
                    ctx,
                )
            except (ToolError, ClientError):
                continue
            if outcome.decision is AuthDecision.FACTORING:
                return GateCheck(
                    name="carrier_consistency",
                    passed=True,
                    detail="factoring sender; one factor legitimately spans several carriers",
                )

        per_load: list[tuple[str, frozenset[str]]] = []
        for load_id in draft.load_ids:
            try:
                carriers = self._carriers_for(load_id, ctx)
            except (ClientError, ToolError):
                continue
            if carriers:
                per_load.append((load_id, carriers))

        shared: frozenset[str] = frozenset()
        for index, (_, carriers) in enumerate(per_load):
            shared = carriers if index == 0 else shared & carriers

        if per_load and not shared:
            spread = "; ".join(
                f"{load_id} = {sorted(carriers)!r}" for load_id, carriers in per_load
            )
            return GateCheck(
                name="carrier_consistency",
                passed=False,
                detail=f"draft mixes loads from different carriers: {spread}",
            )
        return GateCheck(
            name="carrier_consistency",
            passed=True,
            detail=(
                "all disclosed loads belong to one carrier"
                if per_load
                else "no carrier to compare"
            ),
        )

    def _carriers_for(self, load_id: str, ctx: ToolContext) -> frozenset[str]:
        """Every carrier a disclosed load belongs to, casefolded, asking the system that owns it.

        Routed by id length rather than always asking Transport Pro. Asking TP about a
        6-digit id does not fail usefully — it raises, the caller's ``except`` skips the
        load, and the check quietly reports "no carrier to compare". That is the worst
        outcome available for a safety check: a mixed reply would have been compared on its
        Transport Pro loads alone and passed on a technicality.
        """

        if route_load(load_id).system is System.QUICKBOOKS:
            if ctx.cargotel is None:
                raise ToolError(
                    f"load {load_id} is a 6-digit load but no CargoTel client is wired"
                )
            auth = ctx.cargotel.get_authorization_context(load_id)
        else:
            auth = ctx.tp.get_authorization_context(load_id)
        return frozenset(
            name.strip().casefold() for name in auth.carrier_companies if name.strip()
        )

    def _check_grounding(self, draft: SubmitDraftOutput, ctx: ToolContext) -> GateCheck:
        # Magnitudes on both sides — the ledger stores them that way, see record_amount.
        stated_money = {abs(amount) for amount in extract_money_tokens(draft.reply_body)}
        # The sender's own figures are quotable but not grounded, and the difference is
        # _check_sender_amount_attribution's business, not this check's. Here they only
        # need to not be reported as unaccounted for.
        quotable = ctx.ledger.grounded_amounts | ctx.ledger.sender_stated_amounts
        ungrounded_money = stated_money - quotable
        ungrounded_dates = extract_date_tokens(draft.reply_body) - ctx.ledger.grounded_dates
        problems: list[str] = []
        if ungrounded_money:
            problems.append(f"amounts {sorted(str(m) for m in ungrounded_money)}")
        if ungrounded_dates:
            problems.append(f"dates {sorted(d.isoformat() for d in ungrounded_dates)}")
        if problems:
            return GateCheck(
                name="grounding",
                passed=False,
                detail=f"draft contains ungrounded values: {'; '.join(problems)}",
            )
        quoted_back = sorted(
            str(m) for m in (stated_money & ctx.ledger.sender_stated_amounts)
            if m not in ctx.ledger.grounded_amounts
        )
        detail = "every amount and date in the draft is grounded"
        if quoted_back:
            detail = f"{detail} (quoted from the sender, not from a tool: {quoted_back})"
        return GateCheck(name="grounding", passed=True, detail=detail)

    def _check_sender_amount_attribution(
        self, draft: SubmitDraftOutput, ctx: ToolContext
    ) -> GateCheck:
        """A figure only the sender asserted may not be the only figure in the reply.

        Companion to grounding, and the reason the two sets are kept apart. Grounding asks
        whether a number is accountable and now answers yes for the sender's own amounts,
        because a rate dispute has to print theirs beside ours. Nothing in that check looks
        at WHOSE number it is, so a draft was free to take a figure off the sender's email
        and state it as the rate on file.

        Whose it is cannot be read off the wording without guessing at English, so this
        check does not try. It asks the structural question instead: if the reply repeats a
        number only the sender wrote, does it also state one of ours? A reply that quotes
        their figure and cites none of our own is either echoing them back as fact or
        arguing with nothing, and a human should see it either way.

        An amount BOTH sides state is not sender-only — the agreeing case, which is most of
        them, never reaches the failing branch.
        """

        stated_money = {abs(amount) for amount in extract_money_tokens(draft.reply_body)}
        sender_only = (stated_money & ctx.ledger.sender_stated_amounts) - (
            ctx.ledger.grounded_amounts
        )
        if not sender_only:
            return GateCheck(
                name="sender_amount_attribution",
                passed=True,
                detail="no sender-only amount in the reply",
            )
        if stated_money & ctx.ledger.grounded_amounts:
            return GateCheck(
                name="sender_amount_attribution",
                passed=True,
                detail=(
                    f"sender-only amounts {sorted(str(m) for m in sender_only)} stated "
                    "alongside our own"
                ),
            )
        return GateCheck(
            name="sender_amount_attribution",
            passed=False,
            detail=(
                f"draft states amounts {sorted(str(m) for m in sender_only)} that only the "
                "sender asserted, and no amount from a tool alongside them"
            ),
        )

    def _check_weekday_consistency(self, draft: SubmitDraftOutput) -> GateCheck:
        """Companion to grounding: the date is right, but is the weekday beside it?

        Blocks the load-2481130 shape — "Monday, August 11, 2026" for a Tuesday, cited to
        `compute_scheduled_pay_date`, which had actually returned "Tuesday". A carrier reads
        the weekday as the operative fact ("so it went out Monday"), and it is the one part
        of a date the ledger has nothing to compare against.
        """

        mismatches = find_weekday_mismatches(draft.reply_body)
        if mismatches:
            stated = "; ".join(
                f"{m.stated} {m.value.isoformat()} is a {m.correct}" for m in mismatches
            )
            return GateCheck(
                name="weekday_consistency",
                passed=False,
                detail=f"draft names the wrong weekday for a date: {stated}",
            )
        return GateCheck(
            name="weekday_consistency",
            passed=True,
            detail="every weekday named matches its date",
        )

    def _check_tense_consistency(self, draft: SubmitDraftOutput, ctx: ToolContext) -> GateCheck:
        """Companion to weekday consistency: the date is named right, but is it still ahead?

        Blocks the load-302866 shape — "Payment is scheduled for Friday, August 8, 2026" in a
        draft written on August 13. Every other check is satisfied: the date came from
        `cgt_get_load_status`, it is in the ledger, and it is cited. What none of them holds
        is a calendar, so a promise about a day already gone reads as a promise still good.
        """

        mismatches = find_tense_mismatches(draft.reply_body, ctx.today)
        if mismatches:
            stated = "; ".join(
                f'"{m.phrase}" for {m.value.isoformat()}, {m.days_past} day(s) ago'
                for m in mismatches
            )
            return GateCheck(
                name="tense_consistency",
                passed=False,
                detail=(
                    f"draft writes a past date as still upcoming (today is "
                    f"{ctx.today.isoformat()}): {stated}"
                ),
            )
        return GateCheck(
            name="tense_consistency",
            passed=True,
            detail="no past date is described as upcoming",
        )

    def _check_cargotel_payment_claim(self, draft: SubmitDraftOutput) -> GateCheck:
        """A 6-digit load's reply may not say whether it was paid, in either direction.

        The other two date checks exist because grounding compares dates and not the words
        beside them. This one exists because grounding compares *amounts and dates* and not
        status prose at all — so a sentence asserting a payment state has nothing checking
        it, whichever way it points.

        Blocks the load-298891 shape: "was scheduled for payment on Wednesday, August 12,
        2026, but is not yet showing as paid". Correct tense, correct weekday, every figure
        traced to `cgt_get_load_status`, thirteen checks green — and CargoTel had reported
        no payment state at all, because it has none to report. The claim came from the
        skill prompt, which is exactly why the prompt is not sufficient control for it.

        The negative direction is the one worth the code. Claiming payment invites a "no you
        didn't"; claiming NON-payment to a factoring company chasing money invites a
        duplicate-payment request or a dispute, and reads as authoritative because we are
        the payer.

        Scoped to 6-digit loads. On Transport Pro the same sentence is a reading of
        `payment_status` / `actual_payment_date` / `check_number` and must stay sayable — an
        email spanning both systems escalates before it reaches the gate, so a mixed draft
        does not arrive here.
        """

        cargotel_loads = [
            lid for lid in draft.load_ids if route_load(lid).system is System.QUICKBOOKS
        ]
        if not cargotel_loads:
            return GateCheck(
                name="cargotel_payment_claim",
                passed=True,
                detail="no 6-digit load disclosed; payment state is reportable here",
            )

        claims = _cargotel_payment_claims(draft.reply_body)
        if claims:
            return GateCheck(
                name="cargotel_payment_claim",
                passed=False,
                detail=(
                    f"draft characterises payment on 6-digit load(s) {cargotel_loads}, which "
                    f"this system does not report either way: {claims}. State the scheduled "
                    "date and that someone will confirm where it stands."
                ),
            )
        return GateCheck(
            name="cargotel_payment_claim",
            passed=True,
            detail="reply makes no claim about whether a 6-digit load was paid",
        )

    def _check_paperwork_request(self, draft: SubmitDraftOutput, ctx: ToolContext) -> GateCheck:
        """A 6-digit load's reply may ask for a document only while it is awaiting one.

        Live regression, loads 291174/291180/291117 (A & J Transport). Billing had recorded
        the carrier's invoice on 07/14/2026 and assigned A/P number ``291756-00000938``, so
        ``resolve_payment`` returned ``invoiced_no_terms`` with the note "state that it is
        being processed and give no date". The draft answered: "All three are awaiting your
        carrier invoice ... Please send the carrier invoices." Every figure was grounded and
        every other check was green — on the sender's third attempt to get an answer, after
        two ignored calls.

        The model did not invent it. ``missing_documents`` is returned **non-empty in states
        that are not awaiting paperwork**: an invoice that reached billing by email is never
        in the CargoTel Print Docs menu, so ``('carrier invoice',)`` rides along on both
        ``invoiced_no_terms`` and ``scheduled``. The skill keys "name it and ask the sender to
        send it" off that field, so the wrong branch is one plausible read away, and the
        ``note`` forbidding it is prose the prompt cannot enforce. That is the argument for
        the check: the field the model was told to read genuinely does say "carrier invoice"
        here — only ``billing_state`` says whose problem it is.

        Fails closed when the state cannot be re-derived, which is reachable only for a draft
        that *is* making the ask. A caller who cannot confirm the ball is in the sender's
        court must not tell them it is; the cost is a block on the one shape worth blocking,
        not on every reply.

        Scoped to 6-digit loads. Transport Pro's document requirements come from its own file
        history (``REQUIRED_FOR_PAYMENT``) and have no ``BillingState`` to check against, so
        the ask stays sayable there.
        """

        cargotel_loads = [
            lid for lid in draft.load_ids if route_load(lid).system is System.QUICKBOOKS
        ]
        if not cargotel_loads:
            return GateCheck(
                name="paperwork_request",
                passed=True,
                detail="no 6-digit load disclosed; paperwork requests are not policed here",
            )

        asks = _paperwork_requests(draft.reply_body)
        if not asks:
            return GateCheck(
                name="paperwork_request",
                passed=True,
                detail="reply asks the sender for no document",
            )

        states: dict[str, str] = {}
        for load_id in cargotel_loads:
            try:
                states[load_id] = self._cargotel_billing_state(load_id, ctx).value
            except (ClientError, ToolError) as exc:
                return GateCheck(
                    name="paperwork_request",
                    passed=False,
                    detail=(
                        f"reply asks the sender for paperwork ({asks}) but load {load_id}'s "
                        f"billing state could not be re-derived to justify it: {exc}"
                    ),
                )

        awaiting = [lid for lid, state in states.items() if state == BillingState.AWAITING_PAPERWORK]
        if awaiting:
            return GateCheck(
                name="paperwork_request",
                passed=True,
                detail=f"paperwork request justified — load(s) {awaiting} are awaiting_paperwork",
            )

        spread = ", ".join(f"{lid}={state}" for lid, state in sorted(states.items()))
        return GateCheck(
            name="paperwork_request",
            passed=False,
            detail=(
                f"reply asks the sender for paperwork ({asks}) but no disclosed load is "
                f"awaiting_paperwork: {spread}. An invoice that reached billing by email is "
                "not in the Print Docs menu, so missing_documents can name it while the ball "
                "is with us — say it is being processed and ask the sender for nothing."
            ),
        )

    def _check_deduction_disclosure(
        self, draft: SubmitDraftOutput, email: InboundEmail
    ) -> GateCheck:
        """A 6-digit load's reply neither claims it is clean nor ignores the question.

        Live regression, load 317967 (Shadow Freight, Saint John Capital, 2026-08-14). The
        factor asked three things: the rate, "if there were any fuel advances, no claims and
        no deductions", and whether SJC was the factor of record. The draft answered the
        first and third and said nothing at all about the second. It was blocked — but on the
        *factor* sentence, for an unrelated wording collision. Reword that one clause and the
        draft ships with the money question silently dropped.

        Silence is not neutral here. A factor asks because it is about to advance funds
        against the invoice, so "we didn't mention it" is read as "nothing to report". If an
        advance exists, they advance against the gross, we remit less, and the reply is what
        they relied on. ``_check_coverage`` cannot see it: that check counts load ids, not
        questions.

        The opposite failure is worse and is checked first, whether or not anyone asked.
        ``amount`` on this path is a single payable and ``CgtLoadStatusOutput`` returns no
        line items — the skill forbids breaking it into a rate plus charges for exactly that
        reason. So "no deductions" is ungrounded *by construction*, the same footing as the
        paid/unpaid ban in check 14. Grounding cannot catch it: there is no figure in the
        sentence to trace.

        What passes is the honest shape — name the topic, say this system does not carry it,
        hand it to a human. That is the only wording that is both responsive and true.

        Scoped to 6-digit loads. Transport Pro earning lines carry deductions as real signed
        figures (``-11.25`` reaching a reply as "a deduction of $11.25"), so there the
        question is answerable and a definite answer is grounded.
        """

        cargotel_loads = [
            lid for lid in draft.load_ids if route_load(lid).system is System.QUICKBOOKS
        ]
        if not cargotel_loads:
            return GateCheck(
                name="deduction_disclosure",
                passed=True,
                detail="no 6-digit load disclosed; deductions are reportable here",
            )

        clean_claim = _DEDUCTION_CLEAR_CLAIM_RE.search(draft.reply_body)
        if clean_claim:
            return GateCheck(
                name="deduction_disclosure",
                passed=False,
                detail=(
                    f"draft asserts 6-digit load(s) {cargotel_loads} are clear of "
                    f"advances/deductions — {' '.join(clean_claim.group(0).split())!r} — which "
                    "this path cannot support: the payable is one figure with no line items. "
                    "Say it cannot be confirmed here and that someone will follow up."
                ),
            )

        asked = _deduction_questions(f"{email.subject}\n{strip_quoted(email.body)}")
        if not asked:
            return GateCheck(
                name="deduction_disclosure",
                passed=True,
                detail="sender asked about no advance/deduction, and the draft claims none",
            )

        answered = _DEDUCTION_TOPIC_RE.search(_IN_ADVANCE_RE.sub(" ", draft.reply_body))
        if answered is None:
            return GateCheck(
                name="deduction_disclosure",
                passed=False,
                detail=(
                    f"sender asked about advances/deductions ({asked}) on 6-digit load(s) "
                    f"{cargotel_loads} and the reply does not mention them at all. Silence "
                    "reads as 'none' to a factor about to advance against this invoice — say "
                    "the payment record does not carry it and that someone will follow up."
                ),
            )
        return GateCheck(
            name="deduction_disclosure",
            passed=True,
            detail="advance/deduction question addressed without asserting a clean load",
        )

    def _cargotel_billing_state(self, load_id: str, ctx: ToolContext) -> BillingState:
        """Re-derive one CargoTel load's billing state, carrier terms included.

        The carrier record is fetched for the same reason ``CgtGetLoadStatus`` fetches it: it
        supplies the payment term for a load carrying none, and that is the difference between
        ``invoiced_no_terms`` and ``scheduled``. Its absence is not fatal here — neither state
        is ``awaiting_paperwork``, so the check's answer does not turn on it.
        """

        if ctx.cargotel is None:
            raise ToolError(f"load {load_id} is a 6-digit load but no CargoTel client is wired")
        load = ctx.cargotel.get_load(load_id)
        carrier = None
        if load.carrier_client_id:
            try:
                carrier = ctx.cargotel.get_carrier(load.carrier_client_id)
            except (ClientError, ToolError):
                carrier = None
        return resolve_payment(load, carrier).state
