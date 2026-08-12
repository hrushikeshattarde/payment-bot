"""The CargoTel parser and its payment rules.

The load-bearing tests are the ones asserting a *difference* from the Transport Pro path —
that the payment date is the invoice-received date plus the load's own term, that the
Monday/Thursday rule is not applied, and that a page which is really the login page raises
instead of parsing as an empty load. Each of those is a case where the plausible-looking
wrong behaviour produces a confident, incorrect answer to a carrier.

Everything runs against the synthetic page in :mod:`tests.cargotel_pages`; real saved pages
carry customer data and are gitignored.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from tests.cargotel_pages import INVALID_ORDER_PAGE, LOGIN_PAGE, build_page

from payment_bot.clients.cargotel_html import (
    is_invalid_order,
    is_login_page,
    parse_load_html,
)
from payment_bot.domain import compute_scheduled_pay_date
from payment_bot.domain.cargotel import BillingState, missing_documents, resolve_payment
from payment_bot.errors import ClientError
from payment_bot.models.cargotel import net_days_in

pytestmark = pytest.mark.unit


def _load(**kwargs: object):
    return parse_load_html(build_page(**kwargs), str(kwargs.get("load_id", "296006")))  # type: ignore[arg-type]


# --- parsing ----------------------------------------------------------------
def test_the_headline_fields_parse() -> None:
    load = _load()

    assert load.load_id == "296006"
    assert load.carrier_name == "EXAMPLE TRUCKING LLC"
    assert load.business_unit == "CIRCLE LOGISTICS"
    assert load.status == "Delivered"
    assert load.status_date == date(2026, 7, 2)
    assert load.ap_terms == "Check Net 30"
    assert load.payable == Decimal("2000.00")
    assert load.pay_hold is False


def test_the_ap_invoice_is_taken_not_the_ar_one() -> None:
    """A page carries both. Taking the first ``Invoice:`` match reports the customer's.

    On the real load 295089 the A/R block holds invoice ``1125584`` while the carrier has no
    invoice at all — so this mistake does not merely mislabel a field, it invents a carrier
    invoice for a load that has none.
    """

    load = _load(ap_invoice="296006-INVDKD0098", ar_invoice="1125584")

    assert load.ap_invoice_number == "296006-INVDKD0098"


def test_an_unset_received_date_is_a_dropdown_not_a_blank() -> None:
    """CargoTel renders a month ``<select>`` when no date is recorded."""

    load = _load(invoice_received=None)

    assert load.invoice_received is None
    assert load.is_invoiced is False


def test_unset_terms_parse_as_none_rather_than_a_default() -> None:
    load = _load(ap_terms=None)

    assert load.ap_terms is None
    assert load.net_days is None


def test_the_pay_hold_flag_is_read() -> None:
    assert _load(pay_hold=True).pay_hold is True


def test_the_field_seen_marker_is_not_mistaken_for_a_hold() -> None:
    """The bug this caught on live data.

    CargoTel emits a hidden ``fs_``-prefixed marker (``value="1"``) beside the hold checkbox
    whenever that section renders — present on held and unheld loads alike. Reading it as the
    state reports every load as on hold: measured across nine real loads, where exactly one
    was. The state is the checkbox's ``checked`` attribute, nothing else.
    """

    page = build_page(pay_hold=False)
    assert 'name="loadmaint_form__fs_ldmnt_audit_carrier_pay_hold" value="1"' in page

    assert parse_load_html(page, "296006").pay_hold is False


# --- documents --------------------------------------------------------------
def test_generated_documents_are_not_mistaken_for_uploads() -> None:
    """Print Docs is a menu of what CargoTel can render, not a list of what arrived.

    A load with nothing uploaded still offers Image BOL, Standard BOL, Trip Sheet, Manifest,
    Carrier Agreement and Pre-Invoice. Counting those as evidence would mark every load as
    fully documented.
    """

    load = _load(bol05=False, carrier_invoices=None, carrier_agreement=False)

    assert load.documents  # the menu is there
    assert load.attachments == ()
    assert load.has_bol05 is False
    assert load.has_carrier_invoice is False
    assert missing_documents(load) == ("BOL 05", "carrier invoice")


def test_an_attached_invoice_is_detected_with_its_count() -> None:
    load = _load(carrier_invoices=2)

    assert load.has_carrier_invoice is True
    assert load.carrier_invoice_count == 2


def test_bol05_presence_is_the_bol_signal() -> None:
    """``Pdfgenbol05`` is only offered once that BOL has been uploaded."""

    assert _load(bol05=True).has_bol05 is True
    assert _load(bol05=False).has_bol05 is False


# --- the payment rule -------------------------------------------------------
def test_the_payment_date_is_invoice_received_plus_the_term() -> None:
    """The case verified against QuickBooks: 07/07 + Net 30 = 08/06."""

    state = resolve_payment(_load(invoice_received="07/07/2026", ap_terms="Check Net 30"))

    assert state.state is BillingState.SCHEDULED
    assert state.expected_payment_date == date(2026, 8, 6)
    assert state.invoice_received == date(2026, 7, 7)
    assert state.net_days == 30


def test_the_anchor_is_the_invoice_date_not_the_delivery_date() -> None:
    """Delivered 07/02, invoice received 07/07 — anchoring on delivery is five days wrong."""

    state = resolve_payment(_load(status_date="07/02/2026", invoice_received="07/07/2026"))

    assert state.expected_payment_date == date(2026, 8, 6)
    assert state.expected_payment_date != date(2026, 8, 1)


def test_the_monday_thursday_rule_is_not_applied() -> None:
    """The whole point of a separate rule set.

    06/29/2026 + 30 = 07/29/2026, a **Wednesday**. The Transport Pro rule would move that to
    Thursday the 30th, because those carriers are paid only on Mondays and Thursdays.
    CargoTel carriers are not, so the date must survive untouched — this fails the moment
    someone routes this path through ``compute_scheduled_pay_date``.
    """

    state = resolve_payment(_load(invoice_received="06/29/2026"))

    assert state.expected_payment_date == date(2026, 7, 29)
    assert state.expected_payment_date.strftime("%A") == "Wednesday"
    # The same date under the Transport Pro rule, to make the divergence explicit.
    assert compute_scheduled_pay_date(date(2026, 7, 29)).scheduled_pay_date == date(2026, 7, 30)


@pytest.mark.parametrize(
    ("terms", "expected"),
    [
        ("Check Net 30", 30),
        ("Net 7", 7),
        ("Check Net 45", 45),
        ("Due On Receipt", None),
        ("Prepaid", None),
        (None, None),
    ],
)
def test_terms_are_read_per_carrier_not_assumed(terms: str | None, expected: int | None) -> None:
    """Terms vary by carrier, so the day count comes from the load, never a default."""

    assert net_days_in(terms) == expected


def test_no_invoice_means_no_date_and_a_paperwork_answer() -> None:
    state = resolve_payment(_load(invoice_received=None, bol05=False, carrier_invoices=None))

    assert state.state is BillingState.AWAITING_PAPERWORK
    assert state.expected_payment_date is None
    assert state.missing_documents == ("BOL 05", "carrier invoice")
    assert state.note is not None and "needed from the sender" in state.note


def test_documents_in_but_not_yet_billed_does_not_chase_the_sender() -> None:
    """Real loads 275957 and 295894: full document set, no received date.

    The delay here is ours, not the carrier's. Answering "we have not received your invoice"
    when it is sitting attached to the load is false, and it asks a carrier who already did
    their part to do it again.
    """

    state = resolve_payment(_load(invoice_received=None, bol05=True, carrier_invoices=1))

    assert state.state is BillingState.AWAITING_BILLING
    assert state.expected_payment_date is None
    assert state.missing_documents == ()
    assert state.note is not None and "Do NOT ask the sender for paperwork" in state.note


def test_an_invoice_without_terms_gives_no_date() -> None:
    """Defaulting to 30 here would invent a payment promise off an unconfigured load."""

    state = resolve_payment(_load(invoice_received="07/07/2026", ap_terms=None))

    assert state.state is BillingState.INVOICED_NO_TERMS
    assert state.expected_payment_date is None
    assert state.note is not None and "no payment terms" in state.note


def test_a_pay_hold_outranks_everything() -> None:
    """A load can have every document and a clean term and still not be payable."""

    state = resolve_payment(_load(pay_hold=True, invoice_received="07/07/2026"))

    assert state.state is BillingState.ON_HOLD
    assert state.expected_payment_date is None
    assert state.note is not None and "hold" in state.note


def test_a_scheduled_load_still_missing_paperwork_is_flagged() -> None:
    """Billing accepted an invoice the file list does not show — say the date, not "complete"."""

    state = resolve_payment(_load(invoice_received="07/07/2026", bol05=False))

    assert state.state is BillingState.SCHEDULED
    assert state.expected_payment_date == date(2026, 8, 6)
    assert state.missing_documents == ("BOL 05",)
    assert state.note is not None and "do not tell the sender" in state.note


# --- the stale-cookie guard -------------------------------------------------
def test_the_login_page_raises_instead_of_parsing_as_an_empty_load() -> None:
    """The most dangerous failure on this path, because it is silent and confident.

    A stale cookie returns HTTP 200 with the login form. Parsed leniently it yields a load
    with no documents and no invoice — so the bot would tell a queue of carriers their
    paperwork is missing when it is not.
    """

    assert is_login_page(LOGIN_PAGE) is True

    with pytest.raises(ClientError, match="session cookie is probably expired"):
        parse_load_html(LOGIN_PAGE, "296006")


def test_a_real_page_is_not_mistaken_for_the_login_page() -> None:
    assert is_login_page(build_page()) is False


# --- the carrier record -----------------------------------------------------
def test_the_carrier_record_parses() -> None:
    from tests.cargotel_pages import build_carrier_page

    from payment_bot.clients.cargotel_html import parse_carrier_html

    carrier = parse_carrier_html(build_carrier_page(), "74553")

    assert carrier.client_id == "74553"
    assert carrier.name == "EXAMPLE TRUCKING INC"
    assert carrier.factoring_name == "SAINT JOHN CAPITAL C/O EXAMPLE TRUCKING INC"
    assert carrier.is_factored is True
    assert carrier.net_days == 30


def test_the_carrier_name_is_not_the_page_heading() -> None:
    """"Review Account - carrier" appears twice before the name; a loose pattern ate both."""

    from tests.cargotel_pages import build_carrier_page

    from payment_bot.clients.cargotel_html import parse_carrier_html

    carrier = parse_carrier_html(build_carrier_page(name="ACME HAULAGE LLC"), "74553")

    assert carrier.name == "ACME HAULAGE LLC"
    assert "Review Account" not in (carrier.name or "")


def test_contact_emails_in_the_javascript_array_are_collected() -> None:
    """One real carrier's second address exists only in ``clientContactData``."""

    from tests.cargotel_pages import build_carrier_page

    from payment_bot.clients.cargotel_html import parse_carrier_html

    carrier = parse_carrier_html(
        build_carrier_page(contact_emails=("ONLY.HERE@EXAMPLE.COM",)), "74553"
    )

    assert "only.here@example.com" in carrier.emails


