# Reviewer-Sent Drafts — Design

> **Status (2026-08-19): an alternative is being planned instead** — chat-based
> approval where the send goes out from whoever clicks Approve; see
> [CHAT_APPROVAL_PLAN.md](CHAT_APPROVAL_PLAN.md). Implement one, not both. That plan's
> §11 lists which sections here carry over (§3, §4.2, §4.4, §7) and which are parked
> (§4.1, §5 — the assignment/reassignment machinery). This document stays as written
> so the parked design remains implementable if chat approval is abandoned.

Drafts are created **in the three reviewers' own mailboxes** and go out **from their own
addresses** when they press Send. The bot assigns each draft to one reviewer, watches
whether it moves, and re-assigns it to the next reviewer if it sits untouched — so a
draft never waits in a holiday-parked Drafts folder.

Written 2026-08-19. This supersedes the 2026-08-17 decision to create a dedicated
Workspace user (`paybot@circledelivers.com`): the whole reason for that account was to
give drafts a neutral mailbox to live in, and under this design they live in the
reviewers' mailboxes instead. **The account is not needed and should not be created**
(saves the ~$7/user/month Business Starter licence). The reading side keeps the working
configuration proven live on 2026-08-04: impersonate one real user, query
`is:unread to:paystatus@circledelivers.com` (a Google Group cannot be impersonated).

**Hard constraint: zero new AWS spend.** §8 itemises why the delta is cents, not
resources — no new Lambda, no new schedule, no database, no new metrics or alarms.

---

## 1. The requirement

Three people answer payment-status and rate-verification mail. The bot drafts the
replies. The business wants:

* replies sent **from the reviewers' own addresses** — a named person, not a bot or a
  shared identity;
* **no second login** — reviewers work in the Gmail they already have open;
* **no stranded drafts** — if one or two reviewers are away, their pending drafts must
  reach whoever is present, automatically.

Mailbox delegation on a shared account was considered and rejected by the business:
it keeps a bot identity on the wire. Creating the draft in every reviewer's mailbox was
rejected on sight: three live copies of one reply is an invitation to send two of them.
What is left is **one draft, one assignee, and machinery to move it** — which is this
design.

## 2. The design in one pass

Per gate-passing reply, inside the existing worker run (no new schedule):

1. **Pick an assignee** — deterministically from the inbound message id
   (`reviewers[hash(message_id) % n]`), so assignment needs no counter and no state,
   and a re-run of the same message picks the same reviewer.
2. **Find the reviewer's copy** of the carrier's email. Gmail thread ids are
   **per-mailbox** — the reading mailbox's `threadId` does not exist in the reviewer's
   mailbox. The reviewers are members of the `paystatus@` group, so each has their own
   copy: `GET /users/{reviewer}/messages?q=rfc822msgid:<Message-ID>` resolves it and
   its thread id.
3. **Create the draft in the reviewer's mailbox** — same `build_reply`, with
   `from_address` = the reviewer, `Reply-To: paystatus@circledelivers.com` (one new
   header, §4.4), the group in Cc via the existing `reply_cc` setting, and the
   reviewer's own `threadId` so the draft sits inside the conversation they already
   received.
4. **Record the assignment** in a small S3 ledger (`state/assignment_ledger.json`),
   modelled line-for-line on the gate-block ledger: same bucket, same `state/*` IAM
   grant, same load-tolerantly/save-in-`finally` handling.
5. **Sweep on every run** (every 20 minutes, inside the same invocation): for each
   live ledger entry past its deadline, look at the draft and act — confirmed sent,
   deleted, untouched-stale (move to the next reviewer), or edited-in-progress (leave
   it). Decision table in §5.

The reviewer experience is: a ready-to-send reply appears inside the group thread in
their own inbox, addressed correctly, threaded correctly; they read it and press Send.
Nothing to log into, nothing to learn.

## 3. What already works and does not change

Verified against the code and the live account, not assumed:

* **Impersonating any domain user needs no Google-side change.** Domain-wide
  delegation is granted per client id + scopes and applies to every user in the
  domain; it was proven live on 2026-08-04 by minting a token for a personal mailbox
  with the same client id. The reviewers are just three more subjects.
