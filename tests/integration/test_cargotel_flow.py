"""Integration: the 6-digit CargoTel pipeline, driven by a scripted model.

Exercises intake → agent loop → pre-send gate → approval → send for a load CargoTel owns.
The routing tests at the end are what protect the *separation*: which system answers a load,
and what happens when an email names both.
"""

from __future__ import annotations

from datetime import date

import pytest
from tests.cargotel_pages import build_carrier_page, build_page

from payment_bot.clients import (
    ApprovalAction,
    ApprovalDecision,
    CargoTelLoadFixture,
    MockCargoTelClient,
    MockGmailClient,
    MockSlackClient,
    ScriptedApprovalResolver,
    ScriptedLlmClient,
)
from payment_bot.clients.cargotel_html import parse_carrier_html, parse_load_html
from payment_bot.clients.llm import LlmResponse, ToolUseBlock
from payment_bot.config import Settings
from payment_bot.grounding import GroundingLedger, find_weekday_mismatches
from payment_bot.logging import InMemoryAuditSink
from payment_bot.models import InboundEmail
from payment_bot.pipeline import Outcome, PaymentBotPipeline
from payment_bot.sample_data import sample_transport_pro_client
from payment_bot.tools.base import ToolContext
from payment_bot.tools.cargotel import CgtGetLoadStatus, CgtLoadIdInput, CgtLoadStatusOutput

pytestmark = pytest.mark.integration

LOAD_ID = "296006"
SENDER = "dispatch@exampletrucking.com"

#: Every figure traces to ``cgt_get_load_status``: $2,000 to the payable, and Thursday,
#: August 6, 2026 to invoice-received 07/07 plus Net 30.
DRAFT_BODY = (
    "Hi,\n\n"
    "Load 296006 is approved and scheduled. The amount is $2,000.00 and payment is "
    "expected on Thursday, August 6, 2026.\n\n"
    "Thanks,\nCircle Delivers Payments"
)


def _clients(**page_kwargs: object) -> MockCargoTelClient:
    load = parse_load_html(build_page(load_id=LOAD_ID, **page_kwargs), LOAD_ID)  # type: ignore[arg-type]
    carrier = parse_carrier_html(
        build_carrier_page(dispatch_email=SENDER, contact_emails=()), "74553"
    )
    # The synthetic load page has no carrier panel, so point it at the record explicitly.
    load = load.model_copy(update={"carrier_client_id": "74553"})
    return MockCargoTelClient({LOAD_ID: CargoTelLoadFixture(load=load, carrier=carrier)})


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "cargotel_replies": True}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def _llm() -> ScriptedLlmClient:
    def turn(index: int, name: str, payload: dict[str, object]) -> LlmResponse:
        return LlmResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(tool_use_id=f"tu-{index}", name=name, input=payload)],
        )

    return ScriptedLlmClient(
        [
            turn(1, "cgt_get_load_status", {"load_id": LOAD_ID}),
            turn(
                2,
                "check_authorization",
                {"sender_email": SENDER, "load_id": LOAD_ID, "system": "quickbooks"},
            ),
            turn(
                3,
                "submit_draft",
                {
                    "reply_body": DRAFT_BODY,
                    "to": SENDER,
                    "load_ids": [LOAD_ID],
                    "citations": [
                        {
                            "fact": "expected payment date",
                            "value": "2026-08-06",
                            "source_tool": "cgt_get_load_status",
                        }
                    ],
                },
            ),
        ]
    )


def _email(subject: str = f"Payment status for load {LOAD_ID}", body: str = "") -> InboundEmail:
    return InboundEmail(
        message_id=f"msg-{LOAD_ID}",
        thread_id=f"thread-{LOAD_ID}",
        from_email=SENDER,
        from_name="Example Trucking",
        subject=subject,
        body=body or f"Hi, when will load {LOAD_ID} be paid?\n\nThanks",
        thread_text="",
    )


#: The day these fixtures are answered on. The page data is dated early August 2026 and the
#: draft says payment "is expected on Thursday, August 6" — true on the 3rd, a false promise
#: on the 13th. Pinned rather than left to the clock so the tense check judges the draft
#: against the calendar the fixture was written for; `test_tense_consistency.py` covers the
#: passed-date case deliberately.
TODAY = date(2026, 8, 3)


