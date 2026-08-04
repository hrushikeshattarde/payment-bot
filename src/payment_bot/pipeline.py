"""End-to-end orchestration for one inbound email.

This is where the pieces compose into the flow from the PRD architecture diagram:

    intake → shared intake & safety (deterministic) → agent tool-use loop
           → pre-send gate (deterministic) → Slack approval → gated Gmail send

The safety-critical steps run in code around the agent, never inside it:

* **Shared intake & safety (§3.3)** runs first and can stop the run before the agent
  ever sees the email (sensitive change, invalid length, bulk, unsupported system,
  sender authorized for no load).
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
    CheckAuthorizationOutput,
    ClassifyIntentOutput,
    DetectSensitiveChangeOutput,
    ExtractIdentifiersOutput,
)
from payment_bot.tools.submit import SubmitDraftOutput

_log = get_logger("pipeline")

#: Identifies the bulk reply in approval summaries and the audit trail. Not a real skill —
#: no model runs — but `_is_auto_sendable` keys off the skill id, and this must never match
#: PAYMENT_STATUS_SKILL.id, or a bulk reply could auto-send in Phase 2.
_BULK_PORTAL_SKILL_ID = "bulk_portal"

#: Absolute cap on a derived iteration budget, however many loads an email names.
#:
#: The per-load budget scales (see `_iteration_budget`), but it must still terminate: this is
#: the backstop that keeps a runaway model bounded, which is the whole point of having a cap.
#: Set to the same 50 that bounds `agent_max_iterations` in configuration, so a derived
#: budget can never exceed what an operator could have set by hand.
ITERATION_CEILING = 50

#: The §3.3 bulk reply. Deliberately contains no amount, date or load id — see
#: `_bulk_portal_draft` for why that is what makes it safe.
#:
#: Kept short and plain on purpose. It also states no count of loads: naming back what the
#: sender just told us reads as machine-generated, and it is one more number in a body whose
#: safety rests on containing none.
_BULK_PORTAL_BODY = """Hi,

You can check all of these here:

{portal_url}

It's live, and shows the pay date and any deductions per load.

If anything looks off, reply here and we'll sort it out.

