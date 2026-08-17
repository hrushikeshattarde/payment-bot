"""Unit tests for statement attachment text extraction (mime) and its use in intake."""

from __future__ import annotations

import io
import zipfile
from email.message import EmailMessage

import pytest

from payment_bot.clients.mime import parse_inbound_email
from payment_bot.grounding import GroundingLedger
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import ExtractIdentifiers, ExtractIdentifiersInput

_SHEET_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <sheetData>
  <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
  <row r="2"><c r="A2"><v>2462934</v></c><c r="B2"><v>1150.0</v></c></row>
  <row r="3"><c r="A3"><v>2496603</v></c><c r="B3"><v>850.5</v></c></row>
 </sheetData>
</worksheet>"""

_SHARED_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="2" uniqueCount="2">
 <si><t>Load #</t></si><si><t>Amount</t></si>
</sst>"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="xml" ContentType="application/xml"/>
</Types>"""


def _xlsx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("xl/sharedStrings.xml", _SHARED_XML)
        archive.writestr("xl/worksheets/sheet1.xml", _SHEET_XML)
    return buffer.getvalue()


def _email_with_attachment(payload: bytes, filename: str, mime: tuple[str, str]) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "billing@ideaexpedited.com"
    message["Subject"] = "Statement attached"
    message["Message-ID"] = "<att-test-1>"
    message.set_content("Please see the attached statement.")
    message.add_attachment(payload, maintype=mime[0], subtype=mime[1], filename=filename)
    return message