def _pipeline(
    gmail: MockGmailClient,
    slack: MockSlackClient,
    audit: InMemoryAuditSink,
    *,
    cargotel: MockCargoTelClient | None = None,
    settings: Settings | None = None,
) -> PaymentBotPipeline:
    return PaymentBotPipeline(
        tp=sample_transport_pro_client(),
        cargotel=cargotel if cargotel is not None else _clients(),
        gmail=gmail,
        slack=slack,
        llm=_llm(),
        approval_resolver=ScriptedApprovalResolver(ApprovalDecision(ApprovalAction.APPROVE)),
        audit_sink=audit,
        settings=settings or _settings(),
        today=TODAY,
    )


def test_a_six_digit_load_is_answered_through_the_cargotel_tools() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    email = _email()

    result = _pipeline(gmail, slack, audit).process_email(email)

    assert result.outcome is Outcome.SENT, result.detail
    assert result.gate_result is not None and result.gate_result.allowed
    assert gmail.sent[0].to == SENDER
    assert "Thursday, August 6, 2026" in gmail.sent[0].body

    names = [e.tool_name for e in audit.for_correlation(email.message_id)]
    assert names == [
        "classify_intent",
        "extract_identifiers",
        "detect_sensitive_change",
        "check_authorization",
        "cgt_get_load_status",
        "check_authorization",
        "submit_draft",
    ]


def test_no_transport_pro_tool_is_called_for_a_six_digit_load() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    email = _email()

    _pipeline(gmail, slack, audit).process_email(email)

    names = [e.tool_name for e in audit.for_correlation(email.message_id)]
    assert not [n for n in names if n.startswith("tp_")]
    assert "compute_scheduled_pay_date" not in names


def test_the_computed_date_is_grounded_so_the_gate_accepts_it() -> None:
    """The date is computed by the domain rule, not read off the page — if the tool failed
    to record it in the ledger the gate would block every CargoTel draft."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()

    result = _pipeline(gmail, slack, audit).process_email(_email())

    assert result.gate_result is not None
    grounding = next(c for c in result.gate_result.checks if c.name == "grounding")
    assert grounding.passed, grounding.detail


def test_with_cargotel_disabled_a_six_digit_load_still_escalates() -> None:
    """Unchanged behaviour for a deployment that has not enabled the path."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    pipeline = _pipeline(
        gmail,
        slack,
        audit,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
    )

    result = pipeline.process_email(_email())

    assert result.outcome is Outcome.ESCALATED
    assert "non-Transport-Pro" in result.detail
    assert gmail.sent == []


def test_an_unauthorized_sender_never_reaches_the_model() -> None:
    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    email = _email().model_copy(update={"from_email": "stranger@example.test"})

    result = _pipeline(gmail, slack, audit).process_email(email)

    assert result.outcome is Outcome.ESCALATED
    assert "not authorized" in result.detail
    names = [e.tool_name for e in audit.for_correlation(email.message_id)]
    assert "cgt_get_load_status" not in names


def test_an_email_spanning_both_systems_escalates() -> None:
    """Answering one ledger and silently dropping the other is the failure to avoid."""

    gmail, slack, audit = MockGmailClient(), MockSlackClient(), InMemoryAuditSink()
    email = _email(
        subject=f"Payment status for {LOAD_ID} and 2462934",
        body=f"Status for load {LOAD_ID} and load 2462934 please",
    )

    result = _pipeline(gmail, slack, audit).process_email(email)

    assert result.outcome is Outcome.ESCALATED
    assert "spans both systems" in result.detail
    assert gmail.sent == []


def test_a_load_awaiting_paperwork_is_not_given_a_date() -> None:
    """The gate has no say here — the tool simply returns no date to quote."""

    cargotel = _clients(invoice_received=None, bol05=False, carrier_invoices=None)

    ctx = ToolContext(
        tp=sample_transport_pro_client(),
        cargotel=cargotel,
        ledger=GroundingLedger(),
        correlation_id="t",
        settings=_settings(),
    )
    out = CgtGetLoadStatus().run(CgtLoadIdInput(load_id=LOAD_ID), ctx)

    assert out.expected_payment_date is None
    assert out.missing_documents == ["BOL 05", "carrier invoice"]
    assert date(2026, 8, 6) not in ctx.ledger.grounded_dates


