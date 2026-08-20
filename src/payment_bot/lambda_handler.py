"""AWS entrypoint for the Stage 1 worker Lambda (AWS_DEPLOYMENT_PLAN.md §2).

Deliberately thin. The local runner's ``process_inbox`` is already headless — fetch unread,
run the pipeline per email, gate, save a draft to Gmail Drafts — so this module does the
three things Lambda needs and then calls straight into it:

1. **Resolve secrets into the process environment** before ``get_settings()`` reads them.
   Everything the bot needs is a ``PAYBOT_*`` variable, so a secret becomes configuration by
   being exported under the right name; nothing downstream learns where it came from.
2. **Fetch the factoring roster** from S3 to ``/tmp`` and point
   ``PAYBOT_FACTORING_DOMAINS_FILE`` at it. The roster is business data with its own
   lifecycle (§3.3) and must not be baked into the deployment artifact.
3. **Swap the LLM**: :func:`build_bedrock_client` in place of the local Groq client.

Both steps happen at **cold start**, module scope, so a warm container pays neither cost.
That is also why they are not in the handler body: a Secrets Manager read per invocation
would be a per-run charge and a per-run failure mode for a value that never changes within
a container's life.

The handler never sends email. ``process_inbox`` forces ``draft_only`` and passes a
:class:`DeferredApprovalResolver`, and the Gmail client's ``send_reply`` raises — the same
three independent guarantees the local runner documents. Stage 2 is where sending moves,
into a separate callback function with its own role.
"""

from __future__ import annotations

import json
import os
from collections.abc import MutableMapping
from typing import Any

from payment_bot.approvals import ChatPostLedger, S3ApprovalStore
from payment_bot.block_ledger import BlockLedger
from payment_bot.clients import (
    CargoTelClient,
    NullSlackClient,
    SlackClient,
    build_bedrock_client,
    build_cargotel_client,
    build_gmail_api_client,
    build_transport_pro_client,
)
from payment_bot.clients.google_chat import (
    GoogleChatClient,
    approval_card,
    build_google_chat_client,
)
from payment_bot.config import Settings, get_settings
from payment_bot.local_runner import _Clients, process_inbox
from payment_bot.logging import configure_logging, get_logger
from payment_bot.pipeline import Outcome, PipelineResult

_log = get_logger("lambda")

#: Where the roster is written. ``/tmp`` is the only writable path in Lambda, and it
#: survives for the life of the container — so a warm invocation reuses the file.
ROSTER_PATH = "/tmp/factoring_domains.json"

#: Where the carrier contact list is written, same reasoning.
CONTACTS_PATH = "/tmp/carrier_contacts.json"

#: Where the gate-block retry ledger lives, inside the same config bucket the rosters use
#: (one bucket, one lifecycle, one IAM story — same reasoning as the carrier contacts).
#: Deliberately under ``state/`` so a human browsing the bucket can tell operator-owned
#: config from bot-owned bookkeeping at a glance.
BLOCK_LEDGER_KEY = "state/gate_block_ledger.json"

#: Which chat cards have been posted, per kind+message id — the escalation/block cards
#: have no pending entry to dedup on and re-run every poll by design. Same bucket, same
#: ``state/*`` reasoning as the block ledger above.
CHAT_POST_LEDGER_KEY = "state/chat_post_ledger.json"

#: Secret ARN/name in the environment → the ``PAYBOT_*`` variable its value becomes.
#:
#: Keyed this way round so the template names the secrets and this module needs no
#: knowledge of the account. A variable that is absent is skipped rather than defaulted:
#: an unset CargoTel password is a *configuration* state (the path is off), while a wrong
#: one is a failure, and the two must not look alike.
SECRET_ENV_MAP: dict[str, str] = {
    "PAYBOT_SECRET_GOOGLE_SA": "PAYBOT_GOOGLE_SA_JSON",
    "PAYBOT_SECRET_TP_PASSWORD": "PAYBOT_TP_PASSWORD",
}


def _boto3() -> Any:
    """Import boto3, with the same actionable error the other AWS call sites give."""

    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - the Lambda runtime always has it
        raise RuntimeError(
            "boto3 is required in the Lambda runtime but could not be imported"
        ) from exc
    return boto3


