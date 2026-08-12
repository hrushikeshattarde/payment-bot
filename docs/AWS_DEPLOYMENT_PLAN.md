# AWS Deployment Plan — Payments Email Bot

The actionable, phased plan for moving the bot from the workstation to AWS. The companion
[AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md) is the architecture *reference* (service map, Lambda
split, credential flow); this document is the *plan*: what to do, in what order, what each
step needs, how to verify it, and how to roll back.

Written 2026-08-03, revised 2026-08-12, reflecting the system as it runs today: the
authorization pre-check, the 272-factor trust roster, spreadsheet-attachment intake, the
twelve-check pre-send gate, the bank/NOA wording policies, **the CargoTel path for 6-digit
loads**, and 20-minute scheduling via Windows Task Scheduler.

The 2026-08-12 revision is mostly about CargoTel. It is the first dependency that is neither
an API nor ours: a scraped back-office whose session cookie is maintained by a **separate
login bot**. That changes the IAM, the alarms and the risk register, so it is threaded
through this plan rather than noted once.

A second 2026-08-12 pass folded in a day of live-mail fixes that touch deployment. In brief,
because each is picked up where it lands:

* **The HTML part of an email is now read** (`InboundEmail.html_text`). Portal collections
  mail puts its invoice table in the HTML only, so a load id could exist nowhere else. It
  feeds identifier extraction *and* the sensitive-change scan, and the gate's own re-derivation
  of that scan, so all three see the same text.
* **A twelfth gate check, `weekday_consistency`**, re-derives the weekday of every date a
  draft names. Grounding compares dates and never the adjectives attached to them.
* **Bank-redirect detection widened**: a change announcement carrying a supplied account or
  routing number is now hard evidence however passive the grammar. Raises the
  `PolicyAllowedChangeWording` digest's importance (§3.5).
* **CargoTel not-a-load has three shapes, not two** — "Invalid Order ID", the login page, and
  a load form with no order in it. That third one changes a metric filter (§3.5).
* **6-digit ids are not exclusive to CargoTel.** Transport Pro numbered loads with six digits
  years ago and still serves them. Measured, decided, and deliberately *not* "fixed" — see the
  new risk row in §6, and `payment_bot.domain.routing` for the evidence.
* **`PAYBOT_AWS_PROFILE`** is now a setting rather than a shell export, which matters for the
  Lambda: it must be left blank there (§3.2).

---

## 1. Where we are, where we're going

### Today (workstation)

| Concern | Current implementation |
|---|---|
| Trigger | Windows Task Scheduler, every 20 min (`scripts/run_bot.cmd`) |
| Compute | `payment-bot-local` on a developer workstation |
| LLM | OpenRouter, `anthropic/claude-haiku-4.5` (the `PAYBOT_GROQ_*` settings are historically named; the endpoint is OpenAI-compatible, the model is Claude) |
| Review surface | Gmail Drafts (a human reviews and presses Send) |
| Secrets | `.env` file on disk |
| Trust roster | `factoring_domains.json` generated from settlements CSV, local file; hand-verified send-from domains merged from `factoring_domains_manual.json` |
| 6-digit loads | CargoTel back-office scraped over HTTP; session cookie read from S3 via the ambient AWS credential chain (`PAYBOT_AWS_PROFILE`, or a `[default]` profile in `~/.aws/credentials`) |
| AWS credentials | Temporary, human-refreshed. They carry a session token but record no expiry, so boto3 cannot renew them: when they lapse every 6-digit load escalates until someone logs in again. **This is the single strongest operational argument for the migration** — a Lambda execution role has no expiry to manage |
| Audit | In-memory per run + console/log file (`logs/`) |
| Availability | Only while the workstation is on and the user logged in |

### Target (AWS, end state)

