"""Groq LLM client for the portable tool-use loop.

Groq serves an **OpenAI-compatible** chat-completions API, so this module is a translation
layer between our provider-neutral message model (:mod:`payment_bot.clients.llm`) and that
wire format. Because :class:`~payment_bot.clients.llm.LlmClient` is a protocol, dropping
this in place of ``BedrockLlmClient`` changes nothing else: the same agent loop, the same
tools, the same deterministic pre-send gate.

The mapping that needs care is **tool results**. Our model carries them as
:class:`~payment_bot.clients.llm.ToolResultBlock`s inside a *user* message (the Bedrock
Converse shape); OpenAI-compatible APIs instead want one message per result with
``role: "tool"`` and a ``tool_call_id``. So a single neutral message can fan out into
several wire messages — see :func:`_to_wire_messages`.

Reliability notes for this provider:

* Open-weight models are less consistent than Claude at following a long mandatory tool
  sequence. That is *safe* here but not free: a skipped ``compute_scheduled_pay_date``
  means a hallucinated date, which the pre-send gate blocks (§5) and escalates. Watch the
  gate-block rate, and prefer a model with strong tool-calling support.
* ``max_tokens`` covers the whole completion. Too low truncates a reply mid-sentence, so it
  is configurable (``PAYBOT_AGENT_MAX_TOKENS``) rather than hard-coded.
* 429 / 5xx get a bounded exponential backoff; everything else fails fast as a
  :class:`ClientError` so the pipeline escalates rather than sending something half-formed.
"""

from __future__ import annotations

import json
import re
from typing import Any

from payment_bot.clients.http import (
    HttpTransport,
    SleepFn,
    UrllibTransport,
    default_sleep,
)
from payment_bot.clients.llm import (
    ContentBlock,
    LlmResponse,
    Message,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from payment_bot.config import Settings, get_settings
from payment_bot.errors import ClientError
from payment_bot.logging import get_logger

_log = get_logger("clients.groq")

DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"

#: Groq's catalogue moves; verify with `GET /openai/v1/models`. This default is chosen for
#: tool-calling reliability, which is what this agent depends on.
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Groq states the exact wait in the 429 body: "Please try again in 28.645s".
_RETRY_AFTER_RE = re.compile(r"try again in\s*([\d.]+)\s*s", re.IGNORECASE)

#: Ceiling on one wait, however long Groq asks for. A rate window is a minute, so anything
#: beyond this means something other than ordinary throttling and escalating is better than
#: hanging.
_MAX_RETRY_SLEEP_SECONDS = 65.0


def _retry_delay(status: int, body: str, attempt: int) -> float:
    """Seconds to wait before the next attempt.

    Groq's token limits are per *minute*, so exponential backoff starting at 1s cannot clear
    one. Measured on live mail: three consecutive runs were told to wait 28s, 19s and 19s,
    waited 1s then 2s, and escalated with no draft. Every email that reached the agent failed
    this way — not on any safety check.

    So when Groq names the wait, honour it. Anything else keeps the old backoff.
    """

    if status == 429:
        found = _RETRY_AFTER_RE.search(body)
        if found is not None:
            # A little over what was asked, so the window has definitely rolled over.
            return min(float(found.group(1)) + 0.5, _MAX_RETRY_SLEEP_SECONDS)
    # 2.0** rather than 1.0 * (2**…): int.__pow__ is typed as returning Any.
    return 2.0**attempt


#: finish_reason → our neutral stop_reason
_STOP_REASONS = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "content_filtered",
}


