"""Inbound email model.

A normalised view of a Gmail message as delivered by ``gmail_fetch_new`` (§4.5). The
pipeline only reads these fields; nothing here decides authorization or grounding.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EmailAttachment(BaseModel):
    """Attachment metadata only — content is not fetched by the intake step.

    ``detect_sensitive_change`` inspects filenames/types (e.g. a voided-check image or
    an NOA PDF) as one signal, so metadata is enough for the safety checks.
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    mime_type: str | None = None
    size_bytes: int | None = None


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
