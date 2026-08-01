-- =============================================================================
-- Onyx Ledger — 11 · Documents & OCR  (schema: docs)
-- Alembic revision: 0012_docs
-- Never store file bytes in Postgres — only object-storage references + OCR
-- results. document_link substantiates income/expense rows (provenance).
-- =============================================================================

CREATE TABLE docs.document (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id             uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    document_type_id    uuid REFERENCES ref.document_type(id),
    tax_year            ref.tax_year_num,
    storage_provider    text NOT NULL DEFAULT 's3',
    bucket              text NOT NULL,
    object_key          text NOT NULL,               -- pointer, not bytes
    content_hash        text,                        -- integrity / dedupe
    mime_type           text,
    byte_size           bigint,
    status              text NOT NULL DEFAULT 'uploaded'
                        CHECK (status IN ('uploaded','processing','processed','failed','quarantined')),
    uploaded_at         timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    UNIQUE (bucket, object_key)
);
CREATE INDEX ix_document_user ON docs.document (user_id) WHERE deleted_at IS NULL;
CREATE INDEX ix_document_status ON docs.document (status);

CREATE TABLE docs.document_extraction (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    document_id     uuid NOT NULL REFERENCES docs.document(id) ON DELETE CASCADE,
    engine          text NOT NULL,                   -- 'textract','tesseract','structured'
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','processed','needs_review','failed')),
    confidence      ref.rate,
    extracted_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_extraction_document ON docs.document_extraction (document_id);

-- One row per extracted field; bounding_box JSONB justified (OCR geometry).
CREATE TABLE docs.extraction_field (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    extraction_id   uuid NOT NULL REFERENCES docs.document_extraction(id) ON DELETE CASCADE,
    field_name      text NOT NULL,                   -- 'employmentIncome' / box code
    fact_key        text REFERENCES rules.fact_definition(fact_key),
    value_text      text,
    value_number    numeric,
    confidence      ref.rate,
    bounding_box    jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_extraction_field_extraction ON docs.extraction_field (extraction_id);

-- Links a document to the income/expense it substantiates (composite FK into
-- the tax_year-partitioned tables).
CREATE TABLE docs.document_link (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    document_id         uuid NOT NULL REFERENCES docs.document(id) ON DELETE CASCADE,
    income_source_id    uuid,
    income_tax_year     ref.tax_year_num,
    expense_record_id   uuid,
    expense_tax_year    ref.tax_year_num,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CHECK ( (income_source_id IS NOT NULL AND income_tax_year IS NOT NULL)
         OR (expense_record_id IS NOT NULL AND expense_tax_year IS NOT NULL) ),
    FOREIGN KEY (income_source_id, income_tax_year)
        REFERENCES finance.income_source (id, tax_year) ON DELETE CASCADE,
    FOREIGN KEY (expense_record_id, expense_tax_year)
        REFERENCES finance.expense_record (id, tax_year) ON DELETE CASCADE
);
CREATE INDEX ix_document_link_document ON docs.document_link (document_id);
