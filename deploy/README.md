# Deploying the Stage 1 worker

Scripted deployment of the payment bot to AWS: one scheduled Lambda that does exactly what
the workstation task does today — fetch unread carrier mail, run the pipeline and the
pre-send gate, save gate-passing replies to Gmail Drafts. It sends nothing.

The architecture and the reasoning behind it are in
[AWS_DEPLOYMENT_PLAN.md](../docs/AWS_DEPLOYMENT_PLAN.md); this file is how to run it.

| File | What it is |
|---|---|
| `deploy.ps1` | Windows entrypoint. Build → upload → CloudFormation deploy. |
| `deploy.sh` | The same, for CI and non-Windows hosts (§3.6). |
| `build_package.py` | Builds the Lambda zip with **Linux** wheels, from any OS. |
| `template.yaml` | The stack: Lambda, IAM role, schedule, log group, metric filters, alarms. |
| `params.example.json` | Copy to `params.prod.json` and fill in. **No secrets go in it.** |

**Requirements: the AWS CLI and Python 3.11+.** No Docker, no SAM CLI, no Node.

---

## What the script does not do, and why

Four things stay manual. Three are not scriptable; one is deliberate.

1. **Enabling Bedrock model access** is an account-level opt-in with terms attached. No API
   grants it to yourself.
2. **Putting real credential values into Secrets Manager.** The stack creates both secrets
   as empty placeholders and owns their existence and IAM; the values are yours to put in,
   with commands the script prints. Nothing here reads, writes, or logs a credential, and
   none is stored in the repo.
3. **A cross-account bucket policy** on the CargoTel cookie, if the login bot's bucket lives
   in another AWS account. The stack grants *this* role read on the one key; the other
   account must also allow it.
4. **Enabling the schedule.** Deploy disabled, invoke once by hand, diff against the
   workstation log, then enable — cutover step 2 of the plan. The script will not skip that
   for you.

---

## First deployment

### 1. Enable Bedrock model access

Console → Bedrock → Model access → enable Claude. Then confirm from the CLI, because a
model that is visible is not necessarily one you may invoke:

```bash
aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude')].inferenceProfileId"
```

Whatever comes back is what goes in `BedrockModelId`. It must be an **inference profile**
(`us.anthropic.…`), not a bare foundation-model id — the cross-region profile is what has
capacity.

### 2. Create the parameters file

```bash
cp deploy/params.example.json deploy/params.prod.json
```

Fill in `GmailUser`, `TransportProBaseUrl`, `TransportProUsername`, and `RosterBucket`. The
example file explains each one inline. Read the `_Timezone` and `_FetchLimit` notes before
changing either — both have correctness consequences, not just cosmetic ones.

### 3. Deploy

Look before you leap:

```bash
.\deploy\deploy.ps1 -Plan
```

That builds the package, validates the template, and creates a changeset **without
executing it**. When it looks right:

```bash
.\deploy\deploy.ps1
```

The schedule is created **disabled**. Nothing runs yet.

### 4. Fill the secrets

The script prints these with the real ARNs substituted. Run them yourself:

```bash
aws secretsmanager put-secret-value --secret-id paybot/prod/google-sa-json --secret-string file://C:\path\to\service-account.json
```

```bash
aws secretsmanager put-secret-value --secret-id paybot/prod/tp-password --secret-string 'THE-TP-PASSWORD'
```

Until both hold real values the worker fails closed on its first Gmail call. That is
intended: a bot that starts without credentials and reports "no mail matched" is
indistinguishable from a quiet inbox, and would stay that way for as long as nobody looked.

### 5. Smoke test — one email

```bash
aws lambda invoke --function-name paybot-worker-prod --payload '{"limit":1}' --cli-binary-format raw-in-base64-out response.json
```

```bash
aws logs tail /aws/lambda/paybot-worker-prod --follow
```

`{"limit": N}` overrides the fetch limit for one invocation. Everything else — the gate,
draft-only, thread-skip — is untouched, so this is a real run against the live mailbox that
happens to stop after one message.

### 6. Parallel running, then enable the schedule

Diff that run against the same hour's workstation log. What you are checking is *parity of
outcomes per email* — draft or escalation, same reasons — and Bedrock draft quality, where
you should expect strictly fewer gate blocks than the local model produced.

Then set `"ScheduleEnabled": "true"` in `params.prod.json` and re-run `deploy.ps1`. It fires
at :15, :35 and :55 — offset from the workstation task's :39 so the two interleave.
Thread-skip guarantees no duplicate drafts: whichever runs first drafts, the other skips.

After two or three clean days:

```bash
schtasks /change /tn "Payment Bot Hourly" /disable
```

---

## Routine redeploys

```bash
.\deploy\deploy.ps1
```

Idempotent. The zip is keyed on its own content hash, so an unchanged build produces an
unchanged key and CloudFormation reports no changes rather than bouncing the function.

## Rollback

Fastest first:

1. **Stop the bot**: set `"ScheduleEnabled": "false"`, re-run, re-enable the Windows task.
   Two minutes, and the workstation setup stays intact until you delete it.
2. **Previous code**: the artifact bucket is versioned and every deploy is a distinct key,
   so redeploying an earlier build is a `CodeS3Key` override — no rebuild of old source.
3. **Remove everything**: `aws cloudformation delete-stack --stack-name paybot-prod`. The
   secrets are retained by Secrets Manager for a recovery window rather than deleted
   outright, so the credentials survive a stack mistake.

---

## When it fails

| Symptom | Cause |
|---|---|
| `User is not authorized to perform: iam:CreateRole` | The deploying identity cannot create IAM roles. An SSO analyst/data-scientist role usually cannot — this needs a role with CloudFormation, IAM, Lambda, S3, Events and SecretsManager create rights. |
| Stack stuck in `ROLLBACK_COMPLETE` | A failed *first* create cannot be updated, only deleted. `aws cloudformation delete-stack --stack-name paybot-prod`, then redeploy. |
| `Runtime.ImportModuleError: No module named 'pydantic_core...'` | The package was built with Windows wheels. `build_package.py` prevents this; it means the zip was built another way. |
| `AccessDeniedException` calling Bedrock | Model access not enabled (step 1), or `BedrockModelId` names a foundation model rather than an inference profile. |
| `CargoTelCookieUnavailable` alarm | The cookie could not be *read* — IAM or bucket policy, not a stale login. In Lambda the execution role does not expire, so this is a permissions problem. See item 3 above. |
| `CargoTelCookieStale` alarm | The cookie was read and rejected. Not fixable from here: the login bot owns it (plan §3.1 P8). Every 6-digit email escalates until it is refreshed. |
| Runs approach the 15-minute timeout | Expected eventually, and the plan says so (§3.7). Lower `FetchLimit` first; if that is not enough the answer is Fargate — same container, no time cap — not a bigger timeout. |

## Cost

Roughly **$3–6/month** at ~100 emails/month: Bedrock tokens dominate, Lambda and
CloudWatch are cents. See plan §5 for the breakdown.
