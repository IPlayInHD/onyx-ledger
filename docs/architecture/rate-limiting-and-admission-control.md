# Rate Limiting and Admission Control (Entry 10)

Every prior entry made the system *correct*. This one makes it *survivable*: it
bounds how much expensive work one caller, or the platform, can have in flight —
decided before the expensive work starts.

The invariant:

> No single principal can monopolize Onyx compute, worker capacity, document
> ingestion, database connections, or expensive calculation endpoints. Admission
> decisions are deterministic, bounded, observable, and safe under concurrency.

---

## 1. Threat model

Not an attacker-first list. These are the four ways this system actually falls
over, ranked by how likely they are:

| # | Scenario | Why it happens | What it costs |
|---|---|---|---|
| 1 | **Client retry storm** | A request takes 40s, a proxy times out at 30s, the client retries. Then again. | Each retry starts a *new* full optimization. The first one is still running. Nothing is malicious and the system is now doing 5× the work. **This is the most likely failure and it needs no attacker.** |
| 2 | **Impatient user** | A button that takes 30s gets clicked six times. | Six concurrent analyses over the same tax year, all writing the same rows. |
| 3 | **Queue flooding** | A script submits 500 scenarios. | The `ioe` queue backs up; everyone's work is behind it. |
| 4 | **Aggregate overload** | Many principals, each individually reasonable. | Nobody exceeded a limit and the platform is still saturated. |

Scenario 4 is why a per-principal limit alone is insufficient, and scenario 1 is
why idempotency matters more than rate limiting for the expensive paths.

**Out of scope, deliberately:** network-layer DDoS (a CDN/WAF concern, not an
application one), and TLS/connection exhaustion below the ASGI layer.

---

## 2. Operation inventory

Every externally reachable or internally queued operation, measured against the
code as it exists.

| Endpoint / service | Class | Sync/queued | CPU | DB | Worker | Payload-sensitive | Idempotency | Prior guard | Queue |
|---|---|---|---|---|---|---|---|---|---|
| `POST /analysis` | `ANALYSIS_RUN` | sync | **high** — full engine + rules + snapshot | ~20 writes | — | low | none | **none** | — |
| `workers.tasks.ioe.run_optimization` → `OptimizationOrchestrator.generate` | `OPTIMIZATION_RUN` | queued | **very high** — up to 200 engine runs | 3 transactions, bulk child writes | yes | low | spec-hash + `Idempotency-Key` | idempotency only | `ioe` |
| `POST /ioe/scenarios` | `SCENARIO_RUN` | sync | **high** — engine runs per lever | ~10 writes | — | bounded (25/25) | spec-hash + `Idempotency-Key` | idempotency + complexity bounds | — |
| `POST /ioe/scenarios/{id}/refresh` | `SCENARIO_RUN` | sync | high | ~10 writes | — | — | none | none | — |
| `POST /ioe/{type}/{id}/integrity/verify` | `INTEGRITY_VERIFY` | sync | **high** — full replay | moderate | — | — | 409 per entity | per-entity claim | — |
| `POST /documents` | `DOCUMENT_UPLOAD` | sync | low | 2 writes | — | **yes** | none | **none** | — |
| `POST /documents/{id}/process` | `DOCUMENT_PROCESS` | sync (prod: queued) | **high** — OCR/extraction | moderate | yes | **yes** | none | **none** | `documents` |
| `POST /documents/{id}/confirm` | `NORMAL_WRITE` | sync | low | few writes | — | low | none | none | — |
| `POST /tkms/imports`, `/imports/{id}/reparse` | `IMPORT_RUN` | sync→queued | **high** — whole-document parse | many writes | yes | **yes** | checksum dedupe | checksum only | `tkms_*` |
| `POST /tkms/rules/{id}/publish`, `/versions/{id}/*` | `ADMIN_RULE_PUBLISH` | sync | low | moderate | — | low | four-eyes | four-eyes governance | — |
| `POST /ai/ask`, `/conversations/*/messages` | `AI_EXPLAIN` | sync | moderate + **external call** | moderate | — | yes | none | **none** | — |
| `POST /auth/login`, `/register`, `/refresh` | `AUTH_ATTEMPT` | sync | Argon2id (deliberately slow) | few | — | low | — | **none** | — |
| `GET` list/detail endpoints | `CHEAP_READ` | sync | low | 1–3 indexed | — | — | — | pagination caps | — |
| `POST /financials/*`, `PUT /users/me/tax-profile` | `NORMAL_WRITE` | sync | low | 1–2 | — | low | — | none | — |
| Freshness relay, integrity scheduler, sweeps | *internal* | queued | bounded by design | bounded | yes | — | outbox dedupe | Entries 8/9 | `ioe_freshness`, `ioe_integrity` |

**Pre-entry state, stated plainly:** two rate-limit settings existed
(`rate_limit_auth_per_minute`, `rate_limit_analysis_per_minute`) and **nothing
read them**. There was no rate limiting, no concurrency limiting, and no global
capacity control anywhere in the system. What did exist, and is preserved: IOE
spec-hash idempotency, the integrity per-entity claim, scenario complexity
bounds (25 levers / 25 assumptions, enforced twice), TKMS checksum dedupe, and
already-separated Celery queues.

---

## 3. Operation classes

A closed `OperationClass` enum (`app/services/admission/policy.py`). Endpoint
paths are **not** policy keys — they change with routing, multiply with
versioning, and two endpoints of wildly different cost can share a prefix. A
class names what the work *costs*.

