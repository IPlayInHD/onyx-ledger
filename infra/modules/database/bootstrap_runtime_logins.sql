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

-- BillShield, and it is the separation read in the OTHER direction. The two
-- above exist because a privileged capability must not be assumable from the
-- request path. This one exists because the component that parses untrusted
-- customer bill files must not be able to reach a tax record: it is a member of
-- `onyx_billshield_worker` and nothing else, and that group holds an enumerated
-- allowlist of BillShield verbs, `USAGE` on three schemas, and no privilege
-- anywhere in `finance`, `analysis`, `reco`, `docs`, `ioe` or `tax_kb`.
--
-- Never `IN ROLE onyx_app_rw`, and `onyx_app_rw` is never granted this group:
-- the first would hand the extraction worker the whole application, and the
-- second is PD-16 by name.
--
-- `:billshield_password` IS NOT GENERATED YET, and this script must stay
-- runnable until it is. The declaration lands with the database identity; the
-- generated secret, its Secrets Manager entry and its injection into the
-- dormant `worker-billshield` service land with the deployment, so that a
-- credential is created in the same change that wires the consumer using it.
--
-- Hence the guard. Unconditional `:'billshield_password'` would be a syntax
-- error on every provisioning run made before that change — a whole
-- environment unable to create ANY runtime login because of a feature nobody
-- has enabled. The guard is a psql client-side conditional, so:
--
--   * without the variable, the login is simply not created, and the script
--     exits 0 having provisioned the four runtimes that do have credentials;
--   * with it, the login is created exactly as the three above are.
--
-- There is deliberately NO fallback value. A default, a dummy, or a reuse of
-- another runtime's password would each produce a working credential nobody
-- chose — and reuse in particular would give two services one identity, which
-- is precisely the separation these roles exist to make real.
\if :{?billshield_password}
CREATE ROLE onyx_billshield LOGIN PASSWORD :'billshield_password' IN ROLE onyx_billshield_worker;
\else
\echo '-- onyx_billshield NOT created: no password variable supplied.'
\echo '-- Supply -v billshield_password=... once the credential is provisioned.'
\endif

-- Reporting is read-only and is not wired to anything yet; created so that the
-- day somebody needs it, nobody reaches for the application's credentials.
CREATE ROLE onyx_reporting  LOGIN PASSWORD :'reporting_password' IN ROLE onyx_app_ro;

-- Belt and braces: the runtime identities must not inherit anything else.
-- A membership added later by mistake shows up as a diff against this file.
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- What the privilege suite asserts, restated here so a provisioning run can be
-- checked by eye before the tests get a chance to:
--   * none of these five is a superuser
--   * none holds BYPASSRLS
--   * none owns a schema
--   * onyx_api is NOT a member of onyx_privacy_worker, onyx_freshness_worker,
--     or onyx_billshield_worker
--   * onyx_billshield is a member of onyx_billshield_worker and nothing else
