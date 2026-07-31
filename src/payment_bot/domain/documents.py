"""Document classification and the missing-document check (pure, no I/O).

Transport Pro's file search returns one row per uploaded file, typed by ``fileTypeId``
against a 367-entry vocabulary. A single load routinely carries a dozen rows — four copies
of the rate agreement, two invoices, a billing packet — which answers "what is on file" but
not the question anyone actually asks: **what is missing?**

This module turns rows into categories and categories into a missing list.

Classification keys on ``fileTypeId`` because it is stable; ``fileTypeName`` is only a
fallback for ids we have not catalogued, and it is matched on word boundaries. That detail
matters: a substring test for ``"bol"`` also fires on "symbolic" and "bollard", which would
silently report proof of delivery that does not exist.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class DocCategory(StrEnum):
    """What a document *is*, independent of how this tenant names it."""

    CARRIER_INVOICE = "carrier_invoice"
    #: BOL or any delivery receipt — the PRD treats these as one "did it deliver" signal.
    PROOF_OF_DELIVERY = "proof_of_delivery"
    RATE_AGREEMENT = "rate_agreement"
    FREIGHT_BILL = "freight_bill"
    BILLING_PACKET = "billing_packet"
    LOAD_SHEET = "load_sheet"
    FACTORING = "factoring"
    SETTLEMENT = "settlement"
    REMITTANCE = "remittance"
    OTHER = "other"


#: ``fileTypeId`` → category, from the live ``GET /files/document_types`` vocabulary.
#: Ids are stable per tenant; names are not (and are localised).
TYPE_ID_TO_CATEGORY: dict[int, DocCategory] = {
    22: DocCategory.CARRIER_INVOICE,
    103: DocCategory.CARRIER_INVOICE,
    12: DocCategory.PROOF_OF_DELIVERY,   # Bill of Lading
    335: DocCategory.PROOF_OF_DELIVERY,  # International BOL
    20: DocCategory.PROOF_OF_DELIVERY,   # Carrier Delivery Receipt
    53: DocCategory.PROOF_OF_DELIVERY,   # Delivery Receipt
    23: DocCategory.RATE_AGREEMENT,      # Carrier Rate Agreement
    81: DocCategory.FREIGHT_BILL,
    319: DocCategory.BILLING_PACKET,
    344: DocCategory.LOAD_SHEET,
    21: DocCategory.FACTORING,           # Carrier Factoring Agr/Rel
    76: DocCategory.FACTORING,           # Factoring Agreement/Releases
    77: DocCategory.SETTLEMENT,          # Final Settlement
    315: DocCategory.REMITTANCE,         # Check Remittance
    16: DocCategory.REMITTANCE,          # Cancelled Check
    123: DocCategory.OTHER,
}

#: Word-boundary name patterns, used only when the id is unknown to us.
_NAME_PATTERNS: tuple[tuple[re.Pattern[str], DocCategory], ...] = (
    (re.compile(r"\bcarrier\s+invoice\b", re.I), DocCategory.CARRIER_INVOICE),
    (re.compile(r"\bbill\s+of\s+lading\b|\bbol\b|\bpod\b|\bproof\s+of\s+delivery\b"
                r"|\bdelivery\s+receipt\b", re.I), DocCategory.PROOF_OF_DELIVERY),
    (re.compile(r"\brate\s+(agreement|confirmation)\b", re.I), DocCategory.RATE_AGREEMENT),
    (re.compile(r"\bfreight\s+bill\b", re.I), DocCategory.FREIGHT_BILL),
    (re.compile(r"\bbilling\s+packet\b", re.I), DocCategory.BILLING_PACKET),
    (re.compile(r"\bload\s+sheet\b", re.I), DocCategory.LOAD_SHEET),
    (re.compile(r"\bfactoring\b|\bnotice\s+of\s+assignment\b", re.I), DocCategory.FACTORING),
    (re.compile(r"\bsettlement\b", re.I), DocCategory.SETTLEMENT),
    (re.compile(r"\bremittance\b|\bcancelled\s+check\b", re.I), DocCategory.REMITTANCE),
    (re.compile(r"\binvoice\b", re.I), DocCategory.CARRIER_INVOICE),
)

#: What a load needs on file before it can be paid, per PRD §3.1 / §4.3.
#: Deliberately short — every entry here becomes something a carrier gets told is missing.
REQUIRED_FOR_PAYMENT: tuple[DocCategory, ...] = (
    DocCategory.CARRIER_INVOICE,
    DocCategory.PROOF_OF_DELIVERY,
    DocCategory.RATE_AGREEMENT,
)

#: A cancel confirmation is recorded in a comment, not a type — and it escalates (§3.2).
_CANCEL_RE = re.compile(r"\bcancel\s+load\b", re.I)


class ClassifiedDocument(BaseModel):
    """One file, reduced to what matters for the missing-document question."""

    model_config = ConfigDict(frozen=True)

    category: DocCategory
    file_type: str
    file_type_id: int | None = None
    uploaded: date | None = None
    comments: str | None = None
    matches_load: bool = False


class CategorySummary(BaseModel):
    """All files of one category, collapsed."""

    model_config = ConfigDict(frozen=True)

    category: DocCategory
    count: int
    latest: date | None = None


class DocumentStatus(BaseModel):
    """Which required documents a load has, and which it lacks."""

    model_config = ConfigDict(frozen=True)

    document_count: int
    present: list[DocCategory]
    missing: list[DocCategory]
    by_category: list[CategorySummary]
    has_cancel_confirmation: bool = False

    @property
    def is_complete(self) -> bool:
        return not self.missing


def classify(
    file_type: str,
    file_type_id: int | None = None,
) -> DocCategory:
    """Map one file to a category — by id first, then by a word-boundary name match."""

    if file_type_id is not None and file_type_id in TYPE_ID_TO_CATEGORY:
        return TYPE_ID_TO_CATEGORY[file_type_id]
    for pattern, category in _NAME_PATTERNS:
        if pattern.search(file_type):
            return category
    return DocCategory.OTHER


def _load_id_in(text: str | None, load_id: str) -> bool:
    """Does this comment reference the load, as a whole number?

    Substring matching would make load ``246293`` match a comment about ``2462934``.
    """

    if not text or not load_id:
        return False
    return re.search(rf"(?<!\d){re.escape(load_id)}(?!\d)", text) is not None


def assess_documents(
    documents: Iterable[tuple[str, int | None, date | None, str | None]],
    load_id: str = "",
    required: tuple[DocCategory, ...] = REQUIRED_FOR_PAYMENT,
) -> tuple[DocumentStatus, list[ClassifiedDocument]]:
    """Classify a load's files and report what is missing.

    Args:
        documents: ``(file_type, file_type_id, uploaded, comments)`` per file.
        load_id: Used to mark which files reference this load in their comments.
        required: Categories a payable load must have.

    Returns:
        The status, and the classified documents behind it.
    """

    classified: list[ClassifiedDocument] = []
    counts: dict[DocCategory, int] = {}
    latest: dict[DocCategory, date | None] = {}
    has_cancel = False

    for file_type, type_id, uploaded, comments in documents:
        category = classify(file_type, type_id)
        classified.append(
            ClassifiedDocument(
                category=category,
                file_type=file_type,
                file_type_id=type_id,
                uploaded=uploaded,
                comments=comments,
                matches_load=_load_id_in(comments, load_id),
            )
        )
        counts[category] = counts.get(category, 0) + 1
        known = latest.get(category)
        if uploaded and (known is None or uploaded > known):
            latest[category] = uploaded
        elif category not in latest:
            latest[category] = known

        if _CANCEL_RE.search(f"{file_type} {comments or ''}"):
            has_cancel = True

    present = sorted(counts, key=lambda c: c.value)
    status = DocumentStatus(
        document_count=len(classified),
        present=present,
        missing=[c for c in required if c not in counts],
        by_category=[
            CategorySummary(category=c, count=counts[c], latest=latest.get(c)) for c in present
        ],
        has_cancel_confirmation=has_cancel,
    )
    return status, classified
