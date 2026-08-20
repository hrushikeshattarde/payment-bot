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

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from payment_bot.config import Settings, get_settings
from payment_bot.errors import ClientError

#: The neutral token counters every client reports, in the order a reader wants them.
#:
#: Providers disagree on spelling — Bedrock says ``inputTokens``, the OpenAI wire format says
#: ``prompt_tokens`` — so each client maps onto these names and nothing downstream has to know
#: which provider answered. That matters more than tidiness: these names are what the
#: CloudWatch metric filters in ``deploy/template.yaml`` match on, and a filter reads a field
#: by NAME. Rename one here and the metric silently stops recording rather than failing.
#:
#: All four are always present, zero-filled. A metric filter over an ABSENT field produces no
#: data point at all rather than a zero, so a run whose cache missed entirely would leave a
#: gap exactly where the spend spike is — the one shape worth alarming on.
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def empty_usage() -> dict[str, int]:
    """The four counters, all zero. The default for a response that reported none."""

    return dict.fromkeys(USAGE_KEYS, 0)


def normalise_usage(raw: Any, keys: Mapping[str, str]) -> dict[str, int]:
    """Map one provider's usage payload onto :data:`USAGE_KEYS`.

    ``keys`` is that provider's ``wire name -> neutral name`` table. Anything missing,
    non-numeric or unmapped is dropped rather than guessed at: a token count is only worth
    logging when the provider actually sent it, and a zero is the honest reading of absence.
    ``bool`` is excluded explicitly because it is an ``int`` subclass in Python and would
    otherwise arrive as a plausible-looking 0 or 1.
    """

    usage = empty_usage()
    if not isinstance(raw, Mapping):
        return usage
    for wire_key, neutral_key in keys.items():
        value = raw.get(wire_key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            usage[neutral_key] = int(value)
    return usage


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
    #: Opaque provider state that must be echoed back on the next request, or the model
    #: loses track of its own turn. Reasoning models are the reason this exists: they return
    #: a private chain of thought alongside their tool call, and dropping it makes them
    #: forget what they just did. Measured on nvidia/nemotron-3-super via OpenRouter —
    #: replaying a turn without it produced prose claiming a *different* tool had been
    #: called, then looped until the token budget ran out; replaying with it produced the
    #: next tool call. Never interpreted here, only carried.
    provider_state: dict[str, Any] | None = None


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
    #: Token counters under :data:`USAGE_KEYS`, always all four. This is the only per-turn
    #: record of what a run cost, so the agent loop and the id filter log it verbatim.
    usage: dict[str, int] = field(default_factory=empty_usage)
    #: Provider state to carry into the assistant message — see :class:`Message`.
    provider_state: dict[str, Any] | None = None

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


#: Bedrock Converse's ``usage`` spellings. ``cacheReadInputTokens`` is billed at ~10% of the
#: input rate and ``cacheWriteInputTokens`` at ~125%, and Converse reports both SEPARATELY
#: from ``inputTokens`` rather than folded into it — which is what makes a broken cache
#: visible as a jump in ``input_tokens`` rather than a silent price rise.
_BEDROCK_USAGE_KEYS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_tokens",
    "cacheWriteInputTokens": "cache_write_tokens",
}


def _cache_point() -> dict[str, Any]:
    """A Converse prompt-cache checkpoint, built fresh so requests share no mutable state.

    Everything before a checkpoint is cached for ~5 minutes and re-read at ~10% of the
    input price (the write costs +25%, once). Profitable for any loop of two or more
    turns, which the agent loop always is; a prefix under the model's minimum cacheable
    size is silently not cached, so a checkpoint is never worse than absent.
    """

    return {"cachePoint": {"type": "default"}}


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
        bedrock_messages: list[dict[str, Any]] = [
            {"role": m.role.value, "content": [_block_to_bedrock(b) for b in m.content]}
            for m in messages
        ]
        # Cache checkpoints, one per repeated surface. Tools and system are byte-identical
        # across every turn of every email in a run, and each turn's history is the
        # previous turn's plus one exchange — the rolling message checkpoints are what let
        # turn N re-read turns 1..N-1 (the bulky tool results) from cache instead of
        # re-billing them at full price.
        #
        # The last TWO user messages carry a checkpoint, not just the newest. The marker
        # is itself a content block, so it is part of the bytes the cache prefix-matches:
        # a prefix cached "…toolResult, cachePoint" only matches a later request that
        # still contains that cachePoint. Marking only the newest message removes the
        # previous marker each turn and every rolling lookup misses — measured live
        # (2026-08-19): reads collapsed to just the system+tools reuse, ~12% saved where
        # the shape supports ~70%. Two rolling markers + tools + system = the four the
        # request allows.
        marked = 0
        for message in reversed(bedrock_messages):
            if message["role"] == Role.USER.value:
                message["content"].append(_cache_point())
                marked += 1
                if marked == 2:
                    break
        request: dict[str, Any] = {
            "modelId": self._model_id,
            "messages": bedrock_messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system:
            request["system"] = [{"text": system}, _cache_point()]
        if tools:
            request["toolConfig"] = {
                "tools": [
                    *(
                        {
                            "toolSpec": {
                                "name": t.name,
                                "description": t.description,
                                "inputSchema": {"json": t.input_schema},
                            }
                        }
                        for t in tools
                    ),
                    _cache_point(),
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
            usage=normalise_usage(response.get("usage"), _BEDROCK_USAGE_KEYS),
        )


def build_bedrock_client(
    settings: Settings | None = None,
    client: Any | None = None,
) -> BedrockLlmClient:
    """Build a :class:`BedrockLlmClient` from ``PAYBOT_MODEL_DRAFT`` / ``PAYBOT_AWS_REGION``.

    The counterpart to :func:`~payment_bot.clients.llm_groq.build_groq_client`, so the two
    providers are chosen the same way and the Lambda handler reads like the local runner.

    ``aws_profile`` is honoured for the same reason it exists at all: boto3 reads the
    *process* environment and never ``.env``, so a workstation run against Bedrock would
    otherwise fail to find credentials that work fine from the shell. It must stay blank in
    Lambda, where the execution role is the credential source — naming a profile that does
    not exist there would break the call outright.
    """

    resolved = settings or get_settings()
    if client is None and resolved.aws_profile:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ClientError(
                "boto3 is required for BedrockLlmClient. Install with the 'aws' extra: "
                "pip install -e '.[aws]'"
            ) from exc
        session = boto3.Session(profile_name=resolved.aws_profile)
        client = session.client("bedrock-runtime", region_name=resolved.aws_region)
    return BedrockLlmClient(
        model_id=resolved.model_draft,
        region=resolved.aws_region,
        client=client,
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
