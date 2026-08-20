"""Unit tests for the pre-send gate (§5) — one test per failure mode plus the happy path."""

from __future__ import annotations

from decimal import Decimal

import pytest

from payment_bot.gate import PreSendGate
from payment_bot.models import EmailAttachment, InboundEmail
from payment_bot.tools.base import ToolContext
from payment_bot.tools.submit import Citation, SubmitDraftOutput

# A well-grounded, authorized draft for load 2462934 (values match grounded_ctx).
_GOOD_BODY = (
    "Load 2462934 is BILLED. Both earning lines are Pending, totaling $4,650 "
    "($4,500 Brokerage Line Haul + $150 Truck Order Not Used). "
    "Scheduled payment date: Thursday, August 20, 2026."
)


def _draft(
    body: str = _GOOD_BODY,
    load_ids: list[str] | None = None,
    citations: list[Citation] | None = None,
) -> SubmitDraftOutput:
    return SubmitDraftOutput(
        reply_body=body,
        to="billing@ideaexpedited.com",
        load_ids=load_ids if load_ids is not None else ["2462934"],
        citations=citations or [],
    )


def _checks(result: object) -> dict[str, bool]:
    return {c.name: c.passed for c in result.checks}  # type: ignore[attr-defined]


@pytest.mark.unit
def test_happy_path_allows_send(grounded_ctx: ToolContext, sample_email: InboundEmail) -> None:
    result = PreSendGate().evaluate(draft=_draft(), email=sample_email, ctx=grounded_ctx)
    assert result.allowed, result.reasons
    assert all(_checks(result).values())


@pytest.mark.unit
def test_unauthorized_sender_is_blocked(grounded_ctx: ToolContext) -> None:
    stranger = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="scammer@gmail.com",
        subject="Payment status for 2462934",
        body="status?",
    )
    result = PreSendGate().evaluate(draft=_draft(), email=stranger, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["authorization"] is False


@pytest.mark.unit
def test_bank_change_is_blocked(grounded_ctx: ToolContext) -> None:
    fraud = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="billing@ideaexpedited.com",
        subject="Update banking information",
        body="Please update our bank account number and routing number for load 2462934.",
    )
    result = PreSendGate().evaluate(draft=_draft(), email=fraud, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["sensitive_change"] is False


@pytest.mark.unit
def test_noa_attachment_is_blocked(grounded_ctx: ToolContext) -> None:
    with_noa = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="billing@ideaexpedited.com",
        subject="Please add our factoring NOA",
        body="Attached is our notice of assignment to set up factoring.",
        attachments=[EmailAttachment(filename="ACME_NOA.pdf", mime_type="application/pdf")],
    )
    result = PreSendGate().evaluate(draft=_draft(), email=with_noa, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["sensitive_change"] is False


@pytest.mark.unit
def test_ungrounded_amount_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    body = _GOOD_BODY + " An extra fee of $9,999 applies."
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["grounding"] is False
    assert any("9999" in r for r in result.reasons)


@pytest.mark.unit
def test_ungrounded_date_is_blocked(grounded_ctx: ToolContext, sample_email: InboundEmail) -> None:
    body = "Load 2462934 will be paid on September 1, 2026."
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)
    assert not result.allowed
    assert _checks(result)["grounding"] is False
    assert any("2026-09-01" in r for r in result.reasons)


# --- Placeholders -----------------------------------------------------------
#: Verbatim from a live run against real carrier mail. This draft passed all five original
#: checks — including `grounding`, because it states no parseable amount for grounding to
#: object to — and was reported as DRAFT READY FOR REVIEW.
_PLACEHOLDER_BODY = (
    "Our carrier rate for load 2462934 is $XXX, which MATCHES/MISMATCHES the sender's "
    "stated amount of none. The gross total is $XXX, with the following earning lines: XXX. "
    "There are no deductions on file, or each deduction is: XXX. The net rate is $XXX. "
    "The invoice was generated: Yes/Not yet. NOA / factoring on file: XXX."
)


