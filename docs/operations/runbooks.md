# Runbooks

For the moment when something is wrong and nobody wants to reason from first
principles. Each one says what to check, what to do, and what not to do.

**None of these has been rehearsed against real infrastructure**, because none
exists yet. The restore drill in particular is a claim until somebody performs
it — a backup nobody has restored is a hypothesis.

---

## Deploy

1. Confirm the exact SHA is green in both quality gates.
2. Confirm migrations for this revision are backward compatible with the
   revision currently serving. If they are not, this is a two-release change,
   not a deploy.
3. Migrations first, as the migrator identity, as their own step.
4. Deploy to staging. Run the smoke journey and the twelve personas.
5. Promote **by image digest**, not by rebuilding.
6. Watch 5xx rate and p95 latency for ten minutes.

Do not deploy a rebuilt image to production. The artefact staging tested is the
artefact that ships, or staging tested something else.

## Rollback

Application rollback is routine: redeploy the previous digest. It takes minutes
and needs no discussion.

**Database rollback is not routine and is not automatic.** If the bad release
included a migration:

- If the migration was additive, roll the application back and leave the schema
  alone. An unused column costs nothing.
- If the migration was destructive, stop and read the restore runbook. Do not
  run a down-migration against production to "undo" it — a down-migration that
  drops a column drops the data in it, and the data is the thing you are trying
  to save.

## Database migration

- Run as `onyx_migrator`, never from the API container. On a **new** database
  this is load-bearing rather than tidy: create `onyx_migrator` first, then let
  it apply the schema, so it ends up owning it. A schema applied by any other
  identity leaves the migrator unable to reach `identity`, and every write then
  dies inside the audit trigger — the schema applies clean and the first
  registration fails. `tests/security/test_privilege_invariants.py` asserts the
  invariant; run it against a freshly provisioned database before pointing an
  application at it.
- One migration step, before the new revision starts.
- Long-running index builds use `CREATE INDEX CONCURRENTLY` in a migration of
  their own; inside a transaction it will lock the table and take the site down.
- After it completes, check that the drift gate still passes on the deployed
  SHA.

## Restore

1. Decide the target time. Point-in-time recovery restores to a **new**
   instance; it does not overwrite the live one.
2. Restore into staging first, always, even under pressure. A restore verified
   nowhere is a second outage waiting.
3. Verify: row counts on `user_account`, `analysis_run`, `scenario`; then replay
   a sealed scenario result and confirm the hash still matches. **That last
   check is the one that matters** — it proves the restored data is the same
   data, not merely data of the same shape.
4. Repoint the application only after the verification passes.
5. Record actual RPO and RTO from the drill. Estimates are not measurements.

## Secret rotation

| secret | blast radius |
|---|---|
| `ONYX_JWT_SECRET` | every live access token is invalidated; customers are signed out |
| `ONYX_ADMISSION_IDENTITY_SECRET` | admission counters reset; rate limits briefly restart |
| database credentials | rolling restart while connections re-established |
| Redis AUTH | in-flight tasks retry |
| AI provider key | explanations fall back to the deterministic renderer — no customer-visible failure |
| email provider key | verification and reset mail queues |
| payment webhook secret | **rotate in the provider first**, then here, or events arriving between the two fail signature checks |

Rotating the JWT secret does **not** invalidate sealed tax artefacts: those are
hashed over canonical content, not signed with this key. It is a sign-out event,
not a data event.

## Worker outage

Symptom: queue depth climbing, analyses stuck in `running`.

1. Are the worker tasks running at all? If they crash-looped, the logs say why.
2. Is Redis reachable from the worker security group?
3. Is the database at connection-pool exhaustion? Workers and API share a limit.
4. Scale worker tasks up before investigating further — the backlog drains while
   you read.

Do not clear the queue to make the graph look better. Those are customers'
analyses.

## AI provider outage

**Not reachable yet:** no external provider is wired. `get_llm_client()`
returns the deterministic `TemplateLlmClient` unconditionally, so there is
nothing to have an outage. This runbook applies from the day a provider adapter
ships, and the fallback it describes is already the only path.

**Nothing is required of you.** The explanation layer falls back to the
deterministic renderer on timeout, rate limit, malformed response or validation
failure, and the customer reads a perfectly good explanation. Fallback frequency
is a metric, not an alert.

Escalate only if the fallback rate is high *and* the deterministic renderer is
also failing, which is an application defect rather than a provider one.

## Payment outage

1. Entitlement is stored in the database, not fetched from the provider on each
   request, so an outage does not lock existing subscribers out.
2. Webhooks are idempotent and replayable — the provider retries, and duplicate
   delivery is already handled.
3. Do not grant entitlement by hand to work around a webhook failure. Replay it
   after the outage; a hand-granted entitlement has no event behind it and will
   not reconcile.

## Email outage

Verification and password-reset mail queues rather than failing. Customers who
signed up during the outage will need a resend, so the resend path must work
before this runbook is ever needed.

Do not send passwords by email under any circumstances, including as a
workaround for a broken reset flow.

## Suspected compromise

1. **Preserve first.** Snapshot logs and the database before changing anything.
   Rotating credentials destroys the attacker's session and also the evidence of
   what it did.
