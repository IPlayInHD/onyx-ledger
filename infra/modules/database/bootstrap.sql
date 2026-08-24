-- =============================================================================
-- Database bootstrap — run ONCE per environment, as the RDS master user,
-- BEFORE the first migration.
-- =============================================================================
-- This is the ordering `backend/scripts/ci_provision_postgres.sh` uses, adapted
-- to RDS, where there is no true superuser and `rds_superuser` is the ceiling.
--
-- WHY THIS FILE EXISTS AT ALL. Every role the schema defines is NOLOGIN:
--
--     onyx_migrator  onyx_app_rw  onyx_app_ro  onyx_kb_admin
--     onyx_audit_writer  onyx_freshness_worker  onyx_privacy_worker
--
-- They are GROUPS. Nothing can connect as one. The application connects as a
-- LOGIN user that is a MEMBER of the appropriate group, which is exactly what
-- `run_backend_tests.sh` does for the test topology. Provision the groups and
-- stop, and the deployment has no credentials that work.
--
-- ORDER MATTERS AND IS NOT A STYLE PREFERENCE. `onyx_migrator` must exist and
-- must APPLY the schema, because `identity.subject_key_for` is SECURITY DEFINER
-- owned by whoever created it and `audit.log_change` calls it on every write.
-- Apply the schema as the RDS master and `onyx_migrator` is later created by
-- `00_extensions_roles.sql` as a plain NOLOGIN role owning nothing. The schema
-- applies cleanly; the first customer registration fails inside a trigger.
--
-- Passwords are supplied by the deployment pipeline from Secrets Manager and
-- are never written here. `:'migrator_password'` and friends are psql
-- variables: pass them with -v, and psql will not echo them.

\set ON_ERROR_STOP on

-- 1. The migrator. LOGIN, and a member of rds_superuser so it can CREATE
--    EXTENSION. On RDS this is the closest thing to the CI superuser, and it is
--    used by the migration task only — never by a running application.
CREATE ROLE onyx_migrator LOGIN PASSWORD :'migrator_password';
GRANT rds_superuser TO onyx_migrator;

-- 2. Extensions the schema expects. Created here, as the master, so the
--    migration itself does not need to be the thing that installs them.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 3. STOP HERE and run the migration as onyx_migrator:
--
--        ONYX_DATABASE_URL_SYNC=postgresql+psycopg2://onyx_migrator:...@host/onyx \
--        python -m alembic upgrade head
--
--    The group roles below are created by 00_extensions_roles.sql during that
--    run. The runtime logins in step 4 cannot be granted membership until they
--    exist, which is why this file is applied in two passes.