@pytest.mark.unit
def test_live_placeholder_draft_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """The regression this check exists for: an unfilled draft must never read as approved."""

    result = PreSendGate().evaluate(
        draft=_draft(body=_PLACEHOLDER_BODY), email=sample_email, ctx=grounded_ctx
    )
    assert not result.allowed
    assert _checks(result)["placeholders"] is False
    # Grounding still passes — which is precisely why a separate check is needed.
    assert _checks(result)["grounding"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("marker", "body"),
    [
        ("XXX", "Load 2462934: the carrier rate is XXX."),
        ("$XXX", "Load 2462934: the carrier rate is $XXX."),
        ("MATCHES/MISMATCHES", "Load 2462934 MATCHES/MISMATCHES your stated amount."),
        ("spaced alternation", "Load 2462934. Invoice generated: Yes / Not yet."),
        ("Yes/No", "Load 2462934. Invoice generated: Yes/No."),
        ("TBD", "Load 2462934 will be paid TBD."),
        ("angle", "Load 2462934: the rate is <carrier rate>."),
        ("brace", "Load 2462934: the rate is {carrier_rate}."),
        ("double brace", "Load 2462934: the rate is {{ carrier_rate }}."),
    ],
)
def test_each_placeholder_marker_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail, marker: str, body: str
) -> None:
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)
    assert _checks(result)["placeholders"] is False, marker
    assert not result.allowed


@pytest.mark.unit
def test_placeholder_in_a_citation_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """A clean body with a placeholder citation still means the model was emitting template."""

    result = PreSendGate().evaluate(
        draft=_draft(
            citations=[Citation(fact="earning lines", value="XXX", source_tool="tp_get_load_summary")]
        ),
        email=sample_email,
        ctx=grounded_ctx,
    )
    assert not result.allowed
    assert _checks(result)["placeholders"] is False
    assert any("citations" in r for r in result.reasons)


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        _GOOD_BODY,
        # An address in angle brackets must not read as a placeholder.
        _GOOD_BODY + " Reply to <billing@ideaexpedited.com> with questions.",
        # Nor ordinary prose that happens to contain a slash or a capital X.
        _GOOD_BODY + " Contact A/R for anything further. Reference X-9 on the invoice.",
    ],
)
def test_legitimate_drafts_pass_the_placeholder_check(
    grounded_ctx: ToolContext, sample_email: InboundEmail, body: str
) -> None:
    """A gate check that fires on a real reply is worse than no check at all."""

    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)
    assert _checks(result)["placeholders"] is True, result.reasons


@pytest.mark.unit
def test_invalid_length_load_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    result = PreSendGate().evaluate(
        draft=_draft(body="Load 12345 status.", load_ids=["12345"]),
        email=sample_email,
        ctx=grounded_ctx,
    )
    assert not result.allowed
    assert _checks(result)["length_routing"] is False


@pytest.mark.unit
def test_bulk_over_threshold_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    many = [f"246293{n}" for n in range(6)]  # 6 valid 7-digit ids > default threshold 5
    result = PreSendGate().evaluate(
        draft=_draft(body="Multiple loads.", load_ids=many),
        email=sample_email,
        ctx=grounded_ctx,
    )
    assert not result.allowed
    assert _checks(result)["bulk"] is False