2. Rotate the JWT secret — signs everyone out, including whoever should not be
   there.
3. Revoke sessions: `auth_session` rows carry `revoked_at`.
4. Rotate database, Redis, provider and storage credentials.
5. Read the audit log and `login_event` for the window.
6. Then, and only then, decide about restoring.

## Data breach triage

Establish, in this order, and write down the evidence for each: **what data**,
**whose**, **when**, **how**, **is it still happening**.

Canadian breach-of-security-safeguards reporting under PIPEDA turns on real risk
of significant harm, and that determination is not an engineering call. Involve
privacy counsel at the point the first two questions have answers — not after
the technical work is finished.

Do not notify customers before counsel has seen the facts, and do not delay
gathering the facts while waiting for counsel.

## Publisher compromise

The publication identity can change what the tax engine believes.

1. Disable the `kb_publisher` credential immediately.
2. List every publication in the window from the publication audit trail.
3. For each, compare the published content hash against what was approved. The
   four-eyes record says who approved what.
4. Roll back unapproved publications through the existing rollback path.
5. Re-run the affected analyses — a customer priced under a forged rule has a
   wrong number sealed into their history, and it does not correct itself.

---

# Infrastructure runbooks

Added by the infrastructure entry. **None of these has been executed** — no AWS
account was reachable — so each is a procedure to rehearse in staging before it
is needed in anger, not a procedure that has been proven. Where a step says
"expect", that is a prediction, and the first person to run it should correct
this file with what actually happened.

## Deploy

`.github/workflows/deploy.yml`, on merge to `main` for staging and by manual
dispatch for production. The ordering is fixed: gates → build by digest →
migrate → deploy → verify.

What is worth watching the first few times:

- the migration task's **exit code**, not its logs. A migration that prints a
  traceback and exits 0 would deploy.
- `aws ecs wait services-stable` returning quickly for `beat`. Beat is
  configured to stop the old task before starting the new one, so it is briefly
  absent by design; the scheduler missing a minute is acceptable and two beats
  running at once is not.

## Rollback

Application rollback is routine; schema rollback is not.

```
aws ecs update-service --cluster <env>-onyx --service api \
    --task-definition <previous-task-definition-arn>
```

The previous ARN is in the deployment run's log, and every revision stays
registered. Roll every service back together — a worker running new code against
an API running old code is a combination nothing tested.

**Do not roll a migration back as a reflex.** The migrations are written to be
backward compatible with the revision still serving, which means rolling the
application back is usually sufficient and rolling the schema back is usually
destructive. If the schema genuinely must move backwards, that is a person with
this runbook open and a snapshot taken first.

## Restore

Not rehearsed. The drill belongs in staging and is the acceptance criterion for
saying backups work; a checkbox in the RDS console is not evidence.

```
aws rds restore-db-instance-to-point-in-time \
    --source-db-instance-identifier <env>-postgres \
    --target-db-instance-identifier <env>-postgres-restore \
    --restore-time <ISO-8601 within the retention window> \
    --db-subnet-group-name <env>-db --no-publicly-accessible
```

Then, and this is the part that makes it a drill rather than a gesture: point a
task at the restored instance, run the privilege invariant suite against it, and
confirm a synthetic persona's analysis is present and its figures unchanged. A
restore that produces a reachable database with an incoherent schema has proven
nothing.

Record `RESTORE_SUCCESS`, `RTO` (start of restore to first successful query) and
`RPO` (the gap between the restore point and the last committed write) as
measured numbers.

## Secret rotation

Each secret is its own entry under `<env>/onyx/`, so rotation is per secret.

- **JWT signing key** — rotating it signs every live session out. It touches no
  sealed tax artefact; those are hashed over canonical content, not signed with
  this key. So it is a sign-out event, not a data event. Update the secret, then
  restart the API service so tasks pick up the new value.
- **Database passwords** — change the password in PostgreSQL first, then the
  secret, then restart the consuming service. The other order locks the service
  out of its own database.
- **Redis AUTH token** — ElastiCache supports two tokens during rotation. Set the
  new one as `ROTATE`, update the secret, restart the workers, then `SET` to
  finish. Skipping the two-token window drops every worker at once.
- **CloudFront origin secret** — two applies: add the new value to the WAF rule
  as a second allow condition, update the distribution's custom header, then
  remove the old condition. One apply locks the edge out of the origin.

## Worker outage

Symptom: queue depth climbing, or the worker-failure alarm.

1. Which queue? The service names map to queues in
   `infra/modules/compute/services.tf`.
2. `worker-privacy` stuck is the one to treat as urgent-but-careful: it is the
   deletion path, and a customer waiting on erasure is a compliance clock. Do
   not clear its queue to make the alarm stop.
3. Restart is `aws ecs update-service --force-new-deployment`. `acks_late` means
   an in-flight task is re-queued rather than lost.

## Database outage

The API's `/readyz` will fail and the load balancer will keep serving `/healthz`,
so tasks stay up and requests fail rather than the service disappearing. That is
deliberate: a database blip should not trigger a replacement stampede.

