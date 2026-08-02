-- =============================================================================
-- Onyx Ledger — 94 · Seed: TKMS RBAC (granular permissions + roles)
-- Alembic revision: 0025_seed_tkms (data migration). Idempotent.
-- Least privilege: the TKMS pipeline separates who may import, review, approve,
-- publish, and roll back — structurally reinforcing four-eyes governance.
-- Admin USERS are provisioned by the platform, not seeded here.
-- =============================================================================

INSERT INTO admin.permission (code, description) VALUES
    ('tkms.import',   'Create import jobs, upload/re-parse legislation, retry dead-letters'),
    ('tkms.read',     'Read import jobs, extracted rules, validation/change reports, traces'),
    ('tkms.approve',  'Four-eyes approve/reject a draft rule version'),
    ('tkms.publish',  'Publish an approved + validated rule version'),
    ('tkms.rollback', 'Request/approve a legislative rollback to a prior version')
ON CONFLICT (code) DO NOTHING;

INSERT INTO admin.role (code, name) VALUES
    ('kb_importer',  'TKMS importer (ingest + monitor)'),
    ('kb_reviewer',  'TKMS reviewer (read + approve)'),
    ('kb_publisher', 'TKMS publisher (publish + rollback)'),
    ('kb_admin',     'TKMS administrator (all TKMS permissions)')
ON CONFLICT (code) DO NOTHING;

-- kb_importer -> import + read
INSERT INTO admin.role_permission (role_id, permission_id)
SELECT r.id, p.id FROM admin.role r, admin.permission p
WHERE r.code = 'kb_importer' AND p.code IN ('tkms.import','tkms.read')
ON CONFLICT DO NOTHING;

-- kb_reviewer -> read + approve
INSERT INTO admin.role_permission (role_id, permission_id)
SELECT r.id, p.id FROM admin.role r, admin.permission p
WHERE r.code = 'kb_reviewer' AND p.code IN ('tkms.read','tkms.approve')
ON CONFLICT DO NOTHING;

-- kb_publisher -> read + publish + rollback
INSERT INTO admin.role_permission (role_id, permission_id)
SELECT r.id, p.id FROM admin.role r, admin.permission p
WHERE r.code = 'kb_publisher' AND p.code IN ('tkms.read','tkms.publish','tkms.rollback')
ON CONFLICT DO NOTHING;

-- kb_admin -> everything TKMS
INSERT INTO admin.role_permission (role_id, permission_id)
SELECT r.id, p.id FROM admin.role r, admin.permission p
WHERE r.code = 'kb_admin'
  AND p.code IN ('tkms.import','tkms.read','tkms.approve','tkms.publish','tkms.rollback')
ON CONFLICT DO NOTHING;

-- superadmin (from 93_seed_admin) -> also gets every TKMS permission
INSERT INTO admin.role_permission (role_id, permission_id)
SELECT r.id, p.id FROM admin.role r, admin.permission p
WHERE r.code = 'superadmin'
  AND p.code IN ('tkms.import','tkms.read','tkms.approve','tkms.publish','tkms.rollback')
ON CONFLICT DO NOTHING;
