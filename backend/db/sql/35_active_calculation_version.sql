-- =============================================================================
-- Onyx Ledger — 35 · Active calculation version  (schema: ioe)
-- Alembic revision: 0041_active_calculation_version
--
-- Entry 9. Version-change freshness was emitted from application STARTUP, which
-- is unsafe: a process restart is not a version activation. Replicas start
-- together, workers import the same configuration, rolling deploys restart
-- everything repeatedly, and a process can restart with nothing changed —
-- every one of those would emit, or emit from a process that started before
-- migrations finished.
--
-- Activation becomes a DATABASE decision. This table is the authoritative
-- record of which version of each calculation component is active. Activating
-- is a compare-and-swap under `SELECT ... FOR UPDATE`, with the freshness event
-- inserted in the SAME transaction, so ten concurrent replicas produce exactly
-- one activation and exactly one event.
--
-- Not user data: there is no user_id and no RLS. It is deployment state, the
-- same class as `ioe.weight_config`, readable by the application and writable
-- only through the activation service.
-- =============================================================================

CREATE TABLE IF NOT EXISTS ioe.active_calculation_version (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    -- one row per component; the FreshnessEvent value is the key
    version_type        text NOT NULL UNIQUE,
    active_version      text NOT NULL,
    -- monotonic per component, so an A -> B -> A cycle is three distinct
    -- transitions rather than two that collide on the version string
    activation_revision integer NOT NULL DEFAULT 1
                            CHECK (activation_revision > 0),
    activated_at        timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE ioe.active_calculation_version IS
    'Authoritative active version of each calculation component. Compare-and-swapped under FOR UPDATE by ioe version activation, with the freshness event emitted in the same transaction. Deployment state, not user data: no user_id, no RLS.';
COMMENT ON COLUMN ioe.active_calculation_version.activation_revision IS
    'Monotonic transition counter. Part of the freshness dedupe key so an A->B->A cycle emits three events rather than colliding.';

CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON ioe.active_calculation_version
    FOR EACH ROW EXECUTE FUNCTION ref.set_updated_at();

-- The application activates and reads; nothing else needs write access.
GRANT SELECT, INSERT, UPDATE ON ioe.active_calculation_version TO onyx_app_rw;
GRANT SELECT ON ioe.active_calculation_version TO onyx_app_ro;