Check the RDS event log before restarting anything. Multi-AZ failover takes
about a minute and resolves itself; a restart during it extends the outage.

## SES outage

Registration still succeeds — the token is committed before the message is
scheduled, so a customer who does not receive a link can ask for another once
delivery recovers. Nothing is lost and nothing needs replaying.

Watch the bounce and complaint rates rather than only the send count: a spike in
bounces is how a domain's reputation gets damaged, and the recovery from that is
measured in weeks.

## S3 outage

Document upload and download fail; the tax engine does not, because it reads
published knowledge from PostgreSQL. The product degrades to "cannot attach a
document" rather than stopping.

**Do not retry a privacy erasure blindly against a failing bucket.**
`hard_erase` re-lists to confirm removal; if listing is what is failing, a retry
loop can report success on an unverified deletion. Wait for the service to
recover.

## Suspected compromise

1. Revoke first, investigate second. Rotate the JWT secret — every session ends.
2. VPC flow logs record REJECTs for fourteen days; CloudFront and ALB access
   logs record requests. Start there.
3. The deployment role cannot read secret values and cannot alter IAM, by
   explicit deny. If something did either, the compromise is upstream of the
   pipeline.
4. Do not delete evidence to restore service. Snapshot the database before any
   destructive remediation.

---

## Running an ephemeral staging environment

**Never executed.** Written against the interfaces the repository defines; every
step is a plan until it has run against a real account.

Staging is not a place, it is a run. `infra/staging_cycle.sh` is the whole
sequence and its destroy step is in a `trap`, because the expensive failure mode
is not a failed proving run — it is a run that failed at step 5 and left a NAT
gateway, a database and five Fargate tasks standing until somebody notices.

```
RUN_ID=2026-08-24-b6 EVIDENCE_BUCKET=<from envs/shared outputs> \
PRIVATE_SUBNET_IDS=... TASKS_SECURITY_GROUP_ID=... \
  ./infra/staging_cycle.sh
```

**Before starting.** `envs/shared` must already be applied — the registry, the
OIDC provider and the evidence bucket are prerequisites, not part of the run.
The two ACM certificates and the hosted zone must already exist; they are passed
in as ARNs and are not created or destroyed by the cycle.

**What the run proves, in order.** Provision → migrate (exit code 0 required, or
the run stops) → the estate answers `/readyz` → the browser journeys → the
security invariants → privacy hard erasure against real versioned S3 → the
restorable-time check → the twelve-persona tax regression → evidence preserved
to the shared bucket under `RUN_ID` → destroy.

**If the destroy fails.** It will say so and exit non-zero. Do not walk away:
re-run `terraform -chdir=infra/envs/staging destroy` until it succeeds, then
`terraform state list` to confirm nothing is left. A half-destroyed environment
is the one state that costs money and proves nothing.

**Drift.** There is none to accumulate, which is the point. The environment is
created from the committed configuration at the start of every run, so
"staging drifted" stops being something that can happen quietly between runs.
What can still drift is the PERSISTENT tier — the registry's lifecycle policy,
the evidence bucket's settings — and that is why it is a Terraform root of its
own rather than console-managed.

**Cost.** $0.2451 per hour standing. A two-day run is $11.76; ten days is
$58.81. See `docs/operations/cost-model.md`.

## Changing the capacity profile

`infra/modules/capacity` holds two profiles. Moving between them is one line in
an environment root:

```hcl
variable "capacity_profile" {
  default = "high_availability"   # was "lean_launch"
}
```

**Then read the plan.** It will show a database instance-class change (which
replaces the instance and takes a maintenance window), a second NAT gateway, a
second cache node and larger task definitions. None of it is reversible for
free: going back down is another instance-class change.

**What the plan must NOT show.** A change to `publicly_accessible`, to a route
table, to a task role, to a database secret mapping, to `ONYX_ENVIRONMENT`, or
to the listener's default action. If it does, something has been wired through
the capacity module that should not be, and
`backend/tests/security/test_capacity_profiles.py` should have caught it —
treat a plan like that as a defect in the test, not a surprise in the plan.

**The scale-up triggers** that say when to do this are in
`docs/operations/cost-model.md` §7. They are metric thresholds with durations,
not a date.

## Rotating the CloudFront origin secret

Superseded by the lean-launch entry: the secret is now generated in the
environment root (`random_password.origin_verify`) and consumed by BOTH
`modules/compute` — the load balancer's listener rule — and `modules/edge` — the
distribution's custom header. It is no longer inside the edge module, and the
regional WAF web ACL that used to enforce it no longer exists.

The two-apply sequence is unchanged in shape and still necessary, because the
distribution and the listener must not be updated in the same apply:

1. Add the new value as a SECOND accepted value on the listener rule (an
   `http_header` condition takes a list), apply, and wait for the load balancer
   to settle. Both the old and the new header are now admitted.
2. Change the distribution's `custom_header` to the new value, apply, and wait
   for the distribution to deploy — CloudFront propagation is minutes, not
   seconds.
3. Remove the old value from the listener rule, apply. Only now is the old
   secret refused.

Doing it in one apply refuses live traffic for the length of the propagation.
