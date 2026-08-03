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


# --- Phrases must match as whole words -------------------------------------
#: Real subjects and bodies from live mail that escalated on the substring "ach" alone,
#: with no other sensitive signal present. Each is ordinary payment correspondence.
@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "Please see attached invoice for load 2462934.",
        "Attached is our paperwork request.",
        "Please send the status of each load listed below.",
        "Let me know if you cannot reach the dispatcher.",
        "Our approach is to invoice weekly.",
    ],
)
def test_ach_does_not_match_inside_other_words(ctx: ToolContext, body: str) -> None:
    """"attached" / "each" / "reach" are not bank-change requests."""

    out = _run(ctx, subject="Payment status", body=body)
    assert out.action is SensitiveAction.CONTINUE, out.evidence
    assert out.flags == [SensitiveFlag.NONE]


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "Please remit by ACH going forward.",
        "We have switched to ach payments.",
        "Send via ACH/EFT to the account below.",
    ],
)
def test_ach_still_escalates_as_a_whole_word(ctx: ToolContext, body: str) -> None:
    """The fix must not blind the detector to a genuine ACH instruction."""

    out = _run(ctx, subject="Payment", body=body)
    assert out.action is SensitiveAction.ESCALATE
    assert SensitiveFlag.BANK_CHANGE in out.flags


# --- Only what the sender wrote, and only actual requests --------------------
@pytest.mark.unit
def test_quoted_thread_is_not_scanned(ctx: ToolContext) -> None:
    """Verbatim from live mail: two emails escalated on our own quoted reply.

    "Payment Method Direct deposit" was in Angelica's earlier message, quoted back. Nobody
    was requesting anything, and scanning quoted history re-escalates a thread forever.
    """

    out = _run(
        ctx,
        subject="Re: New payment request from American Cross Dock",
        body=(
            "Any update on this one?\n\n"
            "On Mon, Jul 20, 2026 at 2:51 PM Angelica Baracao wrote:\n"
            "> Good afternoon,\n"
            "> Settle Date 07/20/2026\n"
            "> Amount $427.50\n"
            "> Payment Method Direct deposit\n"
        ),
    )
    assert out.action is SensitiveAction.CONTINUE, out.evidence
    assert out.flags == [SensitiveFlag.NONE]


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        # A factor's standard remit-to footer. Nothing is being changed.
        "Please confirm the rate on load 2462934.\n\nACH: Routing Number: 064103833 "
        "Account Number: 7362679545 Bank Name: Fifth Third Bank",
        # Ordinary AP requests that merely name a payment detail.
        "Please send us the remittance advice for load 2462934.",
        "What is the rate? Payment method is ACH as usual.",
        "Confirm the rate on 2462934. Our direct deposit is already on file.",
    ],
)
def test_naming_a_payment_detail_is_not_a_change_request(ctx: ToolContext, body: str) -> None:
    """The core of it: asking about a rate must not be refused because a footer says "ACH"."""

    out = _run(ctx, subject="Rate verification", body=body)
    assert out.action is SensitiveAction.CONTINUE, out.evidence
    assert out.flags == [SensitiveFlag.NONE]


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "Please update our remittance to OTR Solutions.",
        "Our routing number has changed, see below.",
        "Going forward, use the new account number 123456789.",
        "Please remit via ACH to the account below: 7362679545",
        "We have switched banks — updated direct deposit details attached.",
        "Kindly update payment info before the next settlement.",
    ],
)
def test_an_actual_change_request_still_escalates(ctx: ToolContext, body: str) -> None:
    """Narrowing must not blind the detector to someone asking to move the money."""

    out = _run(ctx, subject="Payment", body=body)
    assert out.action is SensitiveAction.ESCALATE, body
    assert SensitiveFlag.BANK_CHANGE in out.flags


@pytest.mark.unit
def test_a_change_request_in_the_sender_text_still_fires_above_a_quote(
    ctx: ToolContext,
) -> None:
    """Stripping the quote must not strip the request that precedes it."""

    out = _run(
        ctx,
        subject="Re: Invoice",
        body=(
            "Please update our bank account for future payments.\n\n"
            "On Mon, Jul 20, 2026 at 2:51 PM someone wrote:\n"
            "> earlier message\n"
        ),
    )
    assert out.action is SensitiveAction.ESCALATE
    assert SensitiveFlag.BANK_CHANGE in out.flags


@pytest.mark.unit
def test_genuine_bank_change_still_escalates_after_the_fix(ctx: ToolContext) -> None:
    """The case that matters most: a real fraud attempt must still be caught."""

    out = _run(
        ctx,
        subject="Updated remittance details",
        body=(
            "Please note our new bank account number is 123456789 and the routing number "
            "is 987654321. Kindly update payment info before the next settlement."
        ),
    )
    assert out.action is SensitiveAction.ESCALATE
    assert SensitiveFlag.BANK_CHANGE in out.flags


