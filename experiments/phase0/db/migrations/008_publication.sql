-- =============================================================================
-- 008_publication.sql —— P0-T13 原子发布：发布事实、发布内 Job、发布审计
-- =============================================================================
-- 归属：P0-T13（原子发布与版本竞态故障注入），迁移编号 008 由 Lead 在
--       docs/poc/evidence/wave-w6-record.md §1.2 判给本任务（001–006 已占用，
--       007 预留给 T27）。
--
-- 依据：
--   * docs/poc/03_Phase0_Task_Backlog.md P0-T13 卡（L218–233）
--   * docs/poc/07_Contract_Closure.md C-01（staging 不可见 / 一次提交 / 锁内不做
--     远端 I/O）、C-08（投递事实归 OutboxService）、C-09（同配置重处理与旧 epoch）
--   * docs/poc/05_Phase0_Acceptance.md PoC-03：原子切换、v1 晚到不覆盖 v2、
--     取消/重建不复活、失败回滚后可见版本不变
--   * docs/tdd/06_Knowledge_Ingestion.md §5–7（版本切换与可见性）
--   * ADR-0002（事务与队列边界，仍为 Proposed，不作为已决结论）
--
-- 与既有契约的偏离（显式记录，不静默）：
--   1. 001 §10 留位要求「后续增量迁移在 p0_t03 内新增独立表」。本迁移改用独立
--      schema ``p0_t13``，原因与 T07/T08/T12 相同：W6 并行子任务各自持有 schema
--      槽位，且 001 的测试 teardown 会 ``DROP SCHEMA p0_t03 CASCADE`` 并
--      ``DROP ROLE p0_app``，共享 schema 会让并行波次互抹。生产落地时应换回共享
--      schema 前缀并补 RLS policy 与 GRANT。
--   2. 本迁移**不建角色、不开 RLS**：P0-T13 的验收面是「原子切换/旧 worker 防护/
--      回滚」，行级隔离已由 P0-T03 在同一实验库证明；避免抢占全局角色名 ``p0_app``。
--   3. 本迁移**不引用** ``p0_t12`` 的表（无跨 schema 外键），保持可独立重建；
--      「同一文档至多一个 active 版本」的唯一性约束没有下沉为跨 schema 局部唯一
--      索引（那会写别人的表），改由 ``index_publication.publish()`` 的行锁 +
--      提交前校验保证。生产化建议与残余风险见 docs/poc/evidence/poc03-publication.md。
--
-- 五态与命名：沿用 C-08 §4 的投递事实口径，但本任务只涉及**发布事实**
-- （publication 一经提交即不可变，没有中间态）与**发布内 Job 的执行状态**
-- （pending/claimed/succeeded/failed）。版本状态（staging/ready/active/retired/
-- failed）仍由 004 的 ``phase0_document_versions`` 持有，本迁移不复刻第二套。
--
-- 幂等：``(workspace_id, document_id, idempotency_key)`` 唯一；
--       ``(workspace_id, document_id, to_version)`` 唯一（同一目标版本只能发布一次，
--       回滚走新版本行，见 C-09「版本不可变」）。
--
-- 本文件不含 BEGIN/COMMIT（由调用方在同一事务内应用并写台账）；
-- ``CREATE SCHEMA`` 刻意不带 ``IF NOT EXISTS``，重复 apply 会显式失败。
-- 本文件不含任何凭据。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- §1 schema 与迁移台账
-- -----------------------------------------------------------------------------
CREATE SCHEMA p0_t13;

