"""Ask the model which of the extracted numbers are actually load ids.

The detector is ``\\b\\d{6,7}\\b`` and everything else in
:mod:`payment_bot.tools.shared` is a guard bolted onto it: URLs, a label before the number,
a company suffix after it, zero padding, the sender's own invoice number, the id they
called a load, the id length they declared. Seven, each added after a live escalation, each
narrow. The regex has good recall and poor precision, and precision is where reading the
sentence helps — a WEX collections table put "Mot Car" forty characters from the number
beneath it, out of reach of a proximity window that cannot widen without attaching labels
to whatever happens to precede a number two cells later.

So the model **filters** and never extracts. That distinction is the whole design:

* the regex proposes every candidate, deterministically, as it does today;
* the model classifies each one in context;
* code keeps the intersection.

An id the model invents therefore cannot survive, because it was never a candidate. That
matters more than it might sound: extracted ids feed ``check_authorization`` and the gate's
coverage baseline, so an invented id the sender happened to be authorized for — another
load of the same carrier — would be disclosed without anyone asking for it.

Every failure keeps today's behaviour. A timeout, a malformed reply, a model that drops
everything: all of them return the candidates unchanged. The filter can only ever remove,
and only when it has something coherent to say.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from payment_bot.clients.llm import LlmClient, Message, Role, TextBlock, ToolSpec, ToolUseBlock
from payment_bot.logging import get_logger

_log = get_logger("id_filter")

#: Below this, there is nothing to disambiguate and no call worth paying for. A single
#: candidate is either the load or it is not, and the guards already decide that as well as
#: a model would.
MIN_CANDIDATES = 2

#: How much of the email the model sees. Enough for a signature and a table; short enough
#: that a forwarded thread does not turn one classification into a long prompt.
MAX_CONTEXT_CHARS = 6000


class IdFilterMode(StrEnum):
    """How much authority the filter has."""

    #: Not called at all.
    OFF = "off"
    #: Called, logged, and ignored. For measuring agreement against the guards on real mail
    #: before anything depends on it.
    SHADOW = "shadow"
    #: Called and applied.
    ENFORCE = "enforce"


_TOOL = ToolSpec(
    name="report_identifier_kinds",
    description=(
        "Report what each candidate number is. One entry per candidate, every candidate "
        "classified, no numbers added."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "identifiers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "description": "the candidate, verbatim"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "load",
                                "invoice",
                                "mc_number",
                                "check_number",
                                "account_number",
                                "phone",
                                "amount",
                                "date",
                                "other",
                            ],
                        },
                        "why": {
                            "type": "string",
                            "description": "the words in the email that say so, briefly",
                        },
                    },
                    "required": ["value", "kind", "why"],
                },
            }
        },
        "required": ["identifiers"],
    },
)

_SYSTEM = """\
You classify numbers already found in a freight payment email. You do not find numbers.

Circle Delivers' own load ids are 6 or 7 digits. So are plenty of things that are not loads:
motor-carrier (MC) numbers, the sender's invoice numbers, check numbers, account numbers,
phone fragments, zip+4. Your job is to say which is which, using what the email itself says.

The strongest evidence is a label the sender wrote — beside the number, or as the column
header above it in a table. Collections tables arrive flattened to one line, so a header
like "Carrier | Mot Car | Account | Invoice | Load" is followed by its values in the SAME
ORDER. Line them up.

On a statement or invoice the SENDER created, the column names mean the opposite of what
they suggest: "Invoice #" and "Order #" carry the sender's OWN numbering (kind "invoice"),
while the number that points at OUR load sits under "Reference #", "Ref", or "Load". A
carrier statement reading "Order # 3040444 | Invoice # 3040444 | Reference # 2519649" is
about load 2519649 — the number repeated across the sender's own columns is their paperwork
chain, not our load. Never drop a Reference/Ref/Load-labelled candidate while keeping an
Invoice/Order-labelled one from the same row.