`CHEAP_READ` · `NORMAL_WRITE` · `AUTH_ATTEMPT` · `ANALYSIS_RUN` ·
`OPTIMIZATION_RUN` · `SCENARIO_RUN` · `DOCUMENT_UPLOAD` · `DOCUMENT_PROCESS` ·
`IMPORT_RUN` · `INTEGRITY_VERIFY` · `ADMIN_RULE_PUBLISH` · `AI_EXPLAIN`

A class added to the enum without a policy raises at import. Every limit lives
in the registry; no handler carries a number.

### What "tenant" means here — and what it does not

**Onyx Ledger has no organization, household, or tenant entity.**
`identity.user_account` *is* the tenant, and row-level security is keyed on
`app.user_id`. The per-user and per-tenant dimensions the brief asks about are
therefore the **same dimension** in this product, and implementing a separate
"tenant tier" would mean inventing a product concept that does not exist.

So this entry provides **anti-monopoly caps per principal plus a global ceiling
— not weighted fair scheduling.** That distinction is real: a principal at its
cap cannot take more, but there is no scheduler ensuring a starved principal is
served *first* when capacity frees up. Admission is first-come-first-served
within the caps.

`ScopeType` is an enum (`USER` / `ADMIN` / `GLOBAL`) rather than a boolean
precisely so that adding an organization tier later is a new member and a policy
field, not a redesign.

---

## 4–8. The three controls, and the numbers

Three controls, deliberately separate because they fail differently:

| Control | Question | Mechanism |
|---|---|---|
| **Rate** | how often may this be asked for? | fixed-window counter, one conditional UPSERT |
| **Concurrency** | how much may be in flight? | live-lease count under an advisory lock |
| **Global** | how much may the *platform* be doing? | the same lease mechanism at `GLOBAL` scope |

A caller under its own rate limit can still be refused because the platform is
saturated; a caller with capacity to spare can still be refused for asking too
fast. One counter cannot express both.

| Class | req/min (+burst) | active/user | active/global | lease | store failure | Retry-After |
|---|---|---|---|---|---|---|
| `CHEAP_READ` | 300 (+60) | — | — | — | **open** | 10s |
| `NORMAL_WRITE` | 60 (+20) | — | — | — | **open** | 15s |
| `AUTH_ATTEMPT` | 10 (+5) | — | — | — | closed | 60s |
| `ANALYSIS_RUN` | 10 (+2) | **1** | 200 | 900s | closed | 30s |
| `OPTIMIZATION_RUN` | 6 (+2) | **2** | 50 | 900s | closed | 60s |
| `SCENARIO_RUN` | 20 (+10) | 3 | 100 | 300s | closed | 30s |
| `DOCUMENT_UPLOAD` | 30 (+10) | — | — | — | closed | 30s |
| `DOCUMENT_PROCESS` | 20 (+5) | 2 | 40 | 600s | closed | 60s |
| `IMPORT_RUN` | 10 (+2) | 2 | 10 | 1800s | closed | 120s |
| `INTEGRITY_VERIFY` | 10 (+5) | 2 | 20 | 300s | closed | 60s |
| `ADMIN_RULE_PUBLISH` | 30 (+10) | — | — | — | closed | 30s |
| `AI_EXPLAIN` | 20 (+5) | 2 | 50 | 180s | closed | 30s |

**How the numbers were chosen.** Each is set where a human working normally
never meets it and a script meets it immediately. `ANALYSIS_RUN` at one
concurrent per user is the sharpest: a second click has nothing to add while the
first is still writing the rows it would duplicate. `OPTIMIZATION_RUN` at two is
generous for the most expensive path in the system (up to 200 engine runs).
Every value is overridable through validated settings (§21).

**Fixed window, not sliding log.** A sliding log stores one row *per request*,
turning the anti-flooding mechanism into its own flooding vector. The cost of a
fixed window is that a caller can spend two windows' allowance across a
boundary; the `burst` allowance already assumes tolerance of exactly that shape.

---

## 9. Authoritative backing store: PostgreSQL

Redis is present as the Celery broker, and `INCR` is a tempting rate counter. It
is the wrong authority here:

- a **lease must outlive a process crash**, and the broker is explicitly
  ephemeral — a flush would silently delete every protection at once;
- the concurrency decision wants to be in the same transaction as the work it
  guards, so a rollback releases the slot with it;
- this database is already the authority for every other correctness property,
  and splitting authority across two stores is how you get two answers.

Cost: one advisory lock plus two small indexed statements, on operations that
already cost engine runs. Cheap reads take the rate path only — a single UPSERT,
measured at **p50 2.1ms / p95 2.6ms** (§23).

Schema `admission`, two tables, no `SECURITY DEFINER` function.

---

## 10. Race safety

The obvious implementation is wrong:

```
count = SELECT count(*) FROM lease WHERE ... AND live
if count < limit: INSERT lease
```

Under READ COMMITTED, ten concurrent requests all run the SELECT before any
INSERT commits, all see 0, and all ten are admitted against a limit of 2. The
window is small, which makes it *worse*: invisible in manual testing, open under
exactly the load the limit exists to survive.

Two mechanisms close it:

- **Rate** — a single statement whose `WHERE` is evaluated against the row it has
  already locked:
  `ON CONFLICT DO UPDATE SET count = count + 1 WHERE count < :allowance RETURNING count`.
  No row returned means the allowance was reached. Concurrent writers serialize
  on the row lock; the counter is deliberately *not* incremented past the
  allowance, so hammering a closed door cannot push the window further out.
- **Concurrency** — `pg_advisory_xact_lock` on a domain-separated SHA-256 of
  `(scope_type, scope_id, operation)`, so count-then-insert is serialized per
  scope. Transaction-scoped, so `COMMIT`/`ROLLBACK` releases it — including the
  rollback of a process that died holding it. The same primitive already used by
  `ioe.version_activation`. The key is a digest, not `hash()`, which is
  randomized per process and would put two API replicas in different namespaces.

