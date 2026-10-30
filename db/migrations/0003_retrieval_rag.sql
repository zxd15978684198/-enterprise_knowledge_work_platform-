-- 0003_retrieval_rag.sql —— 向量检索、会话与消息（Phase 1 / W20）
-- 依赖：0002_knowledge_documents.sql（app.chunks / app.workspaces / app.touch_updated_at）
--
-- 设计说明：
--   * 向量维度固定 1024（`PYKNORA_EMBEDDING_DIM` 必须与列定义一致，换维度需新迁移）；
--   * 词法检索用 pg_trgm 的 trigram 相似度，而不是 tsvector：PG 内置分词器对中文
--     不切词，`simple` 配置下整段中文只会成为一个 token，FTS 实际不生效。trigram
--     对中文连续文本可用，代价是索引更大、语义弱于真正的 BM25（见 W20 记录 §3 限制）；
--   * 两个索引都建在 app schema 上，检索查询始终带 workspace_id 过滤（租户隔离）。

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- 分块向量：可空，未生成向量的分块只参与词法检索
-- ---------------------------------------------------------------------------
ALTER TABLE app.chunks ADD COLUMN embedding vector(1024);

CREATE INDEX chunks_embedding_hnsw_idx
    ON app.chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX chunks_content_trgm_idx
    ON app.chunks USING gin (content gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- 会话
-- ---------------------------------------------------------------------------
CREATE TABLE app.conversations (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid        NOT NULL REFERENCES app.workspaces (id) ON DELETE CASCADE,
    title        text        NOT NULL DEFAULT '',
    created_by   uuid        NOT NULL REFERENCES app.users (id) ON DELETE RESTRICT,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz,
    CONSTRAINT conversations_title_len CHECK (char_length(title) <= 200)
);

CREATE INDEX conversations_workspace_idx
    ON app.conversations (workspace_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE TRIGGER conversations_touch_updated_at
    BEFORE UPDATE ON app.conversations
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

-- ---------------------------------------------------------------------------
-- 消息：一次提问或一次回答；citations 是结构化引用（不做纯文本拼接）
-- ---------------------------------------------------------------------------
CREATE TABLE app.messages (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid        NOT NULL REFERENCES app.workspaces (id) ON DELETE CASCADE,
    conversation_id uuid        NOT NULL REFERENCES app.conversations (id) ON DELETE CASCADE,
    role            text        NOT NULL,
    content         text        NOT NULL DEFAULT '',
    status          text        NOT NULL DEFAULT 'completed',
    citations       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    trace           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT messages_role_check CHECK (role IN ('user', 'assistant', 'system')),
    CONSTRAINT messages_status_check CHECK (
        status IN ('streaming', 'completed', 'insufficient_evidence', 'failed')
    )
);

CREATE INDEX messages_conversation_idx
    ON app.messages (conversation_id, created_at);
