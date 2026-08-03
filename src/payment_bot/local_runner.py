"""Local draft-only entrypoint (``payment-bot-local``).

Reads real carrier mail from ``paystatus@`` via the Gmail API, answers it with the local
LLM driving the tool-use loop against **live Transport Pro** data, runs the deterministic
pre-send gate, and saves each gate-passing reply to Gmail Drafts for review. **No email is
ever sent**: you read the draft and send it yourself.

Nothing is sent because three independent things all say so:

1. ``PAYBOT_DRAFT_ONLY=true`` — the pipeline never takes the auto-send path.
2. :class:`DeferredApprovalResolver` — approval is never granted in-process, so the run ends
   at ``Outcome.AWAITING_REVIEW``.
3. :class:`~payment_bot.clients.gmail_api.GmailApiClient` — its ``send_reply`` raises.

Usage::

    payment-bot-local                 # process the newest unread mail
    payment-bot-local --limit 3       # cap how many messages this run handles
    payment-bot-local --dry-run       # print drafts, post nothing to Slack
    payment-bot-local --check         # verify configuration and connectivity, process nothing

Everything is read from ``PAYBOT_*`` environment variables / ``.env`` — see
``docs/LOCAL_RUN.md``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass

from payment_bot.clients import (
    GMAIL_DRAFT_SCOPES,
    DeferredApprovalResolver,
    DraftingGmailClient,
    DraftMessage,
    GmailClient,
    LlmClient,
    NullSlackClient,
    SlackClient,
    TransportProClient,
    build_gmail_api_client,
    build_groq_client,
    build_transport_pro_client,
    load_service_account_info,
)
from payment_bot.config import Settings, get_settings
from payment_bot.errors import PaymentBotError
from payment_bot.logging import InMemoryAuditSink, configure_logging, get_logger
from payment_bot.models import InboundEmail
from payment_bot.pipeline import Outcome, PaymentBotPipeline, PipelineResult

_log = get_logger("local_runner")

_OUTCOME_LABEL = {
    Outcome.AWAITING_REVIEW: "DRAFT READY FOR REVIEW (not sent)",
    Outcome.ESCALATED: "ESCALATED (no draft)",
    Outcome.BLOCKED: "BLOCKED BY GATE (not sent)",
    Outcome.REJECTED: "REJECTED",
    Outcome.SENT: "SENT",
    Outcome.NO_ACTION: "NO ACTION",
}


@dataclass(slots=True)
class _Clients:
    """The clients one local run needs.

    ``tp_factory`` rather than a Transport Pro instance: a fresh client per email keeps each
    email's load reads to a single consistent snapshot (the client caches for its lifetime).
    """

    tp_factory: Callable[[], TransportProClient]
    gmail: GmailClient
    slack: SlackClient
    llm: LlmClient

    @property
    def drafting_gmail(self) -> DraftingGmailClient | None:
        """The Gmail client, if it can create drafts."""

        client = self.gmail
        return client if isinstance(client, DraftingGmailClient) else None


def _rule(title: str) -> str:
    return f"\n{'─' * 4} {title} {'─' * max(4, 68 - len(title))}"


# ---------------------------------------------------------------------------
# Configuration checks — fail with an actionable message, not a stack trace
# ---------------------------------------------------------------------------
def _missing_configuration(settings: Settings) -> list[str]:
    """Only the genuinely required credentials. Slack is optional."""

    problems: list[str] = []
    if not settings.transport_pro_configured:
        problems.append(
            "Transport Pro: set PAYBOT_TP_BASE_URL, PAYBOT_TP_USERNAME, PAYBOT_TP_PASSWORD"
        )
    if not settings.groq_configured:
        problems.append("Groq: set PAYBOT_GROQ_API_KEY")
    if not settings.gmail_configured:
        problems.append(
            "Gmail: set PAYBOT_GOOGLE_SA_FILE (or PAYBOT_GOOGLE_SA_JSON) and "
            "PAYBOT_GMAIL_USER (the mailbox the service account impersonates)"
        )
    return problems


def _build_clients(settings: Settings, *, dry_run: bool) -> _Clients:
    # No Slack locally: drafts land in Gmail Drafts, and escalations go to the log and the
    # console report. The seam stays for the deployed Phase 1 flow (§8.5).
    slack: SlackClient = NullSlackClient()

    return _Clients(
        tp_factory=lambda: build_transport_pro_client(settings),
        gmail=build_gmail_api_client(settings),
        slack=slack,
        llm=build_groq_client(settings),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _save_draft(
    clients: _Clients,
    email: InboundEmail,
    result: PipelineResult,
    settings: Settings,
) -> DraftMessage | None:
    """Write a gate-passed reply into the Drafts folder, ready for you to send.

    Only ``AWAITING_REVIEW`` qualifies: that outcome means the pre-send gate passed. A
    blocked or escalated run never produces a draft to review.
    """

    if result.draft is None or result.outcome is not Outcome.AWAITING_REVIEW:
        return None
    if not settings.gmail_create_draft:
        return None
    gmail = clients.drafting_gmail
    if gmail is None:
        _log.warning("gmail_client_cannot_create_drafts")
        return None

    try:
        return gmail.create_draft(email, result.draft.reply_body, settings.reply_cc)
    except Exception as exc:
        # A failed draft save must not look like a failed run — and must never kill the
        # emails still queued behind it: the draft text is already in the console report
        # above, so surface the problem and carry on. Broad on purpose; a ValueError from
        # a malformed header once crashed the whole runner mid-batch.
        print(f"  ! could not save the Gmail draft: {exc}")
        _log.warning("gmail_draft_failed", extra={"error": str(exc)})
        return None


def _render(
    email: InboundEmail,
    result: PipelineResult,
    audit: InMemoryAuditSink,
    settings: Settings,
) -> str:
    lines = [
        f"\n{'=' * 74}",
        f"EMAIL   {email.subject or '(no subject)'}",
        f"FROM    {email.from_name or ''} <{email.from_email}>",
        f"ID      {email.message_id}",
    ]

    lines.append(_rule("TOOL TRAIL (§8.1)"))
    for entry in audit.for_correlation(result.correlation_id):
        status = "ok " if entry.ok else "ERR"
        lines.append(f"  [{status}] {entry.tool_name:<28} ({entry.duration_ms:7.1f} ms)")

    lines.append(_rule("PRE-SEND GATE (§5)"))
    if result.gate_result is not None:
        for check in result.gate_result.checks:
            mark = "PASS" if check.passed else "FAIL"
            lines.append(f"  [{mark}] {check.name:<18} {check.detail}")
    else:
        lines.append("  (not reached — the run stopped before a draft existed)")

    if result.draft is not None:
        lines.append(_rule("DRAFT REPLY (NOT SENT)"))
        lines.append(f"  To : {email.from_email}")
        if settings.reply_cc:
            lines.append(f"  Cc : {', '.join(settings.reply_cc)}")
        lines.append(f"  Re : {email.subject}")
        lines.append("")
        for line in result.draft.reply_body.splitlines():
            lines.append(f"  | {line}")
        if result.draft.citations:
            lines.append("")
            lines.append("  Citations:")
            for citation in result.draft.citations:
                lines.append(
                    f"    - {citation.fact}: {citation.value}  ({citation.source_tool})"
                )

    lines.append(_rule("OUTCOME"))
    lines.append(f"  {_OUTCOME_LABEL.get(result.outcome, result.outcome.value.upper())}")
    lines.append(f"  detail : {result.detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def process_inbox(
    settings: Settings | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    clients: _Clients | None = None,
) -> list[PipelineResult]:
    """Fetch mail and produce a reviewable draft for each answerable message.

    Args:
        clients: Pre-built clients, for tests. Built from configuration when omitted.
    """

    # Force draft-only rather than trusting configuration: a local run must never send,
    # whatever PAYBOT_DRAFT_ONLY or PAYBOT_ROLLOUT_PHASE happen to say.
    resolved = (settings or get_settings()).model_copy(update={"draft_only": True})
    clients = clients or _build_clients(resolved, dry_run=dry_run)

    emails = clients.gmail.fetch_new()
    if limit is not None:
        emails = emails[:limit]
    if not emails:
        print(
            f"No mail matched PAYBOT_GMAIL_QUERY={resolved.gmail_query!r} in "
            f"{resolved.gmail_user or resolved.mailbox}. Nothing to do."
        )
        return []

    print(f"Fetched {len(emails)} message(s) from {resolved.gmail_user or resolved.mailbox}.")
    results: list[PipelineResult] = []

    for email in emails:
        audit = InMemoryAuditSink()
        pipeline = PaymentBotPipeline(
            tp=clients.tp_factory(),
            gmail=clients.gmail,
            slack=clients.slack,
            llm=clients.llm,
            approval_resolver=DeferredApprovalResolver(),
            settings=resolved,
            audit_sink=audit,
        )
        result = pipeline.process_email(email)
        results.append(result)
        print(_render(email, result, audit, resolved))

        draft = None if dry_run else _save_draft(clients, email, result, resolved)
        if draft is not None:
            print(f"  saved to : {draft.folder}  (open Gmail → Drafts, review, then Send)")
        elif dry_run and result.outcome is Outcome.AWAITING_REVIEW:
            print("  saved to : nothing — --dry-run, no draft written")

    _summarise(results)
    return results


def _summarise(results: list[PipelineResult]) -> None:
    counts: dict[Outcome, int] = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    print(_rule("SUMMARY"))
    for outcome, count in sorted(counts.items(), key=lambda kv: kv[0].value):
        print(f"  {count:>3} x {_OUTCOME_LABEL.get(outcome, outcome.value)}")
    if any(r.outcome is Outcome.SENT for r in results):  # pragma: no cover - defensive
        print("  ⚠ Something reported SENT in a draft-only run — investigate immediately.")


def check_configuration(settings: Settings | None = None) -> int:
    """Verify configuration and connectivity without processing any mail."""

    resolved = settings or get_settings()
    print(_rule("CONFIGURATION"))
    print("  mode              : DRAFT ONLY — this runner never sends email")
    print(f"  rollout_phase     : {int(resolved.rollout_phase)} (ignored; draft-only is forced)")
    print(f"  mailbox           : {resolved.gmail_user or resolved.mailbox}")
    print(f"  gmail query       : {resolved.gmail_query}")

    if not resolved.google_sa_configured:
        print("  service account   : (unset)")
    else:
        source = resolved.google_sa_file or "(inline JSON)"
        # Print the facts an admin needs, so they can be pasted straight into a ticket.
        try:
            info = load_service_account_info(
                file_path=resolved.google_sa_file,
                inline_json=resolved.google_sa_json.get_secret_value(),
            )
            print(f"  service account   : {source}")
            print(f"  sa client_email   : {info.get('client_email', '?')}")
            print(f"  sa client_id      : {info.get('client_id', '?')}   <- admin needs this")
            print(f"  sa project_id     : {info.get('project_id', '?')}  <- enable Gmail API here")
            print(f"  scopes needed     : {', '.join(GMAIL_DRAFT_SCOPES)}")
        except PaymentBotError as exc:
            print(f"  service account   : x {source}: {exc}")

    print(f"  transport pro     : {resolved.tp_base_url or '(unset)'}")
    print(f"  groq model        : {resolved.groq_model}")
    print(
        "  gmail draft       : "
        + ("yes -> Gmail Drafts (drafts.create)" if resolved.gmail_create_draft else "no")
    )
    print(f"  reply cc          : {', '.join(resolved.reply_cc) or '(none)'}")

    problems = _missing_configuration(resolved)
    if problems:
        print(_rule("MISSING CONFIGURATION"))
        for problem in problems:
            print(f"  x {problem}")
        return 1

    print(_rule("CONNECTIVITY"))
    try:
        gmail = build_gmail_api_client(resolved)

        # Two stages, because "delegation is broken" and "the query matched nothing" are
        # very different problems and a single fetch would conflate them.
        profile = gmail.verify_access()
        print(
            f"  ok impersonation : acting as {profile.get('emailAddress', resolved.gmail_user)}"
            f" ({profile.get('messagesTotal', '?')} messages in the mailbox)"
        )
        fetched = gmail.fetch_new()
        print(f"  ok Gmail API     : {len(fetched)} message(s) match the query")
    except PaymentBotError as exc:
        # The auth and API layers already name the likely fix (enable the API, authorise
        # delegation, add a scope), so surface it verbatim.
        print(f"  x Gmail API      : {exc}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    # A Windows console defaults to cp1252, and drafts routinely carry characters outside
    # it (an em dash was enough). One unencodable character must degrade to '?' in the
    # report, not kill the run mid-inbox with drafts left unwritten.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(
        prog="payment-bot-local",
        description="Draft carrier replies locally from the paystatus inbox. Never sends email.",
    )
    parser.add_argument("--limit", type=int, default=None, help="max messages to process")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print drafts only: write nothing to Gmail Drafts and post nothing to Slack",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify configuration and Gmail connectivity, then exit",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG | INFO | WARNING | ERROR")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(args.log_level or settings.log_level)

    if args.check:
        return check_configuration(settings)

    problems = _missing_configuration(settings)
    if problems:
        print("Cannot start — missing configuration:")
        for problem in problems:
            print(f"  ✗ {problem}")
        print("\nRun `payment-bot-local --check` or see docs/LOCAL_RUN.md.")
        return 1

    try:
        results = process_inbox(settings, limit=args.limit, dry_run=args.dry_run)
    except PaymentBotError as exc:
        print(f"Run failed: {exc}", file=sys.stderr)
        return 1

    # Success means "we got through the mail", not "everything produced a draft" —
    # escalations are a legitimate, expected outcome.
    return 0 if results is not None else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
