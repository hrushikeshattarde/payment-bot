"""A 6-digit load's reply may ask for a document only while it is awaiting one.

Live regression, loads 291174 / 291180 / 291117 (A & J Transport Service Inc, 2026-08-13).
The carrier had chased three times — two unreturned calls and an unanswered email — and the
draft replied:

    "All three are awaiting your carrier invoice to move forward with payment processing.
     Please send the carrier invoices for these loads to freightpay@circledelivers.com."

Billing had recorded the invoice on **07/14/2026** and assigned A/P number
``291756-00000938``. ``resolve_payment`` returned ``invoiced_no_terms`` with the note "state
that it is being processed and give no date". Every figure was grounded, every other check was
green, and the reply told a carrier who had waited a month that we were waiting on her.

The model did not invent it, which is why a prompt fix alone would not hold.
``missing_documents`` is returned **non-empty in states that are not awaiting paperwork**: an
invoice that reaches billing by email is never in the CargoTel Print Docs menu, so
``('carrier invoice',)`` rides along on ``invoiced_no_terms`` and on ``scheduled`` too. The
skill keys "name what is in missing_documents and ask the sender to send it" off exactly that
field. The field really did say "carrier invoice"; only ``billing_state`` said whose problem
it was.

The saved pages these facts came from are NOT in the repo — ``loads/`` is gitignored because
the pages carry real carrier names, contacts and VINs. The fixture below reproduces the shape
instead: invoice received, no invoice attachment, no terms on the load.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from tests.cargotel_pages import build_page

from payment_bot.clients import CargoTelLoadFixture, MockCargoTelClient
from payment_bot.clients.cargotel_html import parse_load_html
from payment_bot.gate import PreSendGate
from payment_bot.gate.presend import _paperwork_requests
from payment_bot.grounding import GroundingLedger
from payment_bot.models import InboundEmail
from payment_bot.sample_data import sample_transport_pro_client
from payment_bot.tools.base import ToolContext
from payment_bot.tools.submit import SubmitDraftOutput

pytestmark = pytest.mark.unit

CGT_LOAD = "291174"  # 6-digit -> CargoTel
TP_LOAD = "2462934"  # 7-digit -> Transport Pro

#: The wording that reached Gmail Drafts.
LIVE_DRAFT = (
    "Hi Allie,\n\n"
    "We have $600.00 payable on each of loads 291174, 291180, and 291117. All three are "
    "awaiting your carrier invoice to move forward with payment processing. Please send the "
    "carrier invoices for these loads to freightpay@circledelivers.com and we'll get them "
    "scheduled right away.\n\n"
    "Circle Delivers Payments"
)

#: The same facts with the ball left where it actually is.
CORRECTED_DRAFT = (
    "Hi Allie,\n\n"
    "Apologies for the silence — you shouldn't have had to chase these three times.\n\n"
    "We have $600.00 payable on each of loads 291174, 291180, and 291117, and your invoice "
    "00000938 is recorded as received on July 14, 2026. All three are with us and being "
    "processed, so there's nothing further we need from you.\n\n"
    "We don't have a payment date to confirm yet. Someone will check where these stand and "
    "come back to you directly.\n\n"
    "Circle Delivers Payments"
)


def _ctx(**page_kwargs: object) -> ToolContext:
    """A context whose CargoTel client serves one load in the state the test needs.

    ``carrier_client_id`` is left as the synthetic page renders it (absent), so the gate's
    carrier lookup is skipped and the load's own terms decide the state. That is the real
    A & J shape: no terms on the load.
    """

    defaults: dict[str, object] = {
        "load_id": CGT_LOAD,
        "carrier": "A & J TRANSPORT SERVICE INC",
        "status_date": "06/19/2026",
        "invoice_received": "07/14/2026",  # billing HAS it
        "ap_invoice": "291756-00000938",
        "ap_terms": None,  # -> invoiced_no_terms
        "carrier_invoices": None,  # never uploaded; it arrived by email
        "bol05": True,
        "payable": "600.00",
    }
    load = parse_load_html(build_page(**{**defaults, **page_kwargs}), CGT_LOAD)  # type: ignore[arg-type]

    ledger = GroundingLedger()
    ledger.record_amount(Decimal("600.00"), "cgt_get_load_status", load_id=CGT_LOAD)
    ledger.record_date(date(2026, 7, 14), "cgt_get_load_status", load_id=CGT_LOAD)
    return ToolContext(
        tp=sample_transport_pro_client(),
        cargotel=MockCargoTelClient({CGT_LOAD: CargoTelLoadFixture(load=load, carrier=None)}),
        ledger=ledger,
        correlation_id="paperwork-request-test",
        today=date(2026, 8, 14),
    )


def _check(reply_body: str, load_ids: list[str], **page_kwargs: object) -> tuple[bool, str]:
    result = PreSendGate(allow_factoring=True)._check_paperwork_request(
        SubmitDraftOutput(reply_body=reply_body, to="a@b.com", load_ids=load_ids, citations=[]),
        _ctx(**page_kwargs),
    )
    return result.passed, result.detail


# --- the live draft ---------------------------------------------------------
def test_the_a_and_j_draft_is_blocked() -> None:
    passed, detail = _check(LIVE_DRAFT, [CGT_LOAD])

    assert passed is False
    assert "invoiced_no_terms" in detail
    assert CGT_LOAD in detail


def test_the_corrected_wording_passes() -> None:
    """The fix has to be sayable, or the check blocks every honest "it's with us" reply.

    This body is the one that shipped, and it is the tightest case in the file: it says
    "nothing further we need from you" one sentence before "We don't have a payment date",
    which a word-window matcher joins into a request for a payment date.
    """

    passed, detail = _check(CORRECTED_DRAFT, [CGT_LOAD])
    assert passed is True, detail


def test_the_ask_is_allowed_when_the_load_really_is_awaiting_paperwork() -> None:
    """The check must not make a legitimate chase unsayable — that is its whole boundary."""

    passed, detail = _check(LIVE_DRAFT, [CGT_LOAD], invoice_received=None)
    assert passed is True, detail
    assert "awaiting_paperwork" in detail


def test_a_scheduled_load_still_blocks_the_ask() -> None:
    """The trap is widest here: scheduled ALSO carries missing_documents=('carrier invoice',).

    With terms on the load the same page resolves to ``scheduled`` with a real date, and its
    note is "billing has the invoice, but the file list still shows no carrier invoice — do
    not tell the sender their paperwork is complete". A date to quote makes the paperwork ask
    more misleading, not less.
    """

    passed, detail = _check(LIVE_DRAFT, [CGT_LOAD], ap_terms="Check Net 30")
    assert passed is False
    assert "scheduled" in detail


def test_a_transport_pro_load_is_not_policed() -> None:
    """Transport Pro's requirements come from its own file history, with no BillingState."""

    passed, _ = _check(LIVE_DRAFT.replace(CGT_LOAD, TP_LOAD), [TP_LOAD])
    assert passed is True