| Concern | Target implementation |
|---|---|
| Trigger | EventBridge Scheduler — match the workstation's **20 minutes**, not the hourly figure this plan originally assumed. Carriers chase within the hour and the local task was tightened for that reason; deploying at a slower cadence than the thing being replaced would be a visible regression |
| Compute | Lambda (Python 3.12) — Fargate fallback if runs outgrow 15 min |
| LLM | **Amazon Bedrock**, `us.anthropic.claude-sonnet-5-v1:0` (`BedrockLlmClient` already in the codebase) |
| Review surface | Gmail Drafts (Stage 1) → Slack Approve/Edit/Reject (Stage 2) |
| Secrets | SSM Parameter Store (config) + Secrets Manager (credentials) |
| Trust roster | S3 object, fetched at cold start; regenerated from settlement exports |
| 6-digit loads | Same scraping, but the cookie is read with the Lambda's own role — no exported profile |
| Audit | DynamoDB audit sink (every tool call + result, PRD §8.1) |
| Availability | Managed, always-on schedule, alarmed |

### CargoTel changes the dependency picture

CargoTel (6-digit loads) **is implemented** and is no longer a non-goal — but it is unlike
every other adapter here, and the difference is a deployment concern rather than a coding
one:

* **No API.** The client parses the back-office HTML. Parsers break when the vendor restyles
  a page, so treat the CargoTel parser as something that will need maintenance on a cadence
  the JSON adapters never will.
* **No credential of our own.** Authentication is a browser session cookie that a **separate
  login bot** writes to `s3://circle-bot-cookies/rubicon/cargotel.json`. Nothing in this
  repository can create or refresh it. The 6-digit path is therefore only as available as
  that bot, which is a dependency this plan does not otherwise have.
* **Its failure is silent.** A dead cookie returns HTTP 200 with the login page. The client
  detects that and raises, so emails escalate rather than being answered wrongly — but from
  the outside nothing looks broken: Gmail still polls, Transport Pro still answers, and only
  6-digit loads fail. §3.5 alarms on it specifically for that reason.

None of this blocks Stage 1. It adds one IAM statement (§3.4), three alarms (§3.5), one
build dependency (the `cargotel` extra), and two prerequisites — P8, naming an owner for the
login bot, and P9, whether to enable the path in Stage 1 at all.

### Non-goals for this plan

* **An itemised rate breakdown on 6-digit loads.** A CargoTel load carries one payable amount
  and no line items, so a rate question is answered as payment status *plus that amount* — the
  reply now states the figure and, where the sender quoted one, says whether the two agree
  (`cargotel_payment_status` 1.1.0). Until today the narrowing was only half-built: the prompt
  named dates and documents per billing state and never asked for the figure, so a Tru Funding
  rate enquiry over five loads was answered entirely in missing-paperwork wording while $2,150
  and $3,000 sat unused in the tool results. What remains out of scope is a genuine
  *breakdown*, which means scraping the Accounting tab — a separate piece of work.
* Auto-send (§8.5 Phase 2) — explicitly the LAST stage, gated on Stage 2 running clean.
* The `/load/missing_documents` cache (see MISSING_DOCUMENTS_CACHE.md) — independent.
* Owning the CargoTel login bot. If it needs to move to AWS too, that is its own plan.

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

