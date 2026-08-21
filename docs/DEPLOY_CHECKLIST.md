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
      `schtasks /change /tn "PaymentBot-DraftRun" /disable`
      (the task's real name, verified 2026-08-20 — earlier docs said "Payment Bot Hourly",
      which matches nothing and fails exactly when you need the rollback)
- [ ] After one clean week: delete the task, then **rotate** the Transport Pro password
      and the Google key, retiring the plaintext `.env` copies on the workstation.

## Phase 9 — Chat approval (optional; docs/CHAT_APPROVAL_PLAN.md §10)

Every switch ships OFF — skip this phase entirely and nothing chat-related exists in
the stack. Do the steps in this order; each is independently deployable and the one
before it is its rollback.

- [ ] **Google side (once):** create the Chat space with the three reviewers; in the
      GCP project of the service-account key (`gsheets-python-350615`): enable the
      **Google Chat API**, configure the app (name, avatar, "Receive 1:1 messages" off,
      "Join spaces" on), and add the app to the space. Note the **project number** —
      it is the `ChatAudience`.
- [ ] **Shadow cards:** set `ChatSpace` (spaces/XXXX from the space URL), keep
      `ApprovalMode: "drafts"`; redeploy with `-SkipBuild`. Cards for every draft,
      escalation and gate block appear (no buttons); Gmail Drafts unchanged. Watch a
      few days: formatting, dedup (no reposted escalations), card volume.
- [ ] **Callback wiring:** the stack output `ChatCallbackUrl` exists once `ChatSpace`
      is set — paste it into the Chat app's *HTTP endpoint URL*, and set
      `ChatAudience` to the project number. Until both halves are done the callback
      rejects everything (fail closed).
- [ ] **Roster of one:** `Reviewers: "[\"<operator>@circledelivers.com\"]"`,
      `ApprovalMode: "chat"`, `ReplyTo: "paystatus@circledelivers.com"`; redeploy.
      Approve one real card: the send leaves **from the operator's address**, Cc/
      Reply-To to the group, threads at the carrier's end, the card updates in place,
      a second click reports who already handled it.
- [ ] **Precondition for three reviewers: the workstation task is parked** (Phase 8) —
      it writes reading-mailbox drafts nobody is watching in chat mode.
- [ ] **All three:** extend `Reviewers`, walk the reviewers through one card each —
      they are lending their names to sends. Watch `approval_sent` / `approval_expired`
      lines for a week.
- [ ] **Rollback at any point:** `ApprovalMode: "drafts"` → redeploy. Pending entries
      expire on their own; cards stay as history; drafts resume in the reading mailbox.
- [ ] **Reviewer joins/leaves:** edit `Reviewers` → redeploy with `-SkipBuild`. A
      leaver's pending cards are claimable by the others already (any reviewer can act
      on any card); nothing to migrate.

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
| Is anything wrong? | CloudWatch → Alarms → the five `paybot-prod-*` alarms green |
| What is it deciding? | CloudWatch → Metrics → `PaymentBot/prod` (DraftsCreated / Escalations / GateBlocked) |
| Everything it owns | CloudFormation → `paybot-prod` → Resources tab |
| What is it costing? | CloudWatch → Metrics → `PaymentBot/prod` → `LlmInputTokens` / `LlmOutputTokens`, Sum, 1 day. This is the leading indicator and it moves within the hour; Cost Explorer grouped by Service ("Amazon Bedrock") is the same story a day late. A Budgets alert (~$10/month) is still worth having as the backstop |
| Why is it costing that? | Logs Insights over the worker log group. Per-skill spend: `filter message="llm_usage" \| stats sum(input_tokens), sum(output_tokens), count(*) by label`. Is the prompt cache working: add `sum(cache_read_tokens)` — near zero past turn 2 on a multi-turn run means the rolling checkpoints have stopped matching. Is one thread being re-billed: `filter message="llm_usage" \| stats sum(input_tokens) as spend by correlation_id \| sort spend desc \| limit 10` — a single correlation_id at the top of a quiet day is the shape `EscalationRetryLimit` exists to stop |
| Is anything stranded? | `EscalationRetriesExhausted` and `GateBlockRetriesExhausted`. Each entry is mail sitting unread that nothing will retry. A spike in the first right after an outage means threads escalated on a transient failure and used up their attempts — those need re-sending by hand |

## Failure signatures seen in the wild

| Symptom | Cause → fix |
|---|---|
| `secretsmanager:CreateSecret ... not authorized` at deploy | Identity can't create resources → admin role or Appendix A policy; delete the `ROLLBACK_COMPLETE` stack before retrying |
| `SettingsError: error parsing value for field "reply_cc"` | `ReplyCc` passed as a bare address → JSON list string |
| `AccessDeniedException` on first model call | Anthropic use-case form not cleared (Phase 1), or `BedrockModelId` not the exact listed id |
| Alarm emails silently stop | Mail scanner hit the SNS unsubscribe link → Phase 6 protected re-confirm |
| A carrier says they never got an answer, and the log shows nothing for hours | The message may have spent its retry budget: `escalation_retries_exhausted` or `gate_block_retries_exhausted` in the log, keyed by `correlation_id`. Both are working as designed — the mail is a human's now. Reply by hand; raise `EscalationRetryLimit` / `GateBlockRetryLimit` only if the underlying cause was transient and is now fixed. Note that raising the limit does **not** re-queue mail already past its window: `newer_than:2d` will have dropped it |
| `LlmSpend` alarm fires on a quiet day | Almost never volume. Run the "Why is it costing that?" query above and group by `correlation_id` — one message re-processing is the usual cause. Check it has not somehow escaped the ledger (unreadable `state/gate_block_ledger.json` resets every counter and logs `block_ledger_unreadable_reset`) |
| Run killed near 15 minutes | Backlog × `FetchLimit` exceeded the Lambda ceiling → lower the limit (mail is not lost; next run retries) |
| `Runtime.ImportModuleError: pydantic_core` | Zip built outside `deploy.ps1` with Windows wheels → always build via the script |
