"""Unit tests for the Bedrock Converse request/response mapping.

A fake ``bedrock-runtime`` client is injected so we test our translation layer — request
shape and response parsing — without boto3 or AWS.
"""

from __future__ import annotations

from typing import Any

import pytest

from payment_bot.clients.llm import (
    BedrockLlmClient,
    Message,
    Role,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)


class _FakeBedrock:
    """Records the converse request and returns a canned tool-use response."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.last_request: dict[str, Any] | None = None

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.last_request = kwargs
        return self.response


@pytest.mark.unit
def test_request_is_shaped_for_converse() -> None:
    fake = _FakeBedrock(
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
    )
    client = BedrockLlmClient(model_id="test-model", client=fake)

    client.converse(
        system="be helpful",
        messages=[
            Message(Role.USER, [TextBlock("hi")]),
            Message(Role.ASSISTANT, [ToolUseBlock("tu-1", "get_x", {"a": 1})]),
            Message(Role.USER, [ToolResultBlock("tu-1", {"ok": True})]),
        ],
        tools=[ToolSpec("get_x", "gets x", {"type": "object", "properties": {}})],
        max_tokens=256,
        temperature=0.0,
    )

    req = fake.last_request
    assert req is not None
    assert req["modelId"] == "test-model"
    assert req["system"] == [{"text": "be helpful"}, {"cachePoint": {"type": "default"}}]
    assert req["inferenceConfig"] == {"maxTokens": 256, "temperature": 0.0}
    # tool spec wrapping, with the cache checkpoint as the catalogue's final entry
    assert req["toolConfig"]["tools"][0]["toolSpec"]["name"] == "get_x"
    assert req["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"
    assert req["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}
    # content-block mapping
    contents = [m["content"] for m in req["messages"]]
    assert contents[0][0] == {"text": "hi"}
    assert contents[1][0]["toolUse"] == {"toolUseId": "tu-1", "name": "get_x", "input": {"a": 1}}
    assert contents[2][0]["toolResult"]["toolUseId"] == "tu-1"
    assert contents[2][0]["toolResult"]["status"] == "success"
    # The last TWO user messages carry rolling checkpoints — the marker is a content
    # block, so the previous request's marker must still be present or its cached prefix
    # never byte-matches again (measured live: rolling reads collapsed to zero when only
    # the newest message was marked). The assistant turn between them carries none, and
    # older history must not either, or four-checkpoint budget (tools + system + 2)
    # would be exhausted.
    assert contents[0][-1] == {"cachePoint": {"type": "default"}}
    assert contents[2][-1] == {"cachePoint": {"type": "default"}}
    assert {"cachePoint": {"type": "default"}} not in contents[1]


@pytest.mark.unit
def test_tool_use_response_is_parsed() -> None:
    fake = _FakeBedrock(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"text": "let me look"},
                        {"toolUse": {"toolUseId": "tu-9", "name": "tp_get_load_summary", "input": {"load_id": "2462934"}}},
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 20, "outputTokens": 8},
        }
    )
    client = BedrockLlmClient(model_id="test-model", client=fake)

    response = client.converse(system="", messages=[Message(Role.USER, [TextBlock("go")])], tools=[])

    assert response.stop_reason == "tool_use"
    assert response.text == "let me look"
    assert len(response.tool_uses) == 1
    call = response.tool_uses[0]
    assert call.name == "tp_get_load_summary"
    assert call.input == {"load_id": "2462934"}
    # Neutral names, not Converse's — see USAGE_KEYS. A cache-free turn still reports both
    # cache counters, as zeroes, because the metric filters match on field name.
    assert response.usage == {
        "input_tokens": 20,
        "output_tokens": 8,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


@pytest.mark.unit
def test_cache_usage_fields_pass_through() -> None:
    """Converse reports cache reads/writes in usage; the mapping must not drop them —
    they are the only signal that caching is actually working in production logs.

    They reach those logs under the neutral names, which is what the ``llm_usage`` line in
    the agent loop spreads and what a metric filter can therefore find.
    """

    fake = _FakeBedrock(
        {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 900,
                "outputTokens": 40,
                "cacheReadInputTokens": 11800,
                "cacheWriteInputTokens": 700,
            },
        }
    )
    client = BedrockLlmClient(model_id="m", client=fake)
    response = client.converse(system="s", messages=[Message(Role.USER, [TextBlock("go")])], tools=[])
    assert response.usage["cache_read_tokens"] == 11800
    assert response.usage["cache_write_tokens"] == 700
    assert response.usage["input_tokens"] == 900
    assert response.usage["output_tokens"] == 40


@pytest.mark.unit
def test_error_result_block_maps_to_error_status() -> None:
    fake = _FakeBedrock(
        {"output": {"message": {"content": []}}, "stopReason": "end_turn"}
    )
    client = BedrockLlmClient(model_id="m", client=fake)
    client.converse(
        system="",
        messages=[Message(Role.USER, [ToolResultBlock("tu-1", {"ok": False}, is_error=True)])],
        tools=[],
    )
    assert fake.last_request is not None
    block = fake.last_request["messages"][0]["content"][0]
    assert block["toolResult"]["status"] == "error"
