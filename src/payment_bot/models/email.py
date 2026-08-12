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
        that drives authorization: every URL, tracking id, pixel width and hex colour lives
        in an *attribute*, so stripping tags discards them and only text a human would have
        read survives. On that Summar mail it reduced 29,363 characters of markup to 1,621 of
        text, yielding exactly one load id and no phantoms. ``<script>``, ``<style>``,
        ``<head>`` and ``<title>`` go with their contents, which are markup, not message.

        No BeautifulSoup: it is an optional extra here, and making every inbound email
        depend on it would turn a missing extra into a dead inbox.
        """

        if not self.html:
            return ""
        text = _NON_TEXT_ELEMENTS_RE.sub(" ", self.html)
        text = _TAG_RE.sub(" ", text)
        return _HSPACE_RE.sub(" ", html_entities.unescape(text)).strip()
