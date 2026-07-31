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
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from payment_bot.domain import route_load
from payment_bot.errors import ClientError, ToolError
from payment_bot.grounding import extract_date_tokens, extract_money_tokens
from payment_bot.logging import get_logger
from payment_bot.models import AuthDecision, InboundEmail, SensitiveFlag, System
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import (
    AttachmentMeta,
    CheckAuthorization,
    CheckAuthorizationInput,
    DetectSensitiveChange,
    DetectSensitiveChangeInput,
)
from payment_bot.tools.submit import SubmitDraftOutput

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
    ) -> GateResult:
        checks = [
            self._check_length_routing(draft),
            self._check_bulk(draft, ctx),
            self._check_authorization(draft, email, ctx),
            self._check_sensitive_change(email, ctx),
            self._check_placeholders(draft),
            self._check_grounding(draft, ctx),
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
                attachments_metadata=[
                    AttachmentMeta(filename=a.filename, mime_type=a.mime_type)
                    for a in email.attachments
                ],
            ),
            ctx,
        )
        flags = [f for f in outcome.flags if f is not SensitiveFlag.NONE]
        if flags:
            return GateCheck(
                name="sensitive_change",
                passed=False,
                detail=f"sensitive change detected: {[f.value for f in flags]}",
            )
        return GateCheck(
            name="sensitive_change", passed=True, detail="no bank/NOA/contact change detected"
        )

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

    def _check_grounding(self, draft: SubmitDraftOutput, ctx: ToolContext) -> GateCheck:
        # Magnitudes on both sides — the ledger stores them that way, see record_amount.
        stated_money = {abs(amount) for amount in extract_money_tokens(draft.reply_body)}
        ungrounded_money = stated_money - ctx.ledger.grounded_amounts
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
        return GateCheck(
            name="grounding", passed=True, detail="every amount and date in the draft is grounded"
        )
