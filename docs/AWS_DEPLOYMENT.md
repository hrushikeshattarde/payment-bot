# AWS Deployment & API Guide

How to run the Payments Email Bot on AWS, which services and external APIs it needs, how
credentials flow, and how the code we built maps onto Lambda functions.

- Companion doc: [LOCAL_SETUP.md](LOCAL_SETUP.md) — local install & run.
- Spec: [`../PRD_Agent_Skills_and_Tools_Catalog_v1.2.md`](../PRD_Agent_Skills_and_Tools_Catalog_v1.2.md) — see §8.1 (runtime) and §8.1.1 (low-cost service map).

> **Design note.** We deploy the **portable tool-use loop** we built (not managed Bedrock
> Agents). Tools run *in-process* inside the processor Lambda as plain Python; the Lambda
> calls Amazon Bedrock only for the model turns. This keeps the deterministic pre-send gate
> (§5) and the §4.1.1 math as auditable code and reduces moving parts. Everything is
> pay-per-use — there is no always-on server.

---

## 1. Architecture on AWS

```
                          EventBridge Scheduler  (cron, e.g. every 2 min)
                                     │ invokes
                                     ▼
                          ┌─────────────────────┐        Gmail API (fetch new mail)
                          │  Poller Lambda        │◀──────────────────────────────────
                          │  gmail_fetch_new      │
                          └──────────┬────────────┘
                                     │ one SQS message per email
                                     ▼
                          ┌─────────────────────┐
                          │      SQS queue        │   (decouple + retry + DLQ)
                          └──────────┬────────────┘
                                     ▼
        Bedrock Runtime  ◀──────┌─────────────────────┐──────▶ Transport Pro API
        (Converse)             │  Processor Lambda     │──────▶ QuickBooks Online API
                               │  PaymentBotPipeline    │
        Secrets/SSM  ◀─────────│  intake → agent loop   │──────▶ Slack API (post approval)
                               │  → pre-send gate (§5)  │
                               │  → post to Slack, STOP │──────▶ DynamoDB (run-state + audit)
                               └─────────────────────┘
                                     ▲                 │ persists draft + gate inputs
                                     │ Approve/Edit/Reject (Block Kit button)
                          ┌──────────┴────────────┐
   Slack  ──────────────▶│ Slack-Callback Lambda  │──────▶ re-run gate → Gmail API (send)
   (Interactivity)        │ (Function URL / APIGW)│──────▶ DynamoDB (mark sent)
                          └───────────────────────┘
```

**Why three Lambdas.** Sending email is the one irreversible action, and in Phase 1 it
happens only *after a human clicks Approve in Slack* — which is asynchronous. So the flow
is split: the **Processor** produces and gate-checks the draft, then stops after posting to
Slack; the **Slack-Callback** resumes on approval, re-runs the gate, and sends. The
**Poller** just turns "new mail" into work items.

### Service map (PRD §8.1.1)

| Job | AWS service | Why |
|---|---|---|
| Scheduled inbox poll | **EventBridge Scheduler** → Lambda | No idle compute; runs on a schedule |
| Work decoupling + retries | **SQS** (+ DLQ) | Isolates failures; automatic retry |
| Agent orchestration (model turns) | **Amazon Bedrock** (Converse) | Token-metered only |
| Classify / extract (high volume) | **Claude Haiku** on Bedrock | Cheap model for frequent steps |
| Final reply drafting | **Claude Sonnet** on Bedrock | Larger model, last step only |
| Tool handlers + gate + pipeline | **AWS Lambda** (Python) | Billed per invocation |
| Slack interactivity callback | **Lambda + Function URL / API Gateway** | Event-driven |
| Audit log (every tool call) | **DynamoDB** or **S3** | Cheap; required for grounding audit |
| Run state (correlation → draft) | **DynamoDB** (on-demand) | Pay-per-request |
| Secrets (TP/QBO/Slack/Gmail) | **SSM Parameter Store** or **Secrets Manager** | Least-privilege secret access |
| Logs / metrics | **CloudWatch** | Observability |

---

## 2. Lambda functions in detail

