# Onyx infrastructure

Terraform for the Onyx Ledger platform. `docs/operations/production-architecture.md`
is the design and the reasoning; this tree is that design expressed as code.

**Nothing here has been applied.** No AWS account was reachable from the entry
that wrote it — the only AWS-shaped credentials in the environment belonged to
the agent proxy (a 14-character access key id beginning `prox`, against a real
key id's 20 characters beginning `AKIA`/`ASIA`). Every module below is
`terraform validate`-clean and `tfsec`-scanned, and none of it has met the AWS
API. Treat a first `plan` as a review step, not a formality.

## Layout

```
bootstrap/            state bucket + lock table. Run once, with local state.
check.sh              fmt + validate + tfsec over every root and module
staging_cycle.sh      the ephemeral staging run, start to destroy
modules/
  capacity/           the two capacity profiles. Creates nothing; decides sizes
  network/            VPC, subnets, NAT, endpoints, security groups
  database/           RDS PostgreSQL, KMS, parameter group, credentials
  cache/              ElastiCache (Valkey), auth token
  storage/            S3 document + legislation buckets
  secrets/            Secrets Manager entries and their KMS key
  compute/            ECS cluster, ALB, API + worker services, migration task
  edge/               CloudFront, WAF, ACM, response headers
  observability/      log groups, alarms, notification topic
  github_oidc/        the deploy roles CI assumes
  cost_guardrails/    budget thresholds and anomaly detection
envs/
  shared/             PERSISTENT: ECR, the OIDC provider, the evidence bucket
  staging/            the staging composition — EPHEMERAL by default
  production/         the production composition
```

**Two tiers, and the split is what makes staging safe to destroy.** `envs/shared`
holds what must outlive an environment: the container registry (so a rebuild
uses the same bytes), the account's single OIDC provider, and the bucket that
keeps what a staging run proved after the estate that proved it is gone. Both
are `prevent_destroy`. Certificates and the hosted zone are persistent too, and
are passed into the environment roots as ARNs rather than created there.

**Every size comes from `modules/capacity`.** An environment root names a
profile — `lean_launch` or `high_availability` — and the module supplies every
instance class, task size, count and pool cap. It also computes the arithmetic
maximum number of PostgreSQL backends the configuration can open and refuses to
plan when that exceeds what the chosen database class allows. See
`docs/operations/cost-model.md` for what was measured and what it refuted.

## Order of operations

`bootstrap` first, once, from a workstation with admin credentials. It creates
the S3 bucket and DynamoDB table the other stacks use for state and locking, and
it is the only stack whose own state is local — a chicken-and-egg that every
Terraform estate has and that is cheaper to accept than to hide.

Then `envs/shared`, once. It creates the registry, the OIDC provider and the
evidence bucket, and neither environment root can be applied without its
outputs.

Then `envs/staging` or `envs/production`, in either order — they no longer
depend on each other, because the OIDC provider they used to fight over now
lives in `shared`. They are separate root modules with separate state files on
purpose: a `terraform apply` typed in the wrong terminal should not be able to
reach production.

Staging is meant to be created and destroyed rather than kept:
`infra/staging_cycle.sh` is the whole run — provision, migrate, verify, journeys,
security, hard erasure, restore check, twelve-persona regression, preserve the
evidence, destroy. Its destroy step is unconditional, because the expensive
failure is not a failed run, it is a run that failed and left the estate up.

## Decisions that differ from the design document

**The frontend moves from Netlify to CloudFront + S3.** The design document
argued for keeping Netlify, on the grounds that it works and rebuilding it buys
nothing a customer can see. That reasoning was sound when the API had no edge in
front of it. It stops being sound here, for one measurable reason: a Netlify
proxy has to reach the API over the public internet, so the load balancer must
accept connections from anywhere, and anything that reaches it directly has
bypassed the security headers, the WAF and the rate rules. With CloudFront in
front, the ALB accepts only CloudFront — enforced by a WAF rule matching a
secret header that Terraform generates and neither human reads.

The header policy itself is not redesigned. `netlify.toml` remains the statement
of what the headers should be; `modules/edge/headers.tf` implements the same set
as a CloudFront response headers policy, and a test asserts the two agree.

**Database identities are group roles, and the design document named them
wrongly.** It listed `onyx_privacy_runtime` and `onyx_freshness_runtime` as if
they were the roles in the schema. The schema's roles are `onyx_privacy_worker`
and `onyx_freshness_worker`, and every one of them — including `onyx_app_rw` —
is `NOLOGIN`. They are groups. Production creates LOGIN users as members, which
is what `scripts/run_backend_tests.sh` does for the test topology and what
`bootstrap.sql` does here. Provisioning from the document as written would
produce roles nothing can log in as.

## Cost

**`docs/operations/cost-model.md` is the cost model.** It carries the rate card
(pulled from the AWS Price List Query API rather than remembered), the measured
sizing evidence, the per-component breakdown for both profiles, the ephemeral
staging figures, the top five drivers and the scale-up triggers. The short
version:

| | $/month |
|---|---:|
| `lean_launch` production, standing | 178.89 |
| `lean_launch` production at 100 users | 187.06 |
| `high_availability` production at 100 users | 733.38 |
| ephemeral staging, 2-day run | 11.76 |
| ephemeral staging, 10-day run | 58.81 |

**A correction to what this file used to say.** An earlier version of this
section claimed that replacing the NAT gateways with interface endpoints for
ECR, Secrets Manager and CloudWatch would be "roughly a wash on price". It is
not. At the real `ca-central-1` rate — $0.011 per endpoint-hour per availability
zone — the five endpoints the tasks need cost $40.15/month in one zone and
$80.30 across the two they actually run in, against $36.50 for one NAT gateway.
The endpoint route is 10% to 120% more expensive, not a wash, and the entry that
found this declined to take it for that reason.
