-- =============================================================================
-- Onyx Ledger — 38 · Admission control  (schema: admission)
-- Alembic revision: 0044_admission_control
--
-- Entry 10. Bounds how much expensive work one caller, or the platform, can have
-- in flight — before the expensive work starts.
--
-- WHY POSTGRESQL AND NOT REDIS
-- Redis is already here as the Celery broker, and an atomic INCR is a tempting
-- rate counter. It is the wrong authority for THIS job:
--
--   * a lease must outlive a process crash, and the broker is explicitly
--     ephemeral — a flush would silently delete every protection at once;
--   * the concurrency decision wants to be in the same transaction as the row
--     that records the admitted work, so a rollback releases the slot with it;
--   * this database is already the authority for every other correctness
--     property in the system, and splitting authority across two stores is how
--     you get two answers.
--
-- The cost is one advisory lock plus two small indexed statements on operations
-- that already cost engine runs. That is an acceptable price for expensive
-- paths, and cheap reads take the rate-counter path only (a single UPSERT).
--
-- WHAT IS DELIBERATELY NOT HERE
-- No financial value, no document text, no snapshot, no specification payload.
-- These tables carry a principal id, an operation code, a bounded counter, and
-- timestamps. An admission row leaking would reveal that somebody ran an
-- optimization, never what was in it.
--
-- NO SECURITY DEFINER FUNCTION IS ADDED. Everything below runs as the calling
-- role under ordinary privileges.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS admission;
COMMENT ON SCHEMA admission IS
  'Rate, concurrency and global-capacity accounting for expensive operations. Identifiers, operation codes and counters only — never financial data.';

GRANT USAGE ON SCHEMA admission TO onyx_app_rw;
GRANT USAGE ON SCHEMA admission TO onyx_app_ro;

-- ---------------------------------------------------------------------------
-- 1 · Request-rate counters (fixed window)
--
-- One row per (scope, operation, window). A fixed window is chosen over a
-- sliding log on purpose: a sliding log stores one row per REQUEST, which turns
-- the anti-flooding mechanism into its own flooding vector. The cost of a fixed
-- window is that a caller can spend two windows' allowance across a boundary;
-- the `burst` allowance already assumes tolerance of exactly that shape.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admission.rate_counter (
    scope_type      text        NOT NULL,
    scope_id        text        NOT NULL,
    operation_code  text        NOT NULL,
    window_start    timestamptz NOT NULL,
    request_count   integer     NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_admission_rate_counter
        PRIMARY KEY (scope_type, scope_id, operation_code, window_start),
    CONSTRAINT ck_admission_rate_counter_scope
        CHECK (scope_type IN ('USER', 'ADMIN', 'GLOBAL')),
    CONSTRAINT ck_admission_rate_counter_count
        CHECK (request_count >= 0)
);

COMMENT ON TABLE admission.rate_counter IS
  'Fixed-window request counters. One row per (scope, operation, window); incremented by a single conditional UPSERT so a concurrent burst cannot overshoot the allowance.';
COMMENT ON COLUMN admission.rate_counter.scope_id IS
  'Principal identity as text: a user or admin UUID, or the literal scope name for GLOBAL. Text rather than uuid because the same table serves non-UUID scopes.';

-- Sweeping expired windows is a range scan over this index, never a full scan.
CREATE INDEX IF NOT EXISTS ix_admission_rate_counter_window
    ON admission.rate_counter (window_start);

-- ---------------------------------------------------------------------------
-- 2 · Concurrency leases
--
-- One row per admitted in-flight operation. The row IS the slot: counting live
-- leases is what a limit is compared against, so there is no denormalized
-- counter that can drift away from reality.
--
-- LEASES EXPIRE. That is the whole reason this is a lease and not a flag. A
-- worker that is OOM-killed mid-optimization never runs its release path, and a
-- design that depends on that callback leaks the user's quota permanently —
-- they would be locked out of their own account by a crash they did not cause.
-- `expires_at` makes recovery a property of time rather than of cleanup code
-- having run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admission.lease (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type      text        NOT NULL,
    scope_id        text        NOT NULL,
    operation_code  text        NOT NULL,
    -- Identity of the logical work, when the caller has one. Lets a retry storm
    -- find the operation it already started instead of starting another.
    dedupe_key      text,
    acquired_at     timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    released_at     timestamptz,
    release_reason  text,

    CONSTRAINT ck_admission_lease_scope
        CHECK (scope_type IN ('USER', 'ADMIN', 'GLOBAL')),
    CONSTRAINT ck_admission_lease_window
        CHECK (expires_at > acquired_at),
    CONSTRAINT ck_admission_lease_release_reason
        CHECK (release_reason IS NULL OR release_reason IN
               ('COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED', 'ABANDONED'))
);

COMMENT ON TABLE admission.lease IS
  'One row per admitted in-flight expensive operation. Live rows are the concurrency count; expiry is what returns quota after a worker dies without running its release path.';
COMMENT ON COLUMN admission.lease.dedupe_key IS
  'Bounded identity of the logical work (never a payload). A retry presenting the same key finds the running operation instead of starting a second one.';
COMMENT ON COLUMN admission.lease.expires_at IS
  'After this instant the lease no longer counts against any limit, whether or not it was released. A crashed job therefore cannot hold a user quota forever.';

-- THE hot path: count live leases for one scope+operation. Partial, so it
-- indexes only the rows a limit check actually looks at — released leases are
-- history and are excluded from the index entirely.
CREATE INDEX IF NOT EXISTS ix_admission_lease_live
    ON admission.lease (scope_type, scope_id, operation_code, expires_at)
    WHERE released_at IS NULL;

-- Duplicate-suppression lookup for retry storms.
CREATE INDEX IF NOT EXISTS ix_admission_lease_dedupe
    ON admission.lease (operation_code, dedupe_key, expires_at)
    WHERE released_at IS NULL AND dedupe_key IS NOT NULL;

-- Retention sweep.
CREATE INDEX IF NOT EXISTS ix_admission_lease_expiry
    ON admission.lease (expires_at);

-- ---------------------------------------------------------------------------
-- 3 · Privileges and row-level security
--
-- These tables are NOT tenant-private data, and pretending otherwise would
-- break the mechanism: a global capacity check has to count rows belonging to
-- every user, and an RLS policy keyed on `app.user_id` would make that count
-- silently return only the caller's own rows — a limiter that stops limiting
-- precisely when the platform is busiest.
--
-- The protection is therefore SHAPE, not RLS: there is nothing here worth
-- reading across tenants. No financial column exists, and the API surface never
-- returns a row from either table — a caller learns only its own accept/reject
-- outcome. Cross-tenant reads are prevented by these tables never being exposed,
-- and `tests/security/test_admission_isolation.py` asserts both halves: that no
-- financial column exists, and that no endpoint reveals another principal's
-- admission state.
--
-- `onyx_app_ro` gets SELECT for operational inspection; it can already read
-- everything else and adds no new exposure.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON admission.rate_counter TO onyx_app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON admission.lease          TO onyx_app_rw;
GRANT SELECT ON admission.rate_counter TO onyx_app_ro;
GRANT SELECT ON admission.lease        TO onyx_app_ro;

-- No grant to PUBLIC, and none to the NOLOGIN worker roles: workers release the
-- leases they hold through the ordinary application role, not a privileged path.
REVOKE ALL ON admission.rate_counter FROM PUBLIC;
REVOKE ALL ON admission.lease        FROM PUBLIC;
REVOKE ALL ON SCHEMA admission       FROM PUBLIC;
