"""MIME parsing and reply building for the Gmail backend.

The Gmail API deals in RFC822 bytes — returned base64url-encoded from
``messages.get?format=RAW`` — so the parsing and reply construction live here rather than
inline in the client.

Parsing is deliberately lenient: a carrier's mail client is not our problem, and a missing
or oddly-encoded header must never crash a run. Anything we cannot read becomes empty, and
the deterministic intake checks then decide what to do about it.
"""

from __future__ import annotations

import re
from email.header import decode_header, make_header
from email.message import EmailMessage as MimeMessage
from email.message import Message as EmailMessage
from email.utils import formatdate, parseaddr

from payment_bot.models import EmailAttachment, InboundEmail

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


def reply_subject(subject: str) -> str:
    """``Re:``-prefix a subject without stacking a second one."""

    cleaned = subject.strip()
    if not cleaned:
        return "Re: (no subject)"
    return cleaned if cleaned.lower().startswith("re:") else f"Re: {cleaned}"


_FOLDED_HEADER_RE = re.compile(r"\s*[\r\n]+\s*")


def decode_header_value(value: str | None) -> str:
    """Decode RFC 2047 encoded-words (``=?utf-8?B?…?=``) into plain text, unfolded.

    Long headers arrive folded across lines (RFC 5322 §2.2.3) and Python hands the
    newlines through. They must not survive: a folded Subject fed back into a reply
    crashed draft creation with "Header values may not contain linefeed" — observed live
    on an OTR Solutions rate verification whose subject wrapped after the carrier name.
    """

    if not value:
        return ""
    try:
        decoded = str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        decoded = value
    return _FOLDED_HEADER_RE.sub(" ", decoded).strip()


def body_text(message: EmailMessage) -> tuple[str, str | None]:
    """Return ``(plain_text, html)``, preferring a real ``text/plain`` part."""

    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():  # an attachment, not body text
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes | bytearray):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = bytes(payload).decode(charset, errors="replace")
        except LookupError:
            text = bytes(payload).decode("utf-8", errors="replace")
        (plain_parts if content_type == "text/plain" else html_parts).append(text)

    html = "\n".join(html_parts) or None
    if plain_parts:
        return "\n".join(plain_parts).strip(), html
    if html:
        # No text/plain alternative: strip tags so identifiers and amounts still scan.
        stripped = _HTML_TAG_RE.sub(" ", html)
        return _WS_RE.sub("\n", stripped).strip(), html
    return "", None


#: Cap on text pulled from one attachment. A statement never needs more; a pathological
#: file must not balloon the intake prompt or the audit trail.
_MAX_ATTACHMENT_TEXT = 100_000

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_text(payload: bytes) -> str:
    """Cell values of every worksheet, tab-separated per row. Stdlib only, never raises.

    Carriers and factors send load lists as spreadsheets; the load ids live here and
    nowhere in the body. Formulas are skipped (cached values are read), shared strings
    resolved. Anything unreadable yields "" — lenient, like the rest of this module.
    """

    import contextlib
    import io
    import zipfile
    from xml.etree import ElementTree

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = [
                    "".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t"))
                    for si in root.iter(f"{_XLSX_NS}si")
                ]
            lines: list[str] = []
            sheets = sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/sheet"))
            for name in sheets:
                sheet = ElementTree.fromstring(archive.read(name))
                for row in sheet.iter(f"{_XLSX_NS}row"):
                    cells: list[str] = []
                    for cell in row.iter(f"{_XLSX_NS}c"):
                        value = cell.find(f"{_XLSX_NS}v")
                        text = value.text if value is not None and value.text else ""
                        if cell.get("t") == "s" and text:
                            with contextlib.suppress(ValueError, IndexError):
                                text = shared[int(text)]
                        cells.append(text)
                    if any(cells):
                        lines.append("\t".join(cells))
                    if sum(len(line) for line in lines) > _MAX_ATTACHMENT_TEXT:
                        return "\n".join(lines)[:_MAX_ATTACHMENT_TEXT]
            return "\n".join(lines)
    except Exception:
        return ""


