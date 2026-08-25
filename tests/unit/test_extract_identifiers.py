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


# ---------------------------------------------------------------------------
# Hidden tracking text. Stripping tags discards attribute values, but a token
# put in element TEXT and hidden with CSS survives that — and every helpdesk
# does exactly this.
# ---------------------------------------------------------------------------
_FRESHDESK_TAIL = (
    "<div><p>Load 277848 — could you provide payment status?</p></div>"
    "<span title=\"fd_tkt_identifier\" style='font-size:0px; font-family:\"fdtktid\"; "
    "min-height:0px; height:0px; opacity:0; max-height:0px; line-height:0px; "
    "color:#ffffff'>25946:4480806</span>"
)


@pytest.mark.unit
def test_a_hidden_tracking_id_is_not_read_as_a_load(ctx: ToolContext) -> None:
    """Live regression, and one my own html_text change introduced.

    Freshdesk closes every message with an invisible span carrying `<ticket>:<id>`. On a
    Cashway Funding enquiry about CargoTel load 277848 the trailing 4480806 was read as a
    7-digit Transport Pro load, and the email was refused as spanning both systems. The number
    is in no plain-text part and no human ever saw it.
    """

    from payment_bot.models import InboundEmail

    email = InboundEmail(
        message_id="<m>",
        thread_id="t",
        from_email="support@cashwayfunding.com",
        subject="Re: 277848",
        body="Load 277848\nCould you please provide payment status?",
        html=f"<html><body>{_FRESHDESK_TAIL}</body></html>",
    )
    out = _run(ctx, subject=email.subject, body=email.body, html_text=email.html_text)

    assert out.load_ids == ["277848"]
    assert "4480806" not in email.html_text


@pytest.mark.unit
@pytest.mark.parametrize(
    "style",
    [
        "display:none",
        "display: none",
        "visibility:hidden",
        "opacity:0",
        "font-size:0px",
        "max-height:0px",
    ],
)
def test_every_hiding_declaration_removes_the_element_and_its_text(
    ctx: ToolContext, style: str
) -> None:
    """Keyed on the CSS, not on Freshdesk's attribute — every helpdesk hides its own way.

    The same rule removes marketing preheader text, which is equally not text a human read.
    """

    from payment_bot.models import InboundEmail

    email = InboundEmail(
        message_id="<m>",
        thread_id="t",
        from_email="a@b.com",
        html=f"<html><body><p>Load 2462934</p><span style='{style}'>9876543</span></body></html>",
    )
    out = _run(ctx, html_text=email.html_text)

    assert out.load_ids == ["2462934"]


@pytest.mark.unit
def test_visible_text_in_a_styled_element_is_still_read(ctx: ToolContext) -> None:
    """The rule must not swallow ordinary styling — most real tables carry a style attribute."""

    from payment_bot.models import InboundEmail

    email = InboundEmail(
        message_id="<m>",
        thread_id="t",
        from_email="a@b.com",
        html=(
            "<html><body><table style='width:600px; font-size:13px; color:#333333'>"
            "<tr><td style='padding:4px'>2502262</td><td>$1,300.00</td></tr>"
            "</table></body></html>"
        ),
    )
    out = _run(ctx, html_text=email.html_text)

    assert out.load_ids == ["2502262"]


# ---------------------------------------------------------------------------
# When ids disagree about system, prefer the one the sender CALLED a load.
#
# Positive evidence, because the negative kind ran out: the competing label is a
# company abbreviation, and no fixed list can hold every carrier's and factor's.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_a_company_reference_does_not_fake_a_second_system(ctx: ToolContext) -> None:
    """Live regression, the third of this shape.

    A VIP Logistics enquiry read "Load #2513318 / VIP #282775-0-A". The second is the sender's
    own reference; six digits routed it to CargoTel and the email was refused as spanning both
    systems. Neither existing guard reaches it — "VIP" is in no label list, and nothing
    captured the number as an invoice.
    """

    out = _run(
        ctx,
        subject="Circle Logistics Statement",
        body="Please advise on the payment date for the below load:\n\nLoad #2513318 / VIP #282775-0-A\n",
    )

    assert out.load_ids == ["2513318"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # No load label anywhere: nothing to prefer, so the escalation stands.
        ("2513318 / VIP #282775-0-A", ["2513318", "282775"]),
        # Neither id labelled: a genuine two-system email a human must see.
        ("Load 2485194 and load 318354 please", ["2485194", "318354"]),
        # Both labelled, in different systems: also a human's call, not ours.
        ("Load 2485194 and load #318354", ["2485194", "318354"]),
        # One system only: the rule cannot fire at all.
        ("Load #2513318 / VIP #2513319-0-A", ["2513318", "2513319"]),
    ],
)
def test_the_preference_only_fires_on_a_resolvable_disagreement(
    ctx: ToolContext, body: str, expected: list[str]
) -> None:
    assert _run(ctx, body=body).load_ids == expected


