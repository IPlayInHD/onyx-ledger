# ACCOUNT_DELETE_CASCADE_UNIVERSE — certified input set

The exact set of tables reachable from `identity.user_account` through
all-CASCADE paths, computed with the corrected walker (recurse only when the
edge being traversed is `confdeltype='c'`) and independently reconfirmed.

    cascade-reachable tables   72
    maximum depth               3 (shortest-path representation)
    direct inbound FKs         39  (35 CASCADE, 4 SET NULL)

Widened from 70 by the Tax Decision Journal entry (migration 0068), which adds
`ioe.decision_journal` (depth 1) and `ioe.decision_journal_event` (depth 2) —
classified LIVE_USER_DATA_DELETE before the migration shipped, per the registry
workflow.

Superseded figures: **73 tables**, **31 protected**, **6-edge cut-set**. Those
came from the walker that gated on the inbound edge's action and are not to be
reused.

## By schema

| schema | tables |
|---|---|
| `ioe` | 27 |
| `finance` | 8 |
| `profile` | 6 |
| `identity` | 5 |
| `wealth` | 5 |
| `analysis` | 5 |
| `ai` | 4 |
| `docs` | 4 |
| `billing` | 4 |
| `reco` | 3 |
| `audit` | 1 |

## The set

Classification column is deliberately empty. It is filled only from evidence —
writers, readers, immutability, replay dependency — never from the schema name.
An unclassified row is an open item, not a default.

| depth | table | classification |
|---|---|---|
| 1 | `ai.ai_conversation` | — |
| 1 | `analysis.analysis_run` | — |
| 1 | `audit.data_export_request` | — |
| 1 | `billing.entitlement` | — |
| 1 | `billing.payment_method_ref` | — |
| 1 | `billing.subscription` | — |
| 1 | `docs.document` | — |
| 1 | `finance.expense_record` | — |
| 1 | `finance.expense_record_default` | — |
| 1 | `finance.expense_record_y2024` | — |
| 1 | `finance.expense_record_y2025` | — |
| 1 | `finance.income_source` | — |
| 1 | `finance.income_source_default` | — |
| 1 | `finance.income_source_y2024` | — |
| 1 | `finance.income_source_y2025` | — |
| 1 | `identity.auth_session` | — |
| 1 | `identity.email_verification_token` | — |
| 1 | `identity.mfa_method` | — |
| 1 | `identity.password_reset_token` | — |
| 1 | `identity.user_credential` | — |
| 1 | `ioe.decision_journal` | — |
| 1 | `ioe.freshness_outbox` | — |
| 1 | `ioe.integrity_check` | — |
| 1 | `ioe.optimization_run` | — |
| 1 | `ioe.scenario` | — |
| 1 | `profile.dependent` | — |
| 1 | `profile.spouse_profile` | — |
| 1 | `profile.tax_profile` | — |
| 1 | `profile.user_preference` | — |
| 1 | `profile.user_privacy_setting` | — |
| 1 | `profile.user_profile` | — |
| 1 | `reco.recommendation` | — |
| 1 | `reco.recommendation_feedback` | — |
| 1 | `wealth.asset` | — |
| 1 | `wealth.liability` | — |
| 2 | `ai.ai_message` | — |
| 2 | `analysis.analysis_assumption` | — |
| 2 | `analysis.analysis_input_snapshot` | — |
| 2 | `analysis.analysis_line_item` | — |
| 2 | `analysis.reconciliation_check` | — |
| 2 | `billing.invoice` | — |
| 2 | `docs.document_extraction` | — |
| 2 | `docs.document_link` | — |
| 2 | `ioe.decision_journal_event` | — |
| 2 | `ioe.multi_year_projection` | — |
| 2 | `ioe.optimization_candidate` | — |
| 2 | `ioe.optimization_run_event` | — |
| 2 | `ioe.recommendation_relationship` | — |
| 2 | `ioe.run_rule_snapshot` | — |
| 2 | `ioe.run_rule_version` | — |
| 2 | `ioe.scenario_assumption` | — |
| 2 | `ioe.scenario_confidence_component` | — |
| 2 | `ioe.scenario_event` | — |
| 2 | `ioe.scenario_input_change` | — |
| 2 | `ioe.scenario_lever` | — |
| 2 | `ioe.scenario_result` | — |
| 2 | `ioe.strategy_portfolio` | — |
| 2 | `reco.recommendation_status_event` | — |
| 2 | `wealth.asset_valuation` | — |
| 2 | `wealth.liability_balance` | — |
| 2 | `wealth.registered_account_detail` | — |
| 3 | `ai.ai_message_citation` | — |
| 3 | `ai.ai_prompt_context` | — |
| 3 | `docs.extraction_field` | — |
| 3 | `ioe.candidate_cost` | — |
| 3 | `ioe.candidate_economic_effect` | — |
| 3 | `ioe.confidence_component` | — |
| 3 | `ioe.portfolio_evaluation_step` | — |
| 3 | `ioe.portfolio_exclusion` | — |
| 3 | `ioe.portfolio_member` | — |
| 3 | `ioe.resource_ledger_entry` | — |
| 3 | `ioe.score_component` | — |

## Classifications established so far, with evidence

| table | classification | evidence |
|---|---|---|
| `ioe.freshness_outbox` | `DERIVED_DELETE` | its own COMMENT: *"Transactional outbox for freshness invalidation… an event exists if and only if the change committed"*. Delivery infrastructure; no retention or purge semantics anywhere in the schema. |
| `reco.recommendation` | `LIVE_USER_DATA_DELETE` | live user-facing recommendation rows; cascade-reachable at depth 1 by its own FK. The protected table once attributed to it (`ioe.optimization_candidate`) is reached instead through `run_id -> ioe.optimization_run` CASCADE. |

**68 tables remain unclassified.** They are not assumed protected and not
assumed deletable.
