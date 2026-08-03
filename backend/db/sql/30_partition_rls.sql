-- =============================================================================
-- Onyx Ledger — 30 · Row-Level Security on table PARTITIONS  (schema: finance)
-- Alembic revision: 0036_partition_rls
--
-- SECURITY FIX — cross-tenant read.
--
-- 16_rls_grants.sql enables and forces RLS on `finance.income_source` and
-- `finance.expense_record`. Both are LIST-partitioned by tax_year, and RLS in
-- PostgreSQL is NOT inherited by partitions: a policy on the parent is applied
-- when the query names the parent, and is bypassed entirely when the query names
-- a partition directly.
--
-- The runtime role holds SELECT on the partitions (granted by
-- `GRANT ... ON ALL TABLES IN SCHEMA finance`), so this was reachable:
--
--     SET app.user_id = '<any uuid>';
--     SELECT count(*) FROM finance.income_source;        -- 0   (policy applied)
--     SELECT count(*) FROM finance.income_source_y2025;  -- 98  (every tenant)
--
-- Verified on a populated database before this migration; see
-- tests/security/test_partition_rls.py, which fails without it.
--
-- The fix enables and forces RLS on every existing partition and attaches the
-- same self-ownership policy the parent carries. An event trigger then applies
-- the same treatment to any partition created later, so next year's
-- `income_source_y2026` cannot reintroduce the hole by being forgotten.
--
-- ADDITIVE. No data is read, moved, or rewritten.
-- =============================================================================

-- ---- 1. Existing partitions -------------------------------------------------
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT n.nspname AS sch, c.relname AS tbl
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_inherits i ON i.inhrelid = c.oid
          JOIN pg_class p ON p.oid = i.inhparent
         WHERE c.relkind = 'r'
           AND p.relrowsecurity          -- the parent is RLS-protected
           AND NOT c.relrowsecurity      -- the partition is not
    LOOP
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', r.sch, r.tbl);
        EXECUTE format('ALTER TABLE %I.%I FORCE ROW LEVEL SECURITY', r.sch, r.tbl);
        EXECUTE format(
            'CREATE POLICY p_self_%s ON %I.%I
                 USING (user_id = ref.current_app_user())
                 WITH CHECK (user_id = ref.current_app_user())',
            r.tbl, r.sch, r.tbl);
        RAISE NOTICE 'RLS applied to partition %.%', r.sch, r.tbl;
    END LOOP;
END $$;

-- ---- 2. Partitions created in future ----------------------------------------
-- A yearly partition is added by hand, and "remember to enable RLS" is exactly
-- the kind of step that gets missed once. The event trigger removes the need to
-- remember: a new partition of an RLS-protected parent is secured at creation.
CREATE OR REPLACE FUNCTION ref.secure_new_partitions()
RETURNS event_trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, ref
AS $$
DECLARE obj record;
BEGIN
    FOR obj IN SELECT * FROM pg_event_trigger_ddl_commands()
    LOOP
        IF obj.command_tag = 'CREATE TABLE' AND obj.object_type = 'table' THEN
            PERFORM 1
              FROM pg_class c
              JOIN pg_inherits i ON i.inhrelid = c.oid
              JOIN pg_class p ON p.oid = i.inhparent
             WHERE c.oid = obj.objid
               AND p.relrowsecurity
               AND NOT c.relrowsecurity
               AND EXISTS (
                   SELECT 1 FROM pg_attribute a
                    WHERE a.attrelid = c.oid AND a.attname = 'user_id'
                      AND NOT a.attisdropped
               );
            IF FOUND THEN
                EXECUTE format(
                    'ALTER TABLE %s ENABLE ROW LEVEL SECURITY', obj.object_identity);
                EXECUTE format(
                    'ALTER TABLE %s FORCE ROW LEVEL SECURITY', obj.object_identity);
                EXECUTE format(
                    'CREATE POLICY p_self_partition ON %s
                         USING (user_id = ref.current_app_user())
                         WITH CHECK (user_id = ref.current_app_user())',
                    obj.object_identity);
                RAISE NOTICE 'RLS auto-applied to new partition %',
                    obj.object_identity;
            END IF;
        END IF;
    END LOOP;
END;
$$;

DROP EVENT TRIGGER IF EXISTS trg_secure_new_partitions;
CREATE EVENT TRIGGER trg_secure_new_partitions
    ON ddl_command_end
    WHEN TAG IN ('CREATE TABLE')
    EXECUTE FUNCTION ref.secure_new_partitions();

COMMENT ON FUNCTION ref.secure_new_partitions() IS
    'Applies ENABLE/FORCE RLS and the self-ownership policy to any new partition of an RLS-protected parent. RLS is not inherited by partitions, and a partition without it is directly readable across tenants.';

-- Same hardening as audit.log_change(): an event-trigger function fires as its
-- owner and needs no EXECUTE grant, so leaving it callable by PUBLIC only adds
-- a definer-rights entry point.
REVOKE EXECUTE ON FUNCTION ref.secure_new_partitions() FROM PUBLIC;
