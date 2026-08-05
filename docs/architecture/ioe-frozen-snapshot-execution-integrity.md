# Onyx Ledger — Frozen Snapshot Execution Integrity (item 3A)

## 1. The defect

TX-1 pinned an immutable analysis snapshot and put its hash into
`optimization_spec_hash`. The compute phase then called
`TaxEngineService.build_input(user_id, tax_year)`, which reads the user's **live**
financial, profile and expense tables. Between the two moments a person can edit last year's
income. That admits:

```
pin snapshot A → live financial data becomes B → calculate from B → seal under A's identity
```

The sealed result's identity claimed to describe snapshot A while its numbers described B. A hash
that does not identify the calculation it labels is not evidence — it is decoration. Item 3 found
this from the outside: replay verification is structurally impossible against a specification that
does not name the actual inputs.

## 2. Affected historical behaviour

Every optimization sealed before this correction was computed from live sources. Where a user's
data did not change between the analysis and the optimization — the common case — the run happens
to be reproducible. Where it did change, the run is not, and no stored field distinguished the two.
That is why the correction adds `input_execution_policy_version` rather than a timestamp
comparison.

## 3. The corrected invariant

> Every optimization, rules evaluation, portfolio evaluation and governed projection is calculated
> exclusively from the exact frozen analysis snapshot pinned in TX-1.

No fallback. A missing, corrupt, incomplete, schema-unsupported or unreconstructible snapshot fails
closed **before the workflow header is created**, so a failed snapshot produces no run and no sealed
children at all.

## 4. Frozen execution-input model

`FrozenAnalysisInput` (`app/services/ioe/frozen/models.py`) is a frozen dataclass carrying
`user_id`, `analysis_id`, `snapshot_id`, `snapshot_hash`, `snapshot_schema_version`, `tax_year`,
`jurisdiction`, the reconstructed `TaxInput`, `baseline_result_hash`, `baseline_tax`, and the rule
pins.

It is the **only** channel through which the compute pipeline receives user data. It holds no
session, no repository and no user-id-keyed loader, so a downstream service has no route back to
mutable state even by accident. `inputs_as_dict()` returns a fresh clone for a hypothetical; the
frozen input is never the thing a lever mutates. `with_pins()` returns a new object rather than
mutating.

It is deliberately **not persisted**: the reconstructed inputs are the user's financial data, they
already live in the analysis snapshot, and copying them into IOE result tables would duplicate raw
financial inputs into evidence.

## 5. Transaction sequence

```
TX-1   authorize the analysis
       → resolve the exact immutable snapshot and verify ownership
       → recompute and compare its canonical hash
       → reconstruct the TaxInput, fail closed on any defect
       → derive and pin the baseline-result identity
       → pin the rule snapshot and the version manifest
       → validate structured constraints and assumptions
       → compute optimization_spec_hash
       → resolve idempotency against that hash
       → create the running header
       → COMMIT

COMPUTE (no transaction held)
       → use the FrozenAnalysisInput TX-1 resolved
       → engine.run(frozen tax input) → facts_for(input, result)
       → evaluate only the pinned rule-version set
       → normalize, score, rank, derive relationships, assemble, project
       → never query mutable finance / profile / wealth state

TX-2   persist all sealed evidence atomically, seal hashes, complete
```

The snapshot is resolved **first**, before the rule snapshot and before the manifest, because
everything downstream describes that input or it describes nothing.

## 6. Snapshot reconstruction

`FrozenAnalysisInputService.resolve()` performs eleven checks, all fail-closed: analysis exists →
belongs to this user → snapshot exists and belongs to that analysis → schema version supported →
content hash recomputed → matches the snapshot's stored hash → matches the hash the run pinned (on
replay) → `TaxInput` reconstructed with a missing field treated as INCOMPLETE rather than defaulted
→ tax year and jurisdiction reconcile with the analysis and with the reconstructed input →
baseline-result identity re-derived through `TaxEngineService` → the immutable object returned.

Failures are a closed enumeration: `PINNED_SNAPSHOT_UNAVAILABLE`,
`PINNED_SNAPSHOT_HASH_MISMATCH`, `PINNED_SNAPSHOT_INCOMPLETE`,
`PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED`, `PINNED_BASELINE_RESULT_UNAVAILABLE`,
`PINNED_BASELINE_RESULT_HASH_MISMATCH`, `FROZEN_INPUT_RECONSTRUCTION_FAILED`. The exception carries
the code and nothing else — no payload, no message destined for storage.

**One codec.** `canonical_snapshot()` / `snapshot_hash()` / `reconstruct_tax_input()` are used by
the writer (`AnalysisService`), by TX-1, and by the replay resolver. A snapshot is reconstructible
by construction rather than by agreement between separately-written pieces of code.

## 7. Live-access removal

`TaxEngineService.build_input` is split:

| Method | Reads | Callers |
|---|---|---|
| `build_input_from_live_sources(user_id, tax_year)` | mutable tables | `AnalysisService` (taking the snapshot), read-time freshness comparison |
| `build_input_from_snapshot(payload)` | a payload | the optimization compute path |

`build_input_from_snapshot` takes a payload rather than a user id, so there is no parameter through
which live state could be reached. The optimization module names none of the live builders — an AST
test asserts it.

## 8. Structural enforcement

Three layers, not comments:

