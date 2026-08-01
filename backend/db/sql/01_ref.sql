-- =============================================================================
-- Onyx Ledger — 01 · Reference (lookup) data
-- Alembic revision: 0002_ref
-- Evolving enumerations as data (never PG ENUM). Seeded in 90_seed_reference.sql.
-- =============================================================================

-- Jurisdiction: federal + each province/territory.
CREATE TABLE ref.jurisdiction (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code        text NOT NULL UNIQUE,               -- 'FED','ON','BC',...
    name        text NOT NULL,
    level       text NOT NULL CHECK (level IN ('federal','provincial','territorial')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Province/territory (subset view of jurisdiction with tax specifics).
CREATE TABLE ref.province (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code            text NOT NULL UNIQUE,           -- 'ON','QC',...
    name            text NOT NULL,
    jurisdiction_id uuid NOT NULL REFERENCES ref.jurisdiction(id),
    has_surtax      boolean NOT NULL DEFAULT false,
    has_health_premium boolean NOT NULL DEFAULT false,
    federal_abatement ref.rate NOT NULL DEFAULT 0,  -- QC = 0.165
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ref.currency (
    code        text PRIMARY KEY,                   -- ISO 4217, 'CAD'
    name        text NOT NULL,
    minor_unit  smallint NOT NULL DEFAULT 2
);

-- Tax year metadata (indexation, lifecycle status).
CREATE TABLE ref.tax_year (
    year            ref.tax_year_num PRIMARY KEY,
    indexation_factor ref.rate,                     -- e.g. 1.028
    status          text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','active','closed')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Small helper for the many code/label lookups below.
-- (Kept as distinct tables — not one polymorphic "enum" table — so each can
--  grow its own attributes and carry proper FKs.)
CREATE TABLE ref.residency_status (
    code text PRIMARY KEY, label text NOT NULL, sort_order int NOT NULL DEFAULT 0
);
CREATE TABLE ref.marital_status (
    code text PRIMARY KEY, label text NOT NULL, treated_as_partnered boolean NOT NULL DEFAULT false
);
CREATE TABLE ref.employment_type (
    code text PRIMARY KEY, label text NOT NULL
);
CREATE TABLE ref.housing_status (
    code text PRIMARY KEY, label text NOT NULL
);
CREATE TABLE ref.verification_status (
    code text PRIMARY KEY, label text NOT NULL
);

-- Income type: drives inclusion rules and slip mapping.
CREATE TABLE ref.income_type (
    id                 uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code               text NOT NULL UNIQUE,        -- 'employment','self_employment',...
    label              text NOT NULL,
    default_inclusion  ref.rate NOT NULL DEFAULT 1, -- capital_gains = 0.5
    is_gross_up        boolean NOT NULL DEFAULT false, -- dividends
    cra_slip_hint      text,                        -- 'T4','T5',...
    sort_order         int NOT NULL DEFAULT 0
);

-- Expense category: hierarchical (parent_id) and extensible.
CREATE TABLE ref.expense_category (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code        text NOT NULL UNIQUE,               -- 'medical','tuition',...
    label       text NOT NULL,
    parent_id   uuid REFERENCES ref.expense_category(id),
    is_credit   boolean NOT NULL DEFAULT false,     -- credit vs deduction
    cra_line    text,                               -- 'line 33099', etc.
    sort_order  int NOT NULL DEFAULT 0
);

-- Asset category (+ registered-account traits).
CREATE TABLE ref.asset_category (
    id            uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code          text NOT NULL UNIQUE,             -- 'real_estate','tfsa',...
    label         text NOT NULL,
    is_registered boolean NOT NULL DEFAULT false,   -- TFSA/RRSP/FHSA/RESP
    is_contribution_limited boolean NOT NULL DEFAULT false,
    sort_order    int NOT NULL DEFAULT 0
);

CREATE TABLE ref.account_registered_type (
    code text PRIMARY KEY, label text NOT NULL      -- 'TFSA','RRSP','FHSA','RESP'
);

CREATE TABLE ref.liability_category (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code        text NOT NULL UNIQUE,               -- 'mortgage','student_loan',...
    label       text NOT NULL,
    sort_order  int NOT NULL DEFAULT 0
);

-- Document (slip) type.
CREATE TABLE ref.document_type (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code        text NOT NULL UNIQUE,               -- 'T4','T5','T2202',...
    label       text NOT NULL,
    category    text NOT NULL DEFAULT 'slip'
                CHECK (category IN ('slip','receipt','statement','other')),
    sort_order  int NOT NULL DEFAULT 0
);

-- Tax rule category.
CREATE TABLE ref.rule_category (
    code text PRIMARY KEY, label text NOT NULL      -- 'credit','deduction','benefit','bracket','limit','threshold'
);

-- Condition operators the rules engine understands (fixed set, but as data so
-- the admin UI can enumerate them; enforced by CHECK in rules.rule_condition).
CREATE TABLE ref.condition_operator (
    code        text PRIMARY KEY,                   -- 'eq','gt','lt','gte','lte','between','in','contains','exists','is_true'
    label       text NOT NULL,
    arity       text NOT NULL CHECK (arity IN ('unary','binary','range','set'))
);
