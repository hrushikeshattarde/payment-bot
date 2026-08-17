"""A dispatch row carries no date a reply may quote.

Live regression, load 2478889 (Forza Transportation Services, 2026-08-15). Barbara Barrón at
Forza asked for payment status on PO# 2478889, mentioning "this was sent on 08/06". The draft
replied "The load was delivered on August 6, 2026" and cited ``tp_get_dispatch_history``.

The load delivered on **2026-06-30**. August 6 is the Dispatch History screen's *Last Updated*
column — ``lastUpdated: 2026-08-06T17:07:38Z``, shown in the UI beside the editing user's name.
Five weeks out, on a reply to a collections analyst working payment terms.

The gate caught it on grounding, because nothing had recorded that date. This pins the reason
it was reachable at all: the row's Pickup and Delivery cells stack a place above a date, only
the place is parsed, and ``last_updated`` is then the sole date-shaped field on the row.

The fix is documentation, not a new field. The dispatch row's own waypoints DO carry stop times
(``appointmentTime.open``), but the consignee one reads 2026-06-27 while the load's delivery
waypoint reads 2026-06-30 — the value the UI actually shows. Surfacing both would add a second
plausible "delivery date" rather than remove the ambiguity, so ``tp_get_load_summary`` stays the
single source, and it is the only tool that grounds it.
"""

from __future__ import annotations

import pytest

from payment_bot.models import DispatchRow
from payment_bot.tools.transport_pro import TpGetDispatchHistory, TpGetLoadSummary

pytestmark = pytest.mark.unit

#: The live row, as the screen showed it.
ROW = DispatchRow(
    carrier_name="Forza Transportation Services Inc",
    mc_number="862551",
    dispatch_status="Delivered",
    pickup="WHITAKERS, NC",
    delivery="LAREDO, TX",
    last_updated="2026-08-06T17:07:38Z",
    comment="Dispatched via Transport Pro carrier booking page.",
)


def test_pickup_and_delivery_are_places_not_dates() -> None:
    """What the parser keeps from those two cells, and it is not the date beside it."""

    assert ROW.pickup == "WHITAKERS, NC"
    assert ROW.delivery == "LAREDO, TX"
    assert ROW.is_delivered is True


def test_the_only_date_shaped_field_is_a_record_stamp() -> None:
    """2026-08-06 against a 2026-06-30 delivery — the exact confusion that shipped."""

    assert ROW.last_updated is not None
    assert ROW.last_updated.startswith("2026-08-06")


def test_the_tool_description_says_it_returns_no_usable_date() -> None:
    """The model reads the description, so the warning has to live there.

    Pinned because the previous description said only "use the Delivered row for carrier and
    rate", which forbids nothing about dates and left `last_updated` looking quotable.
    """

    description = TpGetDispatchHistory.description

    assert "NO usable date" in description
    assert "PLACES" in description
    assert "never a delivery, dispatch or payment date" in description
    # And it names where the date does come from, or the model has nowhere to go.
    assert "tp_get_load_summary" in description


def test_the_load_summary_is_advertised_as_the_date_source() -> None:
    """The redirect has to land somewhere that actually carries pickup/delivery dates."""

    assert "pickup/delivery dates" in TpGetLoadSummary.description


def test_the_model_documents_the_trap() -> None:
    """A reader of the model has to be able to see why `delivery` is not a date.

    Asserted on the class docstring rather than ``model_fields[...].description``: this
    codebase documents fields with ``#:`` comments, which Sphinx reads and pydantic does not,
    so the per-field text is not runtime-introspectable. The docstring is, and it is where the
    reason lives.
    """

    # Whitespace-normalised: the docstring wraps, so a phrase can straddle a newline.
    doc = " ".join((DispatchRow.__doc__ or "").split())

    assert "no delivery date" in doc
    assert "stack a place above a date" in doc
    assert "tp_get_load_summary" in doc
    # The live case, so a future reader knows this is not hypothetical.
    assert "2478889" in doc
    assert "last_updated" in doc
