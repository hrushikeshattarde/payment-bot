# Local Implementation & Run Guide

How to set up, run, test, and extend the Payments Email Bot on your own machine.

This project is designed so the **entire pipeline runs locally with no cloud access** —
external systems (Transport Pro, Gmail, Slack, the LLM) sit behind `Protocol` interfaces
with mock/scripted implementations. You can then progressively swap in real backends.

- Companion doc: [AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md) — production architecture + APIs.
- Spec: [`../PRD_Agent_Skills_and_Tools_Catalog_v1.2.md`](../PRD_Agent_Skills_and_Tools_Catalog_v1.2.md).

---

## 1. The three run modes

| Mode | LLM | Transport Pro / Gmail / Slack | Needs AWS? | Use it for |
|---|---|---|---|---|
| **A. Fully mocked** | `ScriptedLlmClient` | Mock clients | No | Tests, the demo, developing logic |
| **B. Real Bedrock + mock backends** | `BedrockLlmClient` | Mock clients | Yes (Bedrock only) | Seeing the real model drive the loop |
| **C. Fully real** | `BedrockLlmClient` | Real adapters *(you implement)* | Yes | Pre-production integration |

> **Status today:** `BedrockLlmClient` is fully implemented. The **real** Transport Pro /
> Gmail / Slack adapters are *not yet written* (blocked on the PRD §9 API dependencies) —
> only the mock implementations exist. Mode C therefore requires you to implement those
> adapters behind the existing protocols (see [§7](#7-going-fully-real-mode-c)).

---

## 2. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11+ (dev on 3.12) | Uses `enum.StrEnum`, `datetime.UTC` — 3.11 is the floor. |
| pip | recent | Ships with Python. |
| git | optional | Only if you clone rather than copy the folder. |
| AWS account | optional | Only for run modes B and C (Amazon Bedrock). |

Check:

```bash
python --version
```

On Windows the interpreter may be `py -3.12` instead of `python`.

---

## 3. Install

From the project root (`Payment Bot/`):

```bash
python -m venv .venv
```

Activate it:

```bash
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Windows CMD
.venv\Scripts\activate.bat
# bash / git-bash / macOS / Linux
source .venv/Scripts/activate   # or .venv/bin/activate on macOS/Linux
```

Install the package in editable mode with dev tools:

```bash
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

To also install the AWS SDK (needed for run modes B and C):

```bash
python -m pip install -e ".[dev,aws]"
```

> `boto3` is an **optional extra** (`aws`) on purpose — the core, tests, and the scripted
> demo install and run without it. `BedrockLlmClient` imports `boto3` lazily and only when
> actually used.

---

## 4. Configuration

Copy the example env file and edit values as needed:

```bash
# PowerShell
Copy-Item .env.example .env
# bash
cp .env.example .env
```

All settings are read by [`config.py`](../src/payment_bot/config.py) (via
`pydantic-settings`) from environment variables prefixed `PAYBOT_` or from `.env`.
**Never commit `.env`.**

| Variable | Default | Meaning |
|---|---|---|
| `PAYBOT_ENV` | `local` | Environment label (`local`/`dev`/`prod`). |
| `PAYBOT_ROLLOUT_PHASE` | `1` | `1` = every draft needs Slack approval; `2` = clean single-load `payment_status` may auto-send (§8.5). |
| `PAYBOT_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. Logs are JSON lines. |
| `PAYBOT_MAILBOX` | `paystatus@circledelivers.com` | Inbox the bot serves. |
| `PAYBOT_BULK_THRESHOLD` | `5` | Above this many loads in one email → portal fallback. |
| `PAYBOT_PORTAL_URL` | circledelivers.com/payment-status-lookup/ | Portal link for bulk replies. |
| `PAYBOT_PAY_DATE_TZ` | `EDT` | Timezone for the Mon/Thu pay-date rule (§4.1.1). |
| `PAYBOT_AWS_REGION` | `us-east-1` | Region for Bedrock. |
| `PAYBOT_MODEL_FAST` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Cheap model for classify/extract. |
| `PAYBOT_MODEL_DRAFT` | `us.anthropic.claude-sonnet-5-v1:0` | Larger model for the final draft. |
| `PAYBOT_AGENT_MAX_ITERATIONS` | `12` | Hard cap on agent tool-use turns. |
| `PAYBOT_SLACK_APPROVAL_CHANNEL` | `#payments-approvals` | Approval channel. |
| `PAYBOT_SLACK_SECURITY_CHANNEL` | `#payments-security` | Security-escalation channel. |

Secrets (Slack bot token, signing secret, Transport Pro / Gmail credentials) are **not**
in `.env` for production — they come from AWS at runtime (see the AWS doc). For local
experiments you may set them as environment variables.

---

## 5. Verify the install (run mode A)

Everything below works with zero cloud access.

**Run the tests** (84 tests, unit + integration):

```bash
pytest
```

With coverage:

```bash
pytest --cov=payment_bot --cov-report=term-missing
```

Only fast unit tests, or only integration tests:

```bash
pytest -m unit
pytest -m integration
```

**Lint and type-check:**

```bash
ruff check .
mypy
```

**Run the demo** — drives both skills (`payment_status` and `rate_verification`)
end-to-end on sample load `2462934`, printing the audited tool trail, the pre-send gate
decision, the drafted reply, and the outcome:

```bash
payment-bot-demo
```

(equivalently `python -m payment_bot.runner`).

You should see two scenarios, each ending in `OUTCOME: SENT`, with all five gate checks
`PASS`.

---

## 6. Run mode B — real Bedrock, mock backends

This lets a **real Claude model** drive the tool-use loop while Transport Pro / Gmail /
Slack stay mocked. You need AWS credentials and Bedrock model access.

### 6.1 One-time AWS prep

1. Install the AWS extra: `pip install -e ".[dev,aws]"`.
2. Configure credentials (any standard method):
   ```bash
   aws configure sso        # or: aws configure
   # or export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
   ```
3. **Enable Bedrock model access** for the Claude models in your region
   (`us-east-1` by default): AWS Console → Bedrock → *Model access* → enable the Anthropic
   Claude models. See the AWS doc for details and IAM.

### 6.2 A runnable script

Save this as `scratch_bedrock_run.py` in the project root and run
`python scratch_bedrock_run.py`:

```python
from payment_bot.clients import (
    AutoApproveResolver, BedrockLlmClient, MockGmailClient, MockSlackClient,
)
from payment_bot.config import get_settings
from payment_bot.logging import InMemoryAuditSink, configure_logging
from payment_bot.pipeline import PaymentBotPipeline
from payment_bot.sample_data import sample_payment_status_email, sample_transport_pro_client

configure_logging("INFO")
settings = get_settings()

# Real model, mock everything else:
llm = BedrockLlmClient(model_id=settings.model_draft, region=settings.aws_region)

pipeline = PaymentBotPipeline(
    tp=sample_transport_pro_client(),        # mock TP data (load 2462934)
    gmail=MockGmailClient(),                  # records "sent" mail, sends nothing
    slack=MockSlackClient(),                  # records posts, posts nothing
    llm=llm,
    approval_resolver=AutoApproveResolver(),  # demo-only auto-approve
    settings=settings,
    audit_sink=InMemoryAuditSink(),
)

result = pipeline.process_email(sample_payment_status_email())
print(result.outcome, "->", result.detail)
print(result.draft.reply_body if result.draft else "(no draft)")
```

The model now decides which tools to call; the deterministic gate still guards the draft.
Because the backends are mocked, **no real email is sent and no Slack message is posted.**

> Tip: route the cheap steps to the fast model in production; here we use one model for
> simplicity. The pipeline itself doesn't call the model — only the agent loop does.

---

## 7. Going fully real (mode C)

To talk to real systems, implement the client protocols and pass your implementations
into `PaymentBotPipeline` instead of the mocks. The protocols are the contract:

| Protocol | File | Methods to implement |
|---|---|---|
| `TransportProClient` | [`clients/transport_pro.py`](../src/payment_bot/clients/transport_pro.py) | `get_load`, `get_dispatch_history`, `get_settlement_entries`, `get_file_history`, `get_noa_factoring`, `get_authorization_context` |
| `GmailClient` | [`clients/gmail.py`](../src/payment_bot/clients/gmail.py) | `fetch_new`, `send_reply` |
| `SlackClient` | [`clients/slack.py`](../src/payment_bot/clients/slack.py) | `post_approval`, `post_escalation` |
| `LlmClient` | [`clients/llm.py`](../src/payment_bot/clients/llm.py) | already done: `BedrockLlmClient` |

Each real client is a thin HTTP/SDK wrapper that returns the **same typed models** the
mocks return (e.g. `TransportProLoad` parsed from the §4.3.0 payload). Nothing above the
client layer changes — the tools, gate, agent, and pipeline stay identical. Suggested
locations: `clients/transport_pro_http.py`, `clients/gmail_google.py`,
`clients/slack_web.py`. The external endpoints and auth each one needs are enumerated in
[AWS_DEPLOYMENT.md §5](AWS_DEPLOYMENT.md#5-apis-you-will-need).

---

## 8. Common tasks

**Run one skill in the demo** — call `run_demo()` (payment) or `run_rate_demo()` (rate)
from [`runner.py`](../src/payment_bot/runner.py).

**Add a new sample email / scenario** — add a builder in
[`sample_data.py`](../src/payment_bot/sample_data.py) alongside
`sample_payment_status_email()` and, for a scripted run, a `ScriptedLlmClient` factory
that emits the tool-use turns you expect.

**Add a new tool** — subclass `Tool` in `tools/`, define its Pydantic `input_model` and an
output model, implement `run`, register it in `build_default_registry`
([`tools/__init__.py`](../src/payment_bot/tools/__init__.py)), and add it to the relevant
skill's tool tuple (`PAYMENT_STATUS_TOOLS` / `RATE_VERIFICATION_TOOLS`).

**Inspect the audit trail** — pass an `InMemoryAuditSink` to the pipeline and read
`sink.for_correlation(email.message_id)`; every tool call + result is recorded (§8.1).

---

## 9. Project layout

```
Payment Bot/
├── pyproject.toml            # deps, ruff/mypy/pytest config, console script
├── .env.example              # copy to .env
├── README.md
├── docs/
│   ├── LOCAL_SETUP.md        # this file
│   └── AWS_DEPLOYMENT.md
└── src/payment_bot/
    ├── config.py             # typed settings (PAYBOT_* env)
    ├── logging.py            # JSON logging + audit sink
    ├── errors.py
    ├── grounding.py          # ledger the pre-send gate checks against
    ├── models/               # typed data (Transport Pro §4.3.0 payload, email, enums)
    ├── domain/               # pure deterministic logic (Mon/Thu pay date, rate, routing)
    ├── clients/              # protocols + mocks (TP, Gmail, Slack, LLM/Bedrock)
    ├── tools/                # agent-callable tools + registry
    ├── gate/                 # deterministic pre-send gate (§5)
    ├── agent/               # portable tool-use loop + skill prompts
    ├── pipeline.py           # end-to-end orchestration
    ├── runner.py             # `payment-bot-demo`
    └── sample_data.py        # load 2462934 fixture + scripted flows
```

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: payment_bot` | The editable install didn't run or wrong venv is active. Re-run `pip install -e ".[dev]"`. |
| `ClientError: boto3 is required for BedrockLlmClient` | Install the AWS extra: `pip install -e ".[dev,aws]"`. |
| Bedrock `AccessDeniedException` / model not found | Model access not enabled in the region, or IAM lacks `bedrock:InvokeModel`. See [AWS_DEPLOYMENT.md §7](AWS_DEPLOYMENT.md#7-amazon-bedrock-setup). |
| `SyntaxError` around `StrEnum` / `datetime.UTC` | You're on Python < 3.11. Use 3.11+. |
| PowerShell mangles a `python -c "..."` one-liner | Put the code in a `.py` file and run the file, or use a here-string carefully. |
| Path errors on Windows | The folder name contains a space (`Payment Bot`); quote paths in shell commands. |
| A draft is unexpectedly blocked as "ungrounded" | The reply contains a `$` amount or date not produced by a tool. That's the gate (§5) working — every figure must trace to a tool result. |
