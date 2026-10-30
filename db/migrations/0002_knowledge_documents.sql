-- 0002_knowledge_documents.sql —— 知识库、文档与分块（Phase 1 / W19）
-- 依赖：0001_identity.sql（app.users / app.workspaces / app.touch_updated_at）

-- ---------------------------------------------------------------------------
-- 知识库
-- ---------------------------------------------------------------------------
CREATE TABLE app.knowledge_bases (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid        NOT NULL REFERENCES app.workspaces (id) ON DELETE CASCADE,
    name         text        NOT NULL,
    description  text        NOT NULL DEFAULT '',
    created_by   uuid        NOT NULL REFERENCES app.users (id) ON DELETE RESTRICT,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz,
    CONSTRAINT knowledge_bases_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT knowledge_bases_name_len CHECK (char_length(name) <= 120),
    CONSTRAINT knowledge_bases_workspace_name_key UNIQUE (workspace_id, name)
);

CREATE INDEX knowledge_bases_workspace_idx
    ON app.knowledge_bases (workspace_id)
    WHERE deleted_at IS NULL;

CREATE TRIGGER knowledge_bases_touch_updated_at
    BEFORE UPDATE ON app.knowledge_bases
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

-- ---------------------------------------------------------------------------
-- 文档：一次上传/导入一行，status 表示入库流水线状态
-- ---------------------------------------------------------------------------
CREATE TABLE app.documents (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      uuid        NOT NULL REFERENCES app.workspaces (id) ON DELETE CASCADE,
    knowledge_base_id uuid        NOT NULL REFERENCES app.knowledge_bases (id) ON DELETE CASCADE,
    title             text        NOT NULL,
    source_type       text        NOT NULL DEFAULT 'upload',
    source_url        text,
    filename          text        NOT NULL DEFAULT '',
    mime_type         text        NOT NULL DEFAULT 'application/octet-stream',
    size_bytes        bigint      NOT NULL DEFAULT 0,
    checksum          text        NOT NULL DEFAULT '',
    storage_key       text        NOT NULL DEFAULT '',
    status            text        NOT NULL DEFAULT 'pending',
    error             text,
    version           integer     NOT NULL DEFAULT 1,
    chunk_count       integer     NOT NULL DEFAULT 0,
    created_by        uuid        NOT NULL REFERENCES app.users (id) ON DELETE RESTRICT,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    deleted_at        timestamptz,
    CONSTRAINT documents_title_not_blank CHECK (btrim(title) <> ''),
    CONSTRAINT documents_source_type_check CHECK (source_type IN ('upload', 'url')),
    CONSTRAINT documents_status_check
        CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    CONSTRAINT documents_size_non_negative CHECK (size_bytes >= 0),
    CONSTRAINT documents_version_positive CHECK (version >= 1),
    CONSTRAINT documents_chunk_count_non_negative CHECK (chunk_count >= 0)
);

CREATE INDEX documents_kb_idx
    ON app.documents (knowledge_base_id, created_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX documents_workspace_status_idx
    ON app.documents (workspace_id, status)
    WHERE deleted_at IS NULL;
-- 同一知识库内相同内容视为同一文档（幂等去重）；URL 来源与历史空校验和不参与。
CREATE UNIQUE INDEX documents_kb_checksum_key
    ON app.documents (knowledge_base_id, checksum)
    WHERE checksum <> '' AND deleted_at IS NULL;

CREATE TRIGGER documents_touch_updated_at
    BEFORE UPDATE ON app.documents
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

-- ---------------------------------------------------------------------------
-- 分块：正文 + 全文检索列（向量列在 W20 引入）
-- ---------------------------------------------------------------------------
CREATE TABLE app.chunks (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid        NOT NULL REFERENCES app.workspaces (id) ON DELETE CASCADE,
    document_id  uuid        NOT NULL REFERENCES app.documents (id) ON DELETE CASCADE,
    ordinal      integer     NOT NULL,
    content      text        NOT NULL,
    char_count   integer     NOT NULL,
    page         integer,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chunks_ordinal_non_negative CHECK (ordinal >= 0),
    CONSTRAINT chunks_content_not_blank CHECK (btrim(content) <> ''),
    CONSTRAINT chunks_char_count_positive CHECK (char_count > 0),
    CONSTRAINT chunks_document_ordinal_key UNIQUE (document_id, ordinal)
);

CREATE INDEX chunks_document_idx ON app.chunks (document_id, ordinal);
CREATE INDEX chunks_workspace_idx ON app.chunks (workspace_id);
CREATE INDEX chunks_fts_idx ON app.chunks USING gin (to_tsvector('simple', content));
