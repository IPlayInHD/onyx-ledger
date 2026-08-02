-- =============================================================================
-- Onyx Ledger — 19 · Tax Knowledge Management System  (schema: tkms)
-- Alembic revision: 0024_tkms
-- The authoritative WORKFLOW + PROVENANCE layer that imports, parses, extracts,
-- validates, compares, versions, governs, publishes, and rolls back Canadian
-- tax legislation. The deterministic Tax Intelligence Engine consumes ONLY
-- Published rules from tax_kb; TKMS is what earns a version its "published"
-- status through governance and traceability.
--
-- Design stance: EXTEND, don't redesign. In-flight artifacts live in `tkms`;
-- the Tax Knowledge Base (tax_kb + rules) only ever holds real rule versions —
-- a draft is a tax_rule_version with a non-published status, inert to the engine
-- until published. This file also (a) aligns the version status vocabulary and
-- (b) adds provenance columns so every published rule ties back to its import.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS tkms;

-- ---- Import job: one legislative import, cradle-to-grave ---------------------
CREATE TABLE tkms.import_job (
    id                      uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    source_org              text NOT NULL,                 -- 'Canada Revenue Agency'
    source_url              text,
    checksum                text,                          -- of raw doc; dedupe key
    jurisdiction_code       text REFERENCES ref.jurisdiction(code),
    province_code           text REFERENCES ref.province(code),   -- NULL = federal/all
    tax_year                ref.tax_year_num REFERENCES ref.tax_year(year),
    document_version        text,                          -- publisher's version/date label
    format                  text NOT NULL
                            CHECK (format IN ('csv','json','xml','manual','html','pdf')),
    parser_name             text,
    parser_version          text,
    operator_admin_id       uuid REFERENCES admin.admin_user(id),
    -- pipeline lifecycle (distinct from the version status vocabulary)
    status                  text NOT NULL DEFAULT 'received'
                            CHECK (status IN ('received','stored','parsing','parsed',
                                    'extracted','validating','validated','in_review',
                                    'published','failed')),
    validation_status       text NOT NULL DEFAULT 'pending'
                            CHECK (validation_status IN ('pending','passed','warnings','failed')),
    approval_status         text NOT NULL DEFAULT 'pending'
                            CHECK (approval_status IN ('pending','submitted','approved','rejected')),
    processing_started_at   timestamptz,
    processing_completed_at timestamptz,
    processing_duration_ms  integer,
    error                   text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);
-- Idempotent import: at most one job per raw-document checksum.
CREATE UNIQUE INDEX uq_import_job_checksum
    ON tkms.import_job (checksum) WHERE checksum IS NOT NULL;
CREATE INDEX ix_import_job_status ON tkms.import_job (status);
CREATE INDEX ix_import_job_tax_year ON tkms.import_job (tax_year);

