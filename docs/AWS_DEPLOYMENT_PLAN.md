# AWS Deployment Plan — Payments Email Bot

The actionable, phased plan for moving the bot from the workstation to AWS. The companion
[AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md) is the architecture *reference* (service map, Lambda
split, credential flow); this document is the *plan*: what to do, in what order, what each
step needs, how to verify it, and how to roll back.

Written 2026-08-03, reflecting the system as it runs today: the authorization pre-check,
the 269-factor trust roster, spreadsheet-attachment intake, the nine-check pre-send gate,
the bank/NOA wording policies, and hourly scheduling via Windows Task Scheduler.

---

## 1. Where we are, where we're going

### Today (workstation)

| Concern | Current implementation |
|---|---|
| Trigger | Windows Task Scheduler, hourly (`scripts/run_bot.cmd`) |
| Compute | `payment-bot-local` on a developer workstation |
| LLM | OpenRouter free tier (`nemotron-3-ultra-550b:free`) — request-capped, flaky |
| Review surface | Gmail Drafts (a human reviews and presses Send) |
| Secrets | `.env` file on disk |
| Trust roster | `factoring_domains.json` generated from settlements CSV, local file |
| Audit | In-memory per run + console/log file (`logs/`) |
| Availability | Only while the workstation is on and the user logged in |

### Target (AWS, end state)

| Concern | Target implementation |
|---|---|
| Trigger | EventBridge Scheduler (hourly; tighten later) |
| Compute | Lambda (Python 3.12) — Fargate fallback if runs outgrow 15 min |
| LLM | **Amazon Bedrock**, `us.anthropic.claude-sonnet-5-v1:0` (`BedrockLlmClient` already in the codebase) |
| Review surface | Gmail Drafts (Stage 1) → Slack Approve/Edit/Reject (Stage 2) |
| Secrets | SSM Parameter Store (config) + Secrets Manager (credentials) |
| Trust roster | S3 object, fetched at cold start; regenerated from settlement exports |
| Audit | DynamoDB audit sink (every tool call + result, PRD §8.1) |
| Availability | Managed, always-on schedule, alarmed |

### Non-goals for this plan

* QuickBooks (6-digit) support — separate feature, unchanged by deployment.
* Auto-send (§8.5 Phase 2) — explicitly the LAST stage, gated on Stage 2 running clean.
* The `/load/missing_documents` cache (see MISSING_DOCUMENTS_CACHE.md) — independent.

---

## 2. The three stages

Deploy in three stages, each independently valuable, each with its own rollback. The key
insight: **the local runner's draft-only flow is already headless** — Stage 1 is a lift,
not a rewrite.

```
Stage 1  "Same bot, better home"     EventBridge → Worker Lambda → Gmail Drafts
Stage 2  "Slack approvals"           + Poller/SQS split, Slack callback Lambda, DynamoDB
Stage 3  "Selective auto-send"       PAYBOT_ROLLOUT_PHASE=2 for single-load payment status
```

### Stage 1 — scheduled worker Lambda (target: ~1 week)

One Lambda replicating exactly what `payment-bot-local` does hourly today: fetch unread →
pipeline per email → gate → save draft to Gmail Drafts → log. Humans keep reviewing in
Gmail, exactly as now. The only functional change is the LLM: **Bedrock Claude replaces the
free-tier model**, which eliminates the request-cap failures and most instruction-following
noise in one move.

Code changes required (small):

1. **`lambda_handler.py`** — a thin handler calling the same `process_inbox()` path the
   local runner uses (draft-only forced, `NullSlackClient`, `DeferredApprovalResolver`),
   with `build_bedrock_client()` instead of `build_groq_client()`.
2. **Roster loading** — fetch `factoring_domains.json` from S3 to `/tmp` at cold start;
   point `PAYBOT_FACTORING_DOMAINS_FILE` at it. (~15 lines + one IAM statement.)
3. **Settings from environment** — already works; Lambda env vars carry the non-secret
   config, and the handler resolves secrets (below) into env before `get_settings()`.

Definition of done: the workstation task and the Lambda run in parallel for 2–3 business
days producing identical outcomes (thread-skip makes double-processing safe — whichever
runs first drafts, the other skips), then the Windows task is disabled.