@pytest.mark.unit
def test_factoring_blocked_by_default_but_allowed_by_policy(
    grounded_ctx: ToolContext, tp_client: object
) -> None:
    # Register a factoring sender against the load, then send from the factor.
    from payment_bot.models import AuthorizationContext
    from payment_bot.sample_data import build_load_2462934_fixture

    fixture = build_load_2462934_fixture()
    factored = fixture.__class__(
        load=fixture.load,
        dispatch=fixture.dispatch,
        settlement=fixture.settlement,
        files=fixture.files,
        authorization=AuthorizationContext(
            carrier_company="Idea Expedited, Inc",
            authorized_emails=(),
            factoring_company="England Carrier Services",
            factoring_emails=("ar@englandcarrier.com",),
        ),
    )
    tp_client.add(factored)  # type: ignore[attr-defined]
    factor_email = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="ar@englandcarrier.com",
        subject="Payment status 2462934",
        body="status?",
    )

    blocked = PreSendGate(allow_factoring=False).evaluate(
        draft=_draft(), email=factor_email, ctx=grounded_ctx
    )
    assert not blocked.allowed
    assert _checks(blocked)["authorization"] is False

    allowed = PreSendGate(allow_factoring=True).evaluate(
        draft=_draft(), email=factor_email, ctx=grounded_ctx
    )
    assert allowed.allowed, allowed.reasons


