"""External system adapters.

Every external dependency sits behind a ``Protocol`` with a mock implementation, so the
whole pipeline runs and is tested with no network access.

One implementation per concern, deliberately:

===============  =====================================  ==============================
Concern          Live implementation                    Mock / test double
===============  =====================================  ==============================
Transport Pro    :class:`TransportProHttpClient`        :class:`MockTransportProClient`
Gmail            :class:`GmailApiClient` (service acct) :class:`MockGmailClient`
LLM (local)      :class:`GroqLlmClient`                 :class:`ScriptedLlmClient`
LLM (deployed)   :class:`BedrockLlmClient`              :class:`ScriptedLlmClient`
Slack            *(seam only — see below)*              :class:`MockSlackClient`,
                                                        :class:`NullSlackClient`
===============  =====================================  ==============================

Slack keeps its protocol because the pipeline posts approvals and escalations through it,
and the AWS Phase 1 design (§8.5) depends on that seam. Locally there is no Slack client:
:class:`NullSlackClient` logs instead, and drafts go to the Gmail Drafts folder for review.
"""

from __future__ import annotations

from payment_bot.clients.gmail import (
    DraftingGmailClient,
    DraftMessage,
    GmailClient,
    MockGmailClient,
    SentMessage,
)
from payment_bot.clients.gmail_api import (
    GmailApiClient,
    SendingDisabledError,
    build_gmail_api_client,
)
from payment_bot.clients.google_auth import (
    GMAIL_DRAFT_SCOPES,
    GMAIL_READONLY_SCOPES,
    ServiceAccountTokenSource,
    load_service_account_info,
)
from payment_bot.clients.http import HttpResponse, HttpTransport, UrllibTransport
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
from payment_bot.clients.llm_groq import (
    DEFAULT_GROQ_MODEL,
    GroqLlmClient,
    build_groq_client,
)
from payment_bot.clients.mime import reply_subject
from payment_bot.clients.slack import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalResolver,
    ApprovalSummary,
    AutoApproveResolver,
    DeferredApprovalResolver,
    MockSlackClient,
    NullSlackClient,
    ScriptedApprovalResolver,
    SlackClient,
    SlackPost,
)
from payment_bot.clients.transport_pro import (
    LoadFixture,
    MockTransportProClient,
    TransportProClient,
)
from payment_bot.clients.transport_pro_http import (
    TransportProHttpClient,
    TransportProSettings,
    build_transport_pro_client,
)

__all__ = [
    "DEFAULT_GROQ_MODEL",
    "GMAIL_DRAFT_SCOPES",
    "GMAIL_READONLY_SCOPES",
    "ApprovalAction",
    "ApprovalDecision",
    "ApprovalResolver",
    "ApprovalSummary",
    "AutoApproveResolver",
    "BedrockLlmClient",
    "ContentBlock",
    "DeferredApprovalResolver",
    "DraftMessage",
    "DraftingGmailClient",
    "GmailApiClient",
    "GmailClient",
    "GroqLlmClient",
    "HttpResponse",
    "HttpTransport",
    "LlmClient",
    "LlmResponse",
    "LoadFixture",
    "Message",
    "MockGmailClient",
    "MockSlackClient",
    "MockTransportProClient",
    "NullSlackClient",
    "Role",
    "ScriptedApprovalResolver",
    "ScriptedLlmClient",
    "SendingDisabledError",
    "SentMessage",
    "ServiceAccountTokenSource",
    "SlackClient",
    "SlackPost",
    "TextBlock",
    "ToolResultBlock",
    "ToolSpec",
    "ToolUseBlock",
    "TransportProClient",
    "TransportProHttpClient",
    "TransportProSettings",
    "UrllibTransport",
    "build_gmail_api_client",
    "build_groq_client",
    "build_transport_pro_client",
    "load_service_account_info",
    "reply_subject",
]