One Lambda replicating exactly what `payment-bot-local` does every 20 minutes today: fetch unread →
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
4. **CargoTel client** — the local runner already builds one when
   `PAYBOT_CARGOTEL_REPLIES=true` and the path is configured; the handler passes
   `cargotel=` to `PaymentBotPipeline` the same way. No new code beyond the wiring, but the
   Lambda needs `boto3` (already present) *and* `beautifulsoup4` — the `cargotel` extra — in
   the deployment package.

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
| P8 | **CargoTel login bot: owner, refresh cadence, and what happens when it stops** | ops | The 6-digit path depends entirely on it. Establish who is paged when the cookie goes stale, and how quickly it is refreshed — the bot escalates rather than answering wrongly, but every 6-digit email waits until it is fixed. Also confirm the cookie object's real refresh interval: the S3 object's `LastModified` was over a year old while the cookie still worked, so the timestamp is not a health signal |
| P9 | Whether to enable `PAYBOT_CARGOTEL_REPLIES` in Stage 1 or hold it to Stage 2 | ops + eng | Recommendation: **enable in Stage 1**. It is off by default, and running it in parallel with the workstation is the cheapest way to learn its real failure rate before humans depend on it |

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
| `PAYBOT_AGENT_MAX_ITERATIONS`, `PAYBOT_AGENT_ITERATIONS_PER_EXTRA_LOAD`, `PAYBOT_AGENT_MAX_TOKENS` | Lambda env | Defaults 12 / 9 / 4096. The first two are a *base* and a *per-extra-load increment*, not a flat cap — see §3.7 for what that does to run time. 4096 tokens is fine for Claude (non-reasoning-budget); 16384 was a free-model accommodation |
| `PAYBOT_MODEL_DRAFT`, `PAYBOT_AWS_REGION` | Lambda env | Bedrock model id |
| `PAYBOT_CARGOTEL_BASE_URL`, `PAYBOT_CARGOTEL_CLIENT_URL` | Lambda env | Two back-office page URLs; not secret |
| `PAYBOT_CARGOTEL_COOKIE_BUCKET`, `PAYBOT_CARGOTEL_COOKIE_KEY` | Lambda env | Where the login bot leaves the session cookie. The cookie itself is **not** a Secrets Manager entry — it is not ours to store or rotate, only to read |
| `PAYBOT_CARGOTEL_REPLIES` | SSM `/paybot/policy/*` | A policy switch like the factoring ones: off means every 6-digit load escalates. Parameter Store so flipping it is deliberate and audited |
| `PAYBOT_AWS_PROFILE` | Lambda env, **blank** | Exists because boto3 reads the *process environment* and never `.env`, so on a workstation the CargoTel cookie read fails with "Unable to locate credentials" unless a profile is named. **Leave it empty in Lambda**: there are no profiles there, the execution role is the credential source, and naming one that does not exist would break the cookie read outright |
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
* `s3:GetObject` on `arn:aws:s3:::circle-bot-cookies/rubicon/cargotel.json` — the CargoTel
  session cookie. Scope it to that one key, not the bucket: the worker never needs another
  bot's cookies and never needs to write. This is the only cross-system grant in the plan,
  since the bucket belongs to the login bot rather than to us
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
| `CargoTelCookieStale` | escalation reason containing `session cookie is probably expired`, or `cargotel_carrier_unreadable` | **Any → page.** This is the alarm that matters most on the new path: nothing else looks broken while it fires, and every 6-digit email is waiting on it |
| `CargoTelCookieUnavailable` | the `cargotel_cookie_unavailable` log event | **Any → page.** Distinct from the alarm above: this one means the cookie could not be *read* (credentials, permissions, missing object) rather than that it was read and rejected. The log entry carries the bucket, key and profile; the escalation carries only the action, deliberately. Should be near-impossible in Lambda — the execution role does not expire — so a hit here means an IAM or bucket-policy problem, not a stale login |
| `CargoTelNotALoad` | escalation reason containing `Invalid Order ID` **or** `no order on it` | Digest only — routine. A 6-digit number in an email is often the sender's own invoice or reference number rather than a load. **Both phrases are needed:** CargoTel has two ways of saying "not a load" — the explicit message, and a 200 with the real load form and no order in it, which is the quieter and more common of the two |
| `SenderInvoiceIdDropped` | the `sender_invoice_id_dropped` log event | Digest only. An id was withheld from lookup because it was the sender's own invoice number pulled into the wrong system. Watch the rate: a rise means senders' reference formats are drifting, and each one used to cost an escalation |
| `CargoTelParseFailures` | `ClientError` from `parse_load_html` / `parse_carrier_html` other than the cases above | >2/day → investigate: the most likely cause is CargoTel changing its markup |

`CargoTelCookieStale` and `CargoTelNotALoad` must stay separate alarms even though both
surface as escalations. One is systemic and urgent, the other is a carrier writing an invoice
number in an email. Collapsing them into one "CargoTel escalations" metric means the urgent
case is buried in routine noise — the first live run of this path produced exactly one of
each, and the routine one was initially *reported* as the urgent one, which is why the
distinction is drawn in code rather than left to whoever reads the alarm.

