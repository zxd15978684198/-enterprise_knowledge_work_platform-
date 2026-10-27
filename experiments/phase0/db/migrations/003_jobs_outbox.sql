-- =============================================================================
-- PyKnora Phase 0 · P0-T07 · 迁移 003：Job / Outbox 生产事务与派发 lease
-- =============================================================================
-- 归属：P0-T07（w3-t07-outbox 独占；迁移编号 003 归本任务）。
-- 依据：docs/poc/03_Phase0_Task_Backlog.md（P0-T07 任务卡）、
--       docs/poc/07_Contract_Closure.md C-01（发布事务中的 Job 唯一写入者，DEFINED）、
--       C-08（Outbox 枚举 / 死信 / 派发恢复，DEFINED）、
--       C-09（同配置重处理与终态作业重试，DEFINED，重试原语归 T08）、
--       docs/poc/05_Phase0_Acceptance.md PoC-02 强制 PASS 项（rollback 50 轮执行 0、
--         断 Redis / 发成未标记 / 死信 redrive 可复验）、
--       docs/tdd/01_System_Architecture.md §6、docs/tdd/07_Data_API_Security.md §3、
--       docs/adr/0002_Outbox_Version_Publication.md（提议，未被本迁移改成"已接受"）。
--
-- 隔离与契约偏离（需 Lead 复核，未修改任何 ADR/TDD）：
--   本迁移写 schema **p0_t07**，不是 001 文末留位的"p0_t03 内增量"。原因：W3 并行
--   子任务各独占一个 PG schema 槽（p0_t05 / p0_t07 / p0_t10 / p0_t15 / p0_t19），
--   且 001 的会话级 teardown 会 DROP SCHEMA p0_t03 CASCADE + DROP ROLE p0_app，
--   并行执行时会互相抹掉对方对象。P0-T07 需要自己的 Job/Outbox 事实表，不能依赖
--   T03 的清理时机。**表名 / 列名 / 枚举严格按 C-08 §4**，生产落地只是把 schema
--   前缀换成共享 schema 的增量迁移（DDL 不变），因此不构成契约变更；该偏离在
--   outbox_probe.py 模块文档与交付报告中逐条记录。
--
-- Outbox 五态（C-08 §4 固定枚举，本迁移不改名）：
--   pending(待派发) / dispatching(派发中，持 lease 正在发) / retry_wait(重试等待)
--   / dispatched(已派发) / dead_letter(死信)
--   注意：dispatched **只说明消息已发出**，不说明 Job 已完成（C-08 §8）；"成功"
--   是 Job 侧事实，归 JobService（T08），dispatcher 不得改 Job 执行状态。
--
-- 事务：本文件不含 BEGIN/COMMIT，由调用方（outbox_probe.apply_migration）单事务包裹。
-- 秘密：本文件零凭据（实验栈只连 127.0.0.1，凭据来自 .env.example 的本地占位值）。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- §1 schema 与迁移台账
-- -----------------------------------------------------------------------------
CREATE SCHEMA p0_t07;

COMMENT ON SCHEMA p0_t07 IS
    'P0-T07 实验槽：业务事实 + Job + Outbox 单事务生产、SKIP LOCKED 派发、'
    'lease/epoch CAS、死信与重试计数。schema 槽位隔离见文件头"隔离与契约偏离"。';

CREATE TABLE p0_t07.phase0_schema_migrations (
    migration_id text PRIMARY KEY,
    filename     text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

COMMENT ON TABLE p0_t07.phase0_schema_migrations IS
    '迁移台账：文件名 + 内容 sha256，供空库重建与漂移检测（与 001 §2 同构）。';


-- -----------------------------------------------------------------------------
-- §2 业务事实：入库目标（与 Job/Outbox 同事务写入，用于证明"要么都在、要么都不在"）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t07.phase0_ingest_targets (
    workspace_id    uuid NOT NULL,
    target_id       uuid NOT NULL,
    name            text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    desired_version bigint NOT NULL DEFAULT 1 CHECK (desired_version >= 1),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, target_id)
);

COMMENT ON TABLE p0_t07.phase0_ingest_targets IS
    '最小业务事实（本实验代替 DocumentVersion）：业务写入与 Job/Outbox 必须同一事务'
    '提交（C-01 §6 / C-06 §6）。真实文档/版本表由 T11/T12 增量迁移新增，不复制本表。';