### Stage 2 — Slack approvals + durable audit (target: ~2–3 weeks after Stage 1)

The PRD's Phase 1 flow proper (AWS_DEPLOYMENT.md §1 diagram):

* **Poller Lambda** (EventBridge, every few minutes): `fetch_new` → one SQS message per
  email. DLQ after 3 attempts.
* **Processor Lambda** (SQS consumer, concurrency 1–2): the pipeline through the gate,
  then posts the draft to `#payments-approvals` with Approve / Edit / Reject buttons and
  persists the run (draft + gate inputs + correlation id) to DynamoDB.
* **Slack-Callback Lambda** (Function URL or API Gateway, Slack signing-secret verified):
  on Approve/Edit, loads the run from DynamoDB, **re-runs the gate** (human edits are
  re-gated — the pipeline already supports this), sends via Gmail API, marks sent.
* **DynamoDB audit sink**: implement the `AuditSink` protocol against a table
  (`correlation_id` PK, `ts#tool` SK). The seam exists (`payment_bot/logging.py`);
  this is the one genuinely new component.

New code: the three handlers, the DynamoDB sink, a Slack client that posts Block Kit
approvals (the `SlackClient` protocol and channel config already exist). The pipeline
itself does not change.

### Stage 3 — selective auto-send (only after Stage 2 has run clean for weeks)

Flip `PAYBOT_ROLLOUT_PHASE=2`. The code already restricts auto-send to **clean,
single-load payment-status drafts** (`_is_auto_sendable`); rate verification and anything
gate-flagged still requires the human click. Precondition: a written sign-off from
operations, and an alarm on auto-sent count.

---

## 3. Workstream details

### 3.1 Prerequisites & decisions (do these first)

