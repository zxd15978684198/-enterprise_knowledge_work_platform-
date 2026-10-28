-- =============================================================================
-- PyKnora Phase 0 · P0-T12 · 迁移 004：版本 staging、Chunk/Embedding 与引用模型
-- =============================================================================
-- 归属：P0-T12（w5-t12 独占；迁移编号 004 归本任务）。
-- 依据：docs/poc/03_Phase0_Task_Backlog.md（P0-T12 任务卡）、
--       docs/poc/07_Contract_Closure.md C-01（发布事务中的 Job 唯一写入者，DEFINED：
--         build 键 (workspace,document_id,index_version)、chunk 键含 attempt、
--         staging 完成不可见、manifest checksum、row_version）、
--       C-09（同配置重处理与终态作业重试，DEFINED：execution_generation、
--         同 version 新 attempt、旧 attempt 不得写当前分区）、
--       C-10（资产归属与 FK 环，NEEDS_POC：本迁移不建 asset_bindings）、
--       docs/poc/05_Phase0_Acceptance.md PoC-03 强制项（staging 未发布新检索 0、
--         重复批次行数不增长、旧 attempt 写当前分区 0、manifest 缺项不能 ready）、
--       docs/tdd/06_Knowledge_Ingestion.md §5/§6、docs/tdd/07_Data_API_Security.md §3。
--
-- 隔离与偏离（与 T07/T08 同类，需 Lead 复核；未修改任何 ADR/TDD）：
--   1. 本迁移写 schema **p0_t12**，不是 001 §10 留位的"p0_t03 内增量"。W5 并行
--      子任务各占 schema 槽，且 001 的会话级 teardown 会 DROP SCHEMA p0_t03
--      CASCADE + DROP ROLE p0_app，跨任务共用 schema 会被互相抹掉。
--      生产落地时把前缀换回共享 schema 的增量迁移（DDL 不变）。
--   2. `phase0_sources` 是**实验本地**的来源身份镜像（workspace/source/version/
--      content_sha256/object_key）。真实来源事实属 T03/T11 的共享表，跨 schema FK
--      在并行槽位上不可靠，所以本迁移只建最小镜像来证明"错误 workspace/source FK"
--      会被拒绝；不复制 T11 的解析/资产表。
--   3. 对象存储（MinIO/S3）本机不可用：本迁移只存 object_key/object_sha256，
--      真实对象字节不落 PG；"对象缺失阻断 ready"由探针的可注入假存储验证，
--      真实对象存储记 NOT_RUN（见交付报告，不冒充已通）。
--   4. C-01 §7 建议 chunk 键含 attempt_id；本轮采用**严格串行 attempt fencing**
--      （同版本同一时刻仅一个 current attempt，切换 attempt 时清空旧 staging 分区），
--      因此 `(workspace,document_id,index_version,chunk_ordinal)` 已足以保证
--      "同文档同版本 chunk 序号唯一"，attempt_id 只作为审计列保留。这是对 C-01
--      的最小偏离，写在模块文档与报告里，不改 ADR。
--
-- 状态机（版本生命周期，TDD06 §5/§6，T13 才允许 active）：
--   staging → ready   (manifest ready + chunk_count 一致 + required keys 齐全)
--   staging → failed  (构建失败)
--   failed  → staging (同 version 新 attempt 重试；C-09「同版本重置执行状态」)
--   ready   → active  (T13 原子发布，本任务不实现、不写 active_version)
--   active  → retired (T13 切版本)
--   其它转移由触发器拒绝；T12 永远不会把版本写成 active。
--
-- 事务：本文件不含 BEGIN/COMMIT，由调用方（index_staging.apply_migration）单事务包裹。
-- 秘密：零凭据。对象字节/内容不入本文件，也不入 PG。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- §1 schema 与迁移台账（重复 apply 必须显式失败：CREATE SCHEMA 无 IF NOT EXISTS）
-- -----------------------------------------------------------------------------
CREATE SCHEMA p0_t12;

COMMENT ON SCHEMA p0_t12 IS
    'P0-T12 实验槽：版本 staging / attempt fencing / chunk 与 locator / manifest。'
    '隔离于 public；来源身份为最小镜像；真实对象存储与 pgvector 为 NOT_RUN。';

