# Product Requirements Document (PRD)
## Agent Skills & Tools Catalog — Payments Email Bot

| | |
|---|---|
| **Product** | Payments Email Bot — Agent Skills & Tools Catalog |
| **Related PRD** | `PRD_Payment_Status_Email_Bot.md` (system/product requirements) |
| **Owner** | Circle Delivers — Payments |
| **Status** | Draft v1.2 |
| **Last updated** | 2026-07-24 |
| **Inbox** | paystatus@circledelivers.com |
| **Systems** | Transport Pro (7-digit), QuickBooks Online (6-digit), Gmail, Slack |

---

## 1. Purpose

This document defines the **agent skills** and **tools** used to implement the Payments
Email Bot. It is the build-facing companion to the main product PRD.

- **Main PRD** = *what* the product must do (requirements, edge cases, samples).
- **This catalog** = *how* the agent is structured (skills, tool contracts, schemas).

### 1.1 Architecture directive
- **One agent**, not two separate agents.
- **Two primary skills** (Payment Status, Rate Verification) + shared skills.
- Tools wrap **Transport Pro**, **QuickBooks Online**, **Gmail**, and **Slack**.
- A **deterministic pre-send gate** enforces authorization, fraud blocking, and grounding
  before any email is sent (with Slack human approval in Phase 1).

```
                    ┌─────────────────────────────────────┐
                    │           PAYMENTS AGENT              │
                    └──────────────┬──────────────────────┘
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
     Skill: Payment Status   Skill: Rate Verify   Skill: Shared
              │                    │                    │
              └────────┬───────────┴────────┬───────────┘
                       ▼                    ▼
              ┌────────────────┐   ┌────────────────┐
              │ Transport Pro  │   │ QuickBooks     │
              │ tools (7-digit)│   │ Online (6-dig) │
              └────────────────┘   └────────────────┘
                       │
                       ▼
              Pre-send gate → Slack approve → Gmail send
```

---

## 2. Concepts

| Term | Definition |
|---|---|
| **Agent** | Tool-calling LLM that decides which tools to invoke and drafts replies |
| **Skill** | Playbook: when to run, which tools to use, expected reply shape |
| **Tool** | Typed function wrapping an API or deterministic rule (input → JSON output) |
| **Pre-send gate** | Non-negotiable code that blocks send unless auth/fraud/grounding pass |

---

## 3. Skill Catalog

### 3.1 Skill: `payment_status`

| | |
|---|---|
| **ID** | `payment_status` |
| **Use case** | Use Case 1 — Payment Status |
| **Trigger signals** | "payment status", "when will I be paid", "estimated payment date", "settle date", "missing payment" |
| **Goal** | Return payment status + estimated/scheduled pay date per load (grounded) |

**Required tool sequence (typical)**
1. `extract_identifiers`
2. `detect_sensitive_change` (abort to escalate if fraud/change)
3. For each load: `route_load` → TP or QBO tools
4. `tp_get_load_summary` / `qbo_get_payment_status`
5. `tp_get_dispatch_history` + `tp_get_settlement_entries` (or QBO equivalents)
6. `tp_get_file_history` if status blocked / paperwork question
7. `carrier_cross_check`
8. `check_authorization`
9. `compute_scheduled_pay_date` per earning line (Mon/Thu rule, §4.1.1)
10. Optional: `tp_get_notes` for specific billing questions
11. Compose draft → `slack_post_approval` → (on Approve) `gmail_send_reply`

**Reply must include (when available)**
- Load ID, status, and the **scheduled pay date** derived from `estimated_payment_date`
  via the Monday/Thursday rule (§4.1.1); if `actual_payment_date` exists, report it directly
- Amount and payment method/check number when present
- If earning lines have different scheduled dates, report each line separately
- Or clear blocker reason (paperwork missing vs docs on file → escalate to Carrier Rep)

**Escalate when**
- Sensitive change detected; auth deny; carrier mismatch; cancel/re-book ambiguity;
  docs present but unpaid; invalid load length; bulk over threshold (portal reply instead)

---

### 3.2 Skill: `rate_verification`

| | |
|---|---|
| **ID** | `rate_verification` |
| **Use case** | Use Case 2 — Rate Verification |
| **Trigger signals** | "verify the rate", "advances/deductions/fees/claims", "confirm NOA", "factoring company", subject "Rate Verification - …" |
| **Goal** | Confirm rate vs sender's stated amount; list deductions/adjustments; invoice generated?; NOA/factoring on file (read-only) |