Contention is per `(scope, operation)`: two different principals never wait on
each other.

**Measured (§23): 100 concurrent admissions against a limit of 2 → exactly 2
admitted, overshoot 0.**

---

## 11. Idempotency and retry storms

Threat #1 in the model, and the one that needs no attacker.

A `dedupe_key` on the lease identifies the *logical* work. A retry presenting the
same key finds the running operation instead of starting another. Checked
**before** the concurrency limits, so a retry storm resolves to the operation
already in flight rather than being told it hit a limit its own retries created.

**Keys are always owner-prefixed** — `owned_dedupe_key(user_id, ...)` puts the
authenticated user id at the front. A client-supplied string is never an
identity on its own, so a key cannot be used to reach into another account's
running operation. Asserted by
`test_one_users_dedupe_key_never_reaches_another_users_operation`.

| Operation | Dedupe identity |
|---|---|
| Analysis | `user:analysis:{tax_year}` |
| Optimization | `user:optimization:{analysis_id}` |
| Scenario refresh | `user:refresh:{scenario_id}` |
| Document process | `user:process:{document_id}` |

**Measured: 20 identical concurrent optimization requests → 1 expensive job, 19
resolving to it. 50 retries → 1 job, 0 duplicates.**

Existing IOE spec-hash idempotency is untouched and still authoritative for
*returning the result*; the dedupe key's narrower job is to stop a second
*lease* being consumed while that resolution happens.

---

## 12. Leases and recovery

A worker that is OOM-killed never runs its release path. If quota recovery
depended on that callback, **one crash would lock a user out of their own
account** until an operator intervened.

So `expires_at` is on the row and admission simply does not count expired
leases: recovery is a property of time, not of cleanup code having run.
`sweep_expired()` exists to mark them `EXPIRED` so the table reads honestly and
recoveries are countable — it is **not** load-bearing, and
`test_the_sweep_marks_lapsed_leases_without_being_load_bearing` asserts the
count is already zero before it runs.

Release reasons: `COMPLETED` · `FAILED` · `CANCELLED` · `EXPIRED` · `ABANDONED`.
The guard releases in a `finally`, so a *failing* operation returns its slot
immediately rather than waiting for expiry.

The lease is committed in its **own** transaction before the work starts. An
uncommitted INSERT is invisible to other connections, so a lease taken inside
the work's transaction would make the limit hold only within a single process.
The guard therefore never holds a transaction open across an engine run — which
also keeps a long optimization from pinning a connection out of the pool.

---

## 13. Queue backpressure and worker isolation

Queues were **already separated** before this entry (Entries 8/9) and are not
redesigned here:

| Queue | Tasks | Protected from |
|---|---|---|
| `ioe` | `run_optimization` | — (the expensive lane) |
| `ioe_freshness` | relay, sweeps, invalidation | optimization backlog |
| `ioe_integrity` | `verify_sealed_integrity` | both of the above |
| `analysis`, `documents` | user work | each other |
| `tkms_parse/extract/validate/compare/index` | one per stage | each other |
| `maintenance`, `notify` | housekeeping | everything |

**Heavy user optimization traffic cannot starve freshness or integrity work**:
they are different queues consumed by different workers.

Precisely half of that is guaranteed here. **Routing is code-enforced** — every
registered task matches an explicit route, asserted by
`tests/unit/test_admission_wiring.py`, so a new task cannot silently fall to the
default queue and start competing with everything else. **Capacity is not**: how
many workers subscribe to each queue is a `-Q` flag and a concurrency setting in
a deployment no file in this repository owns. One worker subscribed to all
queues makes the separation nominal. Both halves are stated because claiming
"expensive work is isolated" without the second one would be half true.

`task_acks_late = True` and `worker_prefetch_multiplier = 1` were already set:
a worker holds one message at a time, so a slow task cannot sit on a batch, and
a crashed worker's message returns to the queue.

**Queue-depth admission is NOT implemented, and nothing today needs it.**
Nothing in `app/` publishes to the broker: the only `.delay()` calls in the
repository are worker-to-worker stage chaining inside `workers/tasks/tkms.py`,
reached only after an operator-triggered import has already passed `IMPORT_RUN`
admission at the API. There is therefore no user-reachable enqueue path whose
depth could be refused, and the invariant "admission rejected ⇒ zero new
expensive task published" holds by construction rather than by each enqueue site
remembering to check first.

That premise is a fact about the code, so it is asserted rather than trusted:
`tests/unit/test_admission_wiring.py` fails if a module in the request path
starts publishing, which is the moment to put a guard in front of it.
`QUEUE_CAPACITY` is reserved on exactly that basis (§27).

---

## 14. Execution budgets

Without them one pathological input runs forever, holding a worker slot, a
connection, and a lease.

| Task | Soft | Hard |
|---|---|---|
| default | 300s | 360s |
| `run_optimization` | 600s | 660s |
| `verify_sealed_integrity` | 600s | 660s |
| `relay_freshness_outbox`, `sweep_scenario_freshness` | 120s | 150s |
| `tkms.parse`, `tkms.extract` | 900s | 960s |
| `tkms.reindex` | 1800s | 1860s |

The pair matters. **Soft** raises inside the task, so `async with` blocks unwind,
the transaction rolls back, and the lease is released through the ordinary path.
**Hard** SIGKILLs a task that ignored the soft limit — no in-process rollback
runs, which is why it sits well above the soft limit and why the *database's*
transaction abort, not application code, is what guarantees no partial write
survives.

---

## 15. Payload and complexity limits

Named in `app/services/admission/limits.py`, never as literals at call sites.