CREATE TABLE p0_t12.phase0_schema_migrations (
    migration_id text PRIMARY KEY,
    filename     text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

COMMENT ON TABLE p0_t12.phase0_schema_migrations IS
    '迁移台账：文件名 + 内容 sha256（测试断言 sha256 == 文件字节哈希）。';


-- -----------------------------------------------------------------------------
-- §2 来源身份最小镜像（真实来源事实属 T03/T11；见头部偏离 2）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t12.phase0_sources (
    workspace_id      uuid NOT NULL,
    source_id         uuid NOT NULL,
    source_version_id uuid NOT NULL,
    content_sha256    text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    object_key        text NOT NULL CHECK (length(object_key) BETWEEN 1 AND 512),
    file_name         text NOT NULL CHECK (length(file_name) BETWEEN 1 AND 256),
    format            text NOT NULL CHECK (length(format) BETWEEN 1 AND 16),
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, source_id, source_version_id)
);

COMMENT ON TABLE p0_t12.phase0_sources IS
    '实验本地来源镜像：只存身份与 content_sha256/object_key，不存对象字节。'
    'chunk 的复合 FK 指向它，用于证明错误 workspace/source 引用会被拒绝。';


-- -----------------------------------------------------------------------------
-- §3 文档与期望代数（desired version / generation）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t12.phase0_documents (
    workspace_id       uuid NOT NULL,
    document_id        uuid NOT NULL,
    source_id          uuid NOT NULL,
    source_version_id  uuid NOT NULL,
    desired_generation bigint NOT NULL DEFAULT 1 CHECK (desired_generation >= 1),
    -- T13 的活动指针；T12 永远保持 NULL（不发布）。
    active_version     bigint CHECK (active_version IS NULL OR active_version >= 1),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    row_version        bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, document_id),
    CONSTRAINT phase0_documents_source_fk
        FOREIGN KEY (workspace_id, source_id, source_version_id)
        REFERENCES p0_t12.phase0_sources (workspace_id, source_id, source_version_id)
        ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t12.phase0_documents IS
    '文档的期望状态：desired_generation 是唯一的"想要哪一版"事实；'
    'active_version 只由 T13 写，T12 的任何函数都不碰它。';


-- -----------------------------------------------------------------------------
-- §4 版本（build 键 workspace+document+index_version；T13 才 active）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t12.phase0_document_versions (
    workspace_id       uuid NOT NULL,
    document_id        uuid NOT NULL,
    index_version      bigint NOT NULL CHECK (index_version >= 1),
    status             text NOT NULL DEFAULT 'staging'
                       CHECK (status IN ('staging', 'ready', 'active', 'retired', 'failed')),
    -- 同一时刻只有一个 attempt 可以写本版本分区（切换 attempt 时清空旧 staging）。
    current_attempt_id uuid,
    -- 产出本版本数据的 attempt；ready 前必须锁定（T13 据此核对 winner）。
    winning_attempt_id uuid,
    manifest_checksum  text CHECK (manifest_checksum IS NULL
                                   OR manifest_checksum ~ '^[0-9a-f]{64}$'),
    chunk_count        integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    row_version        bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, document_id, index_version),
    CONSTRAINT phase0_versions_document_fk FOREIGN KEY (workspace_id, document_id)
        REFERENCES p0_t12.phase0_documents (workspace_id, document_id) ON DELETE RESTRICT,
    -- ready/active/retired 必须有 winner 与 manifest checksum（缺项不能 ready 的第一层）。
    CONSTRAINT phase0_versions_ready_check CHECK (
        status NOT IN ('ready', 'active', 'retired')
        OR (winning_attempt_id IS NOT NULL AND manifest_checksum IS NOT NULL)
    )
);

COMMENT ON TABLE p0_t12.phase0_document_versions IS
    '版本分区：staging 期间写入不可见；ready 需要同 attempt 的 ready manifest 与'
    '一致的 chunk_count（触发器强制）；active 只由 T13 写。';

CREATE INDEX phase0_versions_document_status_idx
    ON p0_t12.phase0_document_versions (workspace_id, document_id, status);


-- -----------------------------------------------------------------------------
-- §5 attempt（每版同一时刻最多一个 running；generation 是期望代数）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t12.phase0_index_attempts (
    workspace_id  uuid NOT NULL,
    attempt_id    uuid NOT NULL,
    document_id   uuid NOT NULL,
    index_version bigint NOT NULL,
    generation    bigint NOT NULL CHECK (generation >= 1),
    status        text NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'succeeded', 'failed', 'aborted')),
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    row_version   bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, attempt_id),
    CONSTRAINT phase0_attempts_version_fk
        FOREIGN KEY (workspace_id, document_id, index_version)
        REFERENCES p0_t12.phase0_document_versions (workspace_id, document_id, index_version)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_attempts_generation_check CHECK (generation = index_version)
);

