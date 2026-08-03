"""The terminal ``submit_draft`` tool.

The agent has no send capability. When it has gathered and grounded its facts, it calls
``submit_draft`` with the composed reply and the citations backing it. This tool is
*terminal*: the loop stops and hands the draft to the pipeline, which runs the pre-send
gate and (Phase 1) posts to Slack for approval before anything is sent.

Declaring citations is not itself the grounding check — the gate independently verifies
the draft against the ledger — but it forces the model to attribute its facts and gives
reviewers an at-a-glance provenance trail.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from payment_bot.tools.base import Tool, ToolContext

#: Every tool name in the registry, spelled out statically so this module imports nothing
#: heavy. A unit test asserts this stays equal to ``build_default_registry``'s names.
TOOL_NAMES: frozenset[str] = frozenset(
    {
        "classify_intent", "extract_identifiers", "route_load", "detect_sensitive_change",
        "check_authorization", "carrier_cross_check", "compute_scheduled_pay_date",
        "compute_carrier_rate", "tp_get_load_summary", "tp_get_dispatch_history",
        "tp_get_settlement_entries", "tp_get_file_history", "tp_get_noa_factoring",
        "submit_draft",
    }
)  # fmt: skip

_NAMES_ALTERNATION = "|".join(sorted(TOOL_NAMES))
#: A tool name in the reply text, with or without bracket/paren wrapping. Observed live:
#: "a payment of $900 [tp_get_load_summary] scheduled for Thursday, August 13, 2026
#: [compute_scheduled_pay_date]" reached a carrier-facing draft — the model pasted its
#: citation markers into the prose despite the prompt forbidding it.
_TOOL_MENTION_RE = re.compile(
    rf"\s*[\[\(]\s*(?:{_NAMES_ALTERNATION})\s*[\]\)]|\s*\b(?:{_NAMES_ALTERNATION})\b"
)


def strip_tool_mentions(text: str) -> str:
    """Remove tool-name markers from a reply body, mechanically.

    Deterministic and meaning-preserving: tool names never appear legitimately in a reply
    to a carrier, so deleting them (and tidying the spacing left behind) cannot change
    what the draft says. Prompt rules alone did not hold — this makes the cleanup code.
    """

    cleaned = _TOOL_MENTION_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([,.;:!?])", r"\1", cleaned)
    return cleaned


class Citation(BaseModel):
    """One fact the draft relies on, attributed to the tool that produced it."""

    fact: str
    value: str
    source_tool: str


class SubmitDraftInput(BaseModel):
    reply_body: str = Field(
        description=(
            "The finished reply to the carrier, plain text, ready to send. Two to four "
            "sentences. Never a template or a placeholder — every figure filled in."
        )
    )
    to: str = Field(description="The recipient's email address — the sender you are replying to.")
    load_ids: list[str] = Field(
        default_factory=list,
        description="Every load id the reply discloses information about. Digits only.",
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description=(
            "One entry per amount and date stated in the reply, naming the tool that produced "
            "it. The gate independently re-checks these against the tool results."
        ),
    )


class SubmitDraftOutput(BaseModel):
    ok: bool = True
    reply_body: str
    to: str
    load_ids: list[str]
    citations: list[Citation]


class SubmitDraft(Tool):
    """Submit the final drafted reply. Terminal — ends the agent loop."""

    name = "submit_draft"
    description = (
        "Submit the final reply once all facts are gathered and grounded. Provide the "
        "reply body, the recipient, the load ids covered, and a citation for every amount "
        "and date used. Calling this ends your turn; a human reviews before it is sent."
    )
    input_model = SubmitDraftInput
    is_terminal = True

    def run(self, params: BaseModel, ctx: ToolContext) -> SubmitDraftOutput:
        assert isinstance(params, SubmitDraftInput)
        # Tool-name markers are stripped mechanically — citations belong in the citations
        # field, and prompt rules alone did not keep them out of the prose.
        body = strip_tool_mentions(params.reply_body).rstrip()
        # The sign-off is likewise enforced here, not hoped for: three live drafts in one
        # day omitted it despite the intake instruction. Appended only when the configured
        # signature is not already present near the end, so a compliant draft is untouched.
        signature = ctx.settings.reply_signature.strip()
        if signature and signature.lower() not in body[-200:].lower():
            body = f"{body}\n\n{signature}"
        return SubmitDraftOutput(
            reply_body=body,
            to=params.to,
            load_ids=params.load_ids,
            citations=params.citations,
        )
