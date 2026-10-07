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
from datetime import date, datetime
from email.utils import parseaddr
from enum import StrEnum
from zoneinfo import ZoneInfo

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
from payment_bot.followup_reader import (
    FollowUpKind,
    FollowUpRead,
    FollowUpRoute,
    read_follow_up_with_model,
)
from payment_bot.followups import (
    ACTION_HANDOFF,
    ACTION_NOTICE,
    ACTION_STATUS_UPDATE,
    FollowUpRecord,
    FollowUpStore,
    handed_off,
    new_facts,
)
from payment_bot.gate import (
    GateResult,
    PreSendGate,
    asks_to_confirm_payment_direction,
)
from payment_bot.grounding import GroundingLedger
from payment_bot.id_filter import MIN_CANDIDATES, IdFilterMode, apply_filter, classify
from payment_bot.logging import AuditSink, get_logger
from payment_bot.models import (
    AuthDecision,
    InboundEmail,
    Intent,
    PriorReply,
    SensitiveAction,
    System,
)
from payment_bot.roster_candidate import append_manual_entries, build_candidate, log_candidate
from payment_bot.tools import ToolContext, ToolRegistry, build_default_registry
from payment_bot.tools.shared import (
    CheckAuthorizationOutput,
    ClassifyIntentOutput,
    DetectSensitiveChangeOutput,
    ExtractIdentifiersOutput,
    _factor_names_match,
    strip_quoted,
)
from payment_bot.tools.submit import SubmitDraftOutput

_log = get_logger("pipeline")

#: Identifies the bulk reply in approval summaries and the audit trail. Not a real skill —
#: no model runs — but `_is_auto_sendable` keys off the skill id, and this must never match
#: PAYMENT_STATUS_SKILL.id, or a bulk reply could auto-send in Phase 2.
_BULK_PORTAL_SKILL_ID = "bulk_portal"

_CARGOTEL_REFERRAL_SKILL_ID = "cargotel_referral"

#: The CargoTel hand-off reply (``Settings.cargotel_referral_contacts``). Code-authored
#: like the bulk portal body, and for the same reason: there is nothing to reason about,
#: and it deliberately states nothing about the load — no status, no amount, no date —
#: so it has nothing to ground, nothing to disclose, and nothing authorization would
#: need to protect. The named colleagues are Cc'd on the draft, so "they are copied
#: here" is true the moment it sends.
_CARGOTEL_REFERRAL_BODY = """Thank you for reaching out. The load you asked about is handled directly by:

{contacts}

They are copied on this email and will have the most up-to-date information for you.

{signature}"""

#: A follow-up handed to the colleagues instead of answered from the records. Not a real skill
#: — no model runs — and never PAYMENT_STATUS_SKILL.id, so it can never auto-send.
_FOLLOWUP_HANDOFF_SKILL_ID = "followup_handoff"

#: The follow-up handoff. Code-authored, like the CargoTel referral and for the same reasons:
#: it states nothing about the load — no status, amount or date — so there is nothing to ground
#: and nothing to repeat, and the people it says are copied are copied by construction
#: (``extra_cc``). It answers what was ASKED: live on RTS Financial's load 2493116 the factor
#: asked us to fast-track a 90-day-old invoice, then raised recourse, and was sent the same
#: status twice — once with "we are not able to expedite", which nobody had decided.
_FOLLOWUP_HANDOFF_BODY = """Thank you for following up. {ask_line} They are copied on this email and will follow up with you directly.

{signature}"""

#: The handoff's one variable sentence, by what the follow-up asked. None promises an outcome.
_HANDOFF_ASK_LINES = {
    FollowUpKind.PRESSURE: "I have passed your request to our team to review.",
    FollowUpKind.NOT_RECEIVED: "I have passed this to our team to look into.",
    FollowUpKind.DISPUTE: "I have passed your message to our team to review.",
    FollowUpKind.PROCESS_QUESTION: "I have passed your question to our team.",
    FollowUpKind.NEW_INFO: "I have passed this to our team.",
}
_HANDOFF_DEFAULT_LINE = "I have passed your message to our team to review."

#: How each kind reads after "they", on a card and in an escalation reason.
_KIND_LABELS = {
    FollowUpKind.STATUS: "ask for a status update",
    FollowUpKind.PAYMENT_PROOF: "ask for payment details",
    FollowUpKind.NOT_RECEIVED: "say the payment was not received",
    FollowUpKind.PRESSURE: "press for payment",
    FollowUpKind.DISPUTE: "dispute or correct what we said",
    FollowUpKind.PROCESS_QUESTION: "ask how we work",
    FollowUpKind.NEW_INFO: "send new information",
    FollowUpKind.THANKS: "say thanks",
    FollowUpKind.EMPTY: "wrote nothing",
    FollowUpKind.UNKNOWN: "wrote something that could not be read automatically",
}