def test_an_unfactored_carrier_reports_no_factoring() -> None:
    from tests.cargotel_pages import build_carrier_page

    from payment_bot.clients.cargotel_html import parse_carrier_html

    carrier = parse_carrier_html(build_carrier_page(factoring_name=None), "83608")

    assert carrier.factoring_name is None
    assert carrier.is_factored is False


@pytest.mark.parametrize("terms", ["Check Net 30", "ACH Net 30"])
def test_the_payment_method_in_the_terms_does_not_change_the_days(terms: str) -> None:
    """Real carriers use both spellings; only the number matters."""

    from tests.cargotel_pages import build_carrier_page

    from payment_bot.clients.cargotel_html import parse_carrier_html

    assert parse_carrier_html(build_carrier_page(ap_terms=terms), "1").net_days == 30


def test_carrier_terms_are_the_fallback_when_the_load_has_none() -> None:
    """Four of nine real loads had no load-level terms; their carrier records did."""

    from tests.cargotel_pages import build_carrier_page

    from payment_bot.clients.cargotel_html import parse_carrier_html

    load = _load(ap_terms=None, invoice_received="07/07/2026")
    carrier = parse_carrier_html(build_carrier_page(ap_terms="ACH Net 30"), "83608")

    assert resolve_payment(load).state is BillingState.INVOICED_NO_TERMS
    with_carrier = resolve_payment(load, carrier)
    assert with_carrier.state is BillingState.SCHEDULED
    assert with_carrier.expected_payment_date == date(2026, 8, 6)


