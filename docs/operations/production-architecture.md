# Onyx production architecture

**Status: DESIGN. Nothing here is provisioned.** No cloud account, credentials
or infrastructure-as-code exist in this repository, so every component below is
a decision and a rationale, not a running system. Where this document states a
requirement it is a requirement on whoever provisions it, and the closure report
for this entry lists what remains unproven for exactly that reason.

Launch scope is **Federal + Ontario** (`/api/v1/config/launch-scope`).

---

## 1. The shape of it

```
                    ┌──────────────┐
   customer ───────►│  CDN + WAF   │  static frontend, security headers, CSP
                    └──────┬───────┘
                           │ /api/*
                    ┌──────▼───────┐
                    │  FastAPI     │  ECS Fargate service, no public IP
                    │  (API)       │  autoscaled on request count
                    └──┬────┬──────┘
          private VPC  │    │
        ┌──────────────▼┐  ┌▼──────────────┐
        │ RDS Postgres  │  │ ElastiCache   │
        │ (Multi-AZ)    │  │ Redis         │
        └───────▲───────┘  └───▲───────────┘
                │              │
        ┌───────┴──────────────┴───────┐
        │  Celery workers              │  ECS Fargate service, same image
        │  (analysis, freshness,       │  different command, own DB identity
        │   integrity, documents)      │
        └──────────────┬───────────────┘
                       │
                 ┌─────▼─────┐
                 │    S3     │  private, encrypted, no public listing
                 └───────────┘
```

External services, all reached outbound over TLS: transactional email, the
payment provider, the AI provider, and error/metric collection.

## 2. Cloud and region

**AWS, `ca-central-1` (Montréal).**

Canadian tax data belongs in Canada. This is not a legal conclusion — PIPEDA
does not mandate residency, and the privacy counsel review in §16 of the entry
is where that question is actually settled — but keeping customer records in a
Canadian region removes an argument nobody wants to have, and `ca-central-1`
carries every managed service this design needs.

**No Kubernetes.** Two people cannot operate a cluster and a tax engine. ECS
Fargate runs a container without a node to patch, and it is the smallest thing
that runs both the API and the workers from one image.

**One exception to "one cloud": the static frontend stays on Netlify.** It holds
no customer data, its entire configuration is the forty lines in `netlify.toml`,
and it is already built and hardened. Consolidating onto CloudFront + S3 is a
reasonable later move — one bill, one WAF — and the trade is genuinely close;
what is not close is that spending the launch budget rebuilding a working static
deploy buys nothing a customer can see.

## 3. Environments

Three, and they share **nothing**:

| | development | staging | production |
|---|---|---|---|
| database | local container | own RDS instance | own RDS instance |
| Redis | local container | own ElastiCache | own ElastiCache |
| object storage | local MinIO | own bucket | own bucket |
| JWT + admission secrets | dev defaults | own, generated | own, generated |
| AI / payment / email keys | absent or sandbox | sandbox keys | live keys |
| admin identities | local only | staging only | production only |

**Production customer data never flows downhill.** Staging is seeded with
synthetic personas — the same twelve the browser journeys use. A restore drill
that copies a production snapshot into staging (§27) is the one exception, and
it must be treated as production data for as long as it exists there.

The application already refuses to start in production with a development
secret: `Settings._production_secrets_are_real` rejects the dev default and any
key under 32 characters for both `ONYX_JWT_SECRET` and
`ONYX_ADMISSION_IDENTITY_SECRET`.

## 4. PostgreSQL

RDS PostgreSQL 16 with the `vector` extension, Multi-AZ.

- **Not publicly reachable.** Private subnets, no public IP, security group
  admitting only the API and worker security groups. Nothing on the internet
  can open a socket to it.
- TLS required in transit (`sslmode=verify-full`); encryption at rest with a
  customer-managed KMS key.
- Automated backups with **point-in-time recovery**, 35-day window.
- Deletion protection on, so a console mistake cannot remove it.

