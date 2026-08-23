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
payment provider, the AI provider, and error/metric collection. **None of the
four is wired today.** The AI seam in particular returns a deterministic
renderer (`get_llm_client()` → `TemplateLlmClient`) rather than calling a model,
so the product currently makes no outbound provider request of any kind.

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

**Launch blocker, stated plainly — and it is not the one an earlier draft of
this document described.** That draft said the documents pipeline returns 501.
It does not. `POST /api/v1/documents` is fully implemented: it registers the
document, enforces a MIME allow-list and a size ceiling, passes through
admission control, and hands back a presigned upload URL. Process, confirm,
list and delete are implemented too.

What is missing is underneath. `get_object_storage()` returns
`LocalObjectStorage` **unconditionally** — an in-process, in-memory fake whose
presigned URLs are `local://` strings no browser can use. `settings.s3_endpoint_url`
exists and nothing reads it; `boto3` is not in `requirements.lock.txt`; the S3
adapter is a commented-out sketch at the bottom of `app/integrations/storage.py`.

Three subsystems resolve through that factory, and the third is the sharp edge:

| caller | what it stores |
|---|---|
| `document_processing` | customer document bytes |
| `tkms.ingestion` | tax knowledge source bytes |
| `privacy.lifecycle` | **deletes** customer objects |

A deletion phase running against an in-memory fake reports success having
touched nothing. Point that code at a real bucket without writing the adapter
and the privacy pipeline would record erasure that did not happen — which is a
worse failure than an endpoint that refuses.

**Both are now closed.** B2 wrote `S3ObjectStorage` and made the factory refuse
the in-memory store in production; B2A closed PD-10.

**Bucket versioning is SUPPORTED, and stays on.** It was the open question:
`delete_object` on a versioned bucket removes nothing — it writes a delete
marker and every previous version stays readable by anyone who can name one.
B2 refused to call that erasure, which was correct and left the privacy phase
unable to complete against the very bucket configuration this document
recommends.

Erasure is now version-aware. `ObjectStorage.hard_erase` enumerates every
version and delete marker for exactly one key, removes them by version id, and
then re-lists to confirm nothing remains — so "erased" means the bytes are gone,
not hidden. There is one erasure operation rather than a soft and a hard
variant, because both callers mean the same thing and a soft variant would
exist only to be chosen by mistake.

Versioning therefore keeps its durability value for the window before deletion,
and costs nothing at deletion time. Turning it off is **not** required and
should not be used as a way to sidestep erasure.

## 6b. Transactional email

SES v2, one verified sending identity per environment, reached through the
normal AWS credential chain. No static access keys in configuration — the same
rule §7 applies to everything else.

Onyx sends exactly three messages, and the set is closed in code
(`TransactionalEmail`): confirm your address, reset your password, your
password was changed. There is no marketing surface and no generic
"send an email" operation, because the first thing to arrive in one would be
something nobody reviewed against the rule below.

**No customer financial data in any message.** Not minimised — absent. Email
sits unencrypted in somebody else's mailbox, gets forwarded, is indexed by the
provider and quoted into replies, so none of the three carries a figure, a tax
position, a document name, a province or a filing status. The recipient's own
address is the only personal value any of them contains, and it is already in
the envelope.

**Production must not capture its own mail.** `CaptureEmailProvider` appends to
an in-process list and opens no socket. A deployment that selected it would
accept registrations, mint verification tokens, file the messages in a list and
report success — nothing errors, and the only signal is customers who cannot
get in and cannot say why. Two layers refuse it: `Settings` will not construct
in production with `ONYX_EMAIL_PROVIDER=capture`, and `build_email_provider`
refuses it again if a Settings is built some other way.

The same validator requires a sender identity, a region, and an **https**
`ONYX_APP_PUBLIC_URL`. That last one is not cosmetic: verification and reset
links are built from it and from nothing else — never from the `Host` header,
which is attacker-supplied and is the classic reset-poisoning vector — and a
plain-http origin would put a single-use credential in a URL that travels in
cleartext.

**Sending never blocks a request or holds a transaction.** Every send is
scheduled after the response: mint the token, commit, then hand the message to
the provider. That ordering is chosen for three reasons that agree — a failed
send leaves a committed token the customer replaces by asking again (the other
order puts a live link in an inbox for a row that was rolled back); no
multi-second provider call happens with a connection and a row lock held; and
the password-reset request answers in the same measurable time whether or not
the address has an account, which is what keeps its careful wording from being
undone by a stopwatch.

**Not provisioned here.** This describes what the application requires. Buying
the domain, verifying the sending identity, moving out of the SES sandbox,
setting up SPF/DKIM/DMARC and requesting a sending quota are deployment work
and are not done — `LIVE_EMAIL_DELIVERY = NOT_PROVISIONED`. The application is
production-capable and fails closed without the configuration, which is the
part that belongs in the repository.

DKIM and DMARC are worth calling out as more than deliverability hygiene: a
domain that anybody can send as is a domain whose password-reset notices
anybody can forge, and this product's mail is exactly the mail worth forging.

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
