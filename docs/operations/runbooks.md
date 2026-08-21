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

- Run as `onyx_migrator`, never from the API container.
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