def _status(*, today: date = TODAY, **page_kwargs: object) -> CgtLoadStatusOutput:
    """Run ``cgt_get_load_status`` against the fixture page, as of ``today``."""

    ctx = ToolContext(
        tp=sample_transport_pro_client(),
        cargotel=_clients(**page_kwargs),
        ledger=GroundingLedger(),
        correlation_id="t",
        settings=_settings(),
        today=today,
    )
    return CgtGetLoadStatus().run(CgtLoadIdInput(load_id=LOAD_ID), ctx)


def test_every_date_leaves_the_tool_already_written_out() -> None:
    """Load 302866: a bare `date` is a request for the model to name the weekday itself.

    It named two, and got both wrong by a day — Saturday 2026-08-08 as "Friday" and
    Thursday 2026-07-09 as "Wednesday" — while citing this tool for each. The reply now has
    a string to copy for every date it may print, as the Transport Pro path has had since
    `compute_scheduled_pay_date` grew `scheduled_pay_date_display`.
    """

    out = _status()

    assert out.expected_payment_date_display == "Thursday, August 6, 2026"
    assert out.delivered_date_display == "Thursday, July 2, 2026"
    assert out.invoice_received_display == "Tuesday, July 7, 2026"


def test_the_written_out_date_is_the_date() -> None:
    """The display string and the field it renders can never be allowed to drift apart."""

    out = _status()
    for value, display in (
        (out.expected_payment_date, out.expected_payment_date_display),
        (out.delivered_date, out.delivered_date_display),
        (out.invoice_received, out.invoice_received_display),
    ):
        assert display is not None and value is not None
        assert find_weekday_mismatches(display) == [], display
        assert value.strftime("%Y") in display


def test_a_date_the_tool_does_not_have_is_rendered_as_nothing() -> None:
    """No date must never become a string — an empty rendering is a date to be invented."""

    out = _status(invoice_received=None, bol05=False, carrier_invoices=None)

    assert out.expected_payment_date_display is None
    assert out.invoice_received_display is None
    assert out.expected_payment_date_is_past is False


def test_a_pay_date_that_has_gone_by_says_so() -> None:
    """The flag the reply's tense hangs on. August 6 read differently on the 13th."""

    assert _status(today=date(2026, 8, 3)).expected_payment_date_is_past is False
    assert _status(today=date(2026, 8, 6)).expected_payment_date_is_past is False
    assert _status(today=date(2026, 8, 13)).expected_payment_date_is_past is True


