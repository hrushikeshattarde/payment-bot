"""Slack client + human-approval resolver (§4.6, §8.5).

Two responsibilities, deliberately separated:

* :class:`SlackClient` — *posting* approval requests and escalations. Side-effecting
  transport only.
* :class:`ApprovalResolver` — *obtaining the human's decision* (Approve / Edit / Reject).
  In production the decision arrives asynchronously via ``slack_handle_interaction``
  (§4.6); for a synchronous slice we model it behind this protocol so tests can drive
  approve/edit/reject deterministically and the local demo can auto-approve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ApprovalSummary:
    """The at-a-glance context posted alongside a draft (§4.6 ``summary``)."""

    from_: str
    intents: tuple[str, ...]
    load_ids: tuple[str, ...]
    key_facts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SlackPost:
    """Handle to a posted Slack message."""

    slack_ts: str
    channel: str


class ApprovalAction(StrEnum):
    """Block Kit button outcomes (§4.6)."""

    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """A resolved human decision. ``edited_text`` is set only for EDIT."""

    action: ApprovalAction
    edited_text: str | None = None


@runtime_checkable
class SlackClient(Protocol):
    """Posts approval requests and escalations to Slack."""

    def post_approval(
        self,
        channel: str,
        summary: ApprovalSummary,
        draft_reply: str,
        correlation_id: str,
    ) -> SlackPost:
        """Post a draft for Approve/Edit/Reject (§4.6 ``slack_post_approval``)."""

    def post_escalation(
        self,
        channel: str,
        severity: str,
        reason: str,
        load_ids: tuple[str, ...],
        correlation_id: str,
    ) -> SlackPost:
        """Post an escalation (§4.6 ``slack_post_escalation``)."""


@runtime_checkable
class ApprovalResolver(Protocol):
    """Yields the human's decision for a posted approval request."""

    def resolve(self, correlation_id: str, draft_reply: str) -> ApprovalDecision: ...


@dataclass(slots=True)
class MockSlackClient:
    """Records every approval/escalation post for assertions."""

    approvals: list[dict[str, object]] = field(default_factory=list)
    escalations: list[dict[str, object]] = field(default_factory=list)
    _counter: int = 0

    def post_approval(
        self,
        channel: str,
        summary: ApprovalSummary,
        draft_reply: str,
        correlation_id: str,
    ) -> SlackPost:
        self._counter += 1
        self.approvals.append(
            {
                "channel": channel,
                "summary": summary,
                "draft_reply": draft_reply,
                "correlation_id": correlation_id,
            }
        )
        return SlackPost(slack_ts=f"appr-{self._counter}", channel=channel)

    def post_escalation(
        self,
        channel: str,
        severity: str,
        reason: str,
        load_ids: tuple[str, ...],
        correlation_id: str,
    ) -> SlackPost:
        self._counter += 1
        self.escalations.append(
            {
                "channel": channel,
                "severity": severity,
                "reason": reason,
                "load_ids": load_ids,
                "correlation_id": correlation_id,
            }
        )
        return SlackPost(slack_ts=f"esc-{self._counter}", channel=channel)


class AutoApproveResolver:
    """Always approves — used by the local demo only. Never wire this into production."""

    def resolve(self, correlation_id: str, draft_reply: str) -> ApprovalDecision:
        return ApprovalDecision(action=ApprovalAction.APPROVE)


@dataclass(slots=True)
class ScriptedApprovalResolver:
    """Returns a preset decision. Tests use this to exercise approve/edit/reject."""

    decision: ApprovalDecision

    def resolve(self, correlation_id: str, draft_reply: str) -> ApprovalDecision:
        return self.decision
