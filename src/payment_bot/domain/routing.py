"""Load routing by ID length (PRD §4.1 / §4.2 ``route_load``).

The rule is intentionally trivial and total: **6 digits → QuickBooks, 7 digits →
Transport Pro, anything else → invalid (do not look up).** Length is the *only* signal;
we never guess a system for an out-of-range ID.
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
