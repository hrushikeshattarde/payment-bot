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

from payment_bot.clients.cargotel import (
    CargoTelClient,
    CargoTelLoadFixture,
    MockCargoTelClient,
)
from payment_bot.clients.cargotel_html import is_login_page, parse_load_html
from payment_bot.clients.cargotel_http import (
    CargoTelHttpClient,
    CargoTelSettings,
    CookieSource,
    S3CookieSource,
    StaticCookieSource,
    build_cargotel_client,
)
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
    "CargoTelClient",
    "CargoTelHttpClient",
    "CargoTelLoadFixture",
    "CargoTelSettings",
    "ContentBlock",
    "CookieSource",
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
    "MockCargoTelClient",
    "MockGmailClient",
    "MockSlackClient",
    "MockTransportProClient",
    "NullSlackClient",
    "Role",
    "S3CookieSource",
    "ScriptedApprovalResolver",
    "ScriptedLlmClient",
    "SendingDisabledError",
    "SentMessage",
    "ServiceAccountTokenSource",
    "SlackClient",
    "SlackPost",
    "StaticCookieSource",
    "TextBlock",
    "ToolResultBlock",
    "ToolSpec",
    "ToolUseBlock",
    "TransportProClient",
    "TransportProHttpClient",
    "TransportProSettings",
    "UrllibTransport",
    "build_cargotel_client",
    "build_gmail_api_client",
    "build_groq_client",
    "build_transport_pro_client",
    "is_login_page",
    "load_service_account_info",
    "parse_load_html",
    "reply_subject",
]
