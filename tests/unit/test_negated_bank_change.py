"""A negated change phrase is a prohibition, not a request.

Live regression. Far West Capital appends an anti-fraud disclaimer to every email: "Do not
change payment instructions on wires or ACH without calling the person you are paying". That
matched the verb-first proximity scan, set hard_bank, and escalated a two-line TONU status
question at severity=security. The footer is standard factoring-company legal boilerplate, so
it would have blocked every sender who uses it.

The narrowing must not weaken detection of a real instruction — that is what most of these
tests pin.
"""

from __future__ import annotations

import pytest

from payment_bot.models import SensitiveAction, SensitiveFlag
from payment_bot.tools.base import ToolContext
from payment_bot.tools.shared import DetectSensitiveChange, DetectSensitiveChangeInput

#: The real footer, verbatim apart from the em dash.
_FRAUD_FOOTER = (
    "Important Message to our valued customers: Fraud, phishing and email compromise are "
    "on the rise. Do not change payment instructions on wires or ACH without calling the "
    "person you are paying - using a trusted phone number (NOT email)."
)

_STATUS_ASK = "Hello, any updates on TONU #2451034 payment status? Please advise as load is 58 days"


def _run(body: str, ctx: ToolContext, subject: str = "Re: TONU #2451034 at 58 days"):
    return DetectSensitiveChange().run(
        DetectSensitiveChangeInput(subject=subject, body=body, attachments_metadata=[]),
        ctx,
    )


@pytest.mark.unit
def test_the_far_west_footer_no_longer_escalates(ctx: ToolContext) -> None:
    out = _run(f"{_STATUS_ASK}\n\n{_FRAUD_FOOTER}", ctx)
    assert out.action is SensitiveAction.CONTINUE, out.evidence
    assert out.flags == [SensitiveFlag.NONE], out.evidence
    assert out.hard_bank is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "prohibition",
    [
        "Do not change payment instructions on wires or ACH.",
        "We will never change our remittance details by email.",
        "We cannot update our banking details over email.",
        "Circle should not change the remit to address without a call.",
        "We don't change payment instructions by email.",
    ],
)
def test_prohibitions_are_not_requests(prohibition: str, ctx: ToolContext) -> None:
    out = _run(f"{_STATUS_ASK}\n\n{prohibition}", ctx)
    assert out.hard_bank is False, out.evidence


# --- the narrowing must NOT hide a real instruction --------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "request_text",
    [
        "Please update our remittance to Wells Fargo, account 1234567890.",
        "Change the remit to address to RTS Financial, PO Box 840267.",
        "Kindly update bank details for all future payments.",
        "Please update our banking information for all future payments.",
        "Please do not hesitate to update our remittance details to the account below.",
    ],
)
def test_real_change_requests_still_escalate(request_text: str, ctx: ToolContext) -> None:
    """The last case is the trap: its 'do not' belongs to 'hesitate', not to 'update'."""

    out = _run(f"{_STATUS_ASK}\n\n{request_text}", ctx)
    assert out.action is SensitiveAction.ESCALATE, out.evidence
    assert SensitiveFlag.BANK_CHANGE in out.flags, out.evidence
    assert out.hard_bank is True, out.evidence


@pytest.mark.unit
def test_a_real_request_alongside_the_footer_still_escalates(ctx: ToolContext) -> None:
    """The dangerous case: boilerplate must not launder a genuine instruction beside it."""

    body = (
        f"{_STATUS_ASK}\n\n"
        "Also, please update our remittance to Wells Fargo account 1234567890.\n\n"
        f"{_FRAUD_FOOTER}"
    )
    out = _run(body, ctx)
    assert out.action is SensitiveAction.ESCALATE, out.evidence
    assert out.hard_bank is True, out.evidence


@pytest.mark.unit
def test_fraud_wording_alone_does_not_suppress(ctx: ToolContext) -> None:
    """Suppression keys on negation, never on fraud words.

    "Due to fraud we must update our ACH details" is simultaneously a genuine instruction
    and the classic fraud pretext. Had the narrowing keyed on nearby fraud vocabulary, this
    is the email it would have waved through.
    """

    out = _run(
        f"{_STATUS_ASK}\n\nDue to recent fraud we need to update our ACH details urgently.",
        ctx,
    )
    assert out.action is SensitiveAction.ESCALATE, out.evidence
    assert out.hard_bank is True, out.evidence
