"""Payments Email Bot.

An agentic tool-use bot that answers carrier payment-status and rate-verification
emails for Circle Delivers. See the PRD (`PRD_Agent_Skills_and_Tools_Catalog_v1.2.md`)
for the authoritative product/architecture spec.

This package is organised in layers, low-level first:

* ``payment_bot.models``  — typed domain data (Transport Pro payload, email, enums).
* ``payment_bot.domain``  — pure, deterministic business logic (§4.1.1). No I/O.
* ``payment_bot.clients`` — external system adapters (TP / QBO / Gmail / Slack / LLM)
                            behind protocols, with mock implementations for tests.
* ``payment_bot.tools``   — typed tool wrappers the agent can call (§4.2-§4.6).
* ``payment_bot.gate``    — the deterministic pre-send gate (§5).
* ``payment_bot.agent``   — the portable Bedrock Converse tool-use loop (§8.1).
* ``payment_bot.pipeline``— the end-to-end orchestration (intake → agent → gate → send).
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