def load_secrets(env: dict[str, str] | None = None) -> list[str]:
    """Resolve every configured secret into its ``PAYBOT_*`` variable.

    Returns the names of the variables that were set, for the cold-start log line. Values
    are never logged or returned — the point of the indirection is that they exist in the
    process environment and nowhere else.

    A secret that is referenced but unreadable raises. Failing closed is the only safe
    behaviour: the alternative is a run that starts, cannot authenticate to Gmail, and
    reports "no mail matched" — a silent no-op that looks exactly like a quiet inbox.
    """

    environ = os.environ if env is None else env
    resolved: list[str] = []
    client = None
    for source, target in SECRET_ENV_MAP.items():
        secret_id = environ.get(source, "").strip()
        if not secret_id:
            continue
        if client is None:
            client = _boto3().client("secretsmanager")
        value = client.get_secret_value(SecretId=secret_id)["SecretString"]
        environ[target] = value
        resolved.append(target)
    return resolved


def _fetch_config_object(
    environ: MutableMapping[str, str],
    *,
    bucket_var: str,
    key_var: str,
    path: str,
    points_at: str,
) -> str | None:
    """Download one S3 config object to ``path`` and point ``points_at`` at it.

    Returns the path written, or ``None`` when the object is not configured — which is a
    valid deployment for both callers, since each has an inline counterpart that can stand
    alone.

    An unreadable object raises rather than proceeding without it. Both files are
    authorization data, and the failure mode is identical to
    ``Settings._merge_domain_file``'s: a list that silently loaded as empty looks configured
    and authorises nobody, turning every affected enquiry into a quiet escalation that would
    take a day to notice.
    """

    bucket = environ.get(bucket_var, "").strip()
    key = environ.get(key_var, "").strip()
    if not (bucket and key):
        return None

    _boto3().client("s3").download_file(bucket, key, path)
    environ[points_at] = path
    return path


def load_roster(env: dict[str, str] | None = None, *, path: str = ROSTER_PATH) -> str | None:
    """Fetch the factoring roster from S3 to ``path`` and point settings at it.

    ``None`` when no roster is configured: ``PAYBOT_FACTORING_DOMAINS`` can carry the inline
    patches alone.
    """

    return _fetch_config_object(
        os.environ if env is None else env,
        bucket_var="PAYBOT_ROSTER_BUCKET",
        key_var="PAYBOT_ROSTER_KEY",
        path=path,
        points_at="PAYBOT_FACTORING_DOMAINS_FILE",
    )


def load_carrier_contacts(
    env: dict[str, str] | None = None, *, path: str = CONTACTS_PATH
) -> str | None:
    """Fetch the carrier contact list from S3 to ``path`` and point settings at it.

    ``None`` when no contact list is configured: ``PAYBOT_CARRIER_CONTACTS`` can carry the
    inline entries alone, which is how this shipped before the file existed.

    Shares ``PAYBOT_ROSTER_BUCKET`` deliberately. One private config bucket holds both
    objects (§3.3) — a second bucket variable would imply a second lifecycle that does not
    exist, and the IAM grant is per-key either way.
    """

    return _fetch_config_object(
        os.environ if env is None else env,
        bucket_var="PAYBOT_ROSTER_BUCKET",
        key_var="PAYBOT_CARRIER_CONTACTS_KEY",
        path=path,
        points_at="PAYBOT_CARRIER_CONTACTS_FILE",
    )


def load_block_ledger(env: dict[str, str] | None = None) -> tuple[BlockLedger, str | None]:
    """Read the gate-block ledger from the config bucket; ``(ledger, bucket)``.

    No bucket configured (or no ledger written yet) is an ordinary state — the run
    proceeds with an empty ledger, which simply means every message still has its full
    retry budget. Any other read error also degrades to empty rather than failing the
    run: the ledger is bookkeeping, and losing it costs at most a few duplicate retries,
    while raising would stop every draft over it.
    """

    environ = os.environ if env is None else env
    bucket = environ.get("PAYBOT_ROSTER_BUCKET", "").strip()
    if not bucket:
        return BlockLedger(), None
    client = _boto3().client("s3")
    try:
        raw = client.get_object(Bucket=bucket, Key=BLOCK_LEDGER_KEY)["Body"].read()
        return BlockLedger.from_json(raw.decode("utf-8")), bucket
    except client.exceptions.NoSuchKey:
        return BlockLedger(), bucket
    except Exception as exc:
        _log.warning("block_ledger_load_failed", extra={"error": str(exc)})
        return BlockLedger(), bucket


