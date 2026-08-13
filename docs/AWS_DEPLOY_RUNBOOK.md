# AWS Deployment Runbook — Stage 1 Worker

Step-by-step, from an empty AWS account to the bot drafting carrier replies on a schedule
with the workstation task retired. Written to be followed in order by someone who did not
write the code.

**Three documents, three jobs.** Read whichever answers your question:

| Document | Answers |
|---|---|
| [AWS_DEPLOYMENT_PLAN.md](AWS_DEPLOYMENT_PLAN.md) | *Why* it is shaped this way — architecture, decisions, risks, cost |
| **This file** | *How to do it* — the walkthrough, in order, with the commands |
| [deploy/README.md](../deploy/README.md) | *What each file does* — reference and troubleshooting table |

What you are deploying: **one Lambda on a 20-minute schedule**, doing exactly what the
workstation task does today — fetch unread carrier mail, run the pipeline and the pre-send
gate, save gate-passing replies to Gmail Drafts. **It sends nothing.** Sending is Stage 2.

---

## Before you start

### Time and access

Budget **half a day** for the first deployment, then **two to three business days** of
parallel running before you retire the workstation task. The parallel period is not
optional padding — it is how you find out whether Bedrock's drafts differ from the local
model's in ways worth knowing.

You need:

- **The AWS CLI** and **Python 3.11+** on the machine you deploy from. Nothing else — no
  Docker, no SAM CLI, no Node.
- **An AWS identity that can create IAM roles.** This is the step that blocks most people;
  see the next section.
- **The Google service-account JSON** for the mailbox, and the **Transport Pro password**.
  You will paste both into Secrets Manager yourself.

### Check whether your identity can actually deploy

This is the step that blocks most first attempts, and it is worth two minutes up front.

```bash
aws sts get-caller-identity
```

