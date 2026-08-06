# Onyx Ledger — Frozen Scenario Baseline Integrity (item 3B)

## 0. Provenance

| Commit | Contribution |
|---|---|
| `d97f86a` | **Original item 3B implementation.** Removed the live baseline from `ScenarioService`, added `FrozenScenarioInputService` / `FrozenScenarioExecutionInput`, deleted `PinnedScenarioSpec.baseline_inputs`, added `ScenarioBaselineUnavailable`, pinned the execution policy, and wired the replay path to the frozen input. |
| `ec51f25` | **Item 3B closeout.** Corrected integrity-state semantics (legacy ≠ mismatch, migration `0040`), made policy parsing strict and fail-closed, added the seal-time policy guard, and added sanitized internal diagnostics. |
| *this commit* | **Conformance audit and gap closure.** Not a reimplementation: a distinct `PINNED_SCENARIO_SNAPSHOT_INCOMPLETE` classification, a bounded legacy-refusal metric, profile-race coverage, corrected scheduler wording, and the documentation sections below. |

## 1. The defect

Item 3A corrected the optimizer. The identical defect survived one module over.

`ScenarioService._pin_specification` pinned `baseline_input_snapshot_hash` from the
analysis snapshot and then built the baseline by calling
`TaxEngineService.build_input_from_live_sources(user_id, tax_year)` — the user's **live**
financial, profile and expense tables. Between the pin and the calculation a person can edit
last year's income. That admits:

```
pin snapshot A → live financial data becomes B → apply levers to B → seal under A's identity
```

The sealed `tax_delta` is `baseline(B) − scenario(B)` while `baseline_input_snapshot_hash`
names A. A delta is only meaningful relative to a recorded baseline; here the recorded
baseline and the arithmetic baseline were different objects. The scenario `baseline_tax`
column made this worse than in the optimizer, because it *looked* like the baseline was
stored — it was, but it was the live one, not the pinned one.

## 2. The corrected invariant

> Every scenario baseline, hypothetical tax input, rules evaluation, support-score
> calculation, comparison and sealed result derives exclusively from the exact frozen
> snapshot and the baseline-result identity pinned when the scenario specification is
> created.

No fallback. An unresolvable snapshot fails closed **before the scenario header exists**, so
a failed baseline produces no scenario row, no levers, no assumptions, and no sealed
children.

## 3. Reuse, not a second system

The requirement was explicit: reuse item 3A's infrastructure, do not build a second
snapshot reconstruction. What 3B adds is one thin composition:

| Layer | Item | Role |
|---|---|---|
| `canonical_snapshot` / `snapshot_hash` / `reconstruct_tax_input` | 3A | the one codec, shared with the writer and the replay resolver |
| `FrozenAnalysisInputService.resolve()` | 3A | the eleven fail-closed checks and the baseline-result derivation |
| `FrozenAnalysisInput` | 3A | the immutable carrier |
| `FrozenScenarioInputService` | 3B | **composes** the above; adds the scenario's registry pins and translates reason codes |
| `FrozenScenarioExecutionInput` | 3B | wraps `FrozenAnalysisInput`; adds objective, lever/assumption registry and support-score versions |

`FrozenScenarioInputService` contains no hashing, no decoding and no snapshot reading of its
own. Two reconstruction implementations would be two chances to disagree about what a
baseline is, and the disagreement would surface months later as an unexplainable replay
mismatch.

## 4. The pinned specification carries no second input channel

`PinnedScenarioSpec` previously held `baseline_inputs: dict` — a plain copy of the user's
financial figures, sitting beside the hash that was supposed to identify them. It is gone.
The three baseline identities are now **properties** that read through `frozen`:

```python
@property
def baseline_input_snapshot_hash(self) -> str: return self.frozen.snapshot_hash

@property
def baseline_result_hash(self) -> str: return self.frozen.baseline_result_hash

@property
def baseline_tax(self) -> Decimal: return self.frozen.baseline_tax
```