CREATE TABLE p0_t13.phase0_schema_migrations (
    migration_id text PRIMARY KEY CHECK (length(migration_id) BETWEEN 1 AND 64),
    filename     text NOT NULL CHECK (length(filename) BETWEEN 1 AND 200),
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

COMMENT ON TABLE p0_t13.phase0_schema_migrations IS
    'P0-T13 迁移台账：每条迁移一行，sha256 是文件字节摘要（可复验）。';


-- -----------------------------------------------------------------------------
-- §2 发布事实（不可变）：一次成功切换 = 一行 publications
-- -----------------------------------------------------------------------------
-- 设计要点：publication 行与「指针切换」在**同一事务**内写入，因此
--   「publication 行存在」⟺「该次切换已提交」。失败/回滚的尝试只写 events，
--   不写 publications —— 这样读者不会看到"半次发布"的事实。
CREATE TABLE p0_t13.phase0_publications (
    workspace_id        uuid NOT NULL,
    publication_id      uuid NOT NULL,
    document_id         uuid NOT NULL,
    kind                text NOT NULL CHECK (kind IN ('publish', 'rollback')),
    -- 本次切换让哪个 index_version 变为 active（回滚时是新版本行）。
    to_version          bigint NOT NULL CHECK (to_version >= 1),
    -- 切换前的活动版本；首次发布为 NULL。
    from_version        bigint CHECK (from_version IS NULL OR from_version >= 1),
    -- 回滚专用：被回滚到内容的那一版（历史版本），publish 必须为 NULL。
    rollback_of_version bigint CHECK (rollback_of_version IS NULL OR rollback_of_version >= 1),
    winning_attempt_id  uuid NOT NULL,
    manifest_checksum   text NOT NULL CHECK (manifest_checksum ~ '^[0-9a-f]{64}$'),
    chunk_count         integer NOT NULL CHECK (chunk_count >= 0),
    idempotency_key     text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    reason              text CHECK (reason IS NULL OR length(reason) <= 256),
    committed_at        timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, publication_id),
    -- 幂等键：同文档同一请求只产生一次可见性变化。
    CONSTRAINT phase0_publications_idem_key
        UNIQUE (workspace_id, document_id, idempotency_key),
    -- 同一目标版本只能发布一次（版本不可变；回滚建新版本行）。
    CONSTRAINT phase0_publications_target_key
        UNIQUE (workspace_id, document_id, to_version),
    CONSTRAINT phase0_publications_kind_shape CHECK (
        (kind = 'publish'
             AND rollback_of_version IS NULL
             AND reason IS NULL)
        OR (kind = 'rollback'
             AND rollback_of_version IS NOT NULL
             AND reason IS NOT NULL)
    ),
    CONSTRAINT phase0_publications_no_self_rollback CHECK (
        rollback_of_version IS NULL OR rollback_of_version <> to_version
    ),
    CONSTRAINT phase0_publications_from_not_target CHECK (
        from_version IS NULL OR from_version <> to_version
    )
);

COMMENT ON TABLE p0_t13.phase0_publications IS
    '发布事实：一行代表一次已提交的活动指针切换；与指针同在发布事务内写入，'
    '因此"行存在"即"切换已提交"。回滚 = 以新版本行重新发布历史内容（版本不可变）。';

CREATE INDEX phase0_publications_document_idx
    ON p0_t13.phase0_publications (workspace_id, document_id, committed_at DESC);


-- -----------------------------------------------------------------------------
-- §3 发布内 Job（C-01：Job 只由发布路径在事务内写）
-- -----------------------------------------------------------------------------
-- 关键约束：jobs.publication_id 外键指向 publications，且 publications 行与 Job
-- 在同一事务内插入 —— 因此**无法**出现"没有发布事实的 Job"，也无法在事务外
-- 单独补投（外键在 COMMIT 时校验）。重复发布请求命中 unique(dedup_key) → 不产生
-- 第二个 Job，这就是"不二次投递"的数据库层证据。
CREATE TABLE p0_t13.phase0_publication_jobs (
    workspace_id  uuid NOT NULL,
    job_id        uuid NOT NULL,
    publication_id uuid NOT NULL,
    kind          text NOT NULL CHECK (kind = 'index.enrichment'),
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'claimed', 'succeeded', 'failed')),
    dedup_key     text NOT NULL CHECK (length(dedup_key) BETWEEN 1 AND 160),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    row_version   bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, job_id),
    CONSTRAINT phase0_publication_jobs_dedup_key UNIQUE (workspace_id, dedup_key),
    CONSTRAINT phase0_publication_jobs_publication_fk
        FOREIGN KEY (workspace_id, publication_id)
        REFERENCES p0_t13.phase0_publications (workspace_id, publication_id)
        ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t13.phase0_publication_jobs IS
    '发布事务内写出的后续 Job（富化/通知等）。外键 + 同事务插入 = Job 不可能脱离'
    '发布事实单独存在；dedup_key 唯一 = 重复请求不二次投递。';

CREATE INDEX phase0_publication_jobs_publication_idx
    ON p0_t13.phase0_publication_jobs (workspace_id, publication_id, created_at);


