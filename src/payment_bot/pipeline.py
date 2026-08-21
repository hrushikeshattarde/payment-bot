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
from datetime import date
from enum import StrEnum

from payment_bot.agent import (
    AgentLoop,
    Skill,
    build_cargotel_payment_status_intake,
    build_payment_status_intake,
    build_rate_verification_intake,
)
from payment_bot.agent.skills import (
    CARGOTEL_PAYMENT_STATUS_SKILL,
    PAYMENT_STATUS_SKILL,
    RATE_VERIFICATION_SKILL,
)
from payment_bot.clients import (
    ApprovalAction,
    ApprovalResolver,
    ApprovalSummary,
    CargoTelClient,
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
from payment_bot.id_filter import MIN_CANDIDATES, IdFilterMode, apply_filter, classify
from payment_bot.logging import AuditSink, get_logger
from payment_bot.models import AuthDecision, InboundEmail, Intent, SensitiveAction, System
from payment_bot.roster_candidate import append_manual_entries, build_candidate, log_candidate
from payment_bot.tools import ToolContext, ToolRegistry, build_default_registry
from payment_bot.tools.shared import (
    CheckAuthorizationOutput,
    ClassifyIntentOutput,
    DetectSensitiveChangeOutput,
    ExtractIdentifiersOutput,
    _factor_names_match,
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


def _group_by_reason(entries: list[tuple[str, str]]) -> str:
    """Render ``(load_id, reason)`` pairs with the loads that share a reason grouped together.

    An infrastructure failure gives every load in the email the same sentence. Live on a
    five-load Tru Funding email whose CargoTel cookie could not be read: five copies of one
    ~200-character credentials message, and the single fact a reviewer needed — *the AWS
    credentials are missing* — was buried behind four repetitions of itself.

    Per-load reasons still list per load, which is what makes the grouping safe to read: a
    genuine mix of causes stays visible rather than being flattened into whichever came first.
    """

    grouped: dict[str, list[str]] = {}
    for load_id, reason in entries:
        grouped.setdefault(reason, []).append(load_id)
    return "; ".join(f"{', '.join(loads)}={reason}" for reason, loads in grouped.items())

#: The §3.3 bulk reply. Deliberately contains no amount, date or load id — see
#: `_bulk_portal_draft` for why that is what makes it safe.
#:
#: Kept short and plain on purpose. It also states no count of loads: naming back what the
#: sender just told us reads as machine-generated, and it is one more number in a body whose
#: safety rests on containing none.
#:
#: The wording asks for a REVISED LIST rather than "reply if anything looks off", and that is
#: the operational point rather than a style preference: a factor's collections statement runs
#: to a dozen or more invoices, and an open-ended "let us know" invites the whole list back.
#: Asking only for the rows the portal shows as unpaid is what turns the next message into
#: something answerable. Written for the TAFS collections shape — fourteen invoices across
#: fourteen carriers, all of which the portal can already answer.
#:
#: The URL stays a ``{portal_url}`` placeholder so PAYBOT_PORTAL_URL remains the one place it
#: is set; hard-coding it here would put the same address in two files.
_BULK_PORTAL_BODY = """The payment status for these loads are listed on our website - {portal_url}

Once you have checked the website, please send a revised list for loads that show payment not processed on our Website."""


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
    #: True when the agent loop had already run when this outcome was decided.
    #:
    #: Only the retry budget reads it, and only to price a repeat. Escalating BEFORE the loop
    #: costs one id-filter call; escalating after it costs the whole loop — twelve turns for
    #: one load, up to fifty for five. A budget counted in attempts prices those the same,
    #: which is what made the expensive one worth three of them.
    after_agent: bool = False


class PaymentBotPipeline:
    """Wires the clients, tools, agent, and gate into the per-email flow."""

    def __init__(
        self,
        *,
        tp: TransportProClient,
        cargotel: CargoTelClient | None = None,
        gmail: GmailClient,
        slack: SlackClient,
        llm: LlmClient,
        approval_resolver: ApprovalResolver,
        settings: Settings | None = None,
        audit_sink: AuditSink | None = None,
        registry: ToolRegistry | None = None,
        allow_factoring: bool | None = None,
        today: date | None = None,
    ) -> None:
        self._tp = tp
        self._llm = llm
        # Resolved once per pipeline rather than per email so a long-running processor cannot
        # render a date under one day and judge its tense under the next. Injectable because
        # fixture data has fixed dates: a test asserting "Thursday, August 6, 2026" is only
        # meaningful against a pinned today.
        self._today = today or date.today()
        # Optional and defaulted so every existing caller — the demo runner, the local
        # runner, the integration tests — keeps working without a CargoTel client. Unset,
        # 6-digit loads behave exactly as they did before this path existed.
        self._cargotel = cargotel
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
            cargotel=self._cargotel,
            ledger=ledger,
            correlation_id=correlation_id,
            settings=self._settings,
            today=self._today,
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
                # Portal collections mail puts its invoice table in the HTML only, so the
                # load id can exist nowhere else. See InboundEmail.html_text.
                "html_text": email.html_text,
            },
            ctx,
        )
        identifiers = ExtractIdentifiersOutput.model_validate(ident_out.payload)
        # The id filter runs HERE, before routing and authorization.
        #
        # It was moved AFTER authorization as a cost measure: one model call, paid by every
        # email with two or more candidates including the majority that were about to be
        # refused. Moved back, because what it bought was worse than what it saved. A WEX
        # collections table writes the motor-carrier number in a header-row column and the
        # load two columns along; the 24-character label guard cannot see across rows, so
        # 1133075 reached authorization as a load and the escalation reported the carrier's
        # MC number as one of the loads the sender had asked about. Same shape twice more the
        # same day: an MC and a DOT number off a rate confirmation on LD#2550332.
        #
        # Cheap where it matters: MIN_CANDIDATES means a single-id email never calls the model
        # at all, so the cost lands only on multi-candidate mail — which is exactly where the
        # phantoms are. Correct identification of WHICH load and WHICH carrier an email is
        # about is worth more than the call.
        load_ids = self._filter_ids(list(identifiers.load_ids), email, correlation_id)
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
                "html_text": email.html_text,
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

        # §3.3 portal fallback, decided in TWO places, because one is not enough.
        #
        # Here, cheaply, on what the sender WROTE. Reading PDF attachments made the raw id
        # count stop meaning "how many loads is this about": an MDR Capital enquiry naming one
        # load carried an invoice PDF whose client-id, client-invoice and two reference numbers
        # took the count to ten, and the sender got "you can check all of these here" instead
        # of an answer. None of those labels can be suppressed — `invoice` is deliberately
        # excluded from _NOT_A_LOAD_LABEL_RE and `reference` is a positive load label.
        #
        # Falling back to the full set when the sender named NONE is what keeps the genuine
        # case working AND keeps it cheap: a McLeod statement says only "see the attached
        # statement for invoice detail", so its ids are attachment-only, the fallback fires,
        # and it deflects here without spending an authorization lookup per load.
        written = set(identifiers.written_load_ids)
        written_ids = [lid for lid in load_ids if lid in written]
        if len(written_ids or load_ids) > self._settings.bulk_threshold:
            return self._finalize(
                email,
                self._bulk_portal_draft(email),
                load_ids,
                correlation_id,
                ctx,
                _BULK_PORTAL_SKILL_ID,
            )

        # Answer the loads the sender NAMED, and look up nothing else.
        #
        # This used to sit after authorization, which meant every load-shaped number an
        # attachment contributed was looked up first. A BasicBlock rate verification naming
        # ONE load, #322500, carried paperwork whose 6-digit numbers took the count to eleven:
        # eleven lookups, a CargoTel "no load exists" wall in the escalation, and ten ids
        # reported back as loads the sender had asked about. None of them was ever going to be
        # answered — the narrowing removed them again further down — so the only thing the
        # lookups bought was noise.
        #
        # THE TRADE, and it is a real one: the second bulk check counts what survived
        # authorization, to tell "one load written, nine reference numbers attached" (answer
        # the one) from "one written, forty of the sender's REAL loads attached" (deflect to
        # the portal). Narrowing here collapses the second case into the first, so such an
        # email now answers the written load instead of sending a portal link. Taken
        # deliberately: looking up numbers the sender never mentioned is the worse failure.
        load_ids = self._narrow_to_written(load_ids, identifiers.written_load_ids, correlation_id)
        routes = {lid: routes[lid] for lid in load_ids}

        tp_loads = [lid for lid, sys in routes.items() if sys is System.TRANSPORT_PRO]
        # `System.QUICKBOOKS` is the §4.1 routing label for 6-digit ids. CargoTel is the
        # system that actually holds them; QuickBooks receives them downstream as bills.
        cgt_loads = [lid for lid, sys in routes.items() if sys is System.QUICKBOOKS]
        cargotel_available = self._cargotel is not None and self._settings.cargotel_replies

        if not cargotel_available:
            if not tp_loads:
                # Unchanged behaviour where CargoTel is not enabled: a 6-digit-only email
                # escalates exactly as it always did.
                return self._escalate(
                    email,
                    "review",
                    f"non-Transport-Pro loads {cgt_loads}",
                    tuple(load_ids),
                    correlation_id,
                )
            if cgt_loads:
                # A mixed email proceeds with its answerable loads. One stray 6-digit number
                # used to stop the whole email — observed live: "Re: 2476340 - Need payment
                # status" carried '107430' in the body and the answerable 7-digit load
                # escalated with it. The dropped ids are logged; a human reviewing the draft
                # sees the full ask in the thread.
                _log.info(
                    "non_tp_loads_dropped",
                    extra={
                        "correlation_id": correlation_id,
                        "dropped": cgt_loads,
                        "proceeding_with": tp_loads,
                    },
                )
            load_ids = tp_loads
            system = System.TRANSPORT_PRO
        elif tp_loads and cgt_loads:
            # Both systems named, and both answerable — so dropping one set would discard a
            # real question rather than a stray number. One reply cannot be produced by two
            # rule sets, so a human takes the whole email. Same unsolved problem as §3.5.
            return self._escalate(
                email,
                "review",
                f"email spans both systems: Transport Pro {tp_loads}, CargoTel {cgt_loads}",
                tuple(load_ids),
                correlation_id,
            )
        elif cgt_loads:
            load_ids = cgt_loads
            system = System.QUICKBOOKS
        else:
            load_ids = tp_loads
            system = System.TRANSPORT_PRO

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
        unauthorized, authorized_loads, prenoa_loads, unresolved_loads, cancelled_loads = (
            self._authorize_loads(email, load_ids, routes, ctx)
        )
        # POLICY: add the sender's domain for the factor already on the load, then retry once.
        # Off by default; see Settings.auto_add_factoring_domains for what this trades away and
        # the four cases it still refuses.
        if (
            not authorized_loads
            and self._settings.auto_add_factoring_domains
            and self._auto_add_factoring_domains(email, tuple(load_ids), ctx, correlation_id)
        ):
            unauthorized, authorized_loads, prenoa_loads, unresolved_loads, cancelled_loads = (
                self._authorize_loads(email, load_ids, routes, ctx)
            )

        if not authorized_loads:
            # Two different failures, reported as two. Folding a cancelled load into
            # "sender not authorized" blamed the sender for a load that no longer exists and
            # read like a Transport Pro outage; on a Cleanpeace enquiry the one line carried
            # three genuine denials and one cancellation with no way to tell them apart.
            parts: list[str] = []
            if unauthorized:
                parts.append(f"sender not authorized for any load: {_group_by_reason(unauthorized)}")
            # Only an id the SENDER WROTE may be called cancelled. Transport Pro answers a
            # cancelled load and a number that was never a load with the same 400, so folding
            # them together told a reviewer that a DOT number off an attached rate
            # confirmation had been cancelled. Live on LD#2550332: the attachment's header row
            # put "MC Number" and "DOT" further than the label guard's 24 characters from the
            # values beneath them, so 1002691 and 3210943 both survived as load candidates.
            cancelled_written = [lid for lid in cancelled_loads if lid in written]
            if cancelled_written:
                parts.append(
                    f"load(s) cancelled — Transport Pro holds no payable record: "
                    f"{', '.join(cancelled_written)}"
                )
            # Named once, for every bucket at once: which of the ids above the sender never
            # actually wrote. The reply would not have covered them either — `_narrow_to_written`
            # sees to that — but the escalation names them, and a reviewer reading a DENY or a
            # cancellation deserves to know it is about a number a PDF contributed.
            unwritten = [lid for lid in load_ids if lid not in written]
            if unwritten:
                parts.append(
                    "id(s) the sender never wrote, contributed by an attachment: "
                    f"{', '.join(unwritten)}"
                )
            reason = "; ".join(parts) or "no load could be authorized"
            # Assemble the roster packet BEFORE escalating, so the reviewer gets the evidence
            # in the same place as the refusal rather than having to go and find it. This
            # decides nothing — the escalation is unchanged either way; see roster_candidate.
            packet = self._roster_candidate(email, tuple(load_ids), ctx, correlation_id)
            if packet:
                reason = f"{reason}\n\n{packet}"
            return self._escalate(
                email,
                "review",
                reason,
                tuple(load_ids),
                correlation_id,
            )
        if unauthorized or cancelled_loads:
            # `cancelled_loads` belongs in this condition as much as `unauthorized` does.
            # When a cancelled load was the ONLY non-authorized one, `unauthorized` is empty,
            # and narrowing to `authorized_loads` was skipped — so the cancelled id stayed in
            # the answerable set and the agent was sent to look up a load Transport Pro has
            # already said it holds nothing for.
            _log.info(
                "authorization_precheck_partial",
                extra={
                    "correlation_id": correlation_id,
                    "unauthorized": _group_by_reason(unauthorized),
                    "cancelled": cancelled_loads,
                    "proceeding_with": authorized_loads,
                },
            )
            load_ids = authorized_loads

        # Loads the sender NAMED that this reply will not cover, so the draft can say so
        # without naming them. DENY only: a load Transport Pro resolved and this sender is
        # not entitled to. Cancellations and lookup failures are excluded on purpose — the
        # first is answerable and the second is already surfaced as `unlocated_loads`, and
        # Only ids the sender wrote reach authorization at all now, so nothing an attachment
        # contributed can appear here.
        withheld_named = [
            lid
            for lid, reason in unauthorized
            if lid in written and reason.startswith(AuthDecision.DENY.value)
        ]

        # The bulk decision's SECOND half, on the set that survived authorization. This is the
        # count that actually matters: a phantom id from an attachment fails authorization and
        # drops out above, while a real load the sender is entitled to does not. So an email
        # naming one load with nine invoice reference numbers attached answers the one, and an
        # email naming one load with forty of the sender's REAL loads attached still deflects
        # to the portal — which the written-id check alone could not separate, because both
        # look like "one written, N attached".
        #
        # Cheap by construction: the check above already deflected anything whose ids are
        # attachment-only, so reaching here means the sender wrote a small number of ids and
        # only their own loads could have inflated the set.
        if len(load_ids) > self._settings.bulk_threshold:
            return self._finalize(
                email,
                self._bulk_portal_draft(email),
                load_ids,
                correlation_id,
                ctx,
                _BULK_PORTAL_SKILL_ID,
            )

        # 2. Select the skill by intent -------------------------------------
        # Built from load_ids, not routes: dropped loads (non-TP, unauthorized) must not
        # reappear in the intake prompt.
        routes_map = {lid: routes[lid].value for lid in load_ids}
        skill, intake = self._select_skill(
            email,
            classification,
            identifiers,
            load_ids,
            routes_map,
            system,
            prenoa_loads,
            unresolved_loads,
            withheld_named,
        )

        # 3. Agent tool-use loop --------------------------------------------
        budget = self._iteration_budget(len(load_ids))
        agent_result = self._loop.run(
            system=skill.system_prompt,
            intake_prompt=intake,
            allowed_tools=skill.allowed_tools,
            ctx=ctx,
            max_iterations=budget,
            label=skill.id,
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
                after_agent=True,
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
                draft=edited,
                email=email,
                ctx=ctx,
                expected_load_ids=expected,
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
        system: System,
        prenoa_loads: list[str],
        unlocated_loads: list[str],
        withheld_loads: list[str] | None = None,
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
            wants_rate = bool(identifiers.stated_rates)
            _log.info(
                "combined_intent_narrowed",
                extra={
                    "chosen_skill": "rate_verification" if wants_rate else "payment_status",
                    "stated_rates": len(identifiers.stated_rates),
                },
            )
        elif has_rate:
            wants_rate = True
        elif has_payment:
            wants_rate = False
        else:
            # Uncertain intent but the email names loads (possibly only inside an attached
            # statement — "please see attached" carries no keyword). Same reasoning as the
            # classifier's own fallback: this inbox exists to answer payment status, and a
            # human reviews the draft regardless.
            _log.info(
                "intent_defaulted_payment_status",
                extra={"load_count": len(load_ids)},
            )
            wants_rate = False

        if system is System.QUICKBOOKS:
            # CargoTel answers payment status only: a load page carries one payable amount
            # and no line-item breakdown, so there is nothing for a rate skill to itemise.
            # A rate question therefore gets the status answer plus the amount, which is
            # every figure that exists, rather than a skill that could only restate it.
            #
            # That is only true if the amount actually reaches the reply. It did not: the
            # prompt named dates and documents per billing state and never asked for the
            # figure, so a Tru Funding rate enquiry over five loads was answered entirely
            # with missing-paperwork wording while $2,150 and $3,000 sat in the tool results.
            # `rate_question` carries the ask through so the reply leads with the amount.
            if wants_rate:
                _log.info(
                    "cargotel_rate_narrowed_to_status",
                    extra={
                        "load_count": len(load_ids),
                        "stated_rates": len(identifiers.stated_rates),
                    },
                )
            return CARGOTEL_PAYMENT_STATUS_SKILL, build_cargotel_payment_status_intake(
                email,
                load_ids,
                routes_map,
                signature=self._settings.reply_signature,
                documents_email=self._settings.documents_email,
                unlocated_loads=unlocated_loads,
                withheld_loads=withheld_loads,
                rate_question=wants_rate,
                stated_rates=identifiers.stated_rates,
            )

        if wants_rate:
            return RATE_VERIFICATION_SKILL, build_rate_verification_intake(
                email,
                load_ids,
                routes_map,
                identifiers.stated_rates,
                identifiers.factoring_company,
                signature=self._settings.reply_signature,
                documents_email=self._settings.documents_email,
                prenoa_loads=prenoa_loads,
                unlocated_loads=unlocated_loads,
                withheld_loads=withheld_loads,
            )
        return PAYMENT_STATUS_SKILL, build_payment_status_intake(
            email,
            load_ids,
            routes_map,
            signature=self._settings.reply_signature,
            documents_email=self._settings.documents_email,
            prenoa_loads=prenoa_loads,
            unlocated_loads=unlocated_loads,
            withheld_loads=withheld_loads,
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

    def _filter_ids(
        self, load_ids: list[str], email: InboundEmail, correlation_id: str
    ) -> list[str]:
        """Let the model drop candidates the regex should not have called loads.

        Deliberately here rather than inside ``extract_identifiers``: that tool stays
        deterministic, so its output remains reproducible and its tests keep meaning what
        they mean, and the model's opinion is a separate, separately logged step that can be
        turned off without touching it.

        Best-effort throughout. Any failure — an unreachable model, an unreadable answer —
        returns the ids unchanged, which is exactly the behaviour with the filter off.
        """

        try:
            mode = IdFilterMode(self._settings.llm_id_filter)
        except ValueError:
            _log.warning(
                "id_filter_mode_unknown", extra={"value": self._settings.llm_id_filter}
            )
            return load_ids
        if mode is IdFilterMode.OFF or len(load_ids) < MIN_CANDIDATES:
            return load_ids

        try:
            # Attachment text belongs here, last and truncatable but present. Without it the
            # filter was handed candidates that appear NOWHERE in what it was shown: an RTS
            # paperwork verification naming load 2536617 carried a packet whose 1504077 the
            # regex proposed, and the model was asked to classify a number it could not see.
            # `id_filter` only ever removes a candidate it has something coherent to say
            # about, so an attachment-only id survived by construction and was answered.
            #
            # Last on purpose. MAX_CONTEXT_CHARS still truncates, and the sender's own words
            # are what disambiguate the ids they wrote; a long statement losing its tail is
            # the pre-existing cap, not a new failure.
            text = "\n".join(
                p
                for p in (
                    email.subject,
                    email.body,
                    email.html_text,
                    email.thread_text,
                    *(a.extracted_text for a in email.attachments if a.extracted_text),
                )
                if p
            )
            verdicts = classify(self._llm, load_ids, text, correlation_id=correlation_id)
            return apply_filter(mode, load_ids, verdicts, correlation_id=correlation_id)
        except Exception as exc:  # never let the filter break intake
            _log.warning(
                "id_filter_failed", extra={"correlation_id": correlation_id, "error": str(exc)}
            )
            return load_ids

    def _narrow_to_written(
        self, load_ids: list[str], written_load_ids: list[str], correlation_id: str
    ) -> list[str]:
        """Answer the loads the sender NAMED, not every load-shaped number we could find.

        Both bulk checks knew the difference between an id the sender wrote and one an
        attachment contributed; the reply did not. Everything from skill selection down ran
        on the raw set, on the reasoning that authorization had already dropped the phantoms.
        It had — but authorization is the wrong question. Observed live: an RTS paperwork
        verification named ONE load, 2536617, and its attached packet contributed 1504077.
        That id is a REAL load, of a different carrier, and RTS is that carrier's factor, so
        authorization was right to allow it. The reply then volunteered a paragraph about a
        load nobody had asked about. ``id_filter``'s docstring names this exact risk: an id
        the sender happens to be authorized for, disclosed without anyone asking for it.

        Runs LAST, after both portal decisions, and that ordering is the whole design. The
        first check needs the raw count to deflect a statement whose ids are attachment-only;
        the second needs the post-authorization count to tell "one written, nine reference
        numbers" (answer the one) from "one written, forty of the sender's real loads"
        (deflect). Narrowing before either would collapse both into "answer the one".

        Falls back to the full set when the sender named NONE — the statement case, where the
        ids are attachment-only and dropping them would leave nothing to answer.

        THE TRADE: an email naming one load that also attaches a FEW of the sender's real
        loads — few enough to stay under the bulk threshold — now answers only the named one
        instead of all of them. That is the intended reading of "which loads is this about",
        and the drop is logged rather than silent so a deployment meeting the mixed shape
        often can see it happening.
        """

        written = [lid for lid in load_ids if lid in set(written_load_ids)]
        if not written or len(written) == len(load_ids):
            return load_ids
        _log.info(
            "unwritten_load_ids_dropped",
            extra={
                "correlation_id": correlation_id,
                "answered": written,
                "dropped": [lid for lid in load_ids if lid not in set(written)],
            },
        )
        return written

    def _authorize_loads(
        self,
        email: InboundEmail,
        load_ids: list[str],
        routes: dict[str, System],
        ctx: ToolContext,
    ) -> tuple[list[tuple[str, str]], list[str], list[str], list[str], list[str]]:
        """Run the authorization pre-check over every load.

        Returns ``(unauthorized, authorized, prenoa, unresolved, cancelled)``. ``cancelled``
        is a load Transport Pro no longer holds a payable record for; it is neither allowed
        nor denied, because the authorization context comes from the payload that is gone.
        ``unresolved`` is kept
        apart from ``unauthorized`` because the two must not be treated alike downstream: a
        denied load is legitimately withheld and the reply should say nothing about it, while
        an unresolvable load is one the sender explicitly asked about and we simply do not
        know — answering the rest and saying nothing about it is misleading. It feeds the
        gate's coverage baseline.

        A method rather than an inline loop so it can be run a second time after the roster is
        widened by ``auto_add_factoring_domains``, on the one code path where that happens.
        """

        unauthorized: list[tuple[str, str]] = []
        authorized_loads: list[str] = []
        prenoa_loads: list[str] = []
        unresolved_loads: list[str] = []
        cancelled_loads: list[str] = []
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
                unauthorized.append((load_id, f"ERROR({auth_out.payload.get('error')})"))
                unresolved_loads.append(load_id)
                continue
            auth = CheckAuthorizationOutput.model_validate(auth_out.payload)
            if auth.decision is AuthDecision.CANCELLED:
                # Not a denial and not a fault. Kept out of `unauthorized` so the escalation
                # stops saying "sender not authorized" about a load that no longer exists.
                cancelled_loads.append(load_id)
                continue
            if not auth.authorized:
                # Carry the tool's reason — it names the fix (e.g. a factoring domain to
                # add to PAYBOT_FACTORING_DOMAINS), which is what the reviewer acts on.
                detail = f" ({auth.reason})" if auth.reason else ""
                unauthorized.append((load_id, f"{auth.decision.value}{detail}"))
                continue
            authorized_loads.append(load_id)
            if auth.pre_noa:
                prenoa_loads.append(load_id)
        return unauthorized, authorized_loads, prenoa_loads, unresolved_loads, cancelled_loads

    def _auto_add_factoring_domains(
        self,
        email: InboundEmail,
        load_ids: tuple[str, ...],
        ctx: ToolContext,
        correlation_id: str,
    ) -> bool:
        """Roster the sender's domain for the factor already on the load. Policy-gated.

        Returns True when something was added, in which case the caller re-runs the
        authorization pre-check against the widened roster.

        THE COMPANY HALF NEVER COMES FROM THE MAIL. An entry is only written for a load whose
        own factor record names the factor, so what is being taken on trust is exactly one
        thing: that this domain belongs to that company. That is still the thing
        ``roster_candidate`` says a human should decide, and enabling the switch is the
        decision to stop asking.

        Four cases are refused even with the switch on, because each would make the roster
        meaningless rather than merely permissive:

        * a free-mail sender — the domain form would authorise every mailbox at that provider,
          and ``_roster_entry_matches`` refuses it at lookup, so the entry would be inert;
        * a domain already rostered to a DIFFERENT company — the Faro/BasicBlock shape, where
          one factor's real domain arrives on another factor's load. Auto-adding it would let
          one company answer for another's loads;
        * a load with no factor on file — there is then no company to attach the domain to,
          and ``build_candidate`` returns None for exactly this reason;
        * anything the sensitive-change scan flagged, which escalates before reaching here.

        Every write lands in ``factoring_domains_manual.json`` with an ``AUTO-ADDED`` evidence
        note and a WARNING in the log, so an entry nobody verified is findable and revocable
        rather than indistinguishable from one somebody did.
        """

        added: dict[str, str] = {}
        for load_id in load_ids:
            try:
                system = route_load(load_id).system
                if system is System.QUICKBOOKS:
                    if ctx.cargotel is None:
                        continue
                    auth = ctx.cargotel.get_authorization_context(load_id)
                elif system is System.TRANSPORT_PRO:
                    auth = ctx.tp.get_authorization_context(load_id)
                else:
                    continue
            except PaymentBotError:
                continue
            candidate = build_candidate(
                sender_email=email.from_email,
                factor_on_file=auth.factoring_company or "",
                load_ids=(load_id,),
                settings=self._settings,
            )
            if candidate is None:
                continue  # no factor on file: nothing to attach the domain to
            if candidate.free_mail:
                _log.warning(
                    "auto_roster_refused_free_mail",
                    extra={
                        "correlation_id": correlation_id,
                        "sender_domain": candidate.sender_domain,
                        "factor_on_file": candidate.factor_on_file,
                    },
                )
                continue
            elsewhere = sorted(
                name
                for name, domains in self._settings.factoring_domains.items()
                if not _factor_names_match(name, candidate.factor_on_file)
                and any(
                    str(d).strip().lower().lstrip("@") == candidate.sender_domain
                    for d in domains
                )
            )
            if elsewhere:
                _log.warning(
                    "auto_roster_refused_rostered_elsewhere",
                    extra={
                        "correlation_id": correlation_id,
                        "sender_domain": candidate.sender_domain,
                        "factor_on_file": candidate.factor_on_file,
                        "already_rostered_to": elsewhere,
                    },
                )
                continue
            added[candidate.roster_key] = candidate.sender_domain

        if not added:
            return False

        note = (
            f"AUTO-ADDED {self._today.isoformat()} by PAYBOT_AUTO_ADD_FACTORING_DOMAINS. "
            f"Sender {email.from_email} wrote about load(s) {', '.join(load_ids)}, whose factor "
            f"record already named this company; the DOMAIN is attested only by that mail. "
            f"Nobody verified it — subject {email.subject!r}. Revoke if it does not belong."
        )
        try:
            widened = append_manual_entries(added, note=note, settings=self._settings)
        except PaymentBotError as exc:
            _log.warning(
                "auto_roster_write_failed",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return False

        # Swap the widened roster in for the rest of this run, so the retry, the gate's own
        # re-run of check_authorization, and the agent all judge against the same roster.
        self._settings = widened
        ctx.settings = widened
        _log.warning(
            "auto_roster_entry_added",
            extra={
                "correlation_id": correlation_id,
                "added": added,
                "sender": email.from_email,
                "load_ids": list(load_ids),
            },
        )
        return True

    def _roster_candidate(
        self,
        email: InboundEmail,
        load_ids: tuple[str, ...],
        ctx: ToolContext,
        correlation_id: str,
    ) -> str | None:
        """The reviewer's packet for an unknown factoring sender, or None if not applicable.

        Best-effort by construction. Every failure mode here — an unreachable back office, a
        load with no factor, a malformed hints file — returns None and leaves the escalation
        exactly as it was. An escalation that failed to escalate because its *annotation*
        raised would be a far worse bug than the manual lookup this saves.

        Best-effort **per load**, too, which the outer handler alone did not give: a single id
        the back office cannot resolve now skips instead of discarding the packet for every
        other load in the email. "We could not annotate load X" and "we could not annotate
        this escalation" are different outcomes, and only the first is acceptable when the
        unresolvable id is one the sender never asked about.
        """

        try:
            factors: dict[str, list[str]] = {}
            carriers: set[str] = set()
            on_file: set[str] = set()
            for load_id in load_ids:
                system = route_load(load_id).system
                # Per load, because ONE unreadable id must not cost the packet for the others.
                # Live on an Aladdin verification: the body named 2534786 and the attachment
                # contributed three phantom 7-digit ids, two of which Transport Pro 400s on
                # because no such load exists. The first 400 propagated to the handler below,
                # which returned None for everything — so the reviewer got the escalation
                # reason and NOT the block saying we already hold aladdincap.com for this
                # factor while aladdinfactoringapp.com is rostered nowhere. That block was the
                # entire decision the escalation existed to put in front of them, and a load
                # that is not even ours discarded it.
                try:
                    if system is System.QUICKBOOKS:
                        if ctx.cargotel is None:
                            continue
                        auth = ctx.cargotel.get_authorization_context(load_id)
                    elif system is System.TRANSPORT_PRO:
                        auth = ctx.tp.get_authorization_context(load_id)
                    else:
                        continue
                except PaymentBotError as exc:
                    _log.info(
                        "roster_candidate_load_skipped",
                        extra={
                            "correlation_id": correlation_id,
                            "load_id": load_id,
                            "error": str(exc),
                        },
                    )
                    continue
                on_file.update(auth.authorized_emails)
                if not auth.factoring_company:
                    continue
                factors.setdefault(auth.factoring_company, []).append(load_id)
                if auth.carrier_company:
                    carriers.add(auth.carrier_company)

            blocks: list[str] = []
            for factor, loads in factors.items():
                candidate = build_candidate(
                    sender_email=email.from_email,
                    factor_on_file=factor,
                    load_ids=tuple(loads),
                    settings=self._settings,
                    carrier_companies=tuple(sorted(carriers)),
                    carrier_on_file_emails=tuple(sorted(on_file)),
                )
                if candidate is None:
                    continue
                log_candidate(candidate, correlation_id)
                blocks.append(candidate.render())
            return "\n\n".join(blocks) or None
        except Exception as exc:  # never let the annotation break the escalation
            _log.warning(
                "roster_candidate_failed",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return None

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
        after_agent: bool = False,
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
            after_agent=after_agent,
        )