def test_the_loads_own_terms_win_over_the_carrier_default() -> None:
    """A load set differently from its carrier's default was set that way deliberately."""

    from tests.cargotel_pages import build_carrier_page

    from payment_bot.clients.cargotel_html import parse_carrier_html

    load = _load(ap_terms="Check Net 7", invoice_received="07/07/2026")
    carrier = parse_carrier_html(build_carrier_page(ap_terms="Check Net 30"), "1")

    assert resolve_payment(load, carrier).expected_payment_date == date(2026, 7, 14)


# --- the C/O trap -----------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ST JOHN C/O BULLA TRANSPORTATION INC", "ST JOHN"),
        ("SAINT JOHN CAPITAL C/O EME AUTO TRANSPORT", "SAINT JOHN CAPITAL"),
        ("ST JOHN C/O NORTH TRUCKING LLC", "ST JOHN"),
        ("APEX CAPITAL", "APEX CAPITAL"),          # no C/O — untouched
        ("COASTAL CO TRANSPORT", "COASTAL CO TRANSPORT"),  # "CO" inside a name, untouched
    ],
)
def test_the_co_suffix_is_trimmed_from_the_factor(raw: str, expected: str) -> None:
    from payment_bot.models.cargotel import CargoTelCarrier

    assert CargoTelCarrier(client_id="1", factoring_name=raw).factoring_company == expected