`PolicyAllowedChangeWording` deserves more weight than its "daily digest" suggests. It fires
when an email carrying a bank or NOA instruction was drafted anyway because
`PAYBOT_SENSITIVE_BANK_REPLIES` / `_NOA_REPLIES` allow it. The gate guarantees the *draft*
never acknowledges the instruction; nothing guarantees anyone *actions* it. A live example the
same day: a factoring company announced changed banking details with a full account and routing
number alongside a routine rate question, and the bot drafted a correct answer that mentioned
none of it. Sending that reply and closing the thread would have lost the request silently.
Treat each entry as a task, not a log line.

Plus a weekly *capability report*: the ESCALATIONS.md §6 audit run as a read-only scheduled
job, publishing the answerable/escalated breakdown — the number that shows whether the
roster and checks are keeping up with real mail.

### 3.6 CI/CD

GitHub Actions on the repo (branches already in use):

1. **On PR**: `ruff check` + `mypy` + `pytest` (624 tests, no network — the suite is already
   hermetic thanks to `isolate_settings`). The CargoTel parser tests run against a synthetic
   page fixture rather than saved real pages: real ones carry carrier names, contact emails,
   VINs and payable amounts, and this repository is public. The blank-form fixture is built
   from the same `build_page` helper with its values emptied, because the guard it exercises
   exists to tell a real *form* from a real *load* — a hand-written stub would not test it.
2. **On merge to `main`**: `sam build && sam deploy` to a **staging stack** pointed at a
   test mailbox + Transport Pro sandbox credentials (or the mock client if no sandbox
   exists), then manual promotion to prod.
3. Deploy artifact carries no secrets and no roster — both are runtime-fetched. It **does**
   need the `cargotel` extra (`beautifulsoup4`) baked in; the CargoTel client fails closed
   with an actionable error if it is missing, but that is a runtime discovery of a build
   mistake.

### 3.7 Runtime shape & limits

* **The turn budget scales with load count, so "one email" is not one cost.** `_iteration_budget`
  gives `agent_max_iterations + (loads − 1) × 9`, clamped by `ITERATION_CEILING = 50`. On today's
  defaults (`agent_max_iterations = 12`) that is 12 turns for a single load, 21 for two, 30 for
  three, 48 for five and 50 from eight upward. A single-load email is the ~30–90 s case; a
  five-load factoring enquiry — an ordinary shape, not a pathological one — can be four times
  that on its own. `PAYBOT_GMAIL_FETCH_LIMIT=10` therefore does **not** bound a run to ~12 min
  the way the original estimate assumed: ten multi-load emails could exceed Lambda's 15-min cap
  outright. Mitigations, in order: drop the fetch limit to 5 (the 20-minute cadence absorbs it
  easily — three times the runs), then measure real per-email wall time in Stage 1 before
  choosing between a lower ceiling and moving the worker to a scheduled Fargate task (same
  container, no time cap). **Measure this in Stage 1 parallel running specifically**; it is the
  most likely reason Stage 1 needs Fargate rather than Lambda.
* Concurrency **1** on the worker (reserved concurrency) — not for safety (thread-skip
  makes concurrent runs converge) but to keep Gmail API usage and logs sane.
