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


def _factored_tp() -> MockTransportProClient:
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
                factoring_company="England Carrier Services",
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
