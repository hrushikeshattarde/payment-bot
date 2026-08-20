# Chat Approval Plan — Approve in Chat, Send From the Approver's Address

Each gate-passing draft is posted as a card into a chat space the three reviewers
share. Any of them clicks **Approve & send as me**; the reply goes out **from that
person's own address**, `Cc: paystatus@circledelivers.com`,
`Reply-To: paystatus@circledelivers.com`. The pending queue is the chat feed itself:
everyone sees what is waiting, who sent what, and when — so an absent reviewer's
pending work is simply visible work for whoever is present.

Written 2026-08-19. **Implemented 2026-08-19, dark** — the code is in the tree with
every switch at its off default (`ApprovalMode=drafts`, `ChatSpace` blank), so a deploy
today is behaviourally identical to the stack before it existed; §10's build order now
starts at its manual step 1. Implementation map: `payment_bot/approvals.py` (pending
entries, claims, results, the posted-ledger), `payment_bot/clients/google_chat.py`
(cards + posting), `payment_bot/chat_callback.py` (the callback — the one module that
sends), wiring in `local_runner.process_inbox` / `lambda_handler`, and the conditional
callback stack in `deploy/template.yaml` (no `ChatSpace` → no function, no URL).

This is the implementation plan for "option 3" and an
**alternative** to [REVIEWER_DRAFTS_DESIGN.md](REVIEWER_DRAFTS_DESIGN.md) (drafts
placed in reviewers' mailboxes with timeout reassignment). Implement one, not both —
§11 lists exactly which pieces of that design carry over here and which are parked.
This is also "Stage 2" arriving: the codebase reserved a seam for it — the
`SlackClient` protocol with `post_approval` / `ApprovalAction` / `ApprovalResolver`
already exists with only Mock/Null implementations, and the worker's own docstring
says *"Stage 2 is where sending moves, into a separate callback function with its
own role."* This plan fills that seam; it does not invent a new one.

---

## 1. The identity rule (the design's spine)

**The send is always executed as the person who clicked.** The callback impersonates
the clicker — and only ever the clicker — via the existing domain-wide delegation
(any domain user is a valid subject; proven live 2026-08-04; `gmail.compose` already
includes sending, so **no new Google-side grants**). Concretely, every send carries:

* `From:` the approver — Gmail stamps the authenticated sender, so this is enforced
  by *which mailbox executes the send*, not by a header the code could get wrong;
* `Cc: paystatus@circledelivers.com` (the existing `reply_cc` setting) — the reply
  enters the group's record, and the domain rule in `_is_ours` then marks the thread
  human-owned for every future worker run;
* `Reply-To: paystatus@circledelivers.com` — a plain Reply returns to the group, not
  to one person's inbox;
* `In-Reply-To` / `References` from the carrier's message, so the reply threads at
  the carrier's end; the send is also placed in the approver's own copy of the group
  thread (resolved by `rfc822msgid`, with the unthreaded fallback from
  REVIEWER_DRAFTS_DESIGN §4.2), so their Sent view reads normally.

Three consequences worth stating because they are the safety story:

* Reviewer A can never cause a send under reviewer B's name. There is no "send as"
  parameter anywhere — the identity comes from the chat platform's verified event.
* Only identities on the reviewer roster may trigger a send at all; anyone else in
  the space gets "not authorised to send" and a log line.
* Clicking Approve **is** the per-send consent that REVIEWER_DRAFTS_DESIGN §11
  demanded before anything goes out under a person's name. Auto-send stays out of
  scope (§12).

## 2. What the reviewers experience

A card appears in the shared space per gate-passing reply: carrier and sender,
load id(s), the full draft text, the recipients as they will appear
(To / Cc / Reply-To), and three buttons:

* **Approve & send as me** — sends immediately, from the clicker. The card updates
  in place: *"Sent by Priya · 14:32 · from priya@…"*, buttons removed.
* **Move to my Gmail Drafts** — the edit path (§6): the draft lands in the clicker's
  own mailbox, threaded into their copy of the conversation, for editing and manual
  send from Gmail. Card updates: *"With Priya in Gmail"*.
* **Reject** — no send; card updates *"Rejected by Priya — needs a human reply"*, and
  the mail sits unread in the group like any escalation. Rejection is a task marker,
  not a deletion.

Escalations and gate-blocked mail are posted too — as **notice cards** with no buttons,
carrying the short reason the pipeline recorded ("pre-send gate blocked: …",
"sensitive change […]") and the load ids. (Revised 2026-08-19 at the user's direction —
the original plan skipped these; the space is now the full feed of everything the bot
did with the inbox.) Their mailbox behaviour is unchanged: the mail sits unread, and
the card is the task marker. Because escalations re-run every poll by design, notice
cards dedup through a small posted-ledger (`state/chat_post_ledger.json`) so each is
posted once, not every 20 minutes.

## 3. Architecture

Two functions, one shared state prefix, no queue, no database:

* **The worker (existing Lambda, modified).** When chat mode is on, a gate-passing
  reply is *not* saved to the reading mailbox's Drafts. Instead the worker writes a
  **pending entry** to S3 (`state/approvals/<sha of message-id>.json` in the existing
  config bucket, under the existing `state/*` grant) holding everything a send needs
  — full body, recipients, threading headers, thread id, load ids — and posts the
  card, embedding only the entry's id in the buttons. If the chat post fails, the
  worker **falls back to today's behaviour** (draft in the reading mailbox) and logs
  `chat_post_failed`: a chat outage degrades to the current workflow, never to
  silence. The never-send invariant is untouched — the worker still cannot send.
* **The callback (new, small Lambda + Function URL).** Receives button clicks,
  verifies the platform's signature (§7), maps the clicker to a roster email, claims
  the entry (§5), executes the action (send-as-clicker / move-to-drafts / reject),
  updates the entry and the card. Deployed from a **lean bundle** — the Gmail HTTP
  client and token minting already in `clients/` plus stdlib; no pydantic, no
  pipeline imports — so cold starts stay under the chat platforms' reply deadlines.
  It gets **its own IAM role**: the Google SA secret, `state/approvals/*` read/write,
  and nothing else — no Bedrock, no rosters, no Transport Pro. Sending capability
  lives in exactly one deployable, which is what the Stage-2 split was for.

The pending entry doubles as the **duplicate guard**: the worker checks the pending
ledger by thread before drafting, exactly where the draft-in-thread check fires
today — same reasoning as REVIEWER_DRAFTS_DESIGN §4.1, same failure tolerance (a
lost entry costs one duplicate card, not a wrong send, because the claim protocol
still applies).

## 4. Platform: Google Chat or Slack

The plan is platform-agnostic above this line; these are the real differences:

| | **Google Chat** | **Slack** |
|---|---|---|
| Cost / accounts | Included in Workspace; reviewers already have it | Separate product; whatever the org pays today |
| Clicker identity | Event carries the **verified Workspace email** — matches the roster natively | Slack user id → email needs a lookup or a static 3-entry map in config |
| Endpoint verification | Google-signed ID token; verify audience against certs — **no stored secret** | Signing secret + bot token — two values to store |
| Reply deadline | 30 s synchronous — comfortable for cold start + Gmail send | 3 s — workable with the lean bundle, but a cold start plus a slow send flirts with it (Slack retries; the claim protocol absorbs the retry) |
| Worker posting | Chat API with the **same service account** (app auth, `chat.bot` scope — a GCP-project setting, not a DWD grant; enable Chat API in `gsheets-python-350615`) | Bot token stored in SSM |
| Codebase fit | Implements the existing `SlackClient` protocol under a different transport | The protocol is named for it; PRD §4.6 anticipated `#payments-approvals` |

**Recommendation: Google Chat, unless the three reviewers already live in Slack all
day.** Native identity, no secrets to store, a forgiving deadline, and no second
SaaS. The PRD's Slack naming was a plan, not a commitment — the protocol seam
accepts either. **This is Decision 1 in §10 and blocks step 1.**

## 5. Correctness: claims, double-clicks, retries

Two reviewers clicking Approve within the same second must produce exactly one email.

1. **Claim**: `PUT state/approvals/claims/<id>` with `If-None-Match: *` (S3
   conditional write), body `{action, user, at}`. Exactly one caller wins; the loser
   gets HTTP 412 and replies "already being handled by <winner>". This is also what
   makes platform retries (Slack re-POSTs on a slow response) harmless.
2. **Act**: send / move / reject as the claimed action.
3. **Record**: entry status → `sent` / `moved` / `rejected` with who and when; card
   updated in place so the feed is the audit trail humans read.
4. **On failure between 1 and 3** (Gmail 5xx, timeout): delete the claim, restore
   the card's buttons with a visible *"send failed — try again"*, log
   `approval_send_failed`. A claim older than a few minutes with no recorded result
   is treated the same way by the next worker run, so a callback crash cannot wedge
   an entry forever.

Post-send, the `Cc: paystatus@` copy lands in the reading mailbox's thread and the
domain rule owns it — belt and braces on top of the entry's terminal status. Worker
re-posts are idempotent by construction: an entry (any status) for the message id
suppresses a new card; a carrier **follow-up in the same thread** while a card is
pending is suppressed by the by-thread check, and after a send it takes the normal
new-message path.

**Expiry**: a pending entry untouched for `PAYBOT_APPROVAL_EXPIRY_DAYS` (default 3)
is marked `expired`, the card updated to say so. Same semantics as
`gate_block_retries_exhausted`: the mail sits unread, nothing retries it, a human
must act. Terminal entries are pruned after 7 days, mirroring `BlockLedger`.

## 6. The edit path

Editing inside chat is deliberately **not** built first. A modal with a text box is
possible on both platforms, but it invites rewriting a payment answer in a cramped
box with no thread context. Instead, **Move to my Gmail Drafts** reuses
REVIEWER_DRAFTS_DESIGN's create-in-reviewer-mailbox mechanics (resolve their copy by
`rfc822msgid`, draft with their From + the standard Cc/Reply-To, threaded) — the
reviewer edits where they can see the whole conversation and sends from Gmail. The
entry closes as `moved`; from that point the send is a normal human send, visible to
the worker through the Cc copy and thread ownership like any colleague reply.

