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

import json
from enum import IntEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RolloutPhase(IntEnum):
    """Phased rollout from the PRD §8.5.

    The pre-send gate runs in *every* phase; the phase only controls whether a human
    Slack approval click is required before a send.
    """

    APPROVE = 1  # Every draft requires human Approve/Edit/Reject in Slack.
    SELECTIVE_AUTOSEND = 2  # Low-risk, gate-passing intents may send without a click.


def _merge_domain_file(values: Any, inline_key: str, file_key: str) -> Any:
    """Merge a JSON name→domains roster file *under* its inline counterpart.

    A name → tuple-of-domains map grows past what fits on one ``.env`` line, and a
    hand-curated inline entry must be able to override a generated file entry. Kept as a
    free function so a second roster can reuse it without duplicating the failure handling.

    Runs as a ``mode="before"`` validator because :class:`Settings` is frozen. A
    configured-but-unreadable file **raises** rather than silently authorising nobody —
    an authorization roster that quietly failed to load is the worst of both worlds: it
    looks configured and denies everyone, and ops has no signal that it did.
    """

    if not isinstance(values, dict):
        return values
    path_str = values.get(file_key) or ""
    if not path_str:
        return values
    path = Path(str(path_str))
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{file_key} {path_str!r} could not be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{file_key} {path_str!r} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{file_key} {path_str!r} must hold a JSON object")

    inline = values.get(inline_key) or {}
    if isinstance(inline, str):  # env sources may hand the raw JSON string through
        inline = json.loads(inline)
    values[inline_key] = {**loaded, **inline}
    return values


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

    #: Answer a factoring company that is the factor of record on the load.
    #:
    #: Factors are a large share of real inbound — RTS, OTR, Love's, Sound Finance, HaulPay
    #: and others all write in — and a rate-verification request from the factor named on the
    #: load's NOA is a legitimate question, not an attack. With this off, every one of them
    #: escalates.
    #:
    #: Only ever reached via ``factoring_domains``: a sender is recognised as the factor when
    #: its whole domain is configured for the factor recorded on that load. Name resemblance
    #: alone returns DENY. So this switch cannot open the substring-matching hole that
    #: existed before that list.
    #:
    #: Defaults to false — the safe default for a fresh checkout is to escalate. Turning it on
    #: is a deliberate policy decision, and the draft still goes to a human either way.
    allow_factoring: bool = False

    #: Factoring companies whose senders may be recognised, mapped to the domains they mail
    #: from — e.g. ``{"rts financial": ["rtsfinancial.com", "ryanrts.com"]}``.
    #:
    #: This exists because Transport Pro's API gives the factoring company's *name* and no
    #: contact address. Checked on live data: neither ``/voiceai/load/{n}/payment_information``
    #: nor ``GET /carrier/{id}`` returns the remit email the Transport Pro UI displays, so
    #: ``factoring_emails`` is always empty and there is nothing to match a sender against.
    #:
    #: The only alternative is guessing from the company name, and that is not safe for an
    #: authorization decision: the match is a substring test against the sender's flattened
    #: domain, so a factor called "RTS" would be satisfied by ``imports-llc.com`` and one
    #: called "… Financial" by any domain containing "finance". A curated map makes each
    #: factor a deliberate, auditable entry instead of a string coincidence.
    #:
    #: Empty by default: no factoring sender is recognised until someone adds one.
    factoring_domains: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    #: Optional JSON file holding additional factoring domains, same shape as
    #: ``factoring_domains``. Exists because the full roster (generated from the settlement
    #: system's factoring-company export by ``scripts/generate_factoring_domains.py``) runs
    #: to hundreds of entries — too much for one ``.env`` line. Inline entries win on a key
    #: collision, so a hand-curated correction always beats the generated file.
    factoring_domains_file: str = ""

    #: Review artifact listing, per factor, the domains our OWN records hold and how many
    #: carriers factor to them. Read only when an unknown factoring sender escalates, to put
    #: "the domain we have on file" beside "the domain that just wrote in" — see
    #: :mod:`payment_bot.roster_candidate`.
    #:
    #: Never consulted for an authorization decision, and it must not be: these domains come
    #: from mixed sources (AP records, contact records, a website lookup) and a website-sourced
    #: domain is the same evidence class as a guess. It informs a human, nothing else.
    #:
    #: Optional. Unset, escalations lose one line of context and behave exactly as before.
    factor_domain_hints_file: str = ""

    @model_validator(mode="before")
    @classmethod
    def _merge_factoring_domains_file(cls, values: Any) -> Any:
        """Load ``factoring_domains_file`` and merge it under the inline map.

        Runs before validation because the model is frozen. A configured-but-unreadable
        file raises rather than silently authorising nobody — ops must know the roster
        did not load.
        """

        return _merge_domain_file(values, "factoring_domains", "factoring_domains_file")

    # --- CargoTel (6-digit loads) --------------------------------------------
    #: The load-maintenance page. CargoTel has no API, so the "client" scrapes this.
    cargotel_base_url: str = "https://circle.cargotel.com/backoffice/loadmaint.mcgi"
    #: The carrier client record. A separate page, reached from the load's carrier panel,
    #: and the only source of contact addresses on this path.
    cargotel_client_url: str = "https://circle.cargotel.com/backoffice/client.mcgi"
    #: Where the login bot keeps the browser session cookie. There is no service credential
    #: for CargoTel — a scraped session is the only way in — so this is the whole auth story.
    cargotel_cookie_bucket: str = "circle-bot-cookies"
    cargotel_cookie_key: str = "rubicon/cargotel.json"
    #: Generous by default: the page is ~250 KB of CGI-rendered tables.
    cargotel_timeout_seconds: float = Field(default=60.0, gt=0)

    @property
    def cargotel_configured(self) -> bool:
        """True when enough is set to build a live :class:`CargoTelHttpClient`."""

        return bool(
            self.cargotel_base_url
            and self.cargotel_client_url
            and self.cargotel_cookie_bucket
            and self.cargotel_cookie_key
        )

    #: Answer carrier mail about 6-digit / CargoTel loads at all.
    #:
    #: A deliberate policy switch in the spirit of ``allow_factoring``, defaulting to the
    #: strict behaviour — and here the default is doing real work rather than being
    #: cautious for its own sake. **Authorization has no source on this path yet.** A
    #: CargoTel load page names the carrier but carries no contact address, and the
    #: factoring company and its remit email live on a screen that is not wired
    #: (Accounting → Payables → Carrier List & Settings). Until that exists,
    #: ``check_authorization`` cannot resolve a sender and every 6-digit load escalates.
    #:
    #: Turning this on before that lands buys nothing: the loads would still escalate, one
    #: layer later.
    cargotel_replies: bool = False

    # --- Agent loop ----------------------------------------------------------
    #: Iteration budget for a SINGLE-load email. Multi-load emails get more — see
    #: ``agent_iterations_per_extra_load``.
    agent_max_iterations: int = Field(default=12, ge=1, le=50)

    #: Extra iterations granted per load beyond the first.
    #:
    #: The skill procedures are per-load ("`tp_get_load_summary` for each load id"), so a
    #: two-load email costs very nearly twice a one-load email: summary, pay date, dispatch,
    #: cross-check, settlement and file history all run again. A fixed cap therefore cannot
    #: serve a variable number of loads, and ``bulk_threshold`` lets up to five through.
    #:
    #: Observed live on load 2487002 + 2457019: fourteen tool calls, every one successful,
    #: run ended at max_iterations with no draft because the budget was 12. Nothing was
    #: wrong with the model's behaviour — the arithmetic simply did not allow it to finish.
    #:
    #: Default 9 = one full per-load pass (8 calls) plus a little slack.
    #:
    #: Counted off a live 5-load email: per load the agent calls load_summary,
    #: compute_scheduled_pay_date, dispatch_history, carrier_cross_check,
    #: settlement_entries, file_history, noa_factoring and check_authorization — eight —
    #: and then one submit_draft for the whole reply. An earlier value of 7 was one short
    #: per load, which at five loads produced a budget of 40 against 41 calls needed: the
    #: run made every call successfully and died with nothing left for submit_draft.
    #:
    #: The slack matters because a single tool error costs a whole iteration, and this model
    #: issues one tool call per turn even though the loop can execute several.
    #:
    #: The total is clamped by :data:`~payment_bot.pipeline.ITERATION_CEILING`; 9 keeps the
    #: five-load case (48) under it without being clamped.
    agent_iterations_per_extra_load: int = Field(default=9, ge=0, le=20)
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

    #: The sign-off every draft ends with. Injected into the intake message, because on
    #: live mail the model invented identities when left to choose — one draft signed as
    #: the *carrier's* company ("SAM AUTO TRANS LLC Accounting"), i.e. as the person it was
    #: replying to. Config, not prompt text, so each deployment signs as its own team.
    reply_signature: str = "Circle Delivers Payments"

    #: Where senders should email missing paperwork. When ``tp_get_file_history`` reports a
    #: required document absent (a missing carrier invoice is exactly why many loads sit
    #: unscheduled), the reply names what is missing and asks for it at this address —
    #: turning "your payment is pending" into an answer the sender can act on.
    documents_email: str = "freightpay@circledelivers.com"

    #: Draft status replies even when the email contains bank/ACH change WORDING, provided
    #: the sender is authorized and asked something answerable. A deliberate policy switch
    #: (like ``allow_factoring``), defaulting to the strict behaviour.
    #:
    #: What makes it defensible when on: the bot can move no money; every draft is
    #: human-reviewed; the ``change_acknowledgment`` gate check forbids a reply from
    #: confirming or acting on the instruction; and disclosure is still gated by
    #: authorization. What it does NOT relax: paperwork and identity actions (NOA setup,
    #: void-check / direct-deposit / NOA attachments, contact changes) and change
    #: instructions with no answerable ask — those always escalate. The change request
    #: itself still needs a human to action; the reply merely stops being held hostage
    #: to it.
    sensitive_bank_replies: bool = False

    #: The NOA counterpart of ``sensitive_bank_replies``: draft status replies past NOA
    #: action WORDING ("we've updated the factoring", "please add our NOA") for authorized
    #: senders with an answerable ask. An actual NOA **attachment** is governed separately
    #: by ``noa_attachment_replies`` — a Notice of Assignment is a legal document someone
    #: must verify and file, which no status reply can do. Gate check #9 forbids the reply
    #: from acknowledging or acting on the setup request either way.
    sensitive_noa_replies: bool = False

    #: Draft replies past an attached NOA / notice-of-assignment file. Pre-funding factors
    #: attach their NOA to routine rate verifications as part of the standard packet —
    #: England Carrier Services' template does exactly this — and with this off every one
    #: of them escalates unanswered. What it does NOT change: the NOA still needs a human
    #: to verify and file (the draft cannot do either, and the change_acknowledgment gate
    #: check forbids the reply from claiming it was filed); disclosure is still gated by
    #: authorization; and void-check / direct-deposit attachments and contact changes
    #: escalate in every configuration. Defaults to the strict behaviour.
    noa_attachment_replies: bool = False

    #: Answer a roster-verified factoring company about a load that shows NO factor on
    #: file — the pre-funding flow: factors verify rates BEFORE their NOA reaches us, so
    #: Transport Pro still says remit-to self and the per-load factor match cannot fire.
    #: The reply answers the rate/status question and asks for the NOA and billing
    #: paperwork at ``documents_email``. Verification is roster membership (the domains
    #: generated from the settlement export + hand-verified inline patches). A load
    #: factored to a DIFFERENT company is unaffected — that always stays per-load bound.
    factoring_prenoa_replies: bool = False

    #: Which document categories a draft may report as missing and request from the
    #: sender. Values are :class:`payment_bot.domain.documents.DocCategory` names, e.g.
    #: carrier_invoice, proof_of_delivery, rate_agreement. Config so the business can
    #: tune WHAT gets chased without a code change — and the single edit point for when
    #: the authoritative ``GET /load/missing_documents`` source lands (see
    #: docs/MISSING_DOCUMENTS_CACHE.md): the tool keeps this same output shape either way.
    required_documents: tuple[str, ...] = (
        "carrier_invoice",
        "proof_of_delivery",
        "rate_agreement",
    )

    # --- Amazon Bedrock (§8.1) — the deployed LLM provider -------------------
    aws_region: str = "us-east-1"

    #: Named AWS profile to use for boto3 calls. Blank means the default credential chain.
    #:
    #: This setting exists because ``.env`` is not a credential source for boto3 and never
    #: will be: boto3 reads the *process environment*, so a developer who has put everything
    #: else in ``.env`` still gets "Unable to locate credentials" unless they separately
    #: export ``AWS_PROFILE`` in whichever shell happens to run the bot. Observed exactly
    #: that way on a live run — every 6-digit load in one email failed to read the CargoTel
    #: cookie while the same profile worked fine from the command line moments earlier.
    #:
    #: Leave blank in Lambda. There is no profile there: the function's execution role is
    #: the credential source, and naming a profile that does not exist would break it.
    aws_profile: str = ""
    #: Bedrock **inference profile** id used to drive the agent loop in AWS.
    #:
    #: A profile (``us.``/``global.`` prefixed), not a bare foundation-model id: the profile
    #: is what carries cross-region capacity. Both deploy and validate happily with a wrong
    #: value here — the error arrives at the first ``converse`` call, as an access denial
    #: that reads like a permissions problem rather than a typo.
    #:
    #: The version suffix is not universal. This default carried ``-v1:0`` and no such
    #: profile existed: the live account lists ``us.anthropic.claude-sonnet-5`` plain, while
    #: its 4.5 sibling really is ``…-20250929-v1:0``. Always confirm against the account
    #: rather than pattern-matching off another id:
    #:
    #:     aws bedrock list-inference-profiles \
    #:       --query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude')].inferenceProfileId"
    model_draft: str = "us.anthropic.claude-sonnet-5"

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
    #: Members of the monitored group whose addresses are NOT on ``mailbox``'s own domain.
    #:
    #: A thread that anyone on our side has written in is skipped — a colleague who started it
    #: or replied anywhere in it owns that conversation. Colleagues are already covered by the
    #: domain rule, so this list is only for a member who sits outside it: a shared mailbox on
    #: another domain, a contractor, an alias. Leave it empty unless the group has such members.
    #:
    #: The group address itself must NOT be listed. Google rewrites the From of DMARC-strict
    #: external senders to exactly that address ("teamamy via Payment Status <paystatus@…>"),
    #: so treating it as ours would make every such carrier invisible.
    gmail_group_members: tuple[str, ...] = ()

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
