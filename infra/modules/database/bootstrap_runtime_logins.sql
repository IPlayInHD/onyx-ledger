-- =============================================================================
-- Runtime logins — the SECOND pass, after the first migration has run.
-- =============================================================================
-- The schema's group roles now exist. These are the identities that actually
-- connect, each a member of exactly one group and nothing else.
--
-- The separation is not decoration. PD-16 was precisely this boundary being
-- reachable: a `GRANT onyx_freshness_worker TO onyx_app_rw` let any request-path
-- session assume the privileged role and call cross-tenant keyholes. The
-- remediation holds only if production issues genuinely separate credentials,
-- so these three must never be granted each other's groups.

\set ON_ERROR_STOP on

CREATE ROLE onyx_api        LOGIN PASSWORD :'api_password'       IN ROLE onyx_app_rw;
CREATE ROLE onyx_privacy    LOGIN PASSWORD :'privacy_password'   IN ROLE onyx_privacy_worker;
CREATE ROLE onyx_freshness  LOGIN PASSWORD :'freshness_password' IN ROLE onyx_freshness_worker;

-- Reporting is read-only and is not wired to anything yet; created so that the
-- day somebody needs it, nobody reaches for the application's credentials.
CREATE ROLE onyx_reporting  LOGIN PASSWORD :'reporting_password' IN ROLE onyx_app_ro;

-- Belt and braces: the runtime identities must not inherit anything else.
-- A membership added later by mistake shows up as a diff against this file.
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- What the privilege suite asserts, restated here so a provisioning run can be
-- checked by eye before the tests get a chance to:
--   * none of these four is a superuser
--   * none holds BYPASSRLS
--   * none owns a schema
--   * onyx_api is NOT a member of onyx_privacy_worker or onyx_freshness_worker