-- -----------------------------------------------------------------------------
-- §3 Job：执行状态只有 JobService 改；dispatcher 只读
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t07.phase0_jobs (
    workspace_id uuid NOT NULL,
    job_id       uuid NOT NULL,
    target_id    uuid NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('fetch', 'ingest')),
    dedup_key    text NOT NULL CHECK (length(dedup_key) BETWEEN 1 AND 256),
    -- 执行状态沿用 001 §6 的 job 枚举；本表不新增第五种"成功"语义。
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'succeeded', 'failed',
                                   'cancelled', 'superseded')),
    -- Job 侧 epoch 由 JobService（T08 attempt）拥有，这里只留列以申明"各有各的 epoch"。
    lease_epoch  bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    row_version  bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, job_id),
    CONSTRAINT phase0_jobs_dedup_key UNIQUE (workspace_id, kind, dedup_key),
    CONSTRAINT phase0_jobs_target_fk FOREIGN KEY (workspace_id, target_id)
        REFERENCES p0_t07.phase0_ingest_targets (workspace_id, target_id) ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t07.phase0_jobs IS
    '最小 Job 事实：唯一键 (workspace_id, kind, dedup_key) 承担"同一作业重复登记幂等"。'
    '执行状态仅 JobService 写；Outbox dispatcher 不得 UPDATE 本表（C-08 §3）。';

CREATE INDEX phase0_jobs_target_idx ON p0_t07.phase0_jobs (workspace_id, target_id, created_at);


-- -----------------------------------------------------------------------------
-- §4 Outbox：C-08 五态 + lease/epoch + 重试/死信计数
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t07.phase0_outbox (
    workspace_id           uuid NOT NULL,
    outbox_id              uuid NOT NULL,
    job_id                 uuid NOT NULL,
    operation              text NOT NULL CHECK (operation ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    dedup_key              text NOT NULL CHECK (length(dedup_key) BETWEEN 1 AND 256),
    payload_schema_version integer NOT NULL DEFAULT 1 CHECK (payload_schema_version >= 1),
    -- C-08 §4 固定五态；改名需要 ADR/TDD 修订，不在此静默扩展。
    status                 text NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'dispatching', 'retry_wait',
                                             'dispatched', 'dead_letter')),
    -- fencing：每次 claim 严格 +1；旧 epoch 的 CAS 必须 0 行而不是静默成功。
    lease_epoch            bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    lease_expires_at       timestamptz,
    delivery_attempt_count integer NOT NULL DEFAULT 0 CHECK (delivery_attempt_count >= 0),
    max_delivery_attempts  integer NOT NULL DEFAULT 5
                           CHECK (max_delivery_attempts BETWEEN 1 AND 100),
    redrive_count          integer NOT NULL DEFAULT 0 CHECK (redrive_count >= 0),
    last_error_code        text CHECK (last_error_code IS NULL
                                      OR length(last_error_code) BETWEEN 1 AND 64),
    available_at           timestamptz NOT NULL DEFAULT now(),
    last_attempt_at        timestamptz,
    dispatched_at          timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    row_version            bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, outbox_id),
    -- C-08 §7：业务键 (workspace, operation, dedup_key) 唯一 → 重复 append 不改身份。
    CONSTRAINT phase0_outbox_dedup_key UNIQUE (workspace_id, operation, dedup_key),
    -- 同一 Job 的同一 operation 只允许一条投递事实（重复派发复用同一 outbox_id）。
    CONSTRAINT phase0_outbox_job_operation_key UNIQUE (workspace_id, job_id, operation),
    CONSTRAINT phase0_outbox_job_fk FOREIGN KEY (workspace_id, job_id)
        REFERENCES p0_t07.phase0_jobs (workspace_id, job_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_outbox_lease_check CHECK (
        (status = 'dispatching' AND lease_expires_at IS NOT NULL)
        OR (status <> 'dispatching' AND lease_expires_at IS NULL)
    ),
    CONSTRAINT phase0_outbox_dead_letter_check CHECK (
        status <> 'dead_letter' OR last_error_code IS NOT NULL
    )
);

COMMENT ON TABLE p0_t07.phase0_outbox IS
    'Outbox 投递事实（C-08）：pending/dispatching/retry_wait/dispatched/dead_letter 五态。'
    'dispatched 只表示消息已发出，不表示 Job 完成；业务终态在 Job/业务表。';

