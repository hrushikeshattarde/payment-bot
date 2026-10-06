"""Ask the model what a follow-up wants, read next to the reply it is answering.

A follow-up is a carrier writing again after someone here answered them. What it asks only
makes sense against that answer: "Both loads were invoiced already" is a correction of what
we said, not a question; "Thank you for the update" is not a request for one. Keyword lists
cannot see either. On 80 real chases from September and October, labelled by hand, the
keyword rules sent 54 to "answer from the records" and 28 of those were not status
questions at all — aging pressure, proof-of-payment requests, reissues, disputes, thanks.
Answering those with a status is the failure this replaces: live, RTS Financial on load
2493116 was sent the same "pending, no pay date" three times while asking us to expedite.

So the model **chooses a route** and does nothing else. Same contract as :mod:`id_filter`:

* it picks one :class:`FollowUpKind` from a fixed list and writes a one-line summary;
* code decides what each kind does — answer from the records, hand to a person, or nothing;
* it cannot add a fact, a recipient or a promise. The summary is shown to reviewers and the
  agent as a description of the ask, never quoted to the carrier.

Every failure routes to a person. A timeout or an unreadable answer returns ``None`` and the
pipeline hands the follow-up off rather than guessing — the costs are not symmetric: a
person reading a plain status chase loses a minute, a status re-sent to someone asking for
something else is the failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from payment_bot.clients.llm import LlmClient, Message, Role, TextBlock, ToolSpec, ToolUseBlock
from payment_bot.logging import get_logger

_log = get_logger("followup_reader")

#: How much of each side the model sees. A chase is short; our reply is short; a signature
#: block and a forwarded table are what this trims.
MAX_CHARS = 2500


class FollowUpKind(StrEnum):
    """What a follow-up wants. Each maps to exactly one :class:`FollowUpRoute`."""

    STATUS = "status"
    PAYMENT_PROOF = "payment_proof"
    NOT_RECEIVED = "not_received"
    PRESSURE = "pressure"
    DISPUTE = "dispute"
    PROCESS_QUESTION = "process_question"
    NEW_INFO = "new_info"
    THANKS = "thanks"
    EMPTY = "empty"
    #: The model could not be asked, or its answer could not be read.
    UNKNOWN = "unknown"


class FollowUpRoute(StrEnum):
    """What the pipeline does with a follow-up."""

    #: Re-read the records and draft — only if the draft says something new.
    ANSWER = "answer"
    #: Hand to the colleagues, copied on a code-authored reply.
    HANDOFF = "handoff"
    #: Nothing: the conversation is closed or there is nothing to answer.
    NONE = "none"


ROUTES: dict[FollowUpKind, FollowUpRoute] = {
    FollowUpKind.STATUS: FollowUpRoute.ANSWER,
    FollowUpKind.PAYMENT_PROOF: FollowUpRoute.ANSWER,
    FollowUpKind.NOT_RECEIVED: FollowUpRoute.HANDOFF,
    FollowUpKind.PRESSURE: FollowUpRoute.HANDOFF,
    FollowUpKind.DISPUTE: FollowUpRoute.HANDOFF,
    FollowUpKind.PROCESS_QUESTION: FollowUpRoute.HANDOFF,
    FollowUpKind.NEW_INFO: FollowUpRoute.HANDOFF,
    FollowUpKind.THANKS: FollowUpRoute.NONE,
    FollowUpKind.EMPTY: FollowUpRoute.NONE,
    FollowUpKind.UNKNOWN: FollowUpRoute.HANDOFF,
}


@dataclass(frozen=True, slots=True)
class FollowUpRead:
    """The reader's verdict on one follow-up."""

    kind: FollowUpKind
    #: One line, in the reader's words: what the sender wants. For reviewers and the agent.
    summary: str = ""
    #: ``model``, ``memo`` (an earlier run's verdict for the same message) or ``fallback``.
    source: str = "model"

    @property
    def route(self) -> FollowUpRoute:
        return ROUTES[self.kind]


_TOOL = ToolSpec(
    name="report_follow_up",
    description="Report what the sender's follow-up wants. Exactly one kind.",
    input_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [k.value for k in FollowUpKind if k is not FollowUpKind.UNKNOWN],
            },
            "summary": {
                "type": "string",
                "description": "what they want, one short sentence, no greeting",
            },
        },
        "required": ["kind", "summary"],
    },
)