* **No new scopes.** `gmail.compose` covers draft create, read, update, delete —
  everything the sweep does to drafts — and `gmail.readonly` covers the `rfc822msgid`
  lookup. The secret's contract in `deploy/template.yaml` ("scopes stay
  gmail.readonly + gmail.compose — no new Google-side grants") holds.
* **The never-send invariant holds.** `GmailApiClient.send_reply` still raises; the
  sweep creates and deletes drafts but calls neither `drafts.send` nor
  `messages.send`. Humans remain the only path to the wire.
* **Thread ownership already recognises reviewer sends.** `_is_ours`
  ([gmail_api.py:416](../src/payment_bot/clients/gmail_api.py)) treats any
  same-domain sender as ours, so once a reviewer replies, the reading mailbox sees the
  thread as human-owned — no roster wiring needed there (`gmail_group_members` stays
  for off-domain members only, and the group-address DMARC carve-out is untouched).
* **DMARC-rewritten senders keep working.** The reply's To comes from
  `parse_inbound_email`'s recovery of `X-Original-Sender` / `Reply-To`, which this
  design reuses wholesale via `build_reply`.
* **The Cc mechanism exists.** `reply_cc` is already config
  (`ReplyCc` template parameter); setting it to
  `["paystatus@circledelivers.com"]` needs no code. Remember its footgun: the value
  must be a JSON list, never a bare address.

## 4. What breaks, and the fix for each

### 4.1 The duplicate-draft guard breaks — the ledger replaces it

Today `_thread_reply_target` skips a thread when **a draft already exists in it**.
That works only because the reading mailbox and the drafting mailbox are the same
mailbox. Move the drafts elsewhere and the reading mailbox's threads never contain
one — so without a replacement, the worker would draft the same reply again **every
20 minutes**.

The assignment ledger is that replacement, and it is the one piece of state this
design adds. It is consulted where the draft-exists check fires today: a live entry
for the thread means "already assigned, skip". Keyed by inbound **message id**
(consistent with the block ledger's reasoning — a follow-up is a new message) and
carrying the reading-mailbox **thread id** so the lookup can be by-thread:

```json
{
  "<rfc822-message-id>": {
    "thread_id":          "reading-mailbox thread id",
    "reviewer":           "a@circledelivers.com",
    "attempt":            0,
    "draft_id":           "r7726...",
    "reviewer_thread_id": "18c4...",
    "body_sha256":        "...",
    "assigned_at":        "2026-08-19T14:35:00-04:00",
    "status":             "assigned"
  }
}
```

`status` moves to `sent` or `exhausted` and terminal entries are pruned after 7 days,
mirroring `BlockLedger.PRUNE_DAYS` (the `newer_than:2d` intake window means an email
cannot outlive its entry while still being fetchable). A ledger that fails to load
degrades to empty exactly as the block ledger does — the cost is a possible duplicate
draft, which threads into the same carrier conversation where a reviewer will see and
delete it; failing the run over bookkeeping would be worse.

### 4.2 Thread ids are per-mailbox — resolve, with two fallbacks

The `rfc822msgid` lookup (§2 step 2) is the normal path. Two ways it can miss:

* **The reviewer has no copy** (not a group member, a filter archived-and-deleted it,
  or group fan-out is lagging behind the 20-minute poll). Fall through to the next
  reviewer in rotation and log `reviewer_copy_missing`. If *no* reviewer has a copy,
  create the draft **unthreaded** in the first assignee's mailbox — `In-Reply-To` /
  `References` are set from the carrier's message, so the reply still threads
  correctly at the carrier's end; it merely appears as its own conversation in the
  reviewer's mailbox.
* **The inbound has no Message-ID** (rare, malformed). Same unthreaded fallback.

### 4.3 Sent-detection must not depend on the Cc

The lazy way to confirm a send is to watch for the Cc'd copy arriving back through
the group. That fails open in the worst direction: a reviewer who edits the draft and
**removes the Cc** would send a real reply the sweep never sees — it would read the
missing draft as "deleted", reassign, and a second reviewer would answer the carrier
twice.

So the sweep confirms sends from the **reviewer's own thread**, which is authoritative
regardless of headers: when `drafts.get` 404s, fetch `reviewer_thread_id` and look for
a message from that reviewer bearing the `SENT` label. Present → `status: sent`, done.
Absent → the draft was deleted, not sent → reassign (§5). For the unthreaded-fallback
case where no `reviewer_thread_id` exists, search the reviewer's Sent for the carrier
address and reply subject before concluding "deleted".

### 4.4 Outbound headers

* `From`: the assignee. Gmail stamps the authenticated sender at send time anyway;
  setting it in the draft keeps what the reviewer sees truthful.
* `Reply-To: paystatus@circledelivers.com` — **new, one line in `build_reply`** — so
  even a plain Reply (not reply-all) returns to the group and stays visible to the
  bot and all three reviewers.
* `Cc: paystatus@circledelivers.com` via `reply_cc` — puts the outbound reply into
  the group's record and into the reading mailbox's thread, where the domain rule
  marks the thread human-owned for every future run.

The Cc'd copy also **matches the intake query** (`to:` is a superset of `cc:` in
Gmail search — verified live). That is harmless by design: the pipeline fetches it,
`_thread_reply_target` sees a same-domain sender, logs `thread_owned_by_us`, and
skips before any model call — the same cheap skip colleague mail takes today. No
model spend, a few metadata reads.

