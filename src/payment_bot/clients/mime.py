"""MIME parsing and reply building, shared by every Gmail backend.

Both Gmail backends deal in RFC822 bytes — IMAP hands them over from ``FETCH``, and the
Gmail API returns them base64url-encoded from ``messages.get?format=RAW`` — so the parsing
and reply construction live here rather than being duplicated (or, worse, diverging)
between the two.

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


def decode_header_value(value: str | None) -> str:
    """Decode RFC 2047 encoded-words (``=?utf-8?B?…?=``) into plain text."""

    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return value.strip()


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


def attachments(message: EmailMessage) -> list[EmailAttachment]:
    """Attachment **metadata** only — enough for ``detect_sensitive_change`` (§4.2)."""

    out: list[EmailAttachment] = []
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        size = len(payload) if isinstance(payload, bytes | bytearray) else None
        out.append(
            EmailAttachment(
                filename=decode_header_value(filename),
                mime_type=part.get_content_type(),
                size_bytes=size,
            )
        )
    return out


def parse_inbound_email(
    message: EmailMessage,
    *,
    thread_id: str | None = None,
    labels: list[str] | None = None,
) -> InboundEmail:
    """Normalise a parsed MIME message into our :class:`InboundEmail`.

    Args:
        message: The parsed message.
        thread_id: The backend's own thread identifier, when it has one. The Gmail API
            supplies a real ``threadId``; plain IMAP does not, so we fall back to the root
            of the ``References`` chain.
        labels: Backend labels, if available.
    """

    message_id = decode_header_value(message.get("Message-ID")) or decode_header_value(
        message.get("Message-Id")
    )
    from_name, from_email = parseaddr(decode_header_value(message.get("From")))

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