COMMENT ON TABLE p0_t12.phase0_index_attempts IS
    '一次尝试：generation 必须等于版本 index_version（desired 代数即版本代数），'
    '旧 generation/旧 attempt 的写入在 chunks 的触发器与门控 INSERT 上被拒。';

-- 同一版本同一时刻只允许一个 running attempt（切换即旧 attempt 变 aborted）。
CREATE UNIQUE INDEX phase0_attempts_one_running
    ON p0_t12.phase0_index_attempts (workspace_id, document_id, index_version)
    WHERE status = 'running';


-- -----------------------------------------------------------------------------
-- §6 chunk：序号唯一、引用 source/version、locator（offset-free）、embedding
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t12.phase0_index_chunks (
    workspace_id      uuid NOT NULL,
    chunk_id          uuid NOT NULL,
    document_id       uuid NOT NULL,
    index_version     bigint NOT NULL,
    attempt_id        uuid NOT NULL,
    generation        bigint NOT NULL CHECK (generation >= 1),
    chunk_ordinal     integer NOT NULL CHECK (chunk_ordinal >= 0),
    kind              text NOT NULL CHECK (length(kind) BETWEEN 1 AND 32),
    text              text NOT NULL CHECK (length(text) > 0),
    text_sha256       text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    -- T11 的 offset-free locator（schema/kind/format + 结构位置），这里存 JSONB。
    locator           jsonb NOT NULL,
    -- chunk 级内容校验：sha256(text + canonical(locator))，供 manifest 聚合。
    checksum          text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    source_id         uuid NOT NULL,
    source_version_id uuid NOT NULL,
    -- 假 embedding（double precision[]）：本机无 pgvector，见头部偏离 3。
    embedding         double precision[] NOT NULL,
    embedding_dim     integer NOT NULL CHECK (embedding_dim BETWEEN 1 AND 4096),
    embedding_profile text NOT NULL CHECK (length(embedding_profile) BETWEEN 1 AND 64),
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, chunk_id),
    CONSTRAINT phase0_chunks_version_fk
        FOREIGN KEY (workspace_id, document_id, index_version)
        REFERENCES p0_t12.phase0_document_versions (workspace_id, document_id, index_version)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_chunks_attempt_fk FOREIGN KEY (workspace_id, attempt_id)
        REFERENCES p0_t12.phase0_index_attempts (workspace_id, attempt_id)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_chunks_source_fk
        FOREIGN KEY (workspace_id, source_id, source_version_id)
        REFERENCES p0_t12.phase0_sources (workspace_id, source_id, source_version_id)
        ON DELETE RESTRICT,
    -- 验收：同文档同版本的 chunk 序号不可重复（重复批次不增长行数）。
    CONSTRAINT phase0_chunks_ordinal_key
        UNIQUE (workspace_id, document_id, index_version, chunk_ordinal),
    CONSTRAINT phase0_chunks_embedding_len_check
        CHECK (COALESCE(array_length(embedding, 1), 0) = embedding_dim),
    -- locator 必须是结构定位符：必需键在、偏移/跨度键一律禁止。
    CONSTRAINT phase0_chunks_locator_check CHECK (
        locator ? 'schema' AND locator ? 'kind' AND locator ? 'format'
        AND NOT (locator ? 'offset')
        AND NOT (locator ? 'start') AND NOT (locator ? 'end')
        AND NOT (locator ? 'span') AND NOT (locator ? 'span_start')
        AND NOT (locator ? 'span_end')
    )
);

COMMENT ON TABLE p0_t12.phase0_index_chunks IS
    '不可见索引分区的 chunk：序号唯一、结构化 locator、source/version 引用、'
    'double precision[] 假 embedding（真实 pgvector 向量列 NOT_RUN）。';

CREATE INDEX phase0_chunks_version_idx
    ON p0_t12.phase0_index_chunks (workspace_id, document_id, index_version, chunk_ordinal);

CREATE INDEX phase0_chunks_attempt_idx
    ON p0_t12.phase0_index_chunks (workspace_id, attempt_id);


