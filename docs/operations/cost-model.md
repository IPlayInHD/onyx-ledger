# Onyx Ledger — cost model, capacity profiles and scale-up triggers

**Status: MODELLED, NOT OBSERVED.** No Onyx infrastructure has ever been
applied to an AWS account. Every figure below is arithmetic over published
rates, and every sizing decision is arithmetic over a measurement taken on a
development machine. The first real bill will disagree with this document; when
it does, the bill is right.

**Rates.** All prices are `ca-central-1`, retrieved from the AWS Price List
Query API (`pricing.us-east-1.amazonaws.com`) on **2026-08-24**. The Route 53
hosted-zone charge is a global one and comes from the same API's global offer
file. Nothing here is a remembered number.

---

## 1. What was measured, and what it refuted

The entry that produced this document required that sizes be measured rather
than guessed. They were, against the real API and the real Celery workers, with
a representative workload.

| Component | Peak RSS | Peak vCPU | Peak PostgreSQL backends | Workload |
|---|---:|---:|---:|---|
| API (uvicorn, 2 workers) | 329.6 MB | 1.90 | 23 | 4 000 authenticated requests at 534 rps, every response a 200, including an idempotent write |
| worker-app, concurrency 4 | 843.2 MB | — | 97 | 300 real scheduled tasks |
| worker-app, concurrency 2 | 507.7 MB | 1.50 | 97 | 400 real scheduled tasks |
| worker-app, concurrency 1 | 316.5 MB | 0.82 | 63 | 400 real scheduled tasks |
| beat | 98.6 MB | 0.00 | 0 | idle scheduler |

**The hypothesis that workers fit in 0.5 GB is refuted.** At concurrency 2 a
worker peaked at 507.7 MB — 99% of a 512 MiB Fargate limit — while running
`purge_admission_history`, which is the *cheapest* task in the schedule and
completes in 47 ms. Fargate enforces a memory limit by killing the task. A
privacy worker killed part-way through a hard erasure is not a saving, so every
worker gets 1 GiB and only its vCPU allocation differs by role.

**The API hypothesis (0.5 vCPU / 1 GiB) is confirmed.** 329.6 MB is 32% of
1 GiB. Scaling the measured throughput linearly, 0.5 vCPU serves roughly 140
requests per second — two orders of magnitude above launch volume.

**A finding that changed the database decision.** One worker held 97 backends
while draining a burst. That is not pool sizing, it is churn:
`backend/workers/runtime.py` disposes the SQLAlchemy engines after **every**
task invocation, deliberately, because a pooled connection outliving its event
loop is a documented defect. Capping the pool changed the number not at all —
measured, with `ONYX_DB_POOL_SIZE=4`: still 97. What the cap does control is the
arithmetic *ceiling*, and at the application defaults that ceiling is 423
backends against a `db.t4g.small`'s 225. `infra/modules/capacity` now computes
it and refuses to plan when it exceeds the class limit.

| Profile | Ceiling | Class allows (85% of max) |
|---|---:|---:|
| `lean_launch` | 177 | 191 (`db.t4g.small`, 225 max) |
| `high_availability` | 703 | 765 (`db.m7g.large`, 901 max) |

**A defect observed while measuring. FIXED SEPARATELY — see below.** Firing
three `purge_admission_history` tasks back to back, the first succeeded and the
second raised `RuntimeError: got Future attached to a different loop`.

The diagnosis written here first blamed a failing disposal. It was simpler and
worse than that: **the task never called `run_task` at all.** It ran
`asyncio.run(_drain())` directly, so no engine was ever disposed and the pooled
asyncpg connection outlived the loop that opened it. Three other entry points
did the same — `analysis.run_analysis`, `ioe.run_optimization` and the shared
TKMS bridge covering six stages. All four now route through `run_task`, and
`tests/security/test_worker_runtime.py` asserts the structural rule that would
have caught them: `asyncio.run` appears exactly once in the worker tree.

