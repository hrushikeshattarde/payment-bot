"""Local demo entrypoint (``payment-bot-demo``).

Runs both skills — ``payment_status`` and ``rate_verification`` — end-to-end on the sample
emails for load 2462934, using the mock clients and a *scripted* model (no AWS, no network).
For each it prints the audited tool trail, the gate decision, the drafted reply, and the
final outcome, so you can watch the whole flow without wiring Bedrock.

Swap the scripted model for ``BedrockLlmClient(...)`` and the mock clients for the real
adapters to run it live.
"""

from __future__ import annotations

from payment_bot.clients import AutoApproveResolver, LlmClient, MockGmailClient, MockSlackClient
from payment_bot.config import get_settings
from payment_bot.logging import InMemoryAuditSink, configure_logging
from payment_bot.models import InboundEmail
from payment_bot.pipeline import Outcome, PaymentBotPipeline, PipelineResult
from payment_bot.sample_data import (
    sample_payment_status_email,
    sample_rate_verification_email,
    sample_transport_pro_client,
    scripted_payment_status_llm,
    scripted_rate_verification_llm,
)


def _rule(title: str) -> str:
    return f"\n{'─' * 4} {title} {'─' * max(4, 68 - len(title))}"


def _render(scenario: str, result: PipelineResult, audit: InMemoryAuditSink) -> str:
    lines = [f"\n=== Payments Email Bot — {scenario} demo (mock clients + scripted model) ==="]

    lines.append(_rule("AUDITED TOOL TRAIL (§8.1)"))
    for entry in audit.for_correlation(result.correlation_id):
        status = "ok " if entry.ok else "ERR"
        lines.append(f"  [{status}] {entry.tool_name:<28} ({entry.duration_ms:5.1f} ms)")

    lines.append(_rule("PRE-SEND GATE (§5)"))
    if result.gate_result is not None:
        for check in result.gate_result.checks:
            mark = "PASS" if check.passed else "FAIL"
            lines.append(f"  [{mark}] {check.name:<18} {check.detail}")
    else:
        lines.append("  (not reached)")

    lines.append(_rule("DRAFTED REPLY"))
    if result.draft is not None:
        for line in result.draft.reply_body.splitlines():
            lines.append(f"  | {line}")

    lines.append(_rule("OUTCOME"))
    lines.append(f"  outcome : {result.outcome.value.upper()}")
    lines.append(f"  detail  : {result.detail}")
    if result.sent_message is not None:
        lines.append(f"  sent-id : {result.sent_message.sent_message_id} → {result.sent_message.to}")
    return "\n".join(lines)


def _run_scenario(scenario: str, email: InboundEmail, llm: LlmClient) -> PipelineResult:
    audit = InMemoryAuditSink()
    pipeline = PaymentBotPipeline(
        tp=sample_transport_pro_client(),
        gmail=MockGmailClient(),
        slack=MockSlackClient(),
        llm=llm,
        approval_resolver=AutoApproveResolver(),
        settings=get_settings(),
        audit_sink=audit,
    )
    result = pipeline.process_email(email)
    print(_render(scenario, result, audit))
    return result


def run_demo() -> PipelineResult:
    """Run the payment_status scenario (kept as the primary demo)."""

    return _run_scenario(
        "payment_status", sample_payment_status_email(), scripted_payment_status_llm()
    )


def run_rate_demo() -> PipelineResult:
    """Run the rate_verification scenario."""

    return _run_scenario(
        "rate_verification", sample_rate_verification_email(), scripted_rate_verification_llm()
    )


def main() -> int:
    configure_logging("ERROR")  # keep the demo's stdout report clean
    results = [run_demo(), run_rate_demo()]
    return 0 if all(r.outcome is Outcome.SENT for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
