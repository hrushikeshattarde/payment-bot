"""Live Transport Pro HTTP client (PRD §4.3 / §9).

Implements :class:`~payment_bot.clients.transport_pro.TransportProClient` against the
Transport Pro Public API. Nothing above the client layer changes: the tools, gate, agent,
and pipeline keep working against the same typed models the mock returns.

Endpoint selection
------------------

============================  ===============================================================
Protocol method               Transport Pro endpoint
============================  ===============================================================
``get_load``                  ``GET /voiceai/load/{load_number}/payment_information``
``get_dispatch_history``      ``GET /dispatch/search?loadId={load_number}``
``get_file_history``          ``GET /files/search?recordType=loads&recordId={internal_id}``
``get_settlement_entries``    *(no endpoint)* — derived from settled earning lines
``get_noa_factoring``         *(no endpoint)* — derived from ``remit_to`` + factoring docs
``get_authorization_context`` *(no endpoint)* — carrier company + dispatch contacts
============================  ===============================================================

``payment_information`` is the right primary endpoint because it returns exactly the
§4.3.0 payload both skills are grounded on — ``billing_status``, ``account_information``
(including ``remit_to``), every ``earnings[]`` line with its amounts, statuses, estimated /
actual pay dates, method and check number, plus ``deductions[]`` and the waypoints. One
call therefore serves ``payment_status`` (per-line pay dates via the Mon/Thu rule) *and*
``rate_verification`` (gross = Σ earnings, each deduction with its reason, net).

Three live-API details this client absorbs so the rest of the codebase never sees them:

1. **The payload is array-wrapped.** ``payment_information`` returns ``[ { …load… } ]``,
   not a bare object. An empty array means "no such load" → :class:`ClientError`.
2. **The echoed ``load_id`` is not the id you asked for.** The ``/voiceai/load/…`` paths
   take the carrier-facing load number but return Transport Pro's internal record id
   (``/voiceai/load/2333606`` → ``load_id: 1303298``; likewise
   ``/dispatch/search?loadId=2434384`` → ``loadId: 1303132``). We keep the requested
   number as the carrier-facing identity (``load_number``) and use the echoed id only as
   the ``recordId`` key for file search.
3. **Auth is a two-step token flow.** ``POST /auth`` with HTTP Basic returns
   ``access_token`` + ``refresh_token``; every other call sends
   ``Authorization: Bearer <access_token>``. On a 401 we refresh once (``grant_type:
   refresh_token``), fall back to a full re-login, and replay the request a single time.

Deliberately *not* invented: the API exposes no settlement-entries, NOA, or
authorized-parties endpoint, so those three methods derive what the payload genuinely
supports and report nothing further. Every unavailable fact stays ``None``/empty rather
than being guessed — the pre-send gate can only ground what a tool actually returned.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from payment_bot.clients.http import HttpResponse, HttpTransport, UrllibTransport
from payment_bot.config import Settings, get_settings
from payment_bot.errors import ClientError
from payment_bot.logging import get_logger

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "TransportProHttpClient",
    "TransportProSettings",
    "UrllibTransport",
    "build_transport_pro_client",
]
from payment_bot.models import (
    AuthorizationContext,
    DispatchRow,
    FileDocument,
    NoaFactoring,
    SettlementEntry,
    TransportProLoad,
)

_log = get_logger("clients.transport_pro")

#: Transport Pro document types that evidence factoring / assignment on a load. Taken
#: from the live ``GET /files/document_types`` vocabulary (ids 21 and 76).
_FACTORING_DOC_TYPES = ("carrier factoring agr", "factoring agreement", "notice of assignment")

_JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Tokens:
    access: str | None = None
    refresh: str | None = None


class TransportProHttpClient:
    """Read-only :class:`TransportProClient` backed by the Transport Pro Public API.

    Args:
        base_url: API root (the collection's ``{{URL}}``), e.g.
            ``https://<tenant>.transportpro.net/api/v1``. Confirm with the provider.
        username / password: API-user credentials for ``POST /auth``. Supply these from
            SSM / Secrets Manager, never from source.
        transport: Injectable HTTP seam; defaults to :class:`UrllibTransport`.
        timeout: Per-request timeout in seconds.
        cache_loads: Reuse one ``payment_information`` response per load for the lifetime
            of this client. Several tools read the same load in one email run, and a single
            consistent snapshot is what grounding wants — build a fresh client per email.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
        cache_loads: bool = True,
    ) -> None:
        if not base_url:
            raise ClientError("Transport Pro base_url is required")
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._transport: HttpTransport = transport or UrllibTransport()
        self._timeout = timeout
        self._cache_loads = cache_loads
        self._tokens = _Tokens()
        self._load_cache: dict[str, TransportProLoad] = {}
        self._dispatch_cache: dict[str, list[dict[str, Any]]] = {}

    # -- auth ----------------------------------------------------------------
    def _login(self) -> None:
        credentials = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        resp = self._transport.request(
            "POST",
            f"{self._base}/auth",
            headers={**_JSON_HEADERS, "Authorization": f"Basic {credentials}"},
            timeout=self._timeout,
        )
        if resp.status >= 400:
            raise ClientError(f"Transport Pro login failed (HTTP {resp.status})")
        self._store_tokens(resp.json(), context="login")

    def _refresh(self) -> bool:
        """Try the refresh-token grant. Returns False if it is not possible."""

        if not self._tokens.refresh:
            return False
        payload = json.dumps(
            {"grant_type": "refresh_token", "refresh_token": self._tokens.refresh}
        ).encode()
        resp = self._transport.request(
            "POST",
            f"{self._base}/auth",
            headers=dict(_JSON_HEADERS),
            body=payload,
            timeout=self._timeout,
        )
        if resp.status >= 400:
            return False
        self._store_tokens(resp.json(), context="refresh")
        return True

    def _store_tokens(self, data: Any, *, context: str) -> None:
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ClientError(f"Transport Pro {context} returned no access_token")
        self._tokens = _Tokens(
            access=str(data["access_token"]),
            refresh=str(data["refresh_token"]) if data.get("refresh_token") else None,
        )

    # -- request plumbing ----------------------------------------------------
    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET a JSON resource, authenticating and retrying a 401 exactly once."""

        url = f"{self._base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        if self._tokens.access is None:
            self._login()

        resp = self._send(url)
        if resp.status == 401:
            # Token expired mid-run: refresh, else re-login, then replay once.
            if not self._refresh():
                self._login()
            resp = self._send(url)

        if resp.status == 404:
            raise ClientError(f"Transport Pro: not found ({path})")
        if resp.status >= 400:
            raise ClientError(f"Transport Pro GET {path} failed (HTTP {resp.status})")
        return resp.json()

    def _send(self, url: str) -> HttpResponse:
        return self._transport.request(
            "GET",
            url,
            headers={**_JSON_HEADERS, "Authorization": f"Bearer {self._tokens.access}"},
            timeout=self._timeout,
        )

    def _results(self, payload: Any, *, path: str) -> list[dict[str, Any]]:
        """Unwrap a paginated ``{pagination, results}`` envelope."""

        if payload is None:
            return []
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            raise ClientError(f"Transport Pro {path}: unexpected payload {type(payload).__name__}")
        pagination = payload.get("pagination") or {}
        total_pages = pagination.get("totalPages")
        if isinstance(total_pages, int) and total_pages > 1:
            # The collection documents no page parameter, so we read the first page and
            # say so loudly rather than silently truncating.
            _log.warning(
                "transport_pro_paginated_result_truncated",
                extra={"path": path, "total_pages": total_pages},
            )
        results = payload.get("results")
        return [row for row in results if isinstance(row, dict)] if isinstance(results, list) else []

    # -- TransportProClient --------------------------------------------------
    def get_load(self, load_id: str) -> TransportProLoad:
        """Fetch the §4.3.0 payload via ``/voiceai/load/{n}/payment_information``."""

        key = load_id.strip()
        cached = self._load_cache.get(key)
        if cached is not None:
            return cached

        payload = self._get(f"/voiceai/load/{urllib.parse.quote(key)}/payment_information")
        rows = self._results(payload, path="payment_information")
        if not rows:
            raise ClientError(f"Transport Pro: load {load_id!r} not found")

        record = dict(rows[0])
        # Preserve the id the carrier asked about; the echoed load_id is TP's internal one.
        record["load_number"] = key
        try:
            load = TransportProLoad.model_validate(record)
        except Exception as exc:  # pydantic ValidationError → our error envelope
            raise ClientError(f"Transport Pro: unreadable load payload for {load_id!r}: {exc}") from exc

        # Pay dates arrive one calendar day behind the Transport Pro application. The UI
        # stores date-typed pay fields at midnight EDT; the Public API serialises them
        # through a UTC-4 shift (midnight minus four hours = 20:00 the previous day) and
        # then truncates to a date. Verified on load 2479097: the app's "Date To Pay" reads
        # 2026-08-05, the API returns 2026-08-04. Add the day back here, at the live-API
        # boundary, so every consumer — the grounding ledger, the Mon/Thu rule, the drafts —
        # speaks the application's calendar. The mock client is untouched: sample data is
        # authored in app-space already.
        load = load.model_copy(
            update={
                "earnings": [
                    e.model_copy(
                        update={
                            "estimated_payment_date": _app_pay_date(e.estimated_payment_date),
                            "actual_payment_date": _app_pay_date(e.actual_payment_date),
                        }
                    )
                    for e in load.earnings
                ]
            }
        )

        if self._cache_loads:
            self._load_cache[key] = load
        return load

    def get_dispatch_history(self, load_id: str) -> list[DispatchRow]:
        """Dispatch rows via ``/dispatch/search?loadId=``.

        Note the API exposes **no carrier rate** on a dispatch row, so ``freight_bill`` is
        always ``None`` here. ``carrier_cross_check`` still corroborates the carrier name;
        its ``payout_amount`` is simply unavailable, and the authoritative rate comes from
        ``compute_carrier_rate`` over the ``payment_information`` earnings.
        """

        rows = self._dispatch_rows(load_id)
        out: list[DispatchRow] = []
        for row in rows:
            carrier = _dig(row, "assignedTo", "carrier") or {}
            name = _text(carrier.get("companyName"))
            if not name:
                continue  # a dispatch with no assigned carrier tells us nothing
            pickup, delivery = _dispatch_endpoints(row)
            out.append(
                DispatchRow(
                    carrier_name=name,
                    mc_number=_text(carrier.get("mcNumber")),
                    freight_bill=None,  # not exposed by the API
                    dispatch_status=_text(row.get("status")) or "Unknown",
                    pickup=pickup,
                    delivery=delivery,
                    comment=_text(row.get("comment")),
                    last_updated=_text(row.get("lastUpdated") or row.get("dateCreated")),
                )
            )
        return out

    def get_settlement_entries(self, load_id: str) -> list[SettlementEntry]:
        """Settlement rows derived from settled earning lines.

        Transport Pro has no settlement-entries endpoint in the Public API. An earning
        line that carries a ``settlement_id`` or an ``actual_payment_date`` *is* a
        settlement record, so we surface exactly those and nothing more. An unsettled load
        yields ``[]``, which is what ``tp_get_settlement_entries`` reports as "not settled".
        """

        load = self.get_load(load_id)
        carrier = load.account_information.company_name if load.account_information else None
        entries: list[SettlementEntry] = []
        for earning in load.earnings:
            if earning.settlement_id is None and earning.actual_payment_date is None:
                continue
            entries.append(
                SettlementEntry(
                    amount=earning.amount,
                    carrier_name=carrier,
                    settle_date=None,  # not exposed separately by the API
                    pay_date=earning.actual_payment_date,
                    payment_method=earning.payment_method,
                    check_or_ref=earning.check_number,
                    line_type="settlement",
                    description=earning.title,
                )
            )
        return entries

    def get_file_history(self, load_id: str) -> list[FileDocument]:
        """Indexed documents via ``/files/search?recordType=loads&recordId=``.

        Which id ``recordId`` wants is tenant-dependent, and getting it wrong returns an
        empty list rather than an error — a silent "no documents on file", which would make
        the bot tell a carrier their paperwork is missing when it is not. So we try the
        **carrier-facing load number first** (confirmed working against the live tenant),
        and fall back to the internal record id echoed by ``payment_information``.
        """

        requested = load_id.strip()
        docs = self._file_search(requested)
        if docs:
            return docs

        internal = str(self.get_load(load_id).internal_record_id)
        if internal == requested:
            return []
        fallback = self._file_search(internal)
        if fallback:
            _log.info(
                "files_found_under_internal_record_id",
                extra={"requested": requested, "internal": internal},
            )
        return fallback

    def _file_search(self, record_id: str) -> list[FileDocument]:
        payload = self._get("/files/search", {"recordType": "loads", "recordId": record_id})
        docs: list[FileDocument] = []
        for row in self._results(payload, path="files/search"):
            file_type = _text(row.get("fileTypeName"))
            if not file_type:
                continue
            created = _as_date(row.get("dateCreated"))
            uploader = row.get("uploadById")
            type_id = row.get("fileTypeId")
            docs.append(
                FileDocument(
                    file_type=file_type,
                    file_type_id=int(type_id) if isinstance(type_id, int) else None,
                    index_date=created,
                    upload_date=created,
                    indexed_by=str(uploader) if uploader is not None else None,
                    comments=_text(row.get("comments")) or _text(row.get("fileName")),
                )
            )
        return docs

    def get_noa_factoring(self, load_id: str) -> NoaFactoring:
        """Read-only NOA / factoring status derived from ``remit_to`` and file history.

        There is no NOA endpoint. Two signals are available and both are reported with the
        evidence that produced them, so the reply can never overstate what is on file:

        * ``account_information.remit_to.send_payment_to != "self"`` — payment is remitted
          to a third party, and ``remit_to.company_name`` names it.
        * a factoring / assignment document on the load (``Carrier Factoring Agr/Rel``,
          ``Factoring Agreement/Releases``).
        """

        load = self.get_load(load_id)
        remit = load.account_information.remit_to if load.account_information else None
        is_factoring = bool(remit and remit.is_factoring)
        factoring_company = remit.company_name if (remit and is_factoring) else None

        factoring_docs = [
            doc.file_type
            for doc in self.get_file_history(load_id)
            if any(marker in doc.file_type.casefold() for marker in _FACTORING_DOC_TYPES)
        ]

        evidence: list[str] = []
        if is_factoring:
            evidence.append(f"remit-to is {factoring_company or 'a third party'} (not self)")
        if factoring_docs:
            evidence.append(f"factoring document(s) on file: {', '.join(sorted(set(factoring_docs)))}")

        return NoaFactoring(
            noa_on_file=bool(is_factoring or factoring_docs),
            factoring_company_on_file=factoring_company,
            details=(
                "; ".join(evidence)
                if evidence
                else "Remit-to self; no factoring document on file for this load."
            ),
        )

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        """Who may receive disclosure, assembled from the load and its dispatch contacts.

        The API exposes no authorized-parties resource. What it does give us:

        * the carrier company on the load (``account_information.company_name``) — enough
          for ``check_authorization``'s sender-domain match;
        * carrier-side contact emails on the dispatch record, used as the explicit
          allow-list.

        Anything we cannot establish is left empty, so an unrecognised sender falls through
        to DENY and the gate blocks the send.
        """

        load = self.get_load(load_id)
        remit = load.account_information.remit_to if load.account_information else None
        is_factoring = bool(remit and remit.is_factoring)

        emails: list[str] = []
        for row in self._dispatch_rows(load_id):
            assigned = row.get("assignedTo") or {}
            if not isinstance(assigned, dict):
                continue
            carrier = assigned.get("carrier")
            sources: list[Any] = [assigned.get("contacts")]
            if isinstance(carrier, dict):
                sources.extend([carrier.get("emailContacts"), carrier.get("contacts")])
            for source in sources:
                emails.extend(_emails_from(source))

        factoring_company = remit.company_name if (remit and is_factoring) else None
        return AuthorizationContext(
            carrier_company=(
                load.account_information.company_name if load.account_information else None
            ),
            authorized_emails=tuple(dict.fromkeys(e.lower() for e in emails)),
            factoring_company=factoring_company,
            # Factoring contact emails are not exposed; a factoring sender therefore
            # matches only by company-domain, and FACTORING is gated by policy anyway.
            factoring_emails=(),
        )

    # -- internals -----------------------------------------------------------
    def _dispatch_rows(self, load_id: str) -> list[dict[str, Any]]:
        key = load_id.strip()
        cached = self._dispatch_cache.get(key)
        if cached is not None:
            return cached
        payload = self._get("/dispatch/search", {"loadId": key})
        rows = self._results(payload, path="dispatch/search")
        if self._cache_loads:
            self._dispatch_cache[key] = rows
        return rows


# ---------------------------------------------------------------------------
# Parsing helpers — lenient by design: a missing field must never crash a run.
# ---------------------------------------------------------------------------
def _text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _app_pay_date(value: date | None) -> date | None:
    """The pay date as the Transport Pro application displays it (EDT).

    See the comment in :meth:`TransportProHttpClient.get_load` — the Public API reports
    date-typed pay fields one calendar day early, verified against the app on load 2479097.
    """

    return value + timedelta(days=1) if value is not None else None


def _dig(row: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    current: Any = row
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _as_date(value: object) -> date | None:
    """Parse ``2026-05-04T23:42:06Z`` or ``2026-05-04`` into a calendar date."""

    text = _text(value)
    if text is None:
        return None
    try:
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text)
    except ValueError:
        return None


def _emails_from(source: object) -> list[str]:
    """Collect ``email``-ish values from a contact list of unknown exact shape."""

    if not isinstance(source, list):
        return []
    out: list[str] = []
    for item in source:
        if isinstance(item, str) and "@" in item:
            out.append(item.strip())
        elif isinstance(item, dict):
            for key in ("email", "emailAddress", "value"):
                candidate = _text(item.get(key))
                if candidate and "@" in candidate:
                    out.append(candidate)
                    break
    return out


def _dispatch_endpoints(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Render the first pickup and last delivery of a dispatch as "City, ST"."""

    waypoints = row.get("waypoints")
    if not isinstance(waypoints, list):
        return None, None
    places: list[str] = []
    for wp in waypoints:
        if not isinstance(wp, dict):
            continue
        location = wp.get("location") if isinstance(wp.get("location"), dict) else wp
        city = _text(location.get("city")) if isinstance(location, dict) else None
        state = _text(location.get("state")) if isinstance(location, dict) else None
        if city and state:
            places.append(f"{city}, {state}")
        elif city:
            places.append(city)
    if not places:
        return None, None
    return places[0], places[-1]


