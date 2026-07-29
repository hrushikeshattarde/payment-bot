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

from pydantic import Field, SecretStr
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

    # --- Transport Pro API (§4.3 / §9) --------------------------------------
    # Root of the Transport Pro Public API — the Postman collection's `{{URL}}`.
    # Empty means "use the mock client"; the real client refuses to start without it.
    tp_base_url: str = ""
    tp_username: str = ""
    #: Wrapped in SecretStr so it cannot leak into a repr or a JSON log line.
    tp_password: SecretStr = SecretStr("")
    tp_timeout_seconds: float = Field(default=30.0, gt=0)

    @property
    def transport_pro_configured(self) -> bool:
        """True when enough is set to build a live :class:`TransportProHttpClient`."""

        return bool(self.tp_base_url and self.tp_username and self.tp_password.get_secret_value())

    # --- Agent loop ----------------------------------------------------------
    agent_max_iterations: int = Field(default=12, ge=1, le=50)
    #: Cap on each model turn. Must comfortably fit a full reply — too low truncates a
    #: draft mid-sentence. Open-weight models are chattier than Claude, hence the headroom.
    agent_max_tokens: int = Field(default=4096, ge=256, le=32768)

    # --- Draft-only operation ------------------------------------------------
    #: When true, nothing is ever emailed: the pipeline never takes the auto-send path,
    #: regardless of ``rollout_phase``.
    #:
    #: Defaults to **false** so the documented §8.5 phase behaviour is unchanged for the
    #: deployed pipeline. The local runner does not rely on this default — it forces the
    #: flag on, and additionally uses a read-only Gmail client and a resolver that never
    #: approves, so a local run cannot send even if this were misconfigured.
    draft_only: bool = False
    #: Addresses to copy on the eventual reply, shown on the Slack review post. Config only
    #: — the agent never chooses recipients.
    reply_cc: tuple[str, ...] = ()

    # --- Amazon Bedrock (§8.1) — the deployed LLM provider -------------------
    aws_region: str = "us-east-1"
    #: Bedrock model / inference-profile id used to drive the agent loop in AWS.
    #: Verify availability in your region with `aws bedrock list-inference-profiles`.
    model_draft: str = "us.anthropic.claude-sonnet-5-v1:0"

    # --- Groq (local / non-AWS LLM provider) --------------------------------
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: float = Field(default=60.0, gt=0)

    @property
    def groq_configured(self) -> bool:
        return bool(self.groq_api_key.get_secret_value())

    # --- Gmail via a dedicated service account (§8.1.2) ---------------------
    #: Path to the service-account JSON key…
    google_sa_file: str = ""
    #: …or its contents inline, for secret stores that inject a single value.
    google_sa_json: SecretStr = SecretStr("")
    google_timeout_seconds: float = Field(default=30.0, gt=0)

    #: The mailbox to read and draft in — also the user the service account impersonates.
    #: Falls back to ``mailbox`` when blank.
    gmail_user: str = ""
    #: Gmail search syntax (not IMAP): ``is:unread``, ``newer_than:2d``, ``from:…``.
    gmail_query: str = "is:unread"
    gmail_fetch_limit: int = Field(default=10, ge=1, le=200)
    #: Leave false while iterating so the same mail can be reprocessed.
    gmail_mark_seen: bool = False
    #: Save each gate-passing reply to Drafts for a human to review and send.
    gmail_create_draft: bool = True

    @property
    def google_sa_configured(self) -> bool:
        return bool(self.google_sa_file or self.google_sa_json.get_secret_value())

    @property
    def gmail_configured(self) -> bool:
        """True when the Gmail client has a key and a mailbox to impersonate."""

        return self.google_sa_configured and bool(self.gmail_user or self.mailbox)

    # --- Slack (§4.6) — channel names for the deployed approval flow ---------
    #: Local runs post nothing to Slack (drafts go to Gmail Drafts, escalations to the log);
    #: these are the channels the deployed Phase 1 processor targets. A real Slack client
    #: belongs with the callback Lambda — see docs/AWS_DEPLOYMENT.md.
    slack_approval_channel: str = "#payments-approvals"
    slack_security_channel: str = "#payments-security"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached)."""

    return Settings()
