"""A cancellation belongs to a dispatch leg, not to a load.

Live regression, load 2534597 (2026-08-14). OTR Solutions asked to verify the rate for
N S EXPRESS LLC (MC-1415627). Transport Pro held two dispatch rows:

    Nesh Trans Llc   (MC 1200865)  Canceled
    N S Express Llc  (MC 1415627)  Delivered

and four Carrier Rate Agreements, one of which carried ``CANCEL LOAD Confirmation for load -
2534597`` in its COMMENT. The draft replied "load 2534597 is under review due to a cancel
confirmation on file" — true of the load, false of the carrier being asked about, whose leg
ran and delivered.

Two separate defects behind one sentence:

* ``has_cancel_confirmation`` is load-level, and the document names only the load, so nothing
  could say whose cancellation it was. ``tp_get_file_history`` now joins the dispatch rows
  itself, because the model would otherwise have to call a second tool it has no reason to
  call and then reason about supersession.
* the phrase lives in a comment on an ordinary document type, so the back office could not
  find the document either. The tool now returns the source line.

The regex also used to be searched against ``f"{file_type} {comments}"``, which let a match
straddle the join — see ``test_a_match_never_straddles_the_type_comment_join``.
"""

from __future__ import annotations

import pytest

from payment_bot.domain.documents import DocCategory, assess_documents
from payment_bot.errors import ClientError
from payment_bot.models import DispatchRow
from payment_bot.tools.base import ToolContext
from payment_bot.tools.transport_pro import LoadIdInput, TpGetFileHistory

pytestmark = pytest.mark.unit

LOAD = "2534597"
REQUIRED = (DocCategory.CARRIER_INVOICE, DocCategory.PROOF_OF_DELIVERY)

#: The four rate agreements as Transport Pro actually returned them, cancel comment included.
REAL_DOCS = (
    ("Rate Confirmation", 143, None, "30631066_143.pdf"),
    ("Carrier Rate Agreement", 23, None, f"Rate Confirmation for load - {LOAD}"),
    ("Carrier Rate Agreement", 23, None, f"CANCEL LOAD Confirmation for load - {LOAD}"),
    ("Carrier Rate Agreement", 23, None, f"Rate and Dispatch Confirmation for load - {LOAD}"),
)

CANCELED_ROW = DispatchRow(
    carrier_name="Nesh Trans Llc", mc_number="1200865", dispatch_status="Canceled"
)
DELIVERED_ROW = DispatchRow(
    carrier_name="N S Express Llc", mc_number="1415627", dispatch_status="Delivered"
)


class _Tp:
    """Minimal Transport Pro stand-in: file history plus dispatch rows."""

    def __init__(self, docs=REAL_DOCS, rows=(CANCELED_ROW, DELIVERED_ROW), raises=False):
        self._docs = docs
        self._rows = rows
        self._raises = raises
        self.dispatch_calls = 0

    def get_file_history(self, load_id):
        class _Doc:
            def __init__(self, file_type, type_id, uploaded, comments):
                self.file_type = file_type
                self.file_type_id = type_id
                self.upload_date = uploaded
                self.index_date = None
                self.comments = comments

        return [_Doc(*d) for d in self._docs]

    def get_dispatch_history(self, load_id):
        self.dispatch_calls += 1
        if self._raises:
            raise ClientError("dispatch history unavailable")
        return list(self._rows)


def _run(tp: _Tp):
    from payment_bot.grounding import GroundingLedger

    ctx = ToolContext(tp=tp, ledger=GroundingLedger(), correlation_id="cancel-attribution")
    return TpGetFileHistory().run(LoadIdInput(load_id=LOAD), ctx)


# --- the live load ----------------------------------------------------------
def test_the_cancellation_is_attributed_to_the_leg_that_was_cancelled() -> None:
    out = _run(_Tp())

    assert out.has_cancel_confirmation is True  # true of the load, and still reported
    assert out.cancel_confirmation_superseded is True  # but not of the delivering carrier
    assert out.canceled_carriers == ["Nesh Trans Llc"]
    assert out.delivered_carrier == "N S Express Llc"


def test_the_source_document_is_named_so_a_human_can_find_it() -> None:
    """The back office could not find it: the file list shows 3x "Carrier Rate Agreement"."""

    out = _run(_Tp())

    assert len(out.cancel_confirmation_sources) == 1
    source = out.cancel_confirmation_sources[0]
    assert source.startswith("Carrier Rate Agreement: ")
    assert "CANCEL LOAD Confirmation" in source