COMMENT ON COLUMN p0_t07.phase0_outbox.lease_epoch IS
    '派发 fencing 计数：每次 claim 严格 +1（触发器强制）；CAS 更新用旧 epoch 必须 0 行并记 stale。';

-- 派发扫描索引：与 claim 查询的 WHERE/ORDER BY 完全一致（status 部分索引）。
CREATE INDEX phase0_outbox_claim_idx
    ON p0_t07.phase0_outbox (workspace_id, available_at, created_at, outbox_id)
    WHERE status IN ('pending', 'retry_wait');

-- 过期 lease 回收扫描索引。
CREATE INDEX phase0_outbox_lease_idx
    ON p0_t07.phase0_outbox (workspace_id, lease_expires_at)
    WHERE status = 'dispatching';


-- -----------------------------------------------------------------------------
-- §5 投递审计（追加写）：stale / 重试 / 死信 / redrive 都留痕，不能只有返回值
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t07.phase0_outbox_delivery_events (
    workspace_id uuid NOT NULL,
    event_id     uuid NOT NULL,
    -- 追加写序：同一事务内多条事件的 recorded_at 相同（now() 事务级固定），
    -- 用恒增序列给证据一个全序，测试/巡检可以据此还原真实发生顺序。
    event_seq    bigint GENERATED ALWAYS AS IDENTITY,
    outbox_id    uuid NOT NULL,
    lease_epoch  bigint NOT NULL CHECK (lease_epoch >= 0),
    from_status  text CHECK (from_status IS NULL
                             OR from_status IN ('pending', 'dispatching', 'retry_wait',
                                                'dispatched', 'dead_letter')),
    to_status    text NOT NULL
                 CHECK (to_status IN ('pending', 'dispatching', 'retry_wait',
                                      'dispatched', 'dead_letter')),
    outcome      text NOT NULL
                 CHECK (outcome IN ('claimed', 'dispatched', 'retry_wait', 'dead_letter',
                                    'stale', 'reclaimed', 'redrive')),
    error_code   text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    detail       text CHECK (detail IS NULL OR length(detail) <= 512),
    recorded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, event_id),
    CONSTRAINT phase0_outbox_events_seq_key UNIQUE (event_seq),
    CONSTRAINT phase0_outbox_events_outbox_fk FOREIGN KEY (workspace_id, outbox_id)
        REFERENCES p0_t07.phase0_outbox (workspace_id, outbox_id) ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t07.phase0_outbox_delivery_events IS
    '投递审计（仅追加）：outcome=stale 是"旧 epoch 更新 0 行"的证据行，'
    '用于证明旧 worker 不是静默成功而是被显式记录。detail 只放脱敏原因码，不放内容/凭据。';

CREATE INDEX phase0_outbox_events_outbox_idx
    ON p0_t07.phase0_outbox_delivery_events (workspace_id, outbox_id, recorded_at);

CREATE FUNCTION p0_t07.phase0_reject_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t07
    AS $fn$
    BEGIN
        RAISE EXCEPTION '投递审计表只允许追加写，拒绝 %', TG_OP
            USING ERRCODE = 'check_violation';
    END
    $fn$;

CREATE TRIGGER phase0_outbox_events_append_only
    BEFORE UPDATE OR DELETE ON p0_t07.phase0_outbox_delivery_events
    FOR EACH ROW EXECUTE FUNCTION p0_t07.phase0_reject_event_mutation();


