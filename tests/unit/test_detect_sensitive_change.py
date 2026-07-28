"""Unit tests for detect_sensitive_change (§4.2).

The important negative case: merely *naming* a factoring company (normal in rate
verification) must NOT escalate — only an add/attach/update action does.
"""

from __future__ import annotations

import pytest

from payment_bot.models import SensitiveAction, SensitiveFlag
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import (
    AttachmentMeta,
    DetectSensitiveChange,
    DetectSensitiveChangeInput,
    DetectSensitiveChangeOutput,
)


def _run(ctx: ToolContext, **kw: object) -> DetectSensitiveChangeOutput:
    out = DetectSensitiveChange().run(DetectSensitiveChangeInput(**kw), ctx)  # type: ignore[arg-type]
    assert isinstance(out, DetectSensitiveChangeOutput)
    return out


@pytest.mark.unit
def test_bank_change_escalates(ctx: ToolContext) -> None:
    out = _run(ctx, subject="Banking update", body="Please change our account number and routing number.")
    assert out.action is SensitiveAction.ESCALATE
    assert SensitiveFlag.BANK_CHANGE in out.flags


@pytest.mark.unit
def test_noa_add_escalates(ctx: ToolContext) -> None:
    out = _run(ctx, body="Please add our NOA and set up factoring for these loads.")
    assert out.action is SensitiveAction.ESCALATE
    assert SensitiveFlag.NOA_SETUP_CHANGE in out.flags


@pytest.mark.unit
def test_naming_factoring_company_does_not_escalate(ctx: ToolContext) -> None:
    out = _run(ctx, body="Our factoring company is England Carrier Services; please verify the rate.")
    assert out.action is SensitiveAction.CONTINUE
    assert out.flags == [SensitiveFlag.NONE]


@pytest.mark.unit
def test_contact_change_escalates(ctx: ToolContext) -> None:
    out = _run(ctx, body="We want to change our email for this account going forward.")
    assert out.action is SensitiveAction.ESCALATE
    assert SensitiveFlag.EMAIL_CONTACT_CHANGE in out.flags


@pytest.mark.unit
def test_void_check_attachment_flags_bank_change(ctx: ToolContext) -> None:
    out = _run(
        ctx,
        body="Updated remit info attached.",
        attachments_metadata=[AttachmentMeta(filename="VoidCheck_2026.pdf")],
    )
    assert out.action is SensitiveAction.ESCALATE
    assert SensitiveFlag.BANK_CHANGE in out.flags


@pytest.mark.unit
def test_clean_email_continues(ctx: ToolContext) -> None:
    out = _run(ctx, subject="Payment status for 2462934", body="When will I be paid?")
    assert out.action is SensitiveAction.CONTINUE
    assert out.flags == [SensitiveFlag.NONE]
