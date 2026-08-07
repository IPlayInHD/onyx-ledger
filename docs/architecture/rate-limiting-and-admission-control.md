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
they are different queues consumed by different workers. This is *queue*
isolation, and it is only as real as the deployment — if an operator runs one
worker subscribed to all queues, the isolation is nominal. That is a deployment
requirement, and it is stated here because the code cannot enforce it.

`task_acks_late = True` and `worker_prefetch_multiplier = 1` were already set:
a worker holds one message at a time, so a slow task cannot sit on a batch, and
a crashed worker's message returns to the queue.

**Queue-depth admission is NOT implemented.** The global concurrency cap serves
the same purpose from the other side — it bounds work *in flight* rather than
work *queued* — and reading broker depth per request would either be an
expensive scan or a second, ungoverned source of truth. Stated as a limitation
in §26 rather than claimed.

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
| `MAX_DOCUMENT_BYTES` | 25 MB | declared size, refused before a presigned URL is issued |
| `MAX_DOCUMENTS_PER_REQUEST` | 1 | one upload describes one document |
| `MAX_FILENAME_LENGTH` | 255 | Pydantic `max_length` |
| `ALLOWED_DOCUMENT_MIME_TYPES` | 7 types | **allow-list** — a deny-list admits every format nobody thought of |
| `MAX_EXTRACTION_TEXT_BYTES` | 1 MB | Pydantic |
| `MAX_EXTRACTION_FIELDS` | 200 | handler |
| `MAX_IMPORT_BYTES` / `MAX_IMPORT_ROWS` | 50 MB / 10 000 | named; see §26 |
| `MAX_SCENARIO_LEVERS` / `_ASSUMPTIONS` | 25 / 25 | **pre-existing**, enforced at the schema *and* the domain parser |

**A request can be tiny in bytes and enormous in work** — a hundred scenario
levers is a few hundred bytes and a combinatorial assembly problem. Size bounds
and complexity bounds are different things and both are listed.

**Honest limit on the document size check:** the bytes never pass through this
process (they go straight to object storage via a presigned URL), so the size
check here is on the *declared* size and is an early refusal, not the
enforcement point. A client that under-declares still gets a URL. The
enforcement that matters is the presigned policy's own content-length limit at
the storage boundary. Both are stated in the handler docstring rather than
implying the API check is authoritative.

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
(`TENANT_*` and `QUEUE_CAPACITY` are defined and currently unused — see §26.)

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
revision:   0044_admission_control
tables:     admission.rate_counter, admission.lease
indexes:    4 (2 partial, on live leases and live dedupe keys)
constraints: PK + 4 CHECK
RLS:        deliberately none — see §17
grants:     onyx_app_rw (CRUD), onyx_app_ro (SELECT); PUBLIC revoked
retention:  sweep_expired() marks lapsed leases; no automatic deletion yet (§26)
downgrade:  the SQL baseline is torn down as a unit at 0001_foundation
```

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
| Cheap read (rate check only) | **1** (conditional UPSERT) | **2.13ms** | 2.64ms |
| Expensive admission, uncontended | 4 (rate, dedupe, 2× lock+count+insert) | ~8ms | — |
| Expensive admission, 100-way contention on one scope | same | 571ms | 728ms |

The contended figure is the advisory lock serializing exactly what must be
serialized: 100 requests for 2 slots. 98 of them are *rejections*, which is the
cheap outcome — and a real principal never generates that shape. Contention is
per `(scope, operation)`, so it does not spread.

### Load simulation (`scripts/load_admission.py`)

```
100 concurrent OPTIMIZATION_RUN, one principal   limit=2  admitted=2   OVERSHOOT = 0
500 rapid SCENARIO_RUN across 20 principals      ceiling=60 admitted=60 OVERSHOOT = 0
50 identical retries of one logical optimization jobs started=1  DUPLICATES = 0
mixed: 1 noisy (200 attempts) vs 10 quiet        noisy=3 (cap 3), quiet admitted 10/10
                                                                      OVERSHOOT = 0
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

Clock is **injected, never slept** — a window-expiry test built on `sleep(60)` is
a minute of CI per assertion and flakes on a slow runner. Concurrency tests use
genuinely separate transactions via `asyncio.gather`; a sequential loop would
pass against a completely broken implementation.

**CI**: the 38 deterministic tests are blocking, in the `security` job.
**Release-level only**: `scripts/load_admission.py` (§23) and the pollution
regression — CI does not pay for them on every push.

---

## 25. Known limitations

Real, not hedging:

1. **Anti-monopoly caps, not fair scheduling.** A principal at its cap cannot
   take more, but nothing serves a starved principal *first* when capacity frees.
   First-come-first-served within the caps.
2. **No queue-depth admission.** The global concurrency cap bounds work in
   flight, not work queued. `QUEUE_CAPACITY` is defined and unused.
3. **`TENANT_*` reason codes are unused** because the tenant *is* the user in
   this product (§3). They exist so a future organization tier does not need a
   new enumeration.
4. **Document size is checked on the declared value.** The authoritative
   enforcement is the storage tier's content-length policy (§15).
5. **Metrics have no exporter and there is no alerting** (§19).
6. **Worker queue isolation is a deployment property.** One worker subscribed to
   all queues makes it nominal; the code cannot enforce the deployment.
7. **`MAX_IMPORT_BYTES` / `MAX_IMPORT_ROWS` are named but not yet enforced** at
   the TKMS import handler — the class and policy exist, the byte/row check does
   not. The existing checksum dedupe and the four-eyes gate still apply.
8. **No automatic retention deletion** for admission rows; `sweep_expired()`
   marks, it does not purge.
9. **Fixed window, not sliding.** A caller can spend two allowances across a
   window boundary; `burst` assumes that shape.
10. **AI, admin-publish, and import paths are classified but not yet wired** to
    `admission_guard` — see §26.

## 26. Remaining risks

- The classes wired to `admission_guard` are: `ANALYSIS_RUN`,
  `OPTIMIZATION_RUN`, `SCENARIO_RUN`, `DOCUMENT_UPLOAD`, `DOCUMENT_PROCESS`,
  `INTEGRITY_VERIFY`. **`AUTH_ATTEMPT`, `IMPORT_RUN`, `ADMIN_RULE_PUBLISH`,
  `AI_EXPLAIN`, `CHEAP_READ` and `NORMAL_WRITE` have policies but no call site
  yet.** The mechanism, policy, and tests exist; the wiring is a follow-up. This
  is stated rather than implied by the presence of a policy.
- `AUTH_ATTEMPT` is the most significant of those: credential-testing remains
  unthrottled at the application layer. It needs an IP-scoped dimension for
  unauthenticated attempts, which the current `ScopeType` set does not model.
- Advisory-lock contention is per scope and bounded, but a pathological client
  can still make its *own* admissions slow (measured 571ms p50 at 100-way).
  It affects only that principal.
