-- =============================================================================
-- Onyx Ledger — 39 · Admission: pre-authentication scopes  (schema: admission)
-- Alembic revision: 0045_admission_preauth_scopes
--
-- Entry 10 Phase 2. Throttling the login surface introduced the first two scopes
-- that are NOT an authenticated principal:
--
--   IP            the transport peer address, as a keyed digest
--   AUTH_SUBJECT  the identity the caller CLAIMS to be, as a keyed digest
--
-- Both exist because `AUTH_ATTEMPT` runs before anyone is known — the operation
-- IS the act of finding out. Everything else in the system bills an operation to
-- a principal the server issued.
--
-- WHY ONLY `rate_counter`
-- `admission.lease` keeps its original constraint on purpose. A lease is a slot
-- held by in-flight work, and `AUTH_ATTEMPT` declares no concurrency control, so
-- a pre-authentication lease should not exist. Leaving the lease constraint
-- narrow makes that an ENFORCED invariant rather than a comment: if a future
-- change gives the auth class a concurrency limit without thinking it through,
-- the insert fails loudly here instead of quietly letting an unauthenticated
-- caller hold a slot.
--
-- WHAT IS STORED
-- Still no personal data. `scope_id` for these two scopes is an HMAC-SHA256
-- digest under a dedicated secret (see app/services/admission/identity.py), so
-- the table cannot be read as a list of the email addresses or source addresses
-- that touched the login endpoint.
--
-- COLUMN-COMPATIBLE. No column, index, grant, policy or row is changed; one
-- CHECK constraint is replaced by a wider one. Existing rows all satisfy the
-- wider predicate, so the validation scan cannot fail.
-- =============================================================================

ALTER TABLE admission.rate_counter
    DROP CONSTRAINT IF EXISTS ck_admission_rate_counter_scope;

ALTER TABLE admission.rate_counter
    ADD CONSTRAINT ck_admission_rate_counter_scope
        CHECK (scope_type IN ('USER', 'ADMIN', 'GLOBAL', 'IP', 'AUTH_SUBJECT'));

COMMENT ON COLUMN admission.rate_counter.scope_id IS
  'Principal identity as text: a user or admin UUID, the literal scope name for GLOBAL, or a keyed HMAC digest for the pre-authentication IP and AUTH_SUBJECT scopes. Never a plaintext email address or source address.';
