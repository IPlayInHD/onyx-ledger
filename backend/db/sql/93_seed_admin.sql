-- =============================================================================
-- Onyx Ledger — 93 · Seed: admin RBAC (permissions, roles, role_permission)
-- Alembic revision: 0023_seed_admin (data migration). Admin USERS are
-- provisioned by the platform (AdminService.create_admin), not seeded here.
-- Idempotent.
-- =============================================================================

INSERT INTO admin.permission (code, description) VALUES
    ('rule.author','Create/ingest draft tax rules'),
    ('rule.approve','Approve a rule change request (four-eyes)'),
    ('rule.publish','Publish an approved rule version')
ON CONFLICT (code) DO NOTHING;

INSERT INTO admin.role (code, name) VALUES
    ('kb_author','Knowledge base author'),
    ('kb_approver','Knowledge base approver'),
    ('superadmin','Super administrator')
ON CONFLICT (code) DO NOTHING;

-- kb_author -> author
INSERT INTO admin.role_permission (role_id, permission_id)
SELECT r.id, p.id FROM admin.role r, admin.permission p
WHERE r.code='kb_author' AND p.code IN ('rule.author')
ON CONFLICT DO NOTHING;

-- kb_approver -> approve + publish
INSERT INTO admin.role_permission (role_id, permission_id)
SELECT r.id, p.id FROM admin.role r, admin.permission p
WHERE r.code='kb_approver' AND p.code IN ('rule.approve','rule.publish')
ON CONFLICT DO NOTHING;

-- superadmin -> everything
INSERT INTO admin.role_permission (role_id, permission_id)
SELECT r.id, p.id FROM admin.role r, admin.permission p
WHERE r.code='superadmin' AND p.code IN ('rule.author','rule.approve','rule.publish')
ON CONFLICT DO NOTHING;