def test_it_fails_closed_when_the_state_cannot_be_re_derived() -> None:
    """Only reachable for a draft that IS making the ask, which is what makes it safe."""

    result = PreSendGate(allow_factoring=True)._check_paperwork_request(
        SubmitDraftOutput(
            reply_body=LIVE_DRAFT, to="a@b.com", load_ids=[CGT_LOAD], citations=[]
        ),
        ToolContext(
            tp=sample_transport_pro_client(),
            cargotel=None,  # no client wired
            ledger=GroundingLedger(),
            correlation_id="c",
            today=date(2026, 8, 14),
        ),
    )
    assert result.passed is False
    assert "could not be re-derived" in result.detail


def test_no_ask_passes_without_consulting_cargotel() -> None:
    """A reply that asks for nothing must not need a load fetch to be cleared."""

    result = PreSendGate(allow_factoring=True)._check_paperwork_request(
        SubmitDraftOutput(
            reply_body="We have $600.00 payable on load 291174.",
            to="a@b.com",
            load_ids=[CGT_LOAD],
            citations=[],
        ),
        ToolContext(
            tp=sample_transport_pro_client(),
            cargotel=None,  # would raise if consulted
            ledger=GroundingLedger(),
            correlation_id="c",
            today=date(2026, 8, 14),
        ),
    )
    assert result.passed is True


