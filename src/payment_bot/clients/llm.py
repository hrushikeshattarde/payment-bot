"""LLM client for the portable tool-use loop (Bedrock Converse, §8.1).

We own the agent loop rather than delegating to managed Bedrock Agents, so this module
defines a small, provider-neutral message model and a :class:`LlmClient` protocol over
it. Two implementations ship:

* :class:`BedrockLlmClient` — translates to/from the Bedrock Converse API. ``boto3`` is
  imported lazily so the core package installs and tests run without the AWS SDK.
* :class:`ScriptedLlmClient` — replays a fixed list of responses, letting integration
  tests drive an exact tool-use sequence with zero network and full determinism.

Neutral content blocks (text / tool-use / tool-result) map 1:1 onto Converse blocks but
keep the loop independent of any single vendor's wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from payment_bot.errors import ClientError


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


# --- Neutral content blocks -------------------------------------------------
@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: dict[str, Any]
    is_error: bool = False


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: list[ContentBlock]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool advertised to the model (§4.2 ``toolSpec``)."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """One model turn, provider-neutral."""

    stop_reason: str  # "tool_use" | "end_turn" | "max_tokens" | ...
    content: list[ContentBlock]
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(b.text for b in self.content if isinstance(b, TextBlock)).strip()

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]


@runtime_checkable
class LlmClient(Protocol):
    """Turn a conversation + tool catalogue into the next model turn."""

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse: ...


# ---------------------------------------------------------------------------
# Bedrock Converse implementation
# ---------------------------------------------------------------------------
def _block_to_bedrock(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "toolUse": {
                "toolUseId": block.tool_use_id,
                "name": block.name,
                "input": block.input,
            }
        }
    # ToolResultBlock
    return {
        "toolResult": {
            "toolUseId": block.tool_use_id,
            "content": [{"json": block.content}],
            "status": "error" if block.is_error else "success",
        }
    }


def _block_from_bedrock(raw: dict[str, Any]) -> ContentBlock | None:
    if "text" in raw:
        return TextBlock(text=raw["text"])
    if "toolUse" in raw:
        tu = raw["toolUse"]
        return ToolUseBlock(
            tool_use_id=tu["toolUseId"],
            name=tu["name"],
            input=dict(tu.get("input") or {}),
        )
    # Unknown/unsupported block types (e.g. reasoning) are dropped from our view.
    return None


class BedrockLlmClient:
    """:class:`LlmClient` backed by ``bedrock-runtime.converse``.

    Args:
        model_id: Bedrock model / inference-profile id.
        region: AWS region.
        client: Optional pre-built boto3 ``bedrock-runtime`` client. Injecting one keeps
            this class unit-testable without AWS; when omitted, boto3 is imported lazily.
    """

    def __init__(
        self,
        model_id: str,
        region: str = "us-east-1",
        client: Any | None = None,
    ) -> None:
        self._model_id = model_id
        self._region = region
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3  # lazy import keeps boto3 an optional extra
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ClientError(
                "boto3 is required for BedrockLlmClient. Install with the 'aws' extra: "
                "pip install -e '.[aws]'"
            ) from exc
        self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse:
        request: dict[str, Any] = {
            "modelId": self._model_id,
            "messages": [
                {"role": m.role.value, "content": [_block_to_bedrock(b) for b in m.content]}
                for m in messages
            ],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system:
            request["system"] = [{"text": system}]
        if tools:
            request["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": {"json": t.input_schema},
                        }
                    }
                    for t in tools
                ]
            }

        try:
            response = self._ensure_client().converse(**request)
        except ClientError:
            raise
        except Exception as exc:  # normalise any SDK/transport error into ClientError
            raise ClientError(f"Bedrock converse failed: {exc}") from exc

        message = response.get("output", {}).get("message", {})
        blocks: list[ContentBlock] = []
        for raw in message.get("content", []):
            parsed = _block_from_bedrock(raw)
            if parsed is not None:
                blocks.append(parsed)

        return LlmResponse(
            stop_reason=response.get("stopReason", "end_turn"),
            content=blocks,
            usage=response.get("usage", {}) or {},
        )


# ---------------------------------------------------------------------------
# Scripted implementation (tests / demo)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ScriptedLlmClient:
    """Replays a fixed queue of :class:`LlmResponse` objects, in order.

    Records each ``converse`` call so tests can assert what the loop sent. Raises if the
    loop asks for more turns than were scripted — a scripting bug should fail loudly.
    """

    responses: list[LlmResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)
    _index: int = 0

    def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse:
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if self._index >= len(self.responses):
            raise ClientError(
                f"ScriptedLlmClient exhausted after {len(self.responses)} responses"
            )
        response = self.responses[self._index]
        self._index += 1
        return response