| Limit | Value | Enforced |
|---|---|---|
| `MAX_DOCUMENT_BYTES` | 25 MB | **the store**, against the bytes that arrive; the declared size is an extra early refusal |
| `MAX_DOCUMENTS_PER_REQUEST` | 1 | one upload describes one document |
| `MAX_FILENAME_LENGTH` | 255 | Pydantic `max_length` |
| `ALLOWED_DOCUMENT_MIME_TYPES` | 7 types | **allow-list** — a deny-list admits every format nobody thought of |
| `MAX_EXTRACTION_TEXT_BYTES` | 1 MB | handler validator, on the **encoded** length |
| `MAX_EXTRACTION_FIELDS` | 200 | handler |
| `MAX_IMPORT_BYTES` / `MAX_IMPORT_ROWS` | 50 MB / 10 000 | encoded payload at the handler; row count in `ImportService.extract` |
| `MAX_SCENARIO_LEVERS` / `_ASSUMPTIONS` | 25 / 25 | **pre-existing**, enforced at the schema *and* the domain parser |

**A request can be tiny in bytes and enormous in work** — a hundred scenario
levers is a few hundred bytes and a combinatorial assembly problem. Size bounds
and complexity bounds are different things and both are listed.

**Where the document bound actually lives.** The bytes never pass through this
process — they go straight to object storage via a presigned URL — so the
`byte_size` in the request body is a *claim*, and Phase 1 treated that claim as
the bound. It is not one: a caller who wants to store 30 MB under a 25 MB limit
simply declares 1 MB.

So the ceiling is part of the storage port. `presign_put` takes `max_bytes`
(required, with no default, so a call site that forgets it does not quietly
compile) and the store refuses the object that actually arrives; in S3 that is a
POST policy `content-length-range` condition, which S3 enforces itself. The
authorized ceiling is the *tighter* of the platform maximum and the
declaration, so under-declaring buys the caller nothing. The declared-size check
stays as a cheap early refusal for the honest client, and is described as that
rather than as enforcement.

`MAX_EXTRACTION_TEXT_BYTES` had the mirror-image problem: it was enforced by
Pydantic `max_length`, which counts *characters*, so a bound named in bytes
admitted four megabytes of UTF-8. It is now checked on the encoded length.

Two bounds are not one bound: `MAX_IMPORT_BYTES` caps the payload, and
`MAX_IMPORT_ROWS` caps what the payload *expands into* — enforced in
`ImportService.extract`, where the staged-row count is first known and before a
single row is written. A compact document can produce an enormous ruleset, and
it is the staged rows that fill the review queue.

---

## 16. HTTP semantics

| Condition | Status | Meaning |
|---|---|---|
| This caller exceeded a policy | **429** | you asked too much |
| Platform capacity exhausted | **503** | come back later; not your fault |
| Duplicate active operation (analysis, refresh) | **409** | one is already running |
| Payload/complexity bound | **422** | this request is malformed |

Returning 429 for a platform-wide condition would tell a blameless caller to
slow down and would hide a capacity incident from the 5xx rate.

Response is RFC-9457 `application/problem+json` plus:

```json
{ "operation_code": "OPTIMIZATION_RUN",
  "error_code": "USER_CONCURRENCY_LIMIT",
  "retry_after_seconds": 60 }
```

with a `Retry-After` header. **Never**: queue depth, worker counts, remaining
allowance, global capacity, or any other principal's activity — each turns a
rejection into a reconnaissance signal. Asserted by
`test_a_rejection_body_reveals_nothing_about_the_platform`.

Closed reason enumeration: `USER_RATE_LIMIT` · `TENANT_RATE_LIMIT` ·
`USER_CONCURRENCY_LIMIT` · `TENANT_CONCURRENCY_LIMIT` · `GLOBAL_CAPACITY` ·
`QUEUE_CAPACITY` · `PAYLOAD_TOO_LARGE` · `TOO_MANY_ITEMS` ·
`DUPLICATE_ACTIVE_OPERATION` · `OPERATION_COMPLEXITY_LIMIT` ·
`ADMISSION_STORE_UNAVAILABLE`.
(`TENANT_*`, `QUEUE_CAPACITY` and the size/complexity codes are defined and
currently unraised; each carries a recorded reason in `UNUSED_REJECTION_REASONS`
and a test enforces that the record stays true — see §25 and §27.)

---

## 17. Authorization ordering, RLS, and security

**Ordering.** For owner-scoped resources: authenticate → authorize ownership →
operation-specific admission. Admission is keyed on the **caller and the
operation class only, never on the entity**, so a rejection cannot confirm that
another tenant's entity exists. The integrity-verify handler documents this
inline. For account-level limits (auth, rate), enforcement is earlier by design —
there is no resource to leak.

**RLS.** The admission tables deliberately have **no RLS policy**, and that is
load-bearing rather than an oversight: a global capacity check must count rows
belonging to every principal, and a policy keyed on `app.user_id` would silently
return only the caller's own rows — a limiter that stops limiting precisely when
the platform is busiest.

The protection is **shape, not policy**: there is nothing there worth reading.
No financial column exists, and no API surface returns a row from either table —
a caller learns only its own accept/reject outcome. Both halves are asserted:

- `test_admission_tables_carry_no_financial_columns` scans
  `information_schema.columns` for financial-looking names;
- `test_a_rejection_reveals_no_capacity_or_other_principal_state` pins what a
  refused caller can see.

No RLS on any *existing* table was weakened, bypassed, or read around. No new
`SECURITY DEFINER` function (asserted). No new grant to `PUBLIC` (asserted). No
grant to the NOLOGIN worker roles — workers release leases through the ordinary
application role.

**Scope is never caller-supplied.** Every call site passes
`user_scope(authenticated_user_id)`. No body field or header can name a scope.

