"""One load, several carriers — the shape that denied a carrier their own load.

Every fixture here is load 2436437, trimmed verbatim from the live tenant. Its
``payment_information`` response is an array of **three payables**: FOX CARRIERS ($905,
remitted to eCapital), Alina Transport ($150 TONU, remitted to RTS Financial Service) and
Parasource Inc (a $5,000 line haul paid 06/25 plus a $230 lumper, remitted to England
Carrier Services). Its dispatch history has five rows — three delivered, two canceled.

The client read ``results[0]``. Everything above it therefore believed the load was FOX
CARRIERS' $905 and nothing else, which produced two failures at once on real mail:

* Parasource asked about their own load from ``parasourceinc.com`` and was DENIED, because
  the only carrier name authorization had ever seen was FOX CARRIERS. The denial dropped the
  load from the answer set, so the reply covered two of the three loads asked about.
* the $5,000 paid on 06/25 was not merely left out of the draft — it had never been read.

These tests pin the shape rather than the symptom: a payable per carrier, authorization over
all of them, and a reply scoped to the carrier who asked.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from tests.transport_pro_payloads import multi_carrier_transport, relay_transport

from payment_bot.clients.transport_pro_http import TransportProHttpClient
from payment_bot.config import Settings
from payment_bot.grounding import GroundingLedger
from payment_bot.models import AuthDecision, System, split_care_of
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import (
    CarrierCrossCheck,
    CarrierCrossCheckInput,
    CarrierCrossCheckOutput,
    CheckAuthorization,
    CheckAuthorizationInput,
    CheckAuthorizationOutput,
)
from payment_bot.tools.transport_pro import (
    LoadIdInput,
    TpGetLoadSummary,
    TpGetNoaFactoring,
    TpGetSettlementEntries,
    TpLoadSummaryOutput,
    TpNoaFactoringOutput,
    TpSettlementEntriesOutput,
)

LOAD = "2436437"
CARRIER = "Parasource Inc"
#: The carrier's own AR mailbox. Their dispatch contact on file is a Gmail address
#: (``parasourcedsp@gmail.com``), so nothing about this sender is on the load: they are
#: authorized by their domain naming their carrier, and by nothing else.
CARRIER_SENDER = "betty@parasourceinc.com"


def _client() -> TransportProHttpClient:
    return TransportProHttpClient(
        base_url="https://tp.example.test/api/v1",
        username="apiuser",
        password="secret",
        transport=multi_carrier_transport(),
    )


def _ctx(
    scope: dict[str, tuple[str, ...]] | None = None, **settings_kw: object
) -> ToolContext:
    return ToolContext(
        tp=_client(),
        ledger=GroundingLedger(),
        correlation_id="multi-carrier-test",
        settings=Settings(**settings_kw),  # type: ignore[arg-type]
        today=date(2026, 8, 21),
        disclosable_carriers=scope or {},
    )


def _authorize(sender: str, ctx: ToolContext) -> CheckAuthorizationOutput:
    out = CheckAuthorization().run(
        CheckAuthorizationInput(
            sender_email=sender, sender_name=None, load_id=LOAD, system=System.TRANSPORT_PRO
        ),
        ctx,
    )
    assert isinstance(out, CheckAuthorizationOutput)
    return out


def _summary(ctx: ToolContext) -> TpLoadSummaryOutput:
    out = TpGetLoadSummary().run(LoadIdInput(load_id=LOAD), ctx)
    assert isinstance(out, TpLoadSummaryOutput)
    return out


def _settlement(ctx: ToolContext) -> TpSettlementEntriesOutput:
    out = TpGetSettlementEntries().run(LoadIdInput(load_id=LOAD), ctx)
    assert isinstance(out, TpSettlementEntriesOutput)
    return out


# --- the client -------------------------------------------------------------
@pytest.mark.unit
def test_a_load_has_one_payable_per_carrier() -> None:
    payables = _client().get_load_payables(LOAD)

    assert [p.carrier_company for p in payables] == [
        "FOX CARRIERS",
        "Alina Transport Inc",
        "Parasource Inc",
    ]
    # Each carries its OWN remit-to. One `factoring_company` per load was never the shape of
    # this data: three carriers here assigned the load to three different factors.
    assert [p.factoring_company for p in payables] == [
        "eCapital Freight Factoring Corp",
        "RTS Financial Service, Inc",
        "England Carrier Services",
    ]


@pytest.mark.unit
def test_get_load_still_returns_the_first_payable() -> None:
    """The narrow accessor keeps its meaning, so callers that act on one carrier are unchanged."""

    assert _client().get_load(LOAD).carrier_company == "FOX CARRIERS"


@pytest.mark.unit
def test_the_pay_to_pairs_the_carrier_with_whoever_collects_for_them() -> None:
    payable = _client().get_load_payables(LOAD)[2]

    assert payable.pay_to == "Parasource Inc c/o England Carrier Services"
    assert split_care_of(payable.pay_to) == (CARRIER, "England Carrier Services")


@pytest.mark.unit
def test_the_payment_the_draft_was_missing_is_now_a_settlement_row() -> None:
    """The $5,000 the carrier wrote in about, on the payable that used to be unread."""

    rows = _client().get_settlement_entries(LOAD)
    line_haul = next(
        r for r in rows if r.paid_carrier == CARRIER and r.amount == Decimal("5000")
    )

    assert line_haul.carrier_name == "Parasource Inc c/o England Carrier Services"
    assert line_haul.paid_factoring_company == "England Carrier Services"
    # The application shows 06/25 where the API says 06/24 — see _app_pay_date. This is the
    # date the carrier quoted back at us, so the shift has to survive the multi-payable read.
    assert line_haul.pay_date == date(2026, 6, 25)
    # Every payable contributes: three FOX rows, one Alina row, two Parasource rows.
    assert len(rows) == 6


# --- authorization ----------------------------------------------------------
@pytest.mark.unit
def test_authorization_sees_every_carrier_and_every_factor() -> None:
    auth = _client().get_authorization_context(LOAD)

    # Payables first, then the dispatch-only carriers — including the canceled legs, whose
    # contact addresses this load's allow-list has always accepted.
    assert set(auth.carrier_companies) == {
        "FOX CARRIERS",
        "Alina Transport Inc",
        "Parasource Inc",
        "Hazemo Transport Llc",
        "Victory Transit Inc",
    }
    assert auth.factoring_companies == (
        "eCapital Freight Factoring Corp",
        "RTS Financial Service, Inc",
        "England Carrier Services",
    )
    # The pairing is the part no two parallel tuples could carry.
    assert auth.carriers_factored_to("England Carrier Services") == (CARRIER,)


@pytest.mark.unit
def test_the_carrier_that_hauled_it_is_no_longer_denied_their_own_load() -> None:
    """The regression. Live twice: 2026-08-04 and again on 2026-08-21.

    ``sender not authorized for any load: ['2436437=DENY (sender does not match any
    authorized party for this load)']`` — for a carrier asking where their own $5,000 went.
    """

    out = _authorize(CARRIER_SENDER, _ctx())

    assert out.decision is AuthDecision.ALLOW
    assert out.authorized is True
    assert out.matched_party == CARRIER
    # And scoped: authorized ABOUT the load is not authorized about FOX CARRIERS' leg of it.
    assert out.matched_carriers == (CARRIER,)


@pytest.mark.unit
def test_a_stranger_is_still_denied() -> None:
    """Five carriers is five names to match, not a lowered bar."""

    out = _authorize("ap@unrelatedbrokerage.com", _ctx())

    assert out.decision is AuthDecision.DENY
    assert out.authorized is False
    assert out.matched_carriers == ()


@pytest.mark.unit
def test_a_rostered_factor_is_answered_about_the_leg_it_collects_for() -> None:
    """England holds Parasource's assignment; eCapital and RTS hold other legs'.

    Before this, the factor of record was ``results[0]``'s — eCapital — so England's domain
    was measured against eCapital's roster entry and denied.
    """

    ctx = _ctx(
        allow_factoring=True,
        factoring_domains={"england carrier services": ("englandlogistics.com",)},
    )
    out = _authorize("ar@englandlogistics.com", ctx)

    assert out.decision is AuthDecision.FACTORING
    assert out.authorized is True
    assert out.matched_party == "England Carrier Services"
    assert out.matched_carriers == (CARRIER,)


@pytest.mark.unit
def test_one_factor_on_the_load_is_not_answered_about_another_factors_leg() -> None:
    """Three factors on one load, and each is still confined to its own carrier."""

    ctx = _ctx(
        allow_factoring=True,
        factoring_domains={"rts financial": ("rtsfinancial.com",)},
    )
    out = _authorize("adenslow@rtsfinancial.com", ctx)

    assert out.decision is AuthDecision.FACTORING
    assert out.matched_carriers == ("Alina Transport Inc",)
    assert CARRIER not in out.matched_carriers


# --- disclosure scope -------------------------------------------------------
@pytest.mark.unit
def test_the_load_summary_reports_every_carrier_when_nothing_narrows_it() -> None:
    out = _summary(_ctx())

    assert out.multiple_carriers is True
    assert [c.carrier_company for c in out.carriers] == [
        "FOX CARRIERS",
        "Alina Transport Inc",
        "Parasource Inc",
    ]
    assert [c.total_payout for c in out.carriers] == [
        Decimal("905"),
        Decimal("150"),
        Decimal("5230"),
    ]
    # The top-level fields are the FIRST payable's, which is exactly why the prompt may not
    # answer from them on a load like this one.
    assert out.carrier_company == "FOX CARRIERS"
    assert out.total_payout == Decimal("905")


@pytest.mark.unit
def test_the_load_summary_is_scoped_to_the_senders_own_carrier() -> None:
    ctx = _ctx({LOAD: (CARRIER,)})
    out = _summary(ctx)

    # Scoped, it IS a single-carrier answer — so the top-level fields are the whole of it and
    # `carriers` stays empty rather than repeating them.
    assert out.multiple_carriers is False
    assert out.carriers == []
    assert out.carrier_company == CARRIER
    assert out.total_payout == Decimal("5230")
    assert out.factoring_company == "England Carrier Services"
    # The string the reply quotes, and the one the drafts kept leaving out.
    assert out.pay_to == "Parasource Inc c/o England Carrier Services"

    # The enforcement, not the request: another carrier's figures are not in the ledger, so
    # the pre-send gate refuses them as ungrounded however they reach a draft.
    assert Decimal("5000") in ctx.ledger.grounded_amounts
    assert Decimal("905") not in ctx.ledger.grounded_amounts
    assert Decimal("255") not in ctx.ledger.grounded_amounts


@pytest.mark.unit
def test_settlement_entries_are_scoped_to_the_senders_own_carrier() -> None:
    ctx = _ctx({LOAD: (CARRIER,)})
    out = _settlement(ctx)

    assert {r.paid_carrier for r in out.entries} == {CARRIER}
    assert sorted(r.amount for r in out.entries) == [Decimal("230"), Decimal("5000")]
    assert out.multiple_payees is False
    assert Decimal("150") not in ctx.ledger.grounded_amounts


@pytest.mark.unit
def test_an_unscoped_settlement_read_names_each_payee() -> None:
    out = _settlement(_ctx())

    assert out.multiple_payees is True
    assert "Parasource Inc c/o England Carrier Services" in {
        r.carrier_name for r in out.entries
    }


@pytest.mark.unit
def test_a_scope_naming_a_carrier_with_no_payable_does_not_blank_the_load() -> None:
    """Victory Transit's leg was canceled, so it has no payable to narrow to.

    Narrowing to nothing would answer a real question with an empty summary, which reads as
    "this load has no payments" — worse than the over-broad answer it was avoiding.
    """

    out = _summary(_ctx({LOAD: ("Victory Transit Inc",)}))

    assert out.multiple_carriers is True
    assert len(out.carriers) == 3


@pytest.mark.unit
def test_where_payment_goes_is_answered_for_the_senders_own_carrier() -> None:
    """Scoping this one corrects a WRONG answer, not just an over-broad one.

    The factor of record was ``results[0]``'s, so a carrier asking where their payment goes
    was told the factor of whichever leg happened to be first — eCapital, for a carrier whose
    load is assigned to England Carrier Services.
    """

    out = TpGetNoaFactoring().run(LoadIdInput(load_id=LOAD), _ctx({LOAD: (CARRIER,)}))
    assert isinstance(out, TpNoaFactoringOutput)

    assert out.factoring_company_on_file == "England Carrier Services"
    assert "eCapital" not in (out.details or "")
    assert "RTS" not in (out.details or "")


@pytest.mark.unit
def test_an_unscoped_noa_read_names_each_carriers_factor() -> None:
    out = TpGetNoaFactoring().run(LoadIdInput(load_id=LOAD), _ctx())
    assert isinstance(out, TpNoaFactoringOutput)

    assert out.noa_on_file is True
    for factor in ("eCapital Freight Factoring Corp", "RTS Financial Service, Inc",
                   "England Carrier Services"):
        assert factor in (out.factoring_company_on_file or "")
    # Each factor is named beside the carrier it collects for, never as the load's own.
    assert "Parasource Inc: remit-to is England Carrier Services" in (out.details or "")


# --- cross-check ------------------------------------------------------------
@pytest.mark.unit
def test_cross_check_corroborates_across_several_delivered_rows() -> None:
    """Three delivered rows and three payees, all agreeing. Previously: ``mismatch``.

    The old reading compared the first delivered row (FOX CARRIERS) against the first
    settlement pay-to, and on this load those belong to different legs — so a load whose
    dispatch and settlement agree perfectly was reported as a contradiction.
    """

    out = CarrierCrossCheck().run(
        CarrierCrossCheckInput(load_id=LOAD, system=System.TRANSPORT_PRO), _ctx()
    )
    assert isinstance(out, CarrierCrossCheckOutput)

    assert out.ok is True
    assert "mismatch" not in out.issues
    assert out.delivered_carriers == ["FOX CARRIERS", "Parasource Inc", "Alina Transport Inc"]
    assert "Parasource Inc c/o England Carrier Services" in out.settlement_payees
    # Both flags the reply depends on: canceled legs were skipped, and the load has several
    # carriers, so an answer that does not say whose payment it is giving is wrong.
    assert "canceled_row_ignored" in out.issues
    assert "multiple_carriers" in out.issues


# --- the relay: four carriers, all delivered, attribution by contact --------
#
# Load 2469115. Different shape from 2436437 in the way that matters here: nothing was
# canceled, every carrier settled to its own factor, and every carrier has a CORPORATE contact
# domain. So each sender is recognised by their address or its domain — the two branches that
# could not narrow to a leg while the context flattened contacts away from their carriers.
RELAY = "2469115"


def _relay_ctx(scope: dict[str, tuple[str, ...]] | None = None) -> ToolContext:
    return ToolContext(
        tp=TransportProHttpClient(
            base_url="https://tp.example.test/api/v1",
            username="apiuser",
            password="secret",
            transport=relay_transport(),
        ),
        ledger=GroundingLedger(),
        correlation_id="relay-test",
        settings=Settings(),
        today=date(2026, 8, 21),
        disclosable_carriers=scope or {},
    )


def _relay_authorize(sender: str, ctx: ToolContext) -> CheckAuthorizationOutput:
    out = CheckAuthorization().run(
        CheckAuthorizationInput(
            sender_email=sender, sender_name=None, load_id=RELAY, system=System.TRANSPORT_PRO
        ),
        ctx,
    )
    assert isinstance(out, CheckAuthorizationOutput)
    return out


@pytest.mark.unit
def test_a_contact_address_is_attributed_to_its_own_carrier() -> None:
    out = _relay_authorize("dispatch@zmile.io", _relay_ctx())

    assert out.decision is AuthDecision.ALLOW
    assert out.matched_party == "Zmile Inc"
    assert out.matched_carriers == ("Zmile Inc",)


@pytest.mark.unit
def test_a_sender_at_a_contacts_domain_is_attributed_to_that_carrier() -> None:
    """The live case: payroll@ writes, dispatch@ is the address on file.

    Being ON the load authorized this sender before and still does. What changed is that the
    answer is now their own leg: unnarrowed, the summary's top-level fields were the first
    payable — Aralo Express's $2,580 remitted to RTS Financial, for a carrier owed $5,750 by
    Triumph Business Capital.
    """

    out = _relay_authorize("payroll@zmile.io", _relay_ctx())

    assert out.decision is AuthDecision.ALLOW
    assert out.matched_party == "Zmile Inc"
    assert out.matched_carriers == ("Zmile Inc",)


@pytest.mark.unit
def test_every_carrier_on_the_relay_gets_its_own_leg_and_no_other() -> None:
    """One load, four legs, four right answers — and no crossed wires between them."""

    expected = {
        "payroll@zmile.io": ("Zmile Inc c/o Triumph Business Capital", Decimal("5750")),
        "trafico@transporteshh.com": (
            "Transportes H&h Logistics Llc c/o Trilogy Solutions, LLC",
            Decimal("400"),
        ),
        "planner.northbound@araloexpressusa.com": (
            "Aralo Express Usa Inc c/o RTS Financial Service, Inc",
            Decimal("2580"),
        ),
        # Remit-to self, so the pay-to is the carrier's own name with no `c/o` half.
        "logistics@jomije.com": ("Jomije Transporting Llc", Decimal("475")),
    }

    for sender, (pay_to, total) in expected.items():
        ctx = _relay_ctx()
        auth = _relay_authorize(sender, ctx)
        ctx.disclosable_carriers[RELAY] = auth.matched_carriers

        out = TpGetLoadSummary().run(LoadIdInput(load_id=RELAY), ctx)
        assert isinstance(out, TpLoadSummaryOutput)
        assert out.pay_to == pay_to, sender
        assert out.total_payout == total, sender
        assert out.multiple_carriers is False, sender

        # The other three legs are not merely unreported — they are ungrounded, so the
        # pre-send gate refuses them however they reach a draft.
        for other in expected.values():
            if other[1] != total:
                assert other[1] not in ctx.ledger.grounded_amounts, (sender, other)


@pytest.mark.unit
def test_each_leg_keeps_its_own_pay_date() -> None:
    """Four legs, four settlements, four dates — $5,750 on 08/17, $400 the day after."""

    ctx = _relay_ctx({RELAY: ("Zmile Inc",)})
    out = _settlement_of(RELAY, ctx)

    assert [(r.amount, r.pay_date) for r in out.entries] == [
        (Decimal("5750"), date(2026, 8, 17))
    ]

    other = _relay_ctx({RELAY: ("Transportes H&h Logistics Llc",)})
    assert [(r.amount, r.pay_date) for r in _settlement_of(RELAY, other).entries] == [
        (Decimal("400"), date(2026, 8, 18))
    ]


@pytest.mark.unit
def test_the_relay_tells_each_carrier_only_its_own_factor() -> None:
    ctx = _relay_ctx({RELAY: ("Zmile Inc",)})
    out = TpGetNoaFactoring().run(LoadIdInput(load_id=RELAY), ctx)
    assert isinstance(out, TpNoaFactoringOutput)

    assert out.factoring_company_on_file == "Triumph Business Capital"
    for other in ("RTS Financial", "Trilogy"):
        assert other not in (out.details or "")


def _settlement_of(load_id: str, ctx: ToolContext) -> TpSettlementEntriesOutput:
    out = TpGetSettlementEntries().run(LoadIdInput(load_id=load_id), ctx)
    assert isinstance(out, TpSettlementEntriesOutput)
    return out