def test_the_carriers_name_cannot_match_an_unrelated_factor() -> None:
    """The live hole this fix closes.

    CargoTel writes the payee as "<factor> C/O <carrier>", so the carrier's own words are in
    the string. Matched whole against the real 271-entry roster, "ST JOHN C/O NORTH TRUCKING
    LLC" linked to "posshel erzkontor north american" on the token "north" — which would have
    authorized that factor's domain to receive North Trucking's payment details.
    """

    from payment_bot.models.cargotel import CargoTelCarrier
    from payment_bot.tools.shared import _factor_names_match

    raw = "ST JOHN C/O NORTH TRUCKING LLC"
    trimmed = CargoTelCarrier(client_id="1", factoring_name=raw).factoring_company

    assert _factor_names_match("posshel erzkontor north american", raw) is True
    assert _factor_names_match("posshel erzkontor north american", trimmed or "") is False
    # …while the genuine factor still links.
    assert _factor_names_match("loves's solutions llc dba saint john capital, llc", trimmed or "")


def test_the_authorization_context_exposes_the_trimmed_factor() -> None:
    from payment_bot.clients.cargotel import build_authorization_context
    from payment_bot.models.cargotel import CargoTelCarrier

    carrier = CargoTelCarrier(
        client_id="74553",
        name="BULLA TRANSPORTATION INC",
        factoring_name="ST JOHN C/O BULLA TRANSPORTATION INC",
    )
    auth = build_authorization_context(_load(), carrier)

    assert auth.factoring_company == "ST JOHN"


def test_an_id_that_is_not_a_load_is_not_reported_as_an_expired_cookie() -> None:
    """Caught on live mail within minutes of enabling this path.

    A "Past Due Invoices" email carried ``405445`` — an invoice number. CargoTel answered
    HTTP 200 with "Invalid Order ID" and no load form, which the first version reported as an
    expired session. That sends whoever reads the escalation to fix a cookie that was never
    broken, while the real cause goes unnoticed. The two must read differently.
    """

    assert is_invalid_order(INVALID_ORDER_PAGE) is True
    assert is_invalid_order(LOGIN_PAGE) is False
    assert is_invalid_order(build_page()) is False

    with pytest.raises(ClientError, match="no load '405445' exists"):
        parse_load_html(INVALID_ORDER_PAGE, "405445")

    # …and the systemic case still says what it means.
    with pytest.raises(ClientError, match="session cookie is probably expired"):
        parse_load_html(LOGIN_PAGE, "296006")
