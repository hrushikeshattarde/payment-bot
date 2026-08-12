"""Which configuration problems stop ``payment-bot-local`` before it processes anything.

``_missing_configuration`` withholds *every* reply — nothing in the inbox gets answered — so
only a credential the whole run genuinely depends on belongs in it. Anything that degrades
one path while leaving the rest working does not, and should be reported some other way.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from payment_bot.config import Settings
from payment_bot.local_runner import _missing_configuration

pytestmark = pytest.mark.unit


def _settings(**overrides: object) -> Settings:
    """A fully-working local configuration, minus whatever the caller overrides."""

    base: dict[str, object] = {
        "_env_file": None,
        "tp_base_url": "https://tp.example.test/api/v1",
        "tp_username": "apiuser",
        "tp_password": SecretStr("secret"),
        "groq_api_key": SecretStr("key"),
        "google_sa_file": "sa.json",
        "gmail_user": "me@example.test",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"tp_base_url": ""}, "Transport Pro"),
        ({"tp_username": ""}, "Transport Pro"),
        ({"tp_password": SecretStr("")}, "Transport Pro"),
        ({"groq_api_key": SecretStr("")}, "Groq"),
        ({"google_sa_file": ""}, "Gmail"),
    ],
)
def test_a_missing_core_credential_blocks_the_run(
    override: dict[str, object], expected: str
) -> None:
    problems = _missing_configuration(_settings(**override))

    assert any(expected in p for p in problems), problems


def test_a_complete_configuration_blocks_nothing() -> None:
    assert _missing_configuration(_settings()) == []


def test_slack_is_not_required() -> None:
    """Local runs post nothing to Slack, so its absence must not stop a run."""

    assert _missing_configuration(_settings(slack_approval_channel="")) == []
