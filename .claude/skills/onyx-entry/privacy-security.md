# Privacy and security

This system holds financial and identity data for individual people. The
posture is that a privacy or tenancy failure is not a bug class to be traded off
against velocity — it is the failure the rest of the architecture exists to
prevent.

Use the repository's own registries and tests as the authority. Do not invent
parallel policy.

| Concern | Authority |
|---|---|
| Per-table privacy classification, retention, deletion action | `app/privacy/classification.py` |
| Privacy invariants and registry tests | `backend/tests/privacy/` |
| Security invariants (RLS, privilege, partitions) | `backend/tests/security/` |
| Deletion cascade universe and FK matrix | `docs/privacy/` |
| Injection proof that the security gate actually rejects defects | `backend/scripts/prove_security_gate.sh` |

## Row-level security

Tenant tables carry `user_id` and must have an enforced boundary:

- **`ENABLE ROW LEVEL SECURITY`** — without it the table is unprotected.
- **`FORCE ROW LEVEL SECURITY`** — without it policies do not apply to the table
  owner, so the table looks protected while the owning role reads every tenant.
- **Correct `USING`** — controls what is visible.
- **Correct `WITH CHECK`** — controls what can be written. A policy with `USING`
  and no `WITH CHECK` lets a caller write a row it could never read.
- **No existence oracle.** "Permission denied" and "no such row" must not be
  distinguishable in a way that reveals another tenant's data exists.

The suite runs under a runtime role, never a superuser — a superuser bypasses
RLS outright, so the assertions would pass against a database with every policy
removed.

## Privileges

Least privilege throughout. `SECURITY DEFINER` only where deliberately governed,
with a fixed `search_path` where repository policy requires it. Minimal grants;
no `EXECUTE` to `PUBLIC` on definer functions. Application roles must not
inherit owner or superuser behavior — privileged runtimes (privacy worker,
freshness relay, lifecycle worker) hold their own logins and their own
capabilities, modelling production topology rather than convenience.

## Privacy review triggers

Any of these means a privacy review is part of the entry, not a follow-up:

- A new table, or a new column on an existing one.
- A new read consumer of existing data — reading widens the surface even when
  nothing is written.
- Any change to deletion, retention or replay behavior.

Reconcile the two obligations explicitly, because they pull in opposite
directions:

- **Do not silently retain** data that policy says must be purged.
- **Do not silently purge** data that a certified replay contract requires.

When they genuinely conflict, that is a stop condition — report it rather than
picking one.

Post-deletion behavior uses the governed lifecycle semantics. Do not invent a
new "deleted" state; the classification module already defines the vocabulary.

## When a gate finds something

A security or privacy gate failure is a finding until proven otherwise. Before
concluding "flake":

1. Reproduce it. If it will not reproduce, say so with the number of attempts —
   that is data, not a verdict.
2. Check whether the failure is in the invariant under test or somewhere else
   entirely. A restore-step failure naming an unrelated test is usually
   accumulated state, but "usually" is a hypothesis to measure, not a
   conclusion.
3. Never weaken a privacy assertion to make a gate pass. If the assertion is
   wrong, prove it is wrong.
