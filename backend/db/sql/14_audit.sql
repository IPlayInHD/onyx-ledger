-- =============================================================================
-- Onyx Ledger — 14 · Security & Audit  (schema: audit)
-- Alembic revision: 0015_audit
-- Immutable, polymorphic audit trail (RANGE-partitioned by month) + compliance
-- (consent, export/erasure requests, security events). No outbound FK from the
-- audit log so it can never block a delete and outlives the entities it records.
-- =============================================================================

-- Partitioned parent. PK includes the partition key (created_at).
CREATE TABLE audit.audit_log (
    id              uuid NOT NULL DEFAULT ref.uuid_generate_v7(),
    actor_type      text NOT NULL CHECK (actor_type IN ('user','admin','system')),
    actor_id        uuid,                            -- pseudonymized on erasure
    action          text NOT NULL,                   -- 'INSERT','UPDATE','DELETE', domain verbs
    entity_schema   text NOT NULL,
    entity_table    text NOT NULL,
    entity_id       text,                            -- text: some PKs are composite
    previous_value  jsonb,
    new_value       jsonb,
    ip_address      inet,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Initial monthly partitions + a catch-all default (rotate via maintenance job).
CREATE TABLE audit.audit_log_2025m01 PARTITION OF audit.audit_log
    FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');
CREATE TABLE audit.audit_log_2025m02 PARTITION OF audit.audit_log
    FOR VALUES FROM ('2025-02-01') TO ('2025-03-01');
CREATE TABLE audit.audit_log_default PARTITION OF audit.audit_log DEFAULT;

CREATE INDEX ix_audit_entity ON audit.audit_log (entity_schema, entity_table, entity_id);
CREATE INDEX ix_audit_actor ON audit.audit_log (actor_type, actor_id, created_at DESC);
-- BRIN is ideal for an append-only time column (tiny, range-scannable).
CREATE INDEX brin_audit_created ON audit.audit_log USING brin (created_at);

-- Versioned consent events (structured settings live in profile.user_privacy_setting).
CREATE TABLE audit.consent_log (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id     uuid REFERENCES identity.user_account(id) ON DELETE SET NULL,
    consent_type text NOT NULL,                      -- 'terms','privacy','marketing'
    granted     boolean NOT NULL,
    version     text,
    ip_address  inet,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_consent_user ON audit.consent_log (user_id, created_at DESC);

-- PIPEDA data-subject rights.
CREATE TABLE audit.data_export_request (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id     uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    status      text NOT NULL DEFAULT 'requested'
                CHECK (status IN ('requested','processing','ready','delivered','failed')),
    object_key  text,                                -- export artifact in object storage
    requested_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE audit.data_deletion_request (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id     uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    status      text NOT NULL DEFAULT 'requested'
                CHECK (status IN ('requested','processing','completed','rejected')),
    reason      text,
    requested_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE audit.security_event (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id     uuid REFERENCES identity.user_account(id) ON DELETE SET NULL,
    event_type  text NOT NULL,                       -- 'suspicious_login','rate_limit',...
    severity    text NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warning','critical')),
    detail      jsonb,
    ip_address  inet,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_security_event_type ON audit.security_event (event_type, created_at DESC);
