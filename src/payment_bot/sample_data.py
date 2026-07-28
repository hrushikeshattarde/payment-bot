"""Canonical sample data for the demo and tests.

Built around load **2462934** — the real Transport Pro payload from PRD §4.3.0 — plus
internally-consistent auxiliary rows (dispatch/settlement/files) and an authorized
sender. Living in ``src`` (not ``tests``) so the demo runner can import it directly.

The raw §4.3.0 payload is also mirrored verbatim at ``tests/fixtures/loads/2462934.json``
for a model round-trip test; this module is the assembled multi-endpoint fixture.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from payment_bot.clients import (
    LlmResponse,
    LoadFixture,
    MockGmailClient,
    MockTransportProClient,
    ScriptedLlmClient,
    ToolUseBlock,
)
from payment_bot.models import (
    AuthorizationContext,
    DispatchRow,
    FileDocument,
    InboundEmail,
    NoaFactoring,
    TransportProLoad,
)

#: The authoritative §4.3.0 payload, verbatim.
LOAD_2462934_PAYLOAD: dict[str, Any] = {
    "load_id": 2462934,
    "billing_status": "BILLED",
    "account_information": {
        "company_name": "Idea Expedited, Inc",
        "dot_number": "2363192",
        "mc_number": None,
        "address": "5858 W Addison St",
        "city": "Chicago",
        "state": "IL",
        "zip": "60634",
        "remit_to": {"send_payment_to": "self", "company_name": "Idea Expedited, Inc"},
    },
    "deductions": [],
    "earnings": [
        {
            "title": "TRUCK ORDER NOT USED",
            "amount": 150,
            "payment_status": "Pending",
            "settlement_id": None,
            "estimated_payment_date": "2026-08-19",
            "actual_payment_date": None,
            "payment_method": None,
            "check_number": None,
        },
        {
            "title": "Brokerage Line Haul",
            "amount": 4500,
            "payment_status": "Pending",
            "settlement_id": None,
            "estimated_payment_date": "2026-08-19",
            "actual_payment_date": None,
            "payment_method": None,
            "check_number": None,
        },
    ],
    "shipment_information": {
        "waypoints": [
            {
                "type": "Pickup",
                "city": "Spokane",
                "state": "Washington",
                "date": {"timestamp": "2026-06-23T16:24:00Z", "timezone": "PDT"},
            },
            {
                "type": "Delivery",
                "city": "Lithia Springs",
                "state": "Georgia",
                "date": {"timestamp": "2026-06-29T16:00:00Z", "timezone": "EDT"},
            },
        ]
    },
}

#: The carrier contact authorized to receive disclosure for this load.
SAMPLE_SENDER_EMAIL = "billing@ideaexpedited.com"


def build_load_2462934_fixture() -> LoadFixture:
    """Assemble the full multi-endpoint fixture for load 2462934."""

    load = TransportProLoad.model_validate(LOAD_2462934_PAYLOAD)
    return LoadFixture(
        load=load,
        dispatch=[
            DispatchRow(
                carrier_name="Idea Expedited, Inc",
                mc_number=None,
                freight_bill=Decimal("4650"),
                dispatch_status="Delivered",
                pickup="Spokane, WA",
                delivery="Lithia Springs, GA",
                comment="Delivered on time",
                last_updated="2026-06-29",
            ),
        ],
        settlement=[],  # Pending → no settlement entries yet
        files=[
            FileDocument(
                file_type="Carrier Invoice",
                index_date=date(2026, 7, 1),
                upload_date=date(2026, 7, 1),
                indexed_by="system",
                comments="Invoice number 4540 Load Number 2462934",
            ),
            FileDocument(
                file_type="Bill Of Lading",
                index_date=date(2026, 6, 30),
                upload_date=date(2026, 6, 30),
                indexed_by="system",
                comments="POD Load Number 2462934",
            ),
        ],
        noa_factoring=NoaFactoring(
            noa_on_file=False,
            factoring_company_on_file=None,
            details="Remit-to self; no NOA or factoring company on file.",
        ),
        authorization=AuthorizationContext(
            carrier_company="Idea Expedited, Inc",
            authorized_emails=(SAMPLE_SENDER_EMAIL,),
            factoring_company=None,
            factoring_emails=(),
        ),
    )


def sample_transport_pro_client() -> MockTransportProClient:
    """A Transport Pro client preloaded with the sample fixture."""

    return MockTransportProClient({"2462934": build_load_2462934_fixture()})


def sample_payment_status_email() -> InboundEmail:
    """A clean, authorized payment-status inquiry for load 2462934."""

    return InboundEmail(
        message_id="msg-2462934-001",
        thread_id="thread-2462934",
        from_email=SAMPLE_SENDER_EMAIL,
        from_name="Idea Expedited Billing",
        subject="Payment status for load 2462934",
        body=(
            "Hello,\n\n"
            "Could you tell me the payment status and expected pay date for load 2462934?\n\n"
            "Thanks,\nIdea Expedited Billing"
        ),
        thread_text="",
    )


def sample_gmail_client() -> MockGmailClient:
    """A Gmail client with the sample email already in the inbox."""

    return MockGmailClient(inbox=[sample_payment_status_email()])


#: A fully-grounded reply for load 2462934. Every amount ($4,500 / $150 / $4,650) and the
#: date (Thursday, August 20, 2026) traces to a tool result, so it passes the gate.
PAYMENT_STATUS_DRAFT_BODY = (
    "Hello,\n\n"
    "Here is the payment status for load 2462934 (status: BILLED).\n\n"
    "- Brokerage Line Haul: $4,500 - Pending\n"
    "- Truck Order Not Used: $150 - Pending\n\n"
    "Total pending: $4,650. Scheduled payment date: Thursday, August 20, 2026 "
    "(carriers are paid on Mondays and Thursdays). A payment method has not been "
    "assigned yet.\n\n"
    "The delivered carrier on file is Idea Expedited, Inc, and the load has not settled "
    "yet.\n\n"
    "Best regards,\nCircle Delivers Payments"
)


def scripted_payment_status_llm() -> ScriptedLlmClient:
    """A scripted model that plays the §7.4 payment-status tool sequence for 2462934.

    Used by the demo and the integration test to exercise the whole loop deterministically,
    with no Bedrock call. Swap in ``BedrockLlmClient`` for live behaviour.
    """

    lid = "2462934"

    def tu(index: int, name: str, payload: dict[str, Any]) -> ToolUseBlock:
        return ToolUseBlock(tool_use_id=f"tu-{index}", name=name, input=payload)

    def turn(*blocks: ToolUseBlock) -> LlmResponse:
        return LlmResponse(stop_reason="tool_use", content=list(blocks))

    responses = [
        turn(tu(1, "tp_get_load_summary", {"load_id": lid})),
        turn(  # one compute call per earning line
            tu(2, "compute_scheduled_pay_date", {"estimated_payment_date": "2026-08-19", "load_id": lid}),
            tu(3, "compute_scheduled_pay_date", {"estimated_payment_date": "2026-08-19", "load_id": lid}),
        ),
        turn(
            tu(4, "tp_get_dispatch_history", {"load_id": lid}),
            tu(5, "carrier_cross_check", {"load_id": lid, "system": "transport_pro"}),
        ),
        turn(tu(6, "tp_get_settlement_entries", {"load_id": lid})),
        turn(
            tu(
                7,
                "check_authorization",
                {
                    "sender_email": SAMPLE_SENDER_EMAIL,
                    "sender_name": "Idea Expedited Billing",
                    "load_id": lid,
                    "system": "transport_pro",
                },
            )
        ),
        turn(
            tu(
                8,
                "submit_draft",
                {
                    "reply_body": PAYMENT_STATUS_DRAFT_BODY,
                    "to": SAMPLE_SENDER_EMAIL,
                    "load_ids": [lid],
                    "citations": [
                        {
                            "fact": "total pending",
                            "value": "$4,650",
                            "source_tool": "tp_get_load_summary",
                        },
                        {
                            "fact": "scheduled pay date",
                            "value": "2026-08-20",
                            "source_tool": "compute_scheduled_pay_date",
                        },
                    ],
                },
            )
        ),
    ]
    return ScriptedLlmClient(responses=responses)


def sample_rate_verification_email() -> InboundEmail:
    """An authorized rate-verification inquiry for load 2462934 stating a MATCHING amount."""

    return InboundEmail(
        message_id="msg-2462934-rate-001",
        thread_id="thread-2462934-rate",
        from_email=SAMPLE_SENDER_EMAIL,
        from_name="Idea Expedited Billing",
        subject="Rate Verification - Load 2462934",
        body=(
            "Hello,\n\n"
            "Please verify the rate for load 2462934. Our records show $4,650. "
            "Can you also confirm there are no deductions, whether the invoice was "
            "generated, and that no factoring company is on file?\n\n"
            "Thanks,\nIdea Expedited Billing"
        ),
        thread_text="",
    )


#: A fully-grounded rate-verification reply for load 2462934 (matches the stated $4,650).
RATE_VERIFICATION_DRAFT_BODY = (
    "Hello,\n\n"
    "Rate verification for load 2462934:\n\n"
    "Our carrier rate is $4,650, made up of:\n"
    "- Brokerage Line Haul: $4,500\n"
    "- Truck Order Not Used: $150\n\n"
    "This matches your stated amount of $4,650.\n\n"
    "Deductions: no deductions on file, so the net rate is $4,650.\n\n"
    "Invoice generated: Yes (the load is BILLED).\n\n"
    "Factoring/NOA: no notice of assignment or factoring company is on file; payment "
    "remits to Idea Expedited, Inc directly.\n\n"
    "Best regards,\nCircle Delivers Payments"
)


def scripted_rate_verification_llm() -> ScriptedLlmClient:
    """A scripted model that plays the §3.2 rate-verification tool sequence for 2462934."""

    lid = "2462934"

    def tu(index: int, name: str, payload: dict[str, Any]) -> ToolUseBlock:
        return ToolUseBlock(tool_use_id=f"tu-{index}", name=name, input=payload)

    def turn(*blocks: ToolUseBlock) -> LlmResponse:
        return LlmResponse(stop_reason="tool_use", content=list(blocks))

    responses = [
        turn(tu(1, "tp_get_load_summary", {"load_id": lid})),
        turn(tu(2, "compute_carrier_rate", {"load_id": lid})),
        turn(
            tu(3, "tp_get_dispatch_history", {"load_id": lid}),
            tu(4, "carrier_cross_check", {"load_id": lid, "system": "transport_pro"}),
        ),
        turn(tu(5, "tp_get_settlement_entries", {"load_id": lid})),
        turn(tu(6, "tp_get_noa_factoring", {"load_id": lid})),
        turn(tu(7, "tp_get_file_history", {"load_id": lid})),
        turn(
            tu(
                8,
                "check_authorization",
                {
                    "sender_email": SAMPLE_SENDER_EMAIL,
                    "sender_name": "Idea Expedited Billing",
                    "load_id": lid,
                    "system": "transport_pro",
                },
            )
        ),
        turn(
            tu(
                9,
                "submit_draft",
                {
                    "reply_body": RATE_VERIFICATION_DRAFT_BODY,
                    "to": SAMPLE_SENDER_EMAIL,
                    "load_ids": [lid],
                    "citations": [
                        {
                            "fact": "carrier rate (gross)",
                            "value": "$4,650",
                            "source_tool": "compute_carrier_rate",
                        },
                        {
                            "fact": "sender stated amount",
                            "value": "$4,650",
                            "source_tool": "extract_identifiers",
                        },
                    ],
                },
            )
        ),
    ]
    return ScriptedLlmClient(responses=responses)