**Required tool sequence (typical)**
1. `extract_identifiers` (+ stated rate/amount, factoring company name)
2. `detect_sensitive_change` (NOA *add/update* = escalate; bank change = escalate)
3. For each load: `route_load` → TP/QBO
4. `tp_get_load_summary` (payload §4.3.0)
5. `compute_carrier_rate` — **carrier rate = sum of `earnings[].amount`**; subtract deductions
6. `tp_get_dispatch_history` (Delivered row Freight Bill should corroborate the computed rate)
7. `tp_get_settlement_entries` (advances, fees, claims, short pays) as corroboration
8. `tp_get_noa_factoring` / File History Rate Agreement as corroboration
9. `carrier_cross_check` + `check_authorization`
10. Compose draft → Slack → Gmail

**Reply must include**
- Our carrier rate = **sum of all earnings lines** (list each line + amount) vs sender's stated
  rate (match/mismatch)
- **Each deduction reported individually with its reason and amount**, and the net rate
  (gross − deductions); if none, state "no deductions on file"
- Invoice generated: Yes / Not yet
- NOA / factoring company on file (read-only confirmation)

**Escalate when**
- Sender asks to **add/attach/update** NOA or change factoring setup
- CANCEL LOAD Confirmation / conflicting rate agreements
- Rate/carrier ambiguity across dispatch rows

---

### 3.3 Skill: `shared_intake_and_safety` (always on)

| | |
|---|---|
| **ID** | `shared_intake_and_safety` |
| **Goal** | Ingest mail, route, authorize, fraud-block, bulk-fallback, approve/send |

**Always run first / around other skills**
- `gmail_fetch_new` / ingest
- `extract_identifiers`
- `detect_sensitive_change`
- Bulk check → if load count > threshold → `compose_portal_reply` + send/approve
- Invalid length (not 6 or 7) → escalate (no lookup)
- Pre-send gate + `slack_post_approval` + `gmail_send_reply`

---

### 3.4 Skill: `carrier_name_lookup`

| | |
|---|---|
| **ID** | `carrier_name_lookup` |
| **When** | No valid 6/7-digit load/invoice ID, but carrier/trucking company name is present |
| **Tools** | `tp_search_by_carrier` / `qbo_search_by_vendor` |
| **Behavior** | One clear load → proceed with payment_status or rate_verification. Many matches → portal fallback or ask for load #. Ambiguous → escalate. |

---

### 3.5 Combined intents
If an email asks for **both** payment status and rate verification, run **both skills**
(shared extraction/routing once), merge into **one** reply, single Slack approval.

---

## 4. Tool Catalog

### 4.1 Conventions
- All tools are **read-only** toward TP/QBO (except Gmail send / Slack post).
- Every tool returns JSON. Errors return `{ "ok": false, "error": "..." }`.
- Tool results are the **only** allowed source of numbers/dates in replies (grounding).
- Load length routing: **6 → QBO**, **7 → TP**; other lengths → do not call lookup tools.

---

### 4.1.1 Business-logic rules (deterministic — computed in code, not by the model)

These rules operate on the Transport Pro load payload (§4.2.1). They are **deterministic**
and must run in code so results are auditable and grounded; the model never derives dates or
sums on its own.

#### Scheduled payment date (payment_status)

The carrier is paid on **Mondays** and **Thursdays** only. The API's
`estimated_payment_date` (interpreted in **EDT**) is mapped to the next applicable payment day:

| `estimated_payment_date` weekday | Carrier is paid on |
|---|---|
| Monday | Monday (same day) |
| Tuesday | Thursday (same week) |
| Wednesday | Thursday (same week) |
| Thursday | Thursday (same day) |
| Friday | Monday (following week) |
| Saturday | Monday (following week) |
| Sunday | Monday (following week) |

