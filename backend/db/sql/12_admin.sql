-- =============================================================================
-- Onyx Ledger — 12 · Administration  (schema: admin)
-- Alembic revision: 0013_admin
-- Internal staff (separate from end users), RBAC, and the KB governance
-- workflow: a tax_rule_version cannot be published without an approved change
-- request (four-eyes control).
-- =============================================================================

CREATE TABLE admin.admin_user (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    email           citext NOT NULL UNIQUE,
    display_name    text,
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','suspended','disabled')),
    password_hash   text NOT NULL,                   -- Argon2id
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);

CREATE TABLE admin.role (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code        text NOT NULL UNIQUE,                -- 'kb_author','kb_approver','superadmin'
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE admin.permission (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    code        text NOT NULL UNIQUE,                -- 'rule.create','rule.approve','rule.publish'
    description text
);

CREATE TABLE admin.role_permission (
    role_id         uuid NOT NULL REFERENCES admin.role(id) ON DELETE CASCADE,
    permission_id   uuid NOT NULL REFERENCES admin.permission(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

CREATE TABLE admin.admin_user_role (
    admin_user_id   uuid NOT NULL REFERENCES admin.admin_user(id) ON DELETE CASCADE,
    role_id         uuid NOT NULL REFERENCES admin.role(id) ON DELETE CASCADE,
    granted_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (admin_user_id, role_id)
);

-- Now that admin_user exists, wire the deferred FK from 10_ai.ai_explanation.
ALTER TABLE ai.ai_explanation
    ADD CONSTRAINT fk_ai_explanation_reviewer
    FOREIGN KEY (reviewed_by_admin_id) REFERENCES admin.admin_user(id);

-- KB governance: a change request gates status transitions of a rule version.
CREATE TABLE admin.rule_change_request (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    tax_rule_version_id uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    action              text NOT NULL CHECK (action IN ('create','update','publish','retire')),
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','pending','approved','rejected')),
    submitted_by        uuid REFERENCES admin.admin_user(id),
    reviewed_by         uuid REFERENCES admin.admin_user(id),
    submitted_at        timestamptz,
    decided_at          timestamptz,
    notes               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    -- an approver may not be the submitter (four-eyes)
    CHECK (reviewed_by IS NULL OR reviewed_by <> submitted_by)
);
CREATE INDEX ix_change_request_version ON admin.rule_change_request (tax_rule_version_id);
CREATE INDEX ix_change_request_status ON admin.rule_change_request (status);

CREATE TABLE admin.rule_publication (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    tax_rule_version_id uuid NOT NULL REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    change_request_id   uuid REFERENCES admin.rule_change_request(id),
    published_by        uuid REFERENCES admin.admin_user(id),
    published_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_rule_publication_version ON admin.rule_publication (tax_rule_version_id);
