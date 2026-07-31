"""The portable tool-use loop (PRD §8.1).

We own this loop rather than delegating to managed Bedrock Agents, so the control flow —
which tools are exposed, how results feed back, when the turn ends — is explicit, testable
Python. The loop is model-agnostic: it drives any :class:`LlmClient`, so the same code
runs against Bedrock in production and a :class:`ScriptedLlmClient` in tests.

Design choices that matter:

* **Skill boundary is enforced here.** Only ``allowed_tools`` are advertised *and* accepted;
  a call to anything else returns an error result rather than silently executing.
* **``submit_draft`` is terminal.** A successful submit ends the loop and yields the draft.
  A *failed* submit (bad input) is fed back so the model can correct itself.
* **Bounded.** The loop never exceeds ``max_iterations`` — a runaway model cannot spin.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from payment_bot.clients.llm import (
    LlmClient,
    Message,
    Role,
    TextBlock,
    ToolResultBlock,
)
from payment_bot.logging import get_logger
from payment_bot.tools.base import ToolContext, ToolRegistry
from payment_bot.tools.submit import SubmitDraftOutput

_log = get_logger("agent")

#: Sent when the model answers in prose instead of calling the terminal tool.
#:
#: Observed repeatedly on live mail: the agent runs the whole procedure correctly — fourteen
#: clean tool calls — then writes its answer as text and stops. The work is done; it is
#: delivered to the wrong place. The reply must arrive through `submit_draft`, because that
#: call is what carries the recipient, the disclosed load ids and the per-figure citations the
#: pre-send gate checks. Prose carries none of them, so there is nothing to gate.
#:
#: A prompt instruction saying so did not settle it, hence enforcing it here.
_SUBMIT_NUDGE = (
    "That reply was not delivered — text outside a tool call is discarded, so the carrier "
    "received nothing. Call `submit_draft` now with that same answer as `reply_body`, plus "
    "`to`, `load_ids`, and one citation per amount and date you stated. Do not restate the "
    "reply as text."
)


@dataclass(slots=True)
class AgentResult:
    """Outcome of a loop run."""

    draft: SubmitDraftOutput | None
    stop_reason: str  # "submit_draft" | "end_turn" | "max_iterations"
    iterations: int
    final_text: str = ""
    transcript: list[Message] = field(default_factory=list)


class AgentLoop:
    """Drives an :class:`LlmClient` through tool calls until it submits a draft."""

    def __init__(
        self,
        llm: LlmClient,
        registry: ToolRegistry,
        *,
        max_iterations: int = 12,
        max_tokens: int = 1024,
        max_submit_nudges: int = 2,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens
        #: How many times to remind the model to deliver via `submit_draft` before giving up.
        #: Bounded so a model that simply will not call the tool cannot spin.
        self._max_submit_nudges = max(0, max_submit_nudges)

    def run(
        self,
        *,
        system: str,
        intake_prompt: str,
        allowed_tools: tuple[str, ...],
        ctx: ToolContext,
    ) -> AgentResult:
        allowed = set(allowed_tools)
        specs = [self._registry.get(name).spec() for name in allowed_tools]
        messages: list[Message] = [Message(Role.USER, [TextBlock(intake_prompt)])]
        nudges = 0

        for iteration in range(1, self._max_iterations + 1):
            response = self._llm.converse(
                system=system, messages=messages, tools=specs, max_tokens=self._max_tokens
            )
            # provider_state carries anything the provider needs echoed back next turn —
            # a reasoning model's chain of thought, without which it forgets this turn.
            messages.append(
                Message(Role.ASSISTANT, response.content, provider_state=response.provider_state)
            )

            tool_uses = response.tool_uses
            if not tool_uses:
                # Answered in prose. The work may be complete but it was not delivered, so
                # ask once (or twice) for the terminal tool call before giving up.
                if nudges < self._max_submit_nudges:
                    nudges += 1
                    _log.info(
                        "agent_submit_nudge",
                        extra={
                            "correlation_id": ctx.correlation_id,
                            "iteration": iteration,
                            "nudge": nudges,
                        },
                    )
                    messages.append(Message(Role.USER, [TextBlock(_SUBMIT_NUDGE)]))
                    continue

                _log.info(
                    "agent_end_turn",
                    extra={
                        "correlation_id": ctx.correlation_id,
                        "iteration": iteration,
                        "nudges": nudges,
                    },
                )
                return AgentResult(
                    draft=None,
                    stop_reason=response.stop_reason or "end_turn",
                    iterations=iteration,
                    final_text=response.text,
                    transcript=messages,
                )

            result_blocks: list[ToolResultBlock] = []
            for call in tool_uses:
                if call.name not in allowed:
                    result_blocks.append(
                        ToolResultBlock(
                            tool_use_id=call.tool_use_id,
                            content={"ok": False, "error": f"tool {call.name!r} not available"},
                            is_error=True,
                        )
                    )
                    continue

                outcome = self._registry.dispatch(call.name, call.input, ctx)
                result_blocks.append(
                    ToolResultBlock(
                        tool_use_id=call.tool_use_id,
                        content=outcome.payload,
                        is_error=not outcome.ok,
                    )
                )
                if self._registry.is_terminal(call.name) and outcome.ok:
                    draft = SubmitDraftOutput.model_validate(outcome.payload)
                    _log.info(
                        "agent_submitted_draft",
                        extra={"correlation_id": ctx.correlation_id, "iteration": iteration},
                    )
                    return AgentResult(
                        draft=draft,
                        stop_reason="submit_draft",
                        iterations=iteration,
                        transcript=messages,
                    )

            messages.append(Message(Role.USER, list(result_blocks)))

        _log.warning(
            "agent_max_iterations",
            extra={"correlation_id": ctx.correlation_id, "max": self._max_iterations},
        )
        return AgentResult(
            draft=None,
            stop_reason="max_iterations",
            iterations=self._max_iterations,
            transcript=messages,
        )
