-- =============================================================================
-- Onyx Ledger — 20 · RulesEvaluator Opportunity Contract v2  (schemas: rules, tax_kb)
-- Alembic revision: 0026_rules_contract
-- IOE architecture Revision 2 §6.3 + Revision 2.1 §C, §F.
--
-- The Income Optimization Engine must never interpret legislation. Every legally
-- meaningful field it consumes — eligibility basis, required actions, required
-- documents, dependencies, deadlines, economic classification — is therefore
-- authored as RULE DATA here and published through TKMS four-eyes governance.
-- The IOE consumes these verbatim and never infers them.
--
-- ALL CHANGES ARE ADDITIVE. No drops, no retypes, no data rewrites. New columns
-- are nullable so existing rule versions remain valid; a version with no contract
-- metadata is emitted with eligibility_status='indeterminate' and is excluded
-- from portfolio evaluation rather than guessed at.
-- =============================================================================

-- ---- Required actions: what the user must DO to realize the opportunity -----
CREATE TABLE rules.rule_action (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    rule_version_id     uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    action_code         text NOT NULL,                  -- 'CONTRIBUTE_RRSP','RETAIN_RECEIPTS'
    description         text NOT NULL,
    effort_rating       smallint NOT NULL DEFAULT 3
                        CHECK (effort_rating BETWEEN 1 AND 5),   -- 1 = trivial, 5 = complex
    cost_type           text CHECK (cost_type IN
                            ('required_cash_contribution','required_expenditure','implementation_cost')),
    cost_amount         ref.money_amt,
    deadline_code       text,                           -- soft link to rule_deadline.deadline_code
    sort_order          smallint NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_version_id, action_code)
);
CREATE INDEX ix_rule_action_version ON rules.rule_action (rule_version_id, sort_order);

-- ---- Required documents: evidence the user must hold ------------------------
CREATE TABLE rules.rule_required_document (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    rule_version_id     uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    document_type_code  text NOT NULL REFERENCES ref.document_type(code),
    necessity           text NOT NULL DEFAULT 'required'
                        CHECK (necessity IN ('required','recommended','conditional')),
    note                text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_version_id, document_type_code)
);
CREATE INDEX ix_rule_req_doc_version ON rules.rule_required_document (rule_version_id);

-- ---- Dependencies between rules (ordering / prerequisite relationships) -----
-- Referenced by CODE (stable rule identity), not version id: a dependency is on
-- the rule, and each year's version resolves to that year's published version.
CREATE TABLE rules.rule_dependency (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    rule_version_id     uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    depends_on_rule_code text NOT NULL,                 -- tax_kb.tax_rule.code
    dependency_type     text NOT NULL
                        CHECK (dependency_type IN ('requires','precedes','excludes','substitutes')),
    note                text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_version_id, depends_on_rule_code, dependency_type)
);
CREATE INDEX ix_rule_dependency_version ON rules.rule_dependency (rule_version_id);

-- ---- Applicable deadlines ---------------------------------------------------
CREATE TABLE rules.rule_deadline (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    rule_version_id     uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    deadline_code       text NOT NULL,                  -- 'RRSP_CONTRIBUTION_DEADLINE'
    deadline_date       date,                           -- NULL when expressed as a rule/description
    description         text,
    is_hard             boolean NOT NULL DEFAULT true,  -- hard = statutory; soft = advisory
    jurisdiction_code   text REFERENCES ref.jurisdiction(code),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_version_id, deadline_code)
);
CREATE INDEX ix_rule_deadline_version ON rules.rule_deadline (rule_version_id);

-- ---- Shared resource pools (the anti-double-counting substrate) -------------
-- Two opportunities drawing on the same pool (RRSP room, medical expense pool)
-- generate a `shares_limit` relationship edge and are constrained by the IOE's
-- ResourceLedger, so a pool can never be allocated twice.
CREATE TABLE rules.rule_shared_resource (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    rule_version_id     uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    resource_code       text NOT NULL,                  -- 'RRSP_ROOM','MEDICAL_POOL'
    pool_scope          text NOT NULL DEFAULT 'individual'
                        CHECK (pool_scope IN ('individual','household')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_version_id, resource_code)
);
CREATE INDEX ix_rule_shared_resource_version ON rules.rule_shared_resource (rule_version_id);

-- ---- Additive columns on rules.rule_outcome ---------------------------------
-- economic_effect_type classifies WHAT KIND of outcome this is, so the IOE never
-- treats a deferral as a permanent reduction (Revision 2.1 §B).
-- portfolio_lever_code REFERENCES the IOE lever registry by CODE only; rule data
-- never names an engine input field (Revision 2.1 §C).
ALTER TABLE rules.rule_outcome
    ADD COLUMN economic_effect_type text,
    ADD COLUMN reversibility        text,
    ADD COLUMN portfolio_lever_code text,
    ADD COLUMN lever_parameters     jsonb;

ALTER TABLE rules.rule_outcome
    ADD CONSTRAINT rule_outcome_economic_effect_type_check
    CHECK (economic_effect_type IS NULL OR economic_effect_type IN (
        'immediate_refund_impact','current_year_tax_reduction','tax_deferral',
        'refundable_benefit','recurring_annual_benefit',
        'multi_year_projected_benefit','future_option_value')) NOT VALID;

ALTER TABLE rules.rule_outcome
    ADD CONSTRAINT rule_outcome_reversibility_check
    CHECK (reversibility IS NULL OR reversibility IN
        ('reversible','partially_reversible','irreversible')) NOT VALID;

-- Validate separately so the ACCESS EXCLUSIVE lock is not held for the scan.
ALTER TABLE rules.rule_outcome VALIDATE CONSTRAINT rule_outcome_economic_effect_type_check;
ALTER TABLE rules.rule_outcome VALIDATE CONSTRAINT rule_outcome_reversibility_check;

-- ---- Additive column on tax_kb.tax_rule_version -----------------------------
-- Coded reasons establishing eligibility. Its ABSENCE is the signal that a
-- version carries no contract-v2 metadata, which the evaluator reports as
-- eligibility_status='indeterminate'.
ALTER TABLE tax_kb.tax_rule_version
    ADD COLUMN eligibility_basis_codes jsonb;

-- ---- Grants -----------------------------------------------------------------
-- 18_admin_grants set ALTER DEFAULT PRIVILEGES for onyx_app_rw on schemas
-- tax_kb/rules/admin, so the new tables inherit CRUD automatically. The
-- read-only role was granted per-table (not by default privileges), so grant it
-- explicitly here.
GRANT SELECT ON
    rules.rule_action, rules.rule_required_document, rules.rule_dependency,
    rules.rule_deadline, rules.rule_shared_resource
    TO onyx_app_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    rules.rule_action, rules.rule_required_document, rules.rule_dependency,
    rules.rule_deadline, rules.rule_shared_resource
    TO onyx_app_rw;
