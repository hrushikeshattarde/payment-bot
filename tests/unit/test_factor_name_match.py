"""Linking a roster entry to the factor recorded on a load.

Live regression. Load 2444099 records its factor as "G.H. Factor LLC"; the settlement
export spells the same company "GH Factor LLC", so the roster key was `gh factor llc`. The
entry and the sender's `ghfactor.net` domain were both exactly right, but the names could not
be linked: containment failed on `gh` vs `g.h.`, the only shared word is "factor" which is
industry-generic and cannot link alone, and "gh" is below the name-token length. The domain
was therefore never compared and the sender was denied.

The widening must stay confined to spelling. Two different factors that merely share an
industry-generic word must still not link — that is the property protecting one factor from
being answered about another's load.
"""

from __future__ import annotations

import pytest

from payment_bot.tools.shared import _factor_names_match, _normalize_company_name


@pytest.mark.unit
@pytest.mark.parametrize(
    ("configured", "on_file"),
    [
        ("gh factor llc", "G.H. Factor LLC"),          # the live case
        ("g.h. factor llc", "GH Factor LLC"),          # and the reverse
        ("j.d. factors", "JD Factors"),
        ("t.b.s. factoring service", "TBS Factoring Service"),
        ("love's solutions, llc", "Loves Solutions LLC"),
        ("xfactors financial, inc.", "XFactors Financial Inc"),
        ("rts financial service", "RTS Financial Service, Inc"),
    ],
)
def test_one_company_spelled_two_ways_links(configured: str, on_file: str) -> None:
    assert _factor_names_match(configured, on_file) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("configured", "on_file"),
    [
        # Share only "factor" / "factoring" — industry-generic, must not link.
        ("gh factor llc", "Apex Factor LLC"),
        ("bluff city factoring llc", "Sunbelt Factoring LLC"),
        # Share only "capital", "financial", "funding".
        ("apex capital", "Alta Capital"),
        ("triumph business capital", "Blue Water Capital"),
        ("18 wheel funding llc", "Freedom Funding LLC"),
        ("assist financial services, inc.", "Concept Financial Group, Inc"),
    ],
)
def test_different_factors_sharing_a_generic_word_do_not_link(
    configured: str, on_file: str
) -> None:
    """The disclosure property: one factor must never vouch for another's load."""

    assert _factor_names_match(configured, on_file) is False


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["", "   ", ".", "-", "&"])
def test_blank_or_punctuation_only_names_never_link(blank: str) -> None:
    """Normalisation must not turn a punctuation-only name into a match-anything empty key."""

    assert _factor_names_match(blank, "GH Factor LLC") is False
    assert _factor_names_match("GH Factor LLC", blank) is False


@pytest.mark.unit
def test_normalization_is_punctuation_and_case_only() -> None:
    assert _normalize_company_name("  G.H. Factor,  LLC ") == "gh factor llc"
    assert _normalize_company_name("Love's Solutions, LLC") == "loves solutions llc"
    assert _normalize_company_name("RTS - Financial") == "rts financial"
    # Words themselves are untouched — no stemming, no token dropping.
    assert _normalize_company_name("Factoring Solutions") == "factoring solutions"
