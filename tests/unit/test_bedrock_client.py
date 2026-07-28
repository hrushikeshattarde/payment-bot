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
    assert req["system"] == [{"text": "be helpful"}]
    assert req["inferenceConfig"] == {"maxTokens": 256, "temperature": 0.0}
    # tool spec wrapping
    assert req["toolConfig"]["tools"][0]["toolSpec"]["name"] == "get_x"
    assert req["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"
    # content-block mapping
    contents = [m["content"] for m in req["messages"]]
    assert contents[0][0] == {"text": "hi"}
    assert contents[1][0]["toolUse"] == {"toolUseId": "tu-1", "name": "get_x", "input": {"a": 1}}
    assert contents[2][0]["toolResult"]["toolUseId"] == "tu-1"
    assert contents[2][0]["toolResult"]["status"] == "success"


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
    assert response.usage == {"inputTokens": 20, "outputTokens": 8}


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
