# Test isolation

This is the defect class that has cost this repository the most certification
time, and it is entirely preventable.

The repository already documents the lesson in
`backend/scripts/prove_suite_state_independence.sh`. Read it — it is the primary
source, and it explains why `prove_security_gate.sh` reruns the security suite
many times against **one** database, putting the suite in a state no single run
ever sees.

## The rule

Any test whose correctness depends on rules, published rules, candidates,
opportunities, deadlines, eligibility, evidence requirements, pinned versions,
lifecycle state, claim queues, leases, outbox state, or tenant state **must
establish the state it requires**.

Never rely on residue left by another test. Never assume a shared accumulating
database is empty.

## The specific traps

**Bounded oldest-first queues.** `ioe.claim_freshness_events` and
`identity.claim_account_lifecycle` take a bounded batch, ordered oldest-first.
"Claim, then expect my own row back" is really "assert fewer than N claimable
rows exist" — true on a fresh database, false after a few reruns. If a test
needs to reach its own row, it must claim until it finds it, or establish the
bound itself.

**Leases with timeouts.** A lease taken by an earlier test expires on a wall
clock. A suite that runs for two hours crosses that boundary; a suite that runs
for ninety seconds does not. Expire or account for live leases explicitly rather
than assuming none exist.

**Payload growth.** State that accumulates across runs makes payloads grow.
A payload passed through a single `argv` element dies at 128 KiB with `E2BIG`.
Pass a file path.

**Counters that prove nothing.** Patching `module.compute` does nothing if the
caller bound the name directly at import. Patch what the caller actually
resolves, and assert the work happened — a zero-invocation assertion beside an
empty result is vacuous.

## What "passes" means

- Green **alone**, on a fresh database, where practical.
- Green **in combination** with related suites.
- Green **in a different order**, where order-dependence is a plausible risk.

A test that only passes alone is not finished. A test that only passes in
company is worse.

## Before writing a test that reads shared tables

Ask: if a hundred other tests ran first and left rows behind, would this
assertion still be true? If the answer depends on the database being nearly
empty, scope the query, create the state, or bound the search explicitly.
