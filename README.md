# Payments Email Bot

Agentic tool-use bot that answers carrier **payment-status** and **rate-verification**
emails for Circle Delivers' `paystatus@circledelivers.com` inbox.

This repository implements the architecture defined in
[`PRD_Agent_Skills_and_Tools_Catalog_v1.2.md`](PRD_Agent_Skills_and_Tools_Catalog_v1.2.md).
Section references below (e.g. §4.1.1) point into that PRD.

> **Scope of this build.** Production-grade, end-to-end **`payment_status` *and*
> `rate_verification`** skills over **7-digit / Transport Pro** loads: intake → shared
> intake & safety → intent classification → agent tool-use loop → deterministic pre-send
> gate → Slack approval → gated Gmail send. External systems (Transport Pro, Gmail, Slack,
> Bedrock) sit behind protocol interfaces with **mock implementations**, so the whole loop
> runs and is unit-tested locally with no cloud access. QuickBooks (6-digit), carrier-name
> lookup, and combined-intent merging (§3.5) have typed seams in place but are intentionally
> not wired yet.

## Architecture

One agent, deterministic guardrails around it. The agent *reasons and drafts*; all
money-movement-adjacent safety (authorization, fraud, grounding) is **code, not model**.

```
Gmail intake
   │
   ▼
Shared intake & safety (deterministic, §3.3)
   classify_intent → extract_identifiers → route_load → detect_sensitive_change → bulk/length
   │  (escalate & stop on sensitive change / invalid length / bulk / unclear intent)
   ▼
Select skill by intent  ──▶  payment_status   or   rate_verification
   │
   ▼
Agent tool-use loop (Bedrock Converse, §8.1)     ← read-only TP tools + compute tools
   tp_get_load_summary / dispatch / settlement / file_history / noa_factoring
   compute_scheduled_pay_date · compute_carrier_rate · carrier_cross_check · check_authorization
   → submit_draft (terminal)
   │
   ▼
Pre-send gate (deterministic, §5)  ── fail ──▶ Slack escalation (never send)
   authorization · fraud · grounding · length · bulk
   │ pass
   ▼
Slack approval (Phase 1, §8.5) ── approve ──▶ Gmail send  (gated)
   (rate mismatches are never auto-sent, even in Phase 2)
```

### Layers (`src/payment_bot/`)

| Module | Responsibility |
|---|---|
| `models/` | Typed domain data — Transport Pro payload (§4.3.0), email, enums. |
| `domain/` | **Pure** deterministic business logic (§4.1.1). No I/O. Fully unit-tested. |
| `clients/` | External adapters (TP / Gmail / Slack / LLM) behind protocols + mocks. |
| `tools/` | Typed tool wrappers the agent calls; registry generates Bedrock tool specs. |
| `grounding.py` | Ledger of facts emitted by tools — the gate checks drafts against it. |
| `gate/` | The deterministic pre-send gate (§5). Never bypassed. |
| `agent/` | Portable Converse tool-use loop + versioned skill prompts. |
| `pipeline.py` | Wires the whole slice together. |
| `runner.py` | Local demo entrypoint — runs the slice on a sample email with mocks. |

### Why a portable tool-use loop (not managed Bedrock Agents)

The PRD names Bedrock Agents as preferred (§8.1), but the safety-critical parts — the
pre-send gate (§5) and the §4.1.1 math — **must be auditable code**. Owning the loop
keeps routing, the gate, and business logic as plain Python we fully unit-test, while
still calling Bedrock models for reasoning/drafting. `LlmClient` is a protocol:
`BedrockLlmClient` for production, `ScriptedLlmClient` for deterministic tests.

## Documentation

- [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md) — install, configure, test, and run locally
  (three run modes: fully mocked → real Bedrock → fully real).
- [docs/AWS_DEPLOYMENT.md](docs/AWS_DEPLOYMENT.md) — production architecture on AWS, the
  full AWS + external API inventory, IAM, Bedrock setup, and a SAM deployment path.

## Getting started

Requires Python 3.11+ (developed on 3.12).

```bash
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# bash / git-bash:     source .venv/Scripts/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Run the checks and the demo:

```bash
pytest              # unit + integration tests (no cloud access needed)
ruff check .        # lint
mypy                # static type check
payment-bot-demo    # run the payment_status slice end-to-end on a sample email
```

`boto3` is an optional extra (only the real Bedrock/Gmail/Slack adapters need it):

```bash
python -m pip install -e ".[dev,aws]"
```

## Skills

| Skill | What it answers | Key tools |
|---|---|---|
| `payment_status` (§3.1) | Status + scheduled pay date per earning line (Mon/Thu rule). | `tp_get_load_summary`, `compute_scheduled_pay_date`, dispatch/settlement/file-history, `carrier_cross_check`. |
| `rate_verification` (§3.2) | Carrier rate = Σ earnings vs sender's stated amount (match/mismatch), each deduction with reason + net, invoice generated?, NOA/factoring (read-only). | `compute_carrier_rate`, `tp_get_noa_factoring`, load-summary, dispatch/settlement/file-history. |

`classify_intent` routes each email; a rate **mismatch** is never auto-sent (§8.5).

## Testing strategy (§8.4)

* **Unit** — the deterministic core in isolation: the Mon/Thu pay-date table across all
  seven weekdays, carrier-rate computation, load routing, identifier extraction, intent
  classification, sensitive-change detection, and every pre-send-gate failure mode.
* **Integration** — each skill's full pipeline driven by a `ScriptedLlmClient` that emits a
  fixed tool-use sequence, asserting the tool order, a passing gate, and a gated send — plus
  escalation paths (bank/NOA change) and the "rate never auto-sends" guarantee. Load
  `2462934` (the real §4.3.0 payload) is the primary fixture.

## Not yet wired (tracked seams)

* QuickBooks Online tools (6-digit routing returns `quickbooks`; tools are stubs).
* Carrier-name lookup, portal bulk-reply body, combined-intent merging (§3.5, currently
  escalates).
* Real HTTP/API clients (blocked on PRD §9 open dependencies) and infrastructure-as-code.
* Reply wording/templates and Samples A–L regression fixtures — these live in the
  companion `PRD_Payment_Status_Email_Bot.md`, which is not yet in this workspace.