def _attachment_text(content_type: str, filename: str, payload: bytes) -> str:
    """Extract searchable text from spreadsheet attachments; "" for everything else.

    Only spreadsheet types: they are where statement load ids live, they parse with the
    stdlib, and their content is data rather than prose. PDFs and images are out of
    scope — no parser dependency, and OCR territory.
    """

    lower_name = filename.lower()
    if content_type == _XLSX_MIME or lower_name.endswith(".xlsx"):
        return _xlsx_text(payload)
    if content_type in {"text/csv", "application/csv"} or lower_name.endswith(".csv"):
        try:
            return payload.decode("utf-8", errors="replace")[:_MAX_ATTACHMENT_TEXT]
        except Exception:
            return ""
    return ""


def attachments(message: EmailMessage) -> list[EmailAttachment]:
    """Attachment metadata, plus extracted text for spreadsheet types (§4.2)."""

    out: list[EmailAttachment] = []
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        size = len(payload) if isinstance(payload, bytes | bytearray) else None
        decoded_name = decode_header_value(filename)
        content_type = part.get_content_type()
        extracted = (
            _attachment_text(content_type, decoded_name, bytes(payload))
            if isinstance(payload, bytes | bytearray)
            else ""
        )
        out.append(
            EmailAttachment(
                filename=decoded_name,
                mime_type=content_type,
                size_bytes=size,
                extracted_text=extracted,
            )
        )
    return out


def parse_inbound_email(
    message: EmailMessage,
    *,
    thread_id: str | None = None,
    labels: list[str] | None = None,
    group_address: str | None = None,
) -> InboundEmail:
    """Normalise a parsed MIME message into our :class:`InboundEmail`.

    Args:
        message: The parsed message.
        thread_id: The backend's own thread identifier, when it has one. The Gmail API
            supplies a real ``threadId``; without one we fall back to the root of the
            ``References`` chain.
        labels: Backend labels, if available.
        group_address: The monitored group's own address. When From equals it, the real
            sender was rewritten away by Google Groups (DMARC) and is recovered from the
            headers the rewrite leaves behind.
    """

    message_id = decode_header_value(message.get("Message-ID")) or decode_header_value(
        message.get("Message-Id")
    )
    from_name, from_email = parseaddr(decode_header_value(message.get("From")))

    # Google Groups rewrites DMARC-strict senders: "teamamy via Payment Status
    # <paystatus@…>". Authorization must judge the *real* sender — otherwise the group's
    # own address is what gets checked, and it matches nothing. The rewrite preserves the
    # original in X-Original-Sender / X-Original-From / Reply-To (verified live on an OTR
    # Solutions message carrying all three).
    if group_address and from_email.lower() == group_address.strip().lower():
        for header in ("X-Original-Sender", "X-Original-From", "Reply-To"):
            original_name, original_email = parseaddr(decode_header_value(message.get(header)))
            if original_email and original_email.lower() != group_address.strip().lower():
                from_email = original_email
                # "teamamy via Payment Status" → keep the sender part of the display name.
                from_name = original_name or (from_name.split(" via ")[0] if from_name else "")
                break

    if thread_id is None:
        references = decode_header_value(message.get("References")).split()
        in_reply_to = decode_header_value(message.get("In-Reply-To"))
        thread_id = references[0] if references else (in_reply_to or message_id)

    body, html = body_text(message)
    return InboundEmail(
        message_id=message_id or f"mime-{abs(hash(body))}",
        thread_id=thread_id,
        from_email=from_email,
        from_name=from_name or None,
        subject=decode_header_value(message.get("Subject")),
        body=body,
        html=html,
        thread_text="",
        attachments=attachments(message),
        labels=labels or [],
    )


def build_reply(
    source: InboundEmail,
    body: str,
    *,
    from_address: str,
    cc: tuple[str, ...] = (),
    subject: str | None = None,
) -> MimeMessage:
    """Build the reply message that becomes a draft.

    ``In-Reply-To`` / ``References`` are set so Gmail threads the draft under the carrier's
    original message instead of starting a new conversation.
    """

    mime = MimeMessage()
    mime["From"] = from_address
    mime["To"] = source.from_email
    if cc:
        mime["Cc"] = ", ".join(cc)
    mime["Subject"] = subject or reply_subject(source.subject)
    mime["Date"] = formatdate(localtime=True)
    if source.message_id:
        mime["In-Reply-To"] = source.message_id
        references = [source.thread_id] if source.thread_id else []
        if source.message_id not in references:
            references.append(source.message_id)
        mime["References"] = " ".join(dict.fromkeys(references))
    mime.set_content(body)
    return mime