# --- NOA word boundaries -------------------------------------------------------
@pytest.mark.unit
def test_a_factors_own_name_is_not_an_noa_action(ctx: ToolContext) -> None:
    """Verbatim from live mail: "Aladdin Factoring" matched `add…Factor` by substring.

    A rate-verification request signed with the factor's name escalated as an NOA setup
    change — and would have re-escalated on every email that company ever sent.
    """

    out = _run(
        ctx,
        subject="Verification - 2524681",
        body=(
            "Hello,\nCan you please verify the rate on the below load and confirm if any "
            "advances or deductions have been taken?\n\nLoad: 2524681\nNet Pay: $1,550.00\n\n"
            "Thank you,\nAladdin Factoring"
        ),
    )
    assert SensitiveFlag.NOA_SETUP_CHANGE not in out.flags
    assert out.action is SensitiveAction.CONTINUE


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "Please add our NOA and set up factoring for these loads.",
        "Attached is a copy of our notice of assignment.",
        "We have updated the factoring on this account.",
        "Kindly register the attached NOA.",
    ],
)
def test_genuine_noa_actions_still_escalate(ctx: ToolContext, body: str) -> None:
    out = _run(ctx, body=body)
    assert SensitiveFlag.NOA_SETUP_CHANGE in out.flags


# --- §7 hard/soft evidence -------------------------------------------------------
_OTR_TEMPLATE = (
    "Hello, Please review the information below, if correct CLICK HERE to confirm.\n"
    "Please advise if the load has delivered without incident and carrier is in good "
    "standing.\nPlease ensure remittance is updated to OTR Solutions or advise if a "
    "Notice of Assignment is required.\n\nCarrier Name: G & A Trucking LLC (MC-1207945)"
)


@pytest.mark.unit
def test_factoring_template_boilerplate_is_soft(ctx: ToolContext) -> None:
    """The OTR rate-verification template, verbatim from live mail (§7's 5-of-6 case)."""

    out = _run(ctx, subject="Rate Verification - Load #2516049", body=_OTR_TEMPLATE)
    assert SensitiveFlag.BANK_CHANGE in out.flags
    assert out.hard is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("", "Please confirm that the payment remit address has been updated."),
        ("Update banking information", "New details below."),
        ("", "Please add our NOA and set up factoring."),
    ],
)
def test_explicit_signals_stay_hard(ctx: ToolContext, subject: str, body: str) -> None:
    out = _run(ctx, subject=subject, body=body)
    assert out.hard is True


@pytest.mark.unit
def test_a_void_check_attachment_stays_hard(ctx: ToolContext) -> None:
    out = DetectSensitiveChange().run(
        DetectSensitiveChangeInput(
            subject="Rate verification",
            body="Verify the rate please.",
            attachments_metadata=[AttachmentMeta(filename="voidcheck.pdf")],
        ),
        ctx,
    )
    assert out.hard is True
    assert out.paperwork is True
    assert out.noa_attachment is False


@pytest.mark.unit
def test_an_noa_attachment_is_its_own_signal_not_paperwork(ctx: ToolContext) -> None:
    """The England Carrier Services shape: a routine rate verification with the NOA
    attached as part of the standard pre-funding packet. Hard evidence, escalates by
    default — but reported as ``noa_attachment`` so ``noa_attachment_replies`` can
    draft past it, unlike a void check.
    """

    out = _run(
        ctx,
        subject="PLEASE REPLY Rate Verification Carrier Reuben and Gilbert Trucking LLC",
        body="Are you showing England Carrier Services as the factoring company?",
        attachments_metadata=[AttachmentMeta(filename="NOA - Reuben and Gilbert Trucking.pdf")],
    )
    assert out.action is SensitiveAction.ESCALATE
    assert SensitiveFlag.NOA_SETUP_CHANGE in out.flags
    assert out.noa_attachment is True
    assert out.paperwork is False
    assert out.hard is True


@pytest.mark.unit
def test_confirming_existing_payment_details_is_not_a_change(ctx: ToolContext) -> None:
    """WEX's payment-inquiry template, verbatim from live mail.

    Asking us to CONFIRM payment went to their existing remit address is verification —
    the most common factor template shape — not a ratify-the-change ask. An earlier
    confirm-pattern (confirm + any payment noun) escalated every email WEX ever sent.
    """

    out = _run(
        ctx,
        subject="Payment Inquiry CIRCLE LOGISTICS, INC (IN) (7 DIGIT LOAD#S)",
        body=(
            "If payment has been issued to WEX please also provide the below items in order "
            "to expedite the payment processing.\nPlease confirm the following payment "
            "information:\nConfirm payment going to Wex Bank P.O. Box 94565 Cleveland Ohio "
            "44101\nCheck number\nDate issued/Date Mailed\nCheck Amount\nCopy of cleared "
            "check, if the check has been cashed."
        ),
    )
    assert out.hard is False