| Function | Trigger | Responsibility | Code it runs |
|---|---|---|---|
| **Poller** | EventBridge Scheduler (cron) | Call Gmail `users.messages.list` for unread/new mail in the mailbox; for each, enqueue an SQS message with the raw message ref. Optionally label as "queued" to avoid re-processing. | A thin handler around a real `GmailClient.fetch_new`. |
| **Processor** | SQS | Build `PaymentBotPipeline` with real clients + `BedrockLlmClient`; run `process_email`. In Phase 1 it **stops at "post to Slack"** — it does not send. Persist the draft + minimal gate inputs to DynamoDB keyed by `correlation_id`. | `payment_bot.pipeline.PaymentBotPipeline` + real client adapters. |
| **Slack-Callback** | Function URL / API Gateway (HTTPS POST from Slack) | Verify the Slack signature; load run-state from DynamoDB; on **Approve** (or **Edit**) re-run the pre-send gate, then call Gmail `messages.send`; on **Reject** mark rejected. Update Slack message. | Re-uses `PreSendGate` + a real `GmailClient.send_reply`. |

> **Phase 2 selective auto-send (§8.5):** for a clean single-load `payment_status` that
> passes the gate, the Processor may send directly (skipping the Slack round-trip). Rate
> mismatches and all sensitive cases still route through the human. `PaymentBotPipeline`
> already encodes this via `rollout_phase` and `_is_auto_sendable`.

### Resuming across the async gap (run-state)

Because approval is asynchronous, the Processor persists what the Callback needs so it can
send **without** re-invoking the model:

| DynamoDB item (table `paybot-runstate`) | Purpose |
|---|---|
| `correlation_id` (PK) | = the email `message_id`. |
| `thread_id`, `message_id`, `to` | For threading the Gmail reply. |
| `draft_body`, `load_ids`, `skill_id` | The reply to send after approval. |
| `grounding_facts` (serialized ledger) | So the Callback re-runs the gate cheaply (no LLM). |
| `status` | `awaiting_approval` → `sent` / `rejected` / `escalated`. |
| `ttl` | Auto-expire stale items. |

The Callback rebuilds a `ToolContext` with a ledger pre-loaded from `grounding_facts`,
re-runs `PreSendGate.evaluate` (authorization re-checks against Transport Pro; grounding
re-checks the persisted facts), and only then sends.

---

## 3. Handlers to add (code → Lambda)

These are thin entrypoints you add on top of the existing package (nothing in the core
changes). Suggested new module `src/payment_bot/aws/`:

```python
# src/payment_bot/aws/processor.py  (SQS-triggered)
def handler(event, context):
    settings = load_settings_from_ssm()          # you write this
    clients = build_real_clients(settings)        # real TP/Gmail/Slack adapters
    pipeline = PaymentBotPipeline(
        tp=clients.tp, gmail=clients.gmail, slack=clients.slack,
        llm=BedrockLlmClient(settings.model_draft, settings.aws_region),
        approval_resolver=DynamoDeferredResolver(...),  # posts + defers, returns "pending"
        settings=settings, audit_sink=DynamoAuditSink(...),
    )
    for record in event["Records"]:
        email = parse_email_ref(record)           # fetch full message via Gmail
        pipeline.process_email(email)             # posts to Slack, persists run-state

# src/payment_bot/aws/poller.py       (EventBridge-triggered)  -> lists mail, enqueues SQS
# src/payment_bot/aws/slack_callback.py (Function URL)         -> verify sig, re-gate, send
```

The `AuditSink` and approval flow are already abstractions (`logging.AuditSink`,
`clients.ApprovalResolver`) — you implement DynamoDB-backed versions behind them.

---

## 4. Data stores & secrets

| Store | Contents | Notes |
|---|---|---|
| **DynamoDB `paybot-runstate`** | Per-email run state (table above) | On-demand capacity; TTL enabled. |
| **DynamoDB `paybot-audit`** *(or S3)* | Every tool call + result (§8.1) | PK `correlation_id`, SK `seq#`. S3 is fine for write-once archival. |
| **SQS `paybot-work`** (+ DLQ) | One message per inbound email | Visibility timeout ≥ processor timeout. |
| **SSM Parameter Store / Secrets Manager** | All secrets (table in §5) | Parameter Store `SecureString` is cheapest at this volume; Secrets Manager if you want rotation. |

---

## 5. APIs you will need

This is the full inventory. Split into **AWS service APIs** (consumed via the AWS SDK / IAM)
and **external third-party APIs** (consumed over HTTPS with their own credentials).

### 5.1 AWS service APIs

