# Runbook — Scheduled replay-integrity verification

**Audience:** whoever is on call for Onyx Ledger.
**Scope:** the scheduled job that re-proves sealed IOE results still reproduce.

> **Alerting status.** No alerting backend consumes the mismatch signal today.
> A mismatch is written to the database and emitted as a structured log line,
> and **nothing pages anyone**. Until §4 of the closure plan lands, detection
> depends on a human running the inspection queries below. See §13.

---

## 1. What this job is for

Every sealed optimization, portfolio and scenario carries a hash that claims to
identify the calculation that produced it. Verification re-runs that calculation
from the record's own pinned inputs and checks the hash still comes out the
same. It answers one question: **can we still reproduce what we told the user?**

That is a different question from freshness. A result can be `stale` and
perfectly reproducible (the user's income changed, but our answer at the time was
correct for the data at the time), or `current` and non-reproducible (far more
serious). The two are never merged.

Verification **never modifies a sealed result.** It writes an append-only check
row and four current-metadata columns on the parent. The database enforces this:
`ioe.reject_result_mutation()` compares stripped row images and rejects any
update that touches anything else.

## 2. Task, schedule and configuration

| Item | Value |
|---|---|
| Celery task | `workers.tasks.ioe.verify_sealed_integrity` |
| Queue | `ioe_integrity` (its own — it must never block freshness or optimization) |
| Beat entry | `ioe-integrity-verification` in `backend/workers/celery_app.py` |
| Default interval | **15 minutes** |
| Default batch | **10 records per execution**, across all target types combined |
| Hard cap | **50** — enforced by the scheduler *and* again inside the SQL |

| Environment variable | Default | Purpose |
|---|---|---|
| `ONYX_IOE_INTEGRITY_VERIFICATION_ENABLED` | `true` | Master switch. `false` makes the task a no-op. |
| `ONYX_IOE_INTEGRITY_BATCH_SIZE` | `10` | Records per execution, all types combined. |
| `ONYX_IOE_INTEGRITY_INTERVAL_MINUTES` | `15` | Beat interval. |
| `ONYX_IOE_INTEGRITY_TIMEOUT_SECONDS` | `60` | Per-record ceiling. |

A worker must run the `ioe_integrity` queue for the job to execute at all:

```
celery -A workers.celery_app worker -Q ioe_integrity -c 1
celery -A workers.celery_app beat
```

## 3. Required database privileges

Verification uses the same keyhole discipline as the freshness relay.

| Role | What it holds |
|---|---|
| `onyx_freshness_worker` (NOLOGIN) | `EXECUTE` on `ioe.claim_integrity_targets` and `ioe.recover_stale_integrity_checks`, `USAGE` on schema `ioe`, and **zero direct table privileges anywhere** |
| `onyx_app_rw` | member of `onyx_freshness_worker`; the runtime identity the worker process connects as |
| `onyx_migrator` | owns the schema and the `SECURITY DEFINER` functions; applies DDL |
| `PUBLIC` | `EXECUTE` revoked on every definer function |

Both privileged functions are `SECURITY DEFINER` with a fixed `search_path` and
return **identifiers and an owner id only** — no hashes, no result columns, no
financial values. The replay itself then runs under ordinary tenant RLS with
`app.user_id` set from the claimed row, so every row it reads or writes is
checked by the same policies that protect a logged-in user's request.

`tests/security/test_privilege_invariants.py` enumerates this from the catalogue
and fails if the worker gains a table privilege in any schema, present or future.

## 4. Manual bounded invocation

```
cd backend
PYTHONPATH=. python scripts/verify_integrity.py --batch-size 5
PYTHONPATH=. python scripts/verify_integrity.py --entity-type scenario --batch-size 3
```

Exit code `2` means at least one mismatch was found; `0` otherwise. Output is
counts and enumerated reason codes only, so it is safe to paste into an incident
channel.

The script calls the same `IntegrityScheduler` the Celery task calls. It cannot
bypass the privileged claim interface or the verifier, and there is deliberately
no flag that would let it. There is **no HTTP endpoint** for this — verification
is an operational action, not a product surface.

Equivalent Celery invocation, if you would rather go through the broker:

```
celery -A workers.celery_app call workers.tasks.ioe.verify_sealed_integrity --args='[5]'
```

## 5. What gets verified, and in what order

- **Sampled types:** optimizations and scenarios. Portfolios are **not** sampled
  directly — `ioe.claim_integrity_targets` rejects the type. A portfolio is
  verified on demand through the API; its policy is its run's.
- **Eligibility:** `workflow_status = 'completed'` **and** a non-null sealed
  result hash. Running, failed and unsealed records are never claimed.
