"""Parse a CargoTel ``loadmaint.mcgi`` page into a typed load.

:func:`parse_load_html` is a **pure function of the HTML** — no network, no clock, no
config. That is the whole point of separating it from the fetch: the parser is the part
most likely to break when CargoTel changes its markup, and keeping it pure means it can be
unit-tested against saved pages instead of against a live system with a rotating cookie.

Parsing is done with the standard library's ``html.parser`` through BeautifulSoup only when
it is available, and falls back to regex over the raw HTML otherwise — see
:func:`_soup`. The page is CGI-generated table soup (97 tables, 193 inputs on a typical
load), so anchoring on structure rather than position matters: every selector here keys off
a form field ``name`` or a label cell's text, never "the third table".

What the page does *not* say plainly, and how this module resolves it:

1. **Print Docs is a menu, not a manifest.** Most entries are templates CargoTel renders on
   demand and appear on every load. Only ``AttachDoc_*`` entries (which carry a count) and
   the conditional ``Pdfgenbol05`` mean a document actually exists. See
   :mod:`payment_bot.models.cargotel`.
2. **There are two "Invoice:" blocks**, A/R (the customer) and A/P (the carrier). Only the
   A/P one matters here, and it is identified by sitting in the same little table as the
   ``Invoice Received:`` label — not by being the first match. On load 295089 the A/R block
   holds invoice ``1125584``, which a naive scrape reports as the carrier's.
3. **An unset "Invoice Received" is a dropdown, not a blank.** CargoTel renders a
   month/day/year ``<select>`` when no date has been recorded, so the cell's text is
   "Jan Feb Mar …". A value cell containing a ``<select>`` is read as absent.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from payment_bot.errors import ClientError
from payment_bot.models.cargotel import CargoTelCarrier, CargoTelDocument, CargoTelLoad

__all__ = [
    "is_invalid_order",
    "is_login_page",
    "parse_carrier_html",
    "parse_load_html",
]

#: ``type=<value>`` out of a Print Docs menu item's onclick handler.
_DOC_TYPE_RE = re.compile(r"[?&]type=(\w+)", re.IGNORECASE)
#: The "(2)" in "Invoice Attached Doc (2)".
_ATTACH_COUNT_RE = re.compile(r"\((\d+)\)\s*$")
#: The banner: "Delivered 07/02/2026". The separator is a non-breaking space in the live
#: page, so anything non-digit is allowed between the word and the date.
_STATUS_RE = re.compile(
    r"\b(Delivered|In-Route|Dispatched|Active|Inactive|Cancelled|On Hold|Plant Hold)\b\W{0,4}"
    r"(\d{2}/\d{2}/\d{4})"
)
_CARRIER_RE = re.compile(r"\bCarrier\s+([A-Z][A-Z0-9&'.,\-/ ]{2,60}?)\s+Send Bill of Lading")
_BIZ_UNIT_RE = re.compile(r"\bBiz Unit:\s*([A-Z0-9 &.\-]{2,40}?)\s+Order Type")
_US_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")


def _soup(html: str) -> Any:
    """BeautifulSoup over ``html.parser``.

    ``html.parser`` rather than ``lxml`` deliberately: it is in the standard library, so the
    only new dependency this path adds is BeautifulSoup itself. CargoTel's markup is old and
    loose (unquoted attributes, unclosed cells) and ``html.parser`` copes with it fine.
    """

    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - dependency is declared in the extra
        raise ClientError(
            "CargoTel parsing needs beautifulsoup4: pip install -e \".[cargotel]\""
        ) from exc
    return BeautifulSoup(html, "html.parser")


#: CargoTel's answer for an id that is not a load. Served with HTTP 200 and no load form,
#: which makes it indistinguishable from an expired session unless you look for this.
_INVALID_ORDER_RE = re.compile(r"Invalid\s+Order\s+ID", re.IGNORECASE)


def is_invalid_order(html: str) -> bool:
    """True when CargoTel says the id is not a load.

    Distinct from :func:`is_login_page`, and the distinction is operational rather than
    cosmetic. Both return a 200 with no load form, but they mean opposite things:

    * **Invalid Order ID** is routine and per-load. Carrier mail is full of 6-7 digit
      numbers that are not loads — the Transport Pro path has a whole label-suppression
      regex for the same problem. Observed on live mail within minutes of enabling this
      path: a "Past Due Invoices" email carried ``405445``, an invoice number, and the run
      escalated reporting an expired cookie.
    * **The login page** is systemic. Every load in every email fails, and someone needs to
      refresh the session.

    Reporting the first as the second sends whoever reads the escalation to fix a cookie
    that was never broken, while the real cause — a number that was never a load — goes
    unnoticed.
    """

    return _INVALID_ORDER_RE.search(html) is not None


def is_login_page(html: str) -> bool:
    """True when this looks like the login page rather than a load.

    A stale session cookie does not produce an error status — CargoTel answers ``200`` with
    the login form. Unattended, that turns into every load parsing as "no documents on
    file", and the bot telling a queue of carriers their paperwork is missing when it is
    not. So this is checked before parsing and the caller raises on it.

    Keyed on the *absence* of the load form rather than the presence of the word "login":
    the word appears in ordinary page furniture, whereas ``loadmaint_form__`` field names
    only exist on a real load page.
    """

    return "loadmaint_form__" not in html


def _text(node: Any) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node is not None else ""


def _parse_us_date(value: str | None) -> date | None:
    """``MM/DD/YYYY`` → date. Anything else is absent rather than an error.

    Lenient because the same cell can legitimately hold a dropdown's option list, a blank,
    or a date, and only the last is interesting.
    """

    if not value:
        return None
    match = _US_DATE_RE.search(value)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%m/%d/%Y").date()
    except ValueError:
        return None


def _selected_option(soup: Any, name: str) -> str | None:
    """The chosen option of a ``<select>``, by field name."""

    select = soup.find("select", attrs={"name": name})
    if select is None:
        return None
    for option in select.find_all("option"):
        if option.has_attr("selected"):
            return _text(option) or None
    return None


def _input_value(soup: Any, name: str) -> str | None:
    tag = soup.find("input", attrs={"name": name})
    if tag is None:
        return None
    value = (tag.get("value") or "").strip()
    return value or None


def _checkbox_checked(soup: Any, name: str) -> bool:
    """Whether a checkbox is ticked — by its ``checked`` attribute, not its presence.

    Worth its own helper because getting this wrong is silent and total. CargoTel pairs each
    checkbox with a hidden ``fs_``-prefixed "field seen" marker carrying ``value="1"``
    (``fs_ldmnt_audit_carrier_pay_hold`` beside ``ldmnt_audit_carrier_pay_hold``, the same
    pattern as ``fs_rerate_payable``). That marker is present on every page that renders the
    field, so reading it as the state reports **every** load as held — measured across nine
    real loads, where only one actually was.

    An absent checkbox is False: on an already-invoiced load CargoTel renders that section
    read-only and the input is not emitted at all.
    """

    tag = soup.find("input", attrs={"name": name, "type": "checkbox"})
    return bool(tag is not None and tag.has_attr("checked"))


def _decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None


def _labelled_cell(scope: Any, label: str) -> Any:
    """The ``<td>`` following the one whose text is ``label``, searched within ``scope``.

    The A/P invoice block is a two-column table of label/value cells, which is stable across
    the sample pages and independent of where that block sits in the 97-table document.

    ``scope`` matters: searching the whole page for ``Invoice:`` finds the **A/R** block
    first on any page that renders one as a label/value pair, and reports the customer's
    invoice as the carrier's. Callers pass the enclosing table of the ``Invoice Received:``
    label — see :func:`_ap_invoice_block`.
    """

    if scope is None:
        return None
    wanted = label.rstrip(":").strip().casefold()
    for cell in scope.find_all("td"):
        if _text(cell).rstrip(":").strip().casefold() == wanted:
            return cell.find_next_sibling("td")
    return None


def _ap_invoice_block(soup: Any) -> Any:
    """The little table holding the carrier's ``Invoice:`` / ``Invoice Received:`` pair.

    Anchored on ``Invoice Received:``, which is the one label unique to the A/P block — the
    A/R block carries an ``Invoice:`` of its own but never a received date. Returning the
    enclosing table (rather than the whole page) is what keeps the two apart.
    """

    for cell in soup.find_all("td"):
        if _text(cell).rstrip(":").strip().casefold() == "invoice received":
            return cell.find_parent("table") or cell.parent
    return None


def _print_docs(soup: Any) -> tuple[CargoTelDocument, ...]:
    """Every Print Docs menu entry, with its type and attachment count."""

    menu = soup.find("span", id="menuprintdocs")
    if menu is None:
        return ()

    documents: list[CargoTelDocument] = []
    for item in menu.find_all("div", class_="menuItem"):
        label = _text(item)
        handler = item.get("onClick") or item.get("onclick") or ""
        match = _DOC_TYPE_RE.search(handler)
        if match is None:
            continue
        doc_type = match.group(1)
        count_match = _ATTACH_COUNT_RE.search(label)
        documents.append(
            CargoTelDocument(
                label=label,
                doc_type=doc_type,
                # A count only means something on an attachment entry; a generated template
                # never has one, and None keeps "not counted" distinct from "zero files".
                attached_count=(
                    int(count_match.group(1))
                    if count_match and doc_type.startswith("AttachDoc_")
                    else None
                ),
            )
        )
    return tuple(documents)


#: The carrier's client id, from the carrier panel: ``<td id='max_carrier'> ID: 74553 …``.
_CARRIER_ID_RE = re.compile(r"\bID:\s*(\d+)")

#: Contact fields on a client record. Listed rather than swept by pattern so a field that
#: is *not* a contact — a Sentry DSN, an internal notification alias — cannot become an
#: authorization signal by accident.
_CARRIER_EMAIL_FIELDS = (
    "email",
    "carrier_dispatch_email",
    "notify_email",
    "dba_email",
    "invoice_email",
    "bid_email",
)

#: "BULLA TRANSPORTATION INC (74553)" from the record's heading.
#:
#: Restricted to upper-case name characters on purpose. A looser class captured the heading
#: itself — the page renders "Review Account - carrier" twice before the name, and a lazy
#: ``[^()]+?`` happily swallowed both.
_CARRIER_NAME_RE = re.compile(r"([A-Z][A-Z0-9&'.,\-/ ]{2,60}?)\s*\((\d+)\)")

#: The contacts grid is delivered as a JavaScript array literal, not as form fields:
#: ``var clientContactData = [{"fname":"RUSLAN", … "email":"ALGA…@GMAIL.COM", …}, …]``.
#: Those per-contact addresses are the closest thing this path has to Transport Pro's
#: dispatch contacts, and they are invisible to any form-field scrape — ALGA's second
#: address lives only here.
_CONTACT_DATA_RE = re.compile(r"clientContactData\s*=\s*(\[)", re.IGNORECASE)
_JSON_EMAIL_RE = re.compile(r'"email"\s*:\s*"([^"]+)"', re.IGNORECASE)


def _contact_emails(html: str) -> list[str]:
    """Addresses from the ``clientContactData`` array.

    The array is sliced out by matching brackets rather than by a greedy regex, because a
    contact's free-text fields can contain ``]``. Addresses are then read with a regex
    instead of ``json.loads``: this is a JS literal, not guaranteed-strict JSON, and a
    trailing comma or an unquoted key would otherwise cost every contact on the record.
    """

    match = _CONTACT_DATA_RE.search(html)
    if match is None:
        return []

    start = match.start(1)
    depth, in_string, escaped, end = 0, False, False, None
    for index in range(start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        return []

    return [m.group(1).strip() for m in _JSON_EMAIL_RE.finditer(html[start:end])]


def parse_carrier_html(html: str, client_id: str) -> CargoTelCarrier:
    """Parse a ``client.mcgi?id=<client_id>`` carrier record.

    Pure, for the same reason :func:`parse_load_html` is.

    Raises:
        ClientError: If the page carries no client form — the stale-cookie case again.
    """

    if "factoring_name" not in html and "Review Account" not in html:
        raise ClientError(
            f"CargoTel returned no client record for {client_id!r} — the session cookie is "
            "probably expired. Refusing to parse it as a carrier with no contacts."
        )

    soup = _soup(html)
    text = " ".join(soup.get_text(" ", strip=True).split())
    # Anchored on the record's own id, so the "NAME (id)" match is the heading and not some
    # other parenthesised number further down the page.
    name = None
    for candidate in _CARRIER_NAME_RE.finditer(text):
        if candidate.group(2) == str(client_id).strip():
            name = candidate.group(1).strip()
            break

    emails: list[str] = []
    for value in [
        *(_input_value(soup, field) or "" for field in _CARRIER_EMAIL_FIELDS),
        *_contact_emails(html),
    ]:
        # Cheap sanity: these fields are named for addresses, but an operator can type
        # anything into one, and a non-address must never reach an authorization comparison.
        cleaned = value.strip().lower()
        if "@" in cleaned and cleaned not in emails:
            emails.append(cleaned)

    return CargoTelCarrier(
        client_id=str(client_id).strip(),
        name=name,
        factoring_name=_input_value(soup, "factoring_name"),
        ap_terms=_selected_option(soup, "ap_terms"),
        emails=tuple(emails),
    )


def parse_load_html(html: str, load_id: str) -> CargoTelLoad:
    """Parse one ``loadmaint.mcgi`` page.

    Args:
        html: The page source.
        load_id: The id that was requested. Kept as the load's identity rather than read
            back out of the page — the request is what we know for certain, and it is what
            the carrier asked about.

    Raises:
        ClientError: If the page is the login page (a stale cookie) or is not a load page.
    """

    # Order matters: an invalid order id also has no load form, so it must be recognised
    # first or every stray number reads as a broken session.
    if is_invalid_order(html):
        raise ClientError(
            f"CargoTel: no load {load_id!r} exists (Invalid Order ID). The number in the "
            "email is probably not a load id."
        )
    if is_login_page(html):
        raise ClientError(
            f"CargoTel returned a page with no load form for load {load_id!r} — the session "
            "cookie is probably expired. Refusing to parse it as an empty load."
        )

    soup = _soup(html)
    text = " ".join(soup.get_text(" ", strip=True).split())

    status_match = _STATUS_RE.search(text)
    carrier_match = _CARRIER_RE.search(text)
    biz_match = _BIZ_UNIT_RE.search(text)

    # Both invoice fields are read from inside the A/P block only. Scoping is the whole
    # point: page-wide, `Invoice:` can match the A/R block instead.
    ap_block = _ap_invoice_block(soup)
    received_cell = _labelled_cell(ap_block, "Invoice Received:")
    invoice_cell = _labelled_cell(ap_block, "Invoice:")

    # A `<select>` in the value cell is CargoTel's "no date recorded" rendering.
    received: date | None = None
    if received_cell is not None and received_cell.find("select") is None:
        received = _parse_us_date(_text(received_cell))

    invoice_number = _text(invoice_cell) if invoice_cell is not None else ""
    # Guard against picking up a date or an option list if the layout ever shifts.
    if not invoice_number or _US_DATE_RE.search(invoice_number) or len(invoice_number) > 60:
        invoice_number = ""

    carrier_panel = soup.find("td", id="max_carrier")
    carrier_id_match = _CARRIER_ID_RE.search(_text(carrier_panel)) if carrier_panel else None

    load = CargoTelLoad(
        load_id=str(load_id).strip(),
        business_unit=biz_match.group(1).strip() if biz_match else None,
        carrier_client_id=carrier_id_match.group(1) if carrier_id_match else None,
        status=status_match.group(1) if status_match else None,
        status_date=_parse_us_date(status_match.group(2)) if status_match else None,
        carrier_name=carrier_match.group(1).strip() if carrier_match else None,
        ap_terms=_selected_option(soup, "loadmaint_form__APTerms"),
        ap_invoice_number=invoice_number or None,
        invoice_received=received,
        payable=_decimal(_input_value(soup, "loadmaint_form__ye_olde_payable")),
        pay_hold=_checkbox_checked(soup, "loadmaint_form__ldmnt_audit_carrier_pay_hold"),
        documents=_print_docs(soup),
    )

    # The third "that is not a load", and the quietest: the form rendered with no order in
    # it. Judged on the parsed load rather than the markup, because "no order here" is a
    # question about content — see CargoTelLoad.carries_no_order for the measurement behind
    # it. Left unchecked, the caller reads an all-empty load as a real record with missing
    # data and sends someone to add a contact address to a load that does not exist.
    if load.carries_no_order:
        raise ClientError(
            f"CargoTel: no load {load_id!r} exists — the page came back with no order on it "
            "(no business unit, status, carrier or terms). The number in the email is "
            "probably not a load id."
        )
    return load
