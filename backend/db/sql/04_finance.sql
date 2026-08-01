-- =============================================================================
-- Onyx Ledger — 04 · Income & Expenses  (schema: finance)
-- Alembic revision: 0005_finance
-- Per-(user, tax_year) money in / money out. LIST-partitioned by tax_year so a
-- year's analysis touches one partition and old years archive cleanly.
-- Partition key MUST be part of the PK → PK = (id, tax_year).
-- =============================================================================

-- ---- Income sources ---------------------------------------------------------
CREATE TABLE finance.income_source (
    id                  uuid NOT NULL DEFAULT ref.uuid_generate_v7(),
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    tax_year            ref.tax_year_num NOT NULL,
    income_type_id      uuid NOT NULL REFERENCES ref.income_type(id),
    amount              ref.money_amt NOT NULL,
    currency_code       text NOT NULL DEFAULT 'CAD' REFERENCES ref.currency(code),
    frequency           text NOT NULL DEFAULT 'annual'
                        CHECK (frequency IN ('annual','monthly','biweekly','weekly','one_time')),
    province_code       text REFERENCES ref.province(code),
    source_name         text,                        -- employer / payer
    verification_status text NOT NULL DEFAULT 'unverified' REFERENCES ref.verification_status(code),
    document_id         uuid,                        -- provenance (FK added in 11_docs via link table)
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    PRIMARY KEY (id, tax_year)
) PARTITION BY LIST (tax_year);

-- Concrete partitions (add one per active year via migration; DEFAULT catches the rest).
CREATE TABLE finance.income_source_y2024 PARTITION OF finance.income_source FOR VALUES IN (2024);
CREATE TABLE finance.income_source_y2025 PARTITION OF finance.income_source FOR VALUES IN (2025);
CREATE TABLE finance.income_source_default PARTITION OF finance.income_source DEFAULT;

CREATE INDEX ix_income_user_year ON finance.income_source (user_id, tax_year) WHERE deleted_at IS NULL;
CREATE INDEX ix_income_type ON finance.income_source (income_type_id);

-- ---- Expense records --------------------------------------------------------
CREATE TABLE finance.expense_record (
    id                  uuid NOT NULL DEFAULT ref.uuid_generate_v7(),
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    tax_year            ref.tax_year_num NOT NULL,
    expense_category_id uuid NOT NULL REFERENCES ref.expense_category(id),
    amount              ref.money_amt NOT NULL,
    currency_code       text NOT NULL DEFAULT 'CAD' REFERENCES ref.currency(code),
    description         text,
    incurred_on         date,
    verification_status text NOT NULL DEFAULT 'unverified' REFERENCES ref.verification_status(code),
    receipt_document_id uuid,                        -- provenance
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    PRIMARY KEY (id, tax_year)
) PARTITION BY LIST (tax_year);

CREATE TABLE finance.expense_record_y2024 PARTITION OF finance.expense_record FOR VALUES IN (2024);
CREATE TABLE finance.expense_record_y2025 PARTITION OF finance.expense_record FOR VALUES IN (2025);
CREATE TABLE finance.expense_record_default PARTITION OF finance.expense_record DEFAULT;

CREATE INDEX ix_expense_user_year ON finance.expense_record (user_id, tax_year) WHERE deleted_at IS NULL;
CREATE INDEX ix_expense_category ON finance.expense_record (expense_category_id);