- **Ordering:** `last_integrity_checked_at ASC NULLS FIRST, created_at ASC` —
  never-checked records come first, then the least recently checked. This is
  what prevents starvation: every check stamps the record, sending it to the
  back of the queue.
- **Retry of bad outcomes:** a mismatched or unavailable record is re-checked on
  its next rotation, exactly like any other. There is no quarantine list.
- **Legacy records** rotate back in like everything else. Each such check is
  cheap — `_refuse_legacy()` short-circuits **before** any dependency resolution
  or engine run — but it does consume one claim slot per rotation. If the legacy
  population is large enough to crowd out useful work, that is the signal to
  re-run those records rather than to widen the batch.
- **Tenants:** selection is global and ordered by check age, **not** by tenant.
  That is age fairness, not per-tenant fairness: a tenant with far more records
  will occupy proportionally more of each batch, and a tenant whose records were
  all checked recently will be absent from several cycles. Every record is still
  reached within `ceil(N / batch)` cycles, so nothing is starved indefinitely,
  but do not read this as a per-tenant service guarantee. The replay for each
  record runs in that record owner's RLS context.

## 6. Outcome states

| Persisted `status` | `reason_code` | Shown as | Meaning |
|---|---|---|---|
| `verified` | `NONE` | `verified` | Reproduced exactly from its own pinned inputs. |
| `mismatch` | `RESULT_HASH_MISMATCH` / `PORTFOLIO_HASH_MISMATCH` | `non_reproducible` | The replay ran on a contractually reproducible record and got a different identity. **The only state that implies a defect.** |
| `unavailable` | `BASELINE_SNAPSHOT_UNAVAILABLE`, `BASELINE_RESULT_UNAVAILABLE`, `PINNED_RULE_SNAPSHOT_UNAVAILABLE`, `SEALED_EVIDENCE_INCOMPLETE`, … | `unavailable` | A pinned artifact is missing, replaced or malformed. **Nothing was compared.** Often self-healing. |
| `unavailable` | `LEGACY_EXECUTION_POLICY_UNVERIFIABLE` | `legacy_unverifiable` | Sealed before the frozen-input correction; never carried what a replay needs. **Not a fault.** |
| `failed` | `REPLAY_EXECUTION_FAILED` | `unavailable` | The verifier itself crashed. Proves nothing about the record; the entity is never downgraded on the strength of a bug. |

## 7. Distinguishing legacy from a real regression

This is the distinction to get right at 3am.

```sql
-- genuine deterministic replay regressions — investigate these
SELECT entity_type, count(*)
  FROM ioe.integrity_check
 WHERE status = 'mismatch' AND completed_at > now() - interval '24 hours'
 GROUP BY 1;

-- records that predate the guarantee — NOT failures, do not page on these
SELECT count(*)
  FROM ioe.integrity_check
 WHERE reason_code = 'LEGACY_EXECUTION_POLICY_UNVERIFIABLE'
   AND completed_at > now() - interval '24 hours';
```

A legacy record can **never** be a `mismatch`: `_refuse_legacy()` short-circuits
before the hash comparison, and `ck_integrity_check_mismatch_reason` admits only
the two hash reasons when `status = 'mismatch'`. If you ever see a legacy reason
on a mismatch row, the constraint has been altered — treat that as a P1.

The same split is available as metrics without knowing the reason vocabulary:
`integrity_batch_mismatch` (act) versus `integrity_batch_legacy_unverifiable`
(ignore) versus `integrity_batch_unavailable_dependency` (usually self-healing).

## 8. First response to a genuine mismatch

1. **Do not re-run, re-seal or "fix" the record.** The sealed evidence is the
   thing under investigation, and it is immutable by design.
2. Identify it:
   ```sql
   SELECT id, entity_type, optimization_run_id, scenario_id, portfolio_id,
          reason_code, expected_result_hash, actual_result_hash, completed_at
     FROM ioe.integrity_check
    WHERE status = 'mismatch'
    ORDER BY completed_at DESC LIMIT 20;
   ```
3. Ask what changed in the *executable* versions since the record was sealed —
   engine, reference data, canonical serialization, lever/assumption registries.
   A pinned version that no longer matches the running one should surface as
   `unavailable`, not `mismatch`; a mismatch means the versions agree and the
   arithmetic does not.
4. Check the blast radius: one record, one tenant, or one deploy window.
   ```sql
   SELECT date_trunc('hour', completed_at) AS hour, count(*)
     FROM ioe.integrity_check WHERE status = 'mismatch'
    GROUP BY 1 ORDER BY 1 DESC LIMIT 24;
   ```