## 5. Assignment, deadline, and the sweep

**Rotation.** `assignee(message_id, attempt) = reviewers[(hash(message_id) + attempt) % n]`.
Deterministic, stateless, and evenly spread. `attempt` lives in the ledger.

**Deadline.** A draft is *stale* when it has sat untouched for
`PAYBOT_REASSIGN_AFTER_HOURS` (default 4) **business hours** — counted 08:00–18:00
Mon–Fri in the stack's `Timezone` parameter, which is already a correctness input for
the tense gate. Business hours, not wall hours: a 17:00 Friday draft must not burn
through all three reviewers over a weekend nobody works.

**The sweep**, once per run, over live ledger entries past deadline:

| `drafts.get` says            | Meaning              | Action                                                        |
|------------------------------|----------------------|---------------------------------------------------------------|
| 404, sent message in thread  | Reviewer sent it     | `status: sent`. Log `draft_send_confirmed`.                   |
| 404, no sent message         | Reviewer deleted it  | Reassign: `attempt += 1`, next reviewer. Log `draft_reassigned` (`reason: deleted`). |
| Exists, body hash unchanged  | Untouched — stale    | `drafts.delete`, recreate with next reviewer, update ledger. Log `draft_reassigned` (`reason: stale`). |
| Exists, body hash changed    | Being worked on      | Leave it. Extend the deadline once; past a hard ceiling (3× the deadline) log `draft_in_progress_stale` and stop extending — that line is a task, not telemetry. |

**Ordering: delete first, create second.** A crash between the two leaves *no* draft
and a ledger pointing at the deleted one — which the next sweep reads as "deleted →
reassign" and heals unaided. The opposite order can leave two live drafts, which is
the failure this design exists to prevent, and cleaning orphans would need more state.
The ledger is saved in a `finally`, mirroring the handler's block-ledger contract, so
work done before a timeout still counts.

**Exhaustion.** After one full cycle (`attempt == n`, configurable via
`PAYBOT_REASSIGN_MAX_ATTEMPTS`), stop: `status: exhausted`, log
`draft_assignment_exhausted`. Same semantics as `gate_block_retries_exhausted`: the
mail sits unread with no draft anywhere and **nothing will retry it — a human must
act**. With three reviewers and the 4-business-hour default, full exhaustion takes
~1.5 working days, which is the right moment for a human anyway. A deletion by each
of the three reviewers also exhausts — three people declining to send a reply *is*
an answer, and the bot must not overrule it by cycling forever.

## 6. Edge register

The cases above plus everything else considered, in one place:

* **Carrier follows up while a draft is pending.** The follow-up is a new unread
  message in the same thread. The by-thread ledger lookup (§4.1) catches it: a live
  entry for the thread → skip, exactly as the draft-in-thread check does today. The
  assigned draft already answers the thread's newest state or the reviewer edits it.
* **Two reviewers answer the same thread by hand.** Unchanged from today —
  `_thread_reply_target` yields to any human reply before drafting, and after any
  send the thread is owned. The window where both a ledger draft and a spontaneous
  human reply exist closes at the next sweep (`sent`-detection or thread ownership).
