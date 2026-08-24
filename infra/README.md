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
modules/
  network/            VPC, subnets, NAT, endpoints, security groups
  database/           RDS PostgreSQL, KMS, parameter group, credentials
  cache/              ElastiCache (Valkey), auth token
  storage/            S3 document + legislation buckets
  secrets/            Secrets Manager entries and their KMS key
  compute/            ECR, ECS cluster, ALB, API + worker services, migration task
  edge/               CloudFront, WAF, ACM, response headers
  observability/      log groups, alarms, notification topic
  github_oidc/        OIDC provider and the deploy roles CI assumes
envs/
  staging/            the staging composition
  production/         the production composition
```

## Order of operations

`bootstrap` first, once, from a workstation with admin credentials. It creates
the S3 bucket and DynamoDB table the other stacks use for state and locking, and
it is the only stack whose own state is local — a chicken-and-egg that every
Terraform estate has and that is cheaper to accept than to hide.

Then `envs/staging`, then `envs/production`. They are separate root modules with
separate state files on purpose: a `terraform apply` typed in the wrong terminal
should not be able to reach production.

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

`ca-central-1`, monthly, USD, on-demand. These are computed from the instance
types this code actually selects, not from a general estimate — change a
`node_type` or an `instance_class` and this table is wrong until somebody
updates it. Nothing has been applied, so no figure here has met a bill.

| | staging | closed beta | early production |
|---|---:|---:|---:|
| RDS PostgreSQL | $30 `db.t4g.small`, single-AZ | $60 `db.t4g.small`, Multi-AZ | $190 `db.m7g.large`, Multi-AZ |
| ElastiCache Valkey | $12 `cache.t4g.micro` ×1 | $13 `cache.t4g.micro` ×1 | $50 `cache.t4g.small` ×2 |
| Fargate — API | $18 0.5 vCPU ×1 | $18 0.5 vCPU ×1 | $110 1 vCPU ×2–6 |
| Fargate — workers | $36 4 services, small | $36 | $150 |
| NAT gateway | $34 one | $34 one | $75 one per AZ |
| CloudFront + WAF | $12 mostly WAF's fixed fee | $18 | $45 |
| S3 + KMS + Secrets | $10 | $15 | $40 |
| CloudWatch logs + alarms | $8 14-day retention | $12 | $35 |
| SES | $0 under the free tier | $1 | $10 |
| **Total** | **≈ $160** | **≈ $210** | **≈ $700** |

**The two surprises in that table**, because they are the ones that catch people
out at this size:

*The NAT gateway costs more than the API.* $34/month before a byte moves,
per AZ, and production runs one per AZ for availability. The S3 gateway endpoint
already keeps document traffic off it, which is why it is in `modules/network`
rather than left as a later optimisation. If cost pressure ever gets real,
interface endpoints for ECR, Secrets Manager and CloudWatch would let the NAT
gateways go entirely — roughly a wash on price, better on security, and more
moving parts.

*The WAF's fixed fee dominates its own line.* $5 per web ACL plus $1 per rule
group, before any request. Two ACLs — one at CloudFront, one at the ALB — is
most of that $12. It is worth it: the ALB ACL is what makes the load balancer's
open security group safe.

**Staging costs nearly as much as closed beta**, which looks wrong and is not.
Availability is what production buys, and staging deliberately does not buy it —
but the fixed costs (NAT, WAF, the smallest RDS instance that exists) are the
same either way. The way to make staging cheaper is to run it only when needed;
`terraform destroy` on the staging root is safe by construction, which is why
its deletion protection is off and its secret recovery window is zero.
