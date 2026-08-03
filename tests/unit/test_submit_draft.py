"""Unit tests for submit_draft's mechanical tool-mention stripping."""

from __future__ import annotations

import pytest

from payment_bot.tools import build_default_registry
from payment_bot.tools.base import ToolContext
from payment_bot.tools.submit import TOOL_NAMES, SubmitDraft, SubmitDraftInput, strip_tool_mentions


@pytest.mark.unit
def test_tool_names_constant_matches_the_registry() -> None:
    """The static list must never drift from what build_default_registry registers."""

    registry = build_default_registry()
    assert {tool.name for tool in registry._tools.values()} == TOOL_NAMES


@pytest.mark.unit
def test_bracketed_tool_mentions_are_stripped() -> None:
    """Verbatim from a live draft that reached Gmail Drafts."""

    body = (
        "Load 2407673 is BILLED with a payment of $900 [tp_get_load_summary] scheduled "
        "for Thursday, August 13, 2026 [compute_scheduled_pay_date]. The payment status "
        "is Pending [tp_get_load_summary]."
    )
    assert strip_tool_mentions(body) == (
        "Load 2407673 is BILLED with a payment of $900 scheduled for Thursday, "
        "August 13, 2026. The payment status is Pending."
    )


@pytest.mark.unit
def test_bare_and_parenthesised_mentions_are_stripped() -> None:
    body = "Per tp_get_load_summary the amount is $900 (compute_scheduled_pay_date)."
    cleaned = strip_tool_mentions(body)
    assert "tp_get_load_summary" not in cleaned
    assert "compute_scheduled_pay_date" not in cleaned
    assert "$900" in cleaned


@pytest.mark.unit
def test_ordinary_prose_is_untouched() -> None:
    body = "Load 2462934 is BILLED, totaling $4,650. Scheduled: Thursday, August 20, 2026."
    assert strip_tool_mentions(body) == body


@pytest.mark.unit
def test_submit_draft_sanitises_the_body(ctx: ToolContext) -> None:
    out = SubmitDraft().run(
        SubmitDraftInput(
            reply_body="Status is Pending [tp_get_load_summary].",
            to="billing@ideaexpedited.com",
            load_ids=["2462934"],
        ),
        ctx,
    )
    assert out.reply_body == "Status is Pending.\n\nCircle Delivers Payments"


@pytest.mark.unit
def test_missing_signature_is_appended(ctx: ToolContext) -> None:
    """Three live drafts in one day omitted the sign-off; it is code now."""

    out = SubmitDraft().run(
        SubmitDraftInput(
            reply_body="Load 2462934 is BILLED, totaling $4,650.",
            to="billing@ideaexpedited.com",
            load_ids=["2462934"],
        ),
        ctx,
    )
    assert out.reply_body.endswith("\n\nCircle Delivers Payments")


@pytest.mark.unit
def test_an_existing_signature_is_not_duplicated(ctx: ToolContext) -> None:
    body = "Load 2462934 is BILLED.\n\nBest regards,\nCircle Delivers Payments"
    out = SubmitDraft().run(
        SubmitDraftInput(reply_body=body, to="billing@ideaexpedited.com", load_ids=["2462934"]),
        ctx,
    )
    assert out.reply_body.count("Circle Delivers Payments") == 1
