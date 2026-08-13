"""The model filters load-id candidates; it never finds them.

The regex is ``\\b\\d{6,7}\\b`` with seven guards bolted on, each added after a live
escalation. Good recall, poor precision — and precision is what reading the sentence buys.
A WEX collections table put "Mot Car" forty characters from the number under it, out of
reach of a proximity window that cannot widen without attaching labels to whatever precedes
a number two cells later.

What makes handing that judgement to a model acceptable is the direction of travel: it may
only remove candidates the regex already produced. An invented id is not in the candidate
list, so it cannot survive the intersection — which matters because extracted ids feed
``check_authorization`` and the gate's coverage baseline, and an invented id the sender
happened to be authorized for would be disclosed unasked.

Everything below is a different way of asserting that, plus the refusals: an incomplete
answer, an answer that drops everything, and any failure at all leave the candidates alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from payment_bot.clients.llm import (
    LlmResponse,
    Message,
    TextBlock,
    ToolSpec,
    ToolUseBlock,
)
from payment_bot.id_filter import IdFilterMode, Verdict, apply_filter, classify

#: The live WEX row, flattened out of the HTML exactly as it reaches us.
WEX_TEXT = (
    "Carrier Mot Car Account Mot Car Invoice Load Age Balance "
    "FFS Brothers LLC 1601899 CIRCLE LOGISTICS, INC (IN) (7 DIGIT LOAD#S) "
    "(FREIGHTPAY@CIRCLEDELIVERS.COM) 761291 IN-001208 2481841 45 $150.00"
)
WEX_CANDIDATES = ["1601899", "761291", "001208", "2481841"]


class _FakeLlm:
    """Replays one response and records what it was asked."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse:
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _tool_response(rows: list[dict[str, str]]) -> LlmResponse:
    return LlmResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                tool_use_id="t1",
                name="report_identifier_kinds",
                input={"identifiers": rows},
            )
        ],
    )


def _wex_rows() -> list[dict[str, str]]:
    return [
        {"value": "1601899", "kind": "mc_number", "why": "under the Mot Car column"},
        {"value": "761291", "kind": "mc_number", "why": "second Mot Car column"},
        {"value": "001208", "kind": "invoice", "why": "the Invoice cell, IN-001208"},
        {"value": "2481841", "kind": "load", "why": "under the Load column"},
    ]


# --- the case this exists for -----------------------------------------------
def test_the_wex_table_reduces_to_the_one_real_load() -> None:
    """Two guards fired on this row and the MC number still survived them."""

    llm = _FakeLlm(_tool_response(_wex_rows()))
    verdicts = classify(llm, WEX_CANDIDATES, WEX_TEXT)

    assert apply_filter(IdFilterMode.ENFORCE, WEX_CANDIDATES, verdicts) == ["2481841"]


def test_shadow_mode_changes_nothing() -> None:
    """The point of shadow mode: measure agreement on real mail, act on none of it."""

    llm = _FakeLlm(_tool_response(_wex_rows()))
    verdicts = classify(llm, WEX_CANDIDATES, WEX_TEXT)

    assert apply_filter(IdFilterMode.SHADOW, WEX_CANDIDATES, verdicts) == WEX_CANDIDATES


# --- the property that makes this safe --------------------------------------
def test_a_value_the_regex_never_produced_cannot_survive() -> None:
    """Hallucination is structurally impossible rather than merely unlikely.

    Extracted ids feed check_authorization and the gate's coverage baseline. An invented id
    the sender happened to be authorized for — another load of the same carrier — would be
    disclosed without anyone asking for it, so the intersection is not a tidiness measure.
    """

    llm = _FakeLlm(
        _tool_response(
            [
                {"value": "2481841", "kind": "load", "why": "under the Load column"},
                {"value": "9999999", "kind": "load", "why": "invented out of nowhere"},
            ]
        )
    )
    verdicts = classify(llm, ["2481841", "761291"], WEX_TEXT)

    assert "9999999" not in {v.value for v in verdicts}


def test_the_model_is_never_asked_to_find_anything() -> None:
    """The candidates are given to it. Its instructions say classify, not extract."""

    llm = _FakeLlm(_tool_response(_wex_rows()))
    classify(llm, WEX_CANDIDATES, WEX_TEXT)

    system = llm.calls[0]["system"]
    assert "You do not find numbers" in system
    assert "Never report a number that is not in the candidate list" in system
    prompt = llm.calls[0]["messages"][0].content[0].text
    for candidate in WEX_CANDIDATES:
        assert candidate in prompt


# --- refusals: every one leaves the candidates alone ------------------------
def test_an_incomplete_answer_is_ignored_entirely() -> None:
    """A partial classification means it did not do the job; acting on half is worse."""

    verdicts = [Verdict("2481841", "load", "the Load column")]

    assert apply_filter(IdFilterMode.ENFORCE, WEX_CANDIDATES, verdicts) == WEX_CANDIDATES


def test_dropping_every_candidate_is_refused() -> None:
    """Far likelier a model failure than an email that names no load at all.

    And the cost of being wrong is the worst outcome available: a carrier who is never
    answered, with nothing escalated either, because the email looked like it named nothing.
    """

    verdicts = [Verdict(c, "other", "no idea") for c in WEX_CANDIDATES]

    assert apply_filter(IdFilterMode.ENFORCE, WEX_CANDIDATES, verdicts) == WEX_CANDIDATES


def test_no_verdicts_means_no_opinion() -> None:
    assert apply_filter(IdFilterMode.ENFORCE, WEX_CANDIDATES, []) == WEX_CANDIDATES


@pytest.mark.parametrize(
    "response",
    [
        RuntimeError("bedrock is down"),
        LlmResponse(stop_reason="end_turn", content=[TextBlock(text="sure, whatever")]),
        LlmResponse(stop_reason="end_turn", content=[]),
        LlmResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(tool_use_id="t", name="report_identifier_kinds", input={})],
        ),
    ],
)
def test_every_failure_mode_falls_back_to_the_regex(response: Any) -> None:
    """A dead model, a chatty one, an empty one. None of them may change the outcome."""

    verdicts = classify(_FakeLlm(response), WEX_CANDIDATES, WEX_TEXT)

    assert apply_filter(IdFilterMode.ENFORCE, WEX_CANDIDATES, verdicts) == WEX_CANDIDATES


def test_a_single_candidate_is_not_worth_a_call() -> None:
    """Nothing to disambiguate, and the guards already decide it as well as a model would."""

    llm = _FakeLlm(_tool_response([{"value": "2481841", "kind": "other", "why": "-"}]))

    assert classify(llm, ["2481841"], WEX_TEXT) == []
    assert llm.calls == []


def test_json_in_a_text_block_is_accepted() -> None:
    """Not every provider answers a tool call with a tool call."""

    llm = _FakeLlm(
        LlmResponse(
            stop_reason="end_turn",
            content=[
                TextBlock(
                    text='Here you go: {"identifiers": ['
                    '{"value": "2481841", "kind": "load", "why": "Load column"},'
                    '{"value": "761291", "kind": "mc_number", "why": "Mot Car"}]}'
                )
            ],
        )
    )
    verdicts = classify(llm, ["2481841", "761291"], WEX_TEXT)

    assert apply_filter(IdFilterMode.ENFORCE, ["2481841", "761291"], verdicts) == ["2481841"]


def test_order_is_the_senders_not_the_models() -> None:
    """Coverage and the reply read in the order the email listed them."""

    rows = [
        {"value": "2481841", "kind": "load", "why": "b"},
        {"value": "2462934", "kind": "load", "why": "a"},
    ]
    candidates = ["2462934", "2481841"]
    verdicts = classify(_FakeLlm(_tool_response(rows)), candidates, "2462934 and 2481841")

    assert apply_filter(IdFilterMode.ENFORCE, candidates, verdicts) == candidates