* **Reviewer edits, then goes on holiday.** The edited draft is protected from
  deletion (edited ≠ stale), so it *can* strand — that is the one deliberate hole in
  the no-stranding guarantee, because silently discarding a colleague's words is
  worse. The `draft_in_progress_stale` line names the reviewer and the thread; the
  remaining two reviewers see the unanswered thread in the group mailbox regardless.
* **Accidental deletion.** Indistinguishable from a deliberate decline, and that is
  fine: the draft moves to the next reviewer, not into the void.
* **Reviewer offboarded.** `drafts.get` under a suspended/deleted user fails →
  treated as reassign; their hash slot redistributes when the roster parameter is
  updated at the next deploy. Roster updates are a stack parameter change, so the
  deploy checklist gains an offboarding line (§9).
* **Personal Gmail signatures.** Gmail does not inject a signature into an existing
  draft opened from Drafts, so replies keep the configured team sign-off
  (`ReplySignature`). A reviewer adding their name by hand above it is fine and
  human.
* **Send-as aliases.** The draft's From is the reviewer's primary address; Gmail's
  From dropdown in an opened draft defaults to what the draft carries.
* **Group delivery lag.** The group fans out in seconds; the poll is 20 minutes
  behind real time, so the reviewer's copy exists long before we look for it. If it
  ever doesn't, §4.2's fallback covers it.
* **Ledger corrupted or deleted.** Degrades to empty (§4.1): worst case is one
  duplicate draft per pending thread, self-announcing inside the carrier thread.
