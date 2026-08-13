"""The escalation packet for an unknown factoring sender.

What is being automated here is the *preparation*, never the decision. The roster answers
"may this sender see this carrier's payment data?", and a rule that resolved "absent from the
roster" to "add it and proceed" would leave the question unanswered while appearing to answer
it. So every test below asserts the same underlying property from a different angle: the
packet informs and authorises nothing.

The case it was built from is real. On 2026-08-13 a Partners Funding enquiry was authorised
from ``getpartnersfunding.com`` while our own factor record carried ``partnersfundinginc.com``
— two weak sources disagreeing, which is precisely the question to put to the company. Nobody
saw it, because nothing put the two facts beside each other. The conflict block exists so the
next one cannot be missed the same way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from payment_bot.config import Settings
from payment_bot.roster_candidate import build_candidate

SENDER = "leslie.fulmer@getpartnersfunding.com"
FACTOR = "PARTNERS FUNDING"


@pytest.fixture
def hints_file(tmp_path: Path) -> str:
    """A cut-down review file in the shape the real one has."""

    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "factorName": "Partners Funding, a division of Scale Bank",
                        "carrierCount": 374,
                        "tproDomains": ["partnersfundinginc.com"],
                    }
                ],
                "gaps": [{"factorName": "Outgo, Inc", "carrierCount": 523}],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]


# --- the conflict this module exists for ------------------------------------
def test_a_domain_we_already_hold_for_the_factor_is_surfaced(hints_file: str) -> None:
    """The line that would have caught the live case before the entry was added."""

    candidate = build_candidate(
        sender_email=SENDER,
        factor_on_file=FACTOR,
        load_ids=("298891",),
        settings=_settings(factoring_domains={}, factor_domain_hints_file=hints_file),
    )

    assert candidate is not None
    assert candidate.conflicting_domains == ("partnersfundinginc.com",)
    rendered = candidate.render()
    assert "WE ALREADY HOLD A DIFFERENT DOMAIN" in rendered
    assert "partnersfundinginc.com" in rendered
    # And it must not resolve the question it raises.
    assert "Ask the company which domains are theirs" in rendered
    assert "never one from the mail in question" in rendered


def test_the_grant_breadth_is_stated_not_just_the_load(hints_file: str) -> None:
    """An entry authorises every load factored to that company, which is the real decision.

    The escalation names one load. Reviewing it as a one-load question is the mistake this
    line exists to prevent.
    """

    candidate = build_candidate(
        sender_email=SENDER,
        factor_on_file=FACTOR,
        load_ids=("298891",),
        settings=_settings(factor_domain_hints_file=hints_file),
    )

    assert candidate is not None and candidate.carrier_count == 374
    assert "374 carriers" in candidate.render()
    assert "EVERY one of their loads" in candidate.render()


def test_a_domain_already_configured_for_the_factor_also_conflicts() -> None:
    """The roster itself is the other source of "we know a different domain"."""

    candidate = build_candidate(
        sender_email=SENDER,
        factor_on_file=FACTOR,
        load_ids=("298891",),
        settings=_settings(factoring_domains={"partners funding": ("realpartners.com",)}),
    )

    assert candidate is not None
    assert candidate.conflicting_domains == ("realpartners.com",)


def test_the_senders_own_domain_is_not_reported_as_a_conflict() -> None:
    """Re-escalation on an already-configured domain must not read as a discrepancy."""

    candidate = build_candidate(
        sender_email=SENDER,
        factor_on_file=FACTOR,
        load_ids=("298891",),
        settings=_settings(
            factoring_domains={"partners funding": ("getpartnersfunding.com",)}
        ),
    )

    assert candidate is not None
    assert candidate.conflicting_domains == ()
    assert "WE ALREADY HOLD A DIFFERENT DOMAIN" not in candidate.render()


def test_no_corroboration_is_said_out_loud() -> None:
    """Silence about provenance would read as approval. It has to be stated."""

    candidate = build_candidate(
        sender_email=SENDER, factor_on_file=FACTOR, load_ids=("298891",), settings=_settings()
    )

    assert candidate is not None
    assert "nothing corroborates this one" in candidate.render()
    assert "Verify out of band" in candidate.render()


# --- what it must refuse to propose -----------------------------------------
def test_a_load_with_no_factor_produces_no_candidate() -> None:
    """A sender with no factor on the load is not a party to it.

    Offering a roster entry here would invite authorising a stranger to make an escalation
    go away — the escalation being, in that case, entirely correct.
    """

    assert (
        build_candidate(
            sender_email=SENDER, factor_on_file="", load_ids=("298891",), settings=_settings()
        )
        is None
    )
    assert (
        build_candidate(
            sender_email=SENDER, factor_on_file="   ", load_ids=("298891",), settings=_settings()
        )
        is None
    )


def test_an_unusable_sender_address_produces_no_candidate() -> None:
    assert (
        build_candidate(
            sender_email="not-an-address",
            factor_on_file=FACTOR,
            load_ids=("298891",),
            settings=_settings(),
        )
        is None
    )


def test_resemblance_is_described_never_scored() -> None:
    """A number invites treating resemblance as evidence. It is what an attacker controls.

    A lookalike registered this morning scores exactly as well as the real company, which is
    why this is a reviewer's hint and is worded as one.
    """

    candidate = build_candidate(
        sender_email="billing@totally-unrelated.com",
        factor_on_file=FACTOR,
        load_ids=("298891",),
        settings=_settings(),
    )
    assert candidate is not None
    assert candidate.resemblance == ""
    assert "none — the domain does not echo the name" in candidate.render()

    lookalike = build_candidate(
        sender_email="ap@partners-funding-payments.com",
        factor_on_file=FACTOR,
        load_ids=("298891",),
        settings=_settings(),
    )
    # It resembles the name as well as the genuine domain does — stated, not scored.
    assert lookalike is not None
    assert "partners" in lookalike.resemblance
    assert "a human decides this; nothing has been added" in lookalike.render()


# --- degradation ------------------------------------------------------------
@pytest.mark.parametrize("contents", ["", "{not json", '["wrong", "shape"]', "null"])
def test_an_unreadable_hints_file_costs_one_line_and_nothing_else(
    tmp_path: Path, contents: str
) -> None:
    """It is a review artifact, not configuration. The bot works without it.

    Failing the escalation because its annotation could not be built would be a far worse
    bug than the manual lookup this saves.
    """

    path = tmp_path / "broken.json"
    path.write_text(contents, encoding="utf-8")

    candidate = build_candidate(
        sender_email=SENDER,
        factor_on_file=FACTOR,
        load_ids=("298891",),
        settings=_settings(factor_domain_hints_file=str(path)),
    )

    assert candidate is not None
    assert candidate.recorded_domains == ()
    assert candidate.carrier_count is None
    assert "ROSTER CANDIDATE" in candidate.render()


def test_a_missing_hints_file_is_not_an_error() -> None:
    candidate = build_candidate(
        sender_email=SENDER,
        factor_on_file=FACTOR,
        load_ids=("298891",),
        settings=_settings(factor_domain_hints_file="/nowhere/at/all.json"),
    )
    assert candidate is not None and candidate.carrier_count is None


# --- the entry it proposes --------------------------------------------------
def test_the_proposed_key_links_to_the_factor_on_the_load() -> None:
    """A key that does not match the load's factor is a paste that silently does nothing."""

    from payment_bot.tools.shared import _factor_names_match

    candidate = build_candidate(
        sender_email=SENDER,
        factor_on_file="PARTNERS FUNDING C/O VIO LINE INC",
        load_ids=("298891",),
        settings=_settings(),
    )
    assert candidate is not None
    assert _factor_names_match(candidate.roster_key, "PARTNERS FUNDING")
    assert json.loads("{" + candidate.entry_json().rstrip(",") + "}") == {
        candidate.roster_key: ["getpartnersfunding.com"]
    }
