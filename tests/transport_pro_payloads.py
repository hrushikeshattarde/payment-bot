"""Real Transport Pro Public API response shapes, plus a fake HTTP transport.

Lifted verbatim (values trimmed to load 2462934's data) from the Transport Pro Public API
Postman collection so both the unit tests and the pipeline integration test exercise the
**actual** wire shapes:

* ``Voice AI / Load Payment Status`` → ``GET /voiceai/load/{n}/payment_information``
  — a JSON **array**, an *internal* ``load_id`` that differs from the requested number,
  and ``timezone: false`` on waypoints.
* ``Dispatch / Search Dispatch`` → ``GET /dispatch/search?loadId={n}``
  — ``{pagination, results}`` envelope, carrier under ``assignedTo.carrier``, no rate field.
* ``Image Files / Search Files`` → ``GET /files/search?recordType=loads&recordId={id}``
  — ``fileTypeName`` from the live ``document_types`` vocabulary.

Shared here (rather than imported across test packages) so neither test module depends on
the other.
"""

from __future__ import annotations

import json
from typing import Any

from payment_bot.clients.transport_pro_http import HttpResponse

#: `GET /voiceai/load/{n}/payment_information`
PAYMENT_INFORMATION: list[dict[str, Any]] = [
    {
        "load_id": 1302556,
        "billing_status": "BILLED",
        "account_information": {
            "company_name": "Idea Expedited, Inc",
            "dot_number": "2363192",
            "mc_number": "671286-C",
            "remit_to": {"send_payment_to": "self", "company_name": "Idea Expedited, Inc"},
        },
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
        "deductions": [],
        "shipment_information": {
            "waypoints": [
                {
                    "type": "Pickup",
                    "city": "Spokane",
                    "state": "Washington",
                    "date": {"timestamp": "2026-06-23T16:24:00Z", "timezone": False},
                },
                {
                    "type": "Delivery",
                    "city": "Lithia Springs",
                    "state": "Georgia",
                    "date": {"timestamp": "2026-06-29T16:00:00Z", "timezone": False},
                },
            ]
        },
    }
]

#: `GET /dispatch/search?loadId={n}`
DISPATCH_SEARCH: dict[str, Any] = {
    "pagination": {"totalRecords": 1, "perPage": 200, "currentPage": 0, "totalPages": 1},
    "results": [
        {
            "id": 648971,
            "loadId": 1303132,
            "status": "Delivered",
            "lastUpdated": "2026-06-29T12:00:00Z",
            "assignedTo": {
                "type": "brokerCarrier",
                "carrier": {
                    "id": 1042,
                    "status": "ACTIVE",
                    "companyName": "Idea Expedited, Inc",
                    "usDOT": "2363192",
                    "mcNumber": "671286-C",
                    "emailContacts": [{"type": "MAIN", "email": "billing@ideaexpedited.com"}],
                    "internalContacts": [],
                },
                "contacts": [{"name": "Dispatch", "email": "dispatch@ideaexpedited.com"}],
            },
            "waypoints": [
                {"type": "SH", "location": {"city": "Spokane", "state": "WA"}},
                {"type": "CN", "location": {"city": "Lithia Springs", "state": "GA"}},
            ],
        }
    ],
}

#: `GET /files/search?recordType=loads&recordId={internal id}`
FILES_SEARCH: dict[str, Any] = {
    "pagination": {"totalRecords": 2, "perPage": 200, "currentPage": 0, "totalPages": 1},
    "results": [
        {
            "id": 5031660,
            "dateCreated": "2026-07-01T23:42:06Z",
            "uploadById": 1000,
            "fileName": "5031660_22.pdf",
            "comments": "Invoice number 4540 Load Number 2462934",
            "fileTypeId": 22,
            "fileTypeName": "Carrier Invoice",
        },
        {
            "id": 5031661,
            "dateCreated": "2026-06-30T10:00:00Z",
            "uploadById": 1000,
            "fileName": "5031661_12.pdf",
            "comments": None,
            "fileTypeId": 12,
            "fileTypeName": "Bill of Lading",
        },
    ],
}

#: `POST /auth` response
TOKENS: dict[str, str] = {"access_token": "access-1", "refresh_token": "refresh-1"}


class FakeTransport:
    """An :class:`~payment_bot.clients.transport_pro_http.HttpTransport` for tests.

    Routes by URL substring, answers ``/auth`` with tokens, and records every request so
    tests can assert the exact URLs and headers the client produced.
    """

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = routes or {}
        self.calls: list[dict[str, Any]] = []
        self.auth_calls = 0
        #: HTTP statuses to force on the next data GETs, popped in order.
        self.force_status: list[int] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if url.endswith("/auth"):
            self.auth_calls += 1
            return HttpResponse(200, json.dumps(TOKENS).encode())
        if self.force_status:
            return HttpResponse(self.force_status.pop(0), b"{}")
        for fragment, payload in self.routes.items():
            if fragment in url:
                return HttpResponse(200, json.dumps(payload).encode())
        return HttpResponse(404, b'{"error":"not found"}')

    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]

    def data_urls(self) -> list[str]:
        """Every URL except the token endpoint."""

        return [u for u in self.urls() if not u.endswith("/auth")]


def full_transport() -> FakeTransport:
    """A transport wired for all three read endpoints of load 2462934."""

    return FakeTransport(
        {
            "payment_information": PAYMENT_INFORMATION,
            "/dispatch/search": DISPATCH_SEARCH,
            "/files/search": FILES_SEARCH,
        }
    )


def deep_copy(payload: Any) -> Any:
    """A JSON round-trip copy, so a test can mutate a payload without affecting others."""

    return json.loads(json.dumps(payload))