@dataclass(frozen=True, slots=True)
class TransportProSettings:
    """The configuration a :class:`TransportProHttpClient` needs.

    Kept as a tiny value object so the AWS handlers can build it straight from SSM /
    Secrets Manager without importing the whole ``Settings`` model.
    """

    base_url: str
    username: str
    password: str
    timeout: float = 30.0

    def build_client(self, transport: HttpTransport | None = None) -> TransportProHttpClient:
        return TransportProHttpClient(
            base_url=self.base_url,
            username=self.username,
            password=self.password,
            transport=transport,
            timeout=self.timeout,
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> TransportProSettings:
        """Read the ``PAYBOT_TP_*`` configuration into a :class:`TransportProSettings`.

        Raises:
            ClientError: If Transport Pro is not fully configured. Failing here — at
                start-up — is deliberate: a half-configured client must never reach the
                point of answering a carrier.
        """

        resolved = settings or get_settings()
        if not resolved.transport_pro_configured:
            raise ClientError(
                "Transport Pro is not configured: set PAYBOT_TP_BASE_URL, PAYBOT_TP_USERNAME "
                "and PAYBOT_TP_PASSWORD (from SSM / Secrets Manager in production)"
            )
        return cls(
            base_url=resolved.tp_base_url,
            username=resolved.tp_username,
            password=resolved.tp_password.get_secret_value(),
            timeout=resolved.tp_timeout_seconds,
        )


def build_transport_pro_client(
    settings: Settings | None = None,
    transport: HttpTransport | None = None,
) -> TransportProHttpClient:
    """Build a live Transport Pro client from ``PAYBOT_TP_*`` configuration.

    The entrypoint the AWS processor / Slack-callback handlers use. Build one **per email**
    so each run gets a single consistent snapshot of every load it reads.
    """

    return TransportProSettings.from_settings(settings).build_client(transport=transport)