# --- Deduction signs ---------------------------------------------------------
@pytest.mark.unit
def test_a_deduction_written_positively_is_grounded(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Transport Pro returns deductions negative; a correct reply writes them positive.

    Live load 2496737 produced an accurate draft — "$225.00 gross, deduction of $11.25, net
    of $213.75" — and grounding blocked it because the ledger held -11.25. Sign is
    presentation, not provenance.
    """

    grounded_ctx.ledger.record_amount(Decimal("-11.25"), "tp_get_load_summary")
    body = "Gross is $4,650 with a deduction of $11.25."
    result = PreSendGate().evaluate(
        draft=_draft(body=body), email=sample_email, ctx=grounded_ctx
    )

    assert _checks(result)["grounding"] is True, result.reasons


@pytest.mark.unit
def test_an_invented_amount_is_still_ungrounded(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Comparing magnitudes must not let a figure no tool produced slip through."""

    grounded_ctx.ledger.record_amount(Decimal("-11.25"), "tp_get_load_summary")
    result = PreSendGate().evaluate(
        draft=_draft(body="A deduction of $99.99 applies."),
        email=sample_email,
        ctx=grounded_ctx,
    )

    assert _checks(result)["grounding"] is False
    assert any("99.99" in reason for reason in result.reasons)


# --- Configured factoring domains --------------------------------------------
def _factored_ctx(ctx: ToolContext, factoring_company: str, **settings_kw: object) -> ToolContext:
    """Register load 2462934 as factored by ``factoring_company``, with no contact emails.

    Empty `factoring_emails` is not a contrivance — it is what Transport Pro always returns.
    Neither the payment_information endpoint nor GET /carrier/{id} exposes the remit email
    the UI shows, so there is never an address to match a factoring sender against.
    """

    from payment_bot.config import Settings
    from payment_bot.models import AuthorizationContext
    from payment_bot.sample_data import build_load_2462934_fixture

    fixture = build_load_2462934_fixture()
    ctx.tp.add(  # type: ignore[attr-defined]
        fixture.__class__(
            load=fixture.load,
            dispatch=fixture.dispatch,
            settlement=fixture.settlement,
            files=fixture.files,
            authorization=AuthorizationContext(
                carrier_company="Logan Transportation Services Llc",
                authorized_emails=(),
                factoring_company=factoring_company,
                factoring_emails=(),
            ),
        )
    )
    return ToolContext(
        tp=ctx.tp,
        ledger=ctx.ledger,
        correlation_id=ctx.correlation_id,
        settings=Settings(**settings_kw),  # type: ignore[arg-type]
    )


def _from(address: str) -> InboundEmail:
    return InboundEmail(
        message_id="m", thread_id="t", from_email=address, subject="Rate 2462934", body="?"
    )


@pytest.mark.unit
def test_a_configured_factoring_domain_is_recognised(grounded_ctx: ToolContext) -> None:
    """RTS mails from ryanrts.com; the name on the load is "RTS Financial Service, Inc"."""

    ctx = _factored_ctx(
        grounded_ctx,
        "RTS Financial Service, Inc",
        factoring_domains={"rts financial": ("rtsfinancial.com", "ryanrts.com")},
    )
    result = PreSendGate(allow_factoring=True).evaluate(
        draft=_draft(), email=_from("rtssupport@ryanrts.com"), ctx=ctx
    )
    assert result.allowed, result.reasons


@pytest.mark.unit
def test_an_unconfigured_domain_that_merely_resembles_the_factor_is_denied(
    grounded_ctx: ToolContext,
) -> None:
    """The hole this closes.

    "RTS Financial Service" yields the token "financial", matched as a substring against the
    sender's flattened domain — so before this, an unrelated address at any domain containing
    "financial" was treated as the factor, and would have been disclosed to the moment
    factoring was enabled.
    """

    ctx = _factored_ctx(grounded_ctx, "RTS Financial Service, Inc", factoring_domains={})
    result = PreSendGate(allow_factoring=True).evaluate(
        draft=_draft(), email=_from("ap@my-financial-co.example"), ctx=ctx
    )
    assert not result.allowed
    assert _checks(result)["authorization"] is False
    assert any("not configured" in reason for reason in result.reasons)


@pytest.mark.unit
def test_one_factors_domain_does_not_answer_for_another(grounded_ctx: ToolContext) -> None:
    """RTS must not be answered about a load factored by OTR."""

    ctx = _factored_ctx(
        grounded_ctx,
        "OTR Solutions",
        factoring_domains={"rts financial": ("ryanrts.com",), "otr solutions": ("otrsolutions.com",)},
    )
    result = PreSendGate(allow_factoring=True).evaluate(
        draft=_draft(), email=_from("rtssupport@ryanrts.com"), ctx=ctx
    )
    assert not result.allowed


# --- Domain-level contact matching --------------------------------------------
def _contact_ctx(
    ctx: ToolContext, carrier_company: str, authorized_emails: tuple[str, ...]
) -> ToolContext:
    """Register load 2462934 with the given carrier and contact list, nothing factored."""

    from payment_bot.models import AuthorizationContext
    from payment_bot.sample_data import build_load_2462934_fixture

    fixture = build_load_2462934_fixture()
    ctx.tp.add(  # type: ignore[attr-defined]
        fixture.__class__(
            load=fixture.load,
            dispatch=fixture.dispatch,
            settlement=fixture.settlement,
            files=fixture.files,
            authorization=AuthorizationContext(
                carrier_company=carrier_company,
                authorized_emails=authorized_emails,
                factoring_company=None,
                factoring_emails=(),
            ),
        )
    )
    return ctx


@pytest.mark.unit
def test_sender_at_an_authorized_contacts_domain_is_allowed(grounded_ctx: ToolContext) -> None:
    """accounting@ answers for the company whose dispatch@ is on file.

    Observed live on load 2480109: the contact on file was at sky-expressllc.com and the
    payment question came from accounting@ the same domain — exact-address matching denied
    it. The carrier name here shares no token with the domain, so only the domain-level
    contact match can allow this.
    """

    ctx = _contact_ctx(grounded_ctx, "Mapz Logistics Llc", ("dispatch@sky-expressllc.com",))
    result = PreSendGate().evaluate(
        draft=_draft(), email=_from("accounting@sky-expressllc.com"), ctx=ctx
    )
    assert result.allowed, result.reasons


@pytest.mark.unit
def test_free_mail_domains_get_no_domain_level_trust(grounded_ctx: ToolContext) -> None:
    """A gmail.com contact on file must not authorize every gmail.com sender."""

    ctx = _contact_ctx(grounded_ctx, "Tee Group Llc", ("teegroup@gmail.com",))
    result = PreSendGate().evaluate(
        draft=_draft(), email=_from("someone-else@gmail.com"), ctx=ctx
    )
    assert not result.allowed
    assert _checks(result)["authorization"] is False


@pytest.mark.unit
def test_exact_free_mail_contact_still_allows(grounded_ctx: ToolContext) -> None:
    """The free-mail exclusion is domain-level only: the exact address on file still passes."""

    ctx = _contact_ctx(grounded_ctx, "Tee Group Llc", ("teegroup@gmail.com",))
    result = PreSendGate().evaluate(
        draft=_draft(), email=_from("teegroup@gmail.com"), ctx=ctx
    )
    assert result.allowed, result.reasons


# --- Tool mentions and coverage -------------------------------------------------
@pytest.mark.unit
def test_a_tool_name_in_the_body_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Backstop behind submit_draft's sanitizer — other routes to the gate stay covered."""

    body = _GOOD_BODY + " [tp_get_load_summary]"
    result = PreSendGate().evaluate(
        draft=_draft(body=body), email=sample_email, ctx=grounded_ctx
    )
    assert not result.allowed
    assert _checks(result)["tool_mentions"] is False


@pytest.mark.unit
def test_a_skipped_load_fails_coverage(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Observed live: a carrier asked about two loads; the draft silently answered one."""

    result = PreSendGate().evaluate(
        draft=_draft(),  # answers 2462934 only
        email=sample_email,
        ctx=grounded_ctx,
        expected_load_ids=("2462934", "2193195"),
    )
    assert not result.allowed
    assert _checks(result)["coverage"] is False
    assert any("2193195" in reason for reason in result.reasons)


@pytest.mark.unit
def test_a_hold_reply_naming_the_load_passes_coverage(grounded_ctx: ToolContext) -> None:
    """A hold reply discloses nothing but must still show it addressed the load."""

    hold = _draft(body="Load 2462934 is under review; we will follow up shortly.", load_ids=[])
    result = PreSendGate().evaluate(
        draft=hold,
        email=_from("billing@ideaexpedited.com"),
        ctx=grounded_ctx,
        expected_load_ids=("2462934",),
    )
    assert _checks(result)["coverage"] is True


@pytest.mark.unit
def test_no_expected_loads_means_coverage_is_not_enforced(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """The code-authored bulk portal reply names no load, deliberately."""

    result = PreSendGate().evaluate(
        draft=_draft(body="You can check these on the portal.", load_ids=[]),
        email=sample_email,
        ctx=grounded_ctx,
        expected_load_ids=None,
    )
    assert _checks(result)["coverage"] is True


@pytest.mark.unit
def test_a_configured_domain_is_still_subject_to_the_policy_switch(
    grounded_ctx: ToolContext,
) -> None:
    """Recognising the factor is not the same as choosing to answer them."""

    ctx = _factored_ctx(
        grounded_ctx,
        "RTS Financial Service, Inc",
        factoring_domains={"rts financial": ("ryanrts.com",)},
    )
    result = PreSendGate(allow_factoring=False).evaluate(
        draft=_draft(), email=_from("rtssupport@ryanrts.com"), ctx=ctx
    )
    assert not result.allowed
    assert _checks(result)["authorization"] is False


@pytest.mark.unit
def test_a_subdomain_or_lookalike_is_not_the_configured_domain(
    grounded_ctx: ToolContext,
) -> None:
    """Whole-domain equality, never a substring — "ryanrts.com.evil.co" is not RTS."""

    ctx = _factored_ctx(
        grounded_ctx,
        "RTS Financial Service, Inc",
        factoring_domains={"rts financial": ("ryanrts.com",)},
    )
    for address in ("x@ryanrts.com.evil.co", "x@notryanrts.com", "x@ryanrts.co"):
        result = PreSendGate(allow_factoring=True).evaluate(
            draft=_draft(), email=_from(address), ctx=ctx
        )
        assert not result.allowed, address


# --- Change acknowledgment (§7 compensating control) ----------------------------
@pytest.mark.unit
def test_a_draft_acknowledging_a_remit_change_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    body = _GOOD_BODY + " We have updated the remittance to OTR Solutions as requested."
    result = PreSendGate().evaluate(
        draft=_draft(body=body), email=sample_email, ctx=grounded_ctx
    )
    assert not result.allowed
    assert _checks(result)["change_acknowledgment"] is False


@pytest.mark.unit
def test_ordinary_factoring_facts_pass_the_acknowledgment_check(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Stating who a load is payable to is reporting, not acknowledging a change."""

    result = PreSendGate().evaluate(
        draft=_draft(), email=sample_email, ctx=grounded_ctx
    )
    assert _checks(result)["change_acknowledgment"] is True


@pytest.mark.unit
def test_soft_boilerplate_email_passes_the_gate_with_a_clean_draft(
    grounded_ctx: ToolContext,
) -> None:
    """§7: the OTR template proceeds; the gate polices the draft instead of the email."""

    boilerplate = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="billing@ideaexpedited.com",
        subject="Rate Verification - Load 2462934",
        body=(
            "Please verify the rate.\nPlease ensure remittance is updated to OTR Solutions "
            "or advise if a Notice of Assignment is required."
        ),
    )
    result = PreSendGate().evaluate(draft=_draft(), email=boilerplate, ctx=grounded_ctx)
    assert result.allowed, result.reasons
    assert _checks(result)["sensitive_change"] is True


# --- check #10: NOA requests need the intake's say-so -------------------------
_NOA_ASK = (
    " Since this is a factoring arrangement, please email your Notice of Assignment "
    "and billing paperwork to freightpay@circledelivers.com."
)


@pytest.mark.unit
def test_an_uninstructed_noa_request_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Observed live: the factor and NOA were already on file, yet the draft asked the
    sender to email their NOA. Only the intake's pre-NOA instruction legitimises that ask."""

    result = PreSendGate().evaluate(
        draft=_draft(body=_GOOD_BODY + _NOA_ASK), email=sample_email, ctx=grounded_ctx
    )
    assert not result.allowed
    assert _checks(result)["noa_request"] is False


@pytest.mark.unit
def test_an_instructed_noa_request_passes(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    result = PreSendGate().evaluate(
        draft=_draft(body=_GOOD_BODY + _NOA_ASK),
        email=sample_email,
        ctx=grounded_ctx,
        noa_request_expected=True,
    )
    assert result.allowed, result.reasons
    assert _checks(result)["noa_request"] is True


@pytest.mark.unit
def test_reporting_noa_facts_is_not_a_request(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """The rate skill must state what is on file; statements are not paperwork asks."""

    body = (
        _GOOD_BODY + " WEX Fleet One is on file as the factoring company and a notice "
        "of assignment is on file."
    )
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)
    assert result.allowed, result.reasons
    assert _checks(result)["noa_request"] is True


# ---------------------------------------------------------------------------
# The sender's own figures. Quotable, because a rate dispute has to print theirs
# beside ours — but never on their own, and never as though a tool produced them.
# ---------------------------------------------------------------------------
def _detail(result: object, name: str) -> str:
    return next(c.detail for c in result.checks if c.name == name)  # type: ignore[attr-defined]


@pytest.mark.unit
def test_a_sender_stated_amount_does_not_read_as_ungrounded(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """A figure the sender wrote is not a figure nobody can account for."""

    grounded_ctx.ledger.record_sender_amount(
        Decimal("2850"), "extract_identifiers", load_id="2462934"
    )
    body = _GOOD_BODY + " This differs from the $2,850 in your message."
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)

    assert _checks(result)["grounding"] is True, result.reasons
    assert "quoted from the sender" in _detail(result, "grounding")
    assert "2850" in _detail(result, "grounding")


@pytest.mark.unit
def test_a_sender_amount_beside_our_own_is_allowed(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """The rate dispute this permission exists for."""

    grounded_ctx.ledger.record_sender_amount(
        Decimal("2850"), "extract_identifiers", load_id="2462934"
    )
    body = _GOOD_BODY + " This differs from the $2,850 in your message."
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)

    assert result.allowed, result.reasons
    assert _checks(result)["sender_amount_attribution"] is True


@pytest.mark.unit
def test_a_sender_amount_standing_alone_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """The shape the split exists to catch.

    Before the ledger separated provenance this grounded cleanly: the number was in
    ``grounded_amounts`` because intake had put it there, and nothing recorded that intake
    had only read it off the sender's own email.
    """

    grounded_ctx.ledger.record_sender_amount(
        Decimal("300"), "extract_identifiers", load_id="2462934"
    )
    body = "Load 2462934: the carrier rate on file is $300."
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)

    assert not result.allowed
    assert _checks(result)["sender_amount_attribution"] is False
    assert _checks(result)["grounding"] is True  # which is why a separate check is needed
    assert any("only the sender asserted" in r for r in result.reasons)


@pytest.mark.unit
def test_an_amount_both_sides_state_is_not_sender_only(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """The agreeing case, which is most of them, never reaches the failing branch."""

    grounded_ctx.ledger.record_sender_amount(
        Decimal("4650"), "extract_identifiers", load_id="2462934"
    )
    result = PreSendGate().evaluate(draft=_draft(), email=sample_email, ctx=grounded_ctx)

    assert result.allowed, result.reasons
    assert _checks(result)["sender_amount_attribution"] is True
    assert "quoted from the sender" not in _detail(result, "grounding")


# ---------------------------------------------------------------------------
# The pre-NOA ask is not an acknowledgment. Two checks read the same sentence
# and used to disagree about it.
# ---------------------------------------------------------------------------
_PRE_NOA_BODY = (
    "Load 2462934 has not yet been billed. To get this set up, please email the NOA and "
    "all billing paperwork to freightpay@circledelivers.com."
)


@pytest.mark.unit
def test_the_sanctioned_noa_ask_is_not_an_acknowledgment(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Live block on load 2546075.

    ``change_acknowledgment`` failed on "set up, please email the NOA" in the very run where
    ``noa_request`` passed the same words as "NOA request present, per the intake".
    """

    result = PreSendGate().evaluate(
        draft=_draft(body=_PRE_NOA_BODY),
        email=sample_email,
        ctx=grounded_ctx,
        noa_request_expected=True,
    )

    assert _checks(result)["change_acknowledgment"] is True, result.reasons
    assert _checks(result)["noa_request"] is True
    assert result.allowed, result.reasons


@pytest.mark.unit
def test_an_unsanctioned_noa_ask_still_fails_both_checks(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """The exemption is the intake's to grant, not the wording's."""

    result = PreSendGate().evaluate(
        draft=_draft(body=_PRE_NOA_BODY),
        email=sample_email,
        ctx=grounded_ctx,
        noa_request_expected=False,
    )

    assert not result.allowed
    assert _checks(result)["noa_request"] is False
    assert _checks(result)["change_acknowledgment"] is False


@pytest.mark.unit
def test_claiming_the_noa_was_set_up_is_still_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """The shape the check exists for, on the flow that is allowed to ask for one."""

    result = PreSendGate().evaluate(
        draft=_draft(body="Load 2462934: we have set up your NOA as requested."),
        email=sample_email,
        ctx=grounded_ctx,
        noa_request_expected=True,
    )

    assert not result.allowed
    assert _checks(result)["change_acknowledgment"] is False
    assert any("set up your NOA" in r for r in result.reasons)


@pytest.mark.unit
def test_an_acknowledgment_beside_the_sanctioned_ask_is_caught(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Why the exemption is scoped to the matching span and not to the whole draft."""

    result = PreSendGate().evaluate(
        draft=_draft(body=_PRE_NOA_BODY + " We have also added the NOA to the file."),
        email=sample_email,
        ctx=grounded_ctx,
        noa_request_expected=True,
    )

    assert not result.allowed
    assert _checks(result)["change_acknowledgment"] is False
    assert any("added the NOA" in r for r in result.reasons)
    # The sanctioned ask is not what was reported.
    assert not any("please email the NOA" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Answering "confirm all payments will be made to X" is confirming it, even
# with no change verb anywhere in the sentence.
# ---------------------------------------------------------------------------
_ASKS_TO_CONFIRM_REMIT = (
    "Accounts payable,\n\nPlease provide payment status on the invoices below and confirm "
    "all payments will be made to RTS Financial Service P.O. Box 840267 Dallas, TX "
    "75284-0267.\n\nThanks,\nMax"
)


def _remit_email(sample_email: InboundEmail) -> InboundEmail:
    return sample_email.model_copy(update={"body": _ASKS_TO_CONFIRM_REMIT})


@pytest.mark.unit
def test_agreeing_to_a_remit_address_is_blocked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Live on an RTS statement, 2026-08-20.

    The sender asked us to confirm a P.O. Box. The draft answered "payment will be directed
    accordingly" — no change verb, so bank phrases, change wording and NOA action all came
    back empty and the reply confirmed a remit address on the way past.
    """

    body = (
        "Load 2462934 is pending. We have your NOA on file for RTS Financial Service, Inc., "
        "so once the load is cleared and settles, payment will be directed accordingly."
    )
    result = PreSendGate().evaluate(
        draft=_draft(body=body), email=_remit_email(sample_email), ctx=grounded_ctx
    )

    assert not result.allowed
    assert _checks(result)["change_acknowledgment"] is False
    assert any("payment direction" in r for r in result.reasons)


@pytest.mark.unit
def test_a_payment_date_is_not_a_payment_direction(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """The false positive this arm must never produce.

    "Payment will be issued on Thursday" is the answer this inbox exists to give. It differs
    from the blocked sentence only in what follows the verb — a date, not a payee — and it is
    said under the very same confirmation request.
    """

    body = "Load 2462934 is BILLED. Payment will be issued on Thursday, August 20, 2026."
    result = PreSendGate().evaluate(
        draft=_draft(body=body), email=_remit_email(sample_email), ctx=grounded_ctx
    )

    assert _checks(result)["change_acknowledgment"] is True, result.reasons


@pytest.mark.unit
def test_payment_direction_is_only_policed_when_the_sender_asked(
    grounded_ctx: ToolContext, sample_email: InboundEmail
) -> None:
    """Armed by the inbound. On a thread where nobody asked, the arm stays down."""

    body = (
        "Load 2462934 is pending. We have your NOA on file for RTS Financial Service, Inc., "
        "so once the load is cleared and settles, payment will be directed accordingly."
    )
    result = PreSendGate().evaluate(draft=_draft(body=body), email=sample_email, ctx=grounded_ctx)

    assert _checks(result)["change_acknowledgment"] is True, result.reasons


@pytest.mark.unit
def test_confirm_the_payment_status_is_not_a_remit_request() -> None:
    """The commonest sentence in this inbox must never arm the check."""

    from payment_bot.gate.presend import _confirms_payment_direction

    ordinary = InboundEmail(
        message_id="m",
        thread_id="t",
        from_email="billing@ideaexpedited.com",
        body="Could you confirm the payment status for load 2462934?",
    )
    agreeing = "Payment will be remitted to the address on file."

    assert _confirms_payment_direction(agreeing, ordinary) is None


# ---------------------------------------------------------------------------
# Loads the sender named that the reply does not cover.
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_the_withheld_line_never_leaks_the_count_or_the_ids() -> None:
    """The agent is told how many so it knows the situation; the reply must not say."""

    from payment_bot.agent.skills import _withheld_line

    assert _withheld_line(0) == []
    line = _withheld_line(3)[0]
    assert "3" not in line
    assert "do not name" in line.lower()
    assert "not addressed here" in line.lower()
