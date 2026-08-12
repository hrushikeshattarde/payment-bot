"""A synthetic CargoTel ``loadmaint.mcgi`` page, for parser tests.

Synthetic rather than a saved real page, because real pages carry carrier names, contact
emails, VINs and payable amounts and this repository is public. What is reproduced here is
the *structure* the parser depends on, copied field-for-field from real pages for loads
296993, 296006 and 295089:

* the status banner, with the non-breaking space CargoTel actually emits between the word
  and the date;
* ``select[name=loadmaint_form__APTerms]`` with a single selected option;
* the Print Docs hover menu, mixing always-available generated types with conditional ones
  and ``AttachDoc_*`` entries carrying counts;
* **two** ``Invoice:`` blocks — an A/R one and an A/P one — because the A/P block is
  identified by sitting beside ``Invoice Received:``, and a parser that takes the first
  match reports the customer's invoice as the carrier's;
* the month ``<select>`` CargoTel renders in the received-date cell when no date is set.

``build_page`` keeps every one of those switchable so a test can express a state rather
than hand-editing HTML.
"""

from __future__ import annotations

#: Always offered, on every load — these prove nothing about what was received.
_BASE_DOCS: tuple[tuple[str, str], ...] = (
    ("Image BOL", "ImageBol"),
    ("Standard BOL", "StdBol"),
    ("Trip Sheet", "TripSheet"),
    ("Manifest", "Manifest"),
    ("Carrier Agreement", "CarrierAgreementAlt"),
    ("Pre-Invoice", "PreInvoiceBolAlt"),
)


def _menu_item(label: str, doc_type: str) -> str:
    handler = (
        f"location.href='/backoffice/document.mcgi?mode=print&amp;type={doc_type}"
        f"&amp;load_id=999999'"
    )
    return f'<div class="menuItem" onClick="{handler}">{label}</div>'


def build_page(
    *,
    load_id: str = "296006",
    carrier: str = "EXAMPLE TRUCKING LLC",
    business_unit: str = "CIRCLE LOGISTICS",
    status: str = "Delivered",
    status_date: str = "07/02/2026",
    ap_terms: str | None = "Check Net 30",
    ap_invoice: str | None = "296006-INV123",
    invoice_received: str | None = "07/07/2026",
    payable: str = "2000.00",
    pay_hold: bool = False,
    bol05: bool = True,
    carrier_invoices: int | None = 1,
    carrier_agreement: bool = True,
    ar_invoice: str = "1125584",
) -> str:
    """Render a page in whatever billing state the test needs.

    Args:
        ap_terms: ``None`` renders the select with nothing selected, which is how a load
            with unset terms actually appears.
        invoice_received: ``None`` renders the month dropdown CargoTel uses for "no date".
        carrier_invoices: ``None`` omits the attachment entry entirely (nothing uploaded);
            an int renders ``Invoice Attached Doc (n)``.
    """

    docs = list(_BASE_DOCS)
    if bol05:
        docs.insert(2, ("BOL 05 Dealer", "Pdfgenbol05"))
    if carrier_agreement:
        docs.append(
            ("E-Signed Carrier Agreement Attached Doc (1)", "AttachDoc_CarrierAgreementAlt")
        )
    if carrier_invoices is not None:
        docs.append((f"Invoice Attached Doc ({carrier_invoices})", "AttachDoc_Invoice"))
    menu = "".join(_menu_item(label, doc_type) for label, doc_type in docs)

    if ap_terms is None:
        terms_select = '<select name="loadmaint_form__APTerms"><option></option></select>'
    else:
        terms_select = (
            f'<select name="loadmaint_form__APTerms">'
            f'<option selected>{ap_terms}</option></select>'
        )

    if invoice_received is None:
        received_cell = (
            "<td><select name='inv_recv_month'>"
            "<option>Jan</option><option>Feb</option><option>Mar</option>"
            "</select></td>"
        )
    else:
        received_cell = f"<td>{invoice_received}</td>"

    # The hidden "field seen" marker is emitted whenever the section renders, whether or not
    # the box is ticked — reproduced verbatim because reading *it* as the hold state marks
    # every load as held, which is exactly the bug this fixture exists to catch.
    checked = " checked" if pay_hold else ""
    hold = (
        '<input type="hidden" name="loadmaint_form__fs_ldmnt_audit_carrier_pay_hold" value="1">'
        '<input type="checkbox" name="loadmaint_form__ldmnt_audit_carrier_pay_hold"'
        f' value="Y"{checked}>'
    )

    # \xa0 is the separator the live banner uses between the status word and the date.
    return f"""<html><head><title>CargoTel /backoffice/loadmaint.mcgi</title></head><body>
<form name="loadmaint_form">
  <table><tr><td>Load:{load_id}</td><td>Biz Unit: {business_unit}</td>
             <td>Order Type / Related Orders</td></tr></table>
  <table><tr><td>{status}\xa0{status_date} - On Hold &gt;&gt;&gt; Inactive</td></tr></table>
  <table><tr><td class="menuHeader"><b>Print Docs</b></td></tr>
         <tr><td><span id="menuprintdocs" class="menuContainer">{menu}</span></td></tr></table>

  <table><tr><td>AP Terms</td><td>{terms_select}</td>
             <td>Carrier {carrier} Send Bill of Lading</td></tr></table>

  <!-- A/R block: has an Invoice: label but never an Invoice Received: one. -->
  <table><tr class="shadow"><td colspan="11">
    <table><tr><td>Invoice: </td><td>{ar_invoice}</td><td>07/06/2026</td></tr></table>
  </td></tr></table>

  <!-- A/P block: the one the parser must pick. -->
  <table><tr class="shadow"><td colspan="11">
    <table>
      <tr><td>Invoice: </td><td>{ap_invoice or ""}</td><td></td></tr>
      <tr><td>Invoice Received: </td>{received_cell}<td></td></tr>
    </table>
  </td></tr></table>

  <input name="loadmaint_form__ye_olde_payable" value="{payable}">
  {hold}
</form></body></html>"""