---

## 18. Privacy

Admission rows carry: principal id, operation code, bounded counters,
timestamps, release reason, and an owner-prefixed dedupe key. **Never**: income,
tax amounts, document text, SIN, employer names, or snapshot payloads. An
admission row leaking would reveal that somebody ran an optimization — never
what was in it.

Logs carry codes only: `operation=OPTIMIZATION_RUN decision=REJECTED
reason=USER_CONCURRENCY_LIMIT`. Store-failure logs record the **exception class,
never its message** — a DBAPI error string can carry SQL fragments and parameter
values.

---

## 19. Metrics

`admission_requests_total` · `admission_accepted_total` ·
`admission_rejected_total` · `job_lease_recovered_total`.

Labels are bounded codes: `operation_code`, `reason_code`, `outcome`. **Never**
`user_id`, `tenant_id`, `request_id`, or `job_id` — all unbounded in cardinality,
and a per-user metric label is an activity log wearing a metric's clothes.

**These are in-process `collections.Counter` objects with no exporter.** There is
no Prometheus endpoint, no scrape, and **no alerting** in this repository. Saying
otherwise would be claiming an operational capability that does not exist.
Alerting infrastructure is explicitly out of scope for this entry.

---

## 20. Fail-open vs fail-closed

Stated **per class**, because the two wrong answers cost different amounts.

- **Fail-closed** (all expensive classes, plus `AUTH_ATTEMPT`): if the store
  cannot answer, refuse. Failing open on an optimization would remove the
  protection exactly when a database problem makes overload most likely.
- **Fail-open** (`CHEAP_READ`, `NORMAL_WRITE`): admit. A limiter fault must not
  turn into a site outage for reads.

Asserted by `test_expensive_classes_fail_closed_and_cheap_reads_fail_open` and
exercised against a session whose every statement raises.

---

## 21. Configuration

All in `app.core.config`, `ONYX_`-prefixed, validated at startup:

```
ADMISSION_ENABLED
RATE_LIMIT_AUTH_PER_MINUTE           RATE_LIMIT_ANALYSIS_PER_MINUTE
RATE_LIMIT_OPTIMIZATION_PER_MINUTE   RATE_LIMIT_SCENARIO_PER_MINUTE
MAX_ACTIVE_ANALYSES_PER_USER         MAX_ACTIVE_OPTIMIZATIONS_PER_USER
MAX_ACTIVE_SCENARIOS_PER_USER        MAX_ACTIVE_DOCUMENT_PROCESSING_PER_USER
GLOBAL_MAX_ACTIVE_OPTIMIZATIONS      GLOBAL_MAX_ACTIVE_SCENARIOS
GLOBAL_MAX_ACTIVE_ANALYSES           GLOBAL_MAX_ACTIVE_DOCUMENT_PROCESSING
ADMISSION_LEASE_SECONDS
```

Rejected at startup: **zero** (would admit nothing), **negative** (meaningless),
and **per-user above the platform cap** (a limit that looks enforced and can
never be reached). `AdmissionPolicy` validates again on construction, so a bad
override fails at import rather than at 3am.

Global capacity values are **not** exposed to callers: a tenant learning the
global cap learns how much traffic it takes to deny service to everyone else.

---

## 22. Migration

```
revision:   0044_admission_control, then 0045_admission_preauth_scopes
tables:     admission.rate_counter, admission.lease
indexes:    4 (2 partial, on live leases and live dedupe keys)
constraints: PK + 4 CHECK
RLS:        deliberately none — see §17
grants:     onyx_app_rw (CRUD), onyx_app_ro (SELECT); PUBLIC revoked
retention:  sweep_expired() marks lapsed leases; purge() deletes in bounded
            batches on an hourly beat — 2h for counters, 24h for released
            leases
downgrade:  the SQL baseline is torn down as a unit at 0001_foundation
```

0045 widens ONE `CHECK` on `admission.rate_counter` to admit the two
pre-authentication scope types. `admission.lease` keeps the narrow constraint on
purpose: `AUTH_ATTEMPT` declares no concurrency, so a pre-authentication lease
should be impossible, and leaving the constraint narrow makes that an enforced
invariant rather than a comment. Every existing row satisfies the wider
predicate, so the validation scan cannot fail.

Additive only: one new schema, two new tables. No existing table, column,
constraint, trigger, policy, or grant is touched, so it cannot affect any sealed
result or existing query plan. Round-trip and drift verified in §24.

---

## 23. Performance

Local measurement, PostgreSQL 16 on one machine. **Throughput here is not
production capacity**; what transfers is overshoot (must be zero) and the rough
per-admission cost.

| Path | SQL operations | p50 | p95 |
|---|---|---|---|
| Cheap read (rate check only) | **1** (conditional UPSERT) | **3.36ms** | 4.90ms |
| Expensive admission, uncontended | 4 (rate, dedupe, 2× lock+count+insert) | ~8ms | — |
| Lease acquisition, 100-way contention on one scope | lock+count+insert | 137ms | 237ms |
| Lease acquisition, 500-way contention on one scope | lock+count+insert | 511ms | 839ms |

The contended figures are the advisory lock serializing exactly what must be
serialized: 500 requests for 2 slots. 498 of them are *rejections*, which is the
cheap outcome — and a real principal never generates that shape, because the
rate check refuses it long before the lock does. Contention is per
`(scope, operation)`, so it does not spread. §26 has the full comparison against
`pg_try_advisory_xact_lock`, including why it was not adopted.

### Load simulation (`scripts/load_admission.py`)

