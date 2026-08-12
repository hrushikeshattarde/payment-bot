"""Inbound email model.

A normalised view of a Gmail message as delivered by ``gmail_fetch_new`` (§4.5). The
pipeline only reads these fields; nothing here decides authorization or grounding.
"""

from __future__ import annotations

import html as html_entities
import re

from pydantic import BaseModel, ConfigDict, Field

#: Elements whose *contents* are markup rather than message text.
_NON_TEXT_ELEMENTS_RE = re.compile(r"(?is)<(script|style|head|title)\b.*?</\1\s*>")

#: Elements the sender styled invisible, contents included.
#:
#: Stripping tags discards attribute values, which is what makes it safe to scan an HTML part
#: for load ids — every URL, pixel width and hex colour lives in an attribute. A tracking token
#: placed in element *text* and hidden with CSS defeats that, and it is not a hypothetical:
#: Freshdesk closes every message with
#:
#:     <span title="fd_tkt_identifier" style='font-size:0px; opacity:0; max-height:0px;
#:           line-height:0px; color:#ffffff'>25946:4480806</span>
#:
#: On a Cashway Funding enquiry about CargoTel load 277848, that ``4480806`` was read as a
#: seventh-digit Transport Pro load and the email was refused as spanning both systems. The
#: number is not in the plain-text part and no human ever saw it.
#:
#: Keyed on the hiding declarations rather than on Freshdesk's attribute, because every
#: helpdesk does this — and the same rule removes marketing preheader text, which is also not
#: text a human read. Lazy inner match, so a self-contained hidden element is removed and a
#: nested same-tag one merely under-removes, leaving a stray close tag that tag-stripping eats.
_HIDDEN_ELEMENT_RE = re.compile(
    r"(?is)<(\w+)\b[^>]*?\bstyle\s*=\s*(['\"])"
    r"(?:(?!\2).)*?"
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0"
    r"|font-size\s*:\s*0|max-height\s*:\s*0)"
    r"(?:(?!\2).)*?\2[^>]*>.*?</\1\s*>"
)
_TAG_RE = re.compile(r"(?s)<[^>]+>")
#: Horizontal whitespace only — line structure is left alone, because the stated-rate scan
#: reads one line at a time and pairs an amount with a load id on that same line.
_HSPACE_RE = re.compile(r"[ \t\r\f\v]+")


class EmailAttachment(BaseModel):
    """Attachment metadata, plus extracted text for spreadsheet types.

    ``detect_sensitive_change`` inspects filenames/types (e.g. a voided-check image or
    an NOA PDF) as one signal. ``extracted_text`` is filled only for spreadsheet
    attachments (xlsx/csv) so ``extract_identifiers`` can find the load ids carriers send
    as statements — it feeds identifier extraction ONLY, never the sensitive-change scan:
    statement sheets routinely carry remit-to blocks that would false-positive the
    bank-change patterns, and a change request lives in what the sender *wrote*.
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    mime_type: str | None = None
    size_bytes: int | None = None
    extracted_text: str = ""


class InboundEmail(BaseModel):
    """A single inbound message to the payments inbox."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    thread_id: str
    from_email: str
    from_name: str | None = None
    subject: str = ""
    body: str = ""
    html: str | None = None
    thread_text: str = ""
    attachments: list[EmailAttachment] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)

    @property
    def combined_text(self) -> str:
        """Subject + body + thread, joined — the surface identifier/keyword scans read."""

        return "\n".join(part for part in (self.subject, self.body, self.thread_text) if part)

    @property
    def html_text(self) -> str:
        """The visible text of :attr:`html`, or ``""`` when there is no HTML part.

        A sender's plain-text alternative is not required to say the same thing as their
        HTML, and portal mail routinely proves it. Live on a Summar Financial collections
        email: the text part carried the prose but dropped the invoice table, so the load id
        ``2502262``, the carrier and the amount existed only in the HTML — and the run
        escalated with "no valid 6/7-digit load id found" over an id that was right there.
        ``html`` had been captured since this model was written and read by nothing.

        Tags are removed rather than parsed, which is what makes this safe to feed a scan
        that drives authorization: every URL, pixel width and hex colour lives in an
        *attribute*, so stripping tags discards them. On that Summar mail it reduced 29,363
        characters of markup to 1,621 of text, yielding exactly one load id and no phantoms.
        ``<script>``, ``<style>``, ``<head>`` and ``<title>`` go with their contents, which
        are markup, not message.

        Attribute-stripping alone was not enough, and a live escalation proved it: a tracking
        token can be put in element *text* and hidden with CSS instead, which is what every
        helpdesk does. See :data:`_HIDDEN_ELEMENT_RE` — invisible elements go with their
        contents too, so what survives really is only text a human could have read.

        No BeautifulSoup: it is an optional extra here, and making every inbound email
        depend on it would turn a missing extra into a dead inbox.
        """

        if not self.html:
            return ""
        text = _NON_TEXT_ELEMENTS_RE.sub(" ", self.html)
        text = _HIDDEN_ELEMENT_RE.sub(" ", text)
        text = _TAG_RE.sub(" ", text)
        return _HSPACE_RE.sub(" ", html_entities.unescape(text)).strip()