1. **Object boundary** — `FrozenAnalysisInput` is frozen and exposes no session, repository or
   loader. Asserted by field-name inspection and `__dataclass_params__.frozen`.
2. **AST boundary** — `test_the_orchestrator_never_calls_a_live_input_builder` parses
   `orchestrator.py` and fails if `build_input`, `build_input_from_live_sources` or
   `_build_input_live` is called.
3. **Query boundary (defense in depth)** — SQL is captured during the compute phase only, and the
   test fails on any statement naming `finance.`, `profile.` or `wealth.`.

## 9. Tax-engine and rules-evaluator boundaries

`TaxEngineService` remains the only tax-calculation authority; the change is which builder produces
its input. `RulesEvaluatorService` remains the only eligibility authority and still receives
`pinned_rule_version_ids`, so a rule published after TX-1 cannot enter an in-flight run — asserted
by `test_a_rule_published_after_tx1_still_cannot_enter_the_run`. Facts are derived from the frozen
`TaxInput` and its `TaxResult` through the same `facts_for` the portfolio uses.

## 10. Portfolio and projection behaviour

All portfolio work clones the same reconstructed frozen baseline — standalone runs, incremental
runs and the final combined run. I-1 telescoping, I-2 reconciliation, ledger conservation,
structured exclusions, the pinned objective code/version and `optimality_claim = none` are all
unchanged, and now reconcile against the baseline the sealed specification actually names.

## 11. Idempotency

Ordering is normative: authorize → resolve snapshot → verify hash → pin baseline result → pin rule
snapshot → pin manifest and weight config → validate assumptions and constraints → compute
`optimization_spec_hash` → resolve `Idempotency-Key`. Resolution is against the canonical spec
hash, never raw request JSON. Same key + canonically equivalent frozen specification replays; same
key + a different snapshot identity returns `409 idempotency_key_reused`.

## 12. Worked example

```
analysis A frozen with employment_income = 95,000, snapshot_hash = 76f284bf…
TX-1 pins A; spec hash names A
live income edited to 450,000
COMPUTE runs against A — portfolio.baseline_tax equals compute(A).total_payable exactly
TX-2 seals under A
IntegrityVerificationService → integrity_status = verified
```

That is `test_a_live_financial_change_after_tx1_cannot_affect_the_run` plus
`test_a_run_computed_against_a_changed_live_state_verifies_immediately`. Before this correction the
first would have computed 450,000's numbers and the second would have reported `mismatch`.

## 13. Security and privacy

Ownership checks, RLS, parent-qualified reads and partition invariants are untouched. The snapshot
is held in memory for the duration of a run and is copied into no result table, no portfolio trace,
no integrity-check row, no operational event, no log and no queue message. Workflow errors carry
only the closed reason code. Tests assert a synthetic SIN-like value placed inside a snapshot never
appears in logs, the version manifest or stored constraints.

## 14. Migration

`0039_input_execution_policy` → `db/sql/33_input_execution_policy.sql`. One column
(`input_execution_policy_version`, `NOT NULL DEFAULT 'live_source_legacy'`), one CHECK constraint
over the two enumerated values, one index for finding the affected population. Additive; no
existing object altered. Lock risk low — `ADD COLUMN` with a constant default is metadata-only on
PostgreSQL 16. No RLS impact: the column sits on an already-protected table. Forward-only
downgrade, consistent with the chain.

## 15. Historical runs

Pre-correction rows keep `live_source_legacy` and are **not** backfilled as corrected — claiming a
guarantee those runs never had would be worse than the original defect, because it would be
undetectable. They may honestly report `integrity_status = mismatch` or `unavailable`. A user or
operator may refresh an old result into a new corrected run, which links through the existing
supersession fields. **Deployment cutoff:** the commit that introduces
`0039_input_execution_policy`; every run created after it is `frozen_snapshot_v1`.

## 15b. Performance

Container reference environment, 40 candidates, 40 published rules.

| Metric | Before 3A | After 3A | Target |
|---|---:|---:|---|
| SQL statements per optimization | 59 | **47** | ≤ 60 ✔ |
| — reads / inserts / updates | 36 / 21 / 2 | 29 / 16 / 2 | — |
| Snapshot reconstruction | n/a | 3 statements, p50 3.2 ms, p95 8.1 ms | — |
| Engine runs per optimization | 92 | 92 | unchanged |
| Optimization p50 / p95 | — | 225.7 / 291.5 ms | — |
| Peak RSS | 80.5 MiB | 78.3 MiB | — |

The statement count went **down**, not up. Reading the frozen snapshot is three statements; the
live income, expense and profile reads it replaces were more. The approved ≤ 60 target is met with
more headroom than before.

These are unix-socket numbers and are not a managed-database claim. What transfers is the
round-trip count: 47 per run, so ~0.09 s of waiting at a 2 ms RTT.

## 16. Remaining limitations

1. Snapshots written before this correction use the previous flat payload shape and no
   `schema_version`, so they fail closed as `PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED`. Those analyses
   must be re-run before they can be optimized. That is the honest consequence of making the
   snapshot format self-describing.
2. `ScenarioService` still pins a snapshot hash while computing from a live baseline — the same
   defect class, out of scope for this item, and now the only remaining instance.
3. Household and contribution-room resolution do not currently contribute inputs; if they are added
   later they must enter the snapshot rather than being read live.
