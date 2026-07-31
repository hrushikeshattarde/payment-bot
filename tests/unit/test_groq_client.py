"""Unit tests for the Groq chat-completions translation layer.

The interesting part is the message mapping: our neutral model carries tool results inside a
*user* message (the Bedrock shape), while OpenAI-compatible APIs want one ``role: "tool"``
message per result. A fake transport records the exact request body.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr

from payment_bot.clients.http import HttpResponse
from payment_bot.clients.llm import (
    Message,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from payment_bot.clients.llm_groq import GroqLlmClient, build_groq_client
from payment_bot.config import Settings
from payment_bot.errors import ClientError


class FakeGroq:
    """Returns queued responses and records every request body."""

    def __init__(self, *responses: tuple[int, dict[str, Any]]) -> None:
        self.responses = list(responses) or [(200, _text_completion("ok"))]
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        self.headers.append(headers)
        self.requests.append(json.loads(body or b"{}"))
        status, payload = self.responses.pop(0) if self.responses else (200, _text_completion("ok"))
        return HttpResponse(status, json.dumps(payload).encode())


def _text_completion(text: str, finish: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }


def _tool_completion(name: str, args: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
    }


def _client(transport: FakeGroq, **kwargs: Any) -> GroqLlmClient:
    return GroqLlmClient(api_key="gsk_test", transport=transport, sleep=lambda _s: None, **kwargs)


# --- request shape ----------------------------------------------------------
@pytest.mark.unit
def test_system_prompt_and_tools_are_sent() -> None:
    transport = FakeGroq()
    _client(transport, model="test-model").converse(
        system="be careful",
        messages=[Message(Role.USER, [TextBlock("handle load 2462934")])],
        tools=[ToolSpec("tp_get_load_summary", "reads a load", {"type": "object"})],
        max_tokens=2048,
        temperature=0.0,
    )

    body = transport.requests[0]
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 2048
    assert body["temperature"] == 0.0
    assert body["messages"][0] == {"role": "system", "content": "be careful"}
    assert body["messages"][1] == {"role": "user", "content": "handle load 2462934"}
    tool = body["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "tp_get_load_summary"
    assert tool["function"]["parameters"] == {"type": "object"}
    assert body["tool_choice"] == "auto"
    assert transport.headers[0]["Authorization"] == "Bearer gsk_test"


@pytest.mark.unit
def test_tool_results_become_role_tool_messages() -> None:
    """The critical mapping: a user message of tool results fans out to `role: tool`."""

    transport = FakeGroq()
    _client(transport).converse(
        system="",
        messages=[
            Message(Role.USER, [TextBlock("go")]),
            Message(Role.ASSISTANT, [ToolUseBlock("call_7", "tp_get_load_summary", {"load_id": "2462934"})]),
            Message(
                Role.USER,
                [
                    ToolResultBlock("call_7", {"ok": True, "load_id": "2462934"}),
                    ToolResultBlock("call_8", {"ok": False, "error": "boom"}, is_error=True),
                ],
            ),
        ],
        tools=[],
    )

    messages = transport.requests[0]["messages"]
    assert messages[0] == {"role": "user", "content": "go"}

    assistant = messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["id"] == "call_7"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"load_id": "2462934"}

    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call_7"
    assert json.loads(messages[2]["content"]) == {"ok": True, "load_id": "2462934"}
    assert messages[3]["tool_call_id"] == "call_8"


@pytest.mark.unit
def test_no_tools_means_no_tool_choice_key() -> None:
    transport = FakeGroq()
    _client(transport).converse(system="", messages=[Message(Role.USER, [TextBlock("hi")])], tools=[])
    assert "tools" not in transport.requests[0]
    assert "tool_choice" not in transport.requests[0]


# --- response parsing -------------------------------------------------------
@pytest.mark.unit
def test_tool_call_response_is_parsed() -> None:
    transport = FakeGroq((200, _tool_completion("compute_carrier_rate", {"load_id": "2462934"}, "call_9")))
    response = _client(transport).converse(system="", messages=[], tools=[])

    assert response.stop_reason == "tool_use"
    assert len(response.tool_uses) == 1
    call = response.tool_uses[0]
    assert call.tool_use_id == "call_9"
    assert call.name == "compute_carrier_rate"
    assert call.input == {"load_id": "2462934"}
    assert response.usage == {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}


@pytest.mark.unit
def test_text_response_maps_to_end_turn() -> None:
    transport = FakeGroq((200, _text_completion("I need more information.")))
    response = _client(transport).converse(system="", messages=[], tools=[])

    assert response.stop_reason == "end_turn"
    assert response.tool_uses == []
    assert response.text == "I need more information."


@pytest.mark.unit
def test_truncated_response_maps_to_max_tokens() -> None:
    transport = FakeGroq((200, _text_completion("half a sen", finish="length")))
    assert _client(transport).converse(system="", messages=[], tools=[]).stop_reason == "max_tokens"


@pytest.mark.unit
def test_malformed_tool_arguments_yield_empty_input_for_self_correction() -> None:
    """Bad JSON must not crash the run — the registry rejects it and the loop retries."""

    broken = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_x",
                            "type": "function",
                            "function": {"name": "tp_get_load_summary", "arguments": "{not json"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    response = _client(FakeGroq((200, broken))).converse(system="", messages=[], tools=[])

    assert len(response.tool_uses) == 1
    assert response.tool_uses[0].input == {}


@pytest.mark.unit
def test_text_and_tool_call_in_one_turn_are_both_kept() -> None:
    mixed = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Let me look that up.",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "tp_get_load_summary", "arguments": '{"load_id":"2462934"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    response = _client(FakeGroq((200, mixed))).converse(system="", messages=[], tools=[])

    assert response.text == "Let me look that up."
    assert response.tool_uses[0].name == "tp_get_load_summary"


# --- failures and retries ---------------------------------------------------
@pytest.mark.unit
def test_rate_limit_is_retried_then_succeeds() -> None:
    transport = FakeGroq((429, {"error": "slow down"}), (200, _text_completion("done")))
    response = _client(transport).converse(system="", messages=[], tools=[])

    assert response.text == "done"
    assert len(transport.requests) == 2


#: The real body Groq returns, trimmed only of the org id. The wait sits ~250 characters in,
#: which is why the delay is parsed from the untruncated body.
_RATE_LIMIT_BODY = {
    "error": {
        "message": (
            "Rate limit reached for model `llama-3.3-70b-versatile` in organization "
            "`org_test` service tier `on_demand` on tokens per minute (TPM): Limit 12000, "
            "Used 9383, Requested 8346. Please try again in 28.645s. Need more tokens? "
            "Upgrade to Dev Tier today at https://console.groq.com/settings/billing"
        ),
        "type": "tokens",
        "code": "rate_limit_exceeded",
    }
}


@pytest.mark.unit
def test_rate_limit_wait_honours_the_delay_groq_asks_for() -> None:
    """Groq's window is per minute; 1s of backoff cannot clear it.

    Every live email that reached the agent failed this way — told to wait ~28s, waited 1s,
    escalated with no draft.
    """

    slept: list[float] = []
    transport = FakeGroq((429, _RATE_LIMIT_BODY), (200, _text_completion("done")))
    client = GroqLlmClient(api_key="gsk_test", transport=transport, sleep=slept.append)

    assert client.converse(system="", messages=[], tools=[]).text == "done"
    assert slept == [pytest.approx(29.145)]  # 28.645 + a margin so the window has rolled


@pytest.mark.unit
def test_absurd_retry_delay_is_capped() -> None:
    """A wait far beyond one rate window means something else is wrong; do not hang."""

    slept: list[float] = []
    body = {"error": {"message": "Please try again in 8000s."}}
    transport = FakeGroq((429, body), (200, _text_completion("done")))
    client = GroqLlmClient(api_key="gsk_test", transport=transport, sleep=slept.append)

    client.converse(system="", messages=[], tools=[])
    assert slept == [65.0]


@pytest.mark.unit
def test_rate_limit_without_a_stated_delay_falls_back_to_backoff() -> None:
    slept: list[float] = []
    transport = FakeGroq((429, {"error": "slow down"}), (200, _text_completion("done")))
    client = GroqLlmClient(api_key="gsk_test", transport=transport, sleep=slept.append)

    client.converse(system="", messages=[], tools=[])
    assert slept == [1.0]


@pytest.mark.unit
def test_server_errors_still_use_exponential_backoff() -> None:
    """Only 429 carries a stated wait. 5xx keeps the original 1s, 2s escalation."""

    slept: list[float] = []
    transport = FakeGroq(
        (503, {"error": "unavailable"}),
        (503, {"error": "unavailable"}),
        (200, _text_completion("done")),
    )
    client = GroqLlmClient(api_key="gsk_test", transport=transport, sleep=slept.append)

    client.converse(system="", messages=[], tools=[])
    assert slept == [1.0, 2.0]


@pytest.mark.unit
def test_persistent_server_error_becomes_a_client_error() -> None:
    transport = FakeGroq(
        (503, {"error": "unavailable"}),
        (503, {"error": "unavailable"}),
        (503, {"error": "unavailable"}),
    )
    with pytest.raises(ClientError, match="HTTP 503"):
        _client(transport).converse(system="", messages=[], tools=[])


@pytest.mark.unit
def test_bad_request_is_not_retried() -> None:
    transport = FakeGroq((400, {"error": {"message": "unsupported tool schema"}}))
    with pytest.raises(ClientError, match="unsupported tool schema"):
        _client(transport).converse(system="", messages=[], tools=[])
    assert len(transport.requests) == 1  # 400 is our bug, not a blip


@pytest.mark.unit
def test_empty_choices_is_an_error() -> None:
    with pytest.raises(ClientError, match="no choices"):
        _client(FakeGroq((200, {"choices": []}))).converse(system="", messages=[], tools=[])


@pytest.mark.unit
def test_missing_api_key_is_rejected_up_front() -> None:
    with pytest.raises(ClientError, match="api_key is required"):
        GroqLlmClient(api_key="")


# --- factory ----------------------------------------------------------------
@pytest.mark.unit
def test_factory_reads_settings() -> None:
    settings = Settings(groq_api_key=SecretStr("gsk_from_env"), groq_model="cfg-model")
    transport = FakeGroq()
    client = build_groq_client(settings, transport=transport)

    assert client.model == "cfg-model"
    client.converse(system="", messages=[], tools=[])
    assert transport.headers[0]["Authorization"] == "Bearer gsk_from_env"


@pytest.mark.unit
def test_factory_refuses_without_a_key() -> None:
    with pytest.raises(ClientError, match="not configured"):
        build_groq_client(Settings(groq_api_key=SecretStr("")))
