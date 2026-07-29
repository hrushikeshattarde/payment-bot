# Running locally — draft only, no AWS

Read real carrier mail from `paystatus@`, answer it with **Groq** driving the tool-use loop
against **live Transport Pro**, and save each reply straight into the mailbox's **Gmail
Drafts** folder with you on Cc. **No email is ever sent.** You open Drafts, read it, press
Send.

```
paystatus@ inbox  ──Gmail API──▶  intake & safety (code)
                                       │
                                       ▼
                       Groq  ◀──▶  tool-use loop  ◀──▶  Transport Pro API
                                       │
                                       ▼
                             pre-send gate (code)
                                │pass          │fail
                                ▼              ▼
                     Gmail Drafts          console report
                     To: carrier           "escalated — handle by hand"
                     Cc: you
                                │
                                ▼
                   you open Drafts and press Send
```

No AWS, no Bedrock, no Slack. One provider per concern:

| Concern | Local | Deployed (AWS) |
|---|---|---|
| Load data | Transport Pro API | same |
| Mailbox | Gmail API, dedicated service account | same |
| LLM | **Groq** | **Bedrock** |
| Review surface | Gmail Drafts + console | Slack approval (§8.5) |

---

## 1. Why nothing can be sent

Three independent mechanisms, not one flag:

| Guard | Effect |
|---|---|
| `payment-bot-local` forces `draft_only` | The pipeline never takes the auto-send path, whatever `.env` says |
| `DeferredApprovalResolver` | Approval is never granted in-process; the run ends at `AWAITING_REVIEW` |
| `GmailApiClient.send_reply` raises | No code path calls a send API — only `drafts.create` |

Remove any two and it still cannot send. Sending is the one irreversible action in the
system, so it is not a config toggle.

Only a **gate-passing** reply becomes a draft. An escalated or gate-blocked run leaves the
Drafts folder untouched.

> ⚠ **One honest caveat.** Google provides **no draft-only scope**. `gmail.compose` is the
> narrowest scope that permits `drafts.create`, and it *also* permits `messages.send`. So the
> credential is technically *capable* of sending; the guarantee above rests on our code, not
> on the credential. A test asserts the client only ever calls `/drafts`.

---

## 2. Install

```bash
python -m venv .venv
```

```bash
.venv\Scripts\Activate.ps1
```

```bash
python -m pip install -U pip; python -m pip install -e ".[dev,google]"
```

The `google` extra pulls in `google-auth` (used only to RSA-sign the service-account JWT).
`boto3` is *not* needed locally — that is the `aws` extra, for deployment.

---

## 3. Get the three credentials

### 3.1 Transport Pro — you already have these

Base URL, username, password. The base URL is the Postman collection's `{{URL}}`, e.g.
`https://<tenant>.transportpro.net/api/v1`.

> The API user and password embedded in the exported Postman collection should be treated as
> compromised — anyone with that file has API access. Rotate them and use the new
> credentials. Do not commit the collection to the repo.

### 3.2 Groq API key

1. Sign in at <https://console.groq.com> → **API Keys** → **Create API Key** (`gsk_…`)
2. Check which models your key can use:

```bash
curl -H "Authorization: Bearer $PAYBOT_GROQ_API_KEY" https://api.groq.com/openai/v1/models
```

