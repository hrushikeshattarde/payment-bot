"""Unit tests for the extract_identifiers tool (§4.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import (
    ExtractIdentifiers,
    ExtractIdentifiersInput,
    ExtractIdentifiersOutput,
)


def _run(ctx: ToolContext, **kw: str) -> ExtractIdentifiersOutput:
    out = ExtractIdentifiers().run(ExtractIdentifiersInput(**kw), ctx)
    assert isinstance(out, ExtractIdentifiersOutput)
    return out


@pytest.mark.unit
def test_extracts_7_and_6_digit_load_ids(ctx: ToolContext) -> None:
    out = _run(ctx, subject="Loads 2462934 and 123456", body="please advise")
    assert out.load_ids == ["2462934", "123456"]


@pytest.mark.unit
def test_ignores_short_and_long_numbers(ctx: ToolContext) -> None:
    # 5-digit, 8-digit, and a 10-digit phone number are not load ids.
    out = _run(ctx, body="ref 12345, po 12345678, call 5551234567")
    assert out.load_ids == []


@pytest.mark.unit
def test_captures_stated_rate_with_load_on_same_line(ctx: ToolContext) -> None:
    out = _run(ctx, body="Load 2499505 rate is $9,300 per the rate con")
    assert out.stated_rates[0].amount == Decimal("9300")
    assert out.stated_rates[0].load_id == "2499505"


@pytest.mark.unit
def test_captures_sender_invoice_number(ctx: ToolContext) -> None:
    out = _run(ctx, body="Our Invoice #4540 covers load 2462934")
    assert "4540" in out.sender_invoice_numbers


@pytest.mark.unit
def test_detects_column_hints(ctx: ToolContext) -> None:
    out = _run(ctx, body="See Reference# and P.O. Number columns")
    assert out.column_hints  # at least one hint detected


@pytest.mark.unit
def test_dedupes_repeated_load_ids(ctx: ToolContext) -> None:
    out = _run(ctx, subject="2462934", body="2462934 again 2462934")
    assert out.load_ids == ["2462934"]


# --- Numbers that are not load ids -------------------------------------------
@pytest.mark.unit
def test_a_po_box_is_not_a_load_id(ctx: ToolContext) -> None:
    """Verbatim from live mail. The address blocked an email whose load was in the subject.

    "Payment Status: Load#2433209" escalated as a QuickBooks load because 840267 was read as
    a 6-digit load id — one non-Transport-Pro id stops the whole email.
    """

    out = _run(
        ctx,
        subject="Payment Status: Load#2433209",
        body=(
            "Please provide payment status on the invoices below and confirm all payments "
            "will be made to RTS Financial Service P.O. Box 840267 Dallas, TX 75284-0267."
        ),
    )
    assert out.load_ids == ["2433209"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "Carrier: DYNASTY FREIGHT LINES LLC (MC-1757862) needs an update on 2433209.",
        "MC# 1757862 asking about load 2433209.",
        "DOT 1757862, load 2433209 please.",
        "Suite 840267, load 2433209.",
        "Phone 840267 — load 2433209.",
    ],
)
def test_labelled_numbers_are_skipped(ctx: ToolContext, body: str) -> None:
    out = _run(ctx, subject="", body=body)
    assert out.load_ids == ["2433209"], out.load_ids


@pytest.mark.unit
def test_a_reference_number_is_still_a_load_id(ctx: ToolContext) -> None:
    """Factoring templates write the load itself as "Reference#" — do not skip that."""

    out = _run(ctx, subject="Rate Verification", body="Reference#: 2520504\nRate: $1,200.00")
    assert "2520504" in out.load_ids


@pytest.mark.unit
def test_a_numbered_company_is_not_a_load_id(ctx: ToolContext) -> None:
    """Verbatim from live mail. The registration number sits INSIDE the carrier's name.

    "KARNAL FREIGHT SYSTEM OB 9591699 CANADA INC." put a phantom 7-digit load on an
    answerable email; the prefix labels cannot catch it because the tell — the corporate
    suffix — comes after the number.
    """

    out = _run(
        ctx,
        subject="Circle Inv#13707 load 2477822 no payment on portal",
        body=(
            "Kindly Comment/Reason on Payment Status\n\n"
            "KARNAL FREIGHT SYSTEM OB 9591699 CANADA INC. (USD)\n"
        ),
    )
    assert out.load_ids == ["2477822"], out.load_ids


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "Carrier 1042212 Ontario Inc DBA Fast Lanes, load 2433209.",
        "Payment for 987654 LLC, load 2433209 please.",
        "Remit to 7654321 Canada Ltd. Load 2433209.",
    ],
)
def test_corporate_registration_numbers_are_skipped(ctx: ToolContext, body: str) -> None:
    out = _run(ctx, subject="", body=body)
    assert out.load_ids == ["2433209"], out.load_ids


@pytest.mark.unit
def test_a_load_id_before_an_unrelated_company_name_is_kept(ctx: ToolContext) -> None:
    """The suffix must be adjacent: a company name merely following a load id changes nothing."""

    out = _run(ctx, body="Load 2477822 - KARNAL FREIGHT SYSTEM INC is waiting on payment.")
    assert out.load_ids == ["2477822"], out.load_ids


@pytest.mark.unit
def test_numbers_inside_urls_are_not_load_ids(ctx: ToolContext) -> None:
    """Verbatim from live mail: iThrive's signature links their LinkedIn company page,
    and its 7-digit id became a phantom load that Transport Pro 400'd on — on every
    email they ever sent."""

    out = _run(
        ctx,
        subject="VERIFICATION REQUEST: Load #2515153",
        body=(
            "Please verify the rate.\n"
            "[cid:2e6605ee] <https://www.linkedin.com/company/6425192>\n"
            "Refer A Friend, Earn $200! Click Here<http://www.ithrive.com/refer?id=9988776>\n"
            "www.tracking.example/track/1234567\n"
        ),
    )
    assert out.load_ids == ["2515153"], out.load_ids


# --- Remittance blocks: bank numbers are not load ids -------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "labelled",
    [
        "Account #2657147",
        "Account 2657147",
        "Acct #2657147",
        "ACCOUNT NO. 2657147",
        "Routing #2657147",
    ],
)
def test_a_bank_account_number_is_not_a_load_id(ctx: ToolContext, labelled: str) -> None:
    """Live block: Engaged Finance's remittance block put their ACH account on the email.

    "Account #2657147" was read as a load, Transport Pro 400'd on it, the gate's
    authorization check failed on the unresolvable id, and the draft told the factoring
    company it "could not locate load 2657147" — their own bank account number. Every
    factoring template states remit details this way.
    """

    out = _run(ctx, body=f"Payment for load 2523916.\n\nElectronic payments to:\n{labelled}")
    assert out.load_ids == ["2523916"], out.load_ids


@pytest.mark.unit
def test_the_whole_engaged_finance_remittance_block(ctx: ToolContext) -> None:
    """The real email, verbatim: two loads asked about, no bank numbers picked up."""

    body = (
        "The stated loads are not due for payment, but we would like to confirm the "
        "following for 2523916 and 2526677:\n\n"
        "- All check payments will be issued to Engaged Financial, LLC at the following "
        "address:\nP.O. Box 775553\nChicago, IL 60677-5553\n\n"
        "- Electronic payments will be issued to\n\n"
        "Account #2657147\nABA #071006486\nCanadian Imperial Bank of Commerce (CIBC)\n"
    )
    out = _run(ctx, subject="Pre Terms Payment Status* Load # 2523916 & 2526677", body=body)
    assert sorted(out.load_ids) == ["2523916", "2526677"], out.load_ids


@pytest.mark.unit
@pytest.mark.parametrize(
    "phrasing",
    ["Load No. 2523916", "Load Number 2523916", "load no 2523916", "Load # 2523916"],
)
def test_a_load_written_with_no_is_still_a_load(ctx: ToolContext, phrasing: str) -> None:
    """The "no."/"number" filler must never suppress on its own.

    It is only accepted directly after a bank label. Treating a bare "No." as a label would
    discard the ids this tool exists to find.
    """

    out = _run(ctx, body=f"Please advise on {phrasing}.")
    assert out.load_ids == ["2523916"], out.load_ids


@pytest.mark.unit
@pytest.mark.parametrize(
    "labelled",
    ["Settlement 1311088", "Settlement #1311088", "Settlement No. 1311088", "Check #1311088"],
)
def test_a_settlement_or_check_number_is_not_a_load_id(
    ctx: ToolContext, labelled: str
) -> None:
    """Measured collision: 1311088 is a real load belonging to an UNRELATED carrier.

    "Circle Logistics, Inc - Settlement 1311088" arrived from bngtransportation.com; that
    number is a load id owned by Power Transport, LLC. Reading a settlement number as a load
    risks disclosing a third party's load — the same failure as the RTS/Skyway draft.
    """

    out = _run(ctx, subject=f"Fwd: Circle Logistics, Inc - {labelled}", body="Please advise.")
    assert out.load_ids == [], out.load_ids


# ---------------------------------------------------------------------------
# A sender's own invoice number is not one of our loads — but only when
# something in the same email contradicts it. See
# `_drop_stray_sender_invoice_ids` for why the bare word "invoice" cannot be a
# suppression label.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_a_sender_invoice_number_does_not_fake_a_second_system(ctx: ToolContext) -> None:
    """The OperFi regression: an answerable email escalated as spanning two systems.

    "Load #: 2485194" is a real Transport Pro load — carrier Mays Transport, factored to
    Operation Finance, which is the sender. "OperFi Invoice #: 318354" is the sender's own
    invoice number; six digits routed it to CargoTel, where it collided with a record having
    no carrier and no factor, and the whole email escalated.
    """

    out = _run(
        ctx,
        subject="2ND REQUEST: PAYMENT STATUS - Load 2485194 - OperFi Invoice# 318354",
        body="Load #: 2485194\nOperFi Invoice #: 318354\nOperation Finance\nPO Box 227352",
    )

    assert out.load_ids == ["2485194"]
    # Still reported as what it is, so a reviewer can see the reference the sender quoted.
    assert out.sender_invoice_numbers == ["318354"]


@pytest.mark.unit
def test_an_invoice_number_that_is_the_load_survives_on_its_own(ctx: ToolContext) -> None:
    """Carriers say "Invoice 2462934" meaning a real Transport Pro load.

    Nothing contradicts it, so it must stay. Dropping it would refuse a real question — a
    false negative, which is worse than the escalation this rule exists to prevent.
    """

    assert _run(ctx, body="Invoice 2462934 please").load_ids == ["2462934"]


@pytest.mark.unit
def test_an_invoice_labelled_load_survives_alongside_a_same_system_load(
    ctx: ToolContext,
) -> None:
    """Both 7-digit: one system, no disagreement, so nothing is dropped."""

    out = _run(ctx, body="Invoice 2462934 and load 2485194 please")

    assert out.load_ids == ["2462934", "2485194"]


@pytest.mark.unit
def test_a_genuine_two_system_email_still_spans_two_systems(ctx: ToolContext) -> None:
    """No invoice label, so neither id is a candidate and the escalation must still fire."""

    out = _run(ctx, body="Load 2485194 and load 318354 please")

    assert out.load_ids == ["2485194", "318354"]


@pytest.mark.unit
def test_an_email_whose_only_ids_are_invoice_numbers_is_left_alone(ctx: ToolContext) -> None:
    """No anchor system, so there is nothing to judge the ids against.

    Dropping here would empty the email of loads entirely on the strength of a label.
    """

    out = _run(ctx, body="Invoice 2462934 and invoice 318354")

    assert out.load_ids == ["2462934", "318354"]


@pytest.mark.unit
def test_a_sender_invoice_number_in_the_anchor_system_is_kept(ctx: ToolContext) -> None:
    """Only a *different* system makes a sender-invoice id a stray.

    Here the invoice number is 7-digit like the load, so it may well be the same load
    referred to twice; it is not this rule's business to decide otherwise.
    """

    out = _run(ctx, subject="Load 2485194", body="Our invoice 2462934 covers it")

    assert out.load_ids == ["2485194", "2462934"]


# ---------------------------------------------------------------------------
# The HTML part. A sender's plain-text alternative need not say what their HTML
# says, and portal collections mail proves it: the invoice table is HTML-only.
# ---------------------------------------------------------------------------
_SUMMAR_HTML = """
<html><head><style>.t{width:600px;color:#1a2b3c}</style>
<script>var trackingId=4839201;</script></head>
<body><img src="http://url5942.summar.com/x/689196371/pixel.gif" width="600">
<p>Dear Circle Logistics, please release them on your system.</p>
<table><tr><th>Invoice No</th><th>Load No</th><th>Date</th><th>Carrier</th><th>Amount</th></tr>
<tr><td>2502262</td><td>2502262</td><td>07/14/2026</td><td>D&amp;Y USA Inc</td><td>$1,300.00</td></tr>
</table>
<p>Payments to Summar at P.O BOX 748841, Atlanta, GA 30374-8841.</p></body></html>
"""


@pytest.mark.unit
def test_a_load_id_that_exists_only_in_the_html_is_found(ctx: ToolContext) -> None:
    """The Summar regression: escalated "no valid 6/7-digit load id found" over 2502262.

    Its plain-text alternative carried the prose and dropped the invoice table, so the id,
    the carrier and the amount were HTML-only — and `html` was captured by the Gmail client
    and read by nothing.
    """

    from payment_bot.models import InboundEmail

    email = InboundEmail(
        message_id="<m>",
        thread_id="t",
        from_email="sserna@summar.com",
        subject="Missing Website Payment status - Summar Financial LLC",
        body="Dear Circle Logistics, please release them on your system. Sincerely, Summar",
        html=_SUMMAR_HTML,
    )
    out = _run(ctx, subject=email.subject, body=email.body, html_text=email.html_text)

    assert out.load_ids == ["2502262"]
    assert "D&Y USA Inc" in out.carrier_names


@pytest.mark.unit
def test_markup_does_not_become_phantom_load_ids(ctx: ToolContext) -> None:
    """The reason tags are stripped rather than parsed.

    Tracking ids, pixel URLs, widths and hex colours all live in attributes or in
    script/style bodies, so removing those leaves only text a human would have read.
    `4839201` (a script variable) and `689196371` (a URL path) must not become loads.
    """

    from payment_bot.models import InboundEmail

    email = InboundEmail(
        message_id="<m>", thread_id="t", from_email="a@b.com", html=_SUMMAR_HTML
    )
    out = _run(ctx, html_text=email.html_text)

    assert out.load_ids == ["2502262"]
    assert "4839201" not in out.load_ids
    assert "689196371" not in out.load_ids
    # The PO Box is visible text, and the existing label rule still suppresses it.
    assert "748841" not in out.load_ids


@pytest.mark.unit
def test_an_email_with_no_html_part_is_unaffected(ctx: ToolContext) -> None:
    from payment_bot.models import InboundEmail

    email = InboundEmail(
        message_id="<m>", thread_id="t", from_email="a@b.com", body="Load 2462934 status?"
    )
    assert email.html_text == ""
    assert _run(ctx, body=email.body, html_text=email.html_text).load_ids == ["2462934"]


@pytest.mark.unit
def test_a_repeated_id_on_one_line_still_binds_its_amount(ctx: ToolContext) -> None:
    """A text-part table row prints the same number under two column headings."""

    out = _run(ctx, body="2502262\t2502262\t07/14/2026\tD&Y USA Inc\t$1,300.00")

    assert out.load_ids == ["2502262"]
    assert [(r.load_id, str(r.amount)) for r in out.stated_rates] == [("2502262", "1300.00")]


# ---------------------------------------------------------------------------
# Payment mail abbreviates its labels. A list that only knows the long form
# knows half of it.
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "PLACE STOP PAYMENT ON CHK 787147",
        "chk 787147",
        "CHK# 787147",
        "CHK No. 787147",
        "Check# 787147",
        "Checks 787147",
        "CK 787147",
        "cheque 787147",
    ],
)
def test_a_check_number_is_not_a_load_however_it_is_abbreviated(
    ctx: ToolContext, text: str
) -> None:
    """Live regression: an RTS stop-payment notice escalated over a check number.

    "PLACE STOP PAYMENT ON CHK 787147" — six digits, so it routed to CargoTel, and the email
    was refused as spanning two systems. Spelled-out "check 787147" had been suppressed since
    the settlement-number case; only the abbreviation was missing.
    """

    assert _run(ctx, body=text).load_ids == []


@pytest.mark.unit
def test_the_rts_stop_payment_email_leaves_only_its_real_load(ctx: ToolContext) -> None:
    """The whole email: a check number, a PO box, a phone number and one load."""

    out = _run(
        ctx,
        subject="2471739 -",
        body=(
            "**PLACE STOP PAYMENT ON CHK 787147 - PAID TO CARRIER ON 7.27, SEE NOA**\n"
            "Confirm all payments will be made to RTS Financial Service "
            "P.O. Box 840267 Dallas, TX 75284-0267.\n"
            "Global Freight LLC 540 2471739 6.29.26 $7600\n"
            "O: (913) 329-9697\n"
        ),
    )

    assert out.load_ids == ["2471739"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Load # 2471739", ["2471739"]),
        ("Load No. 2523916", ["2523916"]),
        ("load 787147", ["787147"]),  # the same number, labelled as a load
        ("INV 2462934", ["2462934"]),  # "inv" is NOT suppressed, same reason as "ref"
        ("Reference#: 2520504", ["2520504"]),
    ],
)
def test_the_check_labels_do_not_swallow_real_loads(
    ctx: ToolContext, text: str, expected: list[str]
) -> None:
    """The widening stays confined to labels that are never a load reference.

    "INV" and "Reference#" are excluded on purpose: carriers write both meaning the load
    itself, so suppressing them would discard real ids — a false negative, worse than an
    escalation. Nobody labels a load "CHK".
    """

    assert _run(ctx, body=text).load_ids == expected
