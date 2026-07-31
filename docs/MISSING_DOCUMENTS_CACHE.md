# Missing-documents cache — plan and implementation steps

**Status:** design, not built. No code in this repo implements any of this yet.

## 1. The problem

Transport Pro exposes `GET /load/missing_documents`, and it is the only place that carries
**Transport Pro's own opinion of which documents a load requires**:

```jsonc
{
  "pagination": { "totalRecords": 7326, "perPage": 200, "currentPage": 0, "totalPages": 37 },
  "results": [
    {
      "id": 227294,
      "assignedTerminal": 1029,
      "internalContacts": [{ "type": "ORDERTAKER", "id": 1259 }],
      "status": {
        "loadStatus": "Planned",
        "documentStatus": "Waiting for Documents",
        "billingStatus": "Billing Open"
      },
      "billingInfo": { "customerId": 3932, "customer": { "id": 3932, "companyName": "MERCEDES-BENZ USA" } },
      "dispatchRecords": [
        { "id": 267437, "brokerCarrierId": 12294, "dispatcherEmail": null, "dispatcherPhone": "1" }
      ],
      "missingDocuments": ["Bill Of Lading"]
    }
  ]
}
```

It cannot be filtered by load. In the production tenant that means **7,326 records across 37
pages** — 37 sequential HTTP calls to answer a question about one load, on every inbound email.

Mirroring it into MongoDB on a 25–30 minute cycle turns that into one indexed lookup, and moves
the 37 calls off the email path onto a schedule.

Every record on page 0 carries `documentStatus: "Waiting for Documents"` and
`billingStatus: "Billing Open"`, so treat this endpoint as *"loads blocked in Waiting for
Documents"* rather than a general document index.

---

## 2. The design decision that matters

The obvious framing — "cache the endpoint so lookups are fast" — leads to a subtly dangerous
system. The cached fact is a **negative claim about someone's paperwork**. Serve it stale and
the bot emails a carrier to say their BOL is missing twenty minutes after they uploaded it.
That is a customer-facing error, and "the cache was 28 minutes old" is not a defence.

So do not treat the mirror as the answer. Treat it as **the required-document policy**, and
confirm against live data before disclosing anything:

| Source | What it is good for | Freshness |
|---|---|---|
| Mongo mirror of `/load/missing_documents` | Which documents TP *requires* for this load | 25–30 min |
| `GET /files/search?recordType=loads&recordId={n}` | What is *actually on file* right now | live, per-load, already implemented |

The reconciliation rule:

> A document is reported missing only when the mirror says it is required **and** the live
> `files/search` read confirms it is absent. If the mirror says "Bill of Lading missing" but a
> BOL now appears on file, the live read wins and the cache entry is treated as stale.

This is worth the extra step. It means a stale cache can only ever cause an *under*-report
(staying quiet about a genuinely missing document, which escalates to a human) and never an
over-report (telling a carrier they are missing paperwork they have already sent). Given the
pre-send gate's fail-closed posture, that is the right direction for the error to fall.

It also means the refresh interval stops being safety-critical and becomes a
freshness-of-coverage question. 25–30 minutes is fine.

---

## 3. What the production payload settled, and what it did not

### 3.1 RESOLVED — `results[].id` is the load number you can look up

Page 0 ids run from `227294` to `2404150`, one continuous space. `2436795` — the load whose
`files/search?recordId=2436795` call worked — sits in that same range. So `id` is the load
record id, it is the key `files/search` accepts, and it is the number that appears in carrier
email.

**Consequence for the plan:** no id translation is needed. Store `id` as `_id` directly. The
two-id-space problem seen in the vendor demo tenant does not apply here.

### 3.2 STILL OPEN — how does pagination work?

Now urgent rather than incidental: **37 pages, not 2.** The collection documents no page
parameter. Try, in order:

```
GET /load/missing_documents?page=1
GET /load/missing_documents?currentPage=1
GET /load/missing_documents?offset=200
GET /load/missing_documents?perPage=1000
```

Check whether `results[0].id` differs from page 0's `227294`. Record which works.