| API / service | Used for | Key operations | IAM actions (least-privilege) |
|---|---|---|---|
| **Amazon Bedrock Runtime** | The model turns in the agent loop | `Converse` (and `ConverseStream`) | `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` on the model + inference-profile ARNs |
| **AWS Lambda** | Hosts poller / processor / callback | invoke/execute | managed by triggers; functions need an execution role |
| **Amazon EventBridge Scheduler** | Cron trigger for the poller | `CreateSchedule` (deploy-time) | `scheduler:*` at deploy; runtime role `lambda:InvokeFunction` |
| **Amazon SQS** | Work queue + DLQ | `SendMessage`, `ReceiveMessage`, `DeleteMessage` | `sqs:SendMessage` (poller), `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` (processor) |
| **Amazon DynamoDB** | Run-state + audit | `PutItem`, `GetItem`, `UpdateItem`, `Query` | scoped to the two table ARNs |
| **Amazon S3** *(optional)* | Audit archive | `PutObject` | scoped to the audit bucket/prefix |
| **AWS SSM Parameter Store** *(or Secrets Manager)* | Read secrets at cold start | `GetParameter`/`GetParameters` (`ssm`) or `GetSecretValue` (`secretsmanager`) | scoped to `paybot/*` param path / secret ARNs; `kms:Decrypt` for SecureString |
| **Amazon API Gateway (HTTP API)** *(or Lambda Function URL)* | Public HTTPS endpoint for Slack interactivity | route → callback Lambda | Function URL is simplest; API Gateway if you want WAF/custom domain |
| **Amazon CloudWatch** | Logs + metrics + alarms | `PutLogEvents`, `PutMetricData` | included in the basic Lambda execution role |
| **AWS KMS** *(optional)* | Encrypt secrets / tables | `Decrypt`, `GenerateDataKey` | only if using CMKs instead of AWS-managed keys |

### 5.2 External / third-party APIs

| API | Used for | Auth model | Scopes / permissions | Endpoints the client needs | Where the secret lives |
|---|---|---|---|---|---|
| **Gmail API** (Google Workspace) | Read the inbox; send replies | **Service account with domain-wide delegation**, impersonating `paystatus@circledelivers.com` (§8.1.2) — server-side JWT, no interactive OAuth | `gmail.readonly` **or** `gmail.modify` (to label/thread) + `gmail.send` (least privilege — no delete) | `users.messages.list`, `users.messages.get`, `users.messages.send`, `users.threads.get` | Service-account JSON key in Secrets Manager; delegation configured in the Google Admin console |
| **Slack API** | Post approval/escalation with Block Kit buttons; receive button clicks | **Bot token** (`xoxb-…`) for Web API; **signing secret** to verify inbound interactivity requests | Bot scopes: `chat:write` (+ `chat:write.customize` if needed) | `chat.postMessage`, `chat.update`; Interactivity **Request URL** → your callback Lambda | Bot token + signing secret in SSM/Secrets Manager |
| **Transport Pro API** (7-digit loads) | Load summary (§4.3.0 payload), dispatch history, file history — see the endpoint table below | **Token flow, implemented:** `POST /auth` with HTTP Basic → `access_token` + `refresh_token`; all reads send `Authorization: Bearer`. Refresh-token grant on 401. | Read-only | `GET /voiceai/load/{n}/payment_information`, `GET /dispatch/search?loadId={n}`, `GET /files/search?recordType=loads&recordId={internal id}` | API user + password in SSM `SecureString` / Secrets Manager (`PAYBOT_TP_USERNAME` / `PAYBOT_TP_PASSWORD`) |
| **QuickBooks Online API** (6-digit loads) | Payment status / line items for QBO-owned loads | **OAuth2** (authorization-code + refresh token) | Accounting read | `query` for `Bill`/`Invoice`, entity reads | OAuth client id/secret + refresh token in Secrets Manager |

> **Transport Pro is implemented** in
> [`clients/transport_pro_http.py`](../src/payment_bot/clients/transport_pro_http.py) —
> build it with `build_transport_pro_client()` from `PAYBOT_TP_*` config, one client per
> email. `payment_information` is the primary read: it returns the full §4.3.0 payload, so
> a single call serves both skills. Three facts have **no endpoint** in the Public API and
> are derived from that payload rather than invented — settlement entries (from settled
> earning lines), NOA/factoring (from `remit_to` + factoring documents), and authorized
> parties (carrier company + dispatch contact emails). The remaining §9 open item for TP is
> confirming the **base URL** and whether `payment_information` is keyed by the
> carrier-facing load number or the internal record id (the client keys by the number from
> the email and never trusts the echoed id). The QBO (6-digit) path and carrier-name lookup
> are still not built.

### 5.3 Config → secret mapping

