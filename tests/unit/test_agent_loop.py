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
    loop = AgentLoop(llm, registry, max_iterations=5)

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