**The quick heuristic first.** An SSO role named for a *job* rather than a *permission set*
— `DataScientist`, `Analyst`, `ReadOnly`, `PowerUserReadOnly` — almost certainly cannot
create IAM roles. If that is what comes back, skip to
[Appendix A](#appendix-a--policy-for-the-deploying-identity) and get a deployment role
before going further.

**To check properly**, you need two calls, not one. `sts get-caller-identity` returns an
*assumed-role* ARN (`arn:aws:sts::…:assumed-role/Name/session`), and the policy simulator
rejects that shape outright with `InvalidInput` — it wants the underlying IAM role ARN, and
for an SSO role that lives under a `/aws-reserved/sso.amazonaws.com/` path you will not
guess. Resolve it first:

```bash
aws iam get-role --role-name "$(aws sts get-caller-identity --query Arn --output text | cut -d/ -f2)" --query Role.Arn --output text
```

Then simulate against what that returns:

```bash
aws iam simulate-principal-policy --policy-source-arn "<the ARN from above>" --action-names iam:CreateRole lambda:CreateFunction cloudformation:CreateStack secretsmanager:CreateSecret --query "EvaluationResults[].[EvalActionName,EvalDecision]" --output text
```

Four `allowed` and you are clear to deploy. Any `implicitDeny` and you need a different
role.

> **If the simulate call is itself denied** with `not authorized to perform:
> iam:SimulatePrincipalPolicy`, you have your answer by another route: a role that may not
> even *ask* about IAM permissions does not have them. Go to Appendix A.

**There is no cheap definitive test, and it is worth knowing why.** `deploy.ps1 -Plan`
creates a changeset but does not execute it, so it exercises only CloudFormation
permissions — a clean `-Plan` does **not** prove the deploy will succeed. The first real
deploy is the first test of `iam:CreateRole`. It fails safely if you lack it: CloudFormation
rolls back and creates nothing. The one piece of litter is a stack left in
`ROLLBACK_COMPLETE`, which cannot be updated, only deleted, before you retry:

```bash
aws cloudformation delete-stack --stack-name paybot-prod
```

---

## Step 1 — Enable Bedrock model access

The one thing no script can do for you: model access is an account-level opt-in with terms
attached, and there is no API that grants it to yourself.

**Console → Bedrock → Model access → Enable** the Claude models.

Then confirm from the CLI, because a model being *visible* is not the same as one you may
*invoke*:

```bash
aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude')].inferenceProfileId" --output table
```

Whatever comes back is what goes in `BedrockModelId` at step 2. It must be an **inference
profile** — the `us.` or `global.` prefixed form — not a bare foundation-model id. The
profile is what carries cross-region capacity.

**Copy the id from that output; do not pattern-match it off another one.** The version
suffix is not universal, and getting it wrong is the quietest failure in this runbook: a
bad id passes template validation, deploys cleanly, and then fails at the first model call
with an access error that reads like a permissions problem rather than a typo.

This is not hypothetical — it was the shipped default. `PAYBOT_MODEL_DRAFT` read
`us.anthropic.claude-sonnet-5-v1:0` by analogy with its 4.5 sibling, and no such profile
exists. Checked against this account on 2026-08-13:

| Id | Exists |
|---|---|
| `us.anthropic.claude-sonnet-5` | yes, ACTIVE — **this is the one** |
| `us.anthropic.claude-sonnet-5-v1:0` | no |
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | yes — the suffixed form *is* right here |

The defaults now carry the verified value. Re-run the query anyway: model availability is
per-account and per-region, and this list will age.

---

## Step 2 — Fill in the parameters

```bash
cp deploy/params.example.json deploy/params.prod.json
```

Open it and fill in the five that have no sensible default:

| Key | What it is |
|---|---|
| `GmailUser` | **A real user, not the group.** Google Groups cannot be impersonated, so point this at a member's mailbox and let `GmailQuery` narrow it to group mail. |
| `GmailQuery` | Gmail search syntax. `is:unread to:paystatus@circledelivers.com` is the shape that works with the line above. |
| `TransportProBaseUrl` | Transport Pro API host. |
| `TransportProUsername` | The API user. The **password** does not go here — it goes in Secrets Manager at step 4. |
| `BedrockModelId` | Whatever step 1 returned. |

Three more deserve a look before you accept the defaults:

- **`FetchLimit` is 5, not the local 10.** The agent's turn budget scales with load count,
  so a five-load factoring enquiry costs roughly four times a single-load one. Ten
  multi-load emails in one run can exceed Lambda's 15-minute cap outright. The 20-minute
  cadence absorbs a lower limit easily. Raise it only after you have measured real
  per-email wall time in step 7.
- **`Timezone` is `America/New_York`.** This is a correctness input, not a logging
  preference: the pre-send gate's tense check compares dates against the container's
  calendar date, and under Lambda's UTC default a run after 20:00 Eastern is already on
  tomorrow — it would score a same-day pay date as a day late and block a correct reply.
  Use the zone name. `TZ=EST` pins UTC-5 all year and is an hour wrong every summer.
- **`CargoTelReplies` is `true`.** The 6-digit path is off by default, and parallel running
  is the cheapest way to learn its real failure rate before anyone depends on it.

**No credentials belong in this file.** It is gitignored (it carries the mailbox and the
Transport Pro host, and this repository is public), but the reason there is nothing secret
in it is structural: the two credentials live in Secrets Manager and reach the bot as
environment variables the stack wires up.

---

## Step 3 — Deploy the stack

Look before you leap. This builds the package, validates the template, and creates a
changeset **without executing it**:

```bash
.\deploy\deploy.ps1 -Plan
```

On Linux or in CI, `./deploy/deploy.sh --plan` does the same thing.

Read what it says it will create. Then, for real:

```bash
.\deploy\deploy.ps1
```

Expect **three to five minutes**, most of it downloading wheels on the first run. What
happens, in order:

1. **Preflight** — CLI, Python, credentials, region, parameters file. Fails here with a
   fixable message rather than halfway through.
2. **Build** — Linux wheels staged and zipped (~8 MB). The zip is keyed on its own content
   hash, so an unchanged build produces an unchanged key.
3. **Artifact bucket** — `paybot-deploy-<account>-<region>`, created if absent: private,
   versioned, encrypted. Versioning is what later makes a code rollback a redeploy of an
   older key rather than a rebuild of old source.
4. **Deploy** — CloudFormation, `CAPABILITY_NAMED_IAM`.

You now have a Lambda, an execution role, a log group, alarms, two empty secrets, and a
schedule that is **DISABLED**. Nothing runs yet. That is deliberate.

---

## Step 4 — Fill the two secrets

The stack created both as placeholders. It owns their *existence* and their IAM; their
*values* are yours to put in. The script prints these with the real ARNs substituted:

```bash
aws secretsmanager put-secret-value --secret-id paybot/prod/google-sa-json --secret-string file://C:\path\to\service-account.json
```

```bash
aws secretsmanager put-secret-value --secret-id paybot/prod/tp-password --secret-string 'THE-TP-PASSWORD'
```

Until both hold real values the worker fails closed on its first Gmail call — deliberately.
A bot that starts without credentials does not crash: it reports *"no mail matched"*, which
on a schedule is indistinguishable from a quiet inbox for as long as nobody looks.

> **Shell note.** In PowerShell, wrap the password in single quotes so `$` and backticks are
> not interpreted. If it contains a single quote, double it: `'pa''ssword'`.

---

## Step 5 — Smoke test with one email

A real run against the live mailbox that happens to stop after one message. The gate,
draft-only mode and thread-skip are all untouched:

```bash
aws lambda invoke --function-name paybot-worker-prod --payload '{"limit":1}' --cli-binary-format raw-in-base64-out response.json
```

```bash
aws logs tail /aws/lambda/paybot-worker-prod --follow
```

**What good looks like.** `response.json` holds a summary like
`{"processed": 1, "outcomes": {"awaiting_review": 1}}`, and the log shows the tool trail,
every gate check `PASS`, and the draft body. Then open Gmail → Drafts and read the actual
draft.

**What the outcomes mean:**

| Outcome | Meaning |
|---|---|
| `awaiting_review` | Gate passed, draft saved. The success case. |
| `escalated` | Deliberately not answered — unauthorized sender, cross-system email, a bank or NOA instruction. Expected and healthy at some rate. |
| `blocked` | The draft failed a gate check. Read the reason; this is the gate working. |
| `sent` | **Should be impossible.** Three independent guarantees prevent it. Investigate immediately. |

If the first invocation errors, jump to the troubleshooting table in
[deploy/README.md](../deploy/README.md#when-it-fails).

---

## Step 6 — Parallel running

Enable the schedule by setting `"ScheduleEnabled": "true"` in `deploy/params.prod.json` and
re-running `.\deploy\deploy.ps1`. It fires at **:15, :35 and :55** — offset from the
workstation task's :39 so the two interleave. Thread-skip guarantees no duplicate drafts:
whichever runs first drafts the reply, the other skips the thread.

Leave both running for **two to three business days**. What you are checking:

1. **Parity of outcomes per email** — the same draft-or-escalation decision, for the same
   reasons, from both runners. This is the real acceptance test.
2. **Draft quality.** Bedrock should produce *strictly fewer* gate blocks than the local
   model did. If it does not, something is wrong with the deployment rather than the model.
3. **Wall time per email**, from the log timestamps. This is the number that decides whether
   Stage 1 stays on Lambda. If runs approach 15 minutes, lower `FetchLimit` first; if that
   is not enough, the answer is a scheduled Fargate task — same container, no time cap —
   not a bigger timeout.

Daily during this window:

```bash
aws logs tail /aws/lambda/paybot-worker-prod --since 24h --filter-pattern "gate_blocked"
```

---

## Step 7 — Retire the workstation task

Once parallel running is clean:

```bash
schtasks /change /tn "Payment Bot Hourly" /disable
```

Leave it *disabled*, not deleted, for a week. It is your fastest rollback.

After that week: delete the task, then rotate the Transport Pro password and the Google
service-account key — retiring the plaintext `.env` copies that have been sitting on the
workstation.

---

## Routine redeploys

```bash
.\deploy\deploy.ps1
```

Idempotent. Only the difference is applied, and an unchanged build produces an unchanged S3
key so CloudFormation reports no changes rather than needlessly bouncing the function.

## Rollback

Fastest first:

1. **Stop the bot** — set `"ScheduleEnabled": "false"`, re-run the script, re-enable the
   Windows task. Two minutes.
2. **Previous code** — the artifact bucket is versioned and every deploy is a distinct key,
   so an earlier build is a `CodeS3Key` override. No rebuild of old source.
3. **Remove everything** — `aws cloudformation delete-stack --stack-name paybot-prod`. The
   secrets are *retained* for a recovery window rather than destroyed, so credentials
   survive a stack mistake.

## What to watch once it is live

The alarms are wired to an SNS topic; set `AlarmEmail` in the parameters file to get them.

| Alarm | What it means |
|---|---|
| `RunFailures` | Any invocation error. The system is fail-closed, so this is never routine. |
| `GateBlocked` | More than two blocked drafts a day. The gate doing its job is fine; the *rate* is the signal. |
| `CargoTelCookieStale` | The scraped session was read and rejected. **Not fixable from here** — the login bot owns it. Every 6-digit email escalates until it is refreshed. |
| `CargoTelCookieUnavailable` | The cookie could not be read *at all*. In Lambda the execution role does not expire, so this is IAM or a bucket policy, not a stale login. |

`PolicyAllowedChangeWording` is a metric rather than an alarm, and deserves more attention
than that suggests: it fires when an email carrying a bank or NOA instruction was drafted
anyway because policy allows it. The gate guarantees the *draft* never acknowledges the
instruction. Nothing guarantees a human ever *actions* it. **Treat each one as a task.**

---

## Appendix A — Policy for the deploying identity

Hand this to your AWS administrator if the check at the top came back denied. It is what
creating and updating this stack requires, and nothing more. Attach it to a deployment role
you assume, not to a person.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "StackOperations",
      "Effect": "Allow",
      "Action": ["cloudformation:*"],
      "Resource": "*"
    },
    {
      "Sid": "ExecutionRoleLifecycle",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole",
        "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
        "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies", "iam:PassRole"
      ],
      "Resource": "arn:aws:iam::*:role/paybot-worker-*"
    },
    {
      "Sid": "WorkerLifecycle",
      "Effect": "Allow",
      "Action": [
        "lambda:*", "events:*", "logs:*", "cloudwatch:PutMetricAlarm",
        "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms",
        "sns:CreateTopic", "sns:DeleteTopic", "sns:Subscribe",
        "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:TagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ArtifactBucket",
      "Effect": "Allow",
      "Action": ["s3:CreateBucket", "s3:PutObject", "s3:GetObject", "s3:ListBucket",
                 "s3:PutBucketVersioning", "s3:PutBucketPublicAccessBlock",
                 "s3:PutEncryptionConfiguration", "s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::paybot-deploy-*", "arn:aws:s3:::paybot-deploy-*/*"]
    },
    {
      "Sid": "SecretShells",
      "Effect": "Allow",
      "Action": ["secretsmanager:CreateSecret", "secretsmanager:DeleteSecret",
                 "secretsmanager:DescribeSecret", "secretsmanager:TagResource",
                 "secretsmanager:PutSecretValue", "secretsmanager:UpdateSecret",
                 "secretsmanager:GetResourcePolicy"],
      "Resource": "arn:aws:secretsmanager:*:*:secret:paybot/*"
    },
    {
      "Sid": "VerifyModelAccess",
      "Effect": "Allow",
      "Action": ["bedrock:ListInferenceProfiles", "bedrock:ListFoundationModels"],
      "Resource": "*"
    }
  ]
}
```

Two notes for whoever reviews it. The IAM statement is scoped to `paybot-worker-*` — this
policy cannot mint arbitrary roles, which is the concern `iam:CreateRole` usually raises.
And `secretsmanager:GetSecretValue` is **deliberately absent**: the deployer creates the
secret shells and can write values into them, but cannot read back what is stored. Reading
is the worker's job, and only the worker's role has it.

## Appendix B — Where things live once deployed

| Thing | Name |
|---|---|
| Stack | `paybot-prod` |
| Function | `paybot-worker-prod` |
| Log group | `/aws/lambda/paybot-worker-prod` |
| Schedule rule | `paybot-worker-prod` |
| Execution role | `paybot-worker-prod` |
| Secrets | `paybot/prod/google-sa-json`, `paybot/prod/tp-password` |
| Artifact bucket | `paybot-deploy-<account>-<region>` |
| Alarm topic | `paybot-alarms-prod` |
| Metrics namespace | `PaymentBot/prod` |

A `staging` stack is the same names with `staging` substituted — `.\deploy\deploy.ps1 -Env
staging` — pointed at a test mailbox. Nothing is shared between the two but the artifact
bucket.
