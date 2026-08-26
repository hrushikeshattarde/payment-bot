"""Carrier-name matching: the free-mail carrier's way in, and its limits."""

from __future__ import annotations

import pytest

from payment_bot.config import Settings
from payment_bot.grounding import GroundingLedger
from payment_bot.models import AuthDecision, System
from payment_bot.sample_data import sample_transport_pro_client
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import CheckAuthorization, CheckAuthorizationInput
from payment_bot.tools.shared import carrier_name_matches_sender as _match

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("carrier", "sender"),
    [
        # Live senders this was built for. Both are free mail; neither was on file.
        ("NUC EXPRESS INC", "accnucexpress@yahoo.com"),
        ("CLEANPEACE EXPRESS LLC", "cleanpeace.safety@gmail.com"),
        ("CLEANPEACE EXPRESS LLC", "cleanpeacetruck@gmail.com"),
        ("EL LALO TRUCKING LLC", "ellalotrucking@gmail.com"),
        # A carrier on its own domain matches on the domain half.
        ("GREAT HORIZON FREIGHT LLC", "accounting@greathorizonfreight.com"),
    ],
)
def test_a_sender_naming_its_own_carrier_matches(carrier: str, sender: str) -> None:
    assert _match(carrier, sender) is not None


@pytest.mark.parametrize(
    ("carrier", "sender", "why"),
    [
        (
            "Can't stop won't stop Logistics LLC",
            "mrjtparks@gmail.com",
            "a person's name is not the company's",
        ),
        ("NUC EXPRESS INC", "randomperson@gmail.com", "no part of the name is present"),
        (
            "Transport LLC",
            "joestransport@gmail.com",
            "the name is industry furniture and nothing else, so it may never match",
        ),
        (
            "GSM TRANSPORT LLC",
            "invoices@outgo.com",
            "the factor's address is not the carrier's",
        ),
        ("ABC LLC", "abcdefg@gmail.com", "under the minimum, a fragment is not evidence"),
    ],
)
def test_what_must_not_match(carrier: str, sender: str, why: str) -> None:
    assert _match(carrier, sender) is None, why


def test_a_name_of_only_stopwords_can_never_authorise_anyone() -> None:
    """The worst case, called out because it is the one that would be silent.

    A carrier recorded as "Express Trucking LLC" reduces to nothing distinctive. Matching on
    that would hand every mailbox containing "express" the loads of a carrier called nothing
    else, so the whole-name branch is refused too, not just the distinctive one.
    """

    for sender in ("express@gmail.com", "expresstrucking@gmail.com", "a@expresstrucking.com"):
        assert _match("Express Trucking LLC", sender) is None


@pytest.mark.parametrize(
    ("mode", "expected", "authorized"),
    [
        ("off", AuthDecision.DENY, False),
        ("shadow", AuthDecision.DENY, False),
        ("enforce", AuthDecision.ALLOW, True),
    ],
)
def test_the_mode_decides_whether_a_name_match_authorises(
    mode: str, expected: AuthDecision, authorized: bool
) -> None:
    """Shadow logs and refuses; only enforce grants.

    Introduced the way llm_id_filter was: the agreement between this heuristic and reality
    has to be readable off a day of live mail before it decides anything. Every match logs
    `carrier_name_match` at WARNING in all three modes, which is what makes that possible.
    """

    tp = sample_transport_pro_client()
    ctx = ToolContext(
        tp=tp,
        ledger=GroundingLedger(),
        correlation_id="c",
        settings=Settings(carrier_name_match=mode),
    )
    # Load 2462934's carrier is "Idea Expedited, Inc"; this free-mail sender names it and is
    # on neither the load's contacts nor carrier_contacts.json.
    out = CheckAuthorization().run(
        CheckAuthorizationInput(
            sender_email="ideaexpedited.billing@gmail.com",
            load_id="2462934",
            system=System.TRANSPORT_PRO,
        ),
        ctx,
    )

    assert out.decision is expected
    assert out.authorized is authorized


def test_a_name_match_never_reaches_a_sender_the_record_already_denies_for_cause() -> None:
    """The match is a LAST resort, after every address path has been tried and missed.

    It cannot overturn anything: the branches above it return before this runs, so a sender
    the record positively authorises is still ALLOW on that basis, and this only ever
    converts what would otherwise have been a flat DENY.
    """

    tp = sample_transport_pro_client()
    ctx = ToolContext(
        tp=tp,
        ledger=GroundingLedger(),
        correlation_id="c",
        settings=Settings(carrier_name_match="enforce"),
    )
    out = CheckAuthorization().run(
        CheckAuthorizationInput(
            sender_email="stranger@unrelated-company.example",
            load_id="2462934",
            system=System.TRANSPORT_PRO,
        ),
        ctx,
    )

    assert out.decision is AuthDecision.DENY


@pytest.mark.parametrize(
    ("carrier", "sender"),
    [
        # Live on load 2543747: delivered, billing complete, and denied anyway.
        ("Trans 99 Logistics Usa Inc", "accountsteam1@trans99.net"),
        ("MIR Trans Inc (NC)", "accounting@mirtrans.com"),
        ("Reliable Freight Of America", "reliablefreight@gmail.com"),
    ],
)
def test_geography_in_a_legal_name_does_not_block_a_match(carrier: str, sender: str) -> None:
    """A carrier leaves "Usa" and "(NC)" out of its own mailbox, and it should not have to.

    Requiring EVERY distinctive token meant "usa" had to appear in the address. Trans 99
    Logistics Usa Inc delivered 2543747 and wrote from trans99.net; the load was denied and
    the reply never mentioned it. `usa` is not industry furniture so `_STOPWORDS` did not
    cover it, which is why `_CARRIER_PLACE_TOKENS` exists separately.
    """

    assert _match(carrier, sender) is not None


def test_a_place_name_alone_still_authorises_nobody() -> None:
    """Geography stops BLOCKING a match; it never becomes evidence for one."""

    assert _match("USA Trucking Inc", "usa@gmail.com") is None
    assert _match("North American Transport LLC", "north@gmail.com") is None
