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

#: `GET /voiceai/load/2436437/payment_information` — a load with **three payables**.
#:
#: Trimmed verbatim from the live tenant. This is the shape the client used to reduce to
#: ``results[0]``: three carriers ran a leg of one load, each with their own
#: ``account_information``, their own ``remit_to`` factor and their own earnings.
#:
#: * FOX CARRIERS, remitted to eCapital Freight Factoring Corp — $255 + $75 + $575 = $905
#: * Alina Transport Inc, remitted to RTS Financial Service, Inc — $150 TONU
#: * Parasource Inc, remitted to England Carrier Services — a $5,000 line haul and a $230
#:   lumper, the two rows a carrier asking about their own load could not be told about
#:
#: Note the pay dates: the API says ``2026-06-24`` and the application shows ``2026-06-25``,
#: which is the ``_app_pay_date`` shift and the "paid on 06/25" the carrier asked about.
PAYMENT_INFORMATION_MULTI_CARRIER: list[dict[str, Any]] = [
    {
        "load_id": 1304991,
        "billing_status": "BILLED",
        "account_information": {
            "company_name": "FOX CARRIERS",
            "dot_number": "3354881",
            "mc_number": None,
            "remit_to": {
                "send_payment_to": "factoring company",
                "company_name": "eCapital Freight Factoring Corp",
            },
        },
        "earnings": [
            {
                "title": "Special Pricing All",
                "amount": 255,
                "payment_status": "Paid",
                "settlement_id": 1276750,
                "estimated_payment_date": "2026-06-23",
                "actual_payment_date": "2026-06-24",
                "payment_method": "Check",
                "check_number": "775038",
            },
            {
                "title": "TRUCK ORDER NOT USED",
                "amount": 75,
                "payment_status": "Paid",
                "settlement_id": 1276750,
                "estimated_payment_date": "2026-06-23",
                "actual_payment_date": "2026-06-24",
                "payment_method": "Check",
                "check_number": "775038",
            },
            {
                "title": "Brokerage Line Haul",
                "amount": 575,
                "payment_status": "Paid",
                "settlement_id": 1276750,
                "estimated_payment_date": "2026-06-23",
                "actual_payment_date": "2026-06-24",
                "payment_method": "Check",
                "check_number": "775038",
            },
        ],
        "deductions": [],
        "shipment_information": {
            "waypoints": [
                {
                    "type": "Pickup",
                    "city": "IMPERIAL",
                    "state": "Pennsylvania",
                    "date": {"timestamp": "2026-05-22T13:00:00Z", "timezone": "EDT"},
                },
                {
                    "type": "Delivery",
                    "city": "PHOENIX",
                    "state": "Arizona",
                    "date": {"timestamp": "2026-05-27T17:00:00Z", "timezone": "MDT"},
                },
            ]
        },
    },
    {
        "load_id": 1304991,
        "billing_status": "BILLED",
        "account_information": {
            "company_name": "Alina Transport Inc",
            "dot_number": "4372997",
            "mc_number": None,
            "remit_to": {
                "send_payment_to": "factoring company",
                "company_name": "RTS Financial Service, Inc",
            },
        },
        "earnings": [
            {
                "title": "TRUCK ORDER NOT USED",
                "amount": 150,
                "payment_status": "Paid",
                "settlement_id": 1276237,
                "estimated_payment_date": "2026-06-23",
                "actual_payment_date": "2026-06-24",
                "payment_method": "Direct Deposit",
                "check_number": None,
            }
        ],
        "deductions": [],
        "shipment_information": {
            "waypoints": [
                {
                    "type": "Pickup",
                    "city": "IMPERIAL",
                    "state": "Pennsylvania",
                    "date": {"timestamp": "2026-05-23T13:00:00Z", "timezone": "EDT"},
                },
                {
                    "type": "Delivery",
                    "city": "IMPERIAL",
                    "state": "Pennsylvania",
                    "date": {"timestamp": "2026-05-23T15:00:00Z", "timezone": "MDT"},
                },
            ]
        },
    },
    {
        "load_id": 1304991,
        "billing_status": "BILLED",
        "account_information": {
            "company_name": "Parasource Inc",
            "dot_number": "3366614",
            "mc_number": None,
            "remit_to": {
                "send_payment_to": "factoring company",
                "company_name": "England Carrier Services",
            },
        },
        "earnings": [
            {
                "title": "Lumper",
                "amount": 230,
                "payment_status": "Paid",
                "settlement_id": 1312950,
                "estimated_payment_date": "2026-08-11",
                "actual_payment_date": "2026-08-12",
                "payment_method": "Check",
                "check_number": "792113",
            },
            {
                "title": "Brokerage Line Haul",
                "amount": 5000,
                "payment_status": "Paid",
                "settlement_id": 1278292,
                "estimated_payment_date": "2026-06-23",
                "actual_payment_date": "2026-06-24",
                "payment_method": "Direct Deposit",
                "check_number": None,
            },
        ],
        "deductions": [],
        "shipment_information": {
            "waypoints": [
                {
                    "type": "Pickup",
                    "city": "IMPERIAL",
                    "state": "Pennsylvania",
                    "date": {"timestamp": "2026-05-23T12:40:00Z", "timezone": "EDT"},
                },
                {
                    "type": "Delivery",
                    "city": "PHOENIX",
                    "state": "Arizona",
                    "date": {"timestamp": "2026-05-28T00:30:00Z", "timezone": "MDT"},
                },
            ]
        },
    },
]