| # | Decision / prerequisite | Owner | Notes |
|---|---|---|---|
| P1 | AWS account + region | ops | `us-east-1` assumed by config default |
| P2 | Bedrock model access enabled for `us.anthropic.claude-sonnet-5-v1:0` | ops | Console → Bedrock → Model access; verify with `aws bedrock list-inference-profiles` |
| P3 | IaC tool | eng | Recommendation: **AWS SAM** (three Lambdas + queue + tables is squarely its shape); CDK acceptable |
| P4 | Google service-account key handling | eng | Move key JSON into **Secrets Manager**; the code already supports inline JSON via `PAYBOT_GOOGLE_SA_JSON` |
| P5 | Dedicated shared mailbox for `paystatus@` | ops | Today a personal mailbox is impersonated (Google Groups can't be impersonated). A real shared account means drafts live in a team-visible Drafts folder. One `.env` line to switch (`PAYBOT_GMAIL_USER`) |
| P6 | Slack workspace app (Stage 2) | ops | Bot token + signing secret; channels `#payments-approvals`, `#payments-security` |
| P7 | GitHub repo → AWS deploy credentials | eng | GitHub Actions OIDC role (no long-lived keys) |

### 3.2 Configuration & secrets migration

Everything the bot reads is a `PAYBOT_*` variable — the migration is a table, not a
refactor. Secrets Manager for credentials, SSM Parameter Store (String) for plain config,
Lambda env for the boring constants.

| Variable | Destination | Notes |
|---|---|---|
| `PAYBOT_TP_USERNAME` / `PAYBOT_TP_PASSWORD` | Secrets Manager `paybot/transport-pro` | Rotate the password at migration — it has lived in a plaintext `.env` |
| `PAYBOT_GOOGLE_SA_JSON` | Secrets Manager `paybot/google-sa` | Full key JSON, inline. Delete the on-disk key after cutover; rotate the key in GCP |
| Slack bot token + signing secret (Stage 2) | Secrets Manager `paybot/slack` | |
| `PAYBOT_GMAIL_USER`, `PAYBOT_GMAIL_QUERY`, `PAYBOT_MAILBOX` | SSM `/paybot/gmail/*` | Query keeps the `to:paystatus@` guard — it is load-bearing while impersonating a personal mailbox |
| `PAYBOT_FACTORING_DOMAINS` (inline patches) | SSM `/paybot/factoring-domains-inline` | The hand-verified overrides; small JSON |
| `PAYBOT_FACTORING_DOMAINS_FILE` | Lambda env → `/tmp/factoring_domains.json` | Object fetched from S3 at cold start (see 3.3) |
| `PAYBOT_ALLOW_FACTORING`, `PAYBOT_SENSITIVE_BANK_REPLIES`, `PAYBOT_SENSITIVE_NOA_REPLIES` | SSM `/paybot/policy/*` | **Policy switches — changing them should be deliberate and audited**, hence Parameter Store with change history, not plain env |
| `PAYBOT_REPLY_SIGNATURE`, `PAYBOT_REPLY_CC`, `PAYBOT_DOCUMENTS_EMAIL`, `PAYBOT_PORTAL_URL`, `PAYBOT_BULK_THRESHOLD` | Lambda env | Plain constants |
| `PAYBOT_AGENT_MAX_ITERATIONS=20`, `PAYBOT_AGENT_MAX_TOKENS` | Lambda env | 4096 tokens is fine for Claude (non-reasoning-budget); 16384 was a free-model accommodation |
| `PAYBOT_MODEL_DRAFT`, `PAYBOT_AWS_REGION` | Lambda env | Bedrock model id |
| `PAYBOT_GROQ_*` | **dropped** | Local-only provider |
| `PAYBOT_DRAFT_ONLY` | Lambda env, `true` in Stage 1 | Stage 2 keeps it `true` in the processor; only the Slack callback sends |

### 3.3 Trust roster pipeline

The roster is business data with a lifecycle, not code:

1. Private S3 bucket `paybot-config-<acct>`: `factoring_domains.json` (+ versioning on).
2. Regeneration stays a human-triggered step for now: run
   `scripts/generate_factoring_domains.py` against a fresh settlements export, review the
   diff, upload. (Later: a small scheduled job if the export lands somewhere reachable.)
3. Worker fetches the object at cold start; a fetch failure **fails the run loudly**
   (mirrors the local fail-loud file loading) rather than silently authorizing nobody.
4. The inline patches (RTS sister domains, rebrands) live in SSM and win on collision,
   exactly as `.env` does today.

### 3.4 IAM (least privilege per function)

Worker/Processor role — statements only for:

* `bedrock:InvokeModel` on the one model ARN
* `secretsmanager:GetSecretValue` on the three `paybot/*` secrets
* `ssm:GetParameter(s)` on `/paybot/*`
* `s3:GetObject` on the roster object
* `dynamodb:PutItem` on the audit table (Stage 2: + run-state table R/W)
* CloudWatch Logs write (managed policy)

Poller role: Gmail is an external API (no IAM) — just secrets read + `sqs:SendMessage`.
Callback role: run-state read/write, secrets read, no Bedrock.

Gmail scopes stay exactly `gmail.readonly` + `gmail.compose` (domain-wide delegation is
already granted for these). **No new Google-side permissions are needed for any stage** —
sending in Stage 2 uses the same `gmail.compose`-adjacent send call the PRD documents, via
the existing delegated account.

### 3.5 Observability

The JSON logging already emits machine-parseable lines; CloudWatch picks them up as-is.

Metric filters → CloudWatch metrics (per run):

| Metric | Source log event | Alarm |
|---|---|---|
| `DraftsCreated` | `gmail_api_draft_created` | — |
| `Escalations` (dimension: reason) | `escalated` | Spike alarm (>3× 7-day baseline) |
| `GateBlocked` | `gate_blocked` | >2/day — the model is misbehaving |
| `LlmFailures` | Bedrock client errors | >3/hour |
| `PolicyAllowedChangeWording` | `bank_change_language_allowed_by_policy` | Daily digest — every one of these needs a human to action the request |
| `RunFailures` | Lambda errors / DLQ depth | Any → page |

Plus a weekly *capability report*: the ESCALATIONS.md §6 audit run as a read-only scheduled
job, publishing the answerable/escalated breakdown — the number that shows whether the
roster and checks are keeping up with real mail.

### 3.6 CI/CD

GitHub Actions on the repo (branches already in use):

1. **On PR**: `ruff check` + `pytest` (365 tests, no network — the suite is already
   hermetic thanks to `isolate_settings`).
2. **On merge to `main`**: `sam build && sam deploy` to a **staging stack** pointed at a
   test mailbox + Transport Pro sandbox credentials (or the mock client if no sandbox
   exists), then manual promotion to prod.
3. Deploy artifact carries no secrets and no roster — both are runtime-fetched.

### 3.7 Runtime shape & limits

* One email averages 8–15 Bedrock turns (procedure + submit); with Claude Sonnet latency
  that is ~30–90 s per email; `PAYBOT_GMAIL_FETCH_LIMIT=10` keeps the worst-case run under
  ~12 min — inside Lambda's 15-min cap but close. Mitigations, in order: drop fetch limit
  to 5 per run (the hourly cadence absorbs it), raise cadence to every 30 min, or move the
  worker to a scheduled Fargate task (same container, no time cap). Decide after a week of
  Stage 1 timings.
* Concurrency **1** on the worker (reserved concurrency) — not for safety (thread-skip
  makes concurrent runs converge) but to keep Gmail API usage and logs sane.
* Cold start: SA-JWT mint + roster fetch ≈ 1–2 s; irrelevant at this cadence.

---

## 4. Cutover plan (Stage 1)

1. Deploy the stack with the schedule **disabled**; invoke the worker manually once
   against the live mailbox; diff its log against the same hour's workstation log.
2. Enable the EventBridge schedule at :15 past the hour (workstation task keeps :39) —
   two days of interleaved parallel running. Thread-skip guarantees no duplicate drafts;
   what to verify is *parity of outcomes* per email (draft/escalation with same reasons)
   and Bedrock draft quality (expect strictly fewer gate blocks and style repairs).
3. Disable the Windows task: `schtasks /change /tn "Payment Bot Hourly" /disable`.
4. One week later, delete the task and rotate the TP password + Google key (retiring the
   plaintext `.env` copies).

**Rollback at any point**: disable the EventBridge rule, re-enable the Windows task —
the workstation setup remains intact until step 4 and is a two-minute restore.

---

## 5. Cost estimate (monthly, ~100 emails/month at today's volume)

| Item | Estimate | Basis |
|---|---|---|
| Bedrock (Claude Sonnet) | $8–20 | ~100 emails × ~120k in / 6k out tokens |
| Lambda | < $1 | ~720 short invocations + processing |
| EventBridge, SQS, SSM | ~$0 | Well inside free/negligible tiers |
| DynamoDB (on-demand, Stage 2) | < $1 | Tool-call audit rows are tiny |
| Secrets Manager | ~$1.60 | 4 secrets × $0.40 |
| CloudWatch (logs + alarms) | $2–5 | JSON logs, short retention (90 days) |
| **Total** | **~$12–30/month** | Dominated by the model; scales linearly with mail volume |

Cheaper lever if volume grows 10×: route classification-adjacent turns to Claude Haiku and
keep Sonnet for drafting (the PRD's §8.1.1 two-model split; the `LlmClient` seam supports
it without pipeline changes).

---

## 6. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Lambda 15-min cap on a heavy batch | Fetch limit 5 + 30-min cadence; Fargate fallback documented above |
| Google key leakage during migration | Key moves to Secrets Manager *and is rotated*; old key revoked in GCP console |
| Slack callback forged (Stage 2) | Signing-secret verification + timestamp window in the callback Lambda; deny by default |
| Human edits bypassing checks (Stage 2) | Already impossible: edited drafts are re-gated in `_finalize` before send |
| Roster staleness | Weekly capability report surfaces rising "domain not configured" denials; regeneration is one script run |
| Policy switches flipped casually | They live in SSM with change history; the plan requires the same evidence bar used to set them (documented in `.env` comments today) |
| Bedrock regional outage | Runs fail loudly and retry next hour; mail stays unread — the system's fail-closed posture means an outage delays drafts, never corrupts them |
| Duplicate sends (Stage 2/3) | DynamoDB run-state conditional writes (send recorded exactly once); Gmail threading already prevents duplicate drafts |

---

## 7. Open items feeding this plan

* [ ] P1–P7 prerequisite decisions (§3.1)
* [ ] Confirm Transport Pro allows API calls from AWS egress IPs (no allowlist observed, verify)
* [ ] Shared mailbox migration (P5) — removes the personal-mailbox `to:` guard caveat
* [ ] DynamoDB `AuditSink` implementation + tests (the one new component, Stage 2)
* [ ] Slack app manifest + the two channels (Stage 2)
* [ ] Staging mailbox + seeded test threads for CI verification