* Cold start: SA-JWT mint + roster fetch ≈ 1–2 s; irrelevant at this cadence. CargoTel adds
  one S3 read for the cookie per client (not per load), then two page GETs per 6-digit load
  — the load page and its carrier record — of roughly 250 KB each. Both are cached per email,
  so three loads from the same carrier cost four fetches, not six.

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
| S3 (roster + CargoTel cookie reads) | ~$0 | A few GETs per run; negligible |
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
| **CargoTel login bot stops or its cookie goes stale** | The client raises rather than parsing the login page as an empty load, so emails escalate instead of being answered wrongly. `CargoTelCookieStale` pages immediately. **Not mitigable from inside this system** — P8 exists to name an owner |
| **CargoTel changes its HTML** | The parser is a pure function with fixture-backed tests, so a break is loud and reproducible offline rather than a mystery in production. `CargoTelParseFailures` catches it. Residual risk: a *silent* change — a field that moves rather than disappears. The load-bearing selectors key off form-field names, which are far more stable than layout |
| Scraping treated as a stable integration | It is not. Budget for parser maintenance, and prefer an API if CargoTel ever exposes one |
| A 6-digit number that is not a load | Routine and handled in two shapes: CargoTel either answers "Invalid Order ID" or returns the real load form with no order in it, and both now raise with a reason naming the likely cause — a sender's own invoice or reference number. Worth watching the rate, since each one costs a fetch. `_drop_stray_sender_invoice_ids` removes the narrow case that used to cost a whole escalation, where a sender's invoice number dragged an otherwise single-system email into a cross-system refusal |
| **A 6-digit id that exists in BOTH systems** | Real and measured: Transport Pro numbered loads with six digits years ago and still serves them, so 316040 is Ma Trucks in Transport Pro *and* Continental Autoshipping in CargoTel. Every 6-digit Transport Pro load found had settled in 2018–19 while the CargoTel loads sharing those numbers were delivered 2026-08-10 and unpaid, so preferring CargoTel is correct for any live question and routing is unchanged. **The trap is the obvious-looking fix**: adding a Transport Pro fallback when CargoTel has no such load would resolve arbitrary numbers onto strangers' archived loads — 999998, 111111, 222222 and 555555 are all real, distinct Transport Pro loads — and answering one would disclose an unrelated carrier's payment history. Ruled out with the evidence in `payment_bot.domain.routing`, and locked by a test asserting `route_load` consults no client |
| A wrong weekday on a correct date | Closed by gate check 12. Grounding compares dates and never the words beside them, so a fabricated weekday on a grounded date passed all eleven earlier checks and reached Drafts. The pay-date tool now emits one preformatted string for the reply to copy, so there are no longer two fields to mis-pair |
| A load id that exists only in an email's HTML | Closed. `InboundEmail.html_text` strips tags — never parses them — so attribute values (URLs, tracking ids, widths, colours) are discarded and only text a human would have read is scanned. Feeds the sensitive-change scan too, since the mirror case is the dangerous one: a bank instruction present only in the HTML would otherwise pass unseen |

---

## 7. Open items feeding this plan

* [ ] P1–P7 prerequisite decisions (§3.1)
* [ ] Confirm Transport Pro allows API calls from AWS egress IPs (no allowlist observed, verify)
* [ ] Shared mailbox migration (P5) — removes the personal-mailbox `to:` guard caveat
* [ ] DynamoDB `AuditSink` implementation + tests (the one new component, Stage 2)
* [ ] Slack app manifest + the two channels (Stage 2)
* [ ] Staging mailbox + seeded test threads for CI verification
* [ ] **P8: CargoTel login-bot owner, refresh cadence, and paging path** — the one dependency
      this plan cannot satisfy itself
* [ ] Decide P9: enable `PAYBOT_CARGOTEL_REPLIES` in Stage 1 (recommended) or hold it
* [ ] Add Saint John Capital's sending domain to the factoring roster if 6-digit factored
      loads should be answered — most carriers on this path factor to them, so without it
      their enquiries escalate. The roster entry exists; confirm the domain is the one they
      actually mail from
* [x] ~~Consider extending `_NOT_A_LOAD_LABEL_RE` to suppress invoice/account numbers before
      they reach a CargoTel fetch~~ — done differently, and deliberately narrower.
      `invoice` cannot become a suppression label because carriers write "Invoice 2462934"
      meaning a real Transport Pro load, and refusing those would be a false negative, worse
      than an escalation. `_drop_stray_sender_invoice_ids` instead drops a sender-invoice id
      only when it disagrees about *which system* the email is about. Account and routing
      labels are suppressed by the label rule as before
* [ ] **Confirm the CargoTel rate reply end to end against the live model.** The prompt now
      requires the amount and the intake carries the ask through, verified under a scripted
      model — but not yet against Bedrock or OpenRouter on a real rate enquiry, because the
      only one available is a thread the bot's own draft already owns
* [ ] Verify the two hand-added roster domains that rest on a sender's own signature rather
      than on a settlement record — `afgfactor.com` and `aladdincap.com`. Both companies are
      corroborated by the payee table; the domains are not. `_evidence` in
      `factoring_domains_manual.json` records which is which
