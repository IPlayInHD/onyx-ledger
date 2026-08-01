-- =============================================================================
-- Onyx Ledger — 90 · Seed: reference data
-- Alembic revision: 0019_seed_reference (data migration)
-- Idempotent inserts for the lookup tables. Safe to re-run.
-- =============================================================================

INSERT INTO ref.currency (code, name, minor_unit) VALUES
    ('CAD','Canadian Dollar',2), ('USD','US Dollar',2)
ON CONFLICT (code) DO NOTHING;

-- Jurisdictions (federal + provinces/territories).
INSERT INTO ref.jurisdiction (code, name, level) VALUES
    ('FED','Federal','federal'),
    ('ON','Ontario','provincial'), ('QC','Quebec','provincial'),
    ('BC','British Columbia','provincial'), ('AB','Alberta','provincial'),
    ('MB','Manitoba','provincial'), ('SK','Saskatchewan','provincial'),
    ('NS','Nova Scotia','provincial'), ('NB','New Brunswick','provincial'),
    ('PE','Prince Edward Island','provincial'), ('NL','Newfoundland and Labrador','provincial'),
    ('YT','Yukon','territorial'), ('NT','Northwest Territories','territorial'),
    ('NU','Nunavut','territorial')
ON CONFLICT (code) DO NOTHING;

-- Provinces (tax traits).
INSERT INTO ref.province (code, name, jurisdiction_id, has_surtax, has_health_premium, federal_abatement)
SELECT j.code, j.name, j.id,
       j.code = 'ON', j.code = 'ON',
       CASE WHEN j.code = 'QC' THEN 0.165 ELSE 0 END
FROM ref.jurisdiction j
WHERE j.level IN ('provincial','territorial')
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.tax_year (year, indexation_factor, status) VALUES
    (2024, 1.000, 'active'), (2025, 1.028, 'active'), (2026, NULL, 'draft')
ON CONFLICT (year) DO NOTHING;

INSERT INTO ref.residency_status (code, label) VALUES
    ('resident','Resident'), ('non_resident','Non-resident'),
    ('deemed_resident','Deemed resident'), ('newcomer','Newcomer')
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.marital_status (code, label, treated_as_partnered) VALUES
    ('single','Single',false), ('married','Married',true),
    ('common_law','Common-law',true), ('separated','Separated',false),
    ('divorced','Divorced',false), ('widowed','Widowed',false)
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.employment_type (code, label) VALUES
    ('employed','Employed'), ('self_employed','Self-employed'),
    ('mixed','Employed + self-employed'), ('retired','Retired'),
    ('unemployed','Unemployed'), ('student','Student')
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.housing_status (code, label) VALUES
    ('renting','Renting'), ('owner','Homeowner'), ('living_with_family','Living with family')
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.verification_status (code, label) VALUES
    ('unverified','Unverified'), ('user_confirmed','User confirmed'),
    ('document_backed','Document backed'), ('needs_review','Needs review')
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.income_type (code, label, default_inclusion, is_gross_up, cra_slip_hint, sort_order) VALUES
    ('employment','Employment income',1,false,'T4',1),
    ('self_employment','Self-employment income',1,false,'T2125',2),
    ('business','Business income',1,false,'T2125',3),
    ('rental','Rental income',1,false,'T776',4),
    ('investment','Investment income',1,false,'T5',5),
    ('capital_gains','Capital gains',0.5,false,'T5008',6),
    ('eligible_dividends','Eligible dividends',1,true,'T5',7),
    ('non_eligible_dividends','Non-eligible dividends',1,true,'T5',8),
    ('interest','Interest income',1,false,'T5',9),
    ('foreign','Foreign income',1,false,NULL,10),
    ('pension','Pension income',1,false,'T4A',11),
    ('government_benefit','Government benefits',1,false,'T4E',12),
    ('other','Other income',1,false,NULL,13)
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.expense_category (code, label, is_credit, cra_line, sort_order) VALUES
    ('medical','Medical expenses',true,'line 33099',1),
    ('tuition','Tuition',true,'line 32300',2),
    ('childcare','Child care expenses',false,'line 21400',3),
    ('professional_fees','Professional/union dues',false,'line 21200',4),
    ('employment','Employment expenses',false,'line 22900',5),
    ('home_office','Home office',false,'line 22900',6),
    ('business','Business expenses',false,'T2125',7),
    ('donation','Charitable donations',true,'line 34900',8),
    ('investment','Investment/carrying charges',false,'line 22100',9),
    ('moving','Moving expenses',false,'line 21900',10)
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.asset_category (code, label, is_registered, is_contribution_limited, sort_order) VALUES
    ('real_estate','Real estate',false,false,1),
    ('vehicle','Vehicle',false,false,2),
    ('investment_account','Non-registered investments',false,false,3),
    ('tfsa','TFSA',true,true,4), ('rrsp','RRSP',true,true,5),
    ('fhsa','FHSA',true,true,6), ('resp','RESP',true,true,7),
    ('business','Business',false,false,8), ('savings','Savings account',false,false,9),
    ('crypto','Crypto assets',false,false,10)
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.account_registered_type (code, label) VALUES
    ('TFSA','Tax-Free Savings Account'), ('RRSP','Registered Retirement Savings Plan'),
    ('FHSA','First Home Savings Account'), ('RESP','Registered Education Savings Plan')
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.liability_category (code, label, sort_order) VALUES
    ('mortgage','Mortgage',1), ('student_loan','Student loan',2),
    ('credit_card','Credit card',3), ('vehicle_loan','Vehicle loan',4),
    ('business_loan','Business loan',5), ('personal_loan','Personal loan',6)
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.document_type (code, label, category, sort_order) VALUES
    ('T4','T4 — Statement of Remuneration','slip',1),
    ('T4A','T4A — Pension/Other','slip',2),
    ('T5','T5 — Investment Income','slip',3),
    ('T3','T3 — Trust Income','slip',4),
    ('T2202','T2202 — Tuition','slip',5),
    ('T2125','T2125 — Self-employment','statement',6),
    ('T776','T776 — Rental','statement',7),
    ('RRSP','RRSP Contribution Receipt','receipt',8),
    ('DONATION','Donation Receipt','receipt',9),
    ('MEDICAL','Medical Receipt','receipt',10)
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.rule_category (code, label) VALUES
    ('credit','Non-refundable/refundable credit'), ('deduction','Deduction'),
    ('benefit','Government benefit'), ('bracket','Tax bracket'),
    ('limit','Contribution limit'), ('threshold','Threshold')
ON CONFLICT (code) DO NOTHING;

INSERT INTO ref.condition_operator (code, label, arity) VALUES
    ('eq','equals','binary'), ('neq','not equals','binary'),
    ('gt','greater than','binary'), ('gte','greater than or equal','binary'),
    ('lt','less than','binary'), ('lte','less than or equal','binary'),
    ('between','between','range'), ('in','in set','set'),
    ('contains','contains','set'), ('exists','exists','unary'),
    ('is_true','is true','unary')
ON CONFLICT (code) DO NOTHING;