Edited text — whether via this path or a future modal — is human-authored and
carries the editor's name; it gets the same trust as a hand-edited Gmail draft today.

## 7. Security

* **Verify before parse.** Google Chat: validate the bearer ID token's signature and
  audience. Slack: HMAC signature plus a ±5-minute timestamp window against replays.
  Unverified requests get 401 and one log line; no branch of unverified code touches
  S3 or Gmail.
* **Content never comes from the chat payload.** Buttons carry an opaque entry id
  and an action name — the body, recipients, and headers come from the S3 entry the
  *worker* wrote. A tampered payload can at most reference a different pending entry,
  which then still sends only what the gate passed, from the clicker.
* **Roster check** on the verified identity, before the claim. The mapping is config
  (§9), three entries, reviewed like any authorization list in this repo.
* **The Function URL is public by necessity** (chat platforms have no fixed egress).
  Verification is the perimeter; the URL itself is unguessable but treated as known.
* **Audit**: every callback invocation logs verified identity, action, entry id, and
  outcome — `approval_sent`, `approval_moved`, `approval_rejected`,
  `approval_denied_not_reviewer`, `approval_send_failed`. Log lines only; no new
  metric filters or alarms at launch, for the §8 reason.

## 8. AWS cost — honest accounting

The previous design achieved literally zero new resources. **This one cannot**: an
inbound webhook needs an endpoint. The delta, itemised:

| Item | Delta |
|---|---|
| Callback Lambda + Function URL | Function URLs are free; invocations = button clicks, tens/day → fractions of a cent even off free tier. **No API Gateway** (that would be the expensive way to do this). |
| Callback log group | Same 90-day retention; a few KB/day. |
| Chat credentials | **SSM Parameter Store SecureString, not Secrets Manager** — Standard parameters are free; a new Secrets Manager secret is $0.40/month, and on Google Chat there is no secret to store at all. |
| State | `state/approvals/*` in the existing bucket/grant; request counts comparable to the block ledger's. Cents. |
| Worker | One chat POST + one S3 write per gate-passing reply, replacing one Gmail draft call. No runtime pressure. |
| Metrics / alarms | None added (seven custom metrics already sit against CloudWatch's ten free; adding is a knowing $0.30/month decision *later*, if expiry/rejection rates prove worth paging on). |
| Model / scopes / secrets | Bedrock unchanged; Google scopes unchanged; the callback *reuses* the existing SA secret via its own role's grant. |

Realistic total: **under ~$0.25/month**, dominated by log storage. The `paybot@`
licence stays cancelled.

## 9. Configuration and template deltas

| Setting | Parameter | Default | Meaning |
|---|---|---|---|
| `PAYBOT_APPROVAL_MODE` | `ApprovalMode` | `drafts` | `drafts` = today's behaviour; `chat` = post cards, skip reading-mailbox drafts. The kill switch: flipping back is total rollback. |
| `PAYBOT_REVIEWERS` | `Reviewers` | `[]` | JSON list of reviewer emails (shared with the parked design). Same JSON-list footgun as `ReplyCc`. |
| `PAYBOT_CHAT_SPACE` | `ChatSpace` | `""` | Google Chat space name (`spaces/…`) or Slack channel id. |
| `PAYBOT_CHAT_REVIEWER_MAP` | — | `{}` | Slack only: user id → email, three entries. Empty on Google Chat (identity is native). |
| `PAYBOT_APPROVAL_EXPIRY_DAYS` | `ApprovalExpiryDays` | `3` | §5 expiry. |
| `ReplyCc` | — | `["paystatus@circledelivers.com"]` | Existing parameter, new value. |

Template additions: the callback function, its role, its log group, its Function URL,
and (Slack only) two SSM parameters referenced by name.
[DEPLOY_CHECKLIST.md](DEPLOY_CHECKLIST.md) gains: roster/space review, and the §10
ordering constraints.

## 10. Build order

Each step is deployable and verifiable alone; each is also the rollback point for
the next.

1. **Decision 1 — platform** (blocks everything): Google Chat unless the three live
   in Slack. Then the manual setup: create the space with the three reviewers +
   operator; enable Chat API and configure the app in `gsheets-python-350615` (or
   create the Slack app, capture signing secret + bot token into SSM).
2. **Shadow cards.** Worker posts cards **without buttons** while still creating
   reading-mailbox drafts as today (`ApprovalMode=drafts` + a `ChatSpace` set).
   Zero behaviour change; verifies posting, formatting, card size limits (long
   multi-load drafts must render — truncate the *card* view if needed, never the
   stored entry). Runs alongside real traffic for a few days.
3. **Callback, roster of one.** Deploy the callback; buttons appear; roster =
   operator only. Approve a real card: verify the send leaves **from the operator's
   address**, Cc/Reply-To correct, threads at the carrier's end, card updates,
   double-click loses cleanly, `Move to my Drafts` lands threaded, Reject holds.
4. **Cut over.** `ApprovalMode=chat` (reading-mailbox drafts stop), roster = all
   three, after a walkthrough with the reviewers — they are lending their names;
   they should see the flow before it uses them. **Precondition: the workstation
   task is retired** (disabled, kept as rollback) — it would keep writing
   reading-mailbox drafts nobody is watching anymore.
5. **Watch one week.** The feed itself is the report: pending age, rejection rate,
   expiry count. Tune `ApprovalExpiryDays` or card copy; only then decide whether
   any signal deserves a paid metric.

**Rollback at any step**: `ApprovalMode=drafts`. Pending entries expire on their own;
cards stay in the feed as history; drafts resume in the reading mailbox. Nothing to
migrate.

## 11. Relationship to REVIEWER_DRAFTS_DESIGN.md

Carried over (build once, shared): the `Reply-To` addition to `build_reply`; the
`ReplyCc` value; the `rfc822msgid` copy-resolution with its fallbacks (§4.2 there —
used here by threading and by *Move to my Drafts*); the reviewer-mailbox trust
discipline (§7 there — the callback writes drafts/sends only for the verified
clicker, logged); the S3-ledger idiom and prune contract.

Parked, not built: the assignment rotation, the business-hours deadline, the
reassignment sweep, and the assignment ledger — the chat feed replaces routing with
visibility. If chat approval is later abandoned, that design remains implementable
as written.

## 12. Out of scope, deliberately

* **Auto-send in any form.** Under a personal From it would put words in a named
  person's mouth without their click (§1). If selective auto-send ever returns, it
  returns under a non-personal sender and its own design.
* **Inline edit modals** — revisit only if *Move to my Drafts* proves too slow in
  practice (§6).
* **Escalation cards** — escalations keep their current path; posting them to chat
  is a separate, smaller decision this plan does not depend on.
