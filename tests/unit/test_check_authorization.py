"""Unit tests for check_authorization's policy-resolved ``authorized`` flag (§4.2).

The flag exists because decision alone misled the model: a live factoring sender the
pipeline had authorized got a refusal draft ("unable to provide rate details due to
authorization restrictions") because the skill prompt said only ALLOW counts. The tool now
resolves policy itself, and the prompts key off ``authorized``.
"""

from __future__ import annotations

import pytest

from payment_bot.clients import MockTransportProClient
from payment_bot.config import Settings
from payment_bot.grounding import GroundingLedger
from payment_bot.models import AuthDecision, AuthorizationContext, System
from payment_bot.sample_data import (
    SAMPLE_SENDER_EMAIL,
    build_load_2462934_fixture,
    sample_transport_pro_client,
)
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import (
    CheckAuthorization,
    CheckAuthorizationInput,
    CheckAuthorizationOutput,
)


def _factored_tp(factoring_company: str = "England Carrier Services") -> MockTransportProClient:
    """Load 2462934 with a factoring contact on file and no carrier contacts."""

    tp = sample_transport_pro_client()
    fixture = build_load_2462934_fixture()
    tp.add(
        fixture.__class__(
            load=fixture.load,
            dispatch=fixture.dispatch,
            settlement=fixture.settlement,
            files=fixture.files,
            authorization=AuthorizationContext(
                carrier_company="Idea Expedited, Inc",
                authorized_emails=(),
                factoring_company=factoring_company,
                factoring_emails=("ar@englandcarrier.com",),
            ),
        )
    )
    return tp


def _run(tp: MockTransportProClient, sender: str, **settings_kw: object) -> CheckAuthorizationOutput:
    ctx = ToolContext(
        tp=tp,
        ledger=GroundingLedger(),
        correlation_id="t",
        settings=Settings(**settings_kw),  # type: ignore[arg-type]
    )
    out = CheckAuthorization().run(
        CheckAuthorizationInput(
            sender_email=sender, sender_name=None, load_id="2462934", system=System.TRANSPORT_PRO
        ),
        ctx,
    )
    assert isinstance(out, CheckAuthorizationOutput)
    return out


@pytest.mark.unit
def test_allow_is_authorized() -> None:
    out = _run(sample_transport_pro_client(), SAMPLE_SENDER_EMAIL)
    assert out.decision is AuthDecision.ALLOW
    assert out.authorized


@pytest.mark.unit
def test_factoring_is_authorized_only_when_policy_allows() -> None:
    on = _run(_factored_tp(), "ar@englandcarrier.com", allow_factoring=True)
    assert on.decision is AuthDecision.FACTORING
    assert on.authorized

    off = _run(_factored_tp(), "ar@englandcarrier.com", allow_factoring=False)
    assert off.decision is AuthDecision.FACTORING
    assert not off.authorized


@pytest.mark.unit
def test_configured_factor_domain_matches_across_name_forms() -> None:
    """The roster's payName and the load's remit-to name spell the same factor differently.

    The roster is generated from the settlement export ("BUSBOT INCORPORATED DBA AXLE");
    the load records the remit-to name ("Axle Payments"). A curated domain must still
    answer — a strict substring test used to DENY this and escalate the email.
    """

    out = _run(
        _factored_tp("Axle Payments"),
        "status@axlepayments.com",
        allow_factoring=True,
        factoring_domains={"busbot incorporated dba axle": ("axlepayments.com",)},
    )
    assert out.decision is AuthDecision.FACTORING
    assert out.authorized


@pytest.mark.unit
def test_configured_factor_domain_matches_reverse_containment() -> None:
    """A shorter on-file name ("RTS") links to the fuller roster key ("rts financial")."""

    out = _run(
        _factored_tp("RTS"),
        "status@rtsfinancial.com",
        allow_factoring=True,
        factoring_domains={"rts financial": ("rtsfinancial.com",)},
    )
    assert out.decision is AuthDecision.FACTORING
    assert out.authorized


@pytest.mark.unit
def test_generic_name_tokens_do_not_link_unrelated_factors() -> None:
    """Sharing "capital" must not authorize one factor's domain for another's load."""

    out = _run(
        _factored_tp("Apex Capital"),
        "ops@altacapitale.com",
        allow_factoring=True,
        factoring_domains={"alta capital": ("altacapitale.com",)},
    )
    assert out.decision is AuthDecision.DENY
    assert not out.authorized


@pytest.mark.unit
def test_deny_is_never_authorized() -> None:
    out = _run(sample_transport_pro_client(), "stranger@example.com")
    assert out.decision is AuthDecision.DENY
    assert not out.authorized


@pytest.mark.unit
def test_integer_load_id_is_coerced_to_string() -> None:
    """Live models pass load_id as an int; that must not cost an agent iteration."""

    params = CheckAuthorizationInput.model_validate(
        {"sender_email": SAMPLE_SENDER_EMAIL, "load_id": 2462934, "system": "transport_pro"}
    )
    assert params.load_id == "2462934"


@pytest.mark.unit
def test_roster_factor_is_authorized_pre_noa_when_policy_allows() -> None:
    """The pre-funding flow: a roster-verified factor asks about a load with NO factor on
    file (their NOA has not reached us yet). With PAYBOT_FACTORING_PRENOA_REPLIES on, the
    reply is drafted and must request the NOA and billing paperwork."""

    out = _run(
        sample_transport_pro_client(),
        "ar@nextdayfundinginc.com",
        allow_factoring=True,
        factoring_prenoa_replies=True,
        factoring_domains={"next day funding": ("nextdayfundinginc.com",)},
    )
    assert out.decision is AuthDecision.FACTORING
    assert out.authorized
    assert out.pre_noa


@pytest.mark.unit
def test_pre_noa_is_off_by_default() -> None:
    out = _run(
        sample_transport_pro_client(),
        "ar@nextdayfundinginc.com",
        allow_factoring=True,
        factoring_domains={"next day funding": ("nextdayfundinginc.com",)},
    )
    assert out.decision is AuthDecision.DENY
    assert not out.authorized


@pytest.mark.unit
def test_pre_noa_never_fires_on_a_load_factored_to_someone_else() -> None:
    """One factor is never told about another's load, whatever the policy says."""

    out = _run(
        _factored_tp("Apex Capital Corp"),
        "ar@nextdayfundinginc.com",
        allow_factoring=True,
        factoring_prenoa_replies=True,
        factoring_domains={"next day funding": ("nextdayfundinginc.com",)},
    )
    assert out.decision is AuthDecision.DENY
    assert not out.authorized
    assert not out.pre_noa
