# AWS Deployment Checklist

A tick-through companion to [AWS_DEPLOY_RUNBOOK.md](AWS_DEPLOY_RUNBOOK.md), updated with
what an actual first deployment (2026-08-18) hit that the runbook didn't predict. The
runbook explains *why*; this file is the *do this, in this order* list. Values that are
account-specific are written as `<account>` etc. — this repository is public, so real
identifiers stay in the gitignored `deploy/params.prod.json`.

**Time budget:** half a day for a first deployment, ~10 minutes for a repeat one.

---

## Phase 0 — Prerequisites (once per machine / person)

- [ ] **AWS CLI installed** — `aws --version` answers.
- [ ] **CLI authenticated** — `aws sts get-caller-identity` prints the right account.
      SSO logins expire; if any command fails with `Token has expired`, run `aws sso login`.
- [ ] **The identity can create resources.** An analyst-flavoured SSO role
      (`DataScientist`, `Analyst`, `ReadOnly`) **will fail** — observed: CloudFormation
      denied on `secretsmanager:CreateSecret`, which cancels the whole stack. Either use an
      admin role/profile, or hand the runbook's Appendix A policy to the AWS admin.
      A failed first create leaves the stack in `ROLLBACK_COMPLETE`, which must be deleted
      before retrying: `aws cloudformation delete-stack --stack-name paybot-prod`.
- [ ] **Python 3.11+** available (the repo's `.venv` counts).
- [ ] **Credentials at hand** (for Phase 4, human-entered only): the Google
      service-account key JSON file, and the Transport Pro password.
- [ ] Nothing else — no Docker, no SAM, no Node.

## Phase 1 — Bedrock model access (once per account)

- [ ] **Do NOT look for the "Model access" console page — it was retired in 2026.**
      Serverless models now enable themselves on first invocation.
- [ ] **Clear the one-time Anthropic use-case form** (new accounts only): Console →
      Bedrock → Model catalog → the Claude model → *Open in playground* → send "hello".
      If a form appears, fill it once; if the playground answers, access is already live.
      Skipping this makes the worker's first model call fail with `AccessDeniedException`.
- [ ] **Copy the exact inference-profile id — never guess it:**

      aws bedrock list-inference-profiles --region us-east-1 --query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude')].inferenceProfileId" --output table

      The suffix is not uniform across models (`us.anthropic.claude-sonnet-4-6` is plain;
      `...-sonnet-4-5-20250929-v1:0` is dated). A wrong id deploys cleanly and only fails
      at the first model call.

## Phase 2 — Config bucket (once per account)

- [ ] Create it: `aws s3 mb s3://paybot-config-<account>`
- [ ] Upload both rosters from the repo root:
      `aws s3 cp factoring_domains.json s3://paybot-config-<account>/factoring_domains.json`
      `aws s3 cp carrier_contacts.json s3://paybot-config-<account>/carrier_contacts.json`
- [ ] Note: later roster updates are just a re-run of the `cp` — no redeploy; the worker
      re-fetches at its next cold start.

## Phase 3 — Parameters file

- [ ] `Copy-Item deploy\params.example.json deploy\params.prod.json` (gitignored — real
      values are safe here and only here).
- [ ] `GmailUser` — a **real licensed user, never the group** (a Google Group cannot be
      impersonated; the failure is a misleading `unauthorized_client`).
- [ ] `GmailQuery` — `is:unread to:<group-address>` plus a freshness window
      (e.g. `newer_than:2d`) if the mailbox carries old unread backlog.
- [ ] `FetchLimit` — a cap, not a target. Anything a single run cannot finish inside
      Lambda's 15-minute ceiling is killed mid-email (mail survives unread; the next run
      retries), so keep it modest unless a time-budget guard is in the handler.
- [ ] `TransportProBaseUrl` / `TransportProUsername` — from the local `.env`. The
      password does **not** go here.
- [ ] `BedrockModelId` — the exact string Phase 1 printed.
- [ ] `RosterBucket` — the Phase 2 bucket name.
- [ ] `ReplyCc` — **must be a JSON list in a string**, e.g. `"[\"me@example.com\"]"`.
      A bare address crashes the worker at startup (`SettingsError: reply_cc`) because
      pydantic-settings json-parses tuple fields straight from the env var.
- [ ] `AlarmEmail` — where failure alerts go.
- [ ] `ScheduleEnabled` — **"false"** for the first deploy. Always.

## Phase 4 — Deploy

- [ ] Preview first: `.\deploy\deploy.ps1 -Plan -Region us-east-1` — read the changeset
      (first ever run also builds the package and creates the artifact bucket).
- [ ] Real deploy: `.\deploy\deploy.ps1 -Region us-east-1` (add `-AwsProfile <name>` if
      the admin credentials live in a named profile).
- [ ] Windows PowerShell 5.1 note: the script carries deliberate workarounds (local
      `$ErrorActionPreference` drops, `file://` for JSON arguments, ASCII parameter
      files — commit `6d12679`). If editing the script, do not reintroduce inline JSON
      arguments or BOM-writing `-Encoding utf8`, and never let a wrapped `aws` call run
      under the global `Stop` preference.
- [ ] Success = stack outputs printed, `ScheduleState DISABLED`.

## Phase 5 — Secrets (human-entered, never scripted, never in files)