_SYSTEM = """\
You read follow-up emails to Circle Delivers' payments inbox. A carrier or factoring company
asked about a payment, someone at Circle replied, and now they have written again. You are
shown OUR LAST REPLY and THEIR NEW MESSAGE. Decide what their new message wants, read in the
light of what we told them. Pick exactly one kind:

- status: they want an update on payment status and nothing more ("any update?", "kind
  reminder", "following up on the below", "can I have an answer?").
- payment_proof: they want details or proof of a payment: check number, payment method,
  remittance, a screenshot, when it was sent.
- not_received: they say a payment we described has not arrived, a check was lost, it went
  to the wrong party, or they ask us to reissue it.
- pressure: they press for faster payment: the invoice's age ("90 days", "past due"),
  demands, deadlines, threats of escalation, recourse, collections or legal action, asking
  us to expedite, fast-track or backdate, or to explain why it has not been paid yet.
- dispute: they contest or correct something we said or a figure: a deduction, a fee, a
  short pay, "we already invoiced", "the paperwork was sent on ...", "we did not request
  quick pay".
- process_question: they ask how we work or whom to contact ("do you offer quick pay?",
  "what do we need to do?", "who should we contact?").
- new_info: they send something rather than ask: documents, an NOA, bank or address
  details, a change of remit-to, or news unrelated to the payment.
- thanks: they acknowledge or thank, and ask for nothing.
- empty: there is no message — only a greeting, an image, or a signature.

Rules:
- Ignore signatures, disclaimers, standing footers and marketing lines. A footer saying
  unpaid invoices go to collections is not a threat in THIS message.
- A status request combined with pressure is "pressure". Combined with a dispute, it is
  "dispute". A thank-you that also asks for something is that something.
- When torn between "status" and any other kind, choose the other kind.
- The summary describes what they want in your own words. Never invent facts.
"""


def read_follow_up_with_model(
    llm: LlmClient,
    *,
    subject: str,
    our_reply: str,
    their_message: str,
    max_tokens: int = 512,
    correlation_id: str = "",
) -> FollowUpRead | None:
    """Ask the model what the follow-up wants. ``None`` when it cannot say. Never raises.

    ``correlation_id`` labels the ``llm_usage`` line, so this call is visible in the cost
    review beside the agent loop's and the id filter's.
    """

    prompt = (
        f"Subject: {subject}\n\n"
        f"OUR LAST REPLY:\n{our_reply.strip()[:MAX_CHARS]}\n\n"
        f"THEIR NEW MESSAGE:\n{their_message.strip()[:MAX_CHARS] or '(nothing written)'}"
    )
    try:
        response = llm.converse(
            system=_SYSTEM,
            messages=[Message(role=Role.USER, content=[TextBlock(text=prompt)])],
            tools=[_TOOL],
            max_tokens=max_tokens,
            temperature=0.0,
        )
    except Exception as exc:
        _log.warning("followup_reader_call_failed", extra={"error": str(exc)})
        return None

    _log.info(
        "llm_usage",
        extra={
            "correlation_id": correlation_id,
            "label": "followup_reader",
            "iteration": 1,
            **response.usage,
        },
    )

    payload: Any = None
    for block in response.content:
        if isinstance(block, ToolUseBlock) and block.name == _TOOL.name:
            payload = block.input
            break
        # Some providers answer with JSON text rather than a tool call.
        if isinstance(block, TextBlock):
            try:
                payload = json.loads(block.text[block.text.index("{") : block.text.rindex("}") + 1])
            except (ValueError, json.JSONDecodeError):
                continue
    if not isinstance(payload, dict):
        _log.warning("followup_reader_unreadable_response")
        return None
    try:
        kind = FollowUpKind(str(payload.get("kind", "")).strip())
    except ValueError:
        _log.warning("followup_reader_unknown_kind", extra={"kind": payload.get("kind")})
        return None
    if kind is FollowUpKind.UNKNOWN:
        return None
    summary = " ".join(str(payload.get("summary", "")).split())[:240]
    return FollowUpRead(kind=kind, summary=summary, source="model")
