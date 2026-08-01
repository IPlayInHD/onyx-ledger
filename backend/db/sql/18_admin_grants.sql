-- =============================================================================
-- Onyx Ledger — 18 · Runtime grants for KB authoring / ingestion / admin
-- Alembic revision: 0021_admin_grants
-- In this deployment a single runtime role (onyx_app_rw) serves both the user
-- and admin APIs; authorization is enforced at the APPLICATION layer (admin JWT
-- + permission codes) and by the DATABASE four-eyes CHECK on rule_change_request
-- (reviewer <> submitter). These grants give the runtime role the *capability*
-- to author/publish rules and record documents. (In a split deployment, run the
-- admin service under the dedicated onyx_kb_admin role instead.)
-- =============================================================================

GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA tax_kb, rules, admin TO onyx_app_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA admin TO onyx_app_rw;

ALTER DEFAULT PRIVILEGES IN SCHEMA tax_kb, rules, admin
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO onyx_app_rw;

-- KB embeddings for AI retrieval live in ai.knowledge_embedding (already CRUD
-- for onyx_app_rw); ingestion upserts there after publishing.