def save_block_ledger(ledger: BlockLedger, bucket: str | None) -> None:
    """Write the ledger back when anything changed. Failure is logged, never raised."""

    if not bucket or not ledger.dirty:
        return
    try:
        _boto3().client("s3").put_object(
            Bucket=bucket,
            Key=BLOCK_LEDGER_KEY,
            Body=ledger.to_json().encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:
        _log.warning("block_ledger_save_failed", extra={"error": str(exc)})


def load_chat_post_ledger(env: dict[str, str] | None = None) -> tuple[ChatPostLedger, str | None]:
    """Read the chat-post dedup ledger; same degrade-to-empty contract as the block ledger.

    Losing it costs a duplicate card in the space, which a human sees and ignores;
    failing the run over it would stop every draft.
    """

    environ = os.environ if env is None else env
    bucket = environ.get("PAYBOT_ROSTER_BUCKET", "").strip()
    if not bucket:
        return ChatPostLedger(), None
    client = _boto3().client("s3")
    try:
        raw = client.get_object(Bucket=bucket, Key=CHAT_POST_LEDGER_KEY)["Body"].read()
        return ChatPostLedger.from_json(raw.decode("utf-8")), bucket
    except client.exceptions.NoSuchKey:
        return ChatPostLedger(), bucket
    except Exception as exc:
        _log.warning("chat_post_ledger_load_failed", extra={"error": str(exc)})
        return ChatPostLedger(), bucket


def save_chat_post_ledger(ledger: ChatPostLedger, bucket: str | None) -> None:
    """Write the chat-post ledger back when anything changed. Logged, never raised."""

    if not bucket or not ledger.dirty:
        return
    try:
        _boto3().client("s3").put_object(
            Bucket=bucket,
            Key=CHAT_POST_LEDGER_KEY,
            Body=ledger.to_json().encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:
        _log.warning("chat_post_ledger_save_failed", extra={"error": str(exc)})


def bootstrap() -> Settings:
    """Cold-start work: logging, secrets, roster — then a settings object built on top."""

    configure_logging(os.environ.get("PAYBOT_LOG_LEVEL", "INFO"))
    secrets = load_secrets()
    roster = load_roster()
    contacts = load_carrier_contacts()
    # get_settings is lru_cached, so it must not be called before the environment is whole.
    get_settings.cache_clear()
    settings = get_settings()
    _log.info(
        "lambda_cold_start",
        extra={
            "secrets_loaded": secrets,
            "roster_path": roster,
            "carrier_contacts_path": contacts,
            "carrier_contacts": len(settings.carrier_contacts),
            "mailbox": settings.gmail_user or settings.mailbox,
            "model": settings.model_draft,
            "region": settings.aws_region,
            "cargotel": bool(settings.cargotel_replies and settings.cargotel_configured),
            "env": settings.env,
        },
    )
    return settings


def build_clients(
    settings: Settings, chat_post_ledger: ChatPostLedger | None = None
) -> _Clients:
    """The deployed client set: Bedrock for the model; Google Chat when a space is set.

    ``tp_factory`` and ``cargotel_factory`` are per-email by contract — each client caches
    for its lifetime, so one per email is what keeps a single email's reads to one
    consistent snapshot. Reusing one across a batch would let a load change underneath a
    run that had already quoted it.

    The chat client is *interactive* (buttons on cards) only in ``approval_mode=chat``;
    a space with ``approval_mode=drafts`` is shadow mode — cards for visibility, Gmail
    Drafts unchanged (CHAT_APPROVAL_PLAN.md §10 step 2).
    """

    slack: SlackClient = NullSlackClient()
    if settings.chat_space.strip():
        try:
            slack = build_google_chat_client(
                settings,
                interactive=settings.chat_approval_on,
                post_ledger=chat_post_ledger,
            )
        except Exception as exc:
            # A misconfigured chat client must not stop the mail run: drafts still land
            # in Gmail via the runner's fallback, and this line is the signal to fix it.
            _log.warning("chat_client_unavailable", extra={"error": str(exc)})

    def cargotel_factory() -> CargoTelClient | None:
        if not (settings.cargotel_replies and settings.cargotel_configured):
            return None
        return build_cargotel_client(settings)

    return _Clients(
        tp_factory=lambda: build_transport_pro_client(settings),
        gmail=build_gmail_api_client(settings),
        slack=slack,
        llm=build_bedrock_client(settings),
        cargotel_factory=cargotel_factory,
    )


def _sweep_approvals(
    store: S3ApprovalStore, slack: SlackClient, settings: Settings
) -> None:
    """Expire cards nobody clicked; runs inside the invocation, never its own schedule.

    An expired entry is the chat flow's ``gate_block_retries_exhausted``: the mail sits
    unread, nothing will retry it, and the updated card says a human must act
    (CHAT_APPROVAL_PLAN.md §5). Failure here is logged and swallowed — the sweep is
    bookkeeping and the mail run's results already stand.
    """

    try:
        expired = store.sweep(expiry_days=settings.approval_expiry_days)
    except Exception as exc:
        _log.warning("approval_sweep_failed", extra={"error": str(exc)})
        return
    if not expired:
        return
    chat = slack if isinstance(slack, GoogleChatClient) else None
    for entry in expired:
        _log.warning(
            "approval_expired",
            extra={
                "correlation_id": entry.message_id,
                "entry_id": entry.entry_id,
                "age_days": settings.approval_expiry_days,
            },
        )
        if chat is not None and entry.chat_message:
            chat.update_status(
                entry.chat_message,
                approval_card(
                    entry_id=entry.entry_id,
                    from_email=entry.to,
                    load_ids=entry.load_ids,
                    to=entry.to,
                    cc=entry.cc,
                    reply_to=entry.reply_to,
                    subject=entry.subject,
                    body=entry.body,
                    status=(
                        f"EXPIRED — no action for {settings.approval_expiry_days} days. "
                        "The mail sits unread; a human must reply from the group mailbox."
                    ),
                    message_id=entry.message_id,
                ),
            )


#: Resolved once per container. A cold-start failure must surface as an invocation error —
#: an import-time raise here is what makes a broken secret or roster page immediately
#: rather than turning into a run that quietly answers nothing.
_SETTINGS: Settings | None = None


def _settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = bootstrap()
    return _SETTINGS


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    """EventBridge target. Processes the inbox once and returns a per-outcome summary.

    The return value is what CloudWatch shows beside the invocation, so it carries the
    counts a human checks first. Detail stays in the JSON log lines, where the metric
    filters read it.

    ``limit`` may be overridden per invocation — ``{"limit": 1}`` from the console is the
    cutover step-1 smoke test (§4), one email through the live path with everything else
    untouched.
    """

    settings = _settings()
    limit = None
    if isinstance(event, dict) and event.get("limit") is not None:
        limit = int(event["limit"])
    if limit is None:
        limit = settings.gmail_fetch_limit

    ledger, ledger_bucket = load_block_ledger()
    chat_ledger, chat_ledger_bucket = (
        load_chat_post_ledger() if settings.chat_space.strip() else (ChatPostLedger(), None)
    )
    clients = build_clients(settings, chat_post_ledger=chat_ledger)

    # Chat approval needs somewhere for pending entries to live; without the bucket the
    # mode cannot be honoured, so it degrades loudly to Gmail drafts rather than posting
    # buttons whose clicks would find nothing to send.
    approval_store: S3ApprovalStore | None = None
    if settings.chat_approval_on:
        bucket = os.environ.get("PAYBOT_ROSTER_BUCKET", "").strip()
        if bucket:
            approval_store = S3ApprovalStore(bucket)
        else:
            _log.warning("chat_approval_without_bucket_falling_back_to_drafts")

    try:
        results: list[PipelineResult] = process_inbox(
            settings,
            limit=limit,
            clients=clients,
            block_ledger=ledger,
            approval_store=approval_store,
        )
        if approval_store is not None:
            _sweep_approvals(approval_store, clients.slack, settings)
    finally:
        # Saved even when the run raises or is cut off mid-batch: blocks recorded before
        # the interruption must count, or a timeout-looping run never spends its budget.
        save_block_ledger(ledger, ledger_bucket)
        save_chat_post_ledger(chat_ledger, chat_ledger_bucket)

    counts: dict[str, int] = {}
    for result in results:
        counts[result.outcome.value] = counts.get(result.outcome.value, 0) + 1
    summary = {"processed": len(results), "outcomes": counts}

    # A SENT in a draft-only deployment means one of the three guarantees has broken. Log it
    # at error so the RunFailures alarm catches it; do not raise, because the drafts this run
    # produced are legitimate and a retry would reprocess them.
    if any(r.outcome is Outcome.SENT for r in results):  # pragma: no cover - defensive
        _log.error("draft_only_violated", extra={"summary": json.dumps(summary)})

    _log.info("lambda_run_complete", extra=summary)
    return summary