```
100 concurrent OPTIMIZATION_RUN, one principal   limit=2  admitted=2   OVERSHOOT = 0
500 rapid SCENARIO_RUN across 20 principals      ceiling=60 admitted=60 OVERSHOOT = 0
50 identical retries of one logical optimization jobs started=1  DUPLICATES = 0
mixed: 1 noisy (200 attempts) vs 10 quiet        noisy=3 (cap 3), quiet admitted 10/10
                                                                      OVERSHOOT = 0
100 contenders on ONE scope, blocking vs try     admitted=2 both      OVERSHOOT = 0
500 contenders on ONE scope, blocking vs try     admitted=2 both      OVERSHOOT = 0
```

The mixed workload is the anti-monopoly property directly: a principal making 200
attempts holds exactly its cap of 3, and all ten quiet principals are still
admitted.

---

## 24. Tests

| Suite | Count | Covers |
|---|---|---|
| `tests/integration/test_admission_control.py` | 16 | rate window/expiry/burst, 10-way race, global cap, lease pairing, retry storms, dedupe ownership, lease expiry, policy validation |
| `tests/integration/test_admission_api.py` | 8 | 429 + `Retry-After`, body has no leak, concurrent analyses, MIME/size/filename/complexity refusals |
| `tests/integration/test_admission_failure_modes.py` | 7 | store down (open *and* closed), stale lease, malformed policy, malformed settings, failing body releases |
| `tests/security/test_admission_isolation.py` | 7 | no financial columns, no PUBLIC grant, no SECURITY DEFINER, cross-principal isolation, rejection leak, global-scope forgery |
| `tests/integration/test_admission_auth.py` | 8 | the credential surface: per-identity and per-address limits, refusal **before** Argon2, no account-existence disclosure, both counters charged, forwarded headers ignored, no address stored in the clear, spelling folded |
| `tests/integration/test_admission_document_bounds.py` | 5 | the lying declaration (1 MB declared / 30 MB sent / 25 MB limit), tighter-of-two ceiling, undeclared upload bounded, byte-accurate text bound, 20 retries against one in-flight extraction |
| `tests/security/test_admission_preauth.py` | 11 | authorization before admission, pre-auth scope cannot hold a lease, domain separation, digest depends on the secret, production refuses the dev default, purge keeps live windows and drops dead ones |
| `tests/integration/test_admission_control.py` (pool/counter regressions) | 2 | a refused attempt still counts against the rate window; a refused admission does not leak a platform slot |
| `tests/unit/test_admission_wiring.py` | 16 | **no database**: every class wired or explicitly reserved, every reason raised or reserved, nothing in `app/` publishes to the broker, every task explicitly routed |

Clock is **injected, never slept** — a window-expiry test built on `sleep(60)` is
a minute of CI per assertion and flakes on a slow runner. Concurrency tests use
genuinely separate transactions via `asyncio.gather`; a sequential loop would
pass against a completely broken implementation.

**CI**: all 78 deterministic tests are blocking — the database ones in the
`security` job, the source-reading wiring ledger in the fast `lint` job, where it
needs no PostgreSQL and fails in under a second.
**Release-level only**: `scripts/load_admission.py` (§23) and the pollution
regression — CI does not pay for them on every push.

---

## 25. Wiring audit (Phase 2 §1)

Every `OperationClass` was re-checked against the repository as it actually
stands — routers mounted, handlers reachable, services doing work — and given
exactly one classification. Enum membership was not treated as evidence of
anything.

| Class | Production surface found | Classification | Where it is guarded |
|---|---|---|---|
| `AUTH_ATTEMPT` | `POST /auth/login`, `/auth/register`, `/auth/refresh`, `POST /admin/auth/login` — all mounted; `AuthService.authenticate` runs an Argon2id verification per attempt | REACHABLE_REQUIRES_ADMISSION | `admit_auth_attempt`, before the user lookup |
| `IMPORT_RUN` | `POST /tkms/imports`, `/tkms/imports/{id}/reparse`, `/admin/ingestion/jobs` — run the full parse→extract→promote→validate→compare pipeline inline | REACHABLE_REQUIRES_ADMISSION | `admission_guard`, scoped to the acting operator |
| `ADMIN_RULE_PUBLISH` | `POST /tkms/versions/{id}/publish` (publishes + reindexes), `POST /admin/rules/{id}/publish` | REACHABLE_REQUIRES_ADMISSION | `admission_guard`, outside the service call so a refusal precedes activation |
| `AI_EXPLAIN` | `POST /ai/ask`, `POST /ai/conversations/{id}/messages` — mounted, backed by `AiService.ask`: pgvector retrieval, a lazy full reindex of the year on first use, and a provider call | REACHABLE_REQUIRES_ADMISSION | `admission_guard` on both |
| `NORMAL_WRITE` | `POST /financials/income`, `/financials/expenses`, `/documents/{id}/confirm`, `/ai/conversations` — each an unbounded row creator | REACHABLE_REQUIRES_ADMISSION | `admission_guard` on the four |
| `CHEAP_READ` | Many `GET` endpoints, all bounded indexed RLS-scoped reads | REACHABLE_ADMISSION_NOT_NEEDED | Deliberately unguarded — see below |

`AI_EXPLAIN` was expected to come out RESERVED_FUTURE_CAPABILITY and did not.
The router is mounted, the handler is reachable today, and the work behind it is
real database work regardless of the LLM adapter being a deterministic offline
template — a first ask for a year triggers a full reindex of that year's
published rules. Guarding a reachable endpoint is not implementing the AI
capability, and nothing about the AI feature itself was touched.

`CHEAP_READ` is the one class left unguarded, and the reason is that guarding it
would cost more than it saves: an admission-store round trip added to the
cheapest operations in the system means the limiter becomes a meaningful share
of the load it exists to protect against, and its own store saturates first.
Read-abuse belongs at ingress, which sees the request before a worker is woken.
The policy stays as the declared template for that layer.