The measurements in this section are unaffected. Memory, CPU and backend counts
were the same whether a task raised on its way out or not, and the connection
churn behind the 97-backend figure came from `run_task`'s per-invocation
disposal in the workers that already used it.

---

## 2. Rate card (ca-central-1, 2026-08-24)

| Item | Rate | Per month (730 h) |
|---|---|---:|
| NAT gateway | $0.05/hr + $0.05/GB | $36.50 |
| Application Load Balancer | $0.02475/hr + $0.0088/LCU-hr | $18.07 |
| Public IPv4, in use | $0.005/hr | $3.65 each |
| Interface VPC endpoint | $0.011/hr/AZ + $0.01/GB | $8.03 each, per AZ |
| Fargate, x86 | $0.04456/vCPU-hr, $0.004865/GB-hr | — |
| Fargate, ARM (Graviton) | $0.03565/vCPU-hr, $0.00389/GB-hr | — |
| RDS PostgreSQL `db.t4g.micro` | $0.018/hr single-AZ | $13.14 |
| RDS PostgreSQL `db.t4g.small` | $0.035/hr single-AZ, $0.070 Multi-AZ | $25.55 / $51.10 |
| RDS PostgreSQL `db.t4g.medium` | $0.070/hr single-AZ | $51.10 |
| RDS PostgreSQL `db.m7g.large` | $0.1856/hr single-AZ, $0.3712 Multi-AZ | $135.49 / $270.98 |
| RDS gp3 storage | $0.127/GB-mo single-AZ, $0.254 Multi-AZ | — |
| RDS backup beyond the free allowance | $0.105/GB-mo | — |
| ElastiCache Valkey `cache.t4g.micro` | $0.0144/hr | $10.51 |
| ElastiCache Valkey `cache.t4g.small` | $0.028/hr | $20.44 |
| ElastiCache Serverless, Valkey | $0.092/GB-hr stored + $0.0025 per million ECPU | ≥ $6.72 |
| AWS WAF | $5.00/web ACL, $1.00/rule, $0.60/million requests | — |
| KMS customer-managed key | $1.00/key | $1.00 |
| CloudWatch Logs | $0.55/GB ingested, $0.033/GB-mo stored | — |
| CloudWatch alarm | $0.10 | $0.10 |
| Secrets Manager | $0.40/secret + $0.05 per 10 000 calls | $0.40 |
| S3 Standard | $0.025/GB-mo, PUT $0.0055/1 000, GET $0.0044/10 000 | — |
| Route 53 hosted zone | $0.50 (first 25) | $0.50 |

All Onyx task definitions already specify `cpu_architecture = "ARM64"` and the
deploy workflow builds `--platform linux/arm64`; the pinned base-image digest
`sha256:d29f48a3…` is an OCI image index that includes `linux/arm64`, so the ARM
rates above are the ones that apply. Graviton is a 20% saving on the largest
compute line and was already taken.

---

## 3. Lean production — the standing estate

