-- =============================================================================
-- Onyx Ledger — 91 · Seed: worked example — Medical Expense Credit (federal)
-- Alembic revision: 0020_seed_example (data migration)
-- Demonstrates the whole rules-as-data model end to end:
--   tax_rule  →  tax_rule_version (2025)  →  condition tree  →  formula  →  outcome
-- plus how a 2026 version would be added WITHOUT touching 2025 (append-only).
-- =============================================================================

-- ---- Fact catalog entries this rule depends on ------------------------------
INSERT INTO rules.fact_definition (fact_key, data_type, unit, description) VALUES
    ('profile.age','integer','years','Filer age at year end'),
    ('profile.province','enum',NULL,'Province/territory code'),
    ('income.net','money','CAD','Net income for the year'),
    ('expense.medical.total','money','CAD','Total eligible medical expenses'),
    ('derived.marginal_rate','percent',NULL,'Filer marginal tax rate')
ON CONFLICT (fact_key) DO NOTHING;

-- ---- Named constant: the 3% medical floor -----------------------------------
INSERT INTO rules.calc_constant (code, tax_year, value, unit) VALUES
    ('MEDICAL_FLOOR_RATE', 2025, 0.03, 'ratio'),
    ('MEDICAL_FLOOR_CAP',  2025, 2834, 'CAD'),
    ('FED_CREDIT_RATE',    2025, 0.145, 'ratio')   -- 2025 blended rate
ON CONFLICT (code, tax_year) DO NOTHING;

-- ---- Formula: eligible credit = (medical - min(net*0.03, cap)) * credit_rate -
-- Stored as RPN over named inputs; evaluated in the engine sandbox.
INSERT INTO rules.calc_formula (code, expression, expression_lang, output_unit, description)
VALUES (
    'MEDICAL_CREDIT_2025',
    'medical net 0.03 * 2834 min - 0 max 0.145 *',
    'rpn', 'CAD',
    'max(0, medical - min(net*0.03, 2834)) * 0.145'
)
ON CONFLICT (code) DO NOTHING;

INSERT INTO rules.calc_formula_input (formula_id, param_name, fact_key)
SELECT f.id, x.param_name, x.fact_key
FROM rules.calc_formula f
JOIN (VALUES
    ('medical','expense.medical.total'),
    ('net','income.net')
) AS x(param_name, fact_key) ON true
WHERE f.code = 'MEDICAL_CREDIT_2025'
ON CONFLICT (formula_id, param_name) DO NOTHING;

-- ---- Rule identity ----------------------------------------------------------
INSERT INTO tax_kb.tax_rule (code, name, category, jurisdiction_id, province_code)
SELECT 'MEDICAL_EXPENSE_CREDIT','Medical Expense Tax Credit','credit', j.id, NULL
FROM ref.jurisdiction j WHERE j.code = 'FED'
ON CONFLICT (code) DO NOTHING;

-- ---- Version: 2025 (published) ----------------------------------------------
INSERT INTO tax_kb.tax_rule_version (
    tax_rule_id, tax_year, effective_date, expiry_date, status,
    description, ai_explanation, reduction_rate, formula_id,
    source_url, published_at)
SELECT r.id, 2025, DATE '2025-01-01', DATE '2025-12-31', 'published',
    'Federal credit on eligible medical expenses above the lesser of 3% of net income or the annual cap.',
    'You can claim medical expenses above a small floor (3% of your net income, capped). Grouping receipts into one 12-month window and claiming on the lower-income spouse maximizes the credit.',
    0.03,
    (SELECT id FROM rules.calc_formula WHERE code = 'MEDICAL_CREDIT_2025'),
    'https://www.canada.ca/en/revenue-agency.html',
    now()
FROM tax_kb.tax_rule r WHERE r.code = 'MEDICAL_EXPENSE_CREDIT'
ON CONFLICT (tax_rule_id, tax_year) WHERE status = 'published' DO NOTHING;

-- ---- Condition tree: medical > 0 AND net_income exists -----------------------
-- Root AND group.
WITH v AS (
    SELECT id AS version_id FROM tax_kb.tax_rule_version
    WHERE tax_rule_id = (SELECT id FROM tax_kb.tax_rule WHERE code='MEDICAL_EXPENSE_CREDIT')
      AND tax_year = 2025
), g AS (
    INSERT INTO rules.rule_condition_group (rule_version_id, parent_group_id, logical_op, sort_order)
    SELECT version_id, NULL, 'AND', 0 FROM v
    RETURNING id
)
INSERT INTO rules.rule_condition (group_id, fact_key, operator, value_type, value_number, sort_order)
SELECT g.id, 'expense.medical.total', 'gt', 'money', 0, 0 FROM g
UNION ALL
SELECT g.id, 'income.net', 'exists', 'number', NULL, 1 FROM g;

-- ---- Outcome: recommend, impact via the medical formula ----------------------
INSERT INTO rules.rule_outcome (
    rule_version_id, outcome_type, impact_formula_id, priority,
    title_template, mechanism, where_template, how_template, why_template)
SELECT rv.id, 'apply_credit',
    (SELECT id FROM rules.calc_formula WHERE code='MEDICAL_CREDIT_2025'), 2,
    'Claim your medical expense credit',
    'Cuts your tax directly',
    'The lower-income spouse''s return',
    'Total the family''s eligible receipts within one 12-month window ending in the tax year.',
    'Only amounts above 3% of net income (capped) count; pooling on the lower earner shrinks that floor.'
FROM tax_kb.tax_rule_version rv
WHERE rv.tax_rule_id = (SELECT id FROM tax_kb.tax_rule WHERE code='MEDICAL_EXPENSE_CREDIT')
  AND rv.tax_year = 2025;

-- ---- How 2026 is added later (append-only; 2025 stays intact) -----------------
-- 1. INSERT a new tax_rule_version (same tax_rule_id, tax_year=2026, status='draft').
-- 2. INSERT its condition tree / formula (new calc_formula 'MEDICAL_CREDIT_2026').
-- 3. Admin creates a rule_change_request(action='publish'); a DIFFERENT admin
--    approves it; publication flips status→'published'.
-- 4. The 2025 version is never modified; both remain queryable by tax_year.