| Setting (`PAYBOT_*` / secret) | Source in AWS |
|---|---|
| `PAYBOT_MODEL_DRAFT`, `PAYBOT_AWS_REGION`, `PAYBOT_ROLLOUT_PHASE`, thresholds, channels | Lambda env vars or SSM plaintext params |
| `PAYBOT_TP_BASE_URL`, `PAYBOT_TP_USERNAME`, `PAYBOT_TP_TIMEOUT_SECONDS` | Lambda env vars or SSM plaintext params |
| Slack bot token, Slack signing secret | SSM `SecureString` / Secrets Manager |
| Gmail service-account JSON | Secrets Manager |
| `PAYBOT_TP_PASSWORD` (Transport Pro API password) | SSM `SecureString` / Secrets Manager |
| QuickBooks OAuth client id/secret + refresh token | Secrets Manager (rotation recommended) |

---

## 6. IAM roles (least-privilege)

One execution role per function. Sketches:

**Poller role**
- `ssm:GetParameter*` on `paybot/gmail/*` (+ `kms:Decrypt`)
- `sqs:SendMessage` on `paybot-work`
- CloudWatch Logs (basic)

**Processor role**
- `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` on the model + inference-profile ARNs
- `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` on `paybot-work`
- `dynamodb:PutItem`, `UpdateItem`, `GetItem`, `Query` on `paybot-runstate` + `paybot-audit`
- `ssm:GetParameter*` / `secretsmanager:GetSecretValue` on `paybot/*` (+ `kms:Decrypt`)
- CloudWatch Logs

**Slack-Callback role**
- `dynamodb:GetItem`, `UpdateItem` on `paybot-runstate`
- `ssm:GetParameter*` / `secretsmanager:GetSecretValue` on `paybot/gmail/*`, `paybot/slack/*`
- (No Bedrock — it never calls the model)
- CloudWatch Logs

Scope every resource ARN explicitly; avoid `*`. The Slack-Callback deliberately has **no
Bedrock and no TP write** access — it only re-gates and sends.

---

## 7. Amazon Bedrock setup

1. **Enable model access.** Console → Bedrock → *Model access* → request/enable the
   Anthropic Claude models you use in the deployment region.
2. **Use inference profiles / current model IDs.** The defaults in
   [`config.py`](../src/payment_bot/config.py):
   - fast: `us.anthropic.claude-haiku-4-5-20251001-v1:0`
   - draft: `us.anthropic.claude-sonnet-5-v1:0`

   Verify the exact IDs available in your account/region (`aws bedrock list-foundation-models`
   / `list-inference-profiles`) and update the settings. Cross-region inference profiles
   require IAM permission on **both** the profile ARN and the underlying foundation-model
   ARNs in the member regions.
3. **The API is `Converse`.** `BedrockLlmClient` already speaks it — tool specs, tool
   results, and stop reasons are mapped in [`clients/llm.py`](../src/payment_bot/clients/llm.py).
4. **Quotas.** Check the requests-per-minute / tokens-per-minute limits for your models and
   request increases if the mailbox volume warrants it.

---

## 8. Deployment

Recommended: **AWS SAM** (closest to the §8.1.1 pay-per-use map, minimal boilerplate).
CDK (Python) or Terraform work equally well — the resources are the same.

### 8.1 Package layout to add

```
Payment Bot/
├── template.yaml               # SAM: 3 functions + SQS + DynamoDB + schedule + Function URL
├── src/payment_bot/aws/
│   ├── poller.py               # EventBridge → list mail → SQS
│   ├── processor.py            # SQS → PaymentBotPipeline → Slack + run-state
│   └── slack_callback.py       # Function URL → verify sig → re-gate → Gmail send
└── (existing package unchanged)
```

### 8.2 `template.yaml` skeleton (illustrative)

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Globals:
  Function:
    Runtime: python3.12
    Timeout: 60
    MemorySize: 512
    Environment:
      Variables:
        PAYBOT_AWS_REGION: !Ref AWS::Region
        PAYBOT_ROLLOUT_PHASE: "1"