| Component | $/month |
|---|---:|
| Fargate — API 0.5 vCPU/1 GiB | 15.85 |
| Fargate — worker-app 0.5 vCPU/1 GiB | 15.85 |
| Fargate — worker-freshness 0.25 vCPU/1 GiB | 9.35 |
| Fargate — worker-privacy 0.25 vCPU/1 GiB | 9.35 |
| Fargate — beat 0.25 vCPU/0.5 GiB | 7.93 |
| NAT gateway ×1 | 36.50 |
| RDS `db.t4g.small`, single-AZ | 25.55 |
| RDS gp3 storage, 20 GB | 2.54 |
| Application Load Balancer | 18.07 |
| Public IPv4 ×2 (the load balancer's) | 7.30 |
| ElastiCache Valkey `cache.t4g.micro` ×1 | 10.51 |
| WAF — one CloudFront ACL and four rules | 9.00 |
| KMS customer-managed keys ×5 | 5.00 |
| Secrets Manager ×13 | 5.20 |
| CloudWatch alarms ×9 | 0.90 |
| **Standing total** | **178.89** |

Plus usage. The per-user assumptions are stated so they can be argued with:
**500 API requests, 40 MB of documents added, 300 MB of CloudFront egress and
30 MB of application logs, per active user per month**, over a baseline of
20 000 requests, 1 GB stored, 2 GB egress, 1 GB of logs and 10 GB through NAT.

| Users | Variable | **Total** |
|---:|---:|---:|
| 0 | 2.46 | **181.35** |
| 10 | 3.03 | **181.92** |
| 100 | 8.17 | **187.06** |
| 1 000 | 61.55 | **240.44** |

**Against the $180/month target this is 0.7% over at zero users and 4% over at
one hundred.** The gap is structural rather than a tuning miss: NAT ($36.50),
the load balancer ($18.07), the database ($25.55) and the cache ($10.51) come to
$90.63 of managed-service floor before a single container runs, and none of the
four can be removed without giving up a property on the non-negotiable list —
private tasks, health-checked rolling deployment, an encrypted private database,
or a Celery broker.

**Uncertainty.** The standing lines are firm: they are hourly rates times hours,
and they will be within a few percent. The variable lines rest on the per-user
assumptions above and could plausibly be **half or triple** what is shown. At
1 000 users the honest range is **$200–$330/month**, driven almost entirely by
CloudFront egress and log ingestion. A single figure there would be false
precision.

### Top five cost drivers, lean, at launch volume

1. **Fargate, all five tasks — $58.32 (32%).** Already ARM, already at the
   measured minimum that does not risk an OOM kill.
2. **NAT gateway — $36.50 (20%).** See §6: replacing it with interface
   endpoints costs more.
3. **RDS — $28.09 (16%).** Single-AZ `db.t4g.small` with 20 GB.
4. **Load balancer, including its two public IPv4 addresses — $25.37 (14%).**
5. **ElastiCache — $10.51 (6%).** The smallest Valkey node that exists.

---

## 4. Ephemeral staging

Staging is created for a proving run and destroyed at the end of it
(`infra/staging_cycle.sh`). Every line in §3 is billed hourly or prorated
hourly, so the cost of a run is the standing rate — $0.2451/hour — times the
hours it stands, plus a few cents of traffic.

### The five verbs

| Command | What it does | Leaves it standing? |
|---|---|---|
| `staging_cycle.sh` (or `cycle`) | up → certify → down, destroy **trapped** on exit | No — cannot |
| `staging_cycle.sh up` | provision and stop | **Yes** |
| `staging_cycle.sh certify` | steps 2–9 against a standing estate | **Yes** |
| `staging_cycle.sh down` | destroy; idempotent | No |
| `staging_cycle.sh rebuild` | down, then up | **Yes** |

`cycle` is the default because it is the only verb that cannot leave money
running: its destroy is a trap, so it fires on failure and on interrupt too.
`up` and `certify` deliberately do not destroy — a failed proof is exactly when
you want the estate still there to look at — and both print the `down` command
on the way out. Anything automated should call `cycle`.

**`down` initialises before it reads state, and that is not a detail.**
`terraform state list` fails in an uninitialised directory, and an
ephemeral environment is recreated from a fresh checkout by definition, so the
uninitialised directory is the normal case. Without the init, `down` reads
"nothing is standing" whatever is actually running, exits 0, and leaves the
entire estate billing. `test_every_staging_verb_reads_state_only_after_
initialising` pins it, and its non-vacuity case removes the init and requires
the guard to notice.

| Staging lifetime | $/run |
|---|---:|
| 2 days | 11.76 |
| 5 days | 29.41 |
| 10 days | 58.81 |
| 24/7 for a month | 178.89 |

**The ≤ $100/month target for a 24/7 staging environment is NOT met, and cannot
be met by this architecture.** A staging environment that is worth having is one
shaped like production — same worker split, same private subnets, same encrypted
database, same edge — and that shape has a $179 floor. What meets the target,
comfortably, is not running it 24/7: a proving run of up to ten days a month
costs **$12–$59**, which is 67–93% below the target and 70–94% below what the
same estate cost standing idle before this entry.

The cost of the previous, always-on staging configuration, priced at the same
rates for comparison, was **$199.10/month** — it ran four workers at 0.5 vCPU
each rather than the measured split, and carried a second WAF web ACL.

---

## 5. Lean against high availability

| | `lean_launch` | `high_availability` |
|---|---:|---:|
| Standing total | 178.89 | 725.21 |
| At 100 users | 187.06 | 733.38 |
| Database | `db.t4g.small`, single-AZ | `db.m7g.large`, Multi-AZ |
| NAT gateways | 1 | 2 (one per AZ) |
| Cache | 1 × `cache.t4g.micro` | 2 × `cache.t4g.small` |
| API tasks | 1, autoscaling to 2 | 2, autoscaling to 6 |
| Each worker | 1 task | 2 tasks |
| Beat | exactly 1 | exactly 1 |
| Backup retention | 7 days | 35 days |
| Log retention | 30 days | 90 days |

**The delta is $546.32/month — lean costs 75% less.** Nothing else differs.
Both profiles run the same six workloads under the same six database identities
behind the same private network with the same encryption, the same forced RLS,
the same fail-closed production validators and the same edge protections.
`backend/tests/security/test_capacity_profiles.py` asserts that in both.

### What lean actually gives up

Redundancy, and only redundancy:

* **A single-AZ database.** An availability-zone failure takes Onyx offline
  until AWS restores the zone or the instance is restored from a snapshot into
  another. Durability is unchanged — automated backups and PITR are on, at 7
  days rather than 35.
* **One NAT gateway.** If its zone fails, tasks in the other zone lose outbound
  internet. Inbound serving continues.
* **One cache node.** A node failure loses the queue contents. Celery's
  `acks_late` means in-flight tasks are re-queued rather than lost, but a
  backlog would be.
* **One task per service.** A deployment or a task replacement is a brief
  interruption of background work; the API keeps `deployment_minimum_healthy_
  percent = 100` and so rolls without a gap, but it cannot survive an AZ loss.

Every one of these is an **availability** trade, taken deliberately for a
pre-revenue product with no customers to disappoint. None of them is a
confidentiality, integrity, durability or correctness trade.

---

## 6. Decisions taken, with the evidence

### The cache is a node, not Serverless — and not for price

ElastiCache Serverless for Valkey has the **lower floor**: $0.092/GB-hr against
a 0.1 GB minimum is $6.72/month, against $10.51 for a `cache.t4g.micro`. It is
also multi-AZ by default. On price and availability it wins.

**It cannot run Onyx.** ElastiCache Serverless always operates in cluster mode,
where a multi-key command whose keys hash to different slots is refused with
`CROSSSLOT`. Celery's Redis transport issues exactly that. Measured, against a
local Redis with `MONITOR` capturing the wire protocol and Onyx's real queue
list, `worker-app` emits **one BRPOP across 44 keys, once per second** — eleven
queues times four priority steps. Computing the Redis Cluster slot (CRC16 mod
16384) for those keys gives **44 distinct slots**. It is not a worker-app
problem either: `worker-freshness` spans 8 slots and even `worker-privacy`, one
queue at concurrency 1, spans 4.

So the node-based cluster stays, cluster mode disabled, TLS and AUTH unchanged.
This is a functionality decision that happens to cost $3.79/month more, not a
cost decision.

### CloudFront stays on pay-as-you-go, with a public origin

**Flat-rate plans.** CloudFront's flat-rate tiers are Free ($0), Pro ($15),
Business ($200) and Premium ($1 000) per month. Onyx's distribution needs custom
cache policies (`Managed-CachingDisabled` on `/api/*`), a custom response-headers
policy (CSP, HSTS and the rest, asserted by
`test_every_netlify_security_header_survives_at_cloudfront`) and a custom
origin-request policy (`Managed-AllViewerExceptHostHeader`, which is what
forwards `Authorization`). **All three are Business-tier features.** $200/month
is more than the entire lean production estate. Pay-as-you-go costs about $2–26
a month across the modelled range. Free and Pro would require dropping controls
that are on the non-negotiable list.

**VPC origins.** Researched rather than assumed, and rejected:

* ca-central-1 **does** support VPC origins (every AZ except `cac1-az3`). The
  comment previously in `modules/compute/main.tf` claiming they require VPC
  Lattice was wrong and has been corrected.
* The Terraform provider **cannot manage a change** to a VPC origin attached to
  a distribution: AWS returns HTTP 409 and the documented workaround is to
  destroy and recreate the distribution
  (`hashicorp/terraform-provider-aws` issue #40905, open). That is not
  production-ready lifecycle management for the resource that fronts the whole
  product.
* With a VPC origin, CloudFront addresses the load balancer by its AWS-issued
  `*.elb.amazonaws.com` name, and ACM will not issue a publicly-trusted
  certificate for a domain nobody controls — so keeping `https-only` on that hop
  is not straightforward, and `http-only` would give up TLS.

The saving foregone is the two public IPv4 addresses, **$7.30/month**.

### One NAT gateway, and no paid VPC endpoints

Tasks need outbound access to ECR, Secrets Manager, CloudWatch Logs, KMS and
SES. S3 is already free through the gateway endpoint. Replacing the NAT with
interface endpoints would need five, at $0.011/hr per endpoint per AZ:

* five endpoints in **one** AZ: $40.15/month — more than one NAT at $36.50;
* five endpoints across **both** AZs, which is what the tasks actually require:
  $80.30/month — more than *two* NATs.

Plus $0.01/GB of endpoint data processing against NAT's $0.05/GB, which at the
modelled 10–60 GB/month recovers $0.40–$2.40 and does not close a $44 gap. §5 of
the entry says not to add paid endpoints that cost more than the NAT traffic
they replace; these do, so they are not added.

### The regional WAF web ACL is gone; the control is not

The load balancer is public, and what stopped anyone else using it was a
REGIONAL WAF web ACL with default action BLOCK and a single rule admitting the
secret header CloudFront sends. That is $5.00 for the ACL plus $1.00 for the
rule, every month, for one string comparison.

The listener now does it: **default action is a 403 fixed response**, and one
rule forwards to the target group only when `X-Onyx-Origin-Verify` matches. Same
requests refused, at the same point — before a target group is chosen — for
nothing. The CloudFront web ACL is untouched: all four rules, including the
volumetric bound, still sit in front of every public request.
`test_the_load_balancer_refuses_requests_without_the_origin_header` asserts the
replacement directly and is proved non-vacuous by breaking it.

What was given up: the WAF's own `BlockedRequests` metric. What replaces it:
`HTTPCode_ELB_4XX_Count` and the ALB access logs, both already enabled.

### Secrets stay in Secrets Manager; three empty reservations do not

Fourteen of the sixteen secrets hold real credentials — database connection
strings, the database passwords behind them, the JWT signing key, the admission
identity key and the Valkey URL. **None of them moves.** §9's rule is explicit
and it is the right rule: a credential in an SSM standard parameter to save
$0.40 a month is a worse posture for a rounding error.

Three of the sixteen held nothing: `ai-provider`, `payments` and
`error-reporting` were created empty so the plumbing would exist when a provider
is chosen. In practice no task role grants access to them and no container is
given them, so what they reserved was a *name*. They are now off by default
(`reserve_unwired_secret_names`), saving $1.20/month per environment, and
creating them the day a provider is chosen is one apply.

### Fargate Spot is not used anywhere

Spot capacity is 70% cheaper and would save roughly $40/month across the five
tasks. It is not taken, and the reason is that the entry's condition — *proven*
interruption-safe — has not been met for any Onyx workload, and "probably fine"
is not proof.

Taking each in turn against the rule that Spot must never serve requests, run a
sole scheduler, run a migration, or run privacy deletion:

* **api** — request-serving. Excluded by the rule.
* **beat** — the sole scheduler, exactly one task by construction. Excluded.
* **migration** — excluded.
* **worker-privacy** — runs account deletion. Excluded.
* **worker-freshness** — runs `verify_sealed_integrity`, which replays sealed
  calculations and arbitrates at record level through a partial unique index.
  Interruption is *plausibly* safe here because `task_acks_late` returns the
  message to the queue. Plausibly is not proven, and nothing in the test suite
  currently exercises a mid-replay kill.
* **worker-app** — the same argument, over the queues a customer waits on.

So the honest position is that Spot is available for `worker-app` and
`worker-freshness` **after** someone writes the test that kills a worker
mid-task and asserts the work completes exactly once. Until that test exists,
the saving is not banked and the capacity profiles specify no capacity provider
strategy at all, which leaves both services on `FARGATE`.

### Logging is not reduced

$0.55/GB is real money at volume, and the Infrequent Access log class would
roughly halve it. It is not taken, because IA log groups do not support metric
filters and the estate uses metric filters to raise the security alarms. Log
retention moves from 90 days to 30 in the lean profile — a retention choice, not
a coverage one. Everything §10 requires to be logged is still logged, and
everything it forbids — tax documents, credentials, reset tokens, financial
payloads — is still absent.

---

## 7. Scale-up triggers

**Measured conditions, not dates.** Each names the metric, the threshold, the
duration and the action. None of them fires on a calendar.

### Move the database up (`db.t4g.small` → `db.t4g.medium`, $25.55 → $51.10)

| Metric | Threshold | For | Action |
|---|---|---|---|
| `DatabaseConnections` | p95 > 115 (60% of the 191 the profile allows) | 3 consecutive days | Raise the class, or lower a pool cap and re-derive the ceiling |
| `CPUUtilization` | p95 > 70% | 3 consecutive days | Raise the class |
| `CPUCreditBalance` | < 30% of the class's earned maximum | any 24 h | Burst credits are the t4g family's limit; raise the class |
| `FreeableMemory` | < 15% of class memory | 1 h, twice in a week | Raise the class |
| `FreeStorageSpace` | < 25% of allocated | any | Storage autoscaling handles it to 100 GB; beyond that, raise `db_max_allocated_storage` deliberately |

### Raise API capacity

| Metric | Threshold | For | Action |
|---|---|---|---|
| ECS `CPUUtilization` (api) | > 65% with `DesiredCount` pinned at `api_max_count` | 1 h, on 3 days in a week | Raise `api_max_count` toward `api_absolute_max_count`, then `api_cpu` |
| ALB `TargetResponseTime` | p95 > 1.5 s | 3 consecutive days | Same |
| ALB `HTTPCode_ELB_5XX_Count` | > 0 attributable to capacity | any | Investigate first; capacity second |

### Raise worker capacity

| Metric | Threshold | For | Action |
|---|---|---|---|
| ECS `MemoryUtilization` (any worker) | > 80% | 15 min | Raise that worker's memory. This is the OOM-kill precursor |
| Valkey list length for `analysis` | > 50 | 15 min | Raise `worker_app_concurrency`, then `worker_app_count` |
| Valkey list length for `privacy` | > 0 | 30 min | The erasure SLA is at risk. Investigate before scaling — a privacy backlog usually means a failure, not a shortage |
| `ioe_integrity` completion | a scheduled cycle overlaps the next | twice | Raise `worker_freshness_cpu` |

### Raise the cache

| Metric | Threshold | For | Action |
|---|---|---|---|
| `DatabaseMemoryUsagePercentage` | > 60% | 1 h | `cache.t4g.micro` → `cache.t4g.small` (+$9.93) |
| `EngineCPUUtilization` | > 60% | 1 h | Same |
| `Evictions` | > 0 | any | Same, urgently: an evicted Celery message is lost work |

### Move to `high_availability` (+$546/month)

Any **one** of these, and it is a business decision rather than a metric alone:

* A customer-visible outage caused by the loss of a single availability zone.
* Monthly recurring revenue exceeds **ten times** the $546 delta — at which
  point the redundancy costs under a tenth of the revenue it protects.
* A contractual or regulatory availability commitment is made to anyone.
* Stored customer tax data passes a volume where a restore-from-snapshot would
  take longer than the recovery-time objective being promised.

The move is one line: `capacity_profile = "high_availability"` in the
environment root. Nothing else changes, and
`test_the_high_availability_profile_is_preserved_and_complete` fails if that
stops being true.

### Reconsider the NAT decision

| Metric | Threshold | Action |
|---|---|---|
| NAT `BytesOutToDestination` | > 400 GB/month | At $0.05/GB that is $20/month of data processing; re-run the endpoint arithmetic in §6, which may have flipped |

---

## 8. AWS credits

**Credits are not part of any figure above, and no plan depends on one.**

Programmes that Onyx would plausibly qualify for — AWS Activate for startups,
and the credits sometimes attached to an AWS Partner or accelerator
relationship — are typically $1 000 to $5 000 over one or two years, awarded at
AWS's discretion, often conditional on an accelerator or investor affiliation,
and always expiring.

Applied to the modelled lean estate at $181/month, $1 000 of credit is about
five and a half months and $5 000 is about twenty-three. That is a runway
extension. It is not a business model, and it is not a reason to run an estate
that would be unaffordable without it. **The lean profile is designed to be paid
for out of pocket indefinitely.** If a credit arrives, it buys time; if none
does, nothing about the plan changes.

Anything sold on the basis that credits will cover infrastructure should be
treated as a claim to verify, not a number to model.

---

## 9. What this document does not establish

* That an apply succeeds. Nothing here has been applied.
* That the measured sizes hold under production workloads. They were measured
  on one machine, against a development database, driving the scheduled
  maintenance task and the authenticated read/write API paths — not a filing-
  season analysis burst.
* That the per-user assumptions in §3 are right. They are assumptions, labelled
  as such, and the variable lines should be re-derived from the first real bill.
* That $181/month is what will be charged. It is what the rates multiply out to.

---

## 10. What the profiles are actually held to

Five invariants were added after the profiles were built, each because the
property was real but rested on a comment or on nothing at all. All five are in
`backend/tests/security/test_capacity_profiles.py` and all five have a
non-vacuity case that breaks the property on a copy of `infra/` and requires
the guard to fail.

| Guard | What it prevents | Was it asserted before? |
|---|---|---|
| `test_only_the_privacy_worker_can_erase_an_object_version` | The API gaining `s3:DeleteObjectVersion` — the capability that makes a customer's documents unrecoverable | No. `iam.tf` asserted it **in a comment** |
| `test_every_bucket_is_private_versioned_and_encrypted` | A bucket losing public-access blocking, versioning, or KMS | No. The S3 tests cover the client, and read no Terraform |
| `test_the_lean_profile_runs_exactly_one_nat_gateway` | Lean quietly acquiring a NAT per AZ — the second largest standing line, doubling | Only the **HA** side was |
| `test_staging_and_production_cannot_share_terraform_state` | `staging-down` destroying production | No |
| `test_every_staging_verb_reads_state_only_after_initialising` | `down` silently declining to destroy a live estate | No — the defect it pins was introduced and caught in this entry |

The distinction the first one draws is worth keeping straight: the API
legitimately holds `s3:DeleteObject`, because `DELETE /api/v1/documents/{id}`
is a product feature and on a versioned bucket that writes a delete marker and
keeps every prior version — which is the right meaning of "the customer removed
a document". Erasure is the other thing, it needs `DeleteObjectVersion` and
`ListBucketVersions` to enumerate what to destroy, and those two belong to the
privacy worker alone.