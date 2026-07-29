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

from pydantic import BaseModel, Field

from payment_bot.tools.base import Tool, ToolContext


class Citation(BaseModel):
    """One fact the draft relies on, attributed to the tool that produced it."""

    fact: str
    value: str
    source_tool: str


class SubmitDraftInput(BaseModel):
    reply_body: str
    to: str
    load_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


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
        return SubmitDraftOutput(
            reply_body=params.reply_body,
            to=params.to,
            load_ids=params.load_ids,
            citations=params.citations,
        )
