# TERMINAL ACCOUNT REMOVAL — and what a deleted person's UUID is still worth

Every entry from 11B6C onward forbade `identity.terminal_remove_account` by
name. This is the entry that writes it, and the reason it is safe to write now
is that nothing about it is a judgement call: five certified lifecycle phases
already exist, each with a completion guard the database enforces, and the
keyhole refuses unless it can re-read the evidence that all five finished.

**Production enablement is OFF.** No worker calls this. The dispatcher still
stops at the five phases, and `test_no_worker_calls_the_terminal_keyhole`
asserts that against the source so a future wiring has to be a decision rather
than a line in an unrelated diff.

## The keyhole

`identity.terminal_remove_account(user_id, claim_token, worker_id)`, migration
`0066`, SQL `60_terminal_account_removal.sql`. `SECURITY DEFINER`, pinned
`search_path`, PUBLIC revoked, `EXECUTE` to `onyx_privacy_worker` alone.

Because it runs as the owner, no role needed a new grant: `onyx_app_rw`,
`onyx_app_ro` and `onyx_privacy_worker` all still hold **no DELETE** on
`identity.user_account`. The capability to remove ONE claimed account never
becomes the capability to express "every account".

It is fail-closed on six facts, every one re-read at call time:

```
1. the claim token holds this subject
2. the lifecycle is in PURGING
3. all five phases report COMPLETE
4. all five completion guards independently read zero
5. the account still exists
6. PURGING -> COMPLETE is a legal transition
```

Point 4 is the one that matters. A phase row saying COMPLETE is a *claim*; the
counts are the *evidence*. The first version of the test fixture marked the five
phase rows complete without running them, and the keyhole refused —
`SOURCE_DATA: 3 rows; AUDIT_AUTH_DEIDENTIFICATION: 1 rows`. The guard was right
and the fixture was lying, which is the entire argument for re-reading.

## Two defects the proofs found

**A wedged lifecycle after a crash.** The first draft returned `false` when the
account was already gone and left the lifecycle in `PURGING` — permanently. The
person removed, the record still saying a purge was owed. The goal state is
"account gone AND deletion recorded complete", and a retry has to reach it from
either half, so the keyhole now converges the lifecycle even when it removes
nothing. `test_a_crash_after_removal_converges_on_retry` pins it.

**A completion with no completion time.** `ck_account_lifecycle_completion`
requires `completed_at` when the state is COMPLETE. The first draft set the
state and not the time, and the schema refused it — a state that claims to be
finished without saying when is exactly the half-truth this database declines to
store.

## The Actor Attribution Decision Gate (PD-15)

`audit.audit_log.actor_id` holds a deleted customer's own account UUID. The
table is append-only and `trg_audit_immutable` refuses UPDATE **even for the
owner**. Nothing in this entry touches it: rewriting immutable audit history to
make a privacy number look better would be the worst available outcome, and
`test_audit_history_was_not_rewritten` asserts the keyhole issues no statement
against the `audit` schema at all.

So the question was never "can we remove it" but "what is it still worth". That
is measurable, and it was measured.

### Method

A sealed account A is driven through all five phases **by the real worker**,
terminally removed, and a same-email successor B registers. Then every `uuid`
column in the database named like an account or actor reference — built from
`pg_attribute`, not a hand list — is scanned for A's UUID.

The catalogue scan earned its keep immediately: it found two holders no
hand-written list would have contained. `audit.audit_log_default` is a
*partition* of `audit_log`, so the same residue under a different relname; and
`identity.account_lifecycle_event` is a transition log nobody had enumerated.

### Measured inventory after terminal removal

```
analysis.analysis_run.user_id                 1 row    retained sealed root (0060)
ioe.optimization_run.user_id                  1 row    retained sealed root
ioe.scenario.user_id                          1 row    retained sealed root
audit.audit_log_default.actor_id             39 rows   immutable history
identity.account_lifecycle.user_id            1 row    PD-9 deletion record
identity.account_lifecycle_phase.user_id      5 rows   PD-9 deletion record
identity.account_lifecycle_event.user_id      5 rows   PD-9 deletion record
```

Nothing else in the database holds it.

### Prohibited paths, each asserted individually

```
identity.user_account       GONE          -> no email
identity.user_credential    0 rows        -> no credentials
profile.user_profile        0 rows        -> no profile
profile.tax_profile         0 rows        -> no profile
identity.account_subject    0 rows        -> no subject identity
identity.deletion_subject   no user_id column at all
successor B                 different UUID; same email, different person
```

`42 audit rows retain A's UUID; it resolves to no account, no subject key, and
is not B's.`

### Verdict

The surviving `actor_id` is an **orphaned pseudonymous historical/security
correlator**: it correlates a deleted person's own actions to each other within
immutable audit history, and it reaches no identity. On the criterion set for
this gate — email, profile, credentials, subject identity, B, or any other
prohibited personal-identity path — every path is closed by measurement.

**One thing to state plainly rather than bury.** The three retained sealed roots
also keep A's UUID, and they are not merely a correlator — they are that
person's retained tax evidence, deliberately preserved by 0060 and classified
`REPLAY_REQUIRED_RETAIN`. That is a known and separately certified retention
decision, not a new PD-15 finding, but anyone reading this inventory should see
it named rather than discover it later.

## What survives, and still works

The four sealed roots outlive the account because 0060 detached them, and
optimization, portfolio and scenario all still verify with the person gone.
`identity.account_lifecycle` and its phase and event rows outlive it too — they
have no foreign key to the account, which is what makes PD-9's durable record of
the deletion durable.

## Status

```
terminal removal          IMPLEMENTED, NOT ENABLED
onyx_app_rw DELETE        still none on identity.user_account
audit history             not rewritten
```