# ---------------------------------------------------------------------------
# Neutral model → OpenAI-compatible wire format
# ---------------------------------------------------------------------------
def _to_wire_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Flatten neutral messages into OpenAI-compatible chat messages.

    A user message holding tool results becomes one ``role: "tool"`` message per result;
    an assistant message holding tool uses becomes one message with a ``tool_calls`` array.
    """

    wire: list[dict[str, Any]] = []
    for message in messages:
        texts = [b.text for b in message.content if isinstance(b, TextBlock)]
        tool_uses = [b for b in message.content if isinstance(b, ToolUseBlock)]
        tool_results = [b for b in message.content if isinstance(b, ToolResultBlock)]

        if message.role is Role.ASSISTANT:
            entry: dict[str, Any] = {"role": "assistant"}
            # The API requires the key even when the turn was pure tool calls.
            entry["content"] = "\n".join(texts) if texts else None
            # Echo back whatever the provider asked us to carry (reasoning models need
            # their own chain of thought or they forget the turn — see Message).
            if message.provider_state:
                entry.update(message.provider_state)
            if tool_uses:
                entry["tool_calls"] = [
                    {
                        "id": call.tool_use_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.input),
                        },
                    }
                    for call in tool_uses
                ]
            wire.append(entry)
            continue

        # Role.USER — tool results must each become their own `tool` message, and must
        # come before any free text so they stay adjacent to the assistant tool_calls.
        for result in tool_results:
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_use_id,
                    "content": json.dumps(result.content),
                }
            )
        if texts:
            wire.append({"role": "user", "content": "\n".join(texts)})
    return wire


#: Assistant-message fields a provider expects echoed back verbatim on the next request.
#:
#: Reasoning models return a private chain of thought next to their tool call. It is not
#: content and we never read it, but it has to go back or the model forgets its own turn:
#: nvidia/nemotron-3-super via OpenRouter replied with prose naming a tool it had not called,
#: looped, and burned the whole token budget. With these fields restored it called the next
#: tool. Providers that send none of them are unaffected.
_CARRIED_ASSISTANT_FIELDS = ("reasoning_details", "reasoning", "reasoning_content")


def _provider_state(message: dict[str, Any]) -> dict[str, Any] | None:
    """Collect the fields that must be echoed back, or ``None`` if there are none."""

    carried = {
        key: message[key] for key in _CARRIED_ASSISTANT_FIELDS if message.get(key) is not None
    }
    return carried or None


def _to_wire_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def _from_wire_message(raw: dict[str, Any]) -> list[ContentBlock]:
    """Parse an assistant message back into neutral content blocks."""

    blocks: list[ContentBlock] = []
    content = raw.get("content")
    if isinstance(content, str) and content.strip():
        blocks.append(TextBlock(text=content))

    for call in raw.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        # `arguments` is a JSON *string*. If the model emits malformed JSON we pass an empty
        # input through: the registry's validation then returns an error envelope and the
        # loop feeds it back, letting the model correct itself instead of crashing the run.
        raw_args = function.get("arguments")
        parsed: dict[str, Any] = {}
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                candidate = json.loads(raw_args)
                if isinstance(candidate, dict):
                    parsed = candidate
            except ValueError:
                _log.warning("groq_tool_arguments_unparseable", extra={"tool": name})
        elif isinstance(raw_args, dict):
            parsed = raw_args

        blocks.append(
            ToolUseBlock(
                tool_use_id=str(call.get("id") or f"call-{name}"),
                name=str(name),
                input=parsed,
            )
        )
    return blocks


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class GroqLlmClient:
    """:class:`~payment_bot.clients.llm.LlmClient` backed by Groq chat completions.

    Args:
        api_key: Groq API key (``gsk_…``). Supply from the environment / a secret store.
        model: Groq model id. Must support tool calling.
        base_url: API root; override only for a proxy.
        transport: Injectable HTTP seam, so tests need no network.
        timeout: Per-request timeout in seconds.
        max_retries: Extra attempts for 429/5xx responses.
        sleep: Injectable sleep, so retry tests do not wait.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GROQ_MODEL,
        *,
        base_url: str = DEFAULT_GROQ_BASE_URL,
        transport: HttpTransport | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        sleep: SleepFn = default_sleep,
    ) -> None:
        if not api_key:
            raise ClientError("Groq api_key is required (set PAYBOT_GROQ_API_KEY)")
        self._api_key = api_key
        self._model = model
        self._base = base_url.rstrip("/")
        self._transport: HttpTransport = transport or UrllibTransport()
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self._model

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse:
        wire: list[dict[str, Any]] = []
        if system:
            wire.append({"role": "system", "content": system})
        wire.extend(_to_wire_messages(messages))

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": wire,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = _to_wire_tools(tools)
            payload["tool_choice"] = "auto"

        data = self._post_with_retries(payload)

        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ClientError("Groq returned no choices")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ClientError("Groq returned a choice with no message")

        usage_raw = data.get("usage") or {}
        usage = {
            key: int(value)
            for key, value in usage_raw.items()
            if isinstance(value, int | float) and not isinstance(value, bool)
        }

        finish = str(choice.get("finish_reason") or "stop")
        return LlmResponse(
            stop_reason=_STOP_REASONS.get(finish, finish),
            content=_from_wire_message(message),
            usage=usage,
            provider_state=_provider_state(message),
        )

    # -- internals -----------------------------------------------------------
    def _post_with_retries(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/chat/completions"
        body = json.dumps(payload).encode()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        last_status = 0
        last_body = ""
        for attempt in range(self._max_retries + 1):
            response = self._transport.request(
                "POST", url, headers=headers, body=body, timeout=self._timeout
            )
            if response.ok:
                data = response.json()
                if not isinstance(data, dict):
                    raise ClientError("Groq returned a non-object response")
                return data

            full_body = response.text()
            last_status, last_body = response.status, full_body[:400]
            if response.status not in _RETRY_STATUSES or attempt == self._max_retries:
                break
            # Parse the delay from the untruncated body — the wait Groq names sits past 250
            # characters in, so reading it off `last_body` would be luck rather than logic.
            delay = _retry_delay(response.status, full_body, attempt)
            _log.warning(
                "groq_retrying",
                extra={"status": response.status, "attempt": attempt + 1, "delay_s": delay},
            )
            self._sleep(delay)

        raise ClientError(f"Groq chat completion failed (HTTP {last_status}): {last_body}")


def build_groq_client(
    settings: Settings | None = None,
    transport: HttpTransport | None = None,
) -> GroqLlmClient:
    """Build a :class:`GroqLlmClient` from ``PAYBOT_GROQ_*`` configuration."""

    resolved = settings or get_settings()
    api_key = resolved.groq_api_key.get_secret_value()
    if not api_key:
        raise ClientError("Groq is not configured: set PAYBOT_GROQ_API_KEY")
    return GroqLlmClient(
        api_key=api_key,
        model=resolved.groq_model,
        base_url=resolved.groq_base_url,
        transport=transport,
        timeout=resolved.groq_timeout_seconds,
    )