The model **must support tool calling** — the whole agent depends on it. Default is
`llama-3.3-70b-versatile`. See [§7](#7-if-drafts-keep-getting-blocked) on model choice.

### 3.3 A dedicated Google service account

A service account has **no mailbox of its own** — it borrows one by impersonation. So the key
alone is never enough; it needs three things done first.

**Step 1 — create a service account *for this bot only*.** Google Cloud console →
**IAM & Admin → Service Accounts → Create**. Name it something like `paybot-gmail`. It needs
**no IAM roles** at all (Gmail access comes from delegation, not IAM). Then **Keys → Add key
→ JSON** and download it.

> Do not reuse a service account that exists for something else. Delegation grants are made
> per service account, so a dedicated one keeps the grant auditable and independently
> revocable. See the security note below for why that matters.

Point the bot at it:

```ini
PAYBOT_GOOGLE_SA_FILE=C:\path\to\paybot-gmail-key.json
PAYBOT_GMAIL_USER=paystatus@circledelivers.com
```

Now run `payment-bot-local --check`. It prints the exact values the next two steps need —
`client_id`, `project_id`, and the scopes — so you can paste them into a ticket.

**Step 2 — enable the Gmail API** in the key's `project_id`. You can likely do this yourself:

```
https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=<project_id>
```

Symptom if skipped: *"Gmail API has not been used in project … before or it is disabled."*

**Step 3 — authorise domain-wide delegation.** This needs a **Workspace super-admin** and
cannot be done from the Cloud console:

1. Admin console → **Security → Access and data control → API controls** →
   **Domain-wide delegation** → **Add new**
2. **Client ID**: the `client_id` from the key (a ~21-digit number, *not* the email)
3. **OAuth scopes**, comma-separated, **exactly**:
   ```
   https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.compose
   ```

Symptom if skipped: `unauthorized_client`, regardless of how valid the key is.

> **Do not edit the key file.** The `private_key` value contains `\n` escapes that must stay
> intact; reformatting it breaks signing.

> ⚠ **Delegation is domain-wide in capability.** This is worth being precise about: even with
> a dedicated service account, the delegation grant lets it impersonate **any** user in
> `circledelivers.com` within those two scopes — Google offers no per-mailbox restriction in
> the delegation UI. What a dedicated account buys you is **isolation and revocability**: only
> this bot holds the key, the grant is visible in the admin console attributed to this bot
> alone, and revoking it breaks nothing else. That makes it the right choice, but treat the
> key with the care that capability deserves: keep it out of the repo, out of chat, and out of
> shared drives.

---

## 4. Configure

```bash
Copy-Item .env.example .env
```

Fill in `.env`:

```ini
# --- Transport Pro ---
PAYBOT_TP_BASE_URL=https://<tenant>.transportpro.net/api/v1
PAYBOT_TP_USERNAME=<your api user>
PAYBOT_TP_PASSWORD=<your api password>

# --- Groq (local LLM) ---
PAYBOT_GROQ_API_KEY=gsk_...
PAYBOT_GROQ_MODEL=llama-3.3-70b-versatile

# --- Gmail (dedicated service account) ---
PAYBOT_GOOGLE_SA_FILE=C:\path\to\paybot-gmail-key.json
PAYBOT_GMAIL_USER=paystatus@circledelivers.com
PAYBOT_GMAIL_QUERY=is:unread
PAYBOT_GMAIL_FETCH_LIMIT=10
PAYBOT_GMAIL_MARK_SEEN=false
PAYBOT_GMAIL_CREATE_DRAFT=true

# --- Draft only, and who gets Cc'd on the reply ---
PAYBOT_DRAFT_ONLY=true
PAYBOT_REPLY_CC=["hrushikesh.attarde@circledelivers.com"]
```

`.env` is gitignored. Never commit it.

---

## 5. Run

Verify configuration and that impersonation works — processes nothing:

```bash
payment-bot-local --check
```

See the output without writing anything anywhere:

```bash
payment-bot-local --dry-run --limit 1
```

The real thing — one email, draft saved to Gmail:

```bash
payment-bot-local --limit 1
```

Then drop the limit once you trust it:

```bash
payment-bot-local
```

### What you get per email

```
==========================================================================
EMAIL   Payment status for load 2462934
FROM    Idea Expedited Billing <billing@ideaexpedited.com>

──── TOOL TRAIL (§8.1) ───────────────────────────────────────────────────
  [ok ] classify_intent              (    0.1 ms)
  [ok ] tp_get_load_summary          (  310.2 ms)
  [ok ] compute_scheduled_pay_date   (    0.1 ms)
  ...
──── PRE-SEND GATE (§5) ──────────────────────────────────────────────────
  [PASS] length_routing     all disclosed loads are valid 6/7-digit
  [PASS] authorization      sender authorized for all disclosed loads
  [PASS] grounding          every amount and date in the draft is grounded
──── DRAFT REPLY (NOT SENT) ──────────────────────────────────────────────
  To : billing@ideaexpedited.com
  Cc : hrushikesh.attarde@circledelivers.com
  Re : Payment status for load 2462934

  | Hello,
  | Here is the payment status for load 2462934 (status: BILLED).
  | ...
  Citations:
    - total pending: $4,650  (tp_get_load_summary)
    - scheduled pay date: 2026-08-20  (compute_scheduled_pay_date)
──── OUTCOME ─────────────────────────────────────────────────────────────
  DRAFT READY FOR REVIEW (not sent)
  saved to : Drafts (id r-abc123)
```

Then open the `paystatus@` mailbox → **Drafts**. The reply is threaded under the carrier's
original message, addressed to them, with you on Cc. Edit if you want, press **Send**.

> Drafts live in the **`paystatus@`** mailbox, not your personal one. Sign in to it directly,
> or have it delegated to your account (Gmail → Settings → Accounts → *Grant access*).

---

## 6. Reading the outcomes

| Outcome | Meaning | What to do |
|---|---|---|
| `DRAFT READY FOR REVIEW` | Gate passed; the reply is in Gmail Drafts | Review it, press Send |
| `ESCALATED` | Stopped before drafting — sensitive change, unauthorized sender, bulk, unclear intent, non-7-digit load, or a combined payment+rate ask | Handle by hand; check the reason. No draft is created |
| `BLOCKED BY GATE` | A draft existed but failed a §5 check | Read the failing check. Usually the model stated a figure no tool produced. No draft is created |
| `SENT` | **Should be impossible here** | Stop and tell me — that is a bug |

Escalations are normal and healthy, not failures. The system is built to stop rather than
guess.

---

## 7. If drafts keep getting blocked

The gate blocks any figure that did not come from a tool result, which is the safety
property you want — but an open-weight model that skips a step trips it. If the
gate-block rate is high:

1. **Look at the failing check.** `grounding` with an unexplained amount or date means the
   model invented a figure instead of calling `compute_scheduled_pay_date` /
   `compute_carrier_rate`.
2. **Try a stronger tool-calling model** — set `PAYBOT_GROQ_MODEL` to another id from your
   `/models` list. This is the single biggest lever.
3. **Raise the output cap** if replies look cut off mid-sentence:
   `PAYBOT_AGENT_MAX_TOKENS=8192`.
4. **Raise the loop cap** if you see `agent produced no draft (stop_reason=max_iterations)`:
   `PAYBOT_AGENT_MAX_ITERATIONS=16`.

Do **not** "fix" this by loosening the gate. A blocked draft is the gate doing its job.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Gmail API has not been used in project …` | Step 2: enable the Gmail API in that project. The error contains the direct link. |
| `unauthorized_client` | **The most common one.** Step 3: delegation is not authorised, or the scopes do not match *exactly*. The client id must be the SA's numeric `client_id`, not its email. |
| `invalid_grant` | The impersonated address does not exist in the domain, or the machine clock is skewed. Check `PAYBOT_GMAIL_USER` and your system time. |
| `insufficient authentication scopes` | Delegation exists but is missing `gmail.compose`. Add it in the Admin console; no new key needed. |
| `google-auth is required` | `pip install -e ".[dev,google]"`. |
| `missing ['client_email', 'private_key']` | You downloaded an OAuth *client secret*, not a service-account key. |
| `not a usable PEM` | The key file was edited or re-formatted. Re-download it and leave the `\n` escapes alone. |
| `no Gmail mailbox` | The account exists but Gmail is not enabled for it. |
| `No mail matched 'is:unread'` | Everything is read. Set `PAYBOT_GMAIL_QUERY=newer_than:7d` to reprocess, or send a test email. |
| Same mail every run | Expected — `PAYBOT_GMAIL_MARK_SEEN=false` leaves it unread on purpose. Note you get a **new draft each run**, so clean up duplicates. |
| `Transport Pro: not found (…)` | The load number is not in Transport Pro, or the base URL is wrong. Confirm with one manual `curl`. |
| `Transport Pro login failed (HTTP 401)` | Wrong username/password, or the base URL points at the wrong tenant. |
| `Groq ... HTTP 401` | Bad or revoked `PAYBOT_GROQ_API_KEY`. |
| `Groq ... HTTP 400 ... tool` | The model does not support tool calling. Pick another `PAYBOT_GROQ_MODEL`. |
| `intent not answerable in this slice` | The email asks for payment status **and** rate verification. Combined intent (§3.5) is not wired; it escalates by design. |
| `no valid 6/7-digit load id found` | No load number in the email. Carrier-name lookup is not wired. |

---

## 9. What is still not wired

* **Sending.** By design. You press Send in Gmail.
* **Slack approval buttons.** Needs a public callback endpoint — that belongs with the AWS
  deployment; see [AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md). Locally there is no Slack client at
  all; escalations surface in the console report and the JSON log.
* **QuickBooks (6-digit loads)**, carrier-name lookup, combined intents, portal bulk reply —
  all escalate today.

---

## 10. Safety notes

* `.env` holds live secrets and the service-account key path. It is gitignored; keep it that
  way, and do not paste it into chat or tickets.
* The service-account key can impersonate mailboxes in your domain (see the note in §3.3).
  Treat it like a password: not in the repo, not in shared drives. In production it belongs in
  Secrets Manager, injected via `PAYBOT_GOOGLE_SA_JSON`.
* Drafts are written into a **shared mailbox** — anyone with access to `paystatus@` can see
  them before you send.
* Every Transport Pro read is read-only. Nothing in this codebase writes to Transport Pro.
* **`pytest` never reads your `.env`.** An autouse fixture in `tests/conftest.py` strips
  `PAYBOT_*` and disables env-file loading, so the suite cannot touch the real mailbox,
  Transport Pro, or Groq. Do not remove it.
* The pre-send gate runs on every draft, in every mode. Do not add a bypass.