**This table is enforced, not just written down.** `UNWIRED_BY_DESIGN` in
`app/services/admission/policy.py` carries the same verdicts, and
`tests/unit/test_admission_wiring.py` fails the build if a class is neither
guarded in `app/` nor listed there with a reason — and fails equally if a listed
class turns out to have a call site after all. The same discipline covers
`RejectionReason` through `UNUSED_REJECTION_REASONS`.

## 26. Lock contention, measured (Phase 2 §7)

The concurrency check takes a blocking `pg_advisory_xact_lock` per
(scope, operation). The question was whether that can exhaust the connection
pool under contention, and whether `pg_try_advisory_xact_lock` would be better.
Both were measured, on one machine against one PostgreSQL, with every contender
on a SINGLE scope — the worst case the design can produce, since real traffic
spreads across principals.

| Contenders | Mode | Admitted (cap 2) | Overshoot | p50 | p95 | max | Peak backends | Errors |
|---|---|---|---|---|---|---|---|---|
| 100 | blocking (production) | 2 | **0** | 137 ms | 237 ms | 245 ms | 31 | 0 |
| 100 | try-lock | 2 | 0 | 100 ms | 151 ms | 157 ms | 29 | 0 |
| 500 | blocking (production) | 2 | **0** | 511 ms | 839 ms | 878 ms | 30 | 0 |
| 500 | try-lock | 2 | 0 | 288 ms | 441 ms | 458 ms | 30 | 0 |

**The blocking lock is kept, and the measurement is why.** Try-lock is about
twice as fast and answers the wrong question: 467 of the 500 contenders got a
lock-miss, and a contender that could not take the lock has learned nothing
about whether it is under its limit, so the only safe answer is to refuse. That
is a **93% spurious-refusal rate** — callers with quota to spare, turned away
because someone else held a lock — versus a half-second wait. Retrying instead
of refusing just reintroduces the waiting with extra round trips.

Latencies move run to run with machine noise. What is stable, and what the
conclusion rests on, is the shape: **overshoot 0 in every run** and try-lock's
93-94% spurious-refusal ratio.

Reproduce with `python scripts/load_admission.py`.

### 26.1 Pool contention — the two ceilings, and which one was reached

An earlier version of this section reported "peak backends = 30" alongside a
claim that pool exhaustion was "~30x away". Those are two different quantities
and putting them in one sentence was wrong. **30 IS the application pool
ceiling**, so the pool was not near its limit — it was at it, in every run. The
"30x" referred to something else entirely: wall-clock headroom against
`pool_timeout`. The claim has been withdrawn and the question measured properly
(`scripts/probe_admission_pool.py`).

The two ceilings are not the same thing and must not be conflated:

| Ceiling | Value | Source |
|---|---|---|
| Application pool | **30** concurrent checkouts | `db_pool_size` 10 + `db_max_overflow` 20 |
| `pool_timeout` | 30 s | SQLAlchemy default |
| `pool_recycle` | none (-1) | SQLAlchemy default |
| PostgreSQL server | **97** usable | `max_connections` 100 − 3 `superuser_reserved_connections` |

The application can therefore never come close to the server ceiling; the pool
is the binding constraint, by a factor of three.

**Why this needed measuring at all.** A contender waiting on
`pg_advisory_xact_lock` holds its pooled connection while it waits. Enough
contenders on one hot scope and every connection in the pool is parked on one
lock — at which point an unrelated request, from a different user doing
something entirely different, cannot get a connection. The concurrency limit
would be perfectly enforced while the application stopped answering. Zero
overshoot with a starved pool is not a pass.

Measured on one scope, with two unrelated probes running continuously through
the same pool throughout:

| Load | Path | Checkout wait p50/max | Lock or admission work p50 | Peak checked out | Checkout timeouts | Unrelated max |
|---|---|---|---|---|---|---|
| 100 | lock only | 118 / 210 ms | 51 ms | 30/30 | 0 | 164 ms |
| 500 | lock only | 600 / 970 ms | 54 ms | 30/30 | 0 | 931 ms |
| 1000 | lock only | 845 / 1495 ms | 41 ms | 30/30 | 0 | 1464 ms |
| 1000 | **production** (`evaluate`) | 706 / 1219 ms | 31 ms | 30/30 | 0 | 1191 ms |
| 1000 | **control — no admission at all** | 418 / 746 ms | 3 ms | 30/30 | 0 | 719 ms |

Four things follow, and the last one is the point.

1. **The dominant cost is queueing for a connection, not the lock.** At 1000
   contenders the checkout wait is 706 ms and the entire admission decision —
   advisory lock included — is 31 ms. The lock was never the bottleneck.
2. **Nothing failed.** Zero checkout timeouts at every level, no errors, and
   every connection returned: 0 checked out after the runs, so admission leaks
   nothing. Worst observed request 1.31 s against a 30 s pool timeout.
3. **Unrelated work stays serviceable but is degraded.** It completes, never
   errors, and its worst case (1.19 s) is far inside the API deadline — but a
   2 ms operation taking 1.19 s is a real tail degradation and the probe's
   throughput collapses while the flood runs. That is stated, not smoothed over.
4. **The control settles the attribution.** The same 1000 concurrent tasks
   doing nothing but `SELECT 1` saturate the pool identically — 30/30, 970
   queued — and degrade the unrelated probe to 719 ms. So the saturation
   belongs to *1000 concurrent requests against a 30-connection pool*, not to
   admission. Admission adds roughly 1.6x on top of that floor, which is the
   cost of its statements, not monopolisation. The remedy for the remaining
   degradation is pool sizing and ingress concurrency limiting — not a change
   to the limiter.