# --- the matcher ------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "All three are awaiting your carrier invoice to move forward.",
        "Please send the carrier invoices for these loads to freightpay@circledelivers.com.",
        "Please send the BOL 05 to freightpay@circledelivers.com.",
        "We are still waiting on your invoice.",
        "We have not received your carrier invoice.",
        "We don't have your paperwork yet.",
        "Your BOL is still outstanding.",
        "We need the bill of lading before we can schedule this.",
        "Once you forward the documents we'll get it scheduled.",
        "Could you resend the invoice?",
        "The carrier invoice is missing from our file.",
        # A real ask must not be laundered by assurance vocabulary in the same sentence —
        # the clearing spans use containment, and this match starts at "send", before any
        # clearing span opens.
        "Please send all outstanding documents so we have everything on file.",
    ],
)
def test_every_way_of_asking_is_caught(text: str) -> None:
    assert _paperwork_requests(text), text


@pytest.mark.parametrize(
    "text",
    [
        # Verbatim shapes from the G.H. Factor block (load 302618, 2026-08-19): the factor
        # asked "confirm you have all the documents needed", the draft answered that
        # nothing was missing, and the matcher read its own required vocabulary as a
        # request. The thread then re-drafted and re-blocked every 30 minutes for a night.
        "We have $2,500.00 payable on this load, and all required documents are on file "
        "— no paperwork is outstanding.",
        "No paperwork is outstanding on this load.",
        "None of the documents are missing.",
        "All required documents are on file.",
        "Every document has been received.",
    ],
)
def test_an_assurance_is_not_a_request(text: str) -> None:
    assert _paperwork_requests(text) == [], text


@pytest.mark.parametrize(
    "text",
    [
        # Required vocabulary in a real reply. A match on any of these is worse than the bug.
        "We have $600.00 payable on each of loads 291174, 291180, and 291117.",
        "Your invoice 00000938 is recorded as received on July 14, 2026.",
        "Your invoice and BOL are in our system and we're processing the load for payment.",
        "All three are with us and being processed, so there's nothing further we need "
        "from you.",
        "We don't have a payment date to confirm yet.",
        "It was scheduled for payment on Wednesday, August 12, 2026.",
        "The payment terms on this load are Check Net 30.",
        "This load is under review and someone will follow up.",
        "Someone will check where these stand and come back to you directly.",
        "We'll confirm when payment goes out.",
        "",
    ],
)
def test_the_vocabulary_an_honest_reply_needs_is_left_alone(text: str) -> None:
    assert _paperwork_requests(text) == [], text


def test_a_match_never_straddles_a_sentence_boundary() -> None:
    """The specific false positive a word-window matcher produces on the shipped reply."""

    assert _paperwork_requests("Nothing further we need from you. Send my regards.") == []


def test_the_gh_factor_assurance_passes_on_a_scheduled_load() -> None:
    """The inverse of test_a_scheduled_load_still_blocks_the_ask, from the same trap.

    ``scheduled`` still carries missing_documents=('carrier invoice',), and the correct
    reply to "confirm you have all the documents needed" says nothing is missing. That
    sentence must be sayable on a scheduled load, or every factor's completeness question
    blocks — and each block re-drafts every run until a human intervenes.
    """

    draft = (
        "Good Day, Carlin!\n\n"
        "We have $600.00 payable on this load, and all required documents are on file — "
        "no paperwork is outstanding. The load was scheduled for payment on Sunday, "
        "August 9, 2026; someone from our team will follow up.\n\n"
        "Circle Delivers Payments"
    )
    passed, detail = _check(draft, [CGT_LOAD], ap_terms="Check Net 30")
    assert passed is True, detail


# --- the reason this needed a gate check ------------------------------------
def test_every_other_check_passes_on_the_live_draft() -> None:
    """The point of adding it: the draft was green on all fourteen checks before."""

    ctx = _ctx()
    result = PreSendGate(allow_factoring=True).evaluate(
        draft=SubmitDraftOutput(
            reply_body=LIVE_DRAFT, to="a@b.com", load_ids=[], citations=[]
        ),
        email=InboundEmail(
            message_id="<m>",
            thread_id="t",
            from_email="billing@ajtransport.example",
            subject="Invoice # 00000938",
            body="when will payment be made",
        ),
        ctx=ctx,
        expected_load_ids=None,
    )
    for name in ("grounding", "weekday_consistency", "tense_consistency",
                 "cargotel_payment_claim", "noa_request", "placeholders"):
        check = next(c for c in result.checks if c.name == name)
        assert check.passed is True, f"{name} unexpectedly failed: {check.detail}"

    assert any(c.name == "paperwork_request" for c in result.checks)
