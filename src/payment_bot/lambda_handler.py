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
from typing import Any

from payment_bot.clients import (
    CargoTelClient,
    NullSlackClient,
    SlackClient,
    build_bedrock_client,
    build_cargotel_client,
    build_gmail_api_client,
    build_transport_pro_client,
)
from payment_bot.config import Settings, get_settings
from payment_bot.local_runner import _Clients, process_inbox
from payment_bot.logging import configure_logging, get_logger
from payment_bot.pipeline import Outcome, PipelineResult

_log = get_logger("lambda")

#: Where the roster is written. ``/tmp`` is the only writable path in Lambda, and it
#: survives for the life of the container — so a warm invocation reuses the file.
ROSTER_PATH = "/tmp/factoring_domains.json"

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


def load_roster(env: dict[str, str] | None = None, *, path: str = ROSTER_PATH) -> str | None:
    """Fetch the factoring roster from S3 to ``path`` and point settings at it.

    Returns the path written, or ``None`` when no roster is configured — which is a valid
    deployment: ``PAYBOT_FACTORING_DOMAINS`` can carry the inline patches alone.

    An unreadable roster raises rather than proceeding with an empty one, matching
    ``Settings._merge_factoring_domains_file``: a roster that silently authorises nobody
    turns every factoring enquiry into an escalation, and it would take a day to notice.
    """

    environ = os.environ if env is None else env
    bucket = environ.get("PAYBOT_ROSTER_BUCKET", "").strip()
    key = environ.get("PAYBOT_ROSTER_KEY", "").strip()
    if not (bucket and key):
        return None

    _boto3().client("s3").download_file(bucket, key, path)
    environ["PAYBOT_FACTORING_DOMAINS_FILE"] = path
    return path


def bootstrap() -> Settings:
    """Cold-start work: logging, secrets, roster — then a settings object built on top."""

    configure_logging(os.environ.get("PAYBOT_LOG_LEVEL", "INFO"))
    secrets = load_secrets()
    roster = load_roster()
    # get_settings is lru_cached, so it must not be called before the environment is whole.
    get_settings.cache_clear()
    settings = get_settings()
    _log.info(
        "lambda_cold_start",
        extra={
            "secrets_loaded": secrets,
            "roster_path": roster,
            "mailbox": settings.gmail_user or settings.mailbox,
            "model": settings.model_draft,
            "region": settings.aws_region,
            "cargotel": bool(settings.cargotel_replies and settings.cargotel_configured),
            "env": settings.env,
        },
    )
    return settings


def build_clients(settings: Settings) -> _Clients:
    """The deployed client set: Bedrock for the model, no Slack until Stage 2.

    ``tp_factory`` and ``cargotel_factory`` are per-email by contract — each client caches
    for its lifetime, so one per email is what keeps a single email's reads to one
    consistent snapshot. Reusing one across a batch would let a load change underneath a
    run that had already quoted it.
    """

    slack: SlackClient = NullSlackClient()

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

    results: list[PipelineResult] = process_inbox(
        settings,
        limit=limit,
        clients=build_clients(settings),
    )

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