#: The reply to an email whose every load is one where nothing is owed to the sender's carrier
#: (see ``NotPaidLoad``). Code-authored, never auto-sent, and never PAYMENT_STATUS_SKILL.id.
_NOT_PAID_SKILL_ID = "not_paid"

_NOT_PAID_BODY = """Thank you for reaching out.

{facts}

{signature}"""

#: Deterministic drafts name no load ids in their body on purpose, so the gate's
#: coverage check (every requested load addressed) must not be applied to them.
_NO_COVERAGE_SKILL_IDS = (
    _BULK_PORTAL_SKILL_ID,
    _CARGOTEL_REFERRAL_SKILL_ID,
    _FOLLOWUP_HANDOFF_SKILL_ID,
    _NOT_PAID_SKILL_ID,
)

#: Absolute cap on a derived iteration budget, however many loads an email names.
#:
#: The per-load budget scales (see `_iteration_budget`), but it must still terminate: this is
#: the backstop that keeps a runaway model bounded, which is the whole point of having a cap.
#: Set to the same 50 that bounds `agent_max_iterations` in configuration, so a derived
#: budget can never exceed what an operator could have set by hand.
ITERATION_CEILING = 50


def _today_in(tz_name: str) -> date:
    """Today's date in ``tz_name``, falling back to the system date.

    `date.today()` is UTC in Lambda, and every date this bot reasons about is Eastern. From
    20:00 Eastern that made "today" tomorrow: `_check_tense_consistency` would read a payment
    dated today as already past, and `compute_scheduled_pay_date` would walk the Mon/Thu rule
    from the wrong day. Roughly a sixth of runs at the current cadence.

    Falls back rather than raising. A misspelt zone is a configuration slip, and taking the
    inbox down over one would be a worse failure than a date that is off by hours — which is
    exactly what the fallback restores.
    """

    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:  # unknown zone, or no tzdata on the platform
        _log.warning("timezone_unusable", extra={"timezone": tz_name})
        return date.today()


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
    #: The colleague whose reply this email was chasing; blank for first contact. Stamped by
    #: ``process_email`` on every outcome, so a run can say how many of its drafts and
    #: escalations were follow-ups — nothing else in the result tells them apart.
    follow_up_to: str = ""
    #: What the bot did with a follow-up: ``status_update`` (answered from the records),
    #: ``handoff`` (passed to the colleagues, who are copied), ``notice`` (no email, a card),
    #: or the plain outcome. Blank for first contact.
    follow_up_action: str = ""


@dataclass(frozen=True, slots=True)
class NotPaidLoad:
    """A load the sender may be answered about only to say none of its money is theirs.

    Every carrier they may hear about is on the load without a payable — almost always a
    dispatch that was cancelled before another carrier hauled it. The one true answer is that,
    and it is code-authored: written by the model from a lookup, it reported the other
    carrier's payment as theirs (load 2523099, KRGA Transport, told Circle Transportation's
    $1,682.20 direct deposit and asked for an NOA on a load KRGA never ran).
    """

    load_id: str
    carriers: tuple[str, ...]
    #: True when every one of :attr:`carriers` had its dispatch on this load cancelled.
    cancelled: bool

    @property
    def sentence(self) -> str:
        who = " and ".join(self.carriers)
        if self.cancelled:
            return (
                f"Load {self.load_id}: the dispatch to {who} on this load was cancelled, so no "
                f"payment is owed to {who} on it."
            )
        return f"Load {self.load_id}: there is no payment on record for {who} on this load."


def _repeats(body: str, prior: str) -> bool:
    """True when ``prior`` already carries ``body``'s opening line.

    The opening line is what makes each code-authored reply recognisably itself (the referral's
    "The load you asked about is handled directly by", the portal's link sentence), and it
    survives a reviewer's sign-off edits. Compared with whitespace and case folded, because the
    copy in the thread has been through a mail client.
    """

    def fold(text: str) -> str:
        return " ".join(text.split()).lower()

    first = next((line for line in body.splitlines() if line.strip()), "")
    return bool(first.strip()) and fold(first) in fold(prior)


def _who(prior: PriorReply) -> str:
    """The colleague whose reply a follow-up answers, as a reviewer knows them."""

    return prior.from_name or prior.from_email


def _when(prior: PriorReply) -> str:
    sent = prior.sent_at
    return f" on {sent:%a %b} {sent.day}" if sent else ""