-- -----------------------------------------------------------------------------
-- §4 发布审计（只追加）：失败、幂等重放、stale、冲突都留痕
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t13.phase0_publication_events (
    workspace_id   uuid NOT NULL,
    event_seq      bigint GENERATED ALWAYS AS IDENTITY,
    event_id       uuid NOT NULL,
    publication_id uuid,
    document_id    uuid NOT NULL,
    index_version  bigint CHECK (index_version IS NULL OR index_version >= 1),
    outcome        text NOT NULL CHECK (outcome IN (
                       'validated',
                       'committed',
                       'rejected',
                       'idempotent_replay',
                       'stale_attempt',
                       'stale_generation',
                       'conflict',
                       'rolled_back'
                   )),
    error_code     text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    detail         text CHECK (detail IS NULL OR length(detail) <= 512),
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, event_id),
    CONSTRAINT phase0_publication_events_seq_key UNIQUE (event_seq),
    CONSTRAINT phase0_publication_events_publication_fk
        FOREIGN KEY (workspace_id, publication_id)
        REFERENCES p0_t13.phase0_publications (workspace_id, publication_id)
        ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t13.phase0_publication_events IS
    '发布审计（只追加）：validated/committed/rejected/idempotent_replay/'
    'stale_attempt/stale_generation/conflict/rolled_back。失败尝试不写 publications，'
    '但必须留下 outcome 与 error_code，禁止静默吞掉 STALE_ATTEMPT。';

CREATE INDEX phase0_publication_events_document_idx
    ON p0_t13.phase0_publication_events (workspace_id, document_id, event_seq);

CREATE INDEX phase0_publication_events_outcome_idx
    ON p0_t13.phase0_publication_events (workspace_id, outcome, event_seq DESC);


-- -----------------------------------------------------------------------------
-- §5 触发器：审计只追加、发布事实不可变、Job 身份不可变与状态机
-- -----------------------------------------------------------------------------
CREATE FUNCTION p0_t13.phase0_publication_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t13
    AS $fn$
    BEGIN
        RAISE EXCEPTION '发布事实/审计只允许追加写，拒绝 % ON %', TG_OP, TG_TABLE_NAME
            USING ERRCODE = 'check_violation';
    END
    $fn$;

CREATE TRIGGER phase0_publications_immutable
    BEFORE UPDATE OR DELETE ON p0_t13.phase0_publications
    FOR EACH ROW EXECUTE FUNCTION p0_t13.phase0_publication_reject_mutation();

CREATE TRIGGER phase0_publication_events_append_only
    BEFORE UPDATE OR DELETE ON p0_t13.phase0_publication_events
    FOR EACH ROW EXECUTE FUNCTION p0_t13.phase0_publication_reject_mutation();


CREATE FUNCTION p0_t13.phase0_publication_job_enforce() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t13
    AS $fn$
    DECLARE
        allowed boolean;
    BEGIN
        IF NEW.workspace_id <> OLD.workspace_id
           OR NEW.job_id <> OLD.job_id
           OR NEW.publication_id <> OLD.publication_id
           OR NEW.kind <> OLD.kind
           OR NEW.dedup_key <> OLD.dedup_key THEN
            RAISE EXCEPTION 'Job 身份列不可变（workspace/job/publication/kind/dedup_key）'
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
                ('pending', 'claimed'),
                ('pending', 'failed'),
                ('claimed', 'pending'),
                ('claimed', 'succeeded'),
                ('claimed', 'failed')
            ) AS allowed(from_status, to_status)
            WHERE allowed.from_status = OLD.status AND allowed.to_status = NEW.status
        ) INTO allowed;
        IF NOT allowed THEN
            RAISE EXCEPTION '非法 Job 状态转移 % -> %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_publication_job_transition
    BEFORE UPDATE ON p0_t13.phase0_publication_jobs
    FOR EACH ROW EXECUTE FUNCTION p0_t13.phase0_publication_job_enforce();


-- -----------------------------------------------------------------------------
-- §6 留位（不在本迁移实现）
-- -----------------------------------------------------------------------------
-- * 「同一文档至多一个 active 版本」的跨 schema 局部唯一索引：需要写 p0_t12 的
--   表（004 属 T12），本迁移只提供代码层行锁 + 提交前校验；生产化建议交给
--   T12/T13 共同修订的迁移（见证据文档 §6）。
-- * reader 单快照语义：本任务用"登记可见性查询在事务内取快照"证明；真实 REPEATABLE
--   READ 下的跨语句一致性属读取契约（T25），本任务不做声明。
-- * 历史引用/GC、对象清理：T13 卡提到但本轮不实现，记 NOT_RUN。
-- * 富化失败是否能把已成功核心版本改 failed：契约要求"成功核心不被富化改 failed"，
--   本任务用 Job 状态机与"发布事实不可变"表达，未做真实富化 worker（NOT_RUN）。
