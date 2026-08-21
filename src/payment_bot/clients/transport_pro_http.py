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
``get_load_payables``         ``GET /voiceai/load/{load_number}/payment_information``
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

1. **The payload is an array of PAYABLES, one per carrier** — not a one-element wrapper
   around "the load". ``payment_information`` returns ``[ {…carrier A…}, {…carrier B…} ]``
   for a load re-dispatched or split across legs, each entry with its own
   ``account_information``, ``remit_to``, ``earnings`` and ``deductions``. This client read
   ``results[0]``, so on a multi-carrier load the other carriers, their payments and their
   factors did not exist as far as the bot was concerned — see :meth:`get_load_payables`.
   An empty array means the load was CANCELLED →
   :class:`~payment_bot.errors.LoadCancelledError`, same as the 400 that path returns.
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
from payment_bot.errors import ClientError, LoadCancelledError
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

#: Statuses Transport Pro returns on ``payment_information`` for a load it no longer
#: holds a payable record for. Both mean cancelled; neither means the API is unwell.
_LOAD_GONE_STATUSES = frozenset({400, 404})

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
        self._payables_cache: dict[str, list[TransportProLoad]] = {}
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
            raise ClientError(f"Transport Pro: not found ({path})", status=404)
        if resp.status >= 400:
            raise ClientError(
                f"Transport Pro GET {path} failed (HTTP {resp.status})", status=resp.status
            )
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
        """The FIRST payable on the load, via ``/voiceai/load/{n}/payment_information``.

        Kept for the callers that legitimately act on one carrier at a time. Anything that
        has to be right about WHO — authorization, settlement, the reply itself — reads
        :meth:`get_load_payables` instead.
        """

        return self.get_load_payables(load_id)[0]

    def get_load_payables(self, load_id: str) -> list[TransportProLoad]:
        """Every carrier's payable on the load, in the order the API returns them.

        ``payment_information`` returns an array with **one entry per carrier that has a
        payable on the load** — not, as this client assumed, a one-element wrapper around
        "the load". A load dispatched once returns one entry and the difference never shows.
        A load re-dispatched or split across legs returns several, each with its own
        ``account_information`` (carrier *and* ``remit_to``), earnings, deductions and
        waypoints.

        Live on 2436437, three entries: FOX CARRIERS ($905 → eCapital Freight Factoring),
        Alina Transport ($150 TONU → RTS Financial Service), Parasource Inc ($5,000 line haul
        paid 06/25 plus a $230 lumper on 08/12 → England Carrier Services). Taking
        ``results[0]`` made the bot's whole picture of that load FOX CARRIERS' $905:
        Parasource's name was unknown to authorization, so they were DENIED asking about their
        own load, and their $5,000 was not merely left out of the reply — it had never been read.
        """

        key = load_id.strip()
        cached = self._payables_cache.get(key)
        if cached is not None:
            return list(cached)

        # Both "no payable record" signals mean the same thing and are translated here rather
        # than in `_get`: 400/404 is generic at that level, but on THIS path it is Transport
        # Pro saying the load was cancelled. See LoadCancelledError.
        try:
            payload = self._get(f"/voiceai/load/{urllib.parse.quote(key)}/payment_information")
        except LoadCancelledError:
            raise
        except ClientError as exc:
            if exc.status in _LOAD_GONE_STATUSES:
                raise LoadCancelledError(
                    f"Transport Pro holds no payable record for load {load_id!r} "
                    f"(HTTP {exc.status}) — the load was cancelled"
                ) from exc
            raise
        rows = self._results(payload, path="payment_information")
        if not rows:
            raise LoadCancelledError(
                f"Transport Pro holds no payable record for load {load_id!r} "
                "(empty result) — the load was cancelled"
            )

        payables = [self._payable(row, key, load_id) for row in rows]
        if len(payables) > 1:
            # Worth a log line of its own: it decides which carrier a reply is even about,
            # and until now it was the silent difference between a complete answer and a
            # confidently wrong one.
            _log.info(
                "transport_pro_load_has_several_payables",
                extra={
                    "load_id": key,
                    "count": len(payables),
                    "carriers": [p.carrier_company for p in payables],
                },
            )

        if self._cache_loads:
            self._payables_cache[key] = list(payables)
        return payables

    def _payable(self, row: dict[str, Any], key: str, load_id: str) -> TransportProLoad:
        """Parse one ``payment_information`` entry into a payable, in app calendar space."""

        record = dict(row)
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
        return load.model_copy(
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
        """Settlement rows derived from settled earning lines, across EVERY payable.

        Transport Pro has no settlement-entries endpoint in the Public API. An earning
        line that carries a ``settlement_id`` or an ``actual_payment_date`` *is* a
        settlement record, so we surface exactly those and nothing more. An unsettled load
        yields ``[]``, which is what ``tp_get_settlement_entries`` reports as "not settled".

        Walks all payables, not just the first, and stamps each row with that payable's own
        pay-to — ``"Parasource Inc c/o England Carrier Services"``, the string the Settlement
        Entries screen shows. Both halves of the usual enquiry are then answerable from the
        row itself: which carrier the money was for, and who collected it.

        This is the whole of what was missing on 2436437. The $5,000 line haul paid 06/25 and
        the $230 lumper on 08/12 belong to Parasource's payable — the third in the array — so
        a reply built off ``results[0]`` could only ever report FOX CARRIERS' $905 and had no
        way to know the other two rows existed.
        """

        entries: list[SettlementEntry] = []
        for payable in self.get_load_payables(load_id):
            pay_to = payable.pay_to
            for earning in payable.earnings:
                if earning.settlement_id is None and earning.actual_payment_date is None:
                    continue
                entries.append(
                    SettlementEntry(
                        amount=earning.amount,
                        carrier_name=pay_to,
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

        payables = self.get_load_payables(load_id)
        # Every payable's remit-to, in order and de-duplicated: a re-dispatched load is
        # factored per leg, and naming only the first leg's factor is how a carrier gets told
        # about a factoring arrangement that is not theirs.
        factors = list(dict.fromkeys(p.factoring_company for p in payables if p.factoring_company))
        is_factoring = bool(factors)
        factoring_company = "; ".join(factors) if factors else None

        factoring_docs = [
            doc.file_type
            for doc in self.get_file_history(load_id)
            if any(marker in doc.file_type.casefold() for marker in _FACTORING_DOC_TYPES)
        ]

        document_evidence = (
            f"factoring document(s) on file: {', '.join(sorted(set(factoring_docs)))}"
            if factoring_docs
            else None
        )
        evidence: list[str] = []
        if is_factoring:
            noun = "remit-to is" if len(factors) == 1 else "remit-to per carrier is"
            evidence.append(f"{noun} {factoring_company or 'a third party'} (not self)")
        if document_evidence:
            evidence.append(document_evidence)

        return NoaFactoring(
            noa_on_file=bool(is_factoring or factoring_docs),
            factoring_company_on_file=factoring_company,
            details=(
                "; ".join(evidence)
                if evidence
                else "Remit-to self; no factoring document on file for this load."
            ),
            by_carrier=tuple(
                (p.carrier_company, p.factoring_company)
                for p in payables
                if p.carrier_company
            ),
            document_evidence=document_evidence,
        )

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        """Who may receive disclosure, assembled from every payable and the dispatch contacts.

        The API exposes no authorized-parties resource. What it does give us:

        * a carrier company per payable (``account_information.company_name``) and a carrier
          per dispatch row — enough for ``check_authorization``'s sender-domain match;
        * a factor of record per payable (``remit_to``), each eligible for FACTORING on the
          usual terms;
        * carrier-side contact emails on the dispatch record, used as the explicit
          allow-list — paired with the carrier each belongs to, so a sender recognised by
          their address or domain can be narrowed to their own leg.

        **Carriers come from all three sources, and that is the point.** This used to read one
        string — ``results[0]``'s ``company_name`` — and every other carrier on the load was
        therefore a stranger to authorization. Load 2436437 has three payables and five
        dispatch rows; the one string was FOX CARRIERS, so Parasource asking about their own
        $5,000 from ``parasourceinc.com`` matched nothing and was denied, and the denial then
        dropped the load from the answer set, which is why the payment was missing from the
        draft rather than merely unattributed.

        Dispatch rows are read whatever their status, cancelled included. Two reasons: their
        contact addresses are already on this load's allow-list (the loop below has always
        taken every row), so refusing the carrier's *name* while accepting its *mailbox* would
        be incoherent; and a carrier whose leg was cancelled has a real question about that
        leg. What they may then be TOLD is a separate matter, kept separate — see
        ``CheckAuthorizationOutput.matched_carriers`` and ``ToolContext.disclosable_carriers``.

        Anything we cannot establish is left empty, so an unrecognised sender falls through
        to DENY and the gate blocks the send.
        """

        payables = self.get_load_payables(load_id)

        carriers: list[str] = [p.carrier_company for p in payables if p.carrier_company]
        parties: tuple[tuple[str, str | None], ...] = tuple(
            (p.carrier_company, p.factoring_company) for p in payables if p.carrier_company
        )

        emails: list[str] = []
        contacts: list[tuple[str, str]] = []
        for row in self._dispatch_rows(load_id):
            assigned = row.get("assignedTo") or {}
            if not isinstance(assigned, dict):
                continue
            carrier = assigned.get("carrier")
            sources: list[Any] = [assigned.get("contacts")]
            dispatched_to: str | None = None
            if isinstance(carrier, dict):
                sources.extend([carrier.get("emailContacts"), carrier.get("contacts")])
                dispatched_to = _text(carrier.get("companyName"))
                if dispatched_to:
                    carriers.append(dispatched_to)
            for source in sources:
                found = _emails_from(source)
                emails.extend(found)
                # Keep which carrier each address belongs to. Both live on the same dispatch
                # row, and flattening them was why a sender authorized by their own domain
                # could still be answered about another carrier's leg.
                if dispatched_to:
                    contacts.extend((dispatched_to, e.lower()) for e in found)

        # Whether an NOA is INDEXED, which `remit_to.company_name` does not answer: that field
        # is keyed in by hand and is routinely empty on loads whose NOA is on file. Reuses
        # get_noa_factoring rather than re-matching the document markers, and shares this
        # method's already-cached get_load — so the marginal cost is the file-history read,
        # which the agent goes on to make anyway within the same cached client.
        try:
            noa_on_file = self.get_noa_factoring(load_id).noa_on_file
        except ClientError:
            # Never fail authorization over this. False means "we cannot say it is on file",
            # which restores the previous behaviour: pre-NOA fires and the reply asks for it.
            noa_on_file = False

        return AuthorizationContext(
            # De-duplicated case-insensitively but spelling preserved: the same carrier is
            # "FOX CARRIERS" on one screen and "Fox Carriers" on another, and a reason line
            # that lists it twice reads like two companies.
            carrier_companies=_dedupe_names(carriers),
            authorized_emails=tuple(dict.fromkeys(e.lower() for e in emails)),
            carrier_contacts=tuple(dict.fromkeys(contacts)),
            payable_parties=parties,
            # Factoring contact emails are not exposed; a factoring sender therefore
            # matches only by company-domain, and FACTORING is gated by policy anyway.
            factoring_emails=(),
            noa_on_file=noa_on_file,
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
def _dedupe_names(names: list[str]) -> tuple[str, ...]:
    """Company names in first-seen order, one entry per name, matched case-insensitively."""

    seen: dict[str, str] = {}
    for name in names:
        cleaned = name.strip()
        if cleaned:
            seen.setdefault(cleaned.casefold(), cleaned)
    return tuple(seen.values())


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
