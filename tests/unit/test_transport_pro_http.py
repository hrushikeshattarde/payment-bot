"""Unit tests for the live Transport Pro HTTP client.

Every fixture is a real Transport Pro Public API response shape (see
``tests/transport_pro_payloads.py``), including the two traps a naive client falls into:
the array-wrapped ``payment_information`` payload and the echoed internal ``load_id`` that
differs from the load number in the request path. A fake transport records each request, so
all of this runs with no network.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from tests.transport_pro_payloads import (
    FILES_SEARCH,
    PAYMENT_INFORMATION,
    FakeTransport,
    deep_copy,
    full_transport,
)

from payment_bot.clients.transport_pro_http import (
    HttpResponse,
    TransportProHttpClient,
    TransportProSettings,
    build_transport_pro_client,
)
from payment_bot.config import Settings
from payment_bot.errors import ClientError


def _client(transport: FakeTransport) -> TransportProHttpClient:
    return TransportProHttpClient(
        base_url="https://tp.example.test/api/v1",
        username="apiuser",
        password="secret",
        transport=transport,
    )


# --- auth -------------------------------------------------------------------
@pytest.mark.unit
def test_logs_in_with_basic_then_uses_bearer() -> None:
    transport = full_transport()
    _client(transport).get_load("2462934")

    login = transport.calls[0]
    assert login["url"].endswith("/auth")
    expected = base64.b64encode(b"apiuser:secret").decode()
    assert login["headers"]["Authorization"] == f"Basic {expected}"

    data = transport.calls[1]
    assert data["headers"]["Authorization"] == "Bearer access-1"


@pytest.mark.unit
def test_401_refreshes_token_and_replays_request_once() -> None:
    transport = full_transport()
    client = _client(transport)
    client.get_load("2462934")  # primes the token
    transport.force_status = [401]  # the next data GET is unauthorized

    load = client.get_load("2999999")  # a different id, so not served from cache

    assert load.load_id_str == "2999999"
    # login + refresh means /auth was hit twice, and the GET was replayed successfully.
    assert transport.auth_calls == 2
    refresh_body = json.loads(
        next(
            c["body"]
            for c in reversed(transport.calls)
            if c["url"].endswith("/auth") and c["body"]
        )
    )
    assert refresh_body == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}


@pytest.mark.unit
def test_login_failure_is_a_client_error() -> None:
    class FailingAuth(FakeTransport):
        def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
            if url.endswith("/auth"):
                return HttpResponse(403, b'{"error":"bad credentials"}')
            return super().request(method, url, **kwargs)

    with pytest.raises(ClientError, match="login failed"):
        _client(FailingAuth()).get_load("2462934")


# --- get_load ---------------------------------------------------------------
@pytest.mark.unit
def test_unwraps_array_payload_and_keeps_requested_load_number() -> None:
    transport = full_transport()
    load = _client(transport).get_load("2462934")

    # The reply must quote the number the carrier asked about...
    assert load.load_id_str == "2462934"
    # ...while the echoed internal id is kept only for file lookups.
    assert load.internal_record_id == 1302556
    assert "voiceai/load/2462934/payment_information" in transport.data_urls()[0]


@pytest.mark.unit
def test_earnings_deductions_and_status_are_parsed() -> None:
    load = _client(full_transport()).get_load("2462934")

    assert load.billing_status == "BILLED"
    assert [e.title for e in load.earnings] == ["TRUCK ORDER NOT USED", "Brokerage Line Haul"]
    assert [e.amount for e in load.earnings] == [Decimal("150"), Decimal("4500")]
    assert load.earnings[0].estimated_payment_date is not None
    # The raw payload says 2026-08-19; the client reports the date the Transport Pro
    # application shows, one day later — see _app_pay_date (verified on load 2479097).
    assert load.earnings[0].estimated_payment_date.isoformat() == "2026-08-20"
    assert load.deductions == []
    assert load.account_information is not None
    assert load.account_information.company_name == "Idea Expedited, Inc"


@pytest.mark.unit
def test_boolean_timezone_from_live_api_is_tolerated() -> None:
    """The live payload sends `timezone: false`; a strict `str | None` would reject it."""

    load = _client(full_transport()).get_load("2462934")
    assert load.pickup is not None
    assert load.pickup.date is not None
    assert load.pickup.date.timezone is None


@pytest.mark.unit
def test_integer_timezone_offset_is_tolerated() -> None:
    payload = deep_copy(PAYMENT_INFORMATION)
    payload[0]["shipment_information"]["waypoints"][0]["date"]["timezone"] = -5
    load = _client(FakeTransport({"payment_information": payload})).get_load("2462934")

    assert load.pickup is not None and load.pickup.date is not None
    assert load.pickup.date.timezone == "-5"


@pytest.mark.unit
def test_empty_result_array_means_not_found() -> None:
    transport = FakeTransport({"payment_information": []})
    with pytest.raises(ClientError, match="not found"):
        _client(transport).get_load("9999999")


@pytest.mark.unit
def test_http_404_is_a_client_error() -> None:
    with pytest.raises(ClientError, match="not found"):
        _client(FakeTransport()).get_load("2462934")


@pytest.mark.unit
def test_unreadable_payload_is_a_client_error_not_a_crash() -> None:
    transport = FakeTransport({"payment_information": [{"billing_status": "BILLED"}]})
    with pytest.raises(ClientError, match="unreadable load payload"):
        _client(transport).get_load("2462934")


@pytest.mark.unit
def test_load_is_fetched_once_per_client() -> None:
    transport = full_transport()
    client = _client(transport)
    client.get_load("2462934")
    client.get_load("2462934")
    assert sum("payment_information" in u for u in transport.data_urls()) == 1


# --- dispatch ---------------------------------------------------------------
@pytest.mark.unit
def test_dispatch_rows_are_mapped_and_carry_no_rate() -> None:
    transport = full_transport()
    rows = _client(transport).get_dispatch_history("2462934")

    assert len(rows) == 1
    row = rows[0]
    assert row.carrier_name == "Idea Expedited, Inc"
    assert row.mc_number == "671286-C"
    assert row.dispatch_status == "Delivered"
    assert row.is_delivered and not row.is_canceled
    assert row.pickup == "Spokane, WA"
    assert row.delivery == "Lithia Springs, GA"
    # The API exposes no carrier rate on a dispatch row — we must not invent one.
    assert row.freight_bill is None
    assert "loadId=2462934" in next(u for u in transport.data_urls() if "dispatch/search" in u)


@pytest.mark.unit
def test_dispatch_row_without_assigned_carrier_is_skipped() -> None:
    transport = FakeTransport(
        {
            "payment_information": PAYMENT_INFORMATION,
            "/dispatch/search": {"results": [{"id": 1, "status": "Planned", "assignedTo": {}}]},
        }
    )
    assert _client(transport).get_dispatch_history("2462934") == []


# --- settlement (derived) ---------------------------------------------------
@pytest.mark.unit
def test_unsettled_load_yields_no_settlement_entries() -> None:
    assert _client(full_transport()).get_settlement_entries("2462934") == []


@pytest.mark.unit
def test_settled_earning_line_becomes_a_settlement_entry() -> None:
    paid = deep_copy(PAYMENT_INFORMATION)
    paid[0]["earnings"][1].update(
        {
            "payment_status": "Paid",
            "settlement_id": "S-778",
            "actual_payment_date": "2026-08-20",
            "payment_method": "Check",
            "check_number": "100482",
        }
    )
    transport = FakeTransport({"payment_information": paid})

    entries = _client(transport).get_settlement_entries("2462934")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.amount == Decimal("4500")
    assert entry.carrier_name == "Idea Expedited, Inc"
    # Raw actual_payment_date 2026-08-20 → app calendar 2026-08-21 (see _app_pay_date).
    assert entry.pay_date is not None and entry.pay_date.isoformat() == "2026-08-21"
    assert entry.payment_method == "Check"
    assert entry.check_or_ref == "100482"
    assert entry.description == "Brokerage Line Haul"


# --- file history -----------------------------------------------------------
@pytest.mark.unit
def test_file_history_is_keyed_by_the_carrier_facing_load_number() -> None:
    """Confirmed against the live tenant: recordId takes the number from the email."""

    transport = full_transport()
    docs = _client(transport).get_file_history("2462934")

    files_url = next(u for u in transport.data_urls() if "files/search" in u)
    assert "recordId=2462934" in files_url
    assert "recordType=loads" in files_url

    assert [d.file_type for d in docs] == ["Carrier Invoice", "Bill of Lading"]
    assert docs[0].index_date is not None and docs[0].index_date.isoformat() == "2026-07-01"
    assert docs[0].comments == "Invoice number 4540 Load Number 2462934"
    assert docs[0].indexed_by == "1000"
    # comments is null on the second doc → fall back to the file name
    assert docs[1].comments == "5031661_12.pdf"


# --- NOA / factoring (derived) ---------------------------------------------
@pytest.mark.unit
def test_remit_to_self_with_no_factoring_docs_reports_nothing_on_file() -> None:
    noa = _client(full_transport()).get_noa_factoring("2462934")
    assert noa.noa_on_file is False
    assert noa.factoring_company_on_file is None
    assert "Remit-to self" in (noa.details or "")


@pytest.mark.unit
def test_third_party_remit_to_is_reported_as_factoring() -> None:
    factored = deep_copy(PAYMENT_INFORMATION)
    factored[0]["account_information"]["remit_to"] = {
        "send_payment_to": "factoring_company",
        "company_name": "England Carrier Services",
    }
    transport = FakeTransport({"payment_information": factored, "/files/search": FILES_SEARCH})

    noa = _client(transport).get_noa_factoring("2462934")

    assert noa.noa_on_file is True
    assert noa.factoring_company_on_file == "England Carrier Services"
    assert "not self" in (noa.details or "")


@pytest.mark.unit
def test_factoring_document_alone_is_reported_with_its_evidence() -> None:
    files = deep_copy(FILES_SEARCH)
    files["results"].append(
        {
            "id": 5031662,
            "dateCreated": "2026-07-02T10:00:00Z",
            "uploadById": 1000,
            "fileName": "noa.pdf",
            "comments": None,
            "fileTypeId": 21,
            "fileTypeName": "Carrier Factoring Agr/Rel",
        }
    )
    transport = FakeTransport({"payment_information": PAYMENT_INFORMATION, "/files/search": files})

    noa = _client(transport).get_noa_factoring("2462934")

    assert noa.noa_on_file is True
    assert noa.factoring_company_on_file is None  # remit-to is still self
    assert "Carrier Factoring Agr/Rel" in (noa.details or "")


# --- authorization (derived) -----------------------------------------------
@pytest.mark.unit
def test_authorization_context_uses_carrier_company_and_dispatch_emails() -> None:
    auth = _client(full_transport()).get_authorization_context("2462934")

    assert auth.carrier_company == "Idea Expedited, Inc"
    assert "billing@ideaexpedited.com" in auth.authorized_emails
    assert "dispatch@ideaexpedited.com" in auth.authorized_emails
    assert auth.factoring_company is None


@pytest.mark.unit
def test_authorization_context_is_empty_when_no_contacts_are_exposed() -> None:
    transport = FakeTransport(
        {
            "payment_information": PAYMENT_INFORMATION,
            "/dispatch/search": {"results": []},
        }
    )
    auth = _client(transport).get_authorization_context("2462934")

    # Nothing invented: an unknown sender then falls through to DENY and the gate blocks.
    assert auth.authorized_emails == ()
    assert auth.carrier_company == "Idea Expedited, Inc"


# --- misc -------------------------------------------------------------------
@pytest.mark.unit
def test_missing_base_url_is_rejected_up_front() -> None:
    with pytest.raises(ClientError, match="base_url is required"):
        TransportProHttpClient(base_url="", username="u", password="p")


@pytest.mark.unit
def test_settings_object_builds_a_client() -> None:
    transport = full_transport()
    settings = TransportProSettings(
        base_url="https://tp.example.test/api/v1", username="u", password="p"
    )
    load = settings.build_client(transport=transport).get_load("2462934")
    assert load.load_id_str == "2462934"


@pytest.mark.unit
def test_factory_builds_from_app_settings() -> None:
    app_settings = Settings(
        tp_base_url="https://tp.example.test/api/v1",
        tp_username="apiuser",
        tp_password=SecretStr("secret"),
    )
    assert app_settings.transport_pro_configured is True

    transport = full_transport()
    client = build_transport_pro_client(app_settings, transport=transport)
    assert client.get_load("2462934").load_id_str == "2462934"


@pytest.mark.unit
def test_factory_refuses_to_start_when_unconfigured() -> None:
    """A half-configured client must fail at start-up, never mid-answer."""

    app_settings = Settings(tp_base_url="", tp_username="", tp_password=SecretStr(""))
    assert app_settings.transport_pro_configured is False

    with pytest.raises(ClientError, match="not configured"):
        build_transport_pro_client(app_settings)


@pytest.mark.unit
def test_password_is_not_exposed_by_repr() -> None:
    app_settings = Settings(tp_password=SecretStr("super-secret"))
    assert "super-secret" not in repr(app_settings)
    assert "super-secret" not in str(app_settings.tp_password)


@pytest.mark.unit
def test_paginated_response_beyond_first_page_is_logged_not_silently_truncated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    many = deep_copy(FILES_SEARCH)
    many["pagination"]["totalPages"] = 3
    transport = FakeTransport({"payment_information": PAYMENT_INFORMATION, "/files/search": many})

    with caplog.at_level("WARNING"):
        _client(transport).get_file_history("2462934")

    assert "transport_pro_paginated_result_truncated" in caplog.text
