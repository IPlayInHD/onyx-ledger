---
name: onyx-entry
description: Execute one governed Onyx Ledger engineering entry end to end — preflight, architecture review, implementation, targeted verification, full certification, exact-SHA CI, closure report. Invoke manually with /onyx-entry when starting a numbered engineering entry. Do NOT auto-invoke this skill; it drives multi-hour certification workflows and must only start when the user explicitly asks for it by name.
---

# Onyx Certified Engineering Entry

This skill runs one governed entry from a clean certified state to a closed,
certified one. It exists so the stable rules of this repository do not have to
be restated in every feature prompt.

## Invocation boundary — read this first

Run this workflow **only** when the user explicitly invokes `/onyx-entry`, or
names an entry and asks you to execute it under this skill. The workflow
commits, pushes, and runs a release gate that takes hours. Starting it because
a task merely *looks* like Onyx work would be expensive and unwanted.

If you are reading this file because the skill auto-triggered on an ordinary
question, stop and answer the question directly instead.

## The two kinds of knowledge in this skill

Everything here is one of two things, and confusing them is how a skill rots:

**Stable invariants** — the authority model, determinism rules, privacy
posture, isolation discipline. These live in this skill's reference files and
change only when the architecture changes.

**Dynamic repository state** — HEAD, branch, migration head, current schema
version, test counts, CI run IDs, gate timings. These are *never* written down
here. Discover them at invocation time, every time. A number you hardcode
today is a lie you tell yourself in three weeks.

Run `scripts/preflight.sh` (read-only) to collect the dynamic state.

## Reference files — load what the entry needs

Read the SKILL.md body always; pull in references by relevance, not by habit.

| File | Read it when |
|---|---|
| `authority-model.md` | The entry computes, decides, reads historical/sealed state, or adds any service |
| `determinism.md` | The entry touches hashes, canonicalization, ordering, versions, or persisted artifacts |
| `privacy-security.md` | The entry adds persistence, a read consumer, a role/grant, RLS, or touches deletion |
| `test-isolation.md` | The entry adds tests, or a test fails only in combination/reorder |
| `defect-taxonomy.md` | Anything unexpected fails — classify before you change code |
| `certification.md` | Phases 6–8: which gates, in what order, and what "certified" requires |
| `architecture-invariants.md` | Orientation: what the subsystems are and where authority lives |

Templates: `templates/entry-report.md`, `templates/blocker-report.md`.

---

## PHASE 0 — Pre-flight

1. Read the entry request completely. Note explicit prohibitions — entries
   routinely forbid persistence, API surface, or reopening closed work. Those
   constraints are the specification, not suggestions.
2. Run `scripts/preflight.sh` for branch, HEAD, upstream, tree status,
   migration head, and the current scenario-result version authority.
3. Require a clean tree and `HEAD == @{u}` unless the user says otherwise. A
   dirty starting tree makes the closing certification meaningless, because you
   cannot say which edits the gate actually covered.
4. Read the architecture docs the entry touches (`docs/architecture/`) before
   proposing a design. Entries here build on closed entries; contradicting one
   by accident is the most expensive mistake available.

## PHASE 1 — Reproduce and inventory

Before designing anything:

- If the entry claims a gap, **measure it**. A gap you did not reproduce may
  not exist, or may not be the gap described.
- Inventory the authorities involved (`authority-model.md`). Which service
  already owns each decision this entry needs?
- Determine whether the required source actually exists. For historical work,
  ask specifically: is there a *frozen, pinned* source for every family this
  entry must read? If not, that is a blocker, not an invitation to reconstruct.
- Identify persistence, version, and privacy implications now, while they are
  still cheap to design around.
- Surface blockers **before** implementing. A blocker found after the code is
  written costs the code.

## PHASE 2 — Design

Choose the narrowest coherent architecture that satisfies the entry.

- Preserve existing authorities. Do not create a second implementation of a
  governed calculation because calling the first one is inconvenient.
- Prefer reusing a certified helper over writing a parallel one. If the
  comparator needs canonical node equality, it should use the graph hasher's
  payload functions, not invent a second notion of equality.
- Write down assumptions explicitly. State compatibility obligations,
  determinism requirements, and security/privacy implications before coding.
- If the design requires something the entry forbids (a migration, a version
  bump, a new authority), stop and report — see **Stop conditions**.

## PHASE 3 — Implement

- Contained changes only. Respect existing layering.
- Tests establish their own state (`test-isolation.md`). This repository has a
  documented history of tests that passed alone and failed in company.
- **No unrelated refactoring.** If you find an unrelated defect, record and
  classify it; fix it only if it blocks this entry's certification, or the user
  authorizes it. This matters most during certification, where an unrelated
  edit invalidates the gate run you are waiting on.

## PHASE 4 — Targeted verification

Smallest meaningful tests first, widening as they go green. Use
`scripts/run_backend_tests.sh` with explicit paths.

Include, as the entry warrants: focused unit tests, integration tests,
determinism (`PYTHONHASHSEED` variation), historical replay, privacy, RLS and
security invariants, version compatibility, reordered/shared-DB isolation, and
performance measurement.

Prove counters and guards are non-vacuous. A test asserting "the engine ran
zero times" proves nothing unless it also asserts real work was produced —
patch the name the caller actually binds, not the module you assume it reads.

## PHASE 5 — Static quality

Repository-authoritative commands, from `backend/`:

```
ruff check app workers scripts tests
./scripts/check_types.sh          # protected-scope mypy, zero errors
```

## PHASE 6 — Broader verification

Widen only once targeted correctness holds. Running an expensive gate before
the cheap ones pass wastes hours and tells you nothing new. See
`certification.md` for the ordering and what each stage actually proves.

## PHASE 7 — Final certification

`certification.md` has the full protocol. The core rule:

> **Certify the exact tree.** A gate PASS from an earlier SHA never certifies
> later edits, and neither does CI on an earlier SHA.

Commit, push to the designated branch, then run the full release gate on that
exact tree and verify CI on that exact SHA. While a gate is running: do not
edit the tree, do not start another entry, do not restart the gate unless the
process actually exits.

## PHASE 8 — Closure

Produce the report from `templates/entry-report.md`. Mark **CLOSED**,
**PARTIAL**, or **BLOCKED** on evidence, not optimism. Adapt the sections to
the entry rather than padding irrelevant fields.

End with `NEXT: <next entry>` when it is known, then **stop**. Do not begin the
next entry.

---

## Stop conditions

Return a blocker report (`templates/blocker-report.md`) instead of improvising
when:

- No authoritative historical source exists for semantics the entry requires.
- The feature would require inventing a second business authority.
- Privacy policy conflicts with required persistence.
- A schema or protocol version bump is required but the entry forbids one.
- Historical compatibility cannot be preserved under the requested design.
- RLS cannot express the requested access semantics.
- A migration is necessary in an entry that forbids persistence.
- Production code appears necessary in a task classified as test-only.
- The release gate fails during certification.
- Exact-final-SHA CI is not completed/success.
- Repository state changed while certification was running.

Reporting a blocker with evidence is a successful outcome. Silently widening
scope to avoid one is not.

## What this skill will not do

- Modify product code while only the skill or docs were requested.
- Use an LLM as a tax, rules, eligibility, or evidence authority.
- Reconstruct sealed history from current business state.
- Infer zero from missing authority.
- Report an entry closed without gate and CI evidence on the final SHA.