There is therefore no field a future edit could quietly populate from a live query, and no
second copy that could drift from the snapshot the spec hash names.
`test_the_pinned_scenario_spec_carries_no_second_input_channel` asserts the absence of
`baseline_inputs` structurally rather than by comment.

## 5. Transaction sequence

```
TX-1   authorize the analysis (exists, owned, completed)
       → resolve the pinned snapshot through FrozenScenarioInputService
         (schema, hash-vs-stored, reconstruct, year/jurisdiction reconcile,
          baseline RESULT re-derived through TaxEngineService)
       → pin the rule snapshot
       → pin the version manifest, including the execution policy
       → compute scenario_spec_hash over the pinned baseline
       → bind that hash back onto the frozen input
       → resolve Idempotency-Key against the spec hash
       → create the header and write the pinned levers/assumptions
       → COMMIT

COMPUTE (no transaction, no session in scope)
       → clone the frozen baseline
       → apply the typed levers atomically
       → engine.run(clone) → scenario tax
       → objective over the frozen baseline and the hypothetical
       → support score over the registry-validated assumptions

TX-2   persist the sealed evidence and scenario_result_hash, atomically
TX-3   on failure, a sanitized reason code and no result children
```

The snapshot is resolved **first**, before the rule snapshot and before the manifest,
because everything downstream describes that input or it describes nothing. The spec hash is
computed from the pins and then *bound* onto the frozen input; identity can only follow the
things it identifies.

## 6. Failure vocabulary

The underlying checks are 3A's, so the reasons are translated rather than re-invented:

| Analysis reason (3A) | Scenario reason (3B) |
|---|---|
| `PINNED_SNAPSHOT_UNAVAILABLE` | `PINNED_SCENARIO_SNAPSHOT_UNAVAILABLE` |
| `PINNED_SNAPSHOT_INCOMPLETE` | `PINNED_SCENARIO_SNAPSHOT_INCOMPLETE` |
| `PINNED_SNAPSHOT_HASH_MISMATCH` | `PINNED_SCENARIO_SNAPSHOT_HASH_MISMATCH` |
| `PINNED_SNAPSHOT_SCHEMA_UNSUPPORTED` | `PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED` |
| `PINNED_BASELINE_RESULT_UNAVAILABLE` | `PINNED_SCENARIO_BASELINE_UNAVAILABLE` |
| `PINNED_BASELINE_RESULT_HASH_MISMATCH` | `PINNED_SCENARIO_BASELINE_HASH_MISMATCH` |
| `FROZEN_INPUT_RECONSTRUCTION_FAILED` | `SCENARIO_FROZEN_INPUT_RECONSTRUCTION_FAILED` |

An operator reading an alert can tell which workflow refused from the code alone, and a
client can offer the right remedy — "re-run the analysis" for an unsupported legacy format
is a different action from "the snapshot no longer matches its hash".

## 6b. Integrity-state semantics (closeout)

Four verdicts, and the distinctions between them are the point. Three have different
remedies; one is an accusation.

| Case | `integrity_status` | `integrity_reason_code` | `integrity_state` (user-visible) | Meaning |
|---|---|---|---|---|
| Replay reproduced the sealed identity | `verified` | `NONE` | `verified` | The recorded calculation still reproduces from its own pinned inputs. |
| A **frozen-policy** scenario replayed and produced a different identity | `mismatch` | `RESULT_HASH_MISMATCH` | `non_reproducible` | A genuine deterministic replay regression. The only case that may be reported as non-reproducible. |
| A pinned artifact is missing, replaced, malformed or fails its identity check | `unavailable` | `BASELINE_SNAPSHOT_UNAVAILABLE`, `BASELINE_RESULT_UNAVAILABLE`, `PINNED_RULE_SNAPSHOT_UNAVAILABLE`, `SEALED_EVIDENCE_INCOMPLETE`, … | `unavailable` | Nothing was compared. Fixable: when the dependency returns, the next sweep verifies. |
| The result predates the frozen-baseline guarantee | `unavailable` | `LEGACY_EXECUTION_POLICY_UNVERIFIABLE` | `legacy_unverifiable` | It was computed from live sources that were never recorded. Not fixable and not a fault — re-run it to obtain a verifiable result. |

