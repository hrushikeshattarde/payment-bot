"""Unit tests for Settings — the roster and carrier-contact file loaders."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from payment_bot.config import Settings


@pytest.mark.unit
def test_factoring_domains_file_is_merged(tmp_path: Path) -> None:
    roster = tmp_path / "factoring_domains.json"
    roster.write_text(
        json.dumps(
            {
                "rts financial service, inc": ["rtsfinancial.com"],
                "tru funding llc": ["trufunding.net"],
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(factoring_domains_file=str(roster))

    assert settings.factoring_domains["rts financial service, inc"] == ("rtsfinancial.com",)
    assert settings.factoring_domains["tru funding llc"] == ("trufunding.net",)


@pytest.mark.unit
def test_inline_entries_win_over_the_file(tmp_path: Path) -> None:
    """A hand-curated correction must beat the generated roster."""

    roster = tmp_path / "factoring_domains.json"
    roster.write_text(
        json.dumps({"rts financial": ["wrong-domain.example"]}), encoding="utf-8"
    )

    settings = Settings(
        factoring_domains_file=str(roster),
        factoring_domains={"rts financial": ("rtsfinancial.com",)},
    )

    assert settings.factoring_domains["rts financial"] == ("rtsfinancial.com",)


@pytest.mark.unit
def test_missing_file_fails_loudly(tmp_path: Path) -> None:
    """A configured roster that cannot be read must not silently authorise nobody."""

    with pytest.raises(ValueError, match="could not be read"):
        Settings(factoring_domains_file=str(tmp_path / "nope.json"))


@pytest.mark.unit
def test_no_file_configured_changes_nothing() -> None:
    assert Settings().factoring_domains == {}


# --- carrier contacts, same loader, different rules -------------------------
@pytest.mark.unit
def test_carrier_contacts_file_is_merged(tmp_path: Path) -> None:
    contacts = tmp_path / "carrier_contacts.json"
    contacts.write_text(
        json.dumps(
            {
                "ALWAYS THERE LOGISTICS INC": ["alwaystherelogisticsbilling@example.com"],
                "A & J TRANSPORT SERVICE INC": ["ajtrans37848@example.com"],
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(carrier_contacts_file=str(contacts))

    assert settings.carrier_contacts["ALWAYS THERE LOGISTICS INC"] == (
        "alwaystherelogisticsbilling@example.com",
    )
    assert settings.carrier_contacts["A & J TRANSPORT SERVICE INC"] == (
        "ajtrans37848@example.com",
    )


@pytest.mark.unit
def test_documentation_keys_in_a_hand_maintained_file_are_not_carriers(tmp_path: Path) -> None:
    """`_README` and `_evidence` are prose, not a carrier named "_README".

    The roster's file never hit this because its generator strips `_` keys before writing.
    This one is hand-maintained and carries its own README, so the merge has to skip them —
    otherwise a "carrier" appears whose authorised addresses are sentences.
    """

    contacts = tmp_path / "carrier_contacts.json"
    contacts.write_text(
        json.dumps(
            {
                "_README": ["Addresses, never domains.", "The key must match the load."],
                "EXAMPLE TRUCKING LLC": ["billing@example.com"],
                "_evidence": {"EXAMPLE TRUCKING LLC": "confirmed by phone 2026-08-14"},
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(carrier_contacts_file=str(contacts))

    assert set(settings.carrier_contacts) == {"EXAMPLE TRUCKING LLC"}
    assert "_README" not in settings.carrier_contacts
    assert "_evidence" not in settings.carrier_contacts


@pytest.mark.unit
def test_inline_carrier_contacts_win_over_the_file(tmp_path: Path) -> None:
    contacts = tmp_path / "carrier_contacts.json"
    contacts.write_text(
        json.dumps({"EXAMPLE TRUCKING LLC": ["stale@example.com"]}), encoding="utf-8"
    )

    settings = Settings(
        carrier_contacts_file=str(contacts),
        carrier_contacts={"EXAMPLE TRUCKING LLC": ("current@example.com",)},
    )

    assert settings.carrier_contacts["EXAMPLE TRUCKING LLC"] == ("current@example.com",)


@pytest.mark.unit
def test_a_missing_carrier_contacts_file_fails_loudly(tmp_path: Path) -> None:
    """Same contract as the roster: silently authorising nobody is the worst outcome."""

    with pytest.raises(ValueError, match="could not be read"):
        Settings(carrier_contacts_file=str(tmp_path / "nope.json"))


@pytest.mark.unit
def test_no_carrier_contacts_file_configured_changes_nothing() -> None:
    assert Settings().carrier_contacts == {}


@pytest.mark.unit
def test_a_file_contact_authorises_a_cargotel_carrier_on_free_mail(tmp_path: Path) -> None:
    """The whole point: this path is already wired for 6-digit loads.

    ``_decide_cargotel`` consults ``_configured_carrier_contact`` after the CargoTel record's
    own contacts miss, so a file entry reaches a 6-digit load with no code change. Matching is
    on the WHOLE ADDRESS — a carrier on Gmail cannot be authorised any other way, because the
    domain form would be gmail.com.
    """

    import types

    from payment_bot.tools.shared import _configured_carrier_contact

    contacts = tmp_path / "carrier_contacts.json"
    contacts.write_text(
        json.dumps({"STRATAN INC": ["ar.strataninc@example.com"]}), encoding="utf-8"
    )
    ctx = types.SimpleNamespace(settings=Settings(carrier_contacts_file=str(contacts)))

    match = "ar.strataninc@example.com"
    assert _configured_carrier_contact(["STRATAN INC"], match, ctx) == "STRATAN INC"
    # Normalisation only — never token matching, or one carrier answers for another's loads.
    assert _configured_carrier_contact(["Stratan Inc."], match, ctx) == "Stratan Inc."
    assert _configured_carrier_contact(["STRATAN LOGISTICS LLC"], match, ctx) is None
    # The address grants exactly itself, not its domain.
    assert _configured_carrier_contact(["STRATAN INC"], "someone.else@example.com", ctx) is None
    # Which carrier matched is the answer, not just whether one did: on a load with several,
    # it is what scopes the reply to that carrier's own payable.
    assert (
        _configured_carrier_contact(["FOX CARRIERS", "STRATAN INC"], match, ctx)
        == "STRATAN INC"
    )


@pytest.mark.unit
def test_reply_signature_reaches_the_intake_prompt() -> None:
    """The sign-off is config, not model choice — a live draft once signed as the carrier."""

    from payment_bot.agent.skills import build_payment_status_intake
    from payment_bot.sample_data import sample_payment_status_email

    intake = build_payment_status_intake(
        sample_payment_status_email(),
        ["2462934"],
        {"2462934": "transport_pro"},
        signature="Hrushikesh Attarde, Circle Delivers Payments",
    )
    assert "Sign the reply exactly as: Hrushikesh Attarde, Circle Delivers Payments" in intake


@pytest.mark.unit
def test_documents_email_reaches_the_intake_prompt() -> None:
    """Missing-paperwork replies must name where to send documents — from config."""

    from payment_bot.agent.skills import build_payment_status_intake
    from payment_bot.sample_data import sample_payment_status_email

    intake = build_payment_status_intake(
        sample_payment_status_email(),
        ["2462934"],
        {"2462934": "transport_pro"},
        documents_email="freightpay@circledelivers.com",
    )
    assert "Missing paperwork should be emailed to: freightpay@circledelivers.com" in intake