#: CargoTel's answer for an id that is not a load: HTTP 200, no load form, and this phrase.
#: Verified against the live system for id 405445, an invoice number that reached the bot
#: from a "Past Due Invoices" email.
INVALID_ORDER_PAGE = """<html><head><title>CargoTel /backoffice/loadmaint.mcgi</title></head>
<body>Invalid Order ID Copyright Information Feedback &amp; Support</body></html>"""


#: CargoTel's THIRD answer for an id it does not have: HTTP 200 with the real load form and
#: no order in it. No "Invalid Order ID" anywhere, not the login page, so both of the guards
#: above pass and it parses into a load whose every field is empty. Verified against the live
#: system for 246558 and 318354 — 179KB and no order on either.
#:
#: Built from build_page so it stays a genuine load form: the same markup a real load uses,
#: with every headline value emptied. A hand-written stub would not prove the guard works,
#: because the guard's whole job is telling a real form apart from a real load.
BLANK_LOAD_PAGE = build_page(
    carrier="",
    business_unit="",
    status="",
    status_date="",
    ap_terms=None,
    ap_invoice=None,
    invoice_received=None,
    payable="",
    bol05=False,
    carrier_invoices=None,
    carrier_agreement=False,
)


LOGIN_PAGE = """<html><body><form name="login">
<input name="username"><input type="password" name="password">
</form></body></html>"""


def build_carrier_page(
    *,
    client_id: str = "74553",
    name: str = "EXAMPLE TRUCKING INC",
    factoring_name: str | None = "SAINT JOHN CAPITAL C/O EXAMPLE TRUCKING INC",
    ap_terms: str | None = "Check Net 30",
    email: str | None = "example@gmail.com",
    dispatch_email: str | None = "dispatch@exampletrucking.com",
    contact_emails: tuple[str, ...] = ("RUSLAN@EXAMPLETRUCKING.COM",),
) -> str:
    """Render a ``client.mcgi?id=<client_id>`` carrier record.

    Reproduces the two things a form-field scrape alone would miss: the heading is preceded
    by the literal text "Review Account - carrier" *twice* (a loose name pattern swallows
    both), and the contacts grid ships as a JavaScript array rather than as inputs — which
    is where one real carrier's only second address lives.
    """

    contacts = ", ".join(
        f'{{"fname":"CONTACT{i}","department":"Executive","title":null,'
        f'"email":"{addr}","primary_phone":"5551234567","alt_phone":null}}'
        for i, addr in enumerate(contact_emails)
    )
    terms = (
        f'<select name="ap_terms"><option selected>{ap_terms}</option></select>'
        if ap_terms
        else '<select name="ap_terms"><option></option></select>'
    )

    def field(field_name: str, value: str | None) -> str:
        return f'<input name="{field_name}" value="{value or ""}">'

    return f"""<html><head><title>Review Account - carrier</title></head><body>
<h1>Review Account - carrier</h1>
<div>Review Account - carrier {name} ({client_id}) Parent: 62942 CIRCLE LOGISTICS</div>
<form>
  {field("factoring_name", factoring_name)}
  {terms}
  {field("email", email)}
  {field("carrier_dispatch_email", dispatch_email)}
  {field("notify_email", email)}
  {field("invoice_email", None)}
</form>
<script>var clientContactData = [{contacts}];</script>
</body></html>"""
