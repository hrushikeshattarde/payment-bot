"""The agent's iteration budget must scale with the number of loads in the email.

Regression for a live escalation: an email naming loads 2487002 and 2457019 made fourteen
tool calls, every one successful, and still ended at max_iterations with no draft — because
the budget was a flat 12. The skill procedures run per load, so a fixed cap cannot serve a
variable load count, and `bulk_threshold` lets up to five loads reach the agent.
"""

from __future__ import annotations

import pytest

from payment_bot.agent import AgentLoop
from payment_bot.agent.skills import PAYMENT_STATUS_SKILL
from payment_bot.clients import (
    AutoApproveResolver,
    LlmResponse,
    MockGmailClient,
    MockSlackClient,
    ScriptedLlmClient,
    ToolUseBlock,
)
from payment_bot.config import Settings
from payment_bot.grounding import GroundingLedger
from payment_bot.logging import InMemoryAuditSink
from payment_bot.pipeline import ITERATION_CEILING, PaymentBotPipeline
from payment_bot.sample_data import sample_transport_pro_client
from payment_bot.tools import build_default_registry
from payment_bot.tools.base import ToolContext


def _pipeline(**overrides: object) -> PaymentBotPipeline:
    return PaymentBotPipeline(
        tp=sample_transport_pro_client(),
        gmail=MockGmailClient(inbox=[]),
        slack=MockSlackClient(),
        llm=ScriptedLlmClient([]),
        approval_resolver=AutoApproveResolver(),
        settings=Settings(**overrides),  # type: ignore[arg-type]
        audit_sink=InMemoryAuditSink(),
    )


#: What the payment_status procedure costs per load, counted off live tool trails:
#: load_summary, compute_scheduled_pay_date, dispatch_history, carrier_cross_check,
#: settlement_entries, file_history, noa_factoring, check_authorization.
CALLS_PER_LOAD = 8
#: One submit_draft covers the whole reply, however many loads.
TERMINAL_CALLS = 1


@pytest.mark.unit
def test_one_load_keeps_the_configured_budget_exactly() -> None:
    """The single-load case must be unchanged — no behaviour drift for the common email."""

    pipeline = _pipeline(agent_max_iterations=12)
    assert pipeline._iteration_budget(1) == 12


@pytest.mark.unit
@pytest.mark.parametrize(("loads", "expected"), [(2, 21), (3, 30), (4, 39), (5, 48)])
def test_each_extra_load_adds_a_per_load_pass(loads: int, expected: int) -> None:
    pipeline = _pipeline(agent_max_iterations=12)
    assert pipeline._iteration_budget(loads) == expected


@pytest.mark.unit
@pytest.mark.parametrize("loads", [1, 2, 3, 4, 5])
def test_every_answerable_load_count_can_actually_finish(loads: int) -> None:
    """The budget must cover the real cost of the procedure, with room for submit_draft.

    This is the assertion that would have caught the off-by-one: at 5 loads the budget was
    40 against 41 calls needed, so the agent made every call successfully and then had
    nothing left to deliver with. bulk_threshold lets up to 5 loads reach the agent, so
    every count in that range must be finishable.
    """

    pipeline = _pipeline(agent_max_iterations=12)
    required = loads * CALLS_PER_LOAD + TERMINAL_CALLS
    assert pipeline._iteration_budget(loads) >= required, (
        f"{loads} load(s) need {required} calls but the budget is "
        f"{pipeline._iteration_budget(loads)}"
    )


@pytest.mark.unit
def test_the_two_load_email_that_failed_now_gets_enough_budget() -> None:
    """14 successful calls were not enough at 12; two loads must clear that bar."""

    pipeline = _pipeline(agent_max_iterations=12)
    assert pipeline._iteration_budget(2) > 14


@pytest.mark.unit
def test_budget_is_clamped_so_the_loop_still_terminates() -> None:
    """Scaling must not defeat the cap's purpose: bounding a runaway model."""

    pipeline = _pipeline(agent_max_iterations=50, agent_iterations_per_extra_load=20)
    assert pipeline._iteration_budget(99) == ITERATION_CEILING


@pytest.mark.unit
def test_zero_or_one_load_never_goes_below_the_configured_budget() -> None:
    pipeline = _pipeline(agent_max_iterations=12)
    assert pipeline._iteration_budget(0) == 12


@pytest.mark.unit
def test_loop_honours_a_per_run_budget_override() -> None:
    """The loop must use the caller's budget, not its constructor default."""

    ctx = ToolContext(
        tp=sample_transport_pro_client(), ledger=GroundingLedger(), correlation_id="budget-test"
    )
    # Never calls submit_draft, so the run can only end at the iteration cap.
    spins = [
        LlmResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(f"tu-{i}", "tp_get_load_summary", {"load_id": "2462934"})],
        )
        for i in range(30)
    ]
    loop = AgentLoop(ScriptedLlmClient(spins), build_default_registry(), max_iterations=3)

    result = loop.run(
        system=PAYMENT_STATUS_SKILL.system_prompt,
        intake_prompt="p",
        allowed_tools=PAYMENT_STATUS_SKILL.allowed_tools,
        ctx=ctx,
        max_iterations=9,
    )
    assert result.stop_reason == "max_iterations"
    assert result.iterations == 9  # the override, not the constructor's 3


@pytest.mark.unit
def test_loop_falls_back_to_its_default_budget() -> None:
    """Omitting the override must leave the constructor's cap in force.

    Uses tool_use spins, not end_turn: an end_turn run exits via the submit-nudge path
    (`max_submit_nudges`) long before the iteration cap, so it would not test the cap.
    """

    ctx = ToolContext(
        tp=sample_transport_pro_client(), ledger=GroundingLedger(), correlation_id="budget-test"
    )
    spins = [
        LlmResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(f"tu-{i}", "tp_get_load_summary", {"load_id": "2462934"})],
        )
        for i in range(30)
    ]
    loop = AgentLoop(ScriptedLlmClient(spins), build_default_registry(), max_iterations=4)
    result = loop.run(
        system="s", intake_prompt="p", allowed_tools=("tp_get_load_summary",), ctx=ctx
    )
    assert result.stop_reason == "max_iterations"
    assert result.iterations == 4
