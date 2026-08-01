-- =============================================================================
-- Onyx Ledger — 17 · Supplemental indexes (GIN / BRIN / trigram / vector)
-- Alembic revision: 0018_indexes
-- Per-table btree/FK/partial indexes live inline with each table. This file
-- holds cross-cutting index types, built here so heavy ones can go CONCURRENTLY
-- after seed load in production (drop CONCURRENTLY in a transactional migration).
-- =============================================================================

-- ---- JSONB containment (GIN) ------------------------------------------------
CREATE INDEX gin_user_pref_notif ON profile.user_preference USING gin (notification_prefs);
CREATE INDEX gin_plan_features   ON billing.plan            USING gin (features);
CREATE INDEX gin_snapshot        ON analysis.analysis_input_snapshot USING gin (snapshot);
CREATE INDEX gin_audit_newval    ON audit.audit_log         USING gin (new_value);

-- ---- Trigram search (fuzzy admin lookups) -----------------------------------
CREATE INDEX trgm_tax_rule_name  ON tax_kb.tax_rule         USING gin (name gin_trgm_ops);
CREATE INDEX trgm_fact_key       ON rules.fact_definition   USING gin (fact_key gin_trgm_ops);

-- ---- Time-series BRIN (append-only) -----------------------------------------
CREATE INDEX brin_login_event    ON identity.login_event    USING brin (created_at);
CREATE INDEX brin_ai_message     ON ai.ai_message           USING brin (created_at);
CREATE INDEX brin_analysis_run   ON analysis.analysis_run   USING brin (created_at);

-- ---- Vector ANN (pgvector HNSW) ---------------------------------------------
-- Cosine distance; tune m / ef_construction to corpus size. Build after load.
CREATE INDEX hnsw_knowledge_embedding
    ON ai.knowledge_embedding
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