**Why legacy is not a mismatch.** Replaying a legacy scenario compares its sealed numbers
against a snapshot it was never computed from. The difference that produces is not evidence of
regression — the guarantee did not exist yet. Recording it as `mismatch` would assert a fault
nothing has demonstrated, and would bury genuine regressions inside a population of old rows
that can only grow. `_refuse_legacy()` in `replay/services.py` short-circuits before any
dependency work, for scenarios, optimizations and portfolios alike (a portfolio inherits its
run's policy).

**Why legacy is not an ordinary `unavailable` either.** `LEGACY_UNVERIFIABLE` is a distinct
user-visible state with its own wording, because "a pinned dependency is unavailable" tells a
user to wait for something that is never coming.

The three requirements for an umbrella status are met:

1. **Machine-readable.** `LEGACY_EXECUTION_POLICY_UNVERIFIABLE` is its own enumerated reason,
   admitted to the `ioe.integrity_check` CHECK constraint by migration `0040`, and excluded from
   `ck_integrity_check_mismatch_reason` by construction.
2. **Not counted as failure.** `IntegrityMetrics` carries `legacy_unverifiable` and derives
   `unavailable_dependency = unavailable − legacy_unverifiable`, so a dashboard reports the
   actionable count without knowing the reason vocabulary.
   `test_readiness_counting_can_exclude_legacy_from_genuine_failures` asserts a legacy scenario
   contributes zero to the mismatch count.
3. **Preserved in the API and the docs.** `IntegrityOut` carries `integrity_status`,
   `integrity_reason_code`, `integrity_state` and `execution_policy`, and `integrity_warning`
   is reason-aware — the legacy text says "Nothing indicates it is wrong", the mismatch text
   says "could not be reproduced … under review".

## 6c. Policy validation (closeout)

`scenario_execution_policy(manifest)` is strict, and it is the only reading of the key —
presentation, sealing and replay all call it.

| Stored value | Interpretation |
|---|---|
| key absent (or manifest `None`) | `live_baseline_legacy` — the **only** tolerated silence |
| `"frozen_snapshot_v1"` | current policy |
| `"live_baseline_legacy"` | legacy, stated explicitly |
| unknown string, wrong case, unsupported version | **fails closed** — `SCENARIO_EXECUTION_POLICY_INVALID` |
| explicit `null`, number, array, nested object | **fails closed** |
| manifest itself not an object | **fails closed** |

A malformed value never degrades to `live_baseline_legacy`. A corrupt manifest is not a
historical manifest, and filing it beside genuinely old rows is how a defect stops being looked
at. In replay it becomes `unavailable / SEALED_EVIDENCE_INCOMPLETE`, never a legacy verdict.

Absence is a *reliable* marker rather than a guess because `assert_current_scenario_policy()`
runs in TX-1 on the manifest the service has just built: after that line no scenario can be
created without an explicit, current, well-formed policy, so any stored row lacking one was
necessarily written before the policy existed.

**Immutability.** `version_manifest` is in the sealed column list of `trg_guard_transition`, so
the policy cannot be changed once `workflow_status = 'completed'`.
`test_the_sealed_policy_cannot_be_changed_after_completion` asserts the database refuses the
UPDATE. The manifest is also hashed into `manifest_hash`, which is itself sealed and enters
`scenario_spec_hash`, so a policy change would additionally invalidate the scenario's identity.

## 6d. Hash coverage (closeout)

Normative order: **pins → `scenario_spec_hash` → `frozen.bind(spec_hash)` → idempotency**.
Every execution-relevant component is committed before idempotency resolves.

| Component | Reaches the identity via |
|---|---|
| Frozen baseline input identity | `baseline_input_snapshot_hash` (top-level argument) |
| Baseline result identity | `{"baseline_result_hash": …}` in the canonical input, **and** the manifest |
| Objective code / version | `{"objective_code", "objective_version"}`, **and** the manifest |
| Lever-registry version | `lever_registry_version` argument, **and** the manifest |
| Assumption-registry version | manifest → `manifest_hash` |
| Support-score version (`confidence_algorithm_version`) | manifest → `manifest_hash` |
| Engine version / reference data | `engine_version`, `engine_config_version` arguments, **and** the manifest |
| Rule-version set and snapshot hash | `rule_version_set`, `reference_data_versions` |
| Snapshot schema version | manifest → `manifest_hash` |
| **Frozen-baseline execution policy** | manifest → `manifest_hash` |
| Tax year, jurisdiction | top-level arguments |

`test_every_execution_relevant_pin_changes_the_scenario_identity` moves each of these and
asserts the identity moves with it; `test_non_semantic_metadata_does_not_change_the_identity`
asserts label and note do not. Idempotency resolves against `scenario_spec_hash` and never
against raw request JSON, so two requests differing only in label replay the same scenario
while a different policy, registry or baseline is a different scenario.

## 7. API surface

`ScenarioBaselineUnavailable` (`409`,
`https://onyx.ledger/errors/scenario-baseline-unavailable`) carries the enumerated reason
code as its entire detail. 409 rather than 5xx because the condition is about the state of
the pinned analysis, not about the service: re-running the analysis is the remedy, and
retrying the same request will fail identically until then. The payload that failed is the
user's financial data and does not travel with the error.

`IntegrityOut` gains `execution_policy`. An optimization reads it from its
`input_execution_policy_version` column; a scenario reads it from its version manifest,
where **absence means legacy** — see §10. A portfolio reports `null`: its policy is its
run's, and inventing one here would be a claim nothing backs. A stored manifest that is
malformed rather than merely old reports `SCENARIO_EXECUTION_POLICY_INVALID`: a read must not
500 on one bad row, and it must not pretend the row is ordinary either.

**Internal diagnostics.** The 409 body carries one enumerated code, which is correct and also
useless to an operator at 3am. `_record_baseline_failure()` emits a sanitized structured log —
`user_id`, `analysis_id`, `failure_stage`, `expected_artifact`, `expected_artifact_id`,
`expected_snapshot_hash`, `analysis_status`, `execution_policy`, `reason_code`. Every field is
an identifier, an enumerated value or a **content hash**: a snapshot hash names a payload
without revealing a byte of it, which is exactly the property wanted for a line that will be
shipped to a log aggregator. `test_the_internal_diagnostic_names_the_snapshot_by_hash_only`
asserts the hash is present and the figures behind it are not.

## 8. Live-access audit

Every read reachable from a scenario computation, after the change:

| Call site | Reads | Verdict |
|---|---|---|
| `_pin_specification` → `FrozenScenarioInputService` | `analysis.analysis_run`, `analysis.analysis_input_snapshot` | pinned evidence |
| `_pin_specification` → `RuleSnapshotService.capture` | `rules.*` published versions | registry, pinned into the snapshot |
| `_compute` | nothing — no session is in scope | — |
| `_persist` | `ioe.*` | own evidence |
| `ScenarioComparisonService` | `ioe.scenario`, `ioe.scenario_result` | sealed rows only |
| `ScenarioFreshnessService.current_state` | live sources **deliberately** | see below |

`ScenarioFreshnessService` is the one remaining live reader and it must stay one. Its whole
job is to answer "has the world moved since this was sealed?", which requires reading the
world. It compares and labels; it never recomputes a stored result. That boundary is the
same one item 3A drew for read-time freshness comparison.

## 9. Structural enforcement

Four layers, none of them a comment:

1. **Object boundary** — `FrozenScenarioExecutionInput` is frozen and exposes no session,
   repository or loader (`test_the_pinned_scenario_spec_carries_no_second_input_channel`).
2. **Absent-field boundary** — `PinnedScenarioSpec` has no `baseline_inputs` field, so there
   is nothing for a live query to fill.
3. **AST boundary** — `test_the_scenario_module_never_calls_a_live_input_builder` parses
   `scenario/service.py` and fails if `build_input`, `build_input_from_live_sources` or
   `_build_input_live` is called.
4. **Query boundary** — SQL is captured across the *entire* `simulate()` call, not just the
   compute phase, and the test fails on any statement naming `finance.`, `profile.` or
   `wealth.`. The same capture is applied to `ScenarioComparisonService.compare`.

## 10. Historical scenarios

`ScenarioExecutionPolicy` is a **separate** enum from `InputExecutionPolicy`. Optimizations
and scenarios were corrected at different commits, and one shared value would let a scenario
inherit a guarantee it never had.

The policy lives in the version manifest (JSONB), so no migration is required.
`scenario_execution_policy(manifest)` reads absence as `live_baseline_legacy`. Pre-correction
scenarios are **not** backfilled as corrected: claiming a guarantee those scenarios never had
would be worse than the original defect, because it would be undetectable afterwards. They
may honestly report `integrity_status = mismatch` or `unavailable`. A user or operator may
refresh an old scenario, which creates a new one under the corrected policy and links through
the existing supersession fields.

**Deployment cutoff:** the commit introducing `scenario_execution_policy_version`. Every
scenario created after it is `frozen_snapshot_v1`. Operators can count the affected
population directly:

```sql
SELECT count(*) FROM ioe.scenario
WHERE coalesce(version_manifest ->> 'scenario_execution_policy_version',
               'live_baseline_legacy') <> 'frozen_snapshot_v1';
```

## 11. Replay integration

`ScenarioReplayService` now resolves its baseline through the same
`FrozenScenarioInputService`, passing `expected_snapshot_hash` from the sealed scenario. Two
consequences, both deliberate:

- A **replaced or corrupt** snapshot is an `unavailable` dependency, not a mismatch. Nothing
  was compared, so nothing may be reported as non-reproducible.
  `ReplayDependencyResolver.for_scenario` performs the same comparison at the dependency
  layer, mapping to `BASELINE_SNAPSHOT_UNAVAILABLE`.
- The **baseline result identity is not asserted** during replay. The baseline is re-derived
  from the frozen snapshot exactly as production derives it, so a baseline that no longer
  reproduces changes `tax_delta` and surfaces as a result-hash **mismatch** — the honest
  verdict, and the one a legacy live-baseline scenario earns.

## 12. Worked example — measured on both trees

The same script, the same clean database, run against a git worktree at the item-3A commit
and against the corrected tree. Snapshot income 95,000; the user then edits live income to
310,000; one `INCREASE_RRSP_DEDUCTION(5,000)` lever.

| | Pre-3B (`040ec65`) | Post-3B |
|---|---:|---:|
| Baseline recomputed from the pinned snapshot | 20,145.04 | 20,145.04 |
| **Sealed `scenario.baseline_tax`** | **122,045.69** | **20,145.04** |
| Sealed `tax_delta` | 2,676.48 | 1,516.59 |
| Verdict | baseline came from live data, sealed under the snapshot's hash | baseline is the pinned snapshot |

The pre-3B row is the defect in one number: a scenario whose
`baseline_input_snapshot_hash` names a 95,000 snapshot, reporting a baseline of 122,045.69
and a saving measured against it. The user would have been shown a 2,676.48 "saving" derived
from a baseline no stored evidence described.

That is `test_a_live_financial_change_after_pinning_cannot_affect_a_scenario`,
`test_the_hypothetical_is_the_snapshot_plus_the_lever_and_nothing_else` and
`test_a_scenario_computed_against_a_changed_live_state_verifies`. Before this correction the
first two computed 310,000's numbers and the third reported `mismatch`.

## 12b. Lever validation

Every hypothetical mutation goes through the pinned lever registry; there is no
path from a request to an engine field name.

| Rejected | Where |
|---|---|
| Unknown lever code | `ScenarioSpec.parse` — refused before the service sees it |
| Unknown or missing parameter | `lever_registry.apply` |
| Out-of-bounds parameter | registry bounds per lever |
| Non-numeric / float monetary value | parameters are `Decimal`; a float is rejected |
| Mutation outside the lever's declared field set | each lever declares a closed writable allow-list |
| Arbitrary patches, JSONPath, formulas, expressions, callables, SQL | no such channel exists — parameters are typed scalars |
| Unknown engine field | `to_tax_input` refuses any key the engine does not define, so a lever that produced one cannot be silently dropped |

`apply_all` is atomic: a failure part-way raises and leaves the clone untouched,
so a half-applied composite lever never reaches the engine. The frozen baseline
is never the thing a lever mutates — `_compute` takes a second clone afterwards
and asserts it equals the first.

The **pinned** `lever_registry_version` enters `scenario_spec_hash`, so a
scenario sealed under one registry cannot be replayed under another without the
identity changing. Unit coverage:
`tests/unit/ioe/test_levers_and_assumptions.py` (14 lever tests).

## 12c. Assumption validation

Assumptions reach the support score only through
`assumption_registry.build(...)`, which resolves materiality, certainty and
eligibility impact from the registry rather than from whatever the caller
asserted. Unregistered codes, wrong types and out-of-bounds values are refused.
`display_note` is excluded from the canonical form, so rewording a note cannot
change identity. `assumption_registry_version` is pinned into the manifest and
therefore into `scenario_spec_hash`.

The four categories stay distinct and are never merged: an **authoritative
frozen fact** (in the snapshot), a **user-declared assumption** (registry-typed,
in the spec), a **system projection assumption** (governed by
`projection_methodology_version`), and a **derived output** (the sealed result).

## 12d. Worked examples

**Financial race** — `test_a_live_financial_change_after_pinning_cannot_affect_a_scenario`
```
snapshot A pinned → scenario TX-1 completes → live income changes to 310000
→ baseline still reconstructs from A → registered lever applies to a clone of A
→ scenario seals → production replay returns verified
```

**Profile race** — `test_a_live_profile_change_after_pinning_cannot_affect_a_scenario`
and `test_two_live_profile_states_give_one_scenario_identity`
```
snapshot A pinned → scenario TX-1 completes → province ON becomes BC, single becomes married
→ the scenario still uses A's jurisdiction and figures → the result is deterministic
   (BC/married and AB/single produce one identity: spec hash, result hash,
    baseline tax, scenario tax, support score and every support component)
→ production replay returns verified
```

**Unsupported legacy snapshot** — `test_an_unsupported_legacy_snapshot_is_counted_for_operators`
```
legacy flat snapshot → PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED
→ refusal metric increments by exactly one → the engine does not run
→ no live fallback → no partial scenario evidence
```

**Incomplete current snapshot** — `test_an_incomplete_snapshot_fails_closed`
```
supported self-describing schema, required field missing
→ PINNED_SCENARIO_SNAPSHOT_INCOMPLETE (never SCHEMA_UNSUPPORTED)
→ the engine does not run → no live fallback → no partial scenario evidence
```

## 13. Preserved behaviour

`TaxEngineService` remains the only tax-calculation authority; the change is which builder
produces its input. `RulesEvaluatorService` remains the only eligibility authority and still
receives the pinned rule-version set. Levers still arrive as typed registry codes, `apply_all`
is still atomic, the baseline is still cloned and never mutated, historical results are still
never recomputed in place, and a refresh still creates a new scenario. Support scores still
run through the registry-validated assumptions, and the display cap is unchanged. Ownership
checks, RLS, parent-qualified reads and canonical idempotency are untouched.

## 14. Security and privacy

The reconstructed snapshot is held in memory for the duration of a simulation and is copied
into no scenario result row, no applied-change trace, no version manifest, no integrity-check
row, no operational event, no log and no queue message. The applied-change trace records
field names and the values the lever registry produced — not the user's other inputs.
Workflow errors carry only the closed reason code.
`test_no_snapshot_payload_reaches_logs_or_sealed_evidence` places a synthetic SIN-like value
inside a snapshot and asserts it appears in no log record, no manifest and no change row.

## 15. Migration

The item itself needed none: the execution policy and the baseline-result pin both live in the
existing `version_manifest` JSONB column and on the existing `scenario.baseline_result_hash`
column.

The **closeout** added one, `0040_legacy_integrity_reason` → `db/sql/34_legacy_integrity_reason.sql`,
because the reason enumeration on `ioe.integrity_check` is a closed CHECK constraint and
`LEGACY_EXECUTION_POLICY_UNVERIFIABLE` could not be recorded without widening it. Reusing an
existing reason was rejected: none of them means "legacy", and the whole purpose is a
machine-readable distinction.

Additive in effect — one CHECK widened by one value, one index added. Every row valid under the
old constraint is valid under the new one, so the constraint rewrite is a validation pass with
no data change and no lock beyond the ALTER itself. Forward-only downgrade, consistent with the
chain.

## 16. Performance

Container reference environment, one scenario with one `INCREASE_RRSP_DEDUCTION` lever,
20 timed simulations. Both columns measured with the same script on a **freshly provisioned
database** — the "before" figures come from a git worktree at the item-3A commit, not from
memory.

| Metric | Before 3B | After 3B | Note |
|---|---:|---:|---|
| SQL statements per `simulate()` | 30 | **25** | reading the snapshot replaces the live reads |
| — reads / inserts / updates | 19 / 9 / 2 | 14 / 9 / 2 | writes unchanged |
| Engine runs per scenario | 2 | 2 | baseline + hypothetical, unchanged |
| Mutable-source statements | 3 | **0** | `profile.tax_profile`, `finance.income_source`, `finance.expense_record` |
| Scenario p50 / p95 | 30.4 / 35.2 ms | 29.4 / 32.4 ms | |

The statement count went **down**, not up. Reconstructing the snapshot is a single
`session.get` that TX-1 was already positioned to make; the three live reads it replaces
were more.

These are unix-socket numbers and are not a managed-database claim. What transfers is the
round-trip count: 25 per simulation, so ~0.05 s of waiting at a 2 ms RTT.

## 16b. Deployment cutoff

The commit introducing `scenario_execution_policy_version` (`d97f86a`). Every
scenario created after it is `frozen_snapshot_v1`; every scenario created before
it has no policy key and reads as `live_baseline_legacy`. There is no backfill
and no date arithmetic — the presence of the key is the cutoff, which is why
`assert_current_scenario_policy` refuses to seal a scenario without one.

## 16c. Scheduled-verifier interaction

**Overlap.** Healthy measured cycles complete well before the configured
interval. Concurrent cycles remain possible under slow execution, queue delay,
deployment interruption, or timeout conditions. Record-level active-check
arbitration preserves correctness when overlap occurs — timing is never relied
on.

**Fairness.** Global oldest-first ordering provides deterministic eventual
rotation, but it is not a strict per-tenant fairness guarantee.

**Retry.** Mismatch, unavailable and legacy-unverifiable records currently
return to the rotation on the same terms as any other record. Reason-aware
backoff or quarantine is **future operational work, not implemented behaviour**.

**Portfolio coverage.** Portfolios are not sampled by the scheduler —
`ioe.claim_integrity_targets` rejects the type. A portfolio is verified when an
operator or client requests it through the existing verification interface, and
its sealed evidence is also exercised indirectly by its run's optimization
replay. A portfolio that is never explicitly requested **can remain
`not_checked` indefinitely**. That is the limit of the guarantee.


Scheduled verification (`workers.tasks.ioe.verify_sealed_integrity`, closure
entry 8C) samples scenarios. During this correction's rollout the estate holds
both policies, so:

- **Corrected scenarios** replay and return `verified` immediately.
- **Legacy scenarios** are refused *before* any dependency resolution or engine
  run and recorded as `unavailable / LEGACY_EXECUTION_POLICY_UNVERIFIABLE` —
  never `mismatch`, because nothing was compared on equal terms, and never
  `verified`, because nothing was reproduced.
- **Genuine mismatch states are not suppressed.** A corrected scenario that
  stops reproducing still reports `mismatch` / `non_reproducible`.

**Decision on legacy sampling:** option 1 — legacy scenarios continue to rotate
under the ordinary age ordering, and are *explicitly classified* rather than
excluded. Excluding them would need a new predicate inside
`ioe.claim_integrity_targets` (a migration) to filter on a JSONB manifest key,
and would hide the size of the legacy population instead of measuring it. The
cost is one claim slot per legacy record per rotation, and each such check is
engine-free. `integrity_batch_legacy_unverifiable` measures exactly that, so the
decision is reversible on evidence rather than on assumption.

## 16d. Snapshot failure classification

Four distinct snapshot conditions, four distinct codes. They are not collapsed
because they call for different operator action.

| Code | Condition | Operator action |
|---|---|---|
| `PINNED_SCENARIO_SNAPSHOT_UNAVAILABLE` | the row does not exist, or is not an object | investigate deletion / restore |
| `PINNED_SCENARIO_SNAPSHOT_HASH_MISMATCH` | the payload no longer matches its stored canonical hash | treat as tampering or corruption |
| `PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED` | the payload names a schema this reconstructor does not know, **including old flat pre-self-describing snapshots** | re-run the analysis |
| `PINNED_SCENARIO_SNAPSHOT_INCOMPLETE` | the schema is recognised and the hash is intact, but a required engine field is absent or structurally invalid | re-run the analysis; investigate the writer |

Incomplete is never reported as unsupported, and unsupported is never reported
as a generic reconstruction failure. The exception carries the code and nothing
else — no payload, no exception text, nothing destined for storage.

## 16e. Legacy-snapshot refusal metric

`BASELINE_REFUSAL_COUNTS` (`scenario/service.py`) counts refused scenario
baselines keyed by enumerated reason, following the same in-process counter
pattern as `IntegrityMetrics` in `replay/events.py` — not a second metrics
framework.

The one an operator watches during the legacy transition is
`PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED`, which counts analyses predating
the self-describing format. Semantically the counter is:

```
operation="scenario_create"  reason=<enumerated snapshot reason>  execution_policy="legacy"
```

Bounded and low-cardinality by construction: the key space is the closed reason
enumeration. **No** user id, scenario id, analysis id, snapshot id or hash, no
income or tax amount, no personal information, no exception string. Each refusal
increments exactly one key; a refusal for a *different* reason increments that
reason's key, never the legacy one.

## 17. Remaining limitations

1. Snapshots written before the self-describing format fail closed as
   `PINNED_SCENARIO_SNAPSHOT_SCHEMA_UNSUPPORTED`. Those analyses must be re-run before they
   can be simulated. That is the honest consequence of making the snapshot format
   self-describing, and it is the same limitation item 3A recorded.
2. `ScenarioFreshnessService` reads live sources by design (§8). If freshness producer wiring
   later moves that comparison off the read path, this is the module to revisit — it is out
   of scope for this item.
3. Household and contribution-room resolution still contribute no inputs. If they are added
   later they must enter the analysis snapshot rather than being read live by either
   workflow. The SQL-capture boundary across the whole of `simulate()` is what would catch
   a future implementation introducing one.
4. **Scenarios do not evaluate rules.** `ScenarioService` pins a rule snapshot and seals the
   pinned version set, but it never calls `RulesEvaluatorService`; a scenario reports a tax
   delta and a support score, not an eligibility difference. Baseline/hypothetical
   `facts_for` + `evaluate` on the frozen inputs would be a **new capability**, not a
   correction of a live-data leak, and is deliberately out of scope for this item. The pins
   it would need are already sealed, so adding it later changes no identity contract.
5. **The economic result is not fully decomposed.** `scenario_result` carries `tax_delta`,
   `objective_value_baseline/scenario/delta` and `net_benefit`, with the cost taxonomy
   applied on the optimization side. The wider structured breakdown — refund-or-balance,
   refundable-benefit, non-refundable-credit, deferral, asset transfer, non-recoverable
   expenditure, liquidity commitment, contribution room and loss pool consumed, future
   projected effect — would require new columns and a migration, and is not a frozen-input
   concern. Recorded here so it is not mistaken for done.
