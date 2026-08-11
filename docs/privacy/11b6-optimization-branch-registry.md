# Semantic registry — `ioe.optimization_run` branch

Branch membership derived from the live catalog and cross-checked by two
independent implementations (recursive SQL CTE; Python DFS over a flat edge
inventory). **Both agree: 16 tables, maximum depth 2.**

| depth | tables |
|---|---|
| 1 | `integrity_check`, `multi_year_projection`, `optimization_candidate`, `optimization_run_event`, `recommendation_relationship`, `run_rule_snapshot`, `run_rule_version`, `strategy_portfolio` |
| 2 | `candidate_cost`, `candidate_economic_effect`, `confidence_component`, `portfolio_evaluation_step`, `portfolio_exclusion`, `portfolio_member`, `resource_ledger_entry`, `score_component` |

`ioe.integrity_check` and `ioe.multi_year_projection` are reachable at both
depths (multiple paths), reported at their shortest.

## Classified with direct evidence

| table | classification | protected | evidence |
|---|---|---|---|
| `ioe.optimization_run` (root) | `REPLAY_REQUIRED_RETAIN` | yes | `app/services/ioe/replay/verification.py:187` — `run = await session.get(OptimizationRun, entity_id)`. Replay reads the row itself, so deleting it makes historical verification impossible. `DIRECT_CODE_EVIDENCE`. Corroborated by `COMMENT ON COLUMN ioe.optimization_run.integrity_status`: *"Reproducibility of the SEALED result"*. |
| `ioe.run_rule_snapshot` | `SEALED_IMMUTABLE_RETAIN` (`RULE_VERSION_PIN`) | yes | `tests/security/test_sealed_history_after_purge.py:250` reads `rs.snapshot_hash` through `ioe.run_rule_snapshot` — Entry 11B5H3's certified sealed-history-after-purge proof depends on it surviving. `DIRECT_CODE_EVIDENCE`. |

## Not yet classified — 14 of 16

`integrity_check`, `multi_year_projection`, `optimization_candidate`,
`optimization_run_event`, `recommendation_relationship`, `run_rule_version`,
`strategy_portfolio`, `candidate_cost`, `candidate_economic_effect`,
`confidence_component`, `portfolio_evaluation_step`, `portfolio_exclusion`,
`portfolio_member`, `resource_ledger_entry`, `score_component`.

Each needs its own writer/reader/immutability/replay trace. None is assumed
protected and none is assumed deletable. `ioe.integrity_check` is a genuine
branch member (depth 1) but its semantic review was scoped to a separate slice.

## Structural note, not yet a recommendation

The root is protected, so the branch-specific cut requirement is the edge
`identity.user_account -> ioe.optimization_run`. Whether cutting it is
*sufficient* depends on the 14 unclassified members, and whether it is
*appropriate* depends on the sealed-byte/RLS analysis that follows. No FK model
is proposed here.
