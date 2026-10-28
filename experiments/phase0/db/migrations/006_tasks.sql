-- =============================================================================
-- PyKnora Phase 0 · P0-T23 · 迁移 006：TaskGraph / Task 事实 / claim lease / wake
-- =============================================================================
-- 归属：P0-T23（w5-t23-scheduler 独占；迁移编号 006 归本任务，007 属于 T27）。
-- 依据：docs/poc/03_Phase0_Task_Backlog.md（P0-T23 任务卡）、
--       docs/tdd/03_Task_Orchestration.md §3/§5（ready/claim/失败传播/对账）、
--       docs/tdd/01_System_Architecture.md §5（状态拥有者）、§6（事务边界）、
--       docs/poc/05_Phase0_Acceptance.md PoC-07（ready 0 提前执行、同 epoch 双领取 0）、
--       docs/poc/04_Phase0_Dependency_DAG.md（Task DAG 只作规划数据，不是运行态）。
--
-- 状态所有权（AGENTS.md / TDD 01 §5，**不得越界**）：
--   * Task 行/claim/wake 事件只由 OrchestrationService（本任务）写；
--   * Job 状态只在 JobService（T08，schema p0_t08）——本 schema 只存 `job_id` 引用，
--     不复制 Job 状态机、不写 p0_t08 的表；
--   * checkpoint 内容属于 LangGraph（T05）——本 schema 只存 `graph_state_ref` 指针，
--     不存 state payload，也不写 p0_t05 的表。
--
-- 单活前提（T05 结论）：PostgresSaver 无法 fence 独立 saver 连接，W3 的 T05 结论是
-- **单活 runner**。因此本迁移用 epoch CAS 防"旧 owner 迟到写"，但**不**假设多实例
-- 并发安全：多实例调度需要 T23 后续或 ADR 重新评审。
--
-- 隔离：本迁移只建 schema `p0_t23`；不引用/不修改 p0_t03/p0_t07/p0_t08/p0_t05。
-- 事务：本文件不含 BEGIN/COMMIT，由调用方（task_scheduler.apply_migration）单事务包裹。
-- 秘密：零凭据；实验栈只连 127.0.0.1，凭据来自 .env.example 的本地占位值。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- §1 schema 与迁移台账
-- -----------------------------------------------------------------------------
CREATE SCHEMA p0_t23;

COMMENT ON SCHEMA p0_t23 IS
    'P0-T23 实验槽：TaskGraph 登记、Task 事实、ready/claim lease+epoch、失败传播、'
    'wake 去重与按 Job 事实对账。Job 状态归 p0_t08，checkpoint 归 p0_t05。';

CREATE TABLE p0_t23.phase0_schema_migrations (
    migration_id text PRIMARY KEY,
    filename     text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

COMMENT ON TABLE p0_t23.phase0_schema_migrations IS
    '迁移台账：文件名 + 内容 sha256。重复 apply 由 PRIMARY KEY 与显式前置检查双重拒绝。';


-- -----------------------------------------------------------------------------
-- §2 TaskGraph 登记（T22 校验后的冻结快照；同一 revision 不可变）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t23.phase0_task_graphs (
    workspace_id  uuid NOT NULL,
    run_id        uuid NOT NULL,
    revision      integer NOT NULL CHECK (revision >= 1),
    graph_sha256  text NOT NULL CHECK (graph_sha256 ~ '^[0-9a-f]{64}$'),
    node_count    integer NOT NULL CHECK (node_count >= 1),
    edge_count    integer NOT NULL CHECK (edge_count >= 0),
    depth         integer NOT NULL CHECK (depth >= 1),
    topo_order    jsonb NOT NULL CHECK (jsonb_typeof(topo_order) = 'array'),
    registered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, revision)
);

COMMENT ON TABLE p0_t23.phase0_task_graphs IS
    '每个 (workspace, run, revision) 一行，内容来自 T22 validate_graph 的冻结结果；'
    'revision 不可变（T24 重规划只能新增 revision，不能改旧行）。';


