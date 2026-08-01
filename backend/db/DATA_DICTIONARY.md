# Onyx Ledger — Data Dictionary

Per-table reference. Every table also carries `id UUID` (UUIDv7) PK unless
noted, `created_at`/`updated_at timestamptz`, and (for user-owned aggregates)
`deleted_at` for soft delete. Money = `NUMERIC(14,2)`; rate = `NUMERIC(9,6)`.

## `ref` — reference data
| Table | Purpose | Key columns |
|-------|---------|-------------|
| jurisdiction | Federal + provinces/territories | code, level |
| province | Province tax traits | code, has_surtax, has_health_premium, federal_abatement |
| currency | ISO-4217 currencies | code (PK), minor_unit |
| tax_year | Year metadata | year (PK), indexation_factor, status |
| residency_status / marital_status / employment_type / housing_status / verification_status | Small code/label lookups | code (PK) |
| income_type | Income kinds + inclusion/gross-up | code, default_inclusion, is_gross_up |
| expense_category | Hierarchical expense categories | code, parent_id, is_credit, cra_line |
| asset_category | Asset kinds + registered traits | code, is_registered, is_contribution_limited |
| account_registered_type | TFSA/RRSP/FHSA/RESP | code (PK) |
| liability_category | Liability kinds | code |
| document_type | Slip/receipt types | code, category |
| rule_category | credit/deduction/benefit/bracket/limit/threshold | code (PK) |
| condition_operator | Operators the engine supports | code (PK), arity |

## `identity`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| user_account | Hub A root | email (citext), status, deleted_at, row_version |
| user_credential | Argon2id hash, 1:1 | password_hash, must_reset |
| auth_session | Refresh sessions | refresh_token_hash (hash only), expires_at, revoked_at |
| login_event | Append-only login history | event_type, ip_address |
| password_reset_token / email_verification_token | One-time tokens | token_hash, expires_at, used_at |
| mfa_method | MFA readiness | method_type, secret_kms_ref (KMS pointer) |

## `profile`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| user_profile | Display/locale | display_name, locale, timezone |
| tax_profile | Personal/employment/housing facts (1:1) | province_code, marital_status, is_self_employed, owns_home, has_* flags |
| dependent | Dependents (multi-valued) | relationship, date_of_birth, has_disability |
| spouse_profile | Spouse facts (1:1) | has_spouse, spouse_net_income |
| user_preference | Evolving prefs | notification_prefs jsonb, ui_prefs jsonb |
| user_privacy_setting | Consent/retention | marketing_consent, data_retention_years |

## `finance` (LIST-partitioned by tax_year; PK = (id, tax_year))
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| income_source | Per-year income rows | income_type_id, amount, frequency, verification_status |
| expense_record | Per-year expense rows | expense_category_id, amount, incurred_on, receipt_document_id |

## `wealth`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| asset | Balance-sheet assets | asset_category_id, acquisition_cost, current_value (cached) |
| asset_valuation | Value time-series | as_of_date, value |
| registered_account_detail | TFSA/RRSP/FHSA/RESP subtype (1:1) | registered_type, contribution_room, contributions_ytd |
| liability | Debts | liability_category_id, current_balance (cached), interest_rate, status |
| liability_balance | Balance time-series | as_of_date, balance |

## `tax_kb`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| gov_source / legislation_reference | Normalized citations | name/citation, url |
| tax_rule | Stable rule identity | code, category, jurisdiction_id, province_code |
| tax_rule_version | Immutable, bitemporal version | tax_year, effective/expiry_date, status, formula_id, superseded_by_version_id; **unique published per (rule, year)** |
| tax_bracket_set / tax_bracket | Tabular brackets | lower_bound, upper_bound (NULL=∞), rate, ordinal |
| contribution_limit | Registered limits per year | annual_limit, percent_of_income, allows_carryforward |
| benefit_program / benefit_parameter | Government benefits | code, is_refundable, param_key/value/rate |

## `rules` (the engine)
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| fact_definition | Fact catalog | fact_key (unique), data_type, unit |
| calc_formula | Versioned math | code, expression, expression_lang |
| calc_formula_input | Param → fact/literal binding | param_name, fact_key, literal_value |
| calc_constant | Named constants per year | code, tax_year, value |
| rule_condition_group | Boolean AST node | logical_op (AND/OR/NOT), parent_group_id |
| rule_condition | Leaf comparison | fact_key, operator, value_* , value_set_id |
| condition_value_set / _item | Set RHS for in/contains | value_text |
| rule_outcome | THEN half | outcome_type, impact_formula_id, where/how/why templates |

## `analysis`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| analysis_run | Immutable analysis | engine_version, taxable_income, estimated_tax, confidence_score |
| analysis_input_snapshot | Frozen inputs (1:1) | snapshot jsonb, snapshot_hash |
| analysis_line_item | Computed breakdown | kind, label, amount, tax_rule_version_id (citation) |
| reconciliation_check | Assurance checks | check_code, status (pass/review/flag), detail |
| analysis_assumption | Applied assumptions | text |

## `reco`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| recommendation | Opportunity + citation | tax_rule_version_id, where/how/why_text, estimated_impact, status |
| recommendation_status_event | Lifecycle log | status, actor_user_id |
| recommendation_feedback | User rating | rating, comment |

## `ai`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| ai_conversation | Chat thread | title |
| ai_message | Chat turn | role, content, model, confidence_score |
| ai_message_citation | Grounding | tax_rule_version_id / analysis_id / recommendation_id |
| ai_prompt_context | Retrieved context | context jsonb |
| ai_explanation | Generated explanation | tax_rule_version_id, reviewed_by_admin_id |
| knowledge_embedding | Vector store | source_type/source_id, tax_year, embedding vector(1536) |

## `docs`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| document | Object-store ref (no bytes) | bucket, object_key, content_hash, status |
| document_extraction | OCR run | engine, status, confidence |
| extraction_field | Extracted field | field_name, fact_key, value_*, bounding_box jsonb |
| document_link | Provenance to income/expense | composite FKs into finance partitions |

## `admin`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| admin_user | Internal staff | email, password_hash |
| role / permission / role_permission / admin_user_role | RBAC | code |
| rule_change_request | KB governance | action, status, submitted_by/reviewed_by (four-eyes) |
| rule_publication | Publish record | published_by, published_at |

## `billing`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| plan | Subscription plans | code, price_amount, features jsonb |
| subscription | User subscription | status, provider_subscription_id (unique active per user) |
| invoice | Invoices | amount, status, provider_invoice_id |
| payment_method_ref | Tokenized method (no PAN) | provider_method_id, brand, last4 |
| entitlement | Derived feature flags | features jsonb |

## `audit`
| Table | Purpose | Notable columns |
|-------|---------|-----------------|
| audit_log | Immutable, RANGE-partitioned by month | actor_type/id, action, entity_*, previous/new_value jsonb; append-only |
| consent_log | Consent events | consent_type, granted, version |
| data_export_request / data_deletion_request | PIPEDA rights | status, requested/completed_at |
| security_event | Anomaly log | event_type, severity, detail jsonb |
