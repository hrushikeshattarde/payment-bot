"""External system adapters.

Every external dependency (Transport Pro, Gmail, Slack, the LLM) sits behind a
``Protocol`` with a mock/scripted implementation, so the whole pipeline runs and is
tested with no cloud access. Real HTTP/SDK implementations drop in behind the same
protocols once the PRD §9 dependencies land.
"""

from __future__ import annotations

from payment_bot.clients.gmail import GmailClient, MockGmailClient, SentMessage
from payment_bot.clients.llm import (
    BedrockLlmClient,
    ContentBlock,
    LlmClient,
    LlmResponse,
    Message,
    Role,
    ScriptedLlmClient,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from payment_bot.clients.slack import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalResolver,
    ApprovalSummary,
    AutoApproveResolver,
    MockSlackClient,
    ScriptedApprovalResolver,
    SlackClient,
    SlackPost,
)
from payment_bot.clients.transport_pro import (
    LoadFixture,
    MockTransportProClient,
    TransportProClient,
)

__all__ = [
    "ApprovalAction",
    "ApprovalDecision",
    "ApprovalResolver",
    "ApprovalSummary",
    "AutoApproveResolver",
    "BedrockLlmClient",
    "ContentBlock",
    "GmailClient",
    "LlmClient",
    "LlmResponse",
    "LoadFixture",
    "Message",
    "MockGmailClient",
    "MockSlackClient",
    "MockTransportProClient",
    "Role",
    "ScriptedApprovalResolver",
    "ScriptedLlmClient",
    "SentMessage",
    "SlackClient",
    "SlackPost",
    "TextBlock",
    "ToolResultBlock",
    "ToolSpec",
    "ToolUseBlock",
    "TransportProClient",
]