-- -----------------------------------------------------------------------------
-- §3 Task 事实（唯一写者：OrchestrationService）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t23.phase0_tasks (
    workspace_id     uuid NOT NULL,
    run_id           uuid NOT NULL,
    revision         integer NOT NULL,
    task_id          text NOT NULL CHECK (task_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'),
    kind             text NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
    executor_kind    text NOT NULL CHECK (executor_kind IN ('graph', 'fixed_job')),
    weight           integer NOT NULL CHECK (weight >= 1),
    required         boolean NOT NULL DEFAULT true,
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'running', 'succeeded', 'failed',
                                       'blocked', 'cancelled')),
    epoch            integer NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    attempt          integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    lease_owner      text CHECK (lease_owner IS NULL
                                 OR length(lease_owner) BETWEEN 1 AND 128),
    lease_until      timestamptz,
    job_id           uuid,
    fact             jsonb,
    fact_version     bigint NOT NULL DEFAULT 0 CHECK (fact_version >= 0),
    graph_state_ref  jsonb,
    state_version    bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, revision, task_id),
    CONSTRAINT phase0_tasks_graph_fk
        FOREIGN KEY (workspace_id, run_id, revision)
        REFERENCES p0_t23.phase0_task_graphs (workspace_id, run_id, revision)
        ON DELETE CASCADE,
    -- running 与 lease 必须成对出现；其余状态不得残留 lease。
    CONSTRAINT phase0_tasks_running_lease_ck
        CHECK ((status = 'running') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL)),
    -- succeeded 必须有事实；fact_version 单调递增由本表 CHECK + CAS 共同维护。
    CONSTRAINT phase0_tasks_fact_ck
        CHECK (status <> 'succeeded' OR fact IS NOT NULL),
    CONSTRAINT phase0_tasks_graph_ref_ck
        CHECK (graph_state_ref IS NULL OR status = 'succeeded')
);

COMMENT ON TABLE p0_t23.phase0_tasks IS
    'Task 事实：status 只由 OrchestrationService 转移；epoch 每次领取 +1，旧 epoch 的'
    '完成/引用写入必须 CAS 失败并留痕（phase0_task_claim_events）。';
COMMENT ON COLUMN p0_t23.phase0_tasks.graph_state_ref IS
    'Graph 状态**指针**（thread/checkpoint id/version），payload 归 LangGraph(T05)；'
    '本表不存 checkpoint 内容。';
COMMENT ON COLUMN p0_t23.phase0_tasks.job_id IS
    '固定作业走 JobService(T08) 时返回的 job_id（引用）；Job 状态不复制到本表。';

CREATE INDEX phase0_tasks_ready_idx
    ON p0_t23.phase0_tasks (workspace_id, run_id, revision, status, task_id);
CREATE INDEX phase0_tasks_lease_idx
    ON p0_t23.phase0_tasks (workspace_id, run_id, revision, lease_until)
    WHERE status = 'running';
CREATE INDEX phase0_tasks_job_idx
    ON p0_t23.phase0_tasks (workspace_id, job_id)
    WHERE job_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- §4 依赖边（optional=true 表示"可选依赖"：失败不阻断下游）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t23.phase0_task_edges (
    workspace_id uuid NOT NULL,
    run_id       uuid NOT NULL,
    revision     integer NOT NULL,
    from_task    text NOT NULL,
    to_task      text NOT NULL,
    optional     boolean NOT NULL DEFAULT false,
    PRIMARY KEY (workspace_id, run_id, revision, from_task, to_task),
    CONSTRAINT phase0_task_edges_graph_fk
        FOREIGN KEY (workspace_id, run_id, revision)
        REFERENCES p0_t23.phase0_task_graphs (workspace_id, run_id, revision)
        ON DELETE CASCADE,
    CONSTRAINT phase0_task_edges_from_fk
        FOREIGN KEY (workspace_id, run_id, revision, from_task)
        REFERENCES p0_t23.phase0_tasks (workspace_id, run_id, revision, task_id)
        ON DELETE CASCADE,
    CONSTRAINT phase0_task_edges_to_fk
        FOREIGN KEY (workspace_id, run_id, revision, to_task)
        REFERENCES p0_t23.phase0_tasks (workspace_id, run_id, revision, task_id)
        ON DELETE CASCADE,
    CONSTRAINT phase0_task_edges_no_self_ck CHECK (from_task <> to_task)
);

COMMENT ON TABLE p0_t23.phase0_task_edges IS
    '方向 = 前置 -> 后继（与 T22 TaskGraph.edges 一致）。optional 是"可选依赖"：'
    '该前置失败不阻断 to_task，但仍要等它到达终态。';

CREATE INDEX phase0_task_edges_to_idx
    ON p0_t23.phase0_task_edges (workspace_id, run_id, revision, to_task);


