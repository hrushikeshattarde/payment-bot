"""A bill of lading is dense with load-id-shaped numbers that are not loads.

Reading PDF attachments made BOLs reachable by the id extractor for the first time. One
realistic BOL page produced FIVE phantom load ids — from ``BOL No``, ``PRO``, ``Trailer``,
``Seal`` and ``Ref`` — against zero real ones. Each phantom costs an authorization lookup
against a scraped back office (2.5 to 4.1 s apiece on live trails), usually ending in "sender not
authorized for any load". The expensive case is a phantom colliding with a load the sender IS
authorised for, which is the RAD Logistics incident recorded in ``_check_carrier_consistency``:
``1669695`` was an account reference that matched a real load and disclosed another carrier's
payment.

TWO OF THE FIVE SURVIVE ON PURPOSE. ``_LOAD_LABEL_RE`` lists ``pro`` and ``ref`` as words
senders use for OUR load — factoring templates write the load itself as "Reference#: 2520504" —
so suppressing them would discard real ids to catch phantom ones. Tests below pin that, because
"finish the job by adding pro and ref" is the obvious next edit and it is wrong.
"""

from __future__ import annotations

import pytest

from payment_bot.tools.shared import _load_ids_in

pytestmark = pytest.mark.unit

#: A realistic BOL page. Only numbers, no load id anywhere on it.
BOL_TEXT = (
    "BILL OF LADING   BOL No 884213\n"
    "PRO 512884   Trailer 284119   Seal 337201\n"
    "VIN 1FDFE4FN0TDD259447\n"
    "Shipper: Midwest Auto Group, 4471 Industrial Pkwy, Rockdale IL 60436\n"
    "Consignee: Pacific Dealer Network, 90210\n"
    "Declared value 18500.00   Weight 42150 lbs\n"
    "Pieces 3   Ref 719044\n"
)


def test_the_bol_no_longer_yields_five_phantom_loads() -> None:
    found = _load_ids_in(BOL_TEXT)

    assert "884213" not in found, "BOL No"
    assert "284119" not in found, "Trailer"
    assert "337201" not in found, "Seal"


def test_the_two_survivors_are_the_ones_that_must_survive() -> None:
    """`pro` and `ref` mean the load elsewhere, so they cannot be suppressed here."""

    found = _load_ids_in(BOL_TEXT)

    assert set(found) == {"512884", "719044"}, (
        "expected only the PRO and Ref numbers to survive; anything else means a label was "
        "added or lost"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Trailer 284119",
        "Trailer No 284119",
        "Tractor 284119",
        "Seal 337201",
        "Seal # 337201",
        "BOL No 884213",
        "BOL Number 884213",
        "BOL #884213",
        "Dispatch 2707288",
    ],
)
def test_equipment_and_document_numbers_are_suppressed(text: str) -> None:
    assert _load_ids_in(text) == [], text


@pytest.mark.parametrize(
    "text",
    [
        # A sender naming the DOCUMENT by its load — the id is the thing being asked about.
        "Please find attached BOL 2462934",
        "BOL 2462934 is signed",
        # Label not adjacent to the number: the words in between mean it is not a label.
        "Please send the BOL for load 2462934",
        "Trailer was late, load 2462934 delivered anyway",
        # The words _LOAD_LABEL_RE owns.
        "PRO 2462934",
        "Reference#: 2520504",
        "Ref 2462934",
        # Ordinary load vocabulary must be untouched.
        "load 2462934",
        "Order No 2462934",
        "INV 2462934",
    ],
)
def test_real_load_ids_are_still_found(text: str) -> None:
    assert _load_ids_in(text), text


def test_bol_without_a_number_word_is_not_suppressed() -> None:
    """The precise boundary: "BOL No 884213" goes, bare "BOL 2462934" stays.

    Requiring No/# is what separates the document's own number from the load it belongs to.
    Without that distinction this label would drop the id in "attached BOL 2462934", the same
    trap that keeps `inv` out of the suppression list.
    """

    assert _load_ids_in("BOL No 884213") == []
    assert _load_ids_in("BOL 884213") == ["884213"]


@pytest.mark.parametrize(
    "text",
    [
        "Account No. 2657147",
        "PLACE STOP PAYMENT ON CHK 787147",
        "P.O. Box 2657147",
        "MC 2657147",
        "suite 2657147",
    ],
)
def test_the_labels_that_were_already_there_still_work(text: str) -> None:
    """The additions are inserted into a shared alternation, so a typo could break these."""

    assert _load_ids_in(text) == [], text