-- -----------------------------------------------------------------------------
-- §7 manifest：必需项齐全才 ready；checksum 覆盖规范化内容
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t12.phase0_index_manifests (
    workspace_id  uuid NOT NULL,
    document_id   uuid NOT NULL,
    index_version bigint NOT NULL,
    attempt_id    uuid NOT NULL,
    status        text NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'ready', 'failed')),
    manifest      jsonb NOT NULL,
    checksum      text CHECK (checksum IS NULL OR checksum ~ '^[0-9a-f]{64}$'),
    missing_items text[] NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    row_version   bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, document_id, index_version),
    CONSTRAINT phase0_manifests_version_fk
        FOREIGN KEY (workspace_id, document_id, index_version)
        REFERENCES p0_t12.phase0_document_versions (workspace_id, document_id, index_version)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_manifests_attempt_fk FOREIGN KEY (workspace_id, attempt_id)
        REFERENCES p0_t12.phase0_index_attempts (workspace_id, attempt_id)
        ON DELETE RESTRICT,
    -- 第二层：ready 必须有 checksum 且没有缺失项。
    CONSTRAINT phase0_manifests_ready_check CHECK (
        status <> 'ready' OR (checksum IS NOT NULL AND cardinality(missing_items) = 0)
    )
);

COMMENT ON TABLE p0_t12.phase0_index_manifests IS
    '版本 manifest：必需项（chunk_count/chunk_digest/embedding_profile/embedding_dim/'
    'source_id/source_version_id/object_key/object_sha256/locator_schema/format/schema）'
    '缺任一即 missing_items 非空、不能 ready；checksum 是对规范化 manifest 的 sha256。';


-- -----------------------------------------------------------------------------
-- §8 审计（只追加）：attempt / chunk 批次 / manifest / ready / stale 都留痕
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t12.phase0_index_events (
    workspace_id  uuid NOT NULL,
    event_id      uuid NOT NULL,
    event_seq     bigint GENERATED ALWAYS AS IDENTITY,
    document_id   uuid NOT NULL,
    index_version bigint,
    attempt_id    uuid,
    outcome       text NOT NULL
                  CHECK (outcome IN ('attempt_started', 'attempt_aborted', 'chunks_staged',
                                     'manifest_draft', 'manifest_ready', 'version_ready',
                                     'stale_attempt', 'stale_generation', 'batch_rejected')),
    error_code    text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    detail        text CHECK (detail IS NULL OR length(detail) <= 512),
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, event_id),
    CONSTRAINT phase0_events_seq_key UNIQUE (event_seq)
);

CREATE INDEX phase0_events_document_idx
    ON p0_t12.phase0_index_events (workspace_id, document_id, event_seq);


-- -----------------------------------------------------------------------------
-- §9 触发器：版本转移 + ready 前置校验 + chunk attempt/generation fencing + rw 不变式
-- -----------------------------------------------------------------------------
CREATE FUNCTION p0_t12.phase0_version_enforce_transition() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t12
    AS $fn$
    DECLARE
        allowed boolean;
        missing text[];
    BEGIN
        IF NEW.workspace_id <> OLD.workspace_id
           OR NEW.document_id <> OLD.document_id
           OR NEW.index_version <> OLD.index_version THEN
            RAISE EXCEPTION '版本身份列不可变（workspace/document/index_version）'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.row_version <> OLD.row_version + 1 THEN
            RAISE EXCEPTION 'row_version 必须严格 +1：% -> %',
                OLD.row_version, NEW.row_version USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.status = OLD.status THEN
            RETURN NEW;
        END IF;
        SELECT EXISTS (
            SELECT 1 FROM (VALUES
                ('staging', 'ready'),
                ('staging', 'failed'),
                ('failed', 'staging'),
                ('ready', 'active'),
                ('active', 'retired')
            ) AS allowed(from_status, to_status)
            WHERE allowed.from_status = OLD.status AND allowed.to_status = NEW.status
        ) INTO allowed;
        IF NOT allowed THEN
            RAISE EXCEPTION '非法版本状态转移 % -> %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.status = 'ready' THEN
            IF NOT EXISTS (
                SELECT 1 FROM p0_t12.phase0_index_manifests AS m
                WHERE m.workspace_id = NEW.workspace_id
                  AND m.document_id = NEW.document_id
                  AND m.index_version = NEW.index_version
                  AND m.status = 'ready'
                  AND m.attempt_id = NEW.winning_attempt_id
            ) THEN
                RAISE EXCEPTION 'MANIFEST_INCOMPLETE：版本 ready 需要同一 attempt 的 ready manifest'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT ARRAY(
                SELECT key FROM unnest(ARRAY[
                    'schema', 'chunk_count', 'chunk_digest', 'embedding_profile',
                    'embedding_dim', 'source_id', 'source_version_id',
                    'object_key', 'object_sha256', 'locator_schema', 'format'
                ]) AS key
                WHERE NOT EXISTS (
                    SELECT 1 FROM p0_t12.phase0_index_manifests AS m
                    WHERE m.workspace_id = NEW.workspace_id
                      AND m.document_id = NEW.document_id
                      AND m.index_version = NEW.index_version
                      AND m.manifest ? key
                )
            ) INTO missing;
            IF cardinality(missing) > 0 THEN
                RAISE EXCEPTION 'MANIFEST_INCOMPLETE：manifest 缺必需项 %', missing
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.chunk_count <> (
                SELECT count(*) FROM p0_t12.phase0_index_chunks AS c
                WHERE c.workspace_id = NEW.workspace_id
                  AND c.document_id = NEW.document_id
                  AND c.index_version = NEW.index_version
            ) THEN
                RAISE EXCEPTION 'MANIFEST_INCOMPLETE：chunk_count=% 与实际行数不一致',
                    NEW.chunk_count USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_versions_transition
    BEFORE UPDATE ON p0_t12.phase0_document_versions
    FOR EACH ROW EXECUTE FUNCTION p0_t12.phase0_version_enforce_transition();

