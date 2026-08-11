"""Grounding ledger — the audit trail the pre-send gate checks a draft against.

PRD §5 requires that *every amount and date* in a reply trace back to a tool result.
As tools run they **record the facts they produced** here (amounts, dates, statuses,
check numbers…). The gate later extracts the amounts and dates that appear in the draft
and verifies each one is present in this ledger. Anything unaccounted for blocks the send.

The extraction is deliberately conservative and format-driven:

* **Money** is recognised only in monetary form ($-prefixed, thousands-separated, or a
  two-decimal fraction) — the reply template always renders money that way, so real
  amounts are caught while incidental counts in prose ("2 earning lines") are ignored.
* **Dates** are recognised as ISO (``YYYY-MM-DD``) or ``Month DD, YYYY``.

A weekday *name* is not a groundable token — nothing in a tool result is a weekday word to
compare against — so :func:`find_weekday_mismatches` checks it arithmetically instead, from
the date it is printed beside.

Money is compared as :class:`~decimal.Decimal`, so ``$4,650`` and ``4650.00`` match.
This is a heuristic that errs toward *blocking*; it is not a natural-language checker.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

# --- token extraction -------------------------------------------------------
_MONEY_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?"  # $-prefixed: $150, $4,650.00
    r"|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"  # thousands-separated: 4,650
    r"|\b\d+\.\d{2}\b"  # two-decimal fraction: 150.00
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TEXT_DATE_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip


def extract_money_tokens(text: str) -> set[Decimal]:
    """Return the distinct monetary amounts appearing in ``text`` as Decimals."""

    out: set[Decimal] = set()
    for match in _MONEY_RE.finditer(text):
        cleaned = match.group().replace("$", "").replace(",", "").strip()
        try:
            out.add(Decimal(cleaned))
        except InvalidOperation:  # pragma: no cover - regex guarantees a number
            continue
    return out


#: Weekday names, Monday-first to match :meth:`datetime.date.weekday`.
#:
#: Spelled out rather than read from ``strftime("%A")`` so this check cannot change meaning
#: under a non-English locale, and deliberately not imported from ``domain.pay_schedule``:
#: this module stays stdlib-only, so the gate's arithmetic never borrows the same table as
#: the code it is checking.
_WEEKDAY_NAMES: tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)  # fmt: skip

#: Spellings a reply might use → weekday index.
_WEEKDAY_INDEX: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}  # fmt: skip

#: Longest-first, so "monday" is preferred over the "mon" prefix.
_WEEKDAY_ALT = "|".join(sorted(_WEEKDAY_INDEX, key=len, reverse=True))

#: ``<Weekday>, <Month> D, YYYY`` or ``<Weekday>, YYYY-MM-DD``.
_WEEKDAY_DATE_RE = re.compile(
    rf"\b({_WEEKDAY_ALT})\b\.?,?\s+"
    r"(?:([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"
    r"|(\d{4})-(\d{2})-(\d{2}))",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class WeekdayMismatch:
    """A weekday named in a draft that is not the weekday of the date beside it."""

    stated: str  # as written in the draft
    value: date
    correct: str


def find_weekday_mismatches(text: str) -> list[WeekdayMismatch]:
    """Return every ``<Weekday>, <date>`` in ``text`` whose weekday is wrong for that date.

    The ledger cannot catch this. Grounding compares *dates*, so "Monday, August 11, 2026"
    grounds cleanly on a run that produced 2026-08-11 while the weekday word is compared
    against nothing at all. Observed live on load 2481130: ``compute_scheduled_pay_date``
    returned "Tuesday", the reply said "Monday" for that same date and cited the tool for
    it, and all eleven checks passed.

    This is pure arithmetic against the date itself, so it holds however the weekday was
    arrived at — whether the model invented it or faithfully copied a field that did not
    describe the date it was printed next to.
    """

    out: list[WeekdayMismatch] = []
    seen: set[tuple[int, date]] = set()
    for match in _WEEKDAY_DATE_RE.finditer(text):
        index = _WEEKDAY_INDEX[match.group(1).lower()]
        if match.group(5):
            parts = (int(match.group(5)), int(match.group(6)), int(match.group(7)))
        else:
            month_num = _MONTHS.get((match.group(2) or "").lower())
            if month_num is None:
                # Not a date — e.g. "we pay on Monday, and August work is billed later".
                continue
            parts = (int(match.group(4)), month_num, int(match.group(3)))
        try:
            value = date(*parts)
        except ValueError:
            continue
        if value.weekday() == index or (index, value) in seen:
            continue
        seen.add((index, value))
        out.append(
            WeekdayMismatch(
                stated=match.group(1),
                value=value,
                correct=_WEEKDAY_NAMES[value.weekday()],
            )
        )
    return out


def extract_date_tokens(text: str) -> set[date]:
    """Return the distinct calendar dates appearing in ``text`` (ISO or ``Month DD, YYYY``)."""

    out: set[date] = set()
    for iso in _ISO_DATE_RE.finditer(text):
        try:
            out.add(date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))))
        except ValueError:
            continue
    for textual in _TEXT_DATE_RE.finditer(text):
        month_num = _MONTHS.get(textual.group(1).lower())
        if month_num is None:
            continue
        try:
            out.add(date(int(textual.group(3)), month_num, int(textual.group(2))))
        except ValueError:
            continue
    return out


# --- ledger -----------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GroundedFact:
    """One fact a tool asserted, retained for audit and the gate's grounding check."""

    kind: str  # amount | date | scheduled_pay_date | status | method | check_ref | carrier
    value: str
    source_tool: str
    load_id: str | None = None


@dataclass(slots=True)
class GroundingLedger:
    """Accumulates grounded facts across a single email run."""

    facts: list[GroundedFact] = field(default_factory=list)
    grounded_amounts: set[Decimal] = field(default_factory=set)
    grounded_dates: set[date] = field(default_factory=set)

    def record_amount(self, amount: Decimal, source_tool: str, load_id: str | None = None) -> None:
        """Record a tool-produced amount, keyed by magnitude.

        Sign is a presentation choice, not provenance. Transport Pro returns deductions as
        negatives (``-11.25``), and a correct reply naturally writes "a deduction of $11.25" —
        so a signed comparison blocked a draft whose every figure was genuinely grounded.
        Storing the magnitude keeps the check answering the question it actually asks: did a
        tool produce this number? It never claimed to police meaning, and could not — gross
        and net are both grounded, and it cannot tell which belongs where.
        """

        self.grounded_amounts.add(abs(amount))
        self.facts.append(GroundedFact("amount", str(amount), source_tool, load_id))

    def record_date(
        self,
        value: date,
        source_tool: str,
        load_id: str | None = None,
        *,
        kind: str = "date",
    ) -> None:
        self.grounded_dates.add(value)
        self.facts.append(GroundedFact(kind, value.isoformat(), source_tool, load_id))

    def record_text(
        self,
        kind: str,
        value: str,
        source_tool: str,
        load_id: str | None = None,
    ) -> None:
        """Record a non-numeric fact (status, method, carrier, check reference).

        If the value is numeric (e.g. an all-digit check number), it is also added to the
        grounded-amounts set so it can appear in the reply without tripping the gate.
        """

        self.facts.append(GroundedFact(kind, value, source_tool, load_id))
        stripped = value.replace(",", "").strip()
        with contextlib.suppress(InvalidOperation):
            self.grounded_amounts.add(Decimal(stripped))
