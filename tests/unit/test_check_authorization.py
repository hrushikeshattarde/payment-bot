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


# ---------------------------------------------------------------------------
# PAYBOT_CARRIER_CONTACTS — exact addresses authorised for one carrier's loads,
# on top of whatever the back office holds.
#
# Live on 2026-08-13: Always There Logistics wrote from a billing alias that was
# not on their Transport Pro record, which carried alwaystherelogistics@ and
# loads4logistics@. The record was out of date rather than the sender wrong, but
# nothing in a free-mail address can establish that — hence a grant of exactly
# one address, and never a domain.
# ---------------------------------------------------------------------------
CARRIER = "ALWAYS THERE LOGISTICS INC"
CONFIGURED = "alwaystherelogisticsbilling@gmail.com"


def _carrier_ctx(contacts: dict[str, tuple[str, ...]]) -> ToolContext:
    """Load 2462934 with a named carrier and no contacts of its own on file."""

    tp = sample_transport_pro_client()
    fixture = build_load_2462934_fixture()
    tp.add(
        fixture.__class__(
            load=fixture.load,
            dispatch=fixture.dispatch,
            settlement=fixture.settlement,
            files=fixture.files,
            noa_factoring=fixture.noa_factoring,
            authorization=AuthorizationContext(
                carrier_company=CARRIER, authorized_emails=()
            ),
        )
    )
    return ToolContext(
        tp=tp,
        ledger=GroundingLedger(),
        correlation_id="carrier-contacts",
        settings=Settings(_env_file=None, carrier_contacts=contacts),  # type: ignore[arg-type]
    )


def _decide(ctx: ToolContext, sender: str) -> CheckAuthorizationOutput:
    out = CheckAuthorization().run(
        CheckAuthorizationInput(
            sender_email=sender, load_id="2462934", system=System.TRANSPORT_PRO
        ),
        ctx,
    )
    assert isinstance(out, CheckAuthorizationOutput)
    return out


@pytest.mark.unit
def test_a_configured_carrier_contact_is_authorized() -> None:
    out = _decide(_carrier_ctx({CARRIER: (CONFIGURED,)}), CONFIGURED)

    assert out.decision is AuthDecision.ALLOW
    assert out.authorized is True
    assert out.matched_party == CARRIER
    # The reason must say where the grant came from: an address answered here is invisible
    # to anyone reading the carrier's record and wondering why the bot replied to it.
    assert "PAYBOT_CARRIER_CONTACTS" in out.reason


@pytest.mark.unit
def test_the_grant_is_the_address_and_not_its_domain() -> None:
    """The whole reason this is addresses rather than domains.

    Carriers are routinely on free mail — of four CargoTel carrier records measured, two
    listed only a Gmail address. A domain-shaped grant would authorise every Gmail user on
    earth for that carrier's loads.
    """

    ctx = _carrier_ctx({CARRIER: (CONFIGURED,)})

    assert _decide(ctx, "someone.else@gmail.com").decision is AuthDecision.DENY
    assert _decide(ctx, "billing@gmail.com").decision is AuthDecision.DENY
    # A domain accidentally configured where an address belongs authorises nobody.
    assert _decide(_carrier_ctx({CARRIER: ("gmail.com",)}), CONFIGURED).decision is (
        AuthDecision.DENY
    )


@pytest.mark.unit
def test_a_lookalike_of_the_configured_address_is_denied() -> None:
    ctx = _carrier_ctx({CARRIER: (CONFIGURED,)})

    for lookalike in (
        "alwaystherelogisticsbilling@gmail.com.evil.net",
        "alwaystherelogistics.billing@gmail.com",
        "alwaystherelogisticsbiIling@gmail.com",  # capital i for l
    ):
        assert _decide(ctx, lookalike).decision is AuthDecision.DENY, lookalike


