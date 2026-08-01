-- =============================================================================
-- Onyx Ledger — 92 · Seed: core fact catalog
-- Alembic revision: 0022_seed_facts (data migration)
-- The canonical facts the engine + document extractor reference by key. These
-- back the fact_key FKs on rule_condition / calc_formula_input / extraction_field.
-- Idempotent.
-- =============================================================================

INSERT INTO rules.fact_definition (fact_key, data_type, unit, description) VALUES
    ('income.employment','money','CAD','Employment income'),
    ('income.self_employment.net','money','CAD','Net self-employment income'),
    ('income.total','money','CAD','Total income'),
    ('income.net','money','CAD','Net income (proxy for taxable income)'),
    ('expense.medical.total','money','CAD','Total eligible medical expenses'),
    ('expense.tuition.total','money','CAD','Total eligible tuition'),
    ('expense.donation.total','money','CAD','Total charitable donations'),
    ('profile.age','integer','years','Filer age at year end'),
    ('profile.province','enum',NULL,'Province/territory code'),
    ('profile.marital_status','enum',NULL,'Marital status'),
    ('derived.marginal_rate','percent',NULL,'Filer marginal tax rate'),
    ('derived.net_income','money','CAD','Net income'),
    ('rrsp.contribution','money','CAD','RRSP contribution amount')
ON CONFLICT (fact_key) DO NOTHING;