-- chunk 写入的兜底 fencing：即使应用层漏了门控，旧 attempt/generation 也写不进去。
CREATE FUNCTION p0_t12.phase0_chunk_attempt_fence() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t12
    AS $fn$
    DECLARE
        v_current uuid;
        v_generation bigint;
    BEGIN
        SELECT current_attempt_id INTO v_current
          FROM p0_t12.phase0_document_versions
         WHERE workspace_id = NEW.workspace_id
           AND document_id = NEW.document_id
           AND index_version = NEW.index_version;
        SELECT desired_generation INTO v_generation
          FROM p0_t12.phase0_documents
         WHERE workspace_id = NEW.workspace_id AND document_id = NEW.document_id;
        IF v_current IS NULL OR v_current <> NEW.attempt_id THEN
            RAISE EXCEPTION 'STALE_ATTEMPT：attempt % 不是版本当前 attempt %',
                NEW.attempt_id, COALESCE(v_current::text, '<none>')
                USING ERRCODE = 'check_violation';
        END IF;
        IF v_generation IS NULL OR v_generation <> NEW.generation THEN
            RAISE EXCEPTION 'STALE_GENERATION：generation % 已被 desired % 取代',
                NEW.generation, COALESCE(v_generation::text, '<none>')
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_chunks_attempt_fence
    BEFORE INSERT ON p0_t12.phase0_index_chunks
    FOR EACH ROW EXECUTE FUNCTION p0_t12.phase0_chunk_attempt_fence();

CREATE FUNCTION p0_t12.phase0_reject_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t12
    AS $fn$
    BEGIN
        RAISE EXCEPTION '索引审计表只允许追加写，拒绝 %', TG_OP
            USING ERRCODE = 'check_violation';
    END
    $fn$;

CREATE TRIGGER phase0_events_append_only
    BEFORE UPDATE OR DELETE ON p0_t12.phase0_index_events
    FOR EACH ROW EXECUTE FUNCTION p0_t12.phase0_reject_event_mutation();


-- -----------------------------------------------------------------------------
-- §10 留位（不在本迁移实现）
-- -----------------------------------------------------------------------------
-- * active 指针切换、retired 转换、旧版本清理：T13（本迁移只提供 ready/retired 的
--   转移规则，T12 不写 documents.active_version）。
-- * pgvector 向量列与 HNSW/IVFFlat 索引：本机无 `vector` 扩展（已实测
--   pg_available_extensions 为空），本迁移用 double precision[] + embedding_dim，
--   真实向量检索 NOT_RUN，不冒充已通。
-- * asset_bindings/asset owner（C-10）：T11/T13 增量迁移，本迁移只存 object_key。
-- * FTS：pg_trgm 可用但本任务只用确定性 ILIKE 做可见性探针，不做检索质量声明。