@pytest.mark.unit
def test_one_carriers_configured_address_cannot_answer_for_another() -> None:
    """Why the carrier name is matched exactly rather than by token overlap.

    ``_factor_names_match`` would link "ALWAYS THERE LOGISTICS INC" to any other carrier
    sharing a distinctive word. For a grant that authorises a specific address against a
    specific carrier's loads, that looseness is the failure mode, not a convenience.
    """

    ctx = _carrier_ctx({"SOME OTHER LOGISTICS INC": (CONFIGURED,)})

    assert _decide(ctx, CONFIGURED).decision is AuthDecision.DENY


@pytest.mark.unit
def test_punctuation_and_case_in_the_carrier_name_do_not_matter() -> None:
    """Exact after normalisation, not byte-exact — the back office spells names variously."""

    for spelling in ("Always There Logistics, Inc.", "always there logistics inc"):
        ctx = _carrier_ctx({spelling: (CONFIGURED,)})
        assert _decide(ctx, CONFIGURED).decision is AuthDecision.ALLOW, spelling


@pytest.mark.unit
def test_configuring_nothing_changes_nothing() -> None:
    """The default must leave every existing decision exactly as it was."""

    assert _decide(_carrier_ctx({}), CONFIGURED).decision is AuthDecision.DENY


# ---------------------------------------------------------------------------
# Free mail can never authorise a FACTOR, however it got into the roster.
#
# Everything that writes the roster already excludes it — the generator skips
# those rows, the escalation packet refuses to propose one. Neither protects a
# hand edit to factoring_domains_manual.json, which is a file people edit under
# time pressure from an escalation whose own text says "add it to
# PAYBOT_FACTORING_DOMAINS if it is genuine".
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_a_free_mail_domain_in_the_roster_authorises_nobody() -> None:
    """Verified before the guard existed: the lookup matched it like any other domain.

    A single "gmail.com" pasted into the roster would have made every Gmail address on
    earth that factor, on every load factored to them, silently and forever.
    """

    from payment_bot.tools.shared import _is_configured_factor_domain

    ctx = ToolContext(
        tp=sample_transport_pro_client(),
        ledger=GroundingLedger(),
        correlation_id="free-mail",
        settings=Settings(  # type: ignore[arg-type]
            _env_file=None, factoring_domains={"acme factoring": ("gmail.com",)}
        ),
    )

    for sender in ("anyone@gmail.com", "attacker@gmail.com", "real.factor@gmail.com"):
        assert _is_configured_factor_domain("Acme Factoring", sender, ctx) is False, sender


@pytest.mark.unit
def test_the_guard_does_not_disturb_a_legitimate_roster_entry() -> None:
    """The corporate case is the common one and must be untouched."""

    from payment_bot.tools.shared import _is_configured_factor_domain

    ctx = ToolContext(
        tp=sample_transport_pro_client(),
        ledger=GroundingLedger(),
        correlation_id="free-mail",
        settings=Settings(  # type: ignore[arg-type]
            _env_file=None,
            factoring_domains={
                "rts financial": ("rtsfinancial.com",),
                "acme factoring": ("gmail.com",),  # inert, and beside a good entry
            },
        ),
    )

    assert _is_configured_factor_domain("RTS Financial Service, Inc", "ar@rtsfinancial.com", ctx)
    assert not _is_configured_factor_domain("RTS Financial Service, Inc", "ar@gmail.com", ctx)


@pytest.mark.unit
def test_a_bad_roster_entry_is_reportable_as_well_as_inert() -> None:
    """Silently inert configuration is how someone decides the roster is broken and re-edits."""

    from payment_bot.tools.shared import free_mail_roster_entries

    settings = Settings(  # type: ignore[arg-type]
        _env_file=None,
        factoring_domains={
            "rts financial": ("rtsfinancial.com",),
            "acme factoring": ("gmail.com", "acmefactoring.com"),
            "other": ("yahoo.com",),
        },
    )

    assert free_mail_roster_entries(settings) == {
        "acme factoring": ("gmail.com",),
        "other": ("yahoo.com",),
    }
    # A clean roster reports nothing at all.
    assert free_mail_roster_entries(
        Settings(_env_file=None, factoring_domains={"rts financial": ("rtsfinancial.com",)})  # type: ignore[arg-type]
    ) == {}