def _dispatch_row(
    dispatch_id: int,
    status: str,
    company: str,
    dot: str,
    mc: str,
    email: str,
) -> dict[str, Any]:
    """One ``/dispatch/search`` row, trimmed to the fields the client reads."""

    return {
        "id": dispatch_id,
        "loadId": 1304991,
        "status": status,
        "lastUpdated": "2026-05-28T14:35:56Z",
        "assignedTo": {
            "type": "brokerCarrier",
            "carrier": {
                "id": dispatch_id,
                "status": "ACTIVE",
                "companyName": company,
                "usDOT": dot,
                "mcNumber": mc,
                "emailContacts": [{"type": "DISPATCH", "value": email}],
                "internalContacts": [],
            },
            "contacts": [{"type": "DISPATCHER", "name": "Dispatch", "email": email}],
        },
        "waypoints": None,
    }


#: `GET /dispatch/search?loadId=2436437` — five rows, three delivered and two canceled.
#:
#: The live history for the load: Hazemo canceled, FOX CARRIERS delivered, Victory Transit
#: canceled, Parasource delivered, Alina Transport delivered. "The delivered row" does not
#: exist here, which is what ``carrier_cross_check`` used to assume.
DISPATCH_SEARCH_MULTI_CARRIER: dict[str, Any] = {
    "pagination": {"totalRecords": 5, "perPage": 200, "currentPage": 0, "totalPages": 1},
    "results": [
        _dispatch_row(
            2661443, "Canceled", "Hazemo Transport Llc", "3325950", "1059077",
            "gebrhiwetalem@gmail.com",
        ),
        _dispatch_row(
            2661978, "Delivered", "FOX CARRIERS", "3354881", "1073707", "lily@foxcarriers.com",
        ),
        _dispatch_row(
            2662829, "Canceled", "Victory Transit Inc", "1557782", "578104",
            "zack@victorytransitinc.com",
        ),
        _dispatch_row(
            2663241, "Delivered", "Parasource Inc", "3366614", "1078918",
            "parasourcedsp@gmail.com",
        ),
        _dispatch_row(
            2663483, "Delivered", "Alina Transport Inc", "4372997", "1713032",
            "alinatransportinc@gmail.com",
        ),
    ],
}  # fmt: skip


def multi_carrier_transport() -> FakeTransport:
    """A transport wired for load 2436437 — three payables, five dispatch rows."""

    return FakeTransport(
        {
            "payment_information": PAYMENT_INFORMATION_MULTI_CARRIER,
            "/dispatch/search": DISPATCH_SEARCH_MULTI_CARRIER,
            "/files/search": {"results": []},
        }
    )


