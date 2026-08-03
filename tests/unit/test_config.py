"""Unit tests for Settings — the factoring-domains file loader."""

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
