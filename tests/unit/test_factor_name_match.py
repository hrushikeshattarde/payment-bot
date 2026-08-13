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


# ---------------------------------------------------------------------------
# The escalation WORDING when a known factor's domain is not configured.
#
# Diagnostic only: the branch this feeds returns DENY either way, so a hint that
# fires too eagerly costs a reviewer a wasted glance and can never disclose a
# load. That is what makes searching an acronym safe here and nowhere else.
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("AMERICAN FACTORING GROUP, LLC", "afg"),  # the live miss
        ("Triumph Business Capital", "tbc"),
        ("Nu-Ko Capital", "nkc"),
        ("RTS Financial Service, Inc", "rfs"),
        # Two letters are meaningless — "of" appears in a large share of all domains.
        ("Operation Finance, Inc", ""),
        ("Aladdin Financial, Inc.", ""),
        ("Tru Funding LLC", ""),
        ("OTR Capital, LLC", ""),
        ("Apex", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_the_acronym_is_built_from_significant_words_only(name: str | None, expected: str) -> None:
    from payment_bot.tools.shared import company_acronym

    assert company_acronym(name) == expected


@pytest.mark.unit
def test_an_industry_word_stays_in_the_acronym() -> None:
    """"Group" is dropped by _STOPWORDS but belongs in AFG — hence a separate suffix list.

    Reusing _STOPWORDS would yield "af", below the three-character floor, and the live case
    would still be missed.
    """

    from payment_bot.tools.shared import company_acronym, company_tokens

    assert "group" not in company_tokens("AMERICAN FACTORING GROUP, LLC")
    assert company_acronym("AMERICAN FACTORING GROUP, LLC") == "afg"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        ("billing@afgfactor.com", "afgfactor"),
        ("a@bwc.fleetsmarts.net", "bwcfleetsmarts"),  # per-tenant subdomain kept
        ("a@localhost", "localhost"),                 # no TLD to strip
        ("not-an-address", ""),
    ],
)
def test_the_tld_is_excluded_from_the_acronym_search(sender: str, expected: str) -> None:
    """A factor abbreviating to "net" must not resemble every .net address."""

    from payment_bot.tools.shared import _domain_without_tld, company_acronym

    assert _domain_without_tld(sender) == expected
    # "National Equipment Transport" really does abbreviate to a TLD, which is the point.
    assert company_acronym("National Equipment Transport") == "net"
    assert "net" not in _domain_without_tld("a@bwc.fleetsmarts.net")


# ---------------------------------------------------------------------------
# `exact:` keys — for a factor whose one distinctive word belongs to somebody else.
#
# "G Squared Funding, LLC" reduces to the single token "squared" once the generic
# ones are dropped, so an ordinary roster key for it also matches "DB Squared,
# Inc." — 11 carriers under an unrelated factor. No choice of key avoids that:
# the collision is in the matching, not in the keys.
# ---------------------------------------------------------------------------
EXACT_KEY = "exact:g squared funding, llc"


@pytest.mark.unit
def test_an_exact_key_matches_only_that_factor() -> None:
    assert _factor_names_match(EXACT_KEY, "G Squared Funding, LLC") is True
    # The same company's second record in Transport Pro, spelled without the comma.
    assert _factor_names_match(EXACT_KEY, "G Squared Funding LLC") is True


@pytest.mark.unit
def test_an_exact_key_does_not_link_on_a_shared_token() -> None:
    """The whole point. A plain key here would authorise a different company's loads."""

    assert _factor_names_match(EXACT_KEY, "DB Squared, Inc.") is False
    assert _factor_names_match(EXACT_KEY, "Squared Away Logistics") is False
    # And for contrast, the plain key really does collide — this is what is being avoided.
    assert _factor_names_match("g squared funding, llc", "DB Squared, Inc.") is True


@pytest.mark.unit
def test_the_exactness_is_after_normalisation_not_byte_for_byte() -> None:
    """Punctuation and case still vary between the export and the load."""

    assert _factor_names_match("exact:G SQUARED FUNDING LLC", "g squared funding, llc") is True


@pytest.mark.unit
def test_an_exact_key_is_narrower_than_a_plain_one_and_that_costs_something() -> None:
    """Recorded so the trade-off is visible: a spelling it does not cover simply misses.

    The fuzzy match exists because the export and the load spell factors differently, and an
    exact key gives that up. If a third G Squared record appears without "LLC", it will not
    match and the load will escalate — which is the failure to look for first.
    """

    assert _factor_names_match(EXACT_KEY, "G Squared Funding") is False
    assert _factor_names_match("g squared funding, llc", "G Squared Funding") is True


@pytest.mark.unit
def test_plain_keys_are_completely_unaffected() -> None:
    """The prefix is opt-in; nothing else in the roster changes behaviour."""

    assert _factor_names_match("rts financial", "RTS Financial Service, Inc") is True
    assert _factor_names_match("far west capital", "Far West Capital") is True
    assert _factor_names_match("cashway funding", "CT Cash LLC") is False
