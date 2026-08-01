-- =============================================================================
-- Onyx Ledger — 00 · Extensions, roles, shared types, shared functions
-- Alembic revision: 0001_foundation
-- Run FIRST. Requires a superuser / owner connection.
-- =============================================================================

-- ---- Extensions -------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_uuid(), digest(), crypt()
CREATE EXTENSION IF NOT EXISTS citext;      -- case-insensitive email
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- fuzzy / ILIKE search
CREATE EXTENSION IF NOT EXISTS btree_gin;   -- composite GIN (jsonb + scalar)
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector: AI retrieval embeddings

-- ---- Schemas (one per bounded context + shared reference) -------------------
CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS profile;
CREATE SCHEMA IF NOT EXISTS finance;
CREATE SCHEMA IF NOT EXISTS wealth;
CREATE SCHEMA IF NOT EXISTS tax_kb;
CREATE SCHEMA IF NOT EXISTS rules;
CREATE SCHEMA IF NOT EXISTS analysis;
CREATE SCHEMA IF NOT EXISTS reco;
CREATE SCHEMA IF NOT EXISTS ai;
CREATE SCHEMA IF NOT EXISTS docs;
CREATE SCHEMA IF NOT EXISTS admin;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS audit;

-- ---- Shared domain types ----------------------------------------------------
-- Exact money; never float. CAD; multi-currency carries a currency_code FK.
CREATE DOMAIN ref.money_amt AS NUMERIC(14, 2);
-- Rates / percentages expressed as a fraction (0.145 = 14.5%).
CREATE DOMAIN ref.rate AS NUMERIC(9, 6);
-- A calendar tax year (1900..2200 guard).
CREATE DOMAIN ref.tax_year_num AS INTEGER CHECK (VALUE BETWEEN 1900 AND 2200);

-- ---- UUID v7 generator ------------------------------------------------------
-- Time-ordered UUIDs preserve index/heap locality at scale (vs random v4).
-- PostgreSQL < 18 has no built-in uuidv7(); this is a portable implementation.
CREATE OR REPLACE FUNCTION ref.uuid_generate_v7()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    unix_ms bigint := (extract(epoch FROM clock_timestamp()) * 1000)::bigint;
    rand_bytes bytea := gen_random_bytes(10);
    uuid_bytes bytea;
BEGIN
    -- 48 bits unix ms timestamp
    uuid_bytes := set_byte('\x00000000000000000000000000000000'::bytea, 0, (unix_ms >> 40) & 255);
    uuid_bytes := set_byte(uuid_bytes, 1, (unix_ms >> 32) & 255);
    uuid_bytes := set_byte(uuid_bytes, 2, (unix_ms >> 24) & 255);
    uuid_bytes := set_byte(uuid_bytes, 3, (unix_ms >> 16) & 255);
    uuid_bytes := set_byte(uuid_bytes, 4, (unix_ms >> 8) & 255);
    uuid_bytes := set_byte(uuid_bytes, 5, unix_ms & 255);
    -- 74 bits randomness
    uuid_bytes := set_byte(uuid_bytes, 6, get_byte(rand_bytes, 0));
    uuid_bytes := set_byte(uuid_bytes, 7, get_byte(rand_bytes, 1));
    uuid_bytes := set_byte(uuid_bytes, 8, get_byte(rand_bytes, 2));
    uuid_bytes := set_byte(uuid_bytes, 9, get_byte(rand_bytes, 3));
    uuid_bytes := set_byte(uuid_bytes, 10, get_byte(rand_bytes, 4));
    uuid_bytes := set_byte(uuid_bytes, 11, get_byte(rand_bytes, 5));
    uuid_bytes := set_byte(uuid_bytes, 12, get_byte(rand_bytes, 6));
    uuid_bytes := set_byte(uuid_bytes, 13, get_byte(rand_bytes, 7));
    uuid_bytes := set_byte(uuid_bytes, 14, get_byte(rand_bytes, 8));
    uuid_bytes := set_byte(uuid_bytes, 15, get_byte(rand_bytes, 9));
    -- version 7 (high nibble of byte 6) and RFC-4122 variant (top bits of byte 8)
    uuid_bytes := set_byte(uuid_bytes, 6, (get_byte(uuid_bytes, 6) & 15) | 112);
    uuid_bytes := set_byte(uuid_bytes, 8, (get_byte(uuid_bytes, 8) & 63) | 128);
    RETURN encode(uuid_bytes, 'hex')::uuid;
END;
$$;

-- ---- Shared trigger: maintain updated_at ------------------------------------
CREATE OR REPLACE FUNCTION ref.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- ---- Application roles (least privilege) ------------------------------------
-- Passwords/logins are provisioned by the platform operator, not in-repo.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_migrator') THEN
        CREATE ROLE onyx_migrator NOLOGIN;      -- owns DDL; used only by migrations
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_app_rw') THEN
        CREATE ROLE onyx_app_rw NOLOGIN;        -- API runtime: CRUD on user domains
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_app_ro') THEN
        CREATE ROLE onyx_app_ro NOLOGIN;        -- read replicas / reporting
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_kb_admin') THEN
        CREATE ROLE onyx_kb_admin NOLOGIN;      -- tax knowledge base authoring
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onyx_audit_writer') THEN
        CREATE ROLE onyx_audit_writer NOLOGIN;  -- INSERT-only into audit.*
    END IF;
END $$;

-- Schema usage grants (object grants live with each schema's DDL + 16_rls.sql).
GRANT USAGE ON SCHEMA ref, identity, profile, finance, wealth, tax_kb, rules,
    analysis, reco, ai, docs, admin, billing, audit
    TO onyx_app_rw, onyx_app_ro;
GRANT USAGE ON SCHEMA tax_kb, rules, ref TO onyx_kb_admin;
GRANT USAGE ON SCHEMA audit TO onyx_audit_writer;
