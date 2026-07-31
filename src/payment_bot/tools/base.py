"""Tool framework: context, the ``Tool`` base class, and the dispatching registry.

Every agent-callable capability is a :class:`Tool` with a Pydantic input model (which
doubles as its Bedrock ``inputSchema``) and a Pydantic output model. The
:class:`ToolRegistry` is the single choke point through which the agent loop invokes
tools; it enforces the cross-cutting contracts from the PRD:

* **Typed I/O** — input is validated before ``run``; output is serialised to JSON.
* **Error envelope (§4.1)** — any expected failure becomes ``{"ok": false, "error": …}``
  instead of crashing the loop.
* **Audit (§8.1)** — every call and its result is written to the audit sink.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from payment_bot.clients.llm import ToolSpec
from payment_bot.clients.transport_pro import TransportProClient
from payment_bot.config import Settings, get_settings
from payment_bot.errors import ClientError, ToolError
from payment_bot.grounding import GroundingLedger
from payment_bot.logging import AuditRecord, AuditSink, InMemoryAuditSink, get_logger

_log = get_logger("tools")


@dataclass(slots=True)
class ToolContext:
    """Per-run dependencies handed to every tool.

    Note there is **no Gmail or Slack client here** — sending is not an agent capability.
    The agent can only read and compute; the pipeline performs sends after the gate.
    """

    tp: TransportProClient
    ledger: GroundingLedger
    correlation_id: str
    settings: Settings = field(default_factory=get_settings)
    # Free-form space for tools to share intermediate state within one run.
    scratch: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    """Base class for an agent-callable tool.

    Subclasses set ``name``, ``description`` and ``input_model`` and implement ``run``.
    ``run`` receives an already-validated instance of ``input_model`` (typed as
    :class:`~pydantic.BaseModel`; narrow it with ``isinstance`` inside) and returns any
    :class:`~pydantic.BaseModel`.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    #: Terminal tools end the agent loop when called (e.g. ``submit_draft``).
    is_terminal: ClassVar[bool] = False

    @abstractmethod
    def run(self, params: BaseModel, ctx: ToolContext) -> BaseModel: ...

    def spec(self) -> ToolSpec:
        """Bedrock ``toolSpec`` derived from the Pydantic input model."""

        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
        )


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Result of a dispatch: the JSON payload plus a success flag."""

    payload: dict[str, Any]
    ok: bool


class ToolRegistry:
    """Holds tools and dispatches calls through the shared contracts."""

    def __init__(self, audit_sink: AuditSink | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._audit: AuditSink = audit_sink or InMemoryAuditSink()

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool registered: {tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def is_terminal(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.is_terminal)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self._tools.values()]

    @property
    def audit_sink(self) -> AuditSink:
        return self._audit

    def dispatch(self, name: str, raw_input: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        """Validate, run, serialise, audit — never raises for expected failures."""

        started = time.perf_counter()
        payload: dict[str, Any]
        ok: bool

        tool = self._tools.get(name)
        if tool is None:
            payload, ok = {"ok": False, "error": f"unknown tool: {name}"}, False
        else:
            try:
                params = tool.input_model.model_validate(raw_input)
                result = tool.run(params, ctx)
                payload = result.model_dump(mode="json")
                # A tool may signal a soft failure by including ok=false in its model.
                ok = bool(payload.get("ok", True))
            except ValidationError as exc:
                payload, ok = {"ok": False, "error": f"invalid input: {exc.errors()}"}, False
            except (ToolError, ClientError) as exc:
                payload, ok = {"ok": False, "error": str(exc)}, False

        duration_ms = (time.perf_counter() - started) * 1000.0
        self._audit.record(
            AuditRecord(
                correlation_id=ctx.correlation_id,
                tool_name=name,
                request=raw_input,
                response=payload,
                ok=ok,
                duration_ms=duration_ms,
            )
        )
        if not ok:
            # Log the arguments, not just the error. A tool failing repeatedly is nearly always
            # the model sending the wrong shape, and without the arguments the trail shows
            # "[ERR] compute_scheduled_pay_date" seven times with no way to tell why.
            _log.warning(
                "tool_failed",
                extra={
                    "tool": name,
                    "correlation_id": ctx.correlation_id,
                    "error": payload,
                    "arguments": raw_input,
                },
            )
        return ToolOutcome(payload=payload, ok=ok)
