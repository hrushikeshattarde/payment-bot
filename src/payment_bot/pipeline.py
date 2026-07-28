"""End-to-end orchestration for one inbound email.

This is where the pieces compose into the flow from the PRD architecture diagram:

    intake → shared intake & safety (deterministic) → agent tool-use loop
           → pre-send gate (deterministic) → Slack approval → gated Gmail send

The safety-critical steps run in code around the agent, never inside it:

* **Shared intake & safety (§3.3)** runs first and can stop the run before the agent
  ever sees the email (sensitive change, invalid length, bulk, unsupported system).
* **The pre-send gate (§5)** runs after the agent and is authoritative; a block escalates.
* **Sending** happens only after the gate passes and (Phase 1) a human approves — and the
  gate is re-run on any human edit.

Anything unexpected escalates rather than sends: the system fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from payment_bot.agent import (
    AgentLoop,
    Skill,
    build_payment_status_intake,
    build_rate_verification_intake,
)
from payment_bot.agent.skills import PAYMENT_STATUS_SKILL, RATE_VERIFICATION_SKILL
from payment_bot.clients import (
    ApprovalAction,
    ApprovalResolver,
    ApprovalSummary,
    GmailClient,
    LlmClient,
    SentMessage,
    SlackClient,
    TransportProClient,
)
from payment_bot.config import RolloutPhase, Settings, get_settings
from payment_bot.domain import route_load
from payment_bot.errors import PaymentBotError
from payment_bot.gate import GateResult, PreSendGate
from payment_bot.grounding import GroundingLedger
from payment_bot.logging import AuditSink, get_logger
from payment_bot.models import InboundEmail, Intent, SensitiveAction, System
from payment_bot.tools import ToolContext, ToolRegistry, build_default_registry
from payment_bot.tools.shared import (
    ClassifyIntentOutput,
    DetectSensitiveChangeOutput,
    ExtractIdentifiersOutput,
)
from payment_bot.tools.submit import SubmitDraftOutput

_log = get_logger("pipeline")


class Outcome(StrEnum):
    SENT = "sent"
    ESCALATED = "escalated"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    NO_ACTION = "no_action"


@dataclass(slots=True)
class PipelineResult:
    """What happened to one email."""

    outcome: Outcome
    detail: str
    correlation_id: str
    draft: SubmitDraftOutput | None = None
    gate_result: GateResult | None = None
    sent_message: SentMessage | None = None


class PaymentBotPipeline:
    """Wires the clients, tools, agent, and gate into the per-email flow."""

    def __init__(
        self,
        *,
        tp: TransportProClient,
        gmail: GmailClient,
        slack: SlackClient,
        llm: LlmClient,
        approval_resolver: ApprovalResolver,
        settings: Settings | None = None,
        audit_sink: AuditSink | None = None,
        registry: ToolRegistry | None = None,
        allow_factoring: bool = False,
    ) -> None:
        self._tp = tp
        self._gmail = gmail
        self._slack = slack
        self._settings = settings or get_settings()
        self._registry = registry or build_default_registry(audit_sink)
        self._loop = AgentLoop(
            llm, self._registry, max_iterations=self._settings.agent_max_iterations
        )
        self._gate = PreSendGate(allow_factoring=allow_factoring)
        self._resolver = approval_resolver

    # -- public API ----------------------------------------------------------
    def process_email(self, email: InboundEmail) -> PipelineResult:
        correlation_id = email.message_id
        try:
            return self._process(email, correlation_id)
        except PaymentBotError as exc:  # expected-but-unhandled → fail closed
            return self._escalate(email, "review", f"unhandled error: {exc}", (), correlation_id)
        except Exception as exc:  # last-resort safety net; never send on a bug
            _log.exception("pipeline_crash", extra={"correlation_id": correlation_id})
            return self._escalate(email, "security", f"pipeline crash: {exc}", (), correlation_id)

    # -- internal flow -------------------------------------------------------
    def _process(self, email: InboundEmail, correlation_id: str) -> PipelineResult:
        ledger = GroundingLedger()
        ctx = ToolContext(
            tp=self._tp,
            ledger=ledger,
            correlation_id=correlation_id,
            settings=self._settings,
        )

        # 1. Shared intake & safety (deterministic, §3.3) --------------------
        # Run through the registry so every intake tool call is audited (§8.1) too.
        cls_out = self._registry.dispatch(
            "classify_intent",
            {
                "email_subject": email.subject,
                "email_body": email.body,
                "thread_text": email.thread_text,
            },
            ctx,
        )
        classification = ClassifyIntentOutput.model_validate(cls_out.payload)

        ident_out = self._registry.dispatch(
            "extract_identifiers",
            {"subject": email.subject, "body": email.body, "thread_text": email.thread_text},
            ctx,
        )
        identifiers = ExtractIdentifiersOutput.model_validate(ident_out.payload)
        load_ids = identifiers.load_ids
        if not load_ids:
            # No valid id — carrier-name lookup / clarification is out of this slice.
            return self._escalate(
                email, "review", "no valid 6/7-digit load id found", (), correlation_id
            )

        det_out = self._registry.dispatch(
            "detect_sensitive_change",
            {
                "subject": email.subject,
                "body": email.body,
                "attachments_metadata": [
                    {"filename": a.filename, "mime_type": a.mime_type} for a in email.attachments
                ],
            },
            ctx,
        )
        sensitive = DetectSensitiveChangeOutput.model_validate(det_out.payload)
        if sensitive.action is SensitiveAction.ESCALATE:
            flags = [f.value for f in sensitive.flags]
            return self._escalate(
                email, "security", f"sensitive change {flags}", tuple(load_ids), correlation_id
            )

        routes = {lid: route_load(lid).system for lid in load_ids}
        invalid = [lid for lid, sys in routes.items() if sys is System.INVALID]
        if invalid:
            return self._escalate(
                email, "review", f"invalid load length: {invalid}", tuple(load_ids), correlation_id
            )

        if len(load_ids) > self._settings.bulk_threshold:
            # Portal fallback body is out of this slice; escalate so a human sends the link.
            return self._escalate(
                email,
                "review",
                f"bulk request ({len(load_ids)} loads) → use portal {self._settings.portal_url}",
                tuple(load_ids),
                correlation_id,
            )

        non_tp = [lid for lid, sys in routes.items() if sys is not System.TRANSPORT_PRO]
        if non_tp:
            # 6-digit / QuickBooks path is not wired in this slice.
            return self._escalate(
                email, "review", f"non-Transport-Pro loads {non_tp}", tuple(load_ids), correlation_id
            )

        # 2. Select the skill by intent -------------------------------------
        routes_map = {lid: sys.value for lid, sys in routes.items()}
        selected = self._select_skill(email, classification, identifiers, load_ids, routes_map)
        if selected is None:
            intents = [i.value for i in classification.intents]
            return self._escalate(
                email,
                "review",
                f"intent not answerable in this slice: {intents}",
                tuple(load_ids),
                correlation_id,
            )
        skill, intake = selected

        # 3. Agent tool-use loop --------------------------------------------
        agent_result = self._loop.run(
            system=skill.system_prompt,
            intake_prompt=intake,
            allowed_tools=skill.allowed_tools,
            ctx=ctx,
        )
        if agent_result.draft is None:
            return self._escalate(
                email,
                "review",
                f"agent produced no draft (stop_reason={agent_result.stop_reason})",
                tuple(load_ids),
                correlation_id,
            )
        draft = agent_result.draft

        # 4. Pre-send gate (deterministic, §5) ------------------------------
        gate_result = self._gate.evaluate(draft=draft, email=email, ctx=ctx)
        if not gate_result.allowed:
            return self._escalate(
                email,
                "review",
                f"pre-send gate blocked: {gate_result.reasons}",
                tuple(load_ids),
                correlation_id,
                gate_result=gate_result,
                draft=draft,
            )

        # 5. Approval (Phase 1) or selective auto-send (Phase 2, §8.5) ------
        if self._is_auto_sendable(skill.id, load_ids):
            return self._send(email, draft, draft.reply_body, correlation_id, gate_result)

        summary = ApprovalSummary(
            from_=email.from_email,
            intents=(skill.id,),
            load_ids=tuple(load_ids),
            key_facts=(f"loads={load_ids}", f"gate=passed({len(gate_result.checks)} checks)"),
        )
        self._slack.post_approval(
            self._settings.slack_approval_channel, summary, draft.reply_body, correlation_id
        )
        decision = self._resolver.resolve(correlation_id, draft.reply_body)

        if decision.action is ApprovalAction.REJECT:
            return PipelineResult(
                Outcome.REJECTED, "human rejected the draft", correlation_id, draft, gate_result
            )

        body = draft.reply_body
        if decision.action is ApprovalAction.EDIT and decision.edited_text is not None:
            body = decision.edited_text
            # Re-run the gate on the human's edit — approval does not bypass grounding/auth.
            edited = draft.model_copy(update={"reply_body": body})
            regate = self._gate.evaluate(draft=edited, email=email, ctx=ctx)
            if not regate.allowed:
                return self._escalate(
                    email,
                    "review",
                    f"edited draft failed gate: {regate.reasons}",
                    tuple(load_ids),
                    correlation_id,
                    gate_result=regate,
                    draft=edited,
                )
            gate_result = regate

        return self._send(email, draft, body, correlation_id, gate_result)

    # -- helpers -------------------------------------------------------------
    def _select_skill(
        self,
        email: InboundEmail,
        classification: ClassifyIntentOutput,
        identifiers: ExtractIdentifiersOutput,
        load_ids: list[str],
        routes_map: dict[str, str],
    ) -> tuple[Skill, str] | None:
        """Pick the skill + build its intake from the classified intent.

        Returns ``None`` when the email is not answerable in this slice (combined
        payment+rate intent, or unclear) — the caller escalates.
        """

        has_payment = Intent.PAYMENT_STATUS in classification.intents
        has_rate = Intent.RATE_VERIFICATION in classification.intents

        if has_payment and has_rate:
            # §3.5 combined intent (run both, merge into one reply) is not yet wired.
            return None
        if has_rate:
            intake = build_rate_verification_intake(
                email, load_ids, routes_map, identifiers.stated_rates, identifiers.factoring_company
            )
            return RATE_VERIFICATION_SKILL, intake
        if has_payment:
            return PAYMENT_STATUS_SKILL, build_payment_status_intake(email, load_ids, routes_map)
        return None  # uncertain / neither

    def _is_auto_sendable(self, skill_id: str, load_ids: list[str]) -> bool:
        """Phase 2 (§8.5): only a clean single-load payment_status may skip approval.

        Rate verification is never auto-sent — a rate mismatch must always be human-reviewed.
        """

        return (
            self._settings.rollout_phase is RolloutPhase.SELECTIVE_AUTOSEND
            and skill_id == PAYMENT_STATUS_SKILL.id
            and len(load_ids) == 1
        )

    def _send(
        self,
        email: InboundEmail,
        draft: SubmitDraftOutput,
        body: str,
        correlation_id: str,
        gate_result: GateResult,
    ) -> PipelineResult:
        sent = self._gmail.send_reply(
            thread_id=email.thread_id,
            message_id_in_reply_to=email.message_id,
            body=body,
            to=email.from_email,
        )
        _log.info("email_sent", extra={"correlation_id": correlation_id, "to": email.from_email})
        return PipelineResult(
            Outcome.SENT, "reply sent", correlation_id, draft, gate_result, sent_message=sent
        )

    def _escalate(
        self,
        email: InboundEmail,
        severity: str,
        reason: str,
        load_ids: tuple[str, ...],
        correlation_id: str,
        *,
        gate_result: GateResult | None = None,
        draft: SubmitDraftOutput | None = None,
    ) -> PipelineResult:
        channel = (
            self._settings.slack_security_channel
            if severity == "security"
            else self._settings.slack_approval_channel
        )
        self._slack.post_escalation(channel, severity, reason, load_ids, correlation_id)
        _log.info(
            "escalated",
            extra={"correlation_id": correlation_id, "severity": severity, "reason": reason},
        )
        return PipelineResult(
            Outcome.ESCALATED if draft is None else Outcome.BLOCKED,
            reason,
            correlation_id,
            draft,
            gate_result,
        )