Rules:
- Classify EVERY candidate you are given, exactly once, using the value verbatim.
- Never report a number that is not in the candidate list.
- When the email gives you nothing to go on, say "load". And when you are torn between
  "load" and anything else, say "load" too. The costs are not symmetric: a kept non-load
  costs one lookup that comes back empty; a dropped load id is a carrier who never gets
  a reply at all.
"""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the model called one candidate."""

    value: str
    kind: str
    why: str

    @property
    def is_load(self) -> bool:
        return self.kind == "load"


def classify(
    llm: LlmClient,
    candidates: list[str],
    email_text: str,
    *,
    max_tokens: int = 1024,
    correlation_id: str = "",
) -> list[Verdict]:
    """Ask the model what each candidate is. Returns ``[]`` when it cannot say.

    Never raises. The caller treats an empty result as "no opinion", which keeps every
    failure mode identical to not having called at all.

    ``correlation_id`` only labels the ``llm_usage`` line. This call is easy to overlook in
    a cost review — it is one turn against an email that may never reach the agent loop at
    all — so it reports its tokens under the same event name and field names the loop uses,
    and the ``label`` is what separates the two in the total.
    """

    if len(candidates) < MIN_CANDIDATES:
        return []

    prompt = (
        f"Candidates: {', '.join(candidates)}\n\n"
        f"Email:\n{email_text[:MAX_CONTEXT_CHARS]}"
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
        _log.warning("id_filter_call_failed", extra={"error": str(exc)})
        return []

    _log.info(
        "llm_usage",
        extra={
            "correlation_id": correlation_id,
            "label": "id_filter",
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
        _log.warning("id_filter_unreadable_response")
        return []

    allowed = set(candidates)
    verdicts: list[Verdict] = []
    for row in payload.get("identifiers") or []:
        if not isinstance(row, dict):
            continue
        value = str(row.get("value", "")).strip()
        # The intersection that makes hallucination structurally impossible: a value the
        # regex did not produce is discarded here, whatever the model called it.
        if value not in allowed:
            _log.info("id_filter_ignored_unknown_value", extra={"value": value})
            continue
        verdicts.append(
            Verdict(
                value=value,
                kind=str(row.get("kind", "other")).strip() or "other",
                why=str(row.get("why", ""))[:200],
            )
        )
    return verdicts


def apply_filter(
    mode: IdFilterMode,
    candidates: list[str],
    verdicts: list[Verdict],
    *,
    correlation_id: str = "",
) -> list[str]:
    """The ids to proceed with, given the model's opinion and how much authority it has.

    Refuses to act in three cases, each returning the candidates untouched:

    * **no verdicts** — the call failed or was skipped, so there is no opinion to apply;
    * **an incomplete answer** — a candidate the model did not classify means it did not do
      the job asked of it, and acting on a partial answer is worse than ignoring it;
    * **everything dropped** — an email whose every number is "not a load" is far more
      likely a model failure than a real one, and the cost of being wrong is a carrier who
      is never answered at all.
    """

    if not verdicts:
        return candidates

    classified = {v.value for v in verdicts}
    if classified != set(candidates):
        _log.warning(
            "id_filter_incomplete",
            extra={
                "correlation_id": correlation_id,
                "unclassified": sorted(set(candidates) - classified),
            },
        )
        return candidates

    keep = [c for c in candidates if any(v.value == c and v.is_load for v in verdicts)]
    dropped = [v for v in verdicts if not v.is_load]
    if not keep:
        _log.warning(
            "id_filter_would_drop_everything",
            extra={"correlation_id": correlation_id, "candidates": candidates},
        )
        return candidates

    if dropped:
        _log.info(
            "id_filter_shadow" if mode is IdFilterMode.SHADOW else "id_filter_applied",
            extra={
                "correlation_id": correlation_id,
                "kept": keep,
                "dropped": [{"value": v.value, "kind": v.kind, "why": v.why} for v in dropped],
            },
        )

    return keep if mode is IdFilterMode.ENFORCE else candidates
