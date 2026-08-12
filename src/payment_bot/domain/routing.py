"""Load routing by ID length (PRD §4.1 / §4.2 ``route_load``).

The rule is intentionally trivial and total: **6 digits → QuickBooks, 7 digits →
Transport Pro, anything else → invalid (do not look up).** Length is the *only* signal;
we never guess a system for an out-of-range ID.

**Length does not partition the two systems, and a Transport Pro fallback for 6-digit ids
would be a disclosure bug, not a fix.** Transport Pro numbered its loads with six digits
years ago and still serves them, so a 6-digit id can resolve in both systems. Measured
against the live APIs:

* Every 6-digit Transport Pro load found settled in 2018 or 2019 — 246558 (Stockton Farms,
  paid 2018-08-11), 318354 (King Logistics, 2019-03-07), 316040 (Ma Trucks, 2019-02-27),
  318410 (Holiday Transport, 2019-03-06).
* The CargoTel loads sharing two of those numbers were delivered 2026-08-10 and are still
  unpaid. So for any *live* question the CargoTel load is the one being asked about, and
  preferring it is correct.
* Six-digit Transport Pro loads are not rare curiosities. 999998, 111111, 222222 and 555555
  are all real, distinct, differently-carriered loads. So an arbitrary 6-digit number stands
  a fair chance of matching one **by coincidence**.

That last point is what rules out the obvious-looking improvement. Falling back to Transport
Pro when CargoTel has no such load would mostly surface those coincidences: a sender's own
reference number would resolve to a stranger's archived load, and answering it would disclose
an unrelated carrier's payment history to whoever happened to quote the number. Observed on a
Neon Freight email whose "Ref No" column held 246558 — which is Stockton Farms', settled
seven years earlier, and nothing to do with the sender.

The two systems also compute pay dates by different rules (Monday/Thursday against
invoice-received plus term), so a reply about one produced by the other's skill would be
wrong even where authorization allowed it. A 6-digit id CargoTel does not have therefore
escalates. It does not get looked up elsewhere.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from payment_bot.models.enums import System


class RouteResult(BaseModel):
    """Outcome of routing a single load id."""

    model_config = ConfigDict(frozen=True)

    system: System
    length: int


def route_load(load_id: str) -> RouteResult:
    """Route a load id to its owning system by length.

    Args:
        load_id: The candidate identifier as extracted from the email.

    Returns:
        A :class:`RouteResult`. Non-numeric or wrong-length ids route to
        :attr:`System.INVALID` and must not be looked up (§5 length-routing check).
    """

    normalized = load_id.strip()

    if not normalized.isdigit():
        return RouteResult(system=System.INVALID, length=len(normalized))

    length = len(normalized)
    if length == 7:
        return RouteResult(system=System.TRANSPORT_PRO, length=length)
    if length == 6:
        return RouteResult(system=System.QUICKBOOKS, length=length)
    return RouteResult(system=System.INVALID, length=length)
