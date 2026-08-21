"""The pre-NOA ask fires on a missing NOA, not on a missing factor NAME.

Live regression, load 2530268 (Maple Ridge Livestock LLC, 2026-08-13). Truckstop's
factoringverification@ asked to verify the rate. The draft answered it correctly and then
added:

    "Please email the NOA and billing paperwork to freightpay@circledelivers.com."

The load's file history held a **Notice of Assignment indexed three times over**, alongside
the Bill of Lading, Carrier Rate Agreement and Carrier Invoice. `tp_get_noa_factoring` even
reported it: ``NoaFactoring(noa_on_file=True, factoring_company_on_file=None,
details='factoring document(s) on file: Notice of Assignment')``.

The pre-NOA branch keyed off ``auth.factoring_company`` — the company NAME, which comes from
``remit_to`` and has to be typed in by hand — and that was empty. Two fields answering
different questions, and the rule consumed the one that does not mean "the NOA has not
arrived". Third instance of that shape after `missing_documents` (test_paperwork_request.py)
and `has_cancel_confirmation` (test_cancel_attribution.py).

AUTHORIZATION IS DELIBERATELY UNCHANGED by the fix. A roster-verified factor asking about a
load with no factor of record is the same sender whether or not their NOA is indexed, and
gating the whole branch on the NOA would have denied this sender outright. Only the ask moves.
"""

from __future__ import annotations

import pytest

from payment_bot.config import Settings
from payment_bot.grounding import GroundingLedger
from payment_bot.models import AuthDecision, AuthorizationContext, System
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import CheckAuthorization, CheckAuthorizationInput

pytestmark = pytest.mark.unit

LOAD = "2530268"
SENDER = "factoringverification@truckstop.com"
CARRIER = "Maple Ridge Livestock Llc"


class _Tp:
    """Transport Pro stand-in returning one authorization context."""

    def __init__(self, *, noa_on_file: bool, factoring_company: str | None = None) -> None:
        self._ctx = AuthorizationContext(
            carrier_companies=(CARRIER,),
            authorized_emails=("mapleridgelivestock@gmail.com", "cordellb623@gmail.com"),
            payable_parties=((CARRIER, factoring_company),),
            noa_on_file=noa_on_file,
        )

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        return self._ctx


def _decide(*, noa_on_file: bool, factoring_company: str | None = None):
    settings = Settings(
        _env_file=None,
        allow_factoring=True,
        factoring_prenoa_replies=True,
        # Truckstop Factoring is in the generated roster from the settlements export.
        factoring_domains={"truckstop factoring llc": ("truckstop.com",)},  # type: ignore[arg-type]
    )
    ctx = ToolContext(
        tp=_Tp(noa_on_file=noa_on_file, factoring_company=factoring_company),
        ledger=GroundingLedger(),
        correlation_id="prenoa-test",
        settings=settings,
    )
    return CheckAuthorization().run(
        CheckAuthorizationInput(
            sender_email=SENDER, load_id=LOAD, system=System.TRANSPORT_PRO
        ),
        ctx,
    )


# --- the live load ----------------------------------------------------------
def test_an_indexed_noa_suppresses_the_request() -> None:
    out = _decide(noa_on_file=True)

    assert out.decision is AuthDecision.FACTORING
    assert out.authorized is True, "authorization must not change"
    assert out.pre_noa is False, "the NOA is on the load; do not ask for it"
    assert "already indexed" in (out.reason or "")


def test_the_sender_is_still_authorized_when_the_noa_is_on_file() -> None:
    """The trap in fixing this: gating the whole branch would have DENIED this sender.

    Pre-NOA is the only branch that authorises a roster-verified factor on a load carrying no
    factor of record. Skipping it when an NOA is indexed would fall through to the
    carrier-domain check and refuse a legitimate factoring enquiry — trading a bad sentence
    for a lost reply.
    """

    out = _decide(noa_on_file=True)

    assert out.authorized is True
    assert out.matched_party == "truckstop factoring llc"


def test_a_genuinely_missing_noa_still_asks() -> None:
    """The pre-funding flow this branch exists for must keep working."""

    out = _decide(noa_on_file=False)

    assert out.decision is AuthDecision.FACTORING
    assert out.authorized is True
    assert out.pre_noa is True
    assert "request the NOA" in (out.reason or "")


def test_a_named_factor_never_reaches_the_prenoa_branch() -> None:
    """With a factor of record the earlier branch owns the decision, NOA or not."""

    out = _decide(noa_on_file=False, factoring_company="Truckstop Factoring LLC")

    assert out.decision is AuthDecision.FACTORING
    assert out.pre_noa is False


def test_the_context_defaults_to_not_on_file() -> None:
    """An unpopulated field must preserve the old behaviour rather than silence the ask."""

    assert AuthorizationContext().noa_on_file is False