@pytest.mark.unit
def test_reference_counts_as_a_load_label_not_a_sender_reference(ctx: ToolContext) -> None:
    """Factoring templates write the load itself as "Reference#: 2520504".

    That is why `ref` has always been excluded from `_NOT_A_LOAD_LABEL_RE`, and it has to mean
    the same thing here or the rule would drop the very id it should keep.
    """

    assert _run(ctx, body="Reference#: 2520504 and VIP #282775").load_ids == ["2520504"]


@pytest.mark.unit
def test_a_hyphenated_load_reference_is_still_read(ctx: ToolContext) -> None:
    """Suppressing the SUFFIX was the other candidate fix, and it is wrong.

    CargoTel's own A/P invoice number is `<load id>-<carrier invoice>`, so a load id followed
    by a hyphen and more is ordinary rather than suspicious.
    """

    assert _run(ctx, body="load 296006-INVDKD0098").load_ids == ["296006"]


# ---------------------------------------------------------------------------
# A WEX collections table. Four ids in one row, one real load.
#
#   Carrier | Mot Car | Account | Mot Car | Invoice | Load | Age | Balance
#   FFS Brothers LLC | 1601899 | CIRCLE LOGISTICS, INC (IN) (7 DIGIT LOAD#S)
#     (FREIGHTPAY@…) | 761291 | IN-001208 | 2481841 | 45 | $150.00
#
# The row reaches us only through the HTML part, flattened to one line, so the
# column headers end up 40-odd characters from the values beneath them — well
# outside the 24-character label window, and widening that would start attaching
# whatever precedes a number two cells later.
# ---------------------------------------------------------------------------
WEX_SUBJECT = "Payment Inquiry CIRCLE LOGISTICS, INC (IN) (7 DIGIT LOAD#S)"
WEX_HEADERS = "Carrier Mot Car Account Mot Car Invoice Load Age Balance "
WEX_ROW = (
    "FFS Brothers LLC 1601899 CIRCLE LOGISTICS, INC (IN) (7 DIGIT LOAD#S) "
    "(FREIGHTPAY@CIRCLEDELIVERS.COM) 761291 IN-001208 2481841 45 $150.00"
)


@pytest.mark.unit
def test_the_wex_collections_table_no_longer_spans_both_systems(ctx: ToolContext) -> None:
    """Live escalation: 'email spans both systems' on a single-system enquiry.

    Two independent guards do the work. `IN-001208` is dropped as zero-padded, and `761291`
    — the account's Motor Carrier number — is dropped because the sender states our load ids
    are seven digits, in the subject and again in the table.
    """

    out = _run(ctx, subject=WEX_SUBJECT, body=WEX_HEADERS, html_text=WEX_HEADERS + WEX_ROW)

    assert out.load_ids == ["1601899", "2481841"]
    assert "001208" not in out.load_ids
    assert "761291" not in out.load_ids