#: `GET /voiceai/load/2469115/payment_information` — a four-carrier RELAY, all delivered.
#:
#: Trimmed verbatim from the live tenant, and a different shape from 2436437: nothing was
#: canceled and every carrier settled, each to its own factor (and Jomije to itself). One
#: load, four legs, four payables, four pay dates.
#:
#: This is the fixture for **attribution**, not just multiplicity. Every carrier here has a
#: corporate contact domain, so a sender is recognised by ``authorized_emails`` or its domain
#: — the two branches that could not narrow to a leg until the dispatch row's carrier↔contact
#: pairing was kept. Zmile's payroll wrote from ``@zmile.io`` about their $5,750 and the
#: unnarrowed answer was Aralo Express's $2,580 remitted to RTS Financial.
#:
#: Pay dates are pre-shift here (the API's own values); the client reports each one day later.
PAYMENT_INFORMATION_RELAY: list[dict[str, Any]] = [
    {
        "load_id": 1310442,
        "billing_status": "BILLED",
        "account_information": {
            "company_name": 'Aralo Express Usa Inc',
            "dot_number": None,
            "mc_number": None,
            "remit_to": {'send_payment_to': 'factoring company', 'company_name': 'RTS Financial Service, Inc'},
        },
        "earnings": [
            {
                "title": 'Brokerage Line Haul',
                "amount": 2580,
                "payment_status": "Paid",
                "settlement_id": 1305858,
                "estimated_payment_date": '2026-07-30',
                "actual_payment_date": '2026-07-31',
                "payment_method": 'Direct Deposit',
                "check_number": None,
            }
        ],
        "deductions": [],
        "shipment_information": {"waypoints": []},
    },
    {
        "load_id": 1310442,
        "billing_status": "BILLED",
        "account_information": {
            "company_name": 'Transportes H&h Logistics Llc',
            "dot_number": None,
            "mc_number": None,
            "remit_to": {'send_payment_to': 'factoring company', 'company_name': 'Trilogy Solutions, LLC'},
        },
        "earnings": [
            {
                "title": 'Brokerage Line Haul',
                "amount": 400,
                "payment_status": "Paid",
                "settlement_id": 1318126,
                "estimated_payment_date": '2026-08-13',
                "actual_payment_date": '2026-08-17',
                "payment_method": 'Check',
                "check_number": '794504',
            }
        ],
        "deductions": [],
        "shipment_information": {"waypoints": []},
    },
    {
        "load_id": 1310442,
        "billing_status": "BILLED",
        "account_information": {
            "company_name": 'Zmile Inc',
            "dot_number": None,
            "mc_number": None,
            "remit_to": {'send_payment_to': 'factoring company', 'company_name': 'Triumph Business Capital'},
        },
        "earnings": [
            {
                "title": 'Brokerage Line Haul',
                "amount": 5750,
                "payment_status": "Paid",
                "settlement_id": 1318107,
                "estimated_payment_date": '2026-08-13',
                "actual_payment_date": '2026-08-16',
                "payment_method": 'Direct Deposit',
                "check_number": None,
            }
        ],
        "deductions": [],
        "shipment_information": {"waypoints": []},
    },
    {
        "load_id": 1310442,
        "billing_status": "BILLED",
        "account_information": {
            "company_name": 'Jomije Transporting Llc',
            "dot_number": None,
            "mc_number": None,
            "remit_to": {'send_payment_to': 'self', 'company_name': 'Jomije Transporting Llc'},
        },
        "earnings": [
            {
                "title": 'Brokerage Line Haul',
                "amount": 475,
                "payment_status": "Paid",
                "settlement_id": 1305857,
                "estimated_payment_date": '2026-07-30',
                "actual_payment_date": '2026-07-31',
                "payment_method": 'Direct Deposit',
                "check_number": None,
            }
        ],
        "deductions": [],
        "shipment_information": {"waypoints": []},
    },
]


#: `GET /dispatch/search?loadId=2469115` — four delivered rows, one per leg.
DISPATCH_SEARCH_RELAY: dict[str, Any] = {
    "pagination": {"totalRecords": 4, "perPage": 200, "currentPage": 0, "totalPages": 1},
    "results": [
        _dispatch_row(
            701, "Delivered", "Aralo Express Usa Inc", "3931067", "5657",
            "planner.northbound@araloexpressusa.com",
        ),
        _dispatch_row(
            702, "Delivered", "Jomije Transporting Llc", "3521234", "749938",
            "logistics@jomije.com",
        ),
        _dispatch_row(
            703, "Delivered", "Transportes H&h Logistics Llc", "1741080", "174108",
            "trafico@transporteshh.com",
        ),
        _dispatch_row(
            704, "Delivered", "Zmile Inc", "3550091", "1067250", "dispatch@zmile.io",
        ),
    ],
}  # fmt: skip


def relay_transport() -> FakeTransport:
    """A transport wired for load 2469115 — four carriers, each with a corporate domain."""

    return FakeTransport(
        {
            "payment_information": PAYMENT_INFORMATION_RELAY,
            "/dispatch/search": DISPATCH_SEARCH_RELAY,
            "/files/search": {"results": []},
        }
    )


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