-- ---- Raw document: stored source artifact (bytes offloaded to S3) -----------
CREATE TABLE tkms.raw_document (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    import_job_id   uuid NOT NULL REFERENCES tkms.import_job(id) ON DELETE CASCADE,
    storage_bucket  text NOT NULL,
    object_key      text NOT NULL,
    content_hash    text NOT NULL,
    mime_type       text,
    byte_size       bigint,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_raw_document_job ON tkms.raw_document (import_job_id);

-- ---- Parse result: output of one parser run over a raw document -------------
CREATE TABLE tkms.parse_result (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    import_job_id   uuid NOT NULL REFERENCES tkms.import_job(id) ON DELETE CASCADE,
    parser_name     text NOT NULL,
    parser_version  text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','succeeded','failed')),
    confidence      ref.rate,                          -- 0..1 aggregate confidence
    text_object_key text,                              -- extracted text stored in S3
    rule_count      integer NOT NULL DEFAULT 0,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_parse_result_job ON tkms.parse_result (import_job_id);

-- ---- Extracted rule: CANONICAL, immutable snapshot of parser output ---------
-- The single contract every parser produces. Never mutated; drafts are promoted
-- FROM these rows (promoted_version_id links the tax_rule_version they became).
CREATE TABLE tkms.extracted_rule (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    parse_result_id     uuid NOT NULL REFERENCES tkms.parse_result(id) ON DELETE CASCADE,
    import_job_id       uuid NOT NULL REFERENCES tkms.import_job(id) ON DELETE CASCADE,
    ordinal             smallint NOT NULL,
    payload             jsonb NOT NULL,                -- the ExtractedRule contract
    confidence          ref.rate,
    promoted_version_id uuid REFERENCES tax_kb.tax_rule_version(id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (parse_result_id, ordinal)
);
CREATE INDEX ix_extracted_rule_job ON tkms.extracted_rule (import_job_id);

-- ---- Validation report + findings -------------------------------------------
CREATE TABLE tkms.validation_report (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    import_job_id       uuid REFERENCES tkms.import_job(id) ON DELETE CASCADE,
    target_version_id   uuid REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    status              text NOT NULL CHECK (status IN ('passed','warnings','failed')),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_validation_report_job ON tkms.validation_report (import_job_id);
CREATE INDEX ix_validation_report_version ON tkms.validation_report (target_version_id);

CREATE TABLE tkms.validation_finding (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    report_id   uuid NOT NULL REFERENCES tkms.validation_report(id) ON DELETE CASCADE,
    rule_ref    text,                                  -- rule code / extracted_rule ordinal
    severity    text NOT NULL CHECK (severity IN ('error','warning','info')),
    code        text NOT NULL,                         -- 'DUPLICATE_RULE','BAD_FORMULA'
    message     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_validation_finding_report ON tkms.validation_finding (report_id);

-- ---- Change report + items: draft-vs-published diff -------------------------
CREATE TABLE tkms.change_report (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    import_job_id       uuid REFERENCES tkms.import_job(id) ON DELETE CASCADE,
    draft_version_id    uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    baseline_version_id uuid REFERENCES tax_kb.tax_rule_version(id) ON DELETE SET NULL,  -- NULL = new rule
    summary             text,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_change_report_draft ON tkms.change_report (draft_version_id);

CREATE TABLE tkms.change_item (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    change_report_id    uuid NOT NULL REFERENCES tkms.change_report(id) ON DELETE CASCADE,
    field               text NOT NULL,
    change_type         text NOT NULL CHECK (change_type IN ('added','removed','changed')),
    old_value           text,
    new_value           text,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_change_item_report ON tkms.change_item (change_report_id);

-- ---- Rollback record: an executed legislative rollback (four-eyes) ----------
CREATE TABLE tkms.rollback_record (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    tax_rule_id     uuid NOT NULL REFERENCES tax_kb.tax_rule(id) ON DELETE CASCADE,
    from_version_id uuid REFERENCES tax_kb.tax_rule_version(id),  -- current published, superseded
    to_version_id   uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id),  -- prior, restored
    reason          text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','executed')),
    performed_by    uuid REFERENCES admin.admin_user(id),   -- requester
    approved_by     uuid REFERENCES admin.admin_user(id),   -- second admin
    requested_at    timestamptz NOT NULL DEFAULT now(),
    decided_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- four-eyes: the approver may not be the requester
    CHECK (approved_by IS NULL OR approved_by <> performed_by)
);
CREATE INDEX ix_rollback_rule ON tkms.rollback_record (tax_rule_id);

-- ---- Dead-letter: a task that exhausted retries -----------------------------
CREATE TABLE tkms.dead_letter (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    task_name       text NOT NULL,
    queue           text,
    payload         jsonb NOT NULL,
    error           text,
    attempts        integer NOT NULL DEFAULT 0,
    import_job_id   uuid REFERENCES tkms.import_job(id) ON DELETE SET NULL,
    resolved_at     timestamptz,
    resolved_by     uuid REFERENCES admin.admin_user(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_dead_letter_unresolved ON tkms.dead_letter (created_at) WHERE resolved_at IS NULL;

-- =============================================================================
-- Provenance + status alignment on the existing Tax Knowledge Base
-- =============================================================================

-- Provenance: every version can tie back to its import job / parser / validation
-- (nullable — hand-authored and legacy versions predate TKMS).
ALTER TABLE tax_kb.tax_rule_version
    ADD COLUMN import_job_id        uuid REFERENCES tkms.import_job(id),
    ADD COLUMN parser_version       text,
    ADD COLUMN parser_confidence    ref.rate,
    ADD COLUMN validation_report_id uuid REFERENCES tkms.validation_report(id);
CREATE INDEX ix_rule_version_import_job ON tax_kb.tax_rule_version (import_job_id);

-- Spec-required finer classification on the stable rule identity.
ALTER TABLE tax_kb.tax_rule ADD COLUMN subcategory text;

-- Status vocabulary alignment: draft → validated → pending_review → approved →
-- published → superseded → archived. Map the prototype's legacy values first,
-- then swap the CHECK constraint (found by definition so we don't depend on the
-- auto-generated constraint name).
UPDATE tax_kb.tax_rule_version SET status = 'pending_review' WHERE status = 'pending_approval';
UPDATE tax_kb.tax_rule_version SET status = 'archived'       WHERE status = 'retired';

DO $$
DECLARE c text;
BEGIN
    SELECT conname INTO c
      FROM pg_constraint
     WHERE conrelid = 'tax_kb.tax_rule_version'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) ILIKE '%status%';
    IF c IS NOT NULL THEN
        EXECUTE format('ALTER TABLE tax_kb.tax_rule_version DROP CONSTRAINT %I', c);
    END IF;
END $$;

ALTER TABLE tax_kb.tax_rule_version
    ADD CONSTRAINT tax_rule_version_status_check
    CHECK (status IN ('draft','validated','pending_review','approved',
                      'published','superseded','archived'));

-- =============================================================================
-- Triggers: updated_at (15_triggers ran before this schema existed) + audit
-- =============================================================================
CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON tkms.import_job
    FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();
CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON tkms.rollback_record
    FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();
CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON tkms.dead_letter
    FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();

-- Audit the privileged decisions (import lifecycle + rollback). The append-only
-- audit.audit_log captures actor + before/after for every write.
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON tkms.import_job
    FOR EACH ROW EXECUTE FUNCTION audit.log_change();
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON tkms.rollback_record
    FOR EACH ROW EXECUTE FUNCTION audit.log_change();

-- =============================================================================
-- Grants: TKMS is the admin plane (no user-RLS; reached only via admin-
-- permissioned endpoints under the single runtime role, per 18_admin_grants).
-- =============================================================================
GRANT USAGE ON SCHEMA tkms TO onyx_app_rw, onyx_app_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA tkms TO onyx_app_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA tkms TO onyx_app_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA tkms
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO onyx_app_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA tkms
    GRANT SELECT ON TABLES TO onyx_app_ro;
