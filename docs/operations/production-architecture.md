# Onyx production architecture

**Status: DESIGN, now expressed as code. Nothing is provisioned.** The
infrastructure entry turned this document into Terraform under `infra/` —
validated and security-scanned, never applied, because no AWS account was
reachable from it. Every component below is still a decision and a rationale
rather than a running system; the difference is that the decisions now have a
file each. `infra/README.md` records where this document and the code disagree
and why.

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

**~~One exception to "one cloud": the static frontend stays on Netlify.~~
SUPERSEDED by the infrastructure entry.** The original reasoning — it works, it
is hardened, rebuilding buys nothing visible — was sound while the API had no
edge in front of it. It stops being sound once there is one, for a reason that
is measurable rather than aesthetic: a Netlify proxy reaches the API over the
public internet, so the load balancer has to accept connections from anywhere,
and anything that finds its hostname has bypassed the security headers, the WAF
and the rate rules entirely.

With CloudFront as the only origin, the load balancer admits only requests
carrying a secret header that Terraform generates and no human reads.
Same-origin `/api/*` routing is preserved, so the CORS posture does not widen.

**Amended by the lean-launch entry:** that admission was originally enforced by
a regional WAF web ACL whose default action was BLOCK. It is now the load
balancer's own listener — default action a 403 fixed response, one forwarding
rule conditioned on the header. The property is identical and is asserted by
`test_the_load_balancer_refuses_requests_without_the_origin_header`; what went
away is $6.00/month of duplicate enforcement. The CloudFront web ACL is
unchanged.

`netlify.toml` remains in the repository as the statement of the header policy,
and `tests/security/test_deployment_surface.py` fails if the CloudFront response
headers policy drifts from it.

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

**Corrected during the infrastructure entry.** An earlier version of this table
named `onyx_privacy_runtime`, `onyx_freshness_runtime` and `kb_publisher`. Those
roles do not exist. The schema's roles are below, and every one of them is
`NOLOGIN` — they are GROUPS, and nothing can connect as one. Production creates
LOGIN users as members, exactly as `scripts/run_backend_tests.sh` does for the
test topology. Provisioning from the old table would have produced a set of
roles no application could authenticate as, and the mistake would not have
surfaced until the first connection attempt.

| group role (NOLOGIN) | login user | used by | holds |
|---|---|---|---|
| `onyx_migrator` | `onyx_migrator` (LOGIN, `rds_superuser`) | migration task only | DDL, and schema ownership |
| `onyx_app_rw` | `onyx_api` | API and general workers | DML under RLS |
| `onyx_privacy_worker` | `onyx_privacy` | privacy/deletion worker | its own capability, and NOT `onyx_app_rw` |
| `onyx_freshness_worker` | `onyx_freshness` | freshness relay and integrity verifier | its own capability, and NOT `onyx_app_rw` |
| `onyx_app_ro` | `onyx_reporting` | nothing yet | read only |
| `onyx_kb_admin` | — | knowledge authoring | publication |
| `onyx_audit_writer` | — | audit triggers | INSERT into `audit.*` |

`infra/modules/database/bootstrap.sql` and `bootstrap_runtime_logins.sql` are
that ordering, in two passes: the migrator and the extensions first, the schema
second (as the migrator), the runtime logins third — because the group roles do
not exist until the schema has been applied.

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

**~~Order-of-magnitude estimates.~~ SUPERSEDED by
`docs/operations/cost-model.md`,** which replaces the guesses below with rates
pulled from the AWS Price List Query API for `ca-central-1` and sizes taken from
measuring the real API and the real workers rather than from intuition.

The table that stood here estimated closed beta at ≈$165/month and early
production at ≈$495/month. Both were in the right order of magnitude and both
were wrong in the same direction on the same line: they assumed workers could be
sized by eye. Measured, a Celery worker at concurrency 2 peaks at 507.7 MB —
99% of a 512 MiB limit — running the cheapest scheduled task in the product.

What the modelled figures are now, at the same rates for both profiles:

| | $/month |
|---|---:|
| `lean_launch` production, standing | 178.89 |
| `lean_launch` production at 100 users | 187.06 |
| `high_availability` production at 100 users | 733.38 |
| ephemeral staging, per 2-day proving run | 11.76 |

**The scaling drivers, in order:** Fargate across the five services (32% of the
lean estate), then the NAT gateway (20%), then the database (16%). Multi-AZ on
the database is the single largest step available — `db.t4g.small` single-AZ to
`db.m7g.large` Multi-AZ is $25.55 to $270.98 — which is why the move between
profiles is one deliberate line and is governed by measured triggers rather than
by a date. The AI provider is usage-priced and explanations are generated only
when a customer asks; the deterministic renderer answers when the model is
unavailable, so an outage costs nothing and a budget cap degrades rather than
breaks.


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