**The migrator must be the identity that applies the DDL, and this is not a
style preference.** `identity.subject_key_for` is SECURITY DEFINER owned by
`onyx_migrator`, and `audit.log_change` calls it on every write. Nothing in
`db/sql` grants `onyx_migrator` USAGE on `identity` — the design relies on that
role also *owning* the schema, which holds only when the migrator ran
`CREATE SCHEMA`. Apply the same schema as some other identity (an RDS master
user, a provisioning superuser, a CI service container) and `onyx_migrator` is
created by `00_extensions_roles.sql` as a plain NOLOGIN role that can reach
nothing. The schema still applies cleanly. The first customer registration then
fails inside the audit trigger with `permission denied for schema identity`, and
nothing before it does.

So: create `onyx_migrator` first, with enough privilege to create extensions,
then apply the schema **as** `onyx_migrator`. That ordering is what
`backend/scripts/ci_provision_postgres.sh` does, it is what both quality gates
do, and `tests/security/test_privilege_invariants.py` asserts the resulting
invariant directly — every SECURITY DEFINER owner can reach every application
schema — so a database provisioned the wrong way fails a test rather than a
customer.

**The runtime is not the owner.** Migrations run as the migrator identity from
the deployment pipeline, never from the API container. The application connects
as `onyx_app_rw`, which is not a superuser, not the owner, does not hold
`BYPASSRLS`, and cannot run DDL. That separation is what makes row level
security meaningful rather than advisory.

### Identities

The schema already defines dedicated principals, and production uses them
rather than collapsing to one connection string:

| identity | used by | holds |
|---|---|---|
| `onyx_migrator` | deployment pipeline only | DDL |
| `onyx_app_rw` | API and general workers | DML under RLS |
| `onyx_privacy_runtime` | privacy/deletion worker | its own capability, and NOT `onyx_app_rw` |
| `onyx_freshness_runtime` | freshness relay | its own capability, and NOT `onyx_app_rw` |
| `kb_publisher` | publication only | publication, nothing more |

The privacy and freshness runtimes are deliberately separate LOGIN roles. PD-16
was exactly this boundary being reachable from the application identity: a
`GRANT onyx_freshness_worker TO onyx_app_rw` let any HTTP request-path session
assume the privileged role and invoke cross-tenant keyholes. The remediation
holds only if production actually issues them separate credentials.

**Open, and carried forward:** the audit noted `onyx_app_rw` holds broader CRUD
on some administrative and knowledge schemas than it needs. Narrowing it is a
grant review against the existing dynamic privilege suite, not a redesign — it
is listed in the closure report rather than done here.

## 5. Redis, queues and workers

ElastiCache Redis, private, encrypted in transit and at rest, AUTH token from
the secret manager.

Redis is the Celery broker and result backend. The queues already exist and are
already separated by cost —`analysis`, `documents`, `ingestion`, `notify`,
`maintenance`, the six `tkms_*` stages, `ioe`, and `ioe_freshness` — so a slow
ingestion job cannot block a customer's analysis.

Production settings that are not defaults:

- **Serializer pinned to JSON, explicitly, for task, result and accepted
  content.** Celery's pickle support is remote code execution wearing a
  configuration option.
- Result TTL kept short. PD-12 was Celery serializing failed-task exceptions —
  statement text and bound parameters included — into the result backend.
- Workers run as their own database identities, not as the API's.
- `acks_late` with a visibility timeout above the longest task, so a worker
  dying mid-analysis re-queues rather than losing the work.

## 6. Object storage

S3, one bucket per environment.

Block Public Access on at the account level, SSE-KMS at rest, TLS in transit, no
public listing, versioning on. Object keys are random — never `user_id/filename`,
which turns a key into an enumeration oracle.

**No permanent public URLs.** Access is a short-lived pre-signed URL issued by
the backend after it has checked ownership, so the authorisation decision stays
with the application rather than with whoever holds a link.

**Launch blocker, stated plainly:** document upload is scaffolded, not
finished — the documents pipeline returns 501. Until it is implemented, the
bucket exists for nothing and the privacy policy must not describe a document
store the product does not have.

## 7. Secrets