@pytest.mark.unit
def test_a_zero_padded_reference_is_not_a_load(ctx: ToolContext) -> None:
    """`IN-001208` is an invoice number formatted to a fixed width.

    A load id is a sequence number and never carries a leading zero, which is what makes a
    flat rule safe here. Deliberately not a rule about the `IN-` prefix: suppressing digits
    after any letter-hyphen would also drop `INV-2462934`, and `inv` has to keep meaning the
    load itself.
    """

    assert _run(ctx, body="Invoice IN-001208 for load 2481841").load_ids == ["2481841"]
    assert _run(ctx, body="ref 0012345").load_ids == []
    # The shapes that must survive it.
    assert _run(ctx, body="INV 2462934").load_ids == ["2462934"]
    assert _run(ctx, body="load 296006").load_ids == ["296006"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        # Fires: two systems, and the sender says which length ours are.
        ("(7 DIGIT LOAD#S)", "1601899 and 761291 and 2481841", ["1601899", "2481841"]),
        ("(6 DIGIT LOAD#S)", "296006 and 2481841", ["296006"]),
        # Silent: one system, so there is nothing to resolve.
        ("(7 DIGIT LOAD#S)", "2462934 and 2481841", ["2462934", "2481841"]),
        # Silent: no declaration at all — a genuine two-system email must still escalate.
        ("Payment inquiry", "296006 and 2481841", ["296006", "2481841"]),
        # Silent: the email contradicts itself, so it says nothing usable.
        ("6 digit loads and 7 digit loads", "296006 and 2481841", ["296006", "2481841"]),
        # Silent: "7 digit" that is not about a load is a coincidence, not a declaration.
        ("7 digit account numbers", "296006 and 2481841", ["296006", "2481841"]),
        # Silent: the declaration would leave nothing, so it cannot be what was meant.
        ("(6 DIGIT LOAD#S)", "2462934 and 2481841", ["2462934", "2481841"]),
    ],
)
def test_a_declared_id_length_only_breaks_a_real_tie(
    ctx: ToolContext, subject: str, body: str, expected: list[str]
) -> None:
    """Same safety envelope as the labelled-load preference it sits beside.

    It can only ever filter, never introduce an id, and it cannot fire on a single-system
    email or without an explicit statement about *loads*.
    """

    assert _run(ctx, subject=subject, body=body).load_ids == expected


@pytest.mark.unit
def test_a_named_load_is_never_dropped_for_being_the_wrong_length(ctx: ToolContext) -> None:
    """An id the sender CALLED a load outranks a length declared in an account name.

    The two can genuinely disagree, and when they do the specific statement about that
    number beats the general one about the account. Protecting the labelled id leaves the
    disagreement intact here, so nothing is resolved and the email still reaches a human —
    which is the right outcome for a contradiction, not a bug in the tie-break.
    """

    out = _run(ctx, subject="(7 DIGIT LOAD#S)", body="Load #296006 and reference 2481841")

    assert out.load_ids == ["296006", "2481841"]


@pytest.mark.unit
def test_a_declared_length_still_clears_the_unlabelled_noise_around_a_named_load(
    ctx: ToolContext,
) -> None:
    """The two guards compose: the label protects one id, the length drops the strays."""

    out = _run(
        ctx,
        subject="(7 DIGIT LOAD#S)",
        body="Mot Car 761291 Invoice IN-001208 Load #2481841",
    )

    assert out.load_ids == ["2481841"]


# ---------------------------------------------------------------------------
# An invoice table writes money without a currency symbol, and a signature
# block writes a currency symbol without meaning money.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_a_table_amount_with_no_currency_symbol_is_read(ctx: ToolContext) -> None:
    """Porter Billing's collections table prints ``2,800.00`` under Amount, no ``$``.

    The ``$``-anchored scanner could not see it, so the only figure the email was about
    never became a stated rate.
    """

    out = _run(ctx, body="102711 | JBHS TRUCKING INC | 2412641 | 2,800.00 | 93 | 2,800.00")

    assert [str(r.amount) for r in out.stated_rates] == ["2800.00", "2800.00"]


@pytest.mark.unit
def test_a_labelled_amount_needs_no_load_id_beside_it(ctx: ToolContext) -> None:
    out = _run(ctx, body="Balance due: 2,800.00")

    assert [str(r.amount) for r in out.stated_rates] == ["2800.00"]
    assert out.stated_rates[0].load_id is None


@pytest.mark.unit
def test_a_promotional_banner_is_not_a_stated_amount(ctx: ToolContext) -> None:
    """The bug this guard exists for.

    "EARN $300 For Every Funded Referral" sits under the factor's signature on every mail
    they send. It was the only ``$``-prefixed number in a $2,800 enquiry, so it became "the
    amount the sender stated" and the reply asked a collections rep to clarify a figure they
    had never written.
    """

    out = _run(
        ctx,
        body=(
            "Please provide payment status on Id # 2412641.\n"
            "Porter Freight Funding 15 Years\n"
            "EARN $300 For Every Funded Referral\n"
            "SUBMIT A REFERRAL\n"
        ),
    )

    assert out.load_ids == ["2412641"]
    assert out.stated_rates == []