Thanks"""


class Outcome(StrEnum):
    SENT = "sent"
    ESCALATED = "escalated"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    #: The draft passed the gate and was posted for human review; nothing was sent. This is
    #: the terminal outcome of a draft-only run and of the Phase 1 processor (§8.5), where
    #: the approval click arrives asynchronously.
    AWAITING_REVIEW = "awaiting_review"
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
        allow_factoring: bool | None = None,
    ) -> None:
        self._tp = tp
        self._gmail = gmail
        self._slack = slack
        self._settings = settings or get_settings()
        self._registry = registry or build_default_registry(audit_sink)
        self._loop = AgentLoop(
            llm,
            self._registry,
            max_iterations=self._settings.agent_max_iterations,
            max_tokens=self._settings.agent_max_tokens,
        )
        # None means "whatever the configuration says". It used to default to False with no
        # way to reach it: `local_runner` never passed the argument, so a factoring sender
        # could not be answered however the deployment was configured.
        #
        # The resolved value is folded back into the settings object so every consumer in
        # one run judges factoring identically: the gate, the intake pre-check, and the
        # policy-resolved `authorized` flag check_authorization shows the model (it reads
        # ctx.settings.allow_factoring). Diverging views here produced a live draft that
        # refused a sender the pipeline had authorized.
        self._allow_factoring = (
            self._settings.allow_factoring if allow_factoring is None else allow_factoring
        )
        self._settings = self._settings.model_copy(
            update={"allow_factoring": self._allow_factoring}
        )
        self._gate = PreSendGate(allow_factoring=self._allow_factoring)
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
            {
                "subject": email.subject,
                "body": email.body,
                "thread_text": email.thread_text,
                # Spreadsheet statements carry their load ids here and nowhere in the body.
                "attachments_text": "\n".join(
                    a.extracted_text for a in email.attachments if a.extracted_text
                ),
            },
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
            # Three tiers (§7 + the sensitive_*_replies policies):
            #   * paperwork/identity actions (void-check/DD attachments, contact change) —
            #     ALWAYS escalate; there is something to file or an identity to verify.
            #   * hard bank/NOA WORDING or an attached NOA, with an answerable ask —
            #     escalates unless the deployment explicitly opted in to answering past it
            #     (sensitive_bank_replies / sensitive_noa_replies / noa_attachment_replies).
            #   * soft boilerplate with an answerable ask — proceeds.
            # A change instruction with no real question escalates in every tier, and the
            # gate's change_acknowledgment check enforces that no reply ever confirms or
            # acts on the instruction it is ignoring.
            allowed_by_policy = (
                (not sensitive.hard_bank or self._settings.sensitive_bank_replies)
                and (not sensitive.hard_noa or self._settings.sensitive_noa_replies)
                and (not sensitive.noa_attachment or self._settings.noa_attachment_replies)
            )
            if (
                sensitive.paperwork
                or not classification.keyword_grounded
                or not allowed_by_policy
            ):
                flags = [f.value for f in sensitive.flags]
                return self._escalate(
                    email, "security", f"sensitive change {flags}", tuple(load_ids), correlation_id
                )
            if sensitive.hard_bank or sensitive.hard_noa or sensitive.noa_attachment:
                # Proceeding past explicit change evidence is a policy decision; make the
                # audit trail shout about it — a human still has to action the request
                # (file the attached NOA, action the bank change).
                _log.warning(
                    "bank_change_language_allowed_by_policy",
                    extra={"correlation_id": correlation_id, "evidence": sensitive.evidence},
                )
            else:
                _log.info(
                    "sensitive_boilerplate_narrowed",
                    extra={"correlation_id": correlation_id, "evidence": sensitive.evidence},
                )

        routes = {lid: route_load(lid).system for lid in load_ids}
        invalid = [lid for lid, sys in routes.items() if sys is System.INVALID]
        if invalid:
            return self._escalate(
                email, "review", f"invalid load length: {invalid}", tuple(load_ids), correlation_id
            )

        if len(load_ids) > self._settings.bulk_threshold:
            # §3.3 portal fallback: answer with the self-service link rather than escalating.
            return self._finalize(
                email,
                self._bulk_portal_draft(email),
                load_ids,
                correlation_id,
                ctx,
                _BULK_PORTAL_SKILL_ID,
            )

        non_tp = [lid for lid, sys in routes.items() if sys is not System.TRANSPORT_PRO]
        if non_tp:
            tp_loads = [lid for lid, sys in routes.items() if sys is System.TRANSPORT_PRO]
            if not tp_loads:
                # 6-digit / QuickBooks path is not wired in this slice.
                return self._escalate(
                    email,
                    "review",
                    f"non-Transport-Pro loads {non_tp}",
                    tuple(load_ids),
                    correlation_id,
                )
            # A mixed email proceeds with its answerable loads. One stray 6-digit number
            # used to stop the whole email — observed live: "Re: 2476340 - Need payment
            # status" carried '107430' in the body and the answerable 7-digit load
            # escalated with it. The dropped ids are logged; a human reviewing the draft
            # sees the full ask in the thread.
            _log.info(
                "non_tp_loads_dropped",
                extra={
                    "correlation_id": correlation_id,
                    "dropped": non_tp,
                    "proceeding_with": tp_loads,
                },
            )
            load_ids = tp_loads

        # Authorization pre-check: the same `check_authorization` the gate re-runs (§5),
        # brought forward to before the model is invoked. When NO load is authorized, no
        # draft could disclose anything, so running the agent only spends the LLM budget on
        # a reply the gate is certain to block — measured on 30 days of live mail, 16 of 29
        # conversations stopped exactly that way. A *partially* authorized email proceeds
        # with only its authorized loads: handing the agent a denied or unresolvable load
        # just burns iterations re-discovering the verdict — observed live, one email with
        # a phantom id that Transport Pro 400s on ate all 12 iterations retrying it and
        # produced no draft. The gate stays authoritative over what the draft actually
        # discloses; this is an efficiency measure, not a replacement.
        unauthorized: list[str] = []
        authorized_loads: list[str] = []
        prenoa_loads: list[str] = []
        for load_id in load_ids:
            auth_out = self._registry.dispatch(
                "check_authorization",
                {
                    "sender_email": email.from_email,
                    "sender_name": email.from_name,
                    "load_id": load_id,
                    "system": routes[load_id].value,
                },
                ctx,
            )
            if not auth_out.ok:
                # Cannot resolve authorization → treat as denied (fail closed, like the gate).
                unauthorized.append(f"{load_id}=ERROR({auth_out.payload.get('error')})")
                continue
            auth = CheckAuthorizationOutput.model_validate(auth_out.payload)
            if not auth.authorized:
                # Carry the tool's reason — it names the fix (e.g. a factoring domain to
                # add to PAYBOT_FACTORING_DOMAINS), which is what the reviewer acts on.
                detail = f" ({auth.reason})" if auth.reason else ""
                unauthorized.append(f"{load_id}={auth.decision.value}{detail}")
                continue
            authorized_loads.append(load_id)
            if auth.pre_noa:
                prenoa_loads.append(load_id)
        if not authorized_loads:
            return self._escalate(
                email,
                "review",
                f"sender not authorized for any load: {unauthorized}",
                tuple(load_ids),
                correlation_id,
            )
        if unauthorized:
            _log.info(
                "authorization_precheck_partial",
                extra={
                    "correlation_id": correlation_id,
                    "unauthorized": unauthorized,
                    "proceeding_with": authorized_loads,
                },
            )
            load_ids = authorized_loads

        # 2. Select the skill by intent -------------------------------------
        # Built from load_ids, not routes: dropped loads (non-TP, unauthorized) must not
        # reappear in the intake prompt.
        routes_map = {lid: routes[lid].value for lid in load_ids}
        skill, intake = self._select_skill(
            email, classification, identifiers, load_ids, routes_map, prenoa_loads
        )

        # 3. Agent tool-use loop --------------------------------------------
        budget = self._iteration_budget(len(load_ids))
        agent_result = self._loop.run(
            system=skill.system_prompt,
            intake_prompt=intake,
            allowed_tools=skill.allowed_tools,
            ctx=ctx,
            max_iterations=budget,
        )
        if agent_result.draft is None:
            # Include what the model wrote. Without it "produced no draft" is unactionable —
            # it hides whether the answer was complete but delivered as prose, or never
            # arrived at all.
            wrote = " ".join(agent_result.final_text.split())
            aside = f"; model wrote: {wrote[:300]!r}" if wrote else ""
            # Name the budget and the load count on an exhausted run. Without them the
            # reviewer cannot tell a model that misbehaved from one that was simply given
            # less budget than the email needed.
            if agent_result.stop_reason == "max_iterations":
                aside = f"; {len(load_ids)} load(s), budget {budget} iterations{aside}"
            return self._escalate(
                email,
                "review",
                f"agent produced no draft (stop_reason={agent_result.stop_reason}){aside}",
                tuple(load_ids),
                correlation_id,
            )
        draft = agent_result.draft

        return self._finalize(
            email,
            draft,
            load_ids,
            correlation_id,
            ctx,
            skill.id,
            noa_request_expected=bool(prenoa_loads),
        )

    # -- gate → approval → send, shared by every draft path -------------------
    def _finalize(
        self,
        email: InboundEmail,
        draft: SubmitDraftOutput,
        load_ids: list[str],
        correlation_id: str,
        ctx: ToolContext,
        skill_id: str,
        noa_request_expected: bool = False,
    ) -> PipelineResult:
        """Run the gate, then approval, then send or leave the draft for review.

        Extracted so the deterministic bulk-portal reply takes the *same* route as an
        agent-produced draft. There is no second path to a sent email, and no draft that
        reaches a mailbox without passing §5.
        """

        # 4. Pre-send gate (deterministic, §5) ------------------------------
        # The bulk portal reply is code-authored and deliberately names no load, so it
        # carries no expected-coverage list; an agent draft must address every load the
        # intake handed it.
        expected = None if skill_id == _BULK_PORTAL_SKILL_ID else tuple(load_ids)
        gate_result = self._gate.evaluate(
            draft=draft,
            email=email,
            ctx=ctx,
            expected_load_ids=expected,
            noa_request_expected=noa_request_expected,
        )
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
        if self._is_auto_sendable(skill_id, load_ids):
            return self._send(email, draft, draft.reply_body, correlation_id, gate_result)

        summary = ApprovalSummary(
            from_=email.from_email,
            intents=(skill_id,),
            load_ids=tuple(load_ids),
            key_facts=(f"loads={load_ids}", f"gate=passed({len(gate_result.checks)} checks)"),
            cc=self._settings.reply_cc,
        )
        self._slack.post_approval(
            self._settings.slack_approval_channel, summary, draft.reply_body, correlation_id
        )
        decision = self._resolver.resolve(correlation_id, draft.reply_body)

        if decision.action is ApprovalAction.DEFER:
            # Draft-only / Phase 1: posted for review, decision arrives out of band.
            _log.info("draft_awaiting_review", extra={"correlation_id": correlation_id})
            return PipelineResult(
                Outcome.AWAITING_REVIEW,
                "draft ready for review; nothing sent",
                correlation_id,
                draft,
                gate_result,
            )

        if decision.action is ApprovalAction.REJECT:
            return PipelineResult(
                Outcome.REJECTED, "human rejected the draft", correlation_id, draft, gate_result
            )

        body = draft.reply_body
        if decision.action is ApprovalAction.EDIT and decision.edited_text is not None:
            body = decision.edited_text
            # Re-run the gate on the human's edit — approval does not bypass grounding/auth.
            edited = draft.model_copy(update={"reply_body": body})
            regate = self._gate.evaluate(
                draft=edited, email=email, ctx=ctx, expected_load_ids=expected
            )
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
        prenoa_loads: list[str],
    ) -> tuple[Skill, str]:
        """Pick the skill + build its intake from the classified intent.

        Always answers: by the time this runs the email has at least one valid,
        authorized Transport Pro load, and an unclear ask about a real load defaults to
        payment status — a human reviews the draft regardless.
        """

        has_payment = Intent.PAYMENT_STATUS in classification.intents
        has_rate = Intent.RATE_VERIFICATION in classification.intents

        if has_payment and has_rate:
            # §3.5 proper handling is "run both skills and merge", which is not wired. Until
            # it is, answer the more specific of the two rather than refusing: on live mail
            # this was the largest single cause of escalation, 5 of 20 emails, and every one
            # of them was answerable.
            #
            # A quoted figure is the tell. Someone who wrote an amount wants it checked; with
            # no amount the ask is almost always "where is my money". Either way the reply
            # covers only one of the two questions, so it stays a human-reviewed draft.
            chosen = RATE_VERIFICATION_SKILL if identifiers.stated_rates else PAYMENT_STATUS_SKILL
            _log.info(
                "combined_intent_narrowed",
                extra={
                    "chosen_skill": chosen.id,
                    "stated_rates": len(identifiers.stated_rates),
                },
            )
            if chosen is RATE_VERIFICATION_SKILL:
                return chosen, build_rate_verification_intake(
                    email,
                    load_ids,
                    routes_map,
                    identifiers.stated_rates,
                    identifiers.factoring_company,
                    signature=self._settings.reply_signature,
                    documents_email=self._settings.documents_email,
                    prenoa_loads=prenoa_loads,
                )
            return chosen, build_payment_status_intake(
                email,
                load_ids,
                routes_map,
                signature=self._settings.reply_signature,
                documents_email=self._settings.documents_email,
                prenoa_loads=prenoa_loads,
            )
        if has_rate:
            intake = build_rate_verification_intake(
                email,
                load_ids,
                routes_map,
                identifiers.stated_rates,
                identifiers.factoring_company,
                signature=self._settings.reply_signature,
                documents_email=self._settings.documents_email,
                prenoa_loads=prenoa_loads,
            )
            return RATE_VERIFICATION_SKILL, intake
        if has_payment:
            return PAYMENT_STATUS_SKILL, build_payment_status_intake(
                email,
                load_ids,
                routes_map,
                signature=self._settings.reply_signature,
                documents_email=self._settings.documents_email,
                prenoa_loads=prenoa_loads,
            )
        # Uncertain intent but the email names loads (possibly only inside an attached
        # statement — "please see attached" carries no keyword). Same reasoning as the
        # classifier's own fallback: this inbox exists to answer payment status, and a
        # human reviews the draft regardless.
        _log.info(
            "intent_defaulted_payment_status",
            extra={"load_count": len(load_ids)},
        )
        return PAYMENT_STATUS_SKILL, build_payment_status_intake(
            email,
            load_ids,
            routes_map,
            signature=self._settings.reply_signature,
            documents_email=self._settings.documents_email,
            prenoa_loads=prenoa_loads,
        )

    def _bulk_portal_draft(self, email: InboundEmail) -> SubmitDraftOutput:
        """Build the §3.3 bulk reply: point the sender at the self-service portal.

        Written in code, not by the model, because there is nothing to reason about — and
        because it deliberately states **no** load data. That is what lets it pass the gate
        honestly rather than by exemption: ``load_ids`` is empty, so the authorization,
        bulk and length checks have nothing to authorize or route, and the body carries no
        amount or date for grounding to object to. A bulk reply discloses nothing, so the
        gate's own "no loads disclosed" branch applies.
        """

        body = _BULK_PORTAL_BODY.format(portal_url=self._settings.portal_url)
        return SubmitDraftOutput(
            reply_body=body,
            to=email.from_email,
            load_ids=[],  # nothing about any load is disclosed — see the docstring
            citations=[],
        )

    def _is_auto_sendable(self, skill_id: str, load_ids: list[str]) -> bool:
        """Phase 2 (§8.5): only a clean single-load payment_status may skip approval.

        Rate verification is never auto-sent — a rate mismatch must always be human-reviewed.
        ``draft_only`` overrides the phase entirely: nothing auto-sends in a draft-only run.
        """

        if self._settings.draft_only:
            return False
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

    def _iteration_budget(self, load_count: int) -> int:
        """Iteration budget for an email naming ``load_count`` loads.

        The skill procedures run per load, so cost grows with the load count while the
        configured cap does not. One load keeps exactly the configured budget — so nothing
        about the single-load case changes — and each additional load adds one more per-load
        pass, clamped to :data:`ITERATION_CEILING` so the loop still terminates.
        """

        extra = max(0, load_count - 1) * self._settings.agent_iterations_per_extra_load
        return min(self._settings.agent_max_iterations + extra, ITERATION_CEILING)

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
