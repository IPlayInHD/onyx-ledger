-- =============================================================================
-- Onyx Ledger — 07 · Rules Engine Support  (schema: rules)
-- Alembic revision: 0008_rules
-- Eligibility + math as DATA. Fact catalog, relational boolean condition tree,
-- versioned formulas, and rule outcomes. The backend engine is a generic
-- evaluator that reads all of this — nothing hardcoded.
-- =============================================================================

-- ---- Fact catalog: every value the engine can reason about ------------------
-- Addressed by stable dotted key, decoupled from physical columns.
CREATE TABLE rules.fact_definition (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    fact_key    text NOT NULL UNIQUE,                -- 'profile.age','income.total'
    data_type   text NOT NULL CHECK (data_type IN
                    ('number','money','percent','integer','boolean','text','enum','date')),
    unit        text,                                -- 'CAD','years'
    description text,
    resolver_hint text,                              -- how the engine sources it
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ---- Formulas (math kept separate from prose) -------------------------------
CREATE TABLE rules.calc_formula (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code            text NOT NULL UNIQUE,            -- 'MEDICAL_CREDIT_2025'
    expression      text NOT NULL,                   -- sandboxed DSL (RPN/AST)
    expression_lang text NOT NULL DEFAULT 'rpn' CHECK (expression_lang IN ('rpn','ast','cel')),
    output_unit     text,
    description     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Formula inputs bind named params → fact keys (or literal/constant).
CREATE TABLE rules.calc_formula_input (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    formula_id  uuid NOT NULL REFERENCES rules.calc_formula(id) ON DELETE CASCADE,
    param_name  text NOT NULL,                       -- 'net_income'
    fact_key    text REFERENCES rules.fact_definition(fact_key),
    literal_value numeric,                           -- when not sourced from a fact
    UNIQUE (formula_id, param_name),
    CHECK (fact_key IS NOT NULL OR literal_value IS NOT NULL)
);

-- Named constants per tax year (indexation factors, fixed thresholds).
--
-- EFFECTIVE PERIODS. Most published parameters are annual and leave both period
-- columns NULL, which reads as "this is the value for the whole tax year". Some
-- are not: CRA sets prescribed interest rates per QUARTER, so one code and one
-- tax year legitimately carry four different values. Encoding the quarter into
-- the code would put data in the key and make the code non-semantic, and
-- storing one quarter as the annual value would be false for the other nine
-- months, so the period is stored as what it is.
--
-- The EXCLUDE constraint replaces UNIQUE (code, tax_year) and is strictly
-- stronger. `daterange(NULL, NULL, '[]')` is (-infinity, infinity), so an
-- annual row still excludes a second annual row exactly as the unique
-- constraint did — AND it excludes any period row for the same code and year,
-- which is the correct reading of "the 2026 value is X" sitting beside "the Q3
-- 2026 value is Y". Two periods that do not overlap coexist.
CREATE TABLE rules.calc_constant (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code            text NOT NULL,                   -- 'MEDICAL_FLOOR_RATE'
    tax_year        ref.tax_year_num NOT NULL REFERENCES ref.tax_year(year),
    value           numeric NOT NULL,
    unit            text,
    effective_from  date,                            -- NULL = whole tax year
    effective_to    date,                            -- NULL = whole tax year
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_calc_constant_period_ordered CHECK (
        effective_from IS NULL OR effective_to IS NULL
        OR effective_from <= effective_to),
    CONSTRAINT ex_calc_constant_no_overlap EXCLUDE USING gist (
        code WITH =,
        tax_year WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&)
);

-- Now that calc_formula exists, wire up the deferred FKs from 06_tax_kb.
ALTER TABLE tax_kb.tax_rule_version
    ADD CONSTRAINT fk_rule_version_formula
    FOREIGN KEY (formula_id) REFERENCES rules.calc_formula(id);
ALTER TABLE tax_kb.benefit_parameter
    ADD CONSTRAINT fk_benefit_param_formula
    FOREIGN KEY (formula_id) REFERENCES rules.calc_formula(id);

-- ---- Condition tree: relational boolean AST ---------------------------------
-- A group nests via parent_group_id and carries a logical operator; leaves are
-- rule_condition rows. Root group (parent_group_id IS NULL) is the rule's gate.
CREATE TABLE rules.rule_condition_group (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    rule_version_id     uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    parent_group_id     uuid REFERENCES rules.rule_condition_group(id) ON DELETE CASCADE,
    logical_op          text NOT NULL DEFAULT 'AND' CHECK (logical_op IN ('AND','OR','NOT')),
    sort_order          smallint NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_cond_group_version ON rules.rule_condition_group (rule_version_id);
CREATE INDEX ix_cond_group_parent ON rules.rule_condition_group (parent_group_id);

-- Set-valued RHS for 'in' / 'contains' (e.g. province IN {ON,BC}).
CREATE TABLE rules.condition_value_set (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code        text UNIQUE,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE rules.condition_value_set_item (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    value_set_id uuid NOT NULL REFERENCES rules.condition_value_set(id) ON DELETE CASCADE,
    value_text  text NOT NULL,
    UNIQUE (value_set_id, value_text)
);

-- Leaf comparison: fact <operator> value(s).
CREATE TABLE rules.rule_condition (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    group_id            uuid NOT NULL REFERENCES rules.rule_condition_group(id) ON DELETE CASCADE,
    fact_key            text NOT NULL REFERENCES rules.fact_definition(fact_key),
    operator            text NOT NULL REFERENCES ref.condition_operator(code),
    value_type          text NOT NULL CHECK (value_type IN ('number','money','percent','boolean','text','date','set')),
    value_number        numeric,
    value_number_high   numeric,                     -- 'between' upper bound
    value_text          text,
    value_boolean       boolean,
    value_date          date,
    value_set_id        uuid REFERENCES rules.condition_value_set(id),
    sort_order          smallint NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_rule_condition_group ON rules.rule_condition (group_id);
CREATE INDEX ix_rule_condition_fact ON rules.rule_condition (fact_key);

-- ---- Outcome: the THEN half (recommendation / credit / deduction / benefit) --
CREATE TABLE rules.rule_outcome (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    rule_version_id     uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    outcome_type        text NOT NULL CHECK (outcome_type IN
                            ('recommend','apply_credit','apply_deduction','flag_benefit_eligibility')),
    impact_formula_id   uuid REFERENCES rules.calc_formula(id),
    priority            smallint NOT NULL DEFAULT 3,
    -- recommendation template (where/how/why is filled by the engine at runtime)
    title_template      text,
    mechanism           text,
    where_template      text,
    how_template        text,
    why_template        text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_rule_outcome_version ON rules.rule_outcome (rule_version_id);