def test_a_load_cancelled_outright_is_not_reported_as_superseded() -> None:
    """No delivered leg means the cancellation is the load's actual outcome — hold stands."""

    out = _run(_Tp(rows=(CANCELED_ROW,)))

    assert out.has_cancel_confirmation is True
    assert out.cancel_confirmation_superseded is False
    assert out.delivered_carrier is None


def test_a_delivered_load_with_no_cancelled_leg_is_not_superseded() -> None:
    """A cancel document with no cancelled row is unexplained, so it must still hold.

    Requiring a cancelled row is what keeps supersession from firing on a load whose only
    trace of a cancellation is the document itself.
    """

    out = _run(_Tp(rows=(DELIVERED_ROW,)))

    assert out.has_cancel_confirmation is True
    assert out.cancel_confirmation_superseded is False


def test_it_does_not_claim_supersession_when_dispatch_history_is_unreadable() -> None:
    """Fails toward the hold: an unreadable second call must not clear the flag."""

    out = _run(_Tp(raises=True))

    assert out.has_cancel_confirmation is True
    assert out.cancel_confirmation_superseded is False
    assert out.canceled_carriers == []


def test_dispatch_history_is_not_fetched_when_there_is_no_cancellation() -> None:
    """The extra call is paid for only by loads that need it."""

    tp = _Tp(docs=(("Carrier Rate Agreement", 23, None, f"Rate Confirmation for load - {LOAD}"),))
    out = _run(tp)

    assert out.has_cancel_confirmation is False
    assert tp.dispatch_calls == 0


# --- the matcher ------------------------------------------------------------
def test_a_match_never_straddles_the_type_comment_join() -> None:
    """The phrase must be inside ONE field. Joining them invented cancellations.

    "Request to Cancel" beside "Load reinstated 8/1" reads as "...Cancel Load reinstated..."
    once joined by a space, so a load that ran normally went on hold with neither field
    containing the phrase.
    """

    status, _ = assess_documents(
        [("Request to Cancel", None, None, "Load reinstated 8/1, ran normally")],
        load_id=LOAD,
        required=REQUIRED,
    )

    assert status.has_cancel_confirmation is False
    assert status.cancel_confirmation_sources == ()


@pytest.mark.parametrize(
    ("file_type", "comments"),
    [
        ("Cancel Load Confirmation", None),  # in the type
        ("Carrier Rate Agreement", "CANCEL LOAD Confirmation for load - 2534597"),  # in a comment
        ("Proof of Delivery", "cancel load per broker 7/30"),
    ],
)
def test_a_real_cancellation_is_still_caught_in_either_field(file_type, comments) -> None:
    status, _ = assess_documents(
        [(file_type, None, None, comments)], load_id=LOAD, required=REQUIRED
    )
    assert status.has_cancel_confirmation is True


@pytest.mark.parametrize(
    ("file_type", "comments"),
    [
        ("Cancelled Check", None),  # a remittance document, not a cancellation
        ("Proof of Delivery", "load cancelled by shipper"),  # wrong word order
        ("Proof of Delivery", "cancellation of load discussed"),
        ("Proof of Delivery", None),
    ],
)
def test_ordinary_documents_do_not_set_the_flag(file_type, comments) -> None:
    status, _ = assess_documents(
        [(file_type, None, None, comments)], load_id=LOAD, required=REQUIRED
    )
    assert status.has_cancel_confirmation is False


# --- the prompt -------------------------------------------------------------
def test_the_prompt_tells_the_model_a_superseded_cancellation_is_not_a_hold() -> None:
    """The HOLD rule lives in the RATE VERIFICATION prompt — that is the skill OTR's mail hit.

    Worth pinning explicitly: the two prompts carry near-identical wording in places, and the
    rule this fixes is only in one of them.
    """

    from payment_bot.agent.skills import RATE_VERIFICATION_SKILL

    prompt = RATE_VERIFICATION_SKILL.system_prompt

    assert "NOT a hold reason when `cancel_confirmation_superseded` is true" in prompt
    assert "`has_cancel_confirmation` is load-level" in prompt
    assert "their leg ran" in prompt
    # 1.12.0 added the multi-carrier rules and 1.13.0 the past-tense pay-to rule; the
    # superseded-cancellation rule above is unchanged and still has to be in the prompt.
    assert RATE_VERIFICATION_SKILL.version == "1.13.0"
