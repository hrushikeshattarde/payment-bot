"""Runtime configuration.

Settings are read from environment variables (prefixed ``PAYBOT_``) or a local
``.env`` file. In production these values originate from SSM Parameter Store /
Secrets Manager (PRD §8.1.1) — the loader here does not care about the source, only
the resulting environment.

Keeping configuration in one typed, validated object (rather than scattered
``os.getenv`` calls) is what lets the rest of the code depend on plain attributes
and stay unit-testable.
"""

from __future__ import annotations

from enum import IntEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RolloutPhase(IntEnum):
    """Phased rollout from the PRD §8.5.

    The pre-send gate runs in *every* phase; the phase only controls whether a human
    Slack approval click is required before a send.
    """

    APPROVE = 1  # Every draft requires human Approve/Edit/Reject in Slack.
    SELECTIVE_AUTOSEND = 2  # Low-risk, gate-passing intents may send without a click.


class Settings(BaseSettings):
    """Typed application settings. Instantiate via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="PAYBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Environment ---------------------------------------------------------
    env: str = "local"
    rollout_phase: RolloutPhase = RolloutPhase.APPROVE
    log_level: str = "INFO"

    # --- Mailbox -------------------------------------------------------------
    mailbox: str = "paystatus@circledelivers.com"

    # --- Bulk fallback (§3.3 / §5) ------------------------------------------
    bulk_threshold: int = Field(default=5, ge=1)
    portal_url: str = "https://circledelivers.com/payment-status-lookup/"

    # --- Business logic (§4.1.1) --------------------------------------------
    # The API states these dates are in EDT; all pay-date math is done in this zone.
    pay_date_tz: str = "EDT"

    # --- Amazon Bedrock (§8.1) ----------------------------------------------
    aws_region: str = "us-east-1"
    model_fast: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    model_draft: str = "us.anthropic.claude-sonnet-5-v1:0"
    agent_max_iterations: int = Field(default=12, ge=1, le=50)

    # --- Slack (§4.6) --------------------------------------------------------
    slack_approval_channel: str = "#payments-approvals"
    slack_security_channel: str = "#payments-security"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached)."""

    return Settings()
