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
