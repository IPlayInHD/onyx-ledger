-- =============================================================================
-- Onyx Ledger — 10 · AI Knowledge & Conversations  (schema: ai)
-- Alembic revision: 0011_ai
-- Chat + retrieval + explainability. Every AI message can cite the exact tax
-- rule versions / analyses / recommendations that grounded it.
-- =============================================================================

CREATE TABLE ai.ai_conversation (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    user_id     uuid NOT NULL REFERENCES identity.user_account(id) ON DELETE CASCADE,
    title       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX ix_ai_conversation_user ON ai.ai_conversation (user_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE ai.ai_message (
    id              uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    conversation_id uuid NOT NULL REFERENCES ai.ai_conversation(id) ON DELETE CASCADE,
    role            text NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content         text NOT NULL,
    model           text,
    token_count     integer,
    confidence_score smallint CHECK (confidence_score BETWEEN 0 AND 100),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_ai_message_conversation ON ai.ai_message (conversation_id, created_at);

-- Traceability: which rule/analysis/recommendation grounded an assistant turn.
CREATE TABLE ai.ai_message_citation (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    message_id          uuid NOT NULL REFERENCES ai.ai_message(id) ON DELETE CASCADE,
    tax_rule_version_id uuid REFERENCES tax_kb.tax_rule_version(id),
    analysis_id         uuid REFERENCES analysis.analysis_run(id),
    recommendation_id   uuid REFERENCES reco.recommendation(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (tax_rule_version_id IS NOT NULL OR analysis_id IS NOT NULL OR recommendation_id IS NOT NULL)
);
CREATE INDEX ix_ai_citation_message ON ai.ai_message_citation (message_id);

-- Retrieved context snapshot for a turn (JSONB justified: arbitrary payload).
CREATE TABLE ai.ai_prompt_context (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    message_id  uuid NOT NULL REFERENCES ai.ai_message(id) ON DELETE CASCADE,
    context     jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Generated explanations (referenced by rules; reviewable by admins).
CREATE TABLE ai.ai_explanation (
    id                  uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    tax_rule_version_id uuid REFERENCES tax_kb.tax_rule_version(id) ON DELETE CASCADE,
    content             text NOT NULL,
    generated_by        text,                        -- model id
    reviewed_by_admin_id uuid,                        -- FK added in 12_admin
    reviewed_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- Vector store for retrieval (pgvector). Embedding dim set for the chosen model.
CREATE TABLE ai.knowledge_embedding (
    id          uuid PRIMARY KEY DEFAULT ref.uuid_generate_v7(),
    source_type text NOT NULL CHECK (source_type IN ('rule_version','explanation','doc_chunk')),
    source_id   uuid NOT NULL,                       -- polymorphic (app-enforced)
    tax_year    ref.tax_year_num,
    content     text NOT NULL,                       -- copy of chunk (no join on read)
    embedding   vector(1536),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_embedding_source ON ai.knowledge_embedding (source_type, source_id);
CREATE INDEX ix_embedding_year ON ai.knowledge_embedding (tax_year);
-- HNSW index for approximate nearest-neighbour search (created in 17_indexes.sql
-- so it can be built CONCURRENTLY after seed load).