* **Parallel running with the workstation task.** The workstation runner drafts into
  the *reading* mailbox and knows nothing of the ledger. If both run with reviewers
  enabled on the Lambda: workstation-first is safe (its draft sits in the reading
  thread, the Lambda's draft-exists check — which stays as a first guard — skips);
  Lambda-first duplicates (the workstation can't see the ledger). **Therefore the
  reviewer roster stays empty until the workstation task is retired** — enforced by
  rollout order (§10), documented here because it is invisible in the code.
* **All three on holiday at once.** Exhaustion fires (§5) and the mail waits unread —
  which is also exactly what happens today with zero reviewers present, minus the
  log line saying so.
* **Runtime pressure.** Assignment adds ~3 Gmail calls per drafted email; the sweep
  is bounded by live ledger entries (realistically < 15). No interaction with the
  15-minute timeout or `FetchLimit=5` arithmetic.

## 7. Trust boundary: the bot in personal mailboxes

Domain-wide delegation was never per-mailbox — the credential could *always* read any
user in the domain. What changes is that the bot's **code** now touches the three
reviewers' personal mailboxes, and that deserves discipline, not a shrug:

* All reviewer-mailbox access goes through one wrapper (a `ReviewerMailbox` class)
  exposing exactly four operations: resolve-by-`rfc822msgid`, `drafts.create`,
  `drafts.get` by ledger-tracked id, `drafts.delete` by ledger-tracked id — plus the
  one thread read for sent-confirmation. No free-form queries, no body reads of
  anything the bot did not itself write, no label or settings access.
* Every reviewer-mailbox call is logged with mailbox, operation, and the ledger key
  it serves, so the audit trail answers "what did the bot do in my inbox" completely.
* The reviewers should be told, in writing, that the bot will place and may remove
  its own drafts in their mailbox, and touches nothing else. Consent here is a
  courtesy with teeth: they are the ones sending under their names.

## 8. Cost: why this is zero new AWS spend

Additions, exhaustively:

| Item | Delta |
|------|-------|
| Compute | The sweep runs **inside the existing invocation** — no new Lambda, no new EventBridge rule. A few extra seconds per run at 1024 MB is single-digit thousands of GB-seconds/month, inside the existing bill's noise (and the perpetual free tier's 400k GB-s). |
| State | One JSON object in the **existing** config bucket under the **existing** `state/*` read/write grant — the block ledger's bucket, lifecycle, and IAM story. ~72 extra GET+PUT pairs/day ≈ $0.03/month. No DynamoDB, no new bucket, no template IAM change. |
| Observability | The five new events (`draft_assigned`, `draft_reassigned`, `draft_send_confirmed`, `draft_in_progress_stale`, `draft_assignment_exhausted`) are **JSON log lines only** — queryable in Logs Insights on demand. Deliberately **no new metric filters or alarms**: the stack already publishes seven custom metrics against CloudWatch's ten always-free, so each new filter-fed metric risks $0.30/month and each alarm $0.10/month. If exhaustion later proves frequent enough to page on, add exactly one filter then, knowingly. |
| Model | Zero. Assignment and the sweep make no Bedrock calls; the Cc'd-copy skip (§4.4) stops before the model, like all thread-owned skips. |
| Secrets / IAM / logs | Unchanged. Same secret, same scopes, same role. Log volume grows by a handful of INFO lines per run against 90-day retention already priced in. |

And one **reduction**, outside AWS: the `paybot@` Workspace licence (~$84/year) is
cancelled before it started.

## 9. Configuration and template changes

New settings (all inert when the roster is empty — the feature is entirely off and
today's single-mailbox behaviour is untouched):

| Setting | Template parameter | Default | Meaning |
|---|---|---|---|
| `PAYBOT_REVIEWERS` | `Reviewers` | `[]` | JSON list of reviewer addresses, rotation order. Same JSON-list footgun as `ReplyCc`: a bare address kills startup with `SettingsError`. |
| `PAYBOT_REASSIGN_AFTER_HOURS` | `ReassignAfterHours` | `4` | Business hours (08:00–18:00 Mon–Fri, stack `Timezone`) before an untouched draft moves on. `0` = never reassign. |
| `PAYBOT_REASSIGN_MAX_ATTEMPTS` | `ReassignMaxAttempts` | `0` = one full roster cycle | Attempts before `exhausted`. |

Changed values, no new code: `ReplyCc` becomes `["paystatus@circledelivers.com"]`.

Code deltas, for scoping: `Reply-To` support in `build_reply`; the assignment ledger
module (sibling of `block_ledger.py`, same JSON-in/JSON-out contract so tests inject
strings); the `ReviewerMailbox` wrapper; assignment + sweep wiring in the worker path;
ledger-aware skip beside the existing draft-in-thread check.
[DEPLOY_CHECKLIST.md](DEPLOY_CHECKLIST.md) gains: roster parameter review on reviewer
join/leave, and the §10 ordering constraint.

## 10. Rollout and rollback

1. **Land dark.** Code deployed with `Reviewers=[]`; behaviour is bit-for-bit today's.
2. **Roster of one — the operator.** Drafts route to your own mailbox through the new
   path. Validates copy-resolution, threading, ledger round-trip, and sent-detection
   on real mail, with a blast radius of one inbox you own.
3. **Retire the workstation task.** Precondition for any multi-reviewer roster (§6,
   parallel running). Its Task Scheduler entry is disabled, not deleted, so it remains
   the rollback path.
4. **Roster of two, then three.** Watch `draft_reassigned` for a week — its rate is
   the design's report card: near-zero means assignments land with present people;
   high means the deadline or the rotation needs tuning, not that anything is broken.
5. **Rollback** at any point: set `Reviewers=[]` (one stack parameter). Live ledger
   entries are then ignored; already-created reviewer drafts remain where they are —
   human-visible, threaded, harmless — and drafts resume landing in the reading
   mailbox. Nothing to migrate, nothing to clean.

## 11. Out of scope, recorded so they stay decisions

* **Vacation-responder awareness** (skip an OOO reviewer at assignment instead of
  discovering it four business hours later) needs the `gmail.settings.basic` scope —
  a real Google-side grant change. The timeout already covers the failure mode;
  revisit only if reassignment latency is observed to matter.
* **Stage 2 (Slack approve / selective auto-send).** Auto-send under a *reviewer's*
  name is a materially different proposition from auto-send under a bot identity —
  it puts words in a specific person's mouth without their click. If Stage 2
  arrives, auto-send must either return to a non-personal sender or gain per-reviewer
  consent. Flagged now so the future design doesn't inherit this one silently.
* **Read receipts / who-sent-what reporting.** The ledger holds it; a report is a
  Logs Insights query away; building UI for it is not this.
