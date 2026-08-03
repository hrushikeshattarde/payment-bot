"""Document classification and the missing-document check.

The primary fixture is the **real** ``files/search`` response for load 2436795 — eleven
rows, four of them duplicate rate agreements, and no proof of delivery anywhere. That load
was flagged as "BOL missing" in Transport Pro, so it is the case this code exists to answer.

The second fixture is load 2524781: two "Driver Supplied BOL" photos and a rate agreement.
The live draft for that load chased only the invoice because the driver photos matched the
``\\bbol\\b`` name fallback — the reply should have asked for the signed BOL too.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from tests.transport_pro_payloads import PAYMENT_INFORMATION, FakeTransport

from payment_bot.clients.transport_pro_http import TransportProHttpClient
from payment_bot.domain.documents import (
    DocCategory,
    assess_documents,
    classify,
)

# Verbatim from the live tenant: GET /files/search?recordType=loads&recordId=2436795
FILES_2436795: dict[str, Any] = {
    "pagination": {"totalRecords": 11, "perPage": 200, "currentPage": 0, "totalPages": 1},
    "results": [
        {"id": 29245974, "dateCreated": "2026-05-22T13:45:12Z", "uploadById": 1285,
         "fileName": "29245974_23.pdf", "mimeType": "application/pdf",
         "comments": "Rate and Dispatch Confirmation for load - 2436795",
         "fileTypeId": 23, "fileTypeName": "Carrier Rate Agreement"},
        {"id": 29253295, "dateCreated": "2026-05-22T17:22:32Z", "uploadById": 1285,
         "fileName": "29253295_23.pdf", "mimeType": "application/pdf",
         "comments": "Rate and Dispatch Confirmation for load - 2436795",
         "fileTypeId": 23, "fileTypeName": "Carrier Rate Agreement"},
        {"id": 29374206, "dateCreated": "2026-05-29T19:45:37Z", "uploadById": 1336,
         "fileName": "29374206_81.pdf", "mimeType": "application/pdf",
         "comments": "Freight Bill for batch - 2436795",
         "fileTypeId": 81, "fileTypeName": "Freight Bill"},
        {"id": 29380589, "dateCreated": "2026-05-30T02:31:10Z", "uploadById": 1478,
         "fileName": "29380589_319.pdf", "mimeType": "application/pdf",
         "comments": "Billing Packet - 2436795",
         "fileTypeId": 319, "fileTypeName": "Billing Packet"},
        {"id": 29392971, "dateCreated": "2026-06-01T07:55:02Z", "uploadById": 1705,
         "fileName": "29392971_123.pdf", "mimeType": "application/pdf",
         "comments": "Invoice# INVDHS6244 -  Load#2436795",
         "fileTypeId": 123, "fileTypeName": "Other"},
        {"id": 29392972, "dateCreated": "2026-06-01T07:55:02Z", "uploadById": 1705,
         "fileName": "29392972_23.pdf", "mimeType": "application/pdf",
         "comments": "Invoice# INVDHS6244 -  Load#2436795",
         "fileTypeId": 23, "fileTypeName": "Carrier Rate Agreement"},
        {"id": 29392973, "dateCreated": "2026-06-01T07:55:03Z", "uploadById": 1705,
         "fileName": "29392973_22.pdf", "mimeType": "application/pdf",
         "comments": "Invoice# INVDHS6244 -  Load#2436795",
         "fileTypeId": 22, "fileTypeName": "Carrier Invoice"},
        {"id": 30216653, "dateCreated": "2026-07-16T04:37:12Z", "uploadById": 1705,
         "fileName": "30216653_22.pdf", "mimeType": "application/pdf",
         "comments": "Invoice# INVDHS6244 -  Load#2436795",
         "fileTypeId": 22, "fileTypeName": "Carrier Invoice"},
        {"id": 30216654, "dateCreated": "2026-07-16T04:37:12Z", "uploadById": 1705,
         "fileName": "30216654_344.pdf", "mimeType": "application/pdf",
         "comments": "Invoice# INVDHS6244 -  Load#2436795",
         "fileTypeId": 344, "fileTypeName": "Load Sheet"},
        {"id": 30216655, "dateCreated": "2026-07-16T04:37:13Z", "uploadById": 1705,
         "fileName": "30216655_123.pdf", "mimeType": "application/pdf",
         "comments": "Invoice# INVDHS6244 -  Load#2436795",
         "fileTypeId": 123, "fileTypeName": "Other"},
        {"id": 30216656, "dateCreated": "2026-07-16T04:37:13Z", "uploadById": 1705,
         "fileName": "30216656_23.pdf", "mimeType": "application/pdf",
         "comments": "Invoice# INVDHS6244 -  Load#2436795",
         "fileTypeId": 23, "fileTypeName": "Carrier Rate Agreement"},
    ],
}


# Verbatim from the live tenant: GET /files/search?recordType=loads&recordId=2524781
FILES_2524781: dict[str, Any] = {
    "pagination": {"totalRecords": 3, "perPage": 200, "currentPage": 0, "totalPages": 1},
    "results": [
        {"id": 30499089, "dateCreated": "2026-07-31T12:41:16Z", "uploadById": 3982,
         "fileName": "30499089_23.pdf", "mimeType": "application/pdf",
         "comments": "Rate and Dispatch Confirmation for load - 2524781",
         "fileTypeId": 23, "fileTypeName": "Carrier Rate Agreement"},
        {"id": 30516893, "dateCreated": "2026-08-01T02:01:16Z", "uploadById": 1,
         "fileName": "30516893_363.pdf", "mimeType": "application/pdf",
         "comments": "Driver Supplied Image - 2524781",
         "fileTypeId": 363, "fileTypeName": "Driver Supplied BOL"},
        {"id": 30516894, "dateCreated": "2026-08-01T02:01:18Z", "uploadById": 1,
         "fileName": "30516894_363.pdf", "mimeType": "application/pdf",
         "comments": "Driver Supplied Image - 2524781",
         "fileTypeId": 363, "fileTypeName": "Driver Supplied BOL"},
    ],
}


def _rows(payload: dict[str, Any]) -> list[tuple[str, int | None, date | None, str | None]]:
    out = []
    for r in payload["results"]:
        created = date.fromisoformat(r["dateCreated"][:10])
        out.append((r["fileTypeName"], r["fileTypeId"], created, r["comments"]))
    return out


# --- the real load ----------------------------------------------------------
@pytest.mark.unit
def test_load_2436795_is_missing_proof_of_delivery() -> None:
    """Eleven documents on file, and the one that matters for payment is absent."""

    status, _ = assess_documents(_rows(FILES_2436795), load_id="2436795")

    assert status.missing == [DocCategory.PROOF_OF_DELIVERY]
    assert status.is_complete is False
    assert DocCategory.CARRIER_INVOICE in status.present
    assert DocCategory.RATE_AGREEMENT in status.present


@pytest.mark.unit
def test_eleven_rows_collapse_to_six_categories() -> None:
    status, _ = assess_documents(_rows(FILES_2436795), load_id="2436795")

    assert status.document_count == 11
    counts = {s.category: s.count for s in status.by_category}
    assert counts == {
        DocCategory.CARRIER_INVOICE: 2,
        DocCategory.RATE_AGREEMENT: 4,
        DocCategory.FREIGHT_BILL: 1,
        DocCategory.BILLING_PACKET: 1,
        DocCategory.LOAD_SHEET: 1,
        DocCategory.OTHER: 2,
    }


@pytest.mark.unit
def test_latest_upload_per_category_is_tracked() -> None:
    status, _ = assess_documents(_rows(FILES_2436795), load_id="2436795")
    latest = {s.category: s.latest for s in status.by_category}

    assert latest[DocCategory.CARRIER_INVOICE] == date(2026, 7, 16)
    assert latest[DocCategory.RATE_AGREEMENT] == date(2026, 7, 16)


@pytest.mark.unit
def test_every_document_is_matched_to_the_load() -> None:
    _, classified = assess_documents(_rows(FILES_2436795), load_id="2436795")
    assert all(d.matches_load for d in classified)


@pytest.mark.unit
def test_adding_a_bol_clears_the_missing_list() -> None:
    rows = _rows(FILES_2436795)
    rows.append(("Bill of Lading", 12, date(2026, 6, 2), "POD for load - 2436795"))

    status, _ = assess_documents(rows, load_id="2436795")

    assert status.missing == []
    assert status.is_complete is True


# --- the driver-photo load ---------------------------------------------------
@pytest.mark.unit
def test_load_2524781_driver_photos_do_not_satisfy_proof_of_delivery() -> None:
    """Two driver-app BOL photos on file, and the signed BOL is still owed."""

    status, _ = assess_documents(_rows(FILES_2524781), load_id="2524781")

    assert status.missing == [DocCategory.CARRIER_INVOICE, DocCategory.PROOF_OF_DELIVERY]
    assert status.is_complete is False
    assert DocCategory.RATE_AGREEMENT in status.present
    counts = {s.category: s.count for s in status.by_category}
    assert counts[DocCategory.DRIVER_UPLOAD] == 2


# --- classification robustness ----------------------------------------------
@pytest.mark.unit
def test_classification_keys_on_the_stable_type_id() -> None:
    # A renamed or localised display name must not change the answer.
    assert classify("Frachtbrief", 12) is DocCategory.PROOF_OF_DELIVERY
    assert classify("whatever", 22) is DocCategory.CARRIER_INVOICE


@pytest.mark.unit
def test_unknown_type_id_falls_back_to_a_name_match() -> None:
    assert classify("Carrier Invoice", 9999) is DocCategory.CARRIER_INVOICE
    assert classify("Proof of Delivery", None) is DocCategory.PROOF_OF_DELIVERY


@pytest.mark.unit
@pytest.mark.parametrize("name", ["Symbolic Attachment", "Bollard Permit", "Podium Photo"])
def test_bol_and_pod_are_not_matched_as_substrings(name: str) -> None:
    """The old substring test reported delivery proof for 'symbolic' and 'podium'."""

    assert classify(name, None) is not DocCategory.PROOF_OF_DELIVERY


@pytest.mark.unit
def test_driver_supplied_bol_is_a_driver_upload_not_delivery_proof() -> None:
    """By id, and by name when the id is uncatalogued — 'BOL' in the name must not win."""

    assert classify("Driver Supplied BOL", 363) is DocCategory.DRIVER_UPLOAD
    assert classify("Driver Supplied BOL", None) is DocCategory.DRIVER_UPLOAD


@pytest.mark.unit
def test_proof_of_delivery_type_id_is_catalogued() -> None:
    assert classify("Proof of Delivery", 360) is DocCategory.PROOF_OF_DELIVERY


@pytest.mark.unit
def test_repair_invoice_is_not_a_carrier_invoice() -> None:
    """The generic 'invoice' name fallback must not fire for the catalogued repair type."""

    assert classify("Maintenance / Repair Invoice", 310) is DocCategory.OTHER


@pytest.mark.unit
def test_cancel_confirmation_is_detected_from_comments() -> None:
    rows = _rows(FILES_2436795)
    rows.append(("Other", 123, date(2026, 6, 3), "CANCEL LOAD Confirmation - 2436795"))

    status, _ = assess_documents(rows, load_id="2436795")
    assert status.has_cancel_confirmation is True


@pytest.mark.unit
def test_load_number_is_matched_whole_not_as_a_prefix() -> None:
    """`243679` must not match a comment about `2436795`."""

    _, classified = assess_documents(
        [("Carrier Invoice", 22, None, "Invoice for load - 2436795")], load_id="243679"
    )
    assert classified[0].matches_load is False


@pytest.mark.unit
def test_no_documents_means_everything_required_is_missing() -> None:
    status, classified = assess_documents([], load_id="2436795")

    assert classified == []
    assert status.document_count == 0
    assert set(status.missing) == {
        DocCategory.CARRIER_INVOICE,
        DocCategory.PROOF_OF_DELIVERY,
        DocCategory.RATE_AGREEMENT,
    }


# --- the client's recordId resolution ---------------------------------------
@pytest.mark.unit
def test_file_search_uses_the_carrier_facing_load_number_first() -> None:
    transport = FakeTransport(
        {"payment_information": PAYMENT_INFORMATION, "/files/search": FILES_2436795}
    )
    client = TransportProHttpClient(
        base_url="https://tp.example.test/api/v1", username="u", password="p", transport=transport
    )

    docs = client.get_file_history("2436795")

    assert len(docs) == 11
    assert docs[0].file_type_id == 23
    files_url = next(u for u in transport.data_urls() if "files/search" in u)
    assert "recordId=2436795" in files_url
    # The load payload is not even fetched when the first search succeeds.
    assert not any("payment_information" in u for u in transport.data_urls())


@pytest.mark.unit
def test_empty_result_falls_back_to_the_internal_record_id() -> None:
    """A wrong recordId returns [] rather than an error — never report that as 'no docs'."""

    class TwoStage(FakeTransport):
        def request(self, method: str, url: str, **kwargs: Any) -> Any:
            if "files/search" in url and "recordId=2462934" in url:
                return super().request(method, "https://x/empty", **kwargs)
            return super().request(method, url, **kwargs)

    transport = TwoStage(
        {
            "payment_information": PAYMENT_INFORMATION,
            "https://x/empty": {"pagination": {"totalPages": 1}, "results": []},
            "/files/search": FILES_2436795,
        }
    )
    client = TransportProHttpClient(
        base_url="https://tp.example.test/api/v1", username="u", password="p", transport=transport
    )

    docs = client.get_file_history("2462934")

    assert len(docs) == 11  # found under the internal id 1302556
    assert any("recordId=1302556" in u for u in transport.data_urls())