5. If the count is climbing, **pause the schedule** (§10) so the estate is not
   churned while you investigate, and escalate per §13.
6. Users continue to see the preserved original result, labelled
   `non_reproducible`. That labelling is deliberate — do not suppress it.

## 9. Stuck and abandoned claims

A worker that dies mid-replay leaves a `running` row holding the active-check
index, which blocks further verification of that record.

```sql
SELECT id, entity_type, claimed_by, claim_expires_at, started_at
  FROM ioe.integrity_check
 WHERE status = 'running' AND claim_expires_at < now()
 ORDER BY claim_expires_at;
```

Recovery is automatic: every cycle calls `ioe.recover_stale_integrity_checks()`
first, which transitions expired claims to `failed / REPLAY_EXECUTION_FAILED` —
an honest record of an abandoned attempt, not a rewritten one. To force it now,
run any manual invocation (§4); the recovery runs before the batch.

If rows persist as `running` with a **future** `claim_expires_at`, a worker is
alive and still working. Wait for the TTL rather than intervening.

## 10. Pausing, and reducing batch size during an incident

```
# stop scheduled verification entirely (no deploy needed)
ONYX_IOE_INTEGRITY_VERIFICATION_ENABLED=false     # restart the worker

# or slow it down
ONYX_IOE_INTEGRITY_BATCH_SIZE=2
ONYX_IOE_INTEGRITY_INTERVAL_MINUTES=60            # restart beat
```

Pausing is safe: nothing depends on verification running, no queue backs up
(the schedule simply does not enqueue), and no state is left inconsistent — an
in-flight record is released by the stale-claim sweep on resume.

Stopping the `ioe_integrity` worker has the same effect and needs no config
change; messages accumulate on that queue only, and the task is idempotent, so
draining them later is harmless.

## 11. Safe retry and overlap

Cycles **can** overlap — a slow database, a widened batch, a manual invocation
alongside the schedule, or a queue backlog can all put two in flight. Healthy
timing makes that unlikely; it does not make it impossible, and nothing in the
design relies on it being impossible. Correctness under overlap comes from
record-level arbitration:

The task is safe to re-run at any time:

- Two workers may select the same record, but only one can insert the active
  check — the loser records `skipped_active` and moves on.
- A completed check is never rewritten. `_complete()` returns the existing
  outcome if the row is no longer `running`.
- Verification history is append-only, so a repeated cycle adds attempts, never
  contradicts one.

Celery retries twice with exponential backoff, then fails terminally with
`RETRIES_EXHAUSTED` rather than hammering a broken dependency.

## 12. Privacy restrictions

Nothing in this path may log a financial value, a decoded snapshot, a document,
or a SIN-like value.

- Batch logs are **counts and enumerated reason codes only**.
- Per-record logs carry entity type, entity id, tenant id, reason code, verifier
  version and duration. Hashes appear **only** on the mismatch log line, which is
  restricted diagnostic metadata.
- The Celery return value is counts only — it is stored in Redis.
- The privileged claim function is physically incapable of returning a financial
  column; it selects three identifier columns.

If you need the hashes, read `ioe.integrity_check` directly under a tenant
context. Do not copy them into a ticket.

## 13. Escalation — the missing alerting backend

**There is no alert.** The signal exists and nothing consumes it:

| Signal | Emitted where | Shape |
|---|---|---|
| `alert.integrity_mismatch` | `app/services/ioe/replay/events.py` | `log.error` with `correlation_id`, `entity_type`, `reason_code` — no hashes |
| `integrity_verification_mismatch` | `IntegrityMetrics` counter | monotonic count |
| `integrity_batch_mismatch` | per-execution log line | count per batch |

**Acceptance criteria for closing the alerting gap** (§4 of the closure plan):

1. A log or metric pipeline that ingests `alert.integrity_mismatch`.
2. A high-severity rule on `integrity_verification_mismatch > 0` that pages,
   explicitly **excluding** `LEGACY_EXECUTION_POLICY_UNVERIFIABLE` and
   `integrity_batch_unavailable_dependency`, which must not page.
3. A named on-call rotation to receive it.
4. A test or configuration check proving the rule fires on a synthetic mismatch.

Until all four exist, this runbook's §7 queries are the detection mechanism, and
somebody must run them. State that plainly in any status report:
**`log.error(...)` on its own is not an alert.**

## 14. Related

- `docs/architecture/ioe-production-replay-integrity.md` — the verifier design
- `docs/architecture/ioe-frozen-scenario-baseline-integrity.md` §6b — state semantics
- `docs/architecture/release-blocker-closure-plan.md` — entry 8 status
