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