@pytest.mark.unit
def test_the_banner_and_the_table_in_one_email(ctx: ToolContext) -> None:
    """Both halves of the live Porter mail: the real amount survives, the banner does not."""

    out = _run(
        ctx,
        body=(
            "Please provide payment status on Id # 2412641. This is over 90 days due.\n"
            "Invoice 102711 | 2412641 | Amount 2,800.00 | Balance 2,800.00\n"
            "EARN $300 For Every Funded Referral\n"
        ),
    )

    assert Decimal("300") not in {r.amount for r in out.stated_rates}
    assert {r.amount for r in out.stated_rates} == {Decimal("2800.00")}


@pytest.mark.unit
def test_an_unlabelled_amount_alone_on_a_line_is_dropped(ctx: ToolContext) -> None:
    """An HTML table puts each cell on its own line, so the amount cell has no neighbours.

    Previously this survived as an unattributed rate with nothing vouching for it, which is
    the shape the banner arrived in. The rate skill's own fallback covers the loss: with no
    stated amount it tells the agent to state ours rather than compare.
    """

    out = _run(ctx, body="Load 2412641\n2,800.00\n93\n")

    assert out.load_ids == ["2412641"]
    assert out.stated_rates == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "Invoice dated 07.14.2026 for load 2412641",
        "Load 2412641 delivered 07.14.2026",
    ],
)
def test_a_dotted_date_is_not_money(ctx: ToolContext, line: str) -> None:
    """``07.14.2026`` contains ``07.14``, which the widened bare-fraction branch would take."""

    out = _run(ctx, body=line)

    assert out.stated_rates == []


@pytest.mark.unit
def test_what_intake_refuses_is_not_quotable_either(ctx: ToolContext) -> None:
    """The narrowing has to hold on both sides.

    What intake declines to call a stated amount must not reach the ledger, or the pre-send
    gate goes on letting the number through.
    """

    out = _run(ctx, body="Load 2412641 rate 2,800.00\nEARN $300 For Every Funded Referral\n")

    assert [str(r.amount) for r in out.stated_rates] == ["2800.00"]
    assert Decimal("300") not in ctx.ledger.sender_stated_amounts
    assert Decimal("300") not in ctx.ledger.grounded_amounts


@pytest.mark.unit
def test_a_senders_amount_is_recorded_as_theirs_not_as_a_fact(ctx: ToolContext) -> None:
    """Intake reads numbers off an email. It confirms none of them.

    Recording them as grounded facts is what let a figure lifted from the sender's own
    message be written into a reply as the rate on file.
    """

    _run(ctx, body="Load 2412641 rate 2,800.00")

    assert ctx.ledger.sender_stated_amounts == {Decimal("2800.00")}
    assert ctx.ledger.grounded_amounts == set()
    assert [f.kind for f in ctx.ledger.facts] == ["sender_amount"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "EARN $300 For Every Funded Referral — get paid on your invoice faster",
        "Refer a carrier and earn $500 — terms and conditions apply",
        "Unsubscribe from payment reminders — $0 to opt out",
    ],
)
def test_marketing_copy_using_payment_words_is_still_not_a_stated_amount(
    ctx: ToolContext, line: str
) -> None:
    """The label guard alone lets a banner through the moment its copy says "invoice"."""

    out = _run(ctx, body=f"Payment status on load 2412641 please.\n{line}\n")

    assert out.load_ids == ["2412641"]
    assert out.stated_rates == []


@pytest.mark.unit
def test_an_amount_naming_no_load_is_dropped_when_the_email_names_several(
    ctx: ToolContext,
) -> None:
    """It belongs to one of them and nothing says which."""

    out = _run(ctx, body="Loads 2412641 and 2499505.\nTotal balance due 5,300.00\n")

    assert out.load_ids == ["2412641", "2499505"]
    assert out.stated_rates == []
    assert ctx.ledger.sender_stated_amounts == set()


@pytest.mark.unit
def test_the_same_amount_is_kept_when_the_email_names_one_load(ctx: ToolContext) -> None:
    """One load makes an unbound amount unambiguous, which is the ordinary rate dispute."""

    out = _run(ctx, body="Load 2412641.\nWe show the rate as 2,850.00\n")

    assert [str(r.amount) for r in out.stated_rates] == ["2850.00"]
    assert out.stated_rates[0].load_id is None


# ---------------------------------------------------------------------------
# The total is the one unattributed amount a multi-load email may state.
# ---------------------------------------------------------------------------
_LOVES_BODY = (
    "I am reaching out to verify the rate for loads#\n"
    "2542489 -$275.00\n"
    "2542480 -$275.00\n"
    "2542484 -$275.00\n"
    "2542493 -$275.00\n"
    "2542482 - $275.00\n"
    "for Chuck And Jackie Trucking LLC\n"
    "Will the full amount of $1375 be paid to Loves? If not, please list any advances, "
    "deductions, or issues.\n"
)


@pytest.mark.unit
def test_a_total_over_the_listed_loads_is_kept(ctx: ToolContext) -> None:
    """Live block: five loads at $275.00 and one question about "$1375".

    The total names no load because it is about all of them. Dropping it as ambiguous left
    the draft that answered the question stating an amount nothing had recorded, and the
    grounding check blocked the send.
    """

    out = _run(ctx, body=_LOVES_BODY)

    assert len(out.load_ids) == 5
    assert (None, "1375") in [(r.load_id, str(r.amount)) for r in out.stated_rates]
    assert Decimal("1375") in ctx.ledger.sender_stated_amounts


@pytest.mark.unit
def test_an_unattributed_amount_that_is_not_the_total_is_still_dropped(
    ctx: ToolContext,
) -> None:
    """The ambiguous case the guard was written for is unchanged."""

    body = _LOVES_BODY.replace("$1375", "$500")
    out = _run(ctx, body=body)

    assert Decimal("500") not in {r.amount for r in out.stated_rates}
    assert Decimal("500") not in ctx.ledger.sender_stated_amounts
    assert {r.amount for r in out.stated_rates} == {Decimal("275.00")}


@pytest.mark.unit
def test_a_row_printed_twice_does_not_double_the_total(ctx: ToolContext) -> None:
    """An invoice table prints the same row under two column headings.

    Summing the repeats would put the total at 1,100 and the sender's real 550 would then
    look like the ambiguous case.
    """

    out = _run(
        ctx,
        body=(
            "2542489 | 2542489 | $275.00\n"
            "2542480 | 2542480 | $275.00\n"
            "Total due: $550.00\n"
        ),
    )

    assert (None, "550.00") in [(r.load_id, str(r.amount)) for r in out.stated_rates]


@pytest.mark.unit
def test_a_load_the_sender_also_called_an_invoice_is_not_dropped(ctx: ToolContext) -> None:
    """Live on a Factoring Express follow-up, 2026-08-25.

    They number each invoice after the PO, so one number arrived as "Invoice # 2534728" AND
    "PO # 2534728", with a six-digit "ID 187351" beside it. The invoice-drop guard took the
    ID as its anchor and removed 2534728 as the stray -- so the load that appeared four times
    in the email lost to the number that appeared once, and 187351 was looked up in CargoTel
    and denied.

    A positive load/PO label outranks the invoice-drop. It has to be applied inside that
    guard: `_prefer_labelled_loads_across_systems` runs afterwards and can only protect an id
    that is still there.
    """

    out = _run(
        ctx,
        subject="Follow-Up on Invoice Submission 2534728 (NATURAL EXPRESS LLC)",
        body=(
            "invoice sent 5 days ago for 2534728\nID 187351\n"
            "Invoice #\t2534728\nPO #\t2534728\nAmount\t1,600.00"
        ),
    )

    assert out.load_ids == ["2534728"]
    assert "187351" not in out.load_ids


@pytest.mark.unit
def test_po_box_is_still_not_a_load_label(ctx: ToolContext) -> None:
    """`po` joined the load labels, and a remit footer is where that could go wrong."""

    from payment_bot.tools.shared import _LOAD_LABEL_RE

    assert _LOAD_LABEL_RE.findall("Check Payments: PO Box 840267 Nashville TN") == []
    assert _LOAD_LABEL_RE.findall("PO # 2534728") == ["2534728"]
