"""A carrier whose dispatch was cancelled is never told another carrier's payment.

Live, load 2523099 (2026-10-07). KRGA Transport was dispatched, and that dispatch was
cancelled on 08/08; Circle Transportation Inc hauled the load and was paid $1,682.20 by direct
deposit on 09/14. Engaged Financial — KRGA's factor — asked about "KRGA Transport 2523099"
and the reply, approved and sent, read:

    "2523099 - KRGA Transport / Circle Transportation: This load was paid by direct deposit
    on Monday, September 14, 2026 ($1,281.76 line haul + $400.44 fuel surcharge = $1,682.20
    total). However, we do not currently have a Notice of Assignment ... for Engaged
    Financial on this load — please email those documents"

The factor then wrote back asking why we had paid their client directly, around their NOA.
Three defects, each covered here:

* the pre-NOA authorization rule answered a roster factor about EVERY carrier on a load with
  no factor on file — nothing narrowed it to the carrier the factor collects for;
* a sender whose carrier has no payable on the load was shown the whole load anyway, so even
  KRGA's own dispatcher would have been told Circle Transportation's payment;
* the reply asked for an NOA on a load the factor's client never ran.

The answer is now the one true sentence — KRGA's dispatch was cancelled, nothing is owed to
KRGA on it — written by code, never looked up.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from tests.transport_pro_payloads import multi_carrier_transport

from payment_bot.agent.skills import build_payment_status_intake
from payment_bot.clients import DeferredApprovalResolver, MockGmailClient, MockSlackClient
from payment_bot.clients.transport_pro_http import TransportProHttpClient
from payment_bot.config import Settings
from payment_bot.grounding import GroundingLedger
from payment_bot.logging import InMemoryAuditSink
from payment_bot.models import AuthDecision, AuthorizationContext, InboundEmail, System
from payment_bot.pipeline import Outcome, PaymentBotPipeline
from payment_bot.sample_data import sample_payment_status_email, scripted_payment_status_llm
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import CheckAuthorization, CheckAuthorizationInput
from payment_bot.tools.transport_pro import (
    LoadIdInput,
    TpGetNoaFactoring,
    TpGetSettlementEntries,
)

LOAD = "2523099"
KRGA = "KRGA TRANSPORT INC"
CIRCLE_TRANSPORTATION = "Circle Transportation Inc"
FACTOR_SENDER = "nbarnes@engagedfinance.com"
KRGA_DISPATCH = "ivan@krgatransport.org"

#: What the factor wrote, trimmed to one load (the live email also carried bank details).
FACTOR_EMAIL = (
    "Please provide payment status for the following loads:\n\n"
    "*        KRGA Transport 2523099\n\n"
    "If the load has not been paid, please provide an explanation of why."
)


class _Tp:
    """Transport Pro as it answers about 2523099: one payable, two dispatches."""

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        return AuthorizationContext(
            carrier_companies=(CIRCLE_TRANSPORTATION, KRGA),
            authorized_emails=("dispatchteam@circledelivers.com", KRGA_DISPATCH),
            carrier_contacts=(
                (CIRCLE_TRANSPORTATION, "dispatchteam@circledelivers.com"),
                (KRGA, KRGA_DISPATCH),
            ),
            payable_parties=((CIRCLE_TRANSPORTATION, None),),
            canceled_carriers=(KRGA,),
        )


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "allow_factoring": True,
        "factoring_prenoa_replies": True,
        "factoring_domains": {"engaged financial": ("engagedfinance.com",)},
    }
    base.update(overrides)
    return Settings(**base)


def _decide(sender: str, email_text: str = FACTOR_EMAIL, tp: Any = None):
    ctx = ToolContext(
        tp=tp or _Tp(),
        ledger=GroundingLedger(),
        correlation_id="cancelled-carrier",
        settings=_settings(),
        email_text=email_text,
    )
    return CheckAuthorization().run(
        CheckAuthorizationInput(sender_email=sender, load_id=LOAD, system=System.TRANSPORT_PRO),
        ctx,
    )


# --- authorization ---------------------------------------------------------------------
@pytest.mark.unit
def test_the_factor_is_answered_only_about_the_carrier_it_named() -> None:
    out = _decide(FACTOR_SENDER)

    assert out.decision is AuthDecision.FACTORING and out.authorized
    assert out.matched_carriers == (KRGA,)
    assert out.unpaid_carriers == (KRGA,)
    assert out.cancelled_carriers == (KRGA,)
    # KRGA has no payable here, so there is no NOA to ask for.
    assert out.pre_noa is False


@pytest.mark.unit
def test_the_cancelled_carriers_own_dispatcher_is_answered_the_same_way() -> None:
    out = _decide(KRGA_DISPATCH, email_text="any update on 2523099?")

    assert out.decision is AuthDecision.ALLOW
    assert out.matched_carriers == (KRGA,)
    assert out.unpaid_carriers == (KRGA,) and out.cancelled_carriers == (KRGA,)


@pytest.mark.unit
def test_a_factor_that_names_no_carrier_on_a_multi_carrier_load_is_not_answered() -> None:
    out = _decide(FACTOR_SENDER, email_text="Please provide payment status for 2523099.")

    assert out.decision is AuthDecision.DENY and not out.authorized
    assert "names none of its carriers" in out.reason


@pytest.mark.unit
def test_our_own_name_never_counts_as_naming_a_carrier() -> None:
    """Every email here says "Circle" — that is not naming Circle Transportation Inc."""

    out = _decide(
        FACTOR_SENDER,
        email_text="Payment status please for 2523099 - Circle Logistics / Circle Delivers",
    )

    assert out.decision is AuthDecision.DENY


@pytest.mark.unit
def test_a_single_carrier_load_needs_no_naming() -> None:
    """The common pre-NOA case is unchanged: one carrier, nothing to confuse it with."""

    class _OneCarrier:
        def get_authorization_context(self, load_id: str) -> AuthorizationContext:
            return AuthorizationContext(
                carrier_companies=("Maple Ridge Livestock Llc",),
                payable_parties=(("Maple Ridge Livestock Llc", None),),
            )

    out = _decide(FACTOR_SENDER, email_text="Rate verification for 2523099", tp=_OneCarrier())

    assert out.decision is AuthDecision.FACTORING and out.authorized
    assert out.pre_noa is True
    assert out.unpaid_carriers == ()


# --- the lookups fail closed --------------------------------------------------------------
def _multi_ctx(scope: tuple[str, ...]) -> ToolContext:
    return ToolContext(
        tp=TransportProHttpClient(
            base_url="https://tp.example.test/api/v1",
            username="apiuser",
            password="secret",
            transport=multi_carrier_transport(),
        ),
        ledger=GroundingLedger(),
        correlation_id="cancelled-carrier",
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        today=date(2026, 8, 21),
        disclosable_carriers={"2436437": scope},
    )


@pytest.mark.unit
def test_the_authorization_context_knows_whose_dispatch_was_cancelled() -> None:
    auth = _multi_ctx(()).tp.get_authorization_context("2436437")

    assert auth.canceled_carriers == ("Hazemo Transport Llc", "Victory Transit Inc")
    assert not auth.has_payable("Victory Transit Inc")
    assert auth.has_payable("parasource inc")


@pytest.mark.unit
def test_a_cancelled_carriers_settlement_read_is_empty_not_someone_elses() -> None:
    ctx = _multi_ctx(("Victory Transit Inc",))

    out = TpGetSettlementEntries().run(LoadIdInput(load_id="2436437"), ctx)

    # Nothing read means nothing grounded: the gate would block any amount from this load.
    assert out.entries == [] and out.empty


@pytest.mark.unit
def test_a_cancelled_carrier_is_told_no_factor_rather_than_another_carriers() -> None:
    out = TpGetNoaFactoring().run(LoadIdInput(load_id="2436437"), _multi_ctx(("Victory Transit Inc",)))

    assert out.factoring_company_on_file is None
    assert "no payable on this load for victory transit inc" in (out.details or "")


# --- the pipeline ----------------------------------------------------------------------
@pytest.mark.integration
def test_the_factor_gets_the_one_true_sentence_and_nothing_else() -> None:
    llm = scripted_payment_status_llm()
    slack = MockSlackClient()
    pipeline = PaymentBotPipeline(
        tp=_Tp(),  # type: ignore[arg-type]
        gmail=MockGmailClient(),
        slack=slack,
        llm=llm,
        approval_resolver=DeferredApprovalResolver(),
        audit_sink=InMemoryAuditSink(),
        settings=_settings(),
    )
    email = InboundEmail(
        message_id="<engaged@x>",
        thread_id="t-engaged",
        from_email=FACTOR_SENDER,
        from_name="Nicholas Barnes",
        subject="PayStatus Multiple Carriers",
        body=FACTOR_EMAIL,
    )

    result = pipeline.process_email(email)

    assert result.outcome is Outcome.AWAITING_REVIEW, result.detail
    assert llm.calls == [], "no lookup: nothing on the load is the factor's client's"
    body = result.draft.reply_body if result.draft else ""
    assert (
        f"Load {LOAD}: the dispatch to {KRGA} on this load was cancelled, so no payment is "
        f"owed to {KRGA} on it."
    ) in body
    assert "$" not in body
    assert "Circle Transportation" not in body
    assert "NOA" not in body and "Notice of Assignment" not in body
    assert result.gate_result is not None and result.gate_result.allowed


@pytest.mark.unit
def test_the_agent_is_told_the_sentence_when_other_loads_need_it() -> None:
    intake = build_payment_status_intake(
        sample_payment_status_email(),
        ["2462934"],
        {},
        not_paid_sentences=[
            f"Load {LOAD}: the dispatch to {KRGA} on this load was cancelled, so no payment "
            f"is owed to {KRGA} on it."
        ],
    )

    assert "Put each sentence below in the reply word for word" in intake
    assert "never mention any other carrier, amount" in intake
    assert f"Load {LOAD}: the dispatch to {KRGA}" in intake