AWS Secrets Manager, one path per environment, rotation enabled where the
provider supports it.

In the secret manager: JWT signing secret, admission identity secret, database
credentials, Redis AUTH, email provider key, AI provider key, payment secret key
and webhook signing secret, storage credentials, monitoring DSN.

Never: in git, in the frontend bundle, in a `VITE_*` variable, in a CI log, or as
a default in source. The only `VITE_*` variable is `VITE_API_BASE_URL`, a public
origin.

Rotation is documented per secret in `runbooks.md`. **The JWT secret is the one
with a customer-visible cost**: rotating it invalidates every live access token.
It does not touch sealed tax artefacts — those are hashed over canonical content,
not signed with this key — so rotation is a sign-out event, not a data event.

## 8. Edge

CloudFront in front of the API with AWS WAF: managed common rule set, known-bad
inputs, IP reputation, a request body size limit, and rate-based rules as a
coarse outer bound.

**The WAF is not the rate limiter.** Application admission control stays
authoritative for anything user-specific — it is per identity and per operation
class, it understands what an analysis costs, and it is tested. The WAF's job is
volumetric: keeping a flood away from the application at all.

TLS 1.2+ only. HTTP redirects to HTTPS. HSTS is set at the edge, two years,
subdomains included, **preload deliberately not enabled** until the domain is
settled, because preload is very hard to undo.

## 9. Deployment

```
push → quality gates → build image → staging → smoke + journeys → promote
```

The image is tagged with the **exact git SHA** and promoted by digest, so what
runs in production is the artefact staging tested rather than a rebuild that
resolved differently.

Migrations run **before** the new revision starts, as the migrator identity, as
their own step. They must be backward compatible with the revision still
serving: add a column, deploy code that writes it, remove the old one in a later
release. **There is no automatic destructive rollback.** Rolling an application
revision back is routine; rolling a migration back is a decision a person makes
with the runbook open.

## 10. Cost

Order-of-magnitude, `ca-central-1`, monthly, USD. Provisioning will move these.

| | closed beta | early production | 10× early production |
|---|---|---|---|
| RDS PostgreSQL | $60 (t4g.small, Multi-AZ) | $180 (m7g.large, Multi-AZ) | $700 (m7g.2xlarge + replica) |
| ElastiCache Redis | $15 (t4g.micro) | $35 (t4g.small) | $140 (m7g.large) |
| ECS Fargate — API | $25 (1×0.5 vCPU) | $90 (2–3 tasks) | $600 (autoscaled) |
| ECS Fargate — workers | $25 | $70 | $450 |
| S3 + data transfer | $5 | $20 | $150 |
| CloudFront + WAF | $15 | $40 | $200 |
| Secrets, KMS, logs, metrics | $20 | $50 | $200 |
| Email | $1 | $10 | $80 |
| **Infrastructure** | **≈ $165** | **≈ $495** | **≈ $2,500** |
| AI provider | usage | usage | usage |
| Payments | ~2.9% + 30¢ | — | — |

**The scaling drivers, in order:** the database (Multi-AZ doubles it, and a read
replica doubles it again), then worker compute, because analysis and
optimization are CPU-bound and admission control is what stops them from being
unbounded. The AI provider is usage-priced and explanations are generated only
when a customer asks — the deterministic renderer answers when the model is
unavailable, so an outage costs nothing and a budget cap degrades rather than
breaks.

Do not pre-provision for the third column. Multi-AZ from the first paying
customer, a read replica when read latency says so, and nothing else early.

## 11. What this design does not decide

- **The domain.** No hostname is hard-coded anywhere; the frontend takes its
  backend origin from `ONYX_API_ORIGIN` at build time and fails the build if it
  is missing.
- **Retention durations** (PD-3). Deliberately unset: inventing a number for tax
  record retention is a legal question wearing an engineering costume. Flagged
  for privacy counsel in the closure report.
- **Whether a Netlify site was ever connected to this repository** while the
  legacy engine was the deploy target, and whether a Blobs store therefore holds
  real records. Decommissioning the code path does not delete data already
  written — see PD-14.
