"""Unit tests for the portable agent loop."""

from __future__ import annotations

import pytest

from payment_bot.agent import AgentLoop
from payment_bot.agent.skills import PAYMENT_STATUS_SKILL
from payment_bot.clients import LlmResponse, ScriptedLlmClient, TextBlock, ToolUseBlock
from payment_bot.grounding import GroundingLedger
from payment_bot.logging import InMemoryAuditSink
from payment_bot.sample_data import (
    SAMPLE_SENDER_EMAIL,
    sample_transport_pro_client,
    scripted_payment_status_llm,
)
from payment_bot.tools import build_default_registry
from payment_bot.tools.base import ToolContext


def _ctx() -> tuple[ToolContext, InMemoryAuditSink]:
    audit = InMemoryAuditSink()
    ctx = ToolContext(
        tp=sample_transport_pro_client(), ledger=GroundingLedger(), correlation_id="loop-test"
    )
    return ctx, audit


def _tool_use(i: int, name: str, payload: dict[str, object]) -> LlmResponse:
    return LlmResponse(stop_reason="tool_use", content=[ToolUseBlock(f"tu-{i}", name, payload)])


@pytest.mark.unit
def test_loop_runs_scripted_tools_and_returns_draft() -> None:
    ctx, audit = _ctx()
    registry = build_default_registry(audit)
    loop = AgentLoop(scripted_payment_status_llm(), registry, max_iterations=12)

    result = loop.run(
        system=PAYMENT_STATUS_SKILL.system_prompt,
        intake_prompt="handle load 2462934",
        allowed_tools=PAYMENT_STATUS_SKILL.allowed_tools,
        ctx=ctx,
    )

    assert result.stop_reason == "submit_draft"
    assert result.draft is not None
    assert result.draft.to == SAMPLE_SENDER_EMAIL
    names = [e.tool_name for e in audit.for_correlation("loop-test")]
    assert names[0] == "tp_get_load_summary"
    assert names[-1] == "submit_draft"


@pytest.mark.unit
def test_tool_outside_skill_boundary_is_rejected_not_dispatched() -> None:
    ctx, audit = _ctx()
    registry = build_default_registry(audit)
    # route_load IS registered but NOT advertised to payment_status; calling it must be
    # rejected by the loop and never dispatched (so it never reaches the audit trail).
    llm = ScriptedLlmClient(
        responses=[
            _tool_use(1, "route_load", {"load_id": "2462934"}),
            LlmResponse(stop_reason="end_turn", content=[TextBlock("giving up")]),
        ]
    )
    # Nudging off: this is about the skill boundary, not about delivery, and a nudge would
    # ask for a third turn this script does not have.
    loop = AgentLoop(llm, registry, max_iterations=5, max_submit_nudges=0)

    result = loop.run(
        system="s",
        intake_prompt="p",
        allowed_tools=PAYMENT_STATUS_SKILL.allowed_tools,
        ctx=ctx,
    )

    assert result.draft is None
    assert result.stop_reason == "end_turn"
    assert "route_load" not in [e.tool_name for e in audit.for_correlation("loop-test")]


@pytest.mark.unit
def test_max_iterations_guard_stops_a_runaway() -> None:
    ctx, audit = _ctx()
    registry = build_default_registry(audit)
    # Always calls a tool, never submits.
    llm = ScriptedLlmClient(
        responses=[
            _tool_use(1, "tp_get_load_summary", {"load_id": "2462934"}),
            _tool_use(2, "tp_get_load_summary", {"load_id": "2462934"}),
        ]
    )
    loop = AgentLoop(llm, registry, max_iterations=2)

    result = loop.run(
        system="s", intake_prompt="p", allowed_tools=PAYMENT_STATUS_SKILL.allowed_tools, ctx=ctx
    )

    assert result.draft is None
    assert result.stop_reason == "max_iterations"
    assert result.iterations == 2


@pytest.mark.unit
def test_failed_submit_is_fed_back_and_retried() -> None:
    ctx, audit = _ctx()
    registry = build_default_registry(audit)
    # First submit is invalid (missing required 'to'); loop must feed the error back and
    # let the next turn submit a valid draft.
    llm = ScriptedLlmClient(
        responses=[
            _tool_use(1, "submit_draft", {"reply_body": "hi", "load_ids": ["2462934"]}),
            _tool_use(
                2,
                "submit_draft",
                {"reply_body": "hi", "to": SAMPLE_SENDER_EMAIL, "load_ids": ["2462934"]},
            ),
        ]
    )
    loop = AgentLoop(llm, registry, max_iterations=5)

    result = loop.run(
        system="s", intake_prompt="p", allowed_tools=PAYMENT_STATUS_SKILL.allowed_tools, ctx=ctx
    )

    assert result.stop_reason == "submit_draft"
    assert result.draft is not None
    assert result.iterations == 2


# --- Delivery enforcement ----------------------------------------------------
# On live mail the agent ran the whole procedure — fourteen clean tool calls — then wrote its
# answer as prose and stopped. The work was done; it was delivered to the wrong place. A
# prompt instruction did not settle it, so the loop asks for the terminal tool call.
@pytest.mark.unit
def test_prose_answer_is_nudged_into_submit_draft(ctx: ToolContext) -> None:
    llm = ScriptedLlmClient(
        [
            LlmResponse("end_turn", [TextBlock("Load 2462934 is BILLED and pays Thursday.")]),
            LlmResponse(
                "tool_use",
                [
                    ToolUseBlock(
                        "c1",
                        "submit_draft",
                        {
                            "reply_body": "Load 2462934 is BILLED and pays Thursday.",
                            "to": "billing@ideaexpedited.com",
                            "load_ids": ["2462934"],
                        },
                    )
                ],
            ),
        ]
    )
    result = AgentLoop(llm, build_default_registry(None)).run(
        system="s", intake_prompt="p", allowed_tools=("submit_draft",), ctx=ctx
    )

    assert result.stop_reason == "submit_draft"
    assert result.draft is not None
    assert "BILLED" in result.draft.reply_body


@pytest.mark.unit
def test_the_nudge_carries_the_models_own_text_back(ctx: ToolContext) -> None:
    """It must see what it wrote, so it can resubmit that rather than start over."""

    llm = ScriptedLlmClient(
        [
            LlmResponse("end_turn", [TextBlock("Pays Thursday, August 20, 2026.")]),
            LlmResponse("end_turn", [TextBlock("still prose")]),
            LlmResponse("end_turn", [TextBlock("still prose")]),
        ]
    )
    result = AgentLoop(llm, build_default_registry(None)).run(
        system="s", intake_prompt="p", allowed_tools=("submit_draft",), ctx=ctx
    )

    texts = [b.text for m in result.transcript for b in m.content if isinstance(b, TextBlock)]
    assert "Pays Thursday, August 20, 2026." in texts
    assert any("submit_draft" in t for t in texts)


@pytest.mark.unit
def test_nudging_is_bounded(ctx: ToolContext) -> None:
    """A model that will not call the tool must not spin."""

    llm = ScriptedLlmClient([LlmResponse("end_turn", [TextBlock("prose")]) for _ in range(10)])
    result = AgentLoop(llm, build_default_registry(None), max_submit_nudges=2).run(
        system="s", intake_prompt="p", allowed_tools=("submit_draft",), ctx=ctx
    )

    assert result.draft is None
    assert result.stop_reason == "end_turn"
    # One original turn plus exactly two nudged retries.
    assert len(llm.calls) == 3


@pytest.mark.unit
def test_nudging_can_be_disabled(ctx: ToolContext) -> None:
    llm = ScriptedLlmClient([LlmResponse("end_turn", [TextBlock("prose")])])
    result = AgentLoop(llm, build_default_registry(None), max_submit_nudges=0).run(
        system="s", intake_prompt="p", allowed_tools=("submit_draft",), ctx=ctx
    )

    assert result.draft is None
    assert len(llm.calls) == 1