@pytest.mark.unit
def test_xlsx_attachment_text_is_extracted() -> None:
    parsed = parse_inbound_email(
        _email_with_attachment(
            _xlsx_bytes(),
            "statement.xlsx",
            ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
    )
    sheet = next(a for a in parsed.attachments if a.filename == "statement.xlsx")
    assert "2462934" in sheet.extracted_text
    assert "2496603" in sheet.extracted_text
    assert "Load #" in sheet.extracted_text  # shared strings resolved


@pytest.mark.unit
def test_csv_attachment_text_is_extracted() -> None:
    parsed = parse_inbound_email(
        _email_with_attachment(b"Load,Amount\n2462934,1150.00\n", "loads.csv", ("text", "csv"))
    )
    assert "2462934" in parsed.attachments[0].extracted_text


@pytest.mark.unit
def test_broken_xlsx_yields_empty_text_not_a_crash() -> None:
    parsed = parse_inbound_email(
        _email_with_attachment(
            b"not a zip at all",
            "corrupt.xlsx",
            ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )
    )
    assert parsed.attachments[0].extracted_text == ""


@pytest.mark.unit
def test_an_image_attachment_is_not_extracted() -> None:
    """Images stay out of scope — that is OCR territory, and so is a scanned PDF."""

    parsed = parse_inbound_email(
        _email_with_attachment(b"\x89PNG\r\n\x1a\n fake", "voided-check.png", ("image", "png"))
    )
    assert parsed.attachments[0].extracted_text == ""


# --- PDF statements ---------------------------------------------------------
def _pdf_bytes(*lines: str) -> bytes:
    """A minimal single-page PDF with a real text layer, built by hand.

    Hand-built rather than written with a library so the test exercises the READER against a
    genuine text stream, and does not pass because a writer and reader agree with each other.
    """

    stream = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({line}) Tj 0 -16 Td" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


@pytest.mark.unit
def test_a_pdf_statement_is_extracted() -> None:
    """Live gap, 2026-08-15: a McLeod AR statement from Delta Carrier Group.

    The covering email read "See the attached statement for invoice detail" and carried no
    load id anywhere in its body, so the run escalated with "no valid 6/7-digit load id found"
    while the ids sat in the PDF.
    """

    parsed = parse_inbound_email(
        _email_with_attachment(
            _pdf_bytes("Invoice INV-88213  Load 2534512  1450.00"),
            "statement.pdf",
            ("application", "pdf"),
        )
    )
    assert "2534512" in parsed.attachments[0].extracted_text


@pytest.mark.unit
def test_a_pdf_labelled_octet_stream_is_still_read() -> None:
    """Back offices mislabel statements, and Windows mail sends an uppercase extension."""

    parsed = parse_inbound_email(
        _email_with_attachment(
            _pdf_bytes("Load 2534512"), "Statement_08152026.PDF", ("application", "octet-stream")
        )
    )
    assert "2534512" in parsed.attachments[0].extracted_text


@pytest.mark.unit
def test_a_pdf_statement_reaches_the_extractor() -> None:
    """End to end: the ids in a PDF become load ids, which is the whole point."""

    parsed = parse_inbound_email(
        _email_with_attachment(
            _pdf_bytes("Invoice INV-88213  Load 2534512", "Invoice INV-88240  Load 2535880"),
            "statement.pdf",
            ("application", "pdf"),
        )
    )
    out = ExtractIdentifiers().run(
        ExtractIdentifiersInput(
            subject="Invoices due reminder",
            body="See the attached statement for invoice detail.",
            attachments_text=parsed.attachments[0].extracted_text,
        ),
        ToolContext(tp=None, ledger=GroundingLedger(), correlation_id="pdf-test"),
    )
    assert out.load_ids == ["2534512", "2535880"]


@pytest.mark.unit
def test_a_scanned_pdf_yields_empty_text_not_a_crash() -> None:
    """No text layer, no ids — and nothing downstream can tell this from "no ids in it".

    This is the documented limit of the feature: coverage of PDF statements is partial, and
    partial in a way that looks like an ordinary no-ids-found escalation.
    """

    scanned = _pdf_bytes()  # a page whose content stream draws no text
    parsed = parse_inbound_email(
        _email_with_attachment(scanned, "scan.pdf", ("application", "pdf"))
    )
    assert parsed.attachments[0].extracted_text.strip() == ""


@pytest.mark.unit
def test_a_corrupt_pdf_yields_empty_text_not_a_crash() -> None:
    parsed = parse_inbound_email(
        _email_with_attachment(b"%PDF-1.4 truncated garbage", "broken.pdf", ("application", "pdf"))
    )
    assert parsed.attachments[0].extracted_text == ""


@pytest.mark.unit
def test_a_pdf_is_read_only_for_identifiers_never_the_change_scan() -> None:
    """A statement's remit-to block must not read as a bank-change request.

    The restriction predates PDFs and is what makes reading more formats safe: attachment text
    feeds extract_identifiers alone. A McLeod statement carries a remit-to block as a matter of
    course, so without this every one of them would escalate as a suspected change.
    """

    from payment_bot.models import SensitiveFlag
    from payment_bot.tools.shared import DetectSensitiveChange, DetectSensitiveChangeInput

    parsed = parse_inbound_email(
        _email_with_attachment(
            _pdf_bytes(
                "Remit to: Delta Carrier Group",
                "Please update your records: account 123456789 routing 021000021",
                "Load 2534512  1450.00",
            ),
            "statement.pdf",
            ("application", "pdf"),
        )
    )
    assert "021000021" in parsed.attachments[0].extracted_text  # it IS extracted

    out = DetectSensitiveChange().run(
        DetectSensitiveChangeInput(
            subject=parsed.subject,
            body=parsed.body,
            attachments=[
                {"filename": a.filename, "mime_type": a.mime_type} for a in parsed.attachments
            ],
        ),
        ToolContext(tp=None, ledger=GroundingLedger(), correlation_id="pdf-scan-test"),
    )
    # ...and it does not reach this scan. NONE is the "nothing found" sentinel, so the
    # assertion is that no real flag fired — a BANK_CHANGE here would escalate every statement.
    assert out.flags == [SensitiveFlag.NONE], out.evidence
    assert SensitiveFlag.BANK_CHANGE not in out.flags


@pytest.mark.unit
def test_extract_identifiers_reads_attachment_text(ctx: ToolContext) -> None:
    out = ExtractIdentifiers().run(
        ExtractIdentifiersInput(
            subject="Statement",
            body="Please see attached.",
            attachments_text="Load #\tAmount\n2462934\t1150.0\n2496603\t850.5",
        ),
        ctx,
    )
    assert out.load_ids == ["2462934", "2496603"]


# --- Group From-rewrite (DMARC) recovery ----------------------------------------
def _rewritten_message() -> EmailMessage:
    """A DMARC-rewritten group message, as Google Groups delivers it (observed live)."""

    message = EmailMessage()
    message["From"] = "teamamy via Payment Status <paystatus@circledelivers.com>"
    message["Reply-To"] = "teamamy@otrsolutions.com"
    message["X-Original-Sender"] = "teamamy@otrsolutions.com"
    message["Subject"] = "Rate Verification - Load #2517884"
    message["Message-ID"] = "<group-rewrite-1>"
    message.set_content("Please review the information below.")
    return message


@pytest.mark.unit
def test_group_rewritten_sender_is_recovered() -> None:
    parsed = parse_inbound_email(_rewritten_message(), group_address="paystatus@circledelivers.com")
    assert parsed.from_email == "teamamy@otrsolutions.com"
    assert parsed.from_name == "teamamy"


@pytest.mark.unit
def test_without_group_address_the_from_header_stands() -> None:
    parsed = parse_inbound_email(_rewritten_message())
    assert parsed.from_email == "paystatus@circledelivers.com"


@pytest.mark.unit
def test_a_normal_sender_is_untouched_by_the_group_recovery() -> None:
    message = EmailMessage()
    message["From"] = "Kevin <kevin@samautotrans.com>"
    message["Subject"] = "Payment status"
    message["Message-ID"] = "<normal-1>"
    message.set_content("Status please for 2479097.")
    parsed = parse_inbound_email(message, group_address="paystatus@circledelivers.com")
    assert parsed.from_email == "kevin@samautotrans.com"


@pytest.mark.unit
def test_a_folded_subject_is_unfolded() -> None:
    """A wrapped Subject crashed draft creation live ("Header values may not contain
    linefeed") — observed on an OTR rate verification whose subject folded mid-name.

    Folding only exists in wire format, so the fixture parses raw bytes the way the Gmail
    client does — constructing the header directly would be rejected by the email policy.
    """

    import email as email_module

    raw = (
        b"From: teammaria <teammaria@otrsolutions.com>\r\n"
        b"Subject: Rate Verification - Load #2519161 for Austin Logistics LLC\r\n"
        b" (MC-1366087) - MC#1366087\r\n"
        b"Message-ID: <folded-1>\r\n"
        b"Content-Type: text/plain\r\n\r\nbody\r\n"
    )
    message = email_module.message_from_bytes(raw)

    parsed = parse_inbound_email(message)
    assert "\n" not in parsed.subject
    assert parsed.subject == (
        "Rate Verification - Load #2519161 for Austin Logistics LLC (MC-1366087) - MC#1366087"
    )

    from payment_bot.clients.mime import build_reply

    reply = build_reply(parsed, "Test body", from_address="x@circledelivers.com")
    assert "\n" not in str(reply["Subject"])