Notes and open items:
- All date math is performed in the **EDT** timezone (the API's stated zone for these dates).
- **Monday and Thursday are assumed to pay same-day** because the source rule only specified
  Tue/Wed → Thu and Fri/Sat/Sun → Mon. ⚠ **CONFIRM with Payments owner** whether a date that
  already lands on a payment day pays that day or rolls to the next one.
- Use `estimated_payment_date` when `actual_payment_date` is null. If `actual_payment_date`
  is present, report the actual date and do not compute.
- If an earning line has no `estimated_payment_date`, that line cannot be scheduled →
  report as pending/undetermined; do not invent a date.
- When earning lines carry **different** estimated dates, compute per line and report the
  scheduled date for each (do not collapse to one).

#### Rate computation (rate_verification)

- **Carrier rate = sum of all `earnings[].amount`** for the load (e.g. `150 + 4500 = 4650`).
- If `deductions[]` is non-empty, **each deduction must be reported in the reply with its
  reason/description and amount**, and the net is `sum(earnings) − sum(deductions)`.
- If `deductions[]` is empty, state "no deductions on file".
- Compare the computed carrier rate (or net, when the sender's figure is clearly net) against
  the sender's stated amount → match / mismatch. **Mismatch never auto-sends** (see §8.5).

---

### 4.2 Shared tools

#### `classify_intent`
```
Input:  { email_subject, email_body, thread_text }
Output: {
  intents: ["payment_status" | "rate_verification" | "neither" | "uncertain"],
  confidence: number,
  secondary_asks: ["factoring_setup" | "paperwork_receipt" | ...]
}
```

#### `extract_identifiers`
```
Input:  { subject, body, html_tables, thread_text }
Output: {
  load_ids: string[],          // validated 6/7-digit candidates preferred
  stated_rates: [{ load_id?, amount }],
  carrier_names: string[],
  factoring_company?: string,
  sender_invoice_numbers: string[],  // sender's own #s — not for lookup
  column_hints: string[]       // e.g. "Reference#", "P.O. Number"
}
```
Rules: parse subject + body + thread + tables; ignore sender Invoice# when Reference#/PO/Load# present; our invoice # = our load #.

#### `route_load`
```
Input:  { load_id: string }
Output: { system: "transport_pro" | "quickbooks" | "invalid", length: number }
```

#### `detect_sensitive_change`
```
Input:  { subject, body, attachments_metadata }
Output: {
  flags: ["bank_change" | "noa_setup_change" | "email_contact_change" | "none"],
  evidence: string[],
  action: "escalate" | "continue"
}
```

#### `check_authorization`
```
Input:  { sender_email, sender_name, load_id, system }
Output: { decision: "ALLOW" | "DENY" | "FACTORING", matched_party?: string, reason: string }
```

#### `carrier_cross_check`
```
Input:  { load_id, system }
Output: {
  ok: boolean,
  delivered_carrier?: string,
  settlement_carrier?: string,
  payout_amount?: number,
  issues: string[]   // e.g. "canceled_row_ignored", "settlement_empty", "mismatch"
}
```

#### `apply_bulk_fallback`
```
Input:  { load_count: number, threshold?: number }
Output: { use_portal: boolean, portal_url: "https://circledelivers.com/payment-status-lookup/" }
```

#### `compute_scheduled_pay_date`  (deterministic — §4.1.1)
```
Input:  { estimated_payment_date: string, actual_payment_date?: string, tz?: "EDT" }
Output: {
  scheduled_pay_date: string,        // resolved Monday/Thursday date (EDT)
  basis: "actual" | "estimated",     // "actual" passes through actual_payment_date
  estimated_weekday: string,
  rule_applied: string               // e.g. "Wed → Thu (same week)"
}
```
Applies the Monday/Thursday mapping table. Errors (missing/invalid date) return
`{ ok: false, error }` — the line is then reported as undetermined, never guessed.

#### `compute_carrier_rate`  (deterministic — §4.1.1)
```
Input:  { earnings: [{ title, amount }], deductions?: [{ title, amount, reason? }] }
Output: {
  gross_rate: number,                // sum of earnings amounts
  deductions: [{ title, amount, reason }],
  total_deductions: number,
  net_rate: number,                  // gross_rate − total_deductions
  earnings_breakdown: [{ title, amount }]
}
```
Deductions list is echoed verbatim into the reply with reasons (§3.2).

---

### 4.3 Transport Pro tools (7-digit)

Map to **Load Summary** screens.

#### 4.3.0 Actual API payload (authoritative shape)

The live Transport Pro endpoint returns a load object in this shape (one representative sample).
Tool wrappers below normalize from this payload; the fields here are the source of truth for
grounding.

```jsonc
{
  "load_id": 2462934,
  "billing_status": "BILLED",              // e.g. BILLED, Billing Open, Waiting for Documents
  "account_information": {
    "company_name": "Idea Expedited, Inc",
    "dot_number": "2363192",
    "mc_number": null,
    "address": "5858 W Addison St", "city": "Chicago", "state": "IL", "zip": "60634",
    "remit_to": { "send_payment_to": "self", "company_name": "Idea Expedited, Inc" }
  },
  "deductions": [],                        // [{ title, amount, reason? }] — report each with reason
  "earnings": [                            // carrier rate = SUM of these amounts
    { "title": "TRUCK ORDER NOT USED", "amount": 150,
      "payment_status": "Pending", "settlement_id": null,
      "estimated_payment_date": "2026-08-19", "actual_payment_date": null,
      "payment_method": null, "check_number": null },
    { "title": "Brokerage Line Haul", "amount": 4500,
      "payment_status": "Pending", "settlement_id": null,
      "estimated_payment_date": "2026-08-19", "actual_payment_date": null,
      "payment_method": null, "check_number": null }
  ],
  "shipment_information": {
    "waypoints": [
      { "type": "Pickup",   "city": "Spokane",        "state": "Washington",
        "date": { "timestamp": "2026-06-23T16:24:00Z", "timezone": "PDT" } },
      { "type": "Delivery", "city": "Lithia Springs",  "state": "Georgia",
        "date": { "timestamp": "2026-06-29T16:00:00Z", "timezone": "EDT" } }
    ]
  }
}
```

Grounding rules for this payload:
- **Payment status/date:** per `earnings[]` line, use `payment_status`,
  `estimated_payment_date` (EDT), and `actual_payment_date`. Resolve the customer-facing pay
  date via `compute_scheduled_pay_date` (Mon/Thu rule, §4.1.1). Include `payment_method` and
  `check_number` when present.
- **Carrier rate:** `compute_carrier_rate` sums `earnings[].amount`; deductions subtracted and
  each reported with reason.
- `billing_status` maps to load status; `remit_to` indicates self-pay vs factoring
  (a factoring remit-to still routes sensitive *setup changes* to escalation per §4.2 `detect_sensitive_change`).

#### `tp_get_load_summary`
```
Input:  { load_id: string }
Output: {
  load_id, load_status,
  pickup_date, delivery_date,
  total_freight_bill,      // customer revenue (e.g. 3200)
  total_payout,            // carrier rate — authoritative (e.g. 2900)
  revenue_freight, expense_freight,
  carrier_rep: { name, email, phone? },
  order_taker?: { name, email? },
  bill_to?, miles?, rpm?
}
```

#### `tp_get_dispatch_history`
```
Input:  { load_id: string }
Output: {
  rows: [{
    carrier_name, mc_number?,
    freight_bill, dispatch_status,  // Delivered | Canceled Customer Refused | ...
    pickup, delivery, comment, last_updated
  }],
  delivered_row?: { ... }   // convenience: the Delivered row if any
}
```
**Rule:** Use **Delivered** row only for carrier + rate. Ignore canceled rows.

#### `tp_get_settlement_entries`
```
Input:  { load_id: string }
Output: {
  entries: [{
    carrier_name?, amount, settle_date?, pay_date?,
    payment_method?, check_or_ref?,
    line_type?: "advance" | "fee" | "claim" | "short_pay" | "addition" | "settlement" | "other",
    description?
  }],
  empty: boolean   // true when "no settlement entries found"
}
```

#### `tp_get_file_history`
```
Input:  { load_id: string }
Output: {
  documents: [{
    file_type,           // "Carrier Invoice" | "Bill Of Lading" | "Carrier Rate Agreement" | ...
    index_date, upload_date, indexed_by,
    comments,            // e.g. "Invoice number 4540 Load Number 2484035", "POD", "CANCEL LOAD Confirmation..."
    matches_load: boolean  // comments contain this load_id
  }],
  has_carrier_invoice: boolean,
  has_bol_or_pod: boolean,
  has_rate_agreement: boolean,
  has_cancel_confirmation: boolean
}
```
**Rules:** Match docs to load via Load Number in Comments; POD may be in Comments; CANCEL LOAD Confirmation → escalate for rate auto-answer.

#### `tp_get_notes`
```
Input:  { load_id: string }
Output: {
  notes: [{ added_by, last_updated, message, timestamp }],
  location_history_comments?: [{ date, entered_by, comment }]  // e.g. "POD in @ ... uploaded"
}
```
**Guardrail:** do not expose internal-only notes externally; escalate if unsure.

#### `tp_get_noa_factoring`
```
Input:  { load_id: string }
Output: {
  noa_on_file: boolean,
  factoring_company_on_file?: string,
  details?: string
}
```
Read-only. Setup changes escalate via `detect_sensitive_change`.

#### `tp_search_by_carrier`
```
Input:  { carrier_name: string, limit?: number }
Output: {
  matches: [{ load_id, load_status, carrier_name, total_payout? }],
  count: number
}
```

---

### 4.4 QuickBooks Online tools (6-digit)

#### `qbo_find_by_load`
```
Input:  { load_id: string }
Output: { found: boolean, object_type?: "Bill" | "Invoice" | "other", qbo_id?: string }
```

#### `qbo_get_payment_status`
```
Input:  { load_id: string }
Output: {
  status, due_or_expected_pay_date?, paid_date?,
  amount?, payment_method?, payment_ref?,
  vendor_name?, vendor_email?
}
```

#### `qbo_get_line_items`
```
Input:  { load_id: string }
Output: {
  lines: [{ description, amount, type? }],
  deductions_or_adjustments: [...]
}
```

#### `qbo_search_by_vendor`
```
Input:  { vendor_name: string, limit?: number }
Output: { matches: [{ load_id, status, amount? }], count: number }
```

---

### 4.5 Gmail tools

#### `gmail_fetch_new`
```
Input:  { mailbox: "paystatus@circledelivers.com", include_spam: true, since?: string }
Output: { messages: [{ message_id, thread_id, from, subject, body, html, labels }] }
```

#### `gmail_create_draft`
```
Input:  { thread_id, to, subject, body_html_or_text }
Output: { draft_id }
```

#### `gmail_send_reply`
```
Input:  { thread_id, message_id_in_reply_to, body, to? }
Output: { sent_message_id }
```
**Only callable after pre-send gate + Slack Approve (Phase 1).**

---

### 4.6 Slack tools

#### `slack_post_approval`
```
Input: {
  channel: "#payments-approvals",
  summary: { from, intents, load_ids, key_facts },
  draft_reply: string,
  correlation_id: string   // maps to email message_id
}
Output: { slack_ts, channel }
```
Block Kit buttons: **Approve** | **Edit** | **Reject**.

#### `slack_post_escalation`
```
Input: {
  channel: "#payments-security" | "#payments-approvals" | "dm:user",
  severity: "security" | "review" | "sales_rep",
  reason: string,
  load_ids?: string[],
  correlation_id: string
}
Output: { slack_ts }
```

#### `slack_handle_interaction` (callback handler, not agent-called)
```
Input:  Slack signed payload { action, user, correlation_id, edited_text? }
Output: { result: "sent" | "rejected" | "updated" }
```

---

## 5. Pre-Send Gate (deterministic — not a skill)

Runs in **code** before `gmail_send_reply`:

| Check | Pass condition |
|---|---|
| Authorization | `check_authorization` = ALLOW for every load disclosed (FACTORING only if policy allows) |
| Fraud / sensitive change | No bank/NOA-setup/email-change escalate flag |
| Grounding | Every amount/date/status in draft traces to a tool result; scheduled pay dates come only from `compute_scheduled_pay_date` and carrier rate only from `compute_carrier_rate` (no model-derived math) |
| Length routing | Only 6/7-digit loads looked up; invalid lengths not answered as found |
| Bulk | If over threshold, only portal reply allowed |

Fail → Slack escalate; **never send**.

---

## 6. Skill ↔ Tool Matrix

| Tool | payment_status | rate_verification | carrier_name_lookup | shared |
|---|:---:|:---:|:---:|:---:|
| `classify_intent` | ● | ● | ○ | ● |
| `extract_identifiers` | ● | ● | ● | ● |
| `route_load` | ● | ● | ○ | ● |
| `detect_sensitive_change` | ● | ● | ○ | ● |
| `check_authorization` | ● | ● | ● | ● |
| `carrier_cross_check` | ● | ● | ○ | ○ |
| `compute_scheduled_pay_date` | ● | ○ | ○ | ○ |
| `compute_carrier_rate` | ○ | ● | ○ | ○ |
| `tp_get_load_summary` | ● | ● | ○ | ○ |
| `tp_get_dispatch_history` | ● | ● | ○ | ○ |
| `tp_get_settlement_entries` | ● | ● | ○ | ○ |
| `tp_get_file_history` | ● | ● | ○ | ○ |
| `tp_get_notes` | ○ | ○ | ○ | ○ |
| `tp_get_noa_factoring` | ○ | ● | ○ | ○ |
| `tp_search_by_carrier` | ○ | ○ | ● | ○ |
| `qbo_*` | ● (6-digit) | ● (6-digit) | ○ | ○ |
| `gmail_*` | ● | ● | ● | ● |
| `slack_*` | ● | ● | ● | ● |

● = required/typical ○ = optional/as needed

---

## 7. Example Agent Traces

### 7.1 Payment status (7-digit)
1. `classify_intent` → `payment_status`
2. `extract_identifiers` → `["2484035"]`
3. `detect_sensitive_change` → none
4. `route_load("2484035")` → `transport_pro`
5. `tp_get_load_summary` → status Waiting for Documents / Billing Open; payout 2900
6. `tp_get_dispatch_history` → Delivered = Extra Trans Inc $2900; ignore canceled $3100
7. `tp_get_settlement_entries` → empty
8. `tp_get_file_history` → invoice + BOL/POD present
9. `carrier_cross_check` → delivered carrier OK; settlement empty (not settled)
10. `check_authorization` → ALLOW
11. Draft: not yet settled; docs on file → escalate Carrier Rep if needed
12. `slack_post_approval` → Approve → `gmail_send_reply`

### 7.2 Rate verification (7-digit)
1. `classify_intent` → `rate_verification`
2. `extract_identifiers` → load `2499505`, stated amount `9300`, factor `England Carrier Services`
3. `route_load` → `transport_pro`
4. `tp_get_load_summary` + `tp_get_dispatch_history` + `tp_get_settlement_entries` + `tp_get_noa_factoring`
5. Compare stated 9300 vs Total Payout; list deductions; confirm NOA read-only
6. Gate + Slack + send

### 7.3 6-digit load
Same skills; `route_load` → `quickbooks`; use `qbo_get_payment_status` / `qbo_get_line_items` instead of TP tools.

### 7.4 Worked example — load `2462934` (real payload, §4.3.0)

**Payment status:**
- `earnings` = "TRUCK ORDER NOT USED" $150 + "Brokerage Line Haul" $4,500; both
  `payment_status: Pending`, `estimated_payment_date: 2026-08-19` (EDT), `actual_payment_date: null`.
- `2026-08-19` is a **Wednesday** → `compute_scheduled_pay_date` → **Thursday 2026-08-20**
  (rule: Wed → Thu same week).
- Draft: status Pending / BILLED; scheduled payment **Thu Aug 20, 2026**; total pending $4,650;
  method not yet assigned. (⚠ same-week Thursday assumes Mon/Thu pay same-day when the date
  lands on one — pending owner confirmation, §4.1.1.)

**Rate verification:**
- `compute_carrier_rate` → gross = `150 + 4500 = 4650`; `deductions: []` → net = **$4,650**.
- Draft: carrier rate $4,650 (Brokerage Line Haul $4,500 + Truck Order Not Used $150);
  no deductions on file; compare to sender's stated amount → match/mismatch.

---

## 8. Implementation Notes

### 8.1 Runtime
- Preferred: **Amazon Bedrock** Agents / tool-use with Python Lambda tool handlers.
- Store tool schemas (JSON Schema / OpenAPI-style) next to each handler.
- Log **every** tool call + result for audit and grounding checks.

#### 8.1.1 Low-cost AWS service map
All components are pay-per-use so cost scales with email volume; there is no always-on server.

| Job | AWS service | Cost rationale |
|---|---|---|
| Scheduled poll of inbox | **EventBridge Scheduler** → Lambda | No idle compute; runs only on schedule |
| Agent orchestration | **Bedrock Agents** (tool-use) | Token-metered only |
| Classification / extraction (high volume) | **Claude Haiku** on Bedrock | Cheap model for the frequent, simple steps |
| Final reply drafting only | **Claude Sonnet** on Bedrock | Larger model reserved for the last step, not every turn |
| Tool handlers (TP, QBO, Gmail, Slack) | **AWS Lambda** (Python) | Billed per invocation |
| Pre-send gate | **AWS Lambda** (deterministic code) | No model calls |
| Slack interactivity callback | **Lambda + Function URL / API Gateway** | Event-driven, minimal |
| Audit log (every tool call + result) | **DynamoDB** or **S3** | Cheap at this volume; required for grounding |
| Secrets (TP/QBO keys, Slack tokens, Gmail SA) | **SSM Parameter Store** (or Secrets Manager) | Parameter Store cheaper at this scale |
| Correlation-ID / run state | **DynamoDB** (on-demand) | Pay-per-request |

**Primary cost lever:** route `classify_intent` and `extract_identifiers` to the cheap model,
perform lookups deterministically in code, and invoke the larger model only for the final
draft. Most messages never require the larger model.

#### 8.1.2 Gmail access
- **Production:** use a **Gmail API service account with domain-wide delegation** scoped to
  `paystatus@circledelivers.com`. Server-side, unattended, and durable — no interactive OAuth
  session to expire. This is what the `gmail_*` tools (§4.5) assume.
- **Prototyping only:** an MCP / OAuth Gmail connector is acceptable for manual experimentation,
  but is **not** the deployment path for a scheduled bot.
- Scopes: read (`gmail.readonly` or `gmail.modify` for label/thread handling) + send
  (`gmail.send`). Least-privilege; no delete scope.

### 8.2 Skill prompts (store as versioned text)
Each skill = short system instruction:
- When to activate
- Mandatory tools before drafting
- Forbidden actions (invent dates, honor bank changes, modify NOA)
- Reply template reference (main PRD §9)

### 8.3 Build order
1. TP tool wrappers (`tp_get_load_summary`, dispatch, settlement, file history)
2. QBO tool wrappers
3. Shared tools (extract, route, auth, sensitive change)
4. Skill prompts + agent wiring
5. Gmail + Slack + pre-send gate
6. Regression fixtures from main PRD Samples A–L

### 8.4 Testing
- Unit-test each tool against mocked TP/QBO responses (use Load `2484035` as fixture).
- Gate tests: deny send on DENY auth, bank_change flag, ungrounded draft.
- Skill integration tests: Sample emails A–K → expected tool sequence + draft shape.

### 8.5 Phased rollout: draft → human-approve → auto-send

The bot graduates from human-in-the-loop to selective automation. The pre-send gate (§5)
runs in **every** phase and is never bypassed; automation only removes the *human* approval
click for email types that have earned it.

| Phase | Behavior | Exit criteria to advance |
|---|---|---|
| **Phase 1 — Approve** | Every draft posts to Slack; a human must Approve/Edit/Reject before `gmail_send_reply`. | High approval rate with few edits across a meaningful sample, per intent type. |
| **Phase 2 — Selective auto-send** | Low-risk intent types (e.g. clean single-load `payment_status` that passes the gate) send automatically; all others still require Slack approval. | Sustained low correction/complaint rate on auto-sent types. |

**Never auto-send (always human-reviewed), regardless of phase:**
- Any `detect_sensitive_change` flag (bank change, NOA/factoring setup change, contact change)
- `check_authorization` = FACTORING (unless policy explicitly allows) or any DENY
- Rate **mismatch** in `rate_verification`, carrier cross-check issues, or cancel/re-book ambiguity
- Bulk over threshold (portal fallback), or any draft failing the grounding check

**Metrics to gate promotion (log per intent type):** approval rate, edit rate, reject rate,
post-send correction/complaint rate, and gate-block rate. Promote an intent to auto-send only
when these hold steady over a representative volume; demote automatically on a spike.

---

## 9. Open Dependencies (from Phase 0)

Blocking for tool implementation:
- [ ] Transport Pro API endpoints matching Load Summary sections
- [ ] QuickBooks object/field for 6-digit load number
- [ ] Gmail service account for `paystatus@`
- [ ] Slack App (Bot Token, Interactivity, signing secret)
- [ ] Factoring authorization policy (ALLOW vs escalate for FACTORING decision)

---

## 10. Document Control

| Version | Date | Notes |
|---|---|---|
| v1.0 | 2026-07-17 | Initial skills & tools catalog split from main PRD |
| v1.1 | 2026-07-24 | Added §8.1.1 low-cost AWS service map + cost lever, §8.1.2 Gmail service-account guidance, §8.5 phased draft→auto-send rollout |
| v1.2 | 2026-07-24 | Added §4.1.1 payment-date (Mon/Thu) + rate-computation rules, §4.3.0 actual TP payload, `compute_scheduled_pay_date` & `compute_carrier_rate` tools; updated payment_status/rate_verification skills, matrix, and grounding gate |

**Related:** `PRD_Payment_Status_Email_Bot.md` — product requirements, edge cases, samples,
tech stack, Phase 0 checklist.