**If none work,** you cannot build a complete snapshot and must not ship a partial one (§6).
Note that page 0 alone covers ids up to ~2.40M while current loads are ~2.43M+, so page 0 is
*systematically the oldest* loads — the least useful 200 of 7,326. A page-0-only cache would be
worse than no cache, because it would look populated while missing everything recent.

### 3.3 NEW — two required-document names are outside the known vocabulary

`missingDocuments` uses TP's *required-document* names, which are **not** the `fileTypeName`
vocabulary that `files/search` returns. Observed on page 0:

| `missingDocuments` value | Maps to which `DocCategory` |
|---|---|
| `Bill Of Lading` | `PROOF_OF_DELIVERY` |
| `Carrier Invoice` | `CARRIER_INVOICE` |
| `Rate Confirmation` | `RATE_AGREEMENT` — note `files/search` calls this `Carrier Rate Agreement` (id 23) |
| `TONU Approval` | **unmapped** |
| `Reefer Log` | **unmapped** |

Two things follow.

First, the mapping is **not** identity — "Rate Confirmation" (required) and "Carrier Rate
Agreement" (on file) are the same document under two names. Step 4 in §7 has to translate
deliberately, not string-match.

Second, `TONU Approval` and `Reefer Log` are requirements the derived checklist in
`domain/documents.py` knows nothing about, and they are load-type-specific — TONU ("truck
ordered not used") loads need an approval, reefer loads need a temperature log. This is exactly
the value the mirror adds over a fixed checklist: **TP knows the requirement varies by load and
the hard-coded list cannot.** Both should classify to new categories rather than falling into
`OTHER`, or the reconciliation in §2 will silently drop them.

Run a full 37-page pull and collect the distinct `missingDocuments` values before finalising the
mapping. Page 0 is the oldest 200 records and is unlikely to contain every variant.

### 3.4 NEW — this contradicts the 6/7-digit routing rule

Page 0 contains **6-digit load ids in Transport Pro**: `227294`, `421495`, `422049`, `861129`.

PRD §4.1 routes 6 digits to QuickBooks and 7 digits to Transport Pro, and
`domain/routing.py` implements exactly that. But these are Transport Pro loads, returned by a
Transport Pro endpoint, with 6-digit ids.

They are the oldest records in the set, so the rule may have been true when it was written and
drifted since. Either way, **load-id length is not a reliable router**, and this needs settling
independently of the cache work — see the note at the end of this document.

---

## 4. Data model

One collection, `missing_documents`, one document per load. 7,326 records at ~400 bytes is a
few megabytes — not a performance problem, so optimise for correctness and observability.

```jsonc
{
  "_id": "227294",                     // results[].id as a string — confirmed lookup key (§3.1)
  "missing_documents": ["Bill Of Lading"],   // TP's names, verbatim
  "missing_categories": ["proof_of_delivery"], // mapped via §3.3, for reconciliation
  "load_status": "Planned",            // status.loadStatus
  "document_status": "Waiting for Documents",
  "billing_status": "Billing Open",
  "customer_name": "MERCEDES-BENZ USA",// billingInfo.customer.companyName
  "assigned_terminal": 1029,
  "synced_at": "2026-07-30T09:15:04Z", // when this snapshot was taken
  "sync_id": "2026-07-30T09:15:04Z"    // groups every doc from one successful run
}
```

Store both the raw names and the mapped categories. The raw list is what you show a human when
diagnosing; the mapped list is what the reconciliation in §2 compares against. Keeping only the
mapping loses the evidence when an unrecognised name appears.

**Do not** carry `dispatchRecords` across. The dispatcher fields are unreliable — page 0 alone
has `"313346600"`, `"."`, `"x"`, `"Pp"`, `"-"` and `"919-857-6313"` sitting in
`dispatcherEmail`, plus internal `@circledelivers.com` addresses for inter-company loads. Using
them to build an authorisation allow-list would be a mistake; the existing
`get_authorization_context` already filters on `"@"`, and that filter should stay.

Indexes:

```javascript
db.missing_documents.createIndex({ "_id": 1 })                  // implicit, unique
db.missing_documents.createIndex({ "sync_id": 1 })              // for the sweep in §5
db.missing_documents.createIndex({ "synced_at": -1 })           // for the staleness guard
```

A second tiny collection, `sync_runs`, for observability — one document per attempt, with
`started_at`, `finished_at`, `status`, `pages_fetched`, `records`, `error`. Without it, a sync
that has been quietly failing for three days looks identical to a sync with nothing to report.

**No TTL index.** A TTL would silently empty the collection when the sync breaks, and an empty
collection reads as "nothing is missing" — the exact wrong default. Keep the data and let the
reader reject it on age (§6).

---

## 5. The sync job

### 5.1 The one thing that will bite you

`/load/missing_documents` returns **only loads that have missing documents**. A load that is
absent from the response has complete paperwork.

That means an upsert-only sync is wrong, and wrong in the worst direction: once a load appears,
it stays in your cache forever. A carrier uploads the BOL, TP drops the load from the endpoint,
and your mirror keeps insisting the document is missing indefinitely.

The sync must be a **full-snapshot replace**, not an incremental update.

### 5.2 Snapshot semantics

Stamp every document with the run's `sync_id`, then delete anything not stamped — but only
after the whole fetch succeeded:

```python
# Illustrative only — not code in this repo.
sync_id = utcnow().isoformat(timespec="seconds") + "Z"

pages = fetch_all_pages()          # raises if ANY page fails
records = [normalise(r) for page in pages for r in page["results"]]

# Cross-check before touching the collection: the API told us how many to expect.
if len(records) != pages[0]["pagination"]["totalRecords"]:
    raise IncompleteSnapshot(expected=..., got=len(records))

collection.bulk_write(
    [
        UpdateOne({"_id": r["_id"]}, {"$set": {**r, "sync_id": sync_id}}, upsert=True)
        for r in records
    ],
    ordered=False,
)

# Only now is it safe to drop loads that are no longer missing anything.
collection.delete_many({"sync_id": {"$ne": sync_id}})
```

Three properties this gives you:

- **A partial fetch never deletes.** If page 19 of 37 fails, the exception fires before the
  sweep and the previous snapshot survives intact — stale, but complete and age-stamped.
- **The `totalRecords` cross-check catches silent pagination bugs.** Over 37 pages this is not
  hypothetical: getting 200 of 7,326 records and sweeping would delete 7,126 loads' worth of
  real data and leave a cache that confidently reports "nothing missing" for almost everything.
- **The sweep is what makes "fixed" loads disappear.** This is the line that prevents the
  forever-missing bug in §5.1.

### 5.2.1 Consequences of 37 pages

- **Budget the calls.** 37 sequential requests per cycle, every 25 minutes, is ~89 requests an
  hour. Confirm Transport Pro's rate limit before scheduling; add a small delay between pages if
  it is tight, and let the job take a minute rather than burst.
- **Expect page drift.** Results appear sorted by `id` ascending, but a 37-page walk is not a
  snapshot — loads entering or leaving mid-fetch shift page boundaries, so a load can be seen
  twice or missed. Upsert makes duplicates harmless. A miss is a one-cycle gap that the next run
  repairs, which is acceptable *only* because §2 confirms against the live per-load read before
  anything reaches a carrier.
- **Do not parallelise the pages** unless you have confirmed the rate limit allows it. Sequential
  is slow and correct; a 429 mid-walk aborts the whole snapshot.
- **Tolerate a `totalRecords` drift of a few records** between page 0 and the final page, for the
  same reason. An exact-equality check will fail spuriously on a busy tenant — compare with a
  small tolerance, and treat a large gap as `IncompleteSnapshot`.

### 5.3 Do not run two syncs concurrently

Two overlapping runs with different `sync_id`s will each sweep the other's writes. If the
scheduler can double-fire (Lambda retries, an overlapping cron), take a lock — a single-document
`_id: "sync_lock"` upsert with a timestamp, or an EventBridge schedule with no retry and a
concurrency limit of 1.

---

## 6. Reading it back, safely

Two guards on the read path, both fail-safe:

**Staleness.** Reject the cache if `synced_at` is older than a threshold — suggest 90 minutes,
i.e. three missed cycles. A rejected cache means "I do not know what TP requires", which falls
back to the derived checklist in `domain/documents.py`. It must never mean "nothing is missing".

**Absence.** A load not present in a *fresh* collection means complete paperwork. A load not
present in a *stale* collection means nothing at all. The code must distinguish these; a plain
`find_one() is None` cannot.

**Mongo unavailable.** Degrade to the derived checklist and log it. The enrichment is a
nice-to-have; the pipeline must not fail because a cache is down. Never let a cache outage turn
into either a wrong disclosure or a dead inbox.

---

## 7. Where it plugs into this codebase

The seam already exists. `TpGetFileHistory` calls `assess_documents()`, which takes a `required`
tuple:

```python
# src/payment_bot/domain/documents.py
REQUIRED_FOR_PAYMENT: tuple[DocCategory, ...] = (
    DocCategory.CARRIER_INVOICE,
    DocCategory.PROOF_OF_DELIVERY,
    DocCategory.RATE_AGREEMENT,
)
```

Today that tuple is a hard-coded assumption. The mirror replaces it with TP's per-load answer.

Suggested shape, in dependency order:

| Step | File | What |
|---|---|---|
| 1 | `clients/missing_documents.py` *(new)* | `MissingDocumentsSource` protocol: `get(load_id) -> MissingDocsRecord \| None`, plus `MongoMissingDocuments` and `NullMissingDocuments` |
| 2 | `clients/transport_pro_http.py` | Add `get_missing_documents_snapshot()` — the paginated fetch. Reuses the existing `HttpTransport` and `_results()` |
| 3 | `sync/missing_documents.py` *(new)* | The job in §5. Takes a TP client and a Mongo collection; no other dependencies |
| 4 | `domain/documents.py` | `assess_documents()` gains an optional `tp_required: tuple[str, ...]`, mapped through `classify()` into categories. Pure — no Mongo import in `domain/` |
| 5 | `tools/transport_pro.py` | `TpGetFileHistory` consults the source, reconciles per §2, and reports `required_source: "transport_pro" \| "default_checklist"` in its output |
| 6 | `config.py` | `mongo_uri` (`SecretStr`), `mongo_db`, `missing_docs_max_age_minutes`, `missing_docs_enabled` |
| 7 | `pyproject.toml` | `mongo = ["pymongo>=4.6"]` as an **optional extra**, imported lazily — same pattern as `boto3` and `google-auth` |

Keep `domain/` free of I/O. The Mongo client belongs in `clients/`, the job in `sync/`, and the
pure required-vs-present logic stays where it is and stays unit-testable.

Do **not** add a second agent-facing tool for this. `tp_get_file_history` should keep being the
one place the model asks about paperwork; giving a weak open-weight model a choice between two
similar tools is a reliability regression for no gain.

---

## 8. Scheduling

**Locally** — Windows Task Scheduler or a simple loop, every 25 minutes:

```
payment-bot-sync-missing-docs
```

**On AWS** — the pattern already documented in [AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md):

```
EventBridge Scheduler  rate(25 minutes)
        │
        ▼
  Sync Lambda  ──▶ Transport Pro /load/missing_documents (all pages)
        │
        ▼
  DocumentDB / MongoDB Atlas
```

Notes:

- The sync Lambda needs Transport Pro credentials and the Mongo URI — nothing else. No Bedrock,
  no Gmail, no Slack. Give it its own execution role.
- MongoDB Atlas free tier is ample for 243 documents. If you would rather not add a database at
  all, **DynamoDB is a better fit** for this shape and you already have it in the deployment
  plan — one table, `load_id` as the partition key, same snapshot semantics. Mongo only earns
  its place if you want the flexible querying for other purposes.
- Set concurrency to 1 and retries to 0, then alarm on failure (§5.3).

---

## 9. Alarms worth having

A cache that stops refreshing is invisible until it is embarrassing. Alarm on:

- **No successful sync in 90 minutes** — the same threshold the reader uses.
- **`records` below ~5,000 or above ~10,000** — the observed baseline is 7,326. A count near 200
  means only page 0 was fetched; a collapse to a few hundred means pagination broke.
- **`pages_fetched != 37±2`** — the cheapest possible detector for a pagination regression.
- **Any `IncompleteSnapshot`** — should be zero; if it is not, §3.2 is unresolved.
- **An unrecognised `missingDocuments` value** — a new required-document type appeared and needs
  mapping (§3.3). Log the value; do not silently bucket it as `OTHER`.
- **Reads falling back to the default checklist** — if this is most of them, the cache is not
  actually working.

---

## 10. Testing plan

Follow the existing pattern — an injectable transport for HTTP, and for Mongo either
`mongomock` or a thin fake implementing the protocol from step 1.

| Test | Asserts |
|---|---|
| Multi-page fetch | All 37 pages requested; records concatenated; `totalRecords` matched |
| A middle page fails | Raises, **and the previous snapshot is untouched** |
| Record count mismatch | Raises `IncompleteSnapshot`; no sweep runs |
| Only page 0 returns | Raises — 200 of 7,326 must never be swept in |
| Duplicate id across pages | Upsert dedupes; no crash |
| `TONU Approval` / `Reefer Log` | Mapped to a real category, never silently `OTHER` |
| A load drops out of the API | The sweep removes it — the §5.1 bug, pinned down |
| Stale snapshot | Reader rejects it and falls back to the default checklist |
| Empty fresh snapshot | Reads as "nothing missing", not as an error |
| Mongo unavailable | Falls back to the checklist; the pipeline still completes |
| Reconciliation | Mirror says BOL missing + live `files/search` shows a BOL ⇒ **not** reported missing |
| Load 2436795 | Mirror says BOL missing + live read confirms absent ⇒ reported missing |

That second-to-last row is the one that protects a carrier from a wrong email. Write it first.

---

## 11. Phasing

| Phase | Work | Done when |
|---|---|---|
| **0** | Resolve §3.2 (page param) in Postman; pull all 37 pages once and collect the distinct `missingDocuments` values for §3.3 | You can fetch page 37 and you know every required-document name |
| **1** | Steps 1–3: protocol, paginated fetch, sync job + tests | The collection holds ~7,326 docs and the sweep provably removes fixed loads |
| **2** | Steps 4–6: the §3.3 mapping and reconciliation into `assess_documents` and the tool | `tp_get_file_history` reports `required_source: "transport_pro"` |
| **3** | Scheduling and alarms (§8, §9) | A missed sync pages someone before a carrier notices |

Phase 0 is no longer a formality — it is a 37-page pull whose output determines the mapping
table in step 4. Do it first and keep the raw JSON.

Phase 1 is independently useful — 7,326 loads blocked in *Waiting for Documents*, queryable by
customer and terminal, answers "where is our paperwork backlog?" long before the bot consumes
it. `Niagara Bottling` and `Fiat - TONU` dominate page 0 alone.

---

## 13. Out of scope here, but do not lose it

**Load-id length does not identify the system.** §3.4 found 6-digit ids (`227294`, `421495`,
`422049`, `861129`) in Transport Pro's own load report, which contradicts `domain/routing.py`
and PRD §4.1.

This matters beyond the cache. A carrier email quoting a 6-digit load is currently escalated as
"non-Transport-Pro" and never answered — and at least some of those loads are in Transport Pro.
Confirm with one call:

```
GET /voiceai/load/283660/payment_information
```

If that returns a load, the router is wrong and needs a real signal — try Transport Pro first
and fall back to QuickBooks on a miss, rather than guessing from digit count. Track it as its own
piece of work.

---

## 12. Anti-patterns

- **Upsert without a sweep.** The forever-missing bug. The single most likely thing to get wrong.
- **A TTL index on the collection.** Turns a broken sync into "nothing is missing".
- **Sweeping after a partial fetch.** Deletes real data on a transient HTTP error.
- **Treating cache-miss as "complete".** Only valid when the snapshot is fresh.
- **Reporting missing documents from the cache alone.** Read §2 again; always confirm against
  the live per-load read before telling a carrier anything.
- **Making the pipeline depend on Mongo being up.** It is an enrichment, not a dependency.