-- -----------------------------------------------------------------------------
-- §6 状态机触发器：非法转移 / 不可变字段 / epoch 与 row_version 不变式
-- -----------------------------------------------------------------------------
-- 允许的转移（与 outbox_probe.ALLOWED_TRANSITIONS 镜像，测试交叉核对）：
--   pending       → dispatching            (claim)
--   retry_wait    → dispatching            (claim)
--   dispatching   → dispatched             (mark_dispatched)
--   dispatching   → retry_wait             (retry / lease 过期回收)
--   dispatching   → dead_letter            (重试耗尽 / 回收耗尽)
--   retry_wait    → dead_letter            (人工/巡检判定 poison)
--   dead_letter   → pending                (redrive：旧 epoch 作废，沿用 outbox_id/job_id)
-- 所有其它配对（含 pending → dispatched 这种跳过派发的"伪成功"）在 DB 层直接拒绝，
-- 避免应用层漏判把未派发写成已派发。
CREATE FUNCTION p0_t07.phase0_outbox_enforce_transition() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t07
    AS $fn$
    BEGIN
        IF NEW.workspace_id <> OLD.workspace_id
           OR NEW.outbox_id <> OLD.outbox_id
           OR NEW.job_id <> OLD.job_id
           OR NEW.operation <> OLD.operation
           OR NEW.dedup_key <> OLD.dedup_key
           OR NEW.payload_schema_version <> OLD.payload_schema_version THEN
            RAISE EXCEPTION 'Outbox 身份列不可变（C-08 §7：redrive 沿用 outbox_id/job_id/dedup_key）'
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.lease_epoch < OLD.lease_epoch THEN
            RAISE EXCEPTION 'lease_epoch 不得回退（% → %）', OLD.lease_epoch, NEW.lease_epoch
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.row_version <> OLD.row_version + 1 THEN
            RAISE EXCEPTION 'row_version 必须严格 +1（% → %）：否则并发覆盖不可检测',
                OLD.row_version, NEW.row_version
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.status = OLD.status THEN
            RETURN NEW;  -- 同状态刷新（如只更新 lease 时间）不查转移矩阵
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM (VALUES
                ('pending',     'dispatching'),
                ('retry_wait',  'dispatching'),
                ('dispatching', 'dispatched'),
                ('dispatching', 'retry_wait'),
                ('dispatching', 'dead_letter'),
                ('retry_wait',  'dead_letter'),
                ('dead_letter', 'pending')
            ) AS allowed (from_status, to_status)
            WHERE allowed.from_status = OLD.status
              AND allowed.to_status = NEW.status
        ) THEN
            RAISE EXCEPTION '非法 Outbox 状态转移 % → %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;

        -- claim 必须换 epoch：这是"旧 worker 一定用旧值"的前提。
        IF NEW.status = 'dispatching' AND NEW.lease_epoch <> OLD.lease_epoch + 1 THEN
            RAISE EXCEPTION 'claim 必须令 lease_epoch + 1（% → %）',
                OLD.lease_epoch, NEW.lease_epoch
                USING ERRCODE = 'check_violation';
        END IF;

        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_outbox_transition
    BEFORE UPDATE ON p0_t07.phase0_outbox
    FOR EACH ROW EXECUTE FUNCTION p0_t07.phase0_outbox_enforce_transition();


-- -----------------------------------------------------------------------------
-- §7 延迟约束触发器：提交时校验"每个新 Job 都有 Outbox"
-- -----------------------------------------------------------------------------
-- 需求："已提交 Job 全部有 Outbox"（P0-T07 量化验收）。只在应用层 count 断言是
-- 事后检查；这里用 DEFERRABLE INITIALLY DEFERRED 的约束触发器把规则下沉到提交点：
-- 同事务先 INSERT Job、后 INSERT Outbox 可以通过；只提交 Job 会在 COMMIT 时失败，
-- 因此"只写一半"既不能成功也不能被静默忽略。回滚/崩溃路径不会触发（无提交）。
CREATE FUNCTION p0_t07.phase0_job_requires_outbox() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t07
    AS $fn$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM p0_t07.phase0_outbox AS o
            WHERE o.workspace_id = NEW.workspace_id
              AND o.job_id = NEW.job_id
        ) THEN
            RAISE EXCEPTION 'Job % 在提交时没有 Outbox 投递事实（C-01 §6 / C-06 §6：'
                            '业务 + Job + Outbox 必须同一事务）', NEW.job_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NULL;
    END
    $fn$;

CREATE CONSTRAINT TRIGGER phase0_jobs_require_outbox
    AFTER INSERT ON p0_t07.phase0_jobs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION p0_t07.phase0_job_requires_outbox();


-- -----------------------------------------------------------------------------
-- §8 留位（不在本迁移实现）
-- -----------------------------------------------------------------------------
-- * attempt/heartbeat/retry_terminal/recovery_generation（C-09）归 T08 增量迁移；
--   本迁移的 jobs.lease_epoch 只是占位列，不实现 attempt 语义。
-- * 消费者幂等（同 job 重复消费只产生一个业务结果）归 T08；本迁移只保证消息身份
--   稳定（同一 outbox_id/job_id）与投递计数可观测。
-- * Celery 接入：消息体业务键只用 job_id/outbox_id；broker 的 transport_id 不得占用
--   业务 task_id（P0-T07 集成影响），由 T08 的真实 broker 实验确认。