def _excerpt(body: str, limit: int = 160) -> str:
    """The start of what a colleague wrote, quotes stripped, on one line."""

    text = " ".join(strip_quoted(body).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _default_follow_up_action(outcome: Outcome) -> str:
    """The action a follow-up took when no routing decision named one."""

    if outcome in (Outcome.AWAITING_REVIEW, Outcome.SENT, Outcome.REJECTED):
        return ACTION_STATUS_UPDATE  # a draft came out of the agent: the records answered it
    return outcome.value


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
        followup_store: FollowUpStore | None = None,
        followup_reader_llm: LlmClient | None = None,
    ) -> None:
        self._tp = tp
        self._llm = llm
        # The follow-up reader's model. The drafting model unless given one — separable so a
        # test can script the reader's verdict without scripting the agent's turns around it.
        self._reader_llm = followup_reader_llm or llm
        # Resolved once per pipeline rather than per email so a long-running processor cannot
        # render a date under one day and judge its tense under the next. Injectable because
        # fixture data has fixed dates: a test asserting "Thursday, August 6, 2026" is only
        # meaningful against a pinned today.
        self._today = today or _today_in((settings or get_settings()).timezone)
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
        # Per-thread follow-up history (state/followups/). None: no memory across runs — no
        # handoff-already-happened, and every re-run of a message reads it with the model again.
        self._followups = followup_store

    # -- public API ----------------------------------------------------------
    def process_email(self, email: InboundEmail) -> PipelineResult:
        correlation_id = email.message_id
        # What a follow-up wants, read once, next to the reply it answers — and handed to the
        # agent on the email itself, so the intake can tell it what was asked.
        reading = None
        if email.prior_reply is not None:
            reading = self._read_follow_up(email, correlation_id)
            email = email.model_copy(
                update={
                    "prior_reply": email.prior_reply.model_copy(
                        update={"ask_kind": reading.kind.value, "ask_summary": reading.summary}
                    )
                }
            )
        try:
            result = self._process(email, correlation_id, reading)
        except PaymentBotError as exc:  # expected-but-unhandled → fail closed
            result = self._escalate(email, "review", f"unhandled error: {exc}", (), correlation_id)
        except Exception as exc:  # last-resort safety net; never send on a bug
            _log.exception("pipeline_crash", extra={"correlation_id": correlation_id})
            result = self._escalate(
                email, "security", f"pipeline crash: {exc}", (), correlation_id
            )
        if email.prior_reply is not None and reading is not None:
            # Here, once, rather than at each of the many returns in `_process`: every
            # outcome of a follow-up gets the stamp, the one searchable log line and its entry
            # in the thread's record.
            result.follow_up_to = email.prior_reply.from_email
            if not result.follow_up_action:
                result.follow_up_action = _default_follow_up_action(result.outcome)
            _log.info(
                "follow_up_outcome",
                extra={
                    "correlation_id": correlation_id,
                    "ask": reading.kind.value,
                    "ask_source": reading.source,
                    "summary": reading.summary,
                    "action": result.follow_up_action,
                    "outcome": result.outcome.value,
                    "follow_up_to": result.follow_up_to,
                    "detail": result.detail[:300],
                },
            )
            self._record_follow_up(email, reading, result)
        return result

    # -- internal flow -------------------------------------------------------
    def _process(
        self,
        email: InboundEmail,
        correlation_id: str,
        reading: FollowUpRead | None = None,
    ) -> PipelineResult:
        ledger = GroundingLedger()
        ctx = ToolContext(
            tp=self._tp,
            cargotel=self._cargotel,
            ledger=ledger,
            correlation_id=correlation_id,
            settings=self._settings,
            today=self._today,
            # Which carriers the email names — the pre-NOA authorization rule reads it.
            email_text="\n".join(p for p in (email.subject, email.body, email.html_text) if p),
        )

        # 0. A follow-up to our own reply (Settings.followup_replies) ----------
        # Only a follow-up that wants something is answered. A "thanks" after a colleague's
        # answer closes the conversation, and its subject — inherited from the thread — would
        # otherwise classify it as a payment question and spend a full agent run on a draft
        # nobody wants. Left exactly as it was before follow-ups existed: with the colleague.
        if (
            email.prior_reply is not None
            and reading is not None
            and reading.route is FollowUpRoute.NONE
        ):
            _log.info(
                "follow_up_asks_nothing",
                extra={
                    "correlation_id": correlation_id,
                    "after_reply_from": email.prior_reply.from_email,
                },
            )
            return PipelineResult(
                Outcome.NO_ACTION,
                f"follow-up to {email.prior_reply.from_email}'s reply asks for nothing new; "
                "left with them",
                correlation_id,
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
                **self._identifier_text(email),
                # Spreadsheet statements carry their load ids here and nowhere in the body.
                "attachments_text": "\n".join(
                    a.extracted_text for a in email.attachments if a.extracted_text
                ),
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

        # Follow-ups (Settings.followup_replies): a STATUS ask may be answered from the
        # records, once per thread. Anything else goes to a person. After the sensitive-change
        # check on purpose — a bank or NOA instruction escalates whatever else it asks.
        if email.prior_reply is not None and reading is not None:
            routed = self._route_follow_up(email, reading, load_ids, correlation_id, ctx)
            if routed is not None:
                return routed

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

        # CargoTel referral (Settings.cargotel_referral_contacts): a 6-digit-only email
        # gets the deterministic hand-off reply naming the colleagues who work these
        # loads, with them Cc'd — no scrape, no cross-checks, no model call. Checked
        # BEFORE cargotel availability on purpose: the referral needs neither the cookie
        # nor the policy switch, and it must also win over the escalate-when-unavailable
        # branch below. Sensitive-change mail never reaches here (escalated above), and a
        # mixed email falls through to the existing spans-both-systems handling.
        if cgt_loads and not tp_loads and self._settings.cargotel_referral_contacts:
            _log.info(
                "cargotel_referral",
                extra={"correlation_id": correlation_id, "loads": cgt_loads},
            )
            return self._finalize(
                email,
                self._cargotel_referral_draft(email),
                cgt_loads,
                correlation_id,
                ctx,
                _CARGOTEL_REFERRAL_SKILL_ID,
            )

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
        (
            unauthorized,
            authorized_loads,
            prenoa_loads,
            unresolved_loads,
            cancelled_loads,
            not_paid,
        ) = self._authorize_loads(email, load_ids, routes, ctx)
        # POLICY: add the sender's domain for the factor already on the load, then retry once.
        # Off by default; see Settings.auto_add_factoring_domains for what this trades away and
        # the four cases it still refuses.
        if (
            not authorized_loads
            and not not_paid
            and self._settings.auto_add_factoring_domains
            and self._auto_add_factoring_domains(email, tuple(load_ids), ctx, correlation_id)
        ):
            (
                unauthorized,
                authorized_loads,
                prenoa_loads,
                unresolved_loads,
                cancelled_loads,
                not_paid,
            ) = self._authorize_loads(email, load_ids, routes, ctx)

        if not authorized_loads and not_paid and not unauthorized and not cancelled_loads:
            # Every load the sender asked about is one where nothing is owed to them. No
            # lookup can add to that, so no agent runs: the reply is the code-authored facts.
            _log.info(
                "not_paid_loads_answered",
                extra={
                    "correlation_id": correlation_id,
                    "loads": [n.load_id for n in not_paid],
                },
            )
            return self._finalize(
                email,
                self._not_paid_draft(email, not_paid),
                [n.load_id for n in not_paid],
                correlation_id,
                ctx,
                _NOT_PAID_SKILL_ID,
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
                # A follow-up's unwritten ids usually come from the earlier thread (quoted
                # history, or our own reply), not from a PDF — say which.
                source = (
                    "an attachment or the earlier thread"
                    if email.prior_reply is not None
                    else "an attachment"
                )
                parts.append(
                    f"id(s) the sender never wrote, contributed by {source}: "
                    f"{', '.join(unwritten)}"
                )
            if not_paid:
                parts.append(
                    "no payment owed to the sender's carrier on: "
                    + "; ".join(n.sentence for n in not_paid)
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
        if unauthorized or cancelled_loads or not_paid:
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
            not_paid_sentences=[n.sentence for n in not_paid],
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

        follow_up_note = ""
        if email.prior_reply is not None:
            # A follow-up answered from the records is only worth sending if the records say
            # something the carrier was not already told. See followups.new_facts.
            prior = email.prior_reply
            fresh = new_facts(draft.reply_body, strip_quoted(prior.body))
            if not fresh:
                result = self._escalate(
                    email,
                    "review",
                    f"carrier chased again ({prior.ask_summary or 'follow-up'}); the records "
                    f"are unchanged since {_who(prior)}'s reply{_when(prior)} — the draft "
                    "would only repeat it, so no email was drafted. Needs a person.",
                    tuple(load_ids),
                    correlation_id,
                    after_agent=True,
                )
                result.follow_up_action = ACTION_NOTICE
                return result
            follow_up_note = (
                f"They {_KIND_LABELS.get(FollowUpKind(prior.ask_kind or 'unknown'), 'wrote again')}"
                f": {prior.ask_summary or '(no summary)'}\n"
                f"Last reply ({_who(prior)}{_when(prior)}): {_excerpt(prior.body)}\n"
                f"New in this draft: {', '.join(fresh)}"
            )

        return self._finalize(
            email,
            draft,
            load_ids,
            correlation_id,
            ctx,
            skill.id,
            noa_request_expected=bool(prenoa_loads),
            # Not-paid loads are checked like withheld ones: the reply must name each.
            withheld_loads=tuple(withheld_named) + tuple(n.load_id for n in not_paid),
            candidate_load_ids=tuple(identifiers.load_ids),
            follow_up_note=follow_up_note,
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
        withheld_loads: tuple[str, ...] = (),
        candidate_load_ids: tuple[str, ...] | None = None,
        handoff: str = "",
        follow_up_note: str = "",
    ) -> PipelineResult:
        """Run the gate, then approval, then send or leave the draft for review.

        Extracted so the deterministic bulk-portal reply takes the *same* route as an
        agent-produced draft. There is no second path to a sent email, and no draft that
        reaches a mailbox without passing §5.

        ``candidate_load_ids`` is every id intake extracted from the email, pre-narrowing
        — the universe of loads a draft may legitimately talk about. The gate's
        invented-load check compares the draft's own "load N" mentions against it.
        """

        # A code-authored reply is the same words every time, so on a follow-up it may be
        # exactly what the carrier already has. Live, the first run after the handoff fix: a
        # carrier chasing on a 6-digit load was drafted the CargoTel referral it had already
        # been sent ("handled directly by Ashley Wolf and Elizabeth Haussmann ... copied").
        # Those people have the thread; the carrier does not need telling twice.
        if (
            email.prior_reply is not None
            and skill_id in _NO_COVERAGE_SKILL_IDS
            and _repeats(draft.reply_body, email.prior_reply.body)
        ):
            prior = email.prior_reply
            result = self._escalate(
                email,
                "review",
                f"follow-up would repeat the {skill_id} reply the carrier already has; no "
                f"email drafted — with {', '.join(prior.colleagues or (prior.from_email,))}"
                + (f" and {', '.join(draft.extra_cc)}" if draft.extra_cc else ""),
                tuple(load_ids),
                correlation_id,
            )
            result.follow_up_action = ACTION_NOTICE
            return result

        # 4. Pre-send gate (deterministic, §5) ------------------------------
        # The bulk portal reply is code-authored and deliberately names no load, so it
        # carries no expected-coverage list; an agent draft must address every load the
        # intake handed it.
        expected = None if skill_id in _NO_COVERAGE_SKILL_IDS else tuple(load_ids)
        gate_result = self._gate.evaluate(
            draft=draft,
            email=email,
            ctx=ctx,
            expected_load_ids=expected,
            withheld_loads=withheld_loads,
            noa_request_expected=noa_request_expected,
            candidate_load_ids=candidate_load_ids,
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
        # A follow-up is never auto-sent: it answers in a conversation a colleague is part
        # of, and only a human can judge the draft against what that colleague said.
        if email.prior_reply is None and self._is_auto_sendable(skill_id, load_ids):
            return self._send(email, draft, draft.reply_body, correlation_id, gate_result)

        summary = ApprovalSummary(
            from_=email.from_email,
            intents=(skill_id,),
            load_ids=tuple(load_ids),
            key_facts=(f"loads={load_ids}", f"gate=passed({len(gate_result.checks)} checks)"),
            # Per-draft recipients (the CargoTel referral's contacts) shown beside the
            # configured Cc, so the reviewer sees exactly who the send will copy.
            cc=self._settings.reply_cc + tuple(draft.extra_cc),
            follow_up_to=email.prior_reply.from_email if email.prior_reply else "",
            handoff=handoff,
            follow_up_note=follow_up_note,
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
                withheld_loads=withheld_loads,
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
        not_paid_sentences: list[str] | None = None,
    ) -> tuple[Skill, str]:
        """Pick the skill + build its intake from the classified intent.

        Always answers: by the time this runs the email has at least one valid,
        authorized Transport Pro load, and an unclear ask about a real load defaults to
        payment status — a human reviews the draft regardless.
        """

        has_payment = Intent.PAYMENT_STATUS in classification.intents
        has_rate = Intent.RATE_VERIFICATION in classification.intents

        # Whether the sender asked us to affirm where their payments go. Read from the gate's
        # own detector so the instruction the model gets and the check that enforces it can
        # never disagree. Naming a pay-to is required on most mail and forbidden here — see
        # _remit_confirmation_line.
        remit_confirmation_asked = asks_to_confirm_payment_direction(email)

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
                not_paid_sentences=not_paid_sentences,
                remit_confirmation_asked=remit_confirmation_asked,
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
                not_paid_sentences=not_paid_sentences,
                remit_confirmation_asked=remit_confirmation_asked,
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
            not_paid_sentences=not_paid_sentences,
            remit_confirmation_asked=remit_confirmation_asked,
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

    def _cargotel_referral_draft(self, email: InboundEmail) -> SubmitDraftOutput:
        """Build the 6-digit hand-off reply — see ``_CARGOTEL_REFERRAL_BODY``.

        Same honesty contract as the bulk portal draft above: empty ``load_ids`` and a
        body naming no load, no amount, no date — the gate has nothing to object to
        because nothing is disclosed. The configured contacts land in the body by name
        and in ``extra_cc`` by address, so the reply's own claim ("they are copied on
        this email") is enforced by construction rather than hoped for.
        """

        contacts = []
        addresses = []
        for entry in self._settings.cargotel_referral_contacts:
            name, address = parseaddr(entry)
            if not address:
                continue
            contacts.append(f"- {name} ({address})" if name else f"- {address}")
            addresses.append(address)
        body = _CARGOTEL_REFERRAL_BODY.format(
            contacts="\n".join(contacts),
            signature=self._settings.reply_signature,
        )
        return SubmitDraftOutput(
            reply_body=body,
            to=email.from_email,
            load_ids=[],  # nothing about any load is disclosed — same as the portal reply
            citations=[],
            extra_cc=addresses,
        )

    def _not_paid_draft(self, email: InboundEmail, not_paid: list[NotPaidLoad]) -> SubmitDraftOutput:
        """The reply when every load asked about is one with nothing owed to the sender.

        One fixed sentence per load — whose dispatch was cancelled, and that nothing is owed
        to them on it — and nothing else: no amount, date or payee, and above all nothing
        about the carrier that did haul it. ``load_ids`` is empty because no load's money is
        disclosed; the sentences name the sender's own carrier, which they asked about.
        """

        body = _NOT_PAID_BODY.format(
            facts="\n\n".join(n.sentence for n in not_paid),
            signature=self._settings.reply_signature,
        )
        return SubmitDraftOutput(reply_body=body, to=email.from_email, load_ids=[], citations=[])

    # -- follow-ups (Settings.followup_replies) --------------------------------
    def _read_follow_up(self, email: InboundEmail, correlation_id: str) -> FollowUpRead:
        """What a follow-up wants — see :mod:`payment_bot.followup_reader`.

        An earlier run's verdict for the same message is reused: a follow-up that escalates
        is re-processed every poll until its retry budget is spent, and a thank-you the Gmail
        filter let through is re-fetched for days, so reading it again each time would pay
        for the same answer over and over. When the model cannot answer, the follow-up goes to
        a person — UNKNOWN routes to a handoff, never to a guess.
        """

        prior = email.prior_reply
        assert prior is not None  # only called for follow-ups
        for entry in reversed(self._follow_up_history(email.thread_id)):
            if entry.message_id == email.message_id:
                try:
                    return FollowUpRead(FollowUpKind(entry.ask), entry.summary, source="memo")
                except ValueError:
                    break  # a record from before the reader existed; read it afresh
        read = read_follow_up_with_model(
            self._reader_llm,
            subject=email.subject,
            our_reply=strip_quoted(prior.body),
            their_message=strip_quoted(email.body),
            correlation_id=correlation_id,
        )
        if read is not None:
            return read
        return FollowUpRead(
            FollowUpKind.UNKNOWN, "the follow-up could not be read automatically", "fallback"
        )

    def _route_follow_up(
        self,
        email: InboundEmail,
        reading: FollowUpRead,
        load_ids: list[str],
        correlation_id: str,
        ctx: ToolContext,
    ) -> PipelineResult | None:
        """Hand a follow-up to a person, or ``None`` to answer it from the records.

        Answered from the records when it asks for a status or for payment details — and even
        then the draft goes out only if it says something new (see `_process`). Otherwise:

        * already handed off — no email; a notice card for the people who have it. The
          carrier was told who is on it, and telling them again is a repeat;
        * anything else, with ``followup_handoff_cc`` naming an owner — a handoff reply
          copying the colleagues and the owner, who can do what the records cannot:
          reissue, expedite, settle a dispute, take in new paperwork;
        * anything else, with no owner named — a card for the people, and no email at all.
        """

        prior = email.prior_reply
        assert prior is not None  # only called for follow-ups
        history = self._follow_up_history(email.thread_id)
        asked = f"{_KIND_LABELS[reading.kind]}: {reading.summary or '(no summary)'}"

        if handed_off(history):
            result = self._escalate(
                email,
                "review",
                f"carrier chased again after this thread was handed off — they {asked}. "
                f"No email drafted; with {', '.join(prior.colleagues) or prior.from_email}",
                tuple(load_ids),
                correlation_id,
            )
            result.follow_up_action = ACTION_NOTICE
            return result

        if reading.route is FollowUpRoute.ANSWER:
            return None

        reason = f"they {asked}"
        if not self._settings.followup_handoff_cc:
            # No named owner, so no email. The handoff reply tells the carrier the people
            # copied "will follow up with you directly", and with nobody assigned that is a
            # promise nobody keeps: on 300 real chases, half of those after a bot-drafted
            # reply were never answered by anyone. Until FollowupHandoffCc names someone who
            # can act, the follow-up goes to the people as a card and the carrier hears from
            # a person, or not at all — never a holding line followed by silence.
            chases = 1 + sum(1 for h in history if h.message_id != email.message_id)
            result = self._escalate(
                email,
                "review",
                f"follow-up needs a person — {reason}. Last reply ({_who(prior)}{_when(prior)}):"
                f" {_excerpt(prior.body)}. In the thread: "
                f"{', '.join(prior.colleagues) or prior.from_email}"
                + (f". Follow-up #{chases} in this thread" if chases > 1 else "")
                + ". No email sent.",
                tuple(load_ids),
                correlation_id,
            )
            result.follow_up_action = ACTION_HANDOFF
            return result

        cc = self._handoff_cc(email)
        if not cc:
            result = self._escalate(
                email,
                "review",
                f"follow-up needs a person ({reason}) but there is nobody to copy: no "
                "colleague answered the carrier in this thread and FollowupHandoffCc is empty",
                tuple(load_ids),
                correlation_id,
            )
            result.follow_up_action = ACTION_NOTICE
            return result

        _log.info(
            "follow_up_handoff",
            extra={"correlation_id": correlation_id, "reason": reason, "cc": list(cc)},
        )
        result = self._finalize(
            email,
            self._handoff_draft(email, reading.kind, cc),
            load_ids,
            correlation_id,
            ctx,
            _FOLLOWUP_HANDOFF_SKILL_ID,
            handoff=reason,
            follow_up_note=f"Last reply ({_who(prior)}{_when(prior)}): {_excerpt(prior.body)}",
        )
        result.follow_up_action = ACTION_HANDOFF
        return result

    def _handoff_cc(self, email: InboundEmail) -> tuple[str, ...]:
        """Who a handoff copies: the thread's colleagues, then ``followup_handoff_cc``.

        Never the group (the configured Cc already carries it) and never the sender. Ordered
        and de-duplicated without regard to case, so one person is copied once.
        """

        excluded = {self._settings.mailbox.lower(), email.from_email.lower()}
        excluded |= {parseaddr(a)[1].lower() for a in self._settings.reply_cc}
        colleagues = email.prior_reply.colleagues if email.prior_reply else ()
        out: list[str] = []
        for entry in (*colleagues, *self._settings.followup_handoff_cc):
            address = parseaddr(entry)[1].strip()
            if address and address.lower() not in excluded:
                excluded.add(address.lower())
                out.append(address)
        return tuple(out)

    def _handoff_draft(
        self, email: InboundEmail, kind: FollowUpKind, cc: tuple[str, ...]
    ) -> SubmitDraftOutput:
        """Build the handoff reply — see ``_FOLLOWUP_HANDOFF_BODY``.

        Same honesty contract as the CargoTel referral: empty ``load_ids``, and a body naming
        no load, amount or date, so the gate has nothing to object to because nothing is
        disclosed; ``cc`` lands in ``extra_cc``, so "they are copied" is true when it sends.
        """

        body = _FOLLOWUP_HANDOFF_BODY.format(
            ask_line=_HANDOFF_ASK_LINES.get(kind, _HANDOFF_DEFAULT_LINE),
            signature=self._settings.reply_signature,
        )
        return SubmitDraftOutput(
            reply_body=body,
            to=email.from_email,
            load_ids=[],
            citations=[],
            extra_cc=list(cc),
        )

    def _follow_up_history(self, thread_id: str) -> list[FollowUpRecord]:
        """The thread's record, or empty. Unreadable is empty: it costs a repeated model read
        or a missed "already handed off", never a wrong send — a person approves every draft."""

        if self._followups is None or not thread_id:
            return []
        try:
            return self._followups.history(thread_id)
        except Exception as exc:
            _log.warning(
                "follow_up_history_unreadable", extra={"thread_id": thread_id, "error": str(exc)}
            )
            return []

    def _record_follow_up(
        self, email: InboundEmail, reading: FollowUpRead, result: PipelineResult
    ) -> None:
        """Append this follow-up to its thread's record. Never fails the run."""

        if self._followups is None or not email.thread_id:
            return
        draft = result.draft
        entry = FollowUpRecord(
            message_id=email.message_id,
            ask=reading.kind.value,
            summary=reading.summary,
            action=result.follow_up_action,
            outcome=result.outcome.value,
            after_reply_from=result.follow_up_to,
            loads=tuple(draft.load_ids) if draft else (),
            cc=tuple(draft.extra_cc) if draft else (),
            reply=draft.reply_body if draft else "",
            detail=result.detail[:300],
        )
        try:
            self._followups.record(email.thread_id, entry)
        except Exception as exc:
            _log.warning(
                "follow_up_record_failed",
                extra={"correlation_id": email.message_id, "error": str(exc)},
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
            #
            # A follow-up's prior reply is a candidate source too (see `_identifier_text`),
            # so it is shown for the same reason the attachments are.
            prior = (
                f"Our earlier reply in this thread:\n{email.prior_reply.body}"
                if email.prior_reply is not None
                else ""
            )
            text = "\n".join(
                p
                for p in (
                    email.subject,
                    email.body,
                    email.html_text,
                    email.thread_text,
                    prior,
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

    @staticmethod
    def _identifier_text(email: InboundEmail) -> dict[str, str]:
        """Subject, body, quoted history and HTML, as ``extract_identifiers`` takes them.

        An ordinary email goes in whole, as it always has. A follow-up is split: what the
        sender wrote now is the body, and everything quoted beneath it — on a follow-up, the
        colleague's reply — goes in as ``thread_text``, which the tool reads for load ids
        only. Its loads are what the carrier is chasing; its amounts are ours, not theirs.

        The HTML part joins the quoted side. It repeats the same history, and the quote
        markers cannot be found reliably once tags are stripped, so reading it as "written"
        would let the colleague's figures back in through the other door. A reply written
        in a mail client always has a plain part; without one, ``body`` is the HTML's text.

        So does the colleague's reply itself, whole — including the carrier's first message
        quoted beneath it. A chase need not quote anything: live, a carrier wrote back to
        Angelica's reply naming no load in subject or body, and the follow-up escalated as
        "no valid 6/7-digit load id found" while the load sat in the reply we had already
        fetched. Read as ``thread_text`` it can name the load and nothing more.
        """

        if email.prior_reply is None:
            return {
                "subject": email.subject,
                "body": email.body,
                "thread_text": email.thread_text,
                # Portal collections mail puts its invoice table in the HTML only, so the
                # load id can exist nowhere else. See InboundEmail.html_text.
                "html_text": email.html_text,
            }
        written = strip_quoted(email.body)
        quoted = email.body[len(written) :]
        return {
            "subject": email.subject,
            "body": written,
            "thread_text": "\n".join(
                p
                for p in (quoted, email.thread_text, email.html_text, email.prior_reply.body)
                if p
            ),
            "html_text": "",
        }

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
    ) -> tuple[
        list[tuple[str, str]], list[str], list[str], list[str], list[str], list[NotPaidLoad]
    ]:
        """Run the authorization pre-check over every load.

        Returns ``(unauthorized, authorized, prenoa, unresolved, cancelled, not_paid)``.
        ``not_paid`` is a load the sender may be answered about, but only about carriers with
        no payable on it — see :class:`NotPaidLoad`; it is never handed to the agent.
        ``cancelled``
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
        not_paid: list[NotPaidLoad] = []
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
            if auth.matched_carriers and set(auth.unpaid_carriers) >= set(auth.matched_carriers):
                # Every carrier this sender may hear about has NO payable here, so every
                # amount, date and payee on the load belongs to someone else. Looked up, the
                # load could only be answered with that someone else's money — live on 2523099,
                # KRGA's factor was told Circle Transportation's $1,682.20. So it is not looked
                # up: the reply states the one true thing, and the agent never sees the load.
                ctx.disclosable_carriers[load_id] = auth.matched_carriers
                not_paid.append(
                    NotPaidLoad(
                        load_id=load_id,
                        carriers=auth.unpaid_carriers,
                        cancelled=set(auth.cancelled_carriers) >= set(auth.unpaid_carriers),
                    )
                )
                continue
            authorized_loads.append(load_id)
            if auth.pre_noa:
                prenoa_loads.append(load_id)
            # Narrow what the tools may read on this load to the carrier the sender actually
            # matched, when the match named one. This is the enforcement half of the
            # multi-carrier fix: authorization decided the sender may be answered ABOUT the
            # load, and this decides WHICH of its carriers' money that covers. Parasource
            # asking about 2436437 gets Parasource's $5,000, not the $905 FOX CARRIERS was
            # paid for another leg of the same load. Empty means no restriction — see
            # ToolContext.disclosable_carriers.
            if auth.matched_carriers:
                ctx.disclosable_carriers[load_id] = auth.matched_carriers
            else:
                # A second pass after the roster widened must not inherit a stale narrowing.
                ctx.disclosable_carriers.pop(load_id, None)
        return (
            unauthorized,
            authorized_loads,
            prenoa_loads,
            unresolved_loads,
            cancelled_loads,
            not_paid,
        )

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
            # One candidate per factor OF RECORD on the load. A re-dispatched load is
            # factored per leg — 2436437 carries eCapital, RTS and England Carrier Services —
            # and reading only the first meant the roster could never be widened for the
            # others, however plainly the sender's domain named them.
            candidates = [
                build_candidate(
                    sender_email=email.from_email,
                    factor_on_file=factor_on_file,
                    load_ids=(load_id,),
                    settings=self._settings,
                )
                for factor_on_file in (auth.factoring_companies or ("",))
            ]
            candidate = next((c for c in candidates if c is not None), None)
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
                carriers.update(auth.carrier_companies)
                # Every factor of record on the load, not just the first payable's: the block
                # exists to tell a reviewer which rostered domains we already hold against the
                # names on this load, and a load factored per leg has several.
                for factor_on_file in auth.factoring_companies:
                    factors.setdefault(factor_on_file, []).append(load_id)

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