Against the criteria for keeping the blocking lock: no checkout failures,
bounded checkout wait, unrelated traffic serviceable, latency inside the API
timeout, no leak. All five hold, so **the blocking lock stays**.

Reproduce with `python scripts/probe_admission_pool.py`.

### 26.2 The defect this investigation found

The pool measurement was worse on the *production* path than on the raw-lock
path — 4.1 s wall versus 1.7 s — which made no sense, because the rate check is
supposed to refuse a flood cheaply before it ever reaches the lock. It did not,
and the reason was a real defect in the rate limiter:

**`_reject` raised from inside the caller's transaction, and the raise rolled
back the rate-counter increment the decision had just made.** The counter
therefore only ever recorded admissions that SUCCEEDED. Fifty attempts against a
scope at its concurrency cap, with an allowance of 8, left `request_count` at 2
— exactly the number that got in. Every refused attempt erased its own evidence.

The consequence was not cosmetic. A caller at its cap could retry without limit
forever, and each retry took the advisory lock, ran the count query, and held a
pooled connection while doing it. The rate limit exists precisely to make that
storm cheap, and it never engaged.

The fix is the one already applied to the credential surface in Phase 2 and not
generalised then: the decision is RETURNED (`AdmissionOutcome`), the transaction
commits, and `admission_guard` raises afterwards. Because the rejection path now
commits, a global lease taken on the way to a user-concurrency refusal must be
released explicitly rather than rolled back — both are asserted by
`tests/integration/test_admission_control.py`.

After the fix, the same 1000-contender production flood refuses 992 callers at
the rate check for one conditional UPSERT each: wall time 4.1 s to 1.4 s, and
the unrelated-traffic tail 3.83 s to 1.19 s.

## 27. Known limitations

Real, not hedging:

1. **Anti-monopoly caps, not fair scheduling.** A principal at its cap cannot
   take more, but nothing serves a starved principal *first* when capacity frees.
   First-come-first-served within the caps.
2. **No queue-depth admission**, and none is needed today: nothing in `app/`
   publishes to the broker, so there is no user-reachable enqueue path whose
   depth could be refused. `QUEUE_CAPACITY` is reserved with that premise
   asserted by a test, so it cannot quietly become false.
3. **`TENANT_*` reason codes are unused** because the tenant *is* the user in
   this product (§3). They exist so a future organization tier does not need a
   new enumeration.
4. **Metrics have no exporter and there is no alerting** (§19).
5. **The incident switch removes everything at once.** `ONYX_ADMISSION_ENABLED=false`
   bypasses every rate, concurrency and capacity limit, including the login
   throttle — that is the point of an incident switch, and it is why the bypass
   is counted (`admission_bypassed_total`) and announced once per process at
   WARNING. It does not disable the size and complexity bounds: those are
   validation, and a payload that is too large is malformed whether or not the
   platform is under strain.
6. **Worker queue isolation is half a guarantee.** Which queue a task lands on
   is code-enforced and asserted (`tests/unit/test_admission_wiring.py`); how
   many workers consume each queue is a deployment property — a `-Q` flag and a
   concurrency setting — that no file in this repository decides. One worker
   subscribed to all queues makes the separation nominal.
7. **Fixed window, not sliding.** A caller can spend two allowances across a
   window boundary; `burst` assumes that shape.
8. **The source-address scope degrades behind a load balancer.** `client_ip`
   reads the ASGI transport peer and refuses to trust `X-Forwarded-For`, so
   behind a proxy that does not use PROXY protocol every request appears to come
   from the balancer and the address scope collapses to one bucket. That fails
   toward over-throttling one shared bucket rather than toward no throttling;
   the per-identity scope is unaffected. Trusting the header requires a
   configured list of trusted hops, which does not exist yet.
9. **Per-identity throttling is a lockout vector.** Someone who knows an
   address can spend its allowance and have the owner refused for the rest of
   the minute. The window is one minute, the alternative leaves the account open
   to sustained guessing from a rotating address pool, and the trade is made
   knowingly.
10. **Failed logins record nothing.** `AuthService` adds a `login_event` row and
   then raises, so the row rolls back with the request's transaction. Noticed
   while writing the ordering test, and left alone: it is authentication
   behaviour, not admission behaviour, and changing it in this entry would be
   scope creep. Flagged here so it is not lost.

## 28. Remaining risks

- Every class with a reachable production surface is guarded (§25). The one
  unguarded class is `CHEAP_READ`, deliberately and with a recorded reason.
- The two pre-authentication scopes are the only admission keys an
  unauthenticated caller influences. Neither is stored in the clear, neither can
  hold a lease (the database refuses it), and neither can be chosen by the
  caller — the address comes from the transport and the identity is folded and
  keyed. `tests/security/test_admission_preauth.py` asserts each of those.
- Advisory-lock contention is per scope and bounded (§26). A pathological client
  can still make its *own* admissions slow; it affects only that principal.
- **Connection-pool headroom is the real capacity limit, not the limiter.** The
  pool saturates at 30 concurrent checkouts, and a control workload doing no
  admission at all saturates it identically (§26.1). Nothing fails and nothing
  leaks, but unrelated traffic degrades while a flood is in flight. Raising
  `db_pool_size`/`db_max_overflow` — there is room, the server ceiling is 97 —
  or limiting concurrency at ingress is the lever; changing the limiter is not.
- Admission-table growth is now partly attacker-controlled through the
  `AUTH_SUBJECT` scope, and the mitigation is an hourly bounded purge. A
  deployment that never runs Beat keeps the counters forever — costing disk, not
  correctness, since admission already ignores closed windows.