Resources:
  WorkQueue:
    Type: AWS::SQS::Queue
  RunState:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      AttributeDefinitions: [{ AttributeName: correlation_id, AttributeType: S }]
      KeySchema: [{ AttributeName: correlation_id, KeyType: HASH }]
      TimeToLiveSpecification: { AttributeName: ttl, Enabled: true }
  PollerFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: payment_bot.aws.poller.handler
      Events:
        Cron:
          Type: ScheduleV2
          Properties: { ScheduleExpression: "rate(2 minutes)" }
      Policies: [ AWSLambdaBasicExecutionRole, { SQSSendMessagePolicy: { QueueName: !GetAtt WorkQueue.QueueName } } ]
  ProcessorFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: payment_bot.aws.processor.handler
      Events:
        Work: { Type: SQS, Properties: { Queue: !GetAtt WorkQueue.Arn } }
      Policies:
        - AWSLambdaBasicExecutionRole
        - { DynamoDBCrudPolicy: { TableName: !Ref RunState } }
        - Statement:
            - Effect: Allow
              Action: [ "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream" ]
              Resource: "*"     # tighten to model/profile ARNs
  SlackCallbackFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: payment_bot.aws.slack_callback.handler
      FunctionUrlConfig: { AuthType: NONE }     # Slack signature is the auth
      Policies: [ AWSLambdaBasicExecutionRole, { DynamoDBCrudPolicy: { TableName: !Ref RunState } } ]
```

Add the SSM/Secrets read policies, the audit table/bucket, and the DLQ as you flesh it out.

### 8.3 Build & deploy

```bash
sam build
sam deploy --guided        # first time: pick stack name, region, confirm IAM
```

After deploy, take the **Slack-Callback Function URL** from the stack outputs and set it as
the **Interactivity Request URL** in your Slack app config.

### 8.4 Dependencies in the Lambda package

The Lambda build must include the `aws` extra so `boto3` (already in the Lambda runtime) and
`pydantic`/`pydantic-settings` are present. Point SAM at `pyproject.toml` (or a
`requirements.txt` generated from it) so the package installs with `.[aws]`.

---

## 9. Phased rollout on AWS (§8.5)

| Phase | AWS behaviour |
|---|---|
| **Phase 1 — Approve** | Processor always posts to Slack and stops; Slack-Callback sends on Approve. Set `PAYBOT_ROLLOUT_PHASE=1`. |
| **Phase 2 — Selective auto-send** | Processor auto-sends clean single-load `payment_status` that passes the gate; everything else still goes to Slack. Set `PAYBOT_ROLLOUT_PHASE=2`. Rate mismatches, sensitive changes, factoring, bulk, and any gate failure are **never** auto-sent. |

The pre-send gate runs in **every** phase and is never bypassed.

---

## 10. Observability, cost, security

**Observability.** All logs are JSON (parseable in CloudWatch Logs Insights). Every tool
call + result is written to the audit store (§8.1) keyed by `correlation_id`. Track the
§8.5 metrics per intent: approval / edit / reject / post-send-correction / gate-block rates.

**Cost.** Everything is pay-per-use; there is no idle server. The primary lever (§8.1.1):
route `classify_intent` and `extract_identifiers` to the **cheap** model, do all lookups
deterministically in code, and invoke the **larger** model only for the final draft. Most
emails never need the larger model.

**Security checklist.**
- Secrets only in SSM `SecureString` / Secrets Manager — never in code, env files, or logs.
- Verify the Slack signing secret on every callback request; reject stale timestamps.
- Least-privilege IAM per function; the Callback has no Bedrock access.
- Gmail scope is read + send only (no delete); service-account delegation limited to the
  one mailbox.
- The pre-send gate is the last line of defence: authorization, fraud/sensitive-change,
  grounding, length, and bulk all re-checked in code before any send — fail closed.

---

## 11. Pre-deploy checklist (maps to PRD §9)

- [ ] Transport Pro **base URL** confirmed and set as `PAYBOT_TP_BASE_URL` (the client is
      implemented; endpoints and the `POST /auth` token flow are wired).
- [ ] Confirm with Transport Pro whether `/voiceai/load/{n}/payment_information` is keyed by
      the carrier-facing load number or the internal record id, and run one live call
      against a known load to check the returned `load_id` against the number requested.
- [ ] Confirm the derived facts are acceptable, or get real endpoints for them: settlement
      entries, NOA/factoring, and the authorized-parties allow-list.
- [ ] QuickBooks object/field for the 6-digit load number confirmed → implement QBO tools.
- [ ] Gmail service account created with domain-wide delegation to `paystatus@` + scopes granted.
- [ ] Slack app created: Bot token, Interactivity enabled, signing secret, channels created.
- [ ] Factoring authorization policy decided (ALLOW vs escalate) → set `allow_factoring`.
- [ ] Bedrock model access enabled; model IDs verified for the region.
- [ ] Secrets loaded into SSM/Secrets Manager; IAM roles scoped.
- [ ] Reply templates + Samples A–L imported from `PRD_Payment_Status_Email_Bot.md` for
      regression fixtures (companion PRD, not yet in this workspace).