-- -----------------------------------------------------------------------------
-- §5 claim：每 epoch 一行，活跃 claim 唯一（同 epoch 双领取 0）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t23.phase0_task_claims (
    workspace_id uuid NOT NULL,
    run_id       uuid NOT NULL,
    revision     integer NOT NULL,
    task_id      text NOT NULL,
    epoch        integer NOT NULL CHECK (epoch >= 1),
    owner_id     text NOT NULL CHECK (length(owner_id) BETWEEN 1 AND 128),
    claimed_at   timestamptz NOT NULL DEFAULT now(),
    lease_until  timestamptz NOT NULL,
    status       text NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'released', 'expired')),
    closed_at    timestamptz,
    PRIMARY KEY (workspace_id, run_id, revision, task_id, epoch),
    CONSTRAINT phase0_task_claims_task_fk
        FOREIGN KEY (workspace_id, run_id, revision, task_id)
        REFERENCES p0_t23.phase0_tasks (workspace_id, run_id, revision, task_id)
        ON DELETE CASCADE,
    CONSTRAINT phase0_task_claims_lease_ck CHECK (lease_until > claimed_at),
    CONSTRAINT phase0_task_claims_closed_ck
        CHECK ((status = 'active') = (closed_at IS NULL))
);

COMMENT ON TABLE p0_t23.phase0_task_claims IS
    'append-only（每 epoch 一行）：旧 epoch 的 claim 保留，用于证明 CAS 失败与留痕；'
    '活跃 claim 用部分唯一索引约束为 0 或 1 行。';

CREATE UNIQUE INDEX phase0_task_claims_active_uq
    ON p0_t23.phase0_task_claims (workspace_id, run_id, revision, task_id)
    WHERE status = 'active';
CREATE INDEX phase0_task_claims_lease_idx
    ON p0_t23.phase0_task_claims (workspace_id, run_id, revision, lease_until)
    WHERE status = 'active';


-- -----------------------------------------------------------------------------
-- §6 审计事件（追加写；stale epoch / lease 回收 / blocked / wake 都要留痕）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t23.phase0_task_claim_events (
    event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid NOT NULL,
    run_id       uuid NOT NULL,
    revision     integer NOT NULL,
    task_id      text NOT NULL,
    epoch        integer NOT NULL CHECK (epoch >= 0),
    owner_id     text NOT NULL,
    outcome      text NOT NULL CHECK (outcome IN (
        'claimed', 'released', 'lease_expired', 'stale_epoch', 'stale_state_ref',
        'completed', 'failed', 'blocked', 'wake', 'wake_duplicate', 'state_ref_pending',
        'state_ref_attached'
    )),
    detail       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT phase0_task_claim_events_task_fk
        FOREIGN KEY (workspace_id, run_id, revision, task_id)
        REFERENCES p0_t23.phase0_tasks (workspace_id, run_id, revision, task_id)
        ON DELETE CASCADE
);

COMMENT ON TABLE p0_t23.phase0_task_claim_events IS
    'Task 侧审计（追加写）：旧 epoch 领取/完成、lease 回收、失败传播、wake 重复都'
    '必须在这里有行；生产落地按 TDD 07 的隐私规则脱敏后才可外发。';

CREATE INDEX phase0_task_claim_events_task_idx
    ON p0_t23.phase0_task_claim_events (workspace_id, run_id, revision, task_id, event_id);
CREATE INDEX phase0_task_claim_events_outcome_idx
    ON p0_t23.phase0_task_claim_events (outcome, created_at);


-- -----------------------------------------------------------------------------
-- §7 wake 去重（Job 重复完成只唤醒一次；同 key 第二次插入 0 行）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t23.phase0_task_wakes (
    workspace_id uuid NOT NULL,
    run_id       uuid NOT NULL,
    revision     integer NOT NULL,
    task_id      text NOT NULL,
    epoch        integer NOT NULL CHECK (epoch >= 1),
    wake_key     text NOT NULL CHECK (length(wake_key) BETWEEN 1 AND 128),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, revision, task_id, epoch, wake_key),
    CONSTRAINT phase0_task_wakes_task_fk
        FOREIGN KEY (workspace_id, run_id, revision, task_id)
        REFERENCES p0_t23.phase0_tasks (workspace_id, run_id, revision, task_id)
        ON DELETE CASCADE
);

COMMENT ON TABLE p0_t23.phase0_task_wakes IS
    '唤醒去重台账：wake_key 例 job:<uuid>:succeeded。Job 重复完成/重复通知只留一行，'
    '第二次返回 wake_duplicate，不重复推进下游。';
