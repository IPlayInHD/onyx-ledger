# Entry 11B5H3 — sealed-artifact inventory (§3, §4)

Read from the live schema (`information_schema`, `pg_proc`) and from the replay
services at `833ece3`. Not from design notes.

## Replay entry points that actually exist

`app/services/ioe/domain/integrity.py`:

```python
class EntityType(StrEnum):
    OPTIMIZATION = "optimization"
    PORTFOLIO    = "portfolio"
    SCENARIO     = "scenario"
```

Three, and **no `ANALYSIS`**. The single production entry point is
`IntegrityVerificationService(user_id).verify(entity_type, entity_id)`, which
dispatches to `OptimizationReplayService` / `ScenarioReplayService` /
`PortfolioReplayService`.

## Matrix

| Artifact | Table | Hash column(s) | Serialized payload | Replay entry | Live-source dependency | Class |
|---|---|---|---|---|---|---|
| Analysis input snapshot | `analysis.analysis_input_snapshot` | `snapshot_hash` | `snapshot` (jsonb) | consumed by `ReplayDependencyResolver.baseline_input`; not verifiable on its own | **none after sealing** | SEALED_RETAINED_NO_REPLAY_ENTRYPOINT |
| Analysis run | `analysis.analysis_run` | — | — | none | none | SEALED_RETAINED_NO_REPLAY_ENTRYPOINT |
| Optimization run | `ioe.optimization_run` | `optimization_result_hash`, `optimization_spec_hash`, `manifest_hash` | `version_manifest`, `user_constraints`, `assumption_set` (jsonb) | `verify("optimization", run_id)` | none — baseline comes from the sealed snapshot | **PRODUCTION_REPLAY_SUPPORTED** |
| Scenario | `ioe.scenario` | `scenario_result_hash`, `scenario_spec_hash`, `baseline_input_snapshot_hash`, `baseline_result_hash`, `manifest_hash` | lever/assumption child rows | `verify("scenario", scenario_id)` | none | **PRODUCTION_REPLAY_SUPPORTED** |
| Strategy portfolio | `ioe.strategy_portfolio` | `portfolio_result_hash` | member/exclusion child rows | `verify("portfolio", portfolio_id)` | none | DERIVED_FROM_REPLAYABLE_PARENT (anchors on its run's spec hash) |
| Rule snapshot pin | `ioe.run_rule_snapshot` → `ioe.rule_snapshot` | `snapshot_hash` | `pinned_rule_version_ids` | `ReplayDependencyResolver.rule_snapshot` | none | pinned dependency |
| Rule snapshot artifact | `ioe.rule_snapshot_artifact` | `content_hash` | artifact body | resolver | none | pinned dependency |
| Assumption set | `ioe.assumption_set` | `set_hash` | — | resolver | none | pinned dependency |
| Version manifest | `ioe.optimization_run.version_manifest` / `ioe.scenario.manifest_hash` | `manifest_hash` | `version_manifest` (jsonb) | `ReplayDependencyResolver.check_versions` | none | pinned dependency |
| Integrity check | `ioe.integrity_check` | `expected_result_hash`, `actual_result_hash`, `expected_spec_hash` | — | written by `verify`; append-only evidence | none | NOT_RELEVANT_TO_H3 (evidence, not seal) |

## Why analysis has no replay entry point (§21)

`AnalysisInputSnapshot` is the *input* to replay, not a replayable artifact.
`ReplayDependencyResolver.baseline_input` reads it and reconstructs a `TaxInput`
through the shared codec. Its docstring states the contract directly:

> "This is the difference between verifying a historical result and recomputing
> a new one. `TaxEngineService.build_input` reads live financial tables; a
> replay that used it would report a mismatch every time a user edited last
> year's income, which says nothing about whether the sealed result was
> reproducible."

So analysis is exercised *indirectly*, every time an optimization or scenario
replay resolves its baseline. H3 tests that path rather than inventing an
`EntityType.ANALYSIS`.

## Live source tables that replay must not touch (§16, §17)

From the Entry 11B5 classification matrix, the governed live SOURCE_DATA tables
whose reads would constitute a fallback:

```
finance.income_source
finance.expense_record
profile.tax_profile
profile.user_profile
profile.spouse_profile
profile.dependent
```

The three that carry values used by `_build_input_live` — `income_source`,
`expense_record`, `tax_profile` — are the ones a fallback would actually read.
