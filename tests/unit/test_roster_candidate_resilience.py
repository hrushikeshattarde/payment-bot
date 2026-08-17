"""One unreadable load must not cost the roster packet for the others.

Live regression, 2026-08-17. An Aladdin Factoring verification named ONE load (2534786) in its
body; the attached packet contributed three more 7-digit numbers, two of which Transport Pro
400s on because no such load exists. The first 400 propagated out of the per-load loop to
``_roster_candidate``'s outer handler, which returns None — so the escalation carried its reason
and **no roster-candidate block at all**:

    roster_candidate_failed  ...load/4303006/payment_information failed (HTTP 400)

That block was the entire decision the escalation existed to put in front of a reviewer: we
hold ``aladdincap.com`` for Aladdin, ``aladdinfactoringapp.com`` is rostered nowhere, and those
two facts belong side by side. A load that is not even ours discarded it.

"We could not annotate load X" and "we could not annotate this escalation" are different
outcomes, and only the first is acceptable when the unresolvable id is one the sender never
asked about.
"""

from __future__ import annotations

import pytest

from payment_bot.errors import ClientError
from payment_bot.grounding import GroundingLedger
from payment_bot.models import AuthorizationContext, InboundEmail
from payment_bot.pipeline import PaymentBotPipeline
from payment_bot.tools.base import ToolContext

pytestmark = pytest.mark.unit

#: The load the sender actually named, and the phantom the attachment contributed.
REAL = "2534786"
PHANTOM = "4303006"


class _Tp:
    """Resolves the real load; 400s on the phantom, as Transport Pro does."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def get_authorization_context(self, load_id: str) -> AuthorizationContext:
        self.asked.append(load_id)
        if load_id == PHANTOM:
            raise ClientError(
                f"Transport Pro GET /voiceai/load/{load_id}/payment_information failed (HTTP 400)"
            )
        return AuthorizationContext(
            carrier_company="Blue Hawk Trucking Inc",
            authorized_emails=("dispatch@bluehawk.example",),
            factoring_company="Aladdin Financial, Inc.",
        )


def _packet(load_ids: tuple[str, ...], tp: _Tp) -> str | None:
    from payment_bot.config import Settings

    settings = Settings(
        _env_file=None,
        allow_factoring=True,
        factoring_domains={"aladdin financial, inc.": ("aladdincap.com",)},  # type: ignore[arg-type]
    )
    pipeline = PaymentBotPipeline.__new__(PaymentBotPipeline)
    pipeline._settings = settings  # type: ignore[attr-defined]
    email = InboundEmail(
        message_id="<m>",
        thread_id="t",
        from_email="portal@aladdinfactoringapp.com",
        from_name="Aladdin Factoring",
        subject=f"Verification - {REAL}",
        body=f"Can you please verify the rate on load {REAL}?",
    )
    ctx = ToolContext(tp=tp, ledger=GroundingLedger(), correlation_id="resilience-test")
    return pipeline._roster_candidate(email, load_ids, ctx, "resilience-test")


def test_a_phantom_load_no_longer_discards_the_whole_packet() -> None:
    """The regression: the phantom is listed FIRST, so it used to abort before the real one."""

    tp = _Tp()

    packet = _packet((PHANTOM, REAL), tp)

    assert packet is not None, "one unreadable load must not lose the packet"
    assert "Aladdin Financial, Inc." in packet
    assert "aladdinfactoringapp.com" in packet
    # The decision the reviewer needed: a different domain already on file for this factor.
    assert "aladdincap.com" in packet
    assert "WE ALREADY HOLD A DIFFERENT DOMAIN FOR THIS FACTOR" in packet


def test_every_load_is_still_attempted() -> None:
    """Skipping means skipping that load, not stopping the loop."""

    tp = _Tp()

    _packet((PHANTOM, REAL), tp)

    assert tp.asked == [PHANTOM, REAL]


def test_the_packet_is_unchanged_when_nothing_fails() -> None:
    tp = _Tp()

    packet = _packet((REAL,), tp)

    assert packet is not None
    assert "aladdincap.com" in packet


def test_all_loads_unreadable_still_returns_none_rather_than_raising() -> None:
    """The outer contract survives: a failed annotation never breaks the escalation."""

    tp = _Tp()

    assert _packet((PHANTOM, PHANTOM), tp) is None
