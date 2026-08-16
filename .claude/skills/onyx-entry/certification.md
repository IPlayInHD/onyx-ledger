# Certification

Certification answers one question: **is this exact tree known good?**
Everything below serves that, and any shortcut that breaks the link between the
evidence and the revision destroys the answer.

## The core rule

> A release gate PASS from an earlier SHA never certifies later edits.
> CI on an earlier SHA never certifies later edits.

Before calling an entry CLOSED, `HEAD`, upstream, working tree, release gate and
CI must all correspond to the **same final revision**.

## Ordering — cheap gates first

Run in this order. Widening before the narrow checks pass wastes hours and
tells you nothing new.

1. Targeted tests for the change (`./scripts/run_backend_tests.sh <paths>`)
2. `ruff check app workers scripts tests`
3. `./scripts/check_types.sh` — protected-scope mypy, zero errors
4. Related suites in combination, and in a different order
5. Full backend suite (`./scripts/run_backend_tests.sh`)
6. `./scripts/release_gate.sh --full`

## What the release gate contains

`backend/scripts/release_gate.sh` is the authority; `--fast` is lock, lint,
types and drift with no database, `--full` is everything including a freshly
provisioned one. Discover the stage list from the script rather than trusting a
copy — at time of writing it runs, in fail-cheapest-first order:

```
python runtime · dependency lock · ruff · protected mypy
clean install proof · gate failure proof
migration smoke · revision hygiene · schema drift · schema drift proof
admission control · security invariants · security gate proof
determinism · full suite (fresh DB)
```

The gate prints `release gate (--full): PASS` and exits 0 only if every stage
passed. Nothing is tolerated with `|| true`.

**Practical notes.** The full gate takes hours, dominated by the security gate
proof: it reruns the whole security suite twice per injection case — once with
the invariant removed, expecting failure, once after restoring it, expecting
green — so its cost scales with the number of cases. Log to a file and read
stage markers from it —
piping through `tail` buffers the entire run and loses the stage record if the
process is killed. Do not wrap it in a `timeout` shorter than the run; `timeout`
forwards its signal to the child, so killing the wrapper kills the gate.

## While a gate is running

- Do not edit the tree.
- Do not start another entry.
- Do not restart the gate unless the process actually exited.
- Do not change HEAD.

If it **fails**: classify the failure (`defect-taxonomy.md`) and diagnose the
exact stage before changing anything. Identify whether the failure is in the
change under certification or elsewhere, and say which, with evidence.

## Exact-SHA CI

CI is `.github/workflows/backend-quality-gate.yml`. Verify the run for the exact
final SHA is `completed` / `success`, and enumerate the blocking jobs
individually rather than trusting the roll-up. Discover the job list from the
workflow file or the run itself.

Note that CI and the local `--full` gate are not identical: CI does not run the
injection-proof harnesses. A green CI is necessary, not sufficient.

## Git protocol

Develop and push only to the branch the task designates. Use
`git push -u origin <branch>`. On network failure, retry with exponential
backoff (2s, 4s, 8s, 16s). Do not open a pull request unless explicitly asked.

If the branch's PR has already merged, restart the branch from the latest
default branch rather than stacking new commits on merged history.

## Closure evidence checklist

- Starting SHA and clean tree recorded at Phase 0.
- Final SHA; `HEAD == @{u}`; working tree clean.
- Targeted test results with counts.
- Full suite result with counts.
- `release gate (--full): PASS` on the final tree.
- CI run ID, status, conclusion, blocking jobs enumerated, on the final SHA.
- Defects discovered, classified.

Missing evidence means **PARTIAL** or **BLOCKED**, not CLOSED.