- [ ] Google key (from the repo root, path per your checkout):
      `aws secretsmanager put-secret-value --secret-id paybot/prod/google-sa-json --secret-string file://<service-account>.json`
- [ ] Transport Pro password (single quotes; double any embedded single quote):
      `aws secretsmanager put-secret-value --secret-id paybot/prod/tp-password --secret-string '<password>'`
- [ ] Each reply contains a `VersionId` — that is the success signal.
- [ ] Until both are real, the worker **fails closed** on its first Gmail call — by design.

## Phase 6 — Alarm email subscription

- [ ] Click **Confirm subscription** in the AWS email.
- [ ] **Watch for a follow-up "Unsubscribe Confirmation"** — corporate mail scanners
      follow every link in the email, including AWS's one-click unsubscribe, silently
      deactivating the alerts. If it happens: click *Resubscribe*, and confirm via CLI
      with unsubscribe-protection instead of clicking the email link:
      `aws sns confirm-subscription --topic-arn <AlarmTopicArn> --token <token-from-confirm-link> --authenticate-on-unsubscribe true`
- [ ] Verify: `aws sns list-subscriptions-by-topic --topic-arn <AlarmTopicArn>` shows the
      address with a real ARN (not `PendingConfirmation`).

## Phase 7 — Smoke test (one email, live pipeline)

- [ ] `aws lambda invoke --function-name paybot-worker-prod --payload '{\"limit\":1}' --cli-binary-format raw-in-base64-out response.json`
- [ ] `response.json` reads like `{"processed": 1, "outcomes": {"awaiting_review": 1}}`.
      Outcome meanings: `awaiting_review` good · `escalated` fine (deliberate) ·
      `blocked` read the gate reason · `sent` should be impossible — investigate.
- [ ] Logs show the tool trail and `gmail_api_draft_created`:
      `aws logs tail /aws/lambda/paybot-worker-prod --since 15m`
- [ ] **Open Gmail → Drafts and read the actual draft.** This is the real acceptance test.

## Phase 8 — Go live

- [ ] Flip `"ScheduleEnabled": "true"` (and the cadence via `ScheduleExpression` if
      desired — keep it offset from the workstation task's :39).
- [ ] Redeploy: `.\deploy\deploy.ps1 -Region us-east-1 -SkipBuild`
- [ ] Verify: stack output `ScheduleState ENABLED`, and after the next firing minute a
      fresh log stream appears.
- [ ] **Parallel-run 2–3 business days** against the workstation task; compare
      draft-or-escalate decisions daily:
      `aws logs tail /aws/lambda/paybot-worker-prod --since 24h`
- [ ] Park the workstation task (disabled, not deleted — it is the fastest rollback):
      `schtasks /change /tn "Payment Bot Hourly" /disable`
- [ ] After one clean week: delete the task, then **rotate** the Transport Pro password
      and the Google key, retiring the plaintext `.env` copies on the workstation.

---

## Routine operations

| Task | How |
|---|---|
| Ship a code change | `.\deploy\deploy.ps1 -Region us-east-1` — rebuild is keyed on content hash; unchanged code = no-op |
| Change a parameter | edit `params.prod.json` → same command with `-SkipBuild` |
| Update a roster | `aws s3 cp <file> s3://paybot-config-<account>/<file>` — no redeploy |
| Rotate a secret | `put-secret-value` again — no redeploy |
| Emergency stop | `ScheduleEnabled: "false"` → redeploy (≈2 min) |
| Roll back code | redeploy an older `CodeS3Key` — the artifact bucket keeps every hash |
| Remove everything | `aws cloudformation delete-stack --stack-name paybot-prod` (secrets are retained for a recovery window) |

## Health checks (console)

| Question | Where |
|---|---|
| Is it running? | Lambda → `paybot-worker-prod` → Monitor: an Invocation dot per scheduled firing, Errors flat zero |
| What did it do? | CloudWatch → Log groups → `/aws/lambda/paybot-worker-prod` → newest stream |
| Is anything wrong? | CloudWatch → Alarms → the four `paybot-prod-*` alarms green |
| What is it deciding? | CloudWatch → Metrics → `PaymentBot/prod` (DraftsCreated / Escalations / GateBlocked) |
| Everything it owns | CloudFormation → `paybot-prod` → Resources tab |
| What is it costing? | Cost Explorer → group by Service ("Amazon Bedrock" ≈ the bot's real cost); Budgets alert recommended (~$10/month threshold) |

## Failure signatures seen in the wild

| Symptom | Cause → fix |
|---|---|
| `secretsmanager:CreateSecret ... not authorized` at deploy | Identity can't create resources → admin role or Appendix A policy; delete the `ROLLBACK_COMPLETE` stack before retrying |
| `SettingsError: error parsing value for field "reply_cc"` | `ReplyCc` passed as a bare address → JSON list string |
| `AccessDeniedException` on first model call | Anthropic use-case form not cleared (Phase 1), or `BedrockModelId` not the exact listed id |
| Alarm emails silently stop | Mail scanner hit the SNS unsubscribe link → Phase 6 protected re-confirm |
| Run killed near 15 minutes | Backlog × `FetchLimit` exceeded the Lambda ceiling → lower the limit (mail is not lost; next run retries) |
| `Runtime.ImportModuleError: pydantic_core` | Zip built outside `deploy.ps1` with Windows wheels → always build via the script |