# ---------------------------------------------------------------------------
# A rate question on a 6-digit load. This skill answers both asks — a CargoTel
# load has one payable and no line items to itemise — but the narrowing is only
# honest if the amount reaches the reply.
# ---------------------------------------------------------------------------
def _rate_intake(**kwargs: object) -> str:
    from payment_bot.agent.skills import build_cargotel_payment_status_intake
    from payment_bot.models import InboundEmail

    email = InboundEmail(
        message_id="<m>",
        thread_id="t",
        from_email="m.anastasovska@Trufunding.net",
        subject="Rate Verification, Please",
        body="Please verify the rates on the loads below.",
    )
    return build_cargotel_payment_status_intake(
        email,
        ["316039", "318410"],
        {"316039": "quickbooks", "318410": "quickbooks"},
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.integration
def test_a_rate_question_tells_the_agent_to_lead_with_the_amount() -> None:
    """Live regression: a Tru Funding rate enquiry over five loads got no figure at all.

    Every load was correctly reported as awaiting a carrier invoice while $2,150 and $3,000
    sat in the tool results. The prompt asked for dates and documents per billing state and
    never for the amount, so the reply obeyed it and dropped the question.
    """

    intake = _rate_intake(rate_question=True, stated_rates=[])

    assert "asked about the RATE" in intake
    assert "Lead with each load's amount" in intake
    assert "quoted no amount" in intake


@pytest.mark.integration
def test_a_quoted_amount_is_carried_in_for_comparison() -> None:
    from decimal import Decimal

    from payment_bot.tools.shared import StatedRate

    intake = _rate_intake(
        rate_question=True,
        stated_rates=[
            StatedRate(load_id="316039", amount=Decimal("2150")),
            StatedRate(load_id=None, amount=Decimal("3000")),
        ],
    )

    assert "316039: $2,150" in intake
    assert "unattributed: $3,000" in intake
    assert "Never adjust theirs to match" in intake


@pytest.mark.integration
def test_a_timing_question_carries_no_rate_wording() -> None:
    """Otherwise a plain "where is my money" invites an argument about undisputed figures."""

    from decimal import Decimal

    from payment_bot.tools.shared import StatedRate

    intake = _rate_intake(
        rate_question=False,
        stated_rates=[StatedRate(load_id="316039", amount=Decimal("2150"))],
    )

    assert "RATE" not in intake
    assert "2,150" not in intake


@pytest.mark.integration
def test_the_prompt_requires_the_amount_and_forbids_a_breakdown() -> None:
    """Both halves matter: state the one payable, and never invent line items beside it."""

    from payment_bot.agent.skills import CARGOTEL_PAYMENT_STATUS_SKILL

    prompt = CARGOTEL_PAYMENT_STATUS_SKILL.system_prompt

    assert "Give each load's `amount`" in prompt
    assert "State it even when the load is not yet scheduled" in prompt
    assert "no line items" in prompt
    assert CARGOTEL_PAYMENT_STATUS_SKILL.version == "1.3.0"


@pytest.mark.integration
def test_the_prompt_hands_dates_over_rather_than_asking_for_them() -> None:
    """1.2.0: neither the weekday nor the tense of a date may be the model's to work out.

    The formatting rule this replaced ("write dates as Thursday, August 6, 2026") named a
    shape no tool produced, which is a rule the model can only satisfy by deriving the very
    thing the same prompt forbids it to derive.
    """

    from payment_bot.agent.skills import CARGOTEL_PAYMENT_STATUS_SKILL

    prompt = CARGOTEL_PAYMENT_STATUS_SKILL.system_prompt

    for field in (
        "expected_payment_date_display",
        "delivered_date_display",
        "invoice_received_display",
    ):
        assert field in prompt, f"{field} is not offered to the model"
    assert "Never work out a weekday yourself" in prompt
    assert "expected_payment_date_is_past" in prompt


@pytest.mark.integration
def test_the_prompt_forbids_a_payment_claim_in_either_direction() -> None:
    """1.3.0: this system cannot report payment, so the reply may not characterise it.

    The bug this pins was mine, and it shipped for a few hours. 1.2.0's tense rule ended
    "say it is not showing as paid yet" — written to stop the model claiming payment, and
    instructing an equally ungrounded claim in the opposite direction. Live on load 298891:
    "was scheduled for payment on Wednesday, August 12, 2026, but is not yet showing as
    paid". Tense right, weekday right, every figure grounded, all thirteen gate checks
    passed — and the one clause a factoring company would act on came from the prompt.

    Grounding cannot catch it: it compares amounts and dates, never status prose. So the
    prompt is the control, and the assertion below is what keeps it.
    """

    from payment_bot.agent.skills import CARGOTEL_PAYMENT_STATUS_SKILL

    prompt = CARGOTEL_PAYMENT_STATUS_SKILL.system_prompt

    assert "NEVER say whether a load has been paid, in either direction" in prompt
    # The negative claim specifically — the direction that reads as harmless and is not.
    assert '"not yet paid"' in prompt and '"not showing as paid"' in prompt
    # And the reason, so a future edit cannot quietly reintroduce it as a formatting tweak.
    assert "`billing_state` has no paid value" in prompt


@pytest.mark.integration
def test_the_transport_pro_prompt_keeps_the_rule_this_system_cannot_support() -> None:
    """The same instruction is correct over there, and the asymmetry is the point.

    Transport Pro earning lines carry payment_status, actual_payment_date and check_number,
    so "not showing as paid" is a reading of the data rather than an inference from a signal
    that does not exist. Deleting it there would lose a true statement; keeping it here lost
    a grounded one. Same words, opposite verdicts, because the systems differ.
    """

    from payment_bot.agent.skills import PAYMENT_STATUS_SKILL

    assert "not showing as paid" in PAYMENT_STATUS_SKILL.system_prompt
