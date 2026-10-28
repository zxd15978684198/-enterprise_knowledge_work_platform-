-- =============================================================================
-- PyKnora Phase 0 · P0-T20 · 迁移 005：预算预留 / 结算 / 成本账本
-- =============================================================================
-- 归属：P0-T20（w4-t20-budget 独占；迁移编号 005 归本任务）。
-- 依据：docs/poc/03_Phase0_Task_Backlog.md（P0-T20 任务卡：上下文裁剪不破坏工具对、
--       执行预算并发原子归集；量化验收 = 并发账本重复扣费/绕过上限 0、已发未知费用
--       自动释放 0）、docs/tdd/02_Agent_Harness.md §7（Budget 表按 workspace/run 建立
--       同一账本；reserve UNIQUE(workspace_id,run_id,purpose_key)；事务检查
--       spent+reserved+maximum<=limit；settle 原子 reserved-=maximum、spent+=actual；
--       UNIQUE(reservation_id,settlement) 防重复；未知 usage 不得自动释放；
--       actual 超预留记录真实超额并冻结后续）、docs/tdd/01_System_Architecture.md §6。
--
-- 隔离（与 001/003 同策略，Lead 已要求独立 schema）：
--   本迁移只写 schema **p0_t20**，不碰 p0_t03/p0_t06/p0_t07。原因：W4 各子任务并行，
--   且 001 的会话 teardown 会 DROP SCHEMA p0_t03 CASCADE + DROP ROLE p0_app，不能共用。
--   生产落地时 DDL 前缀换成共享 schema 增量；表名/列名/枚举不变。
--
-- RLS 说明（有意不做，非遗漏）：001 用 p0_app + FORCE RLS 证明工作区隔离；本实验
--   schema 只由测试超级用户访问，且 001 的 teardown 会删 p0_app 角色。在此复制 RLS
--   会引入「角色被别的任务 drop」的跨任务耦合，收益为零。生产预算表必须落在 001 的
--   共享 schema 下并继承其 RLS（T31 前需在 TDD07 评审）。该偏离写入交付报告。
--
-- 事务：本文件不含 BEGIN/COMMIT，由调用方（budget_ledger.apply_migration）单事务包裹。
-- 秘密：零凭据；只连 127.0.0.1，凭据取自 .env.example 的本地占位值。
--
-- 金额维度统一**整数 micro-units**（1e-6 货币单位）；不出现浮点列。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- §1 schema 与迁移台账
-- -----------------------------------------------------------------------------
CREATE SCHEMA p0_t20;

COMMENT ON SCHEMA p0_t20 IS
    'P0-T20 实验槽：按 (workspace, run) 的单一预算账本 + 预留/结算/释放事实 + 审计事件。'
    '隔离与 RLS 偏离见文件头；生产必须并入共享 schema 的增量迁移。';

CREATE TABLE p0_t20.phase0_schema_migrations (
    migration_id text PRIMARY KEY,
    filename     text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

COMMENT ON TABLE p0_t20.phase0_schema_migrations IS
    '迁移台账：文件名 + 内容 sha256，供空库重建与漂移检测（与 001 §2 / 003 §1 同构）。';


-- -----------------------------------------------------------------------------
-- §2 预算账本：父 run 是成本总归集主体（TDD02 §7）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t20.phase0_budget_ledgers (
    workspace_id              uuid NOT NULL,
    run_id                    uuid NOT NULL,
    currency                  text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    -- 限额（每维度独立；0 表示该维度禁用）
    limit_tokens              bigint NOT NULL CHECK (limit_tokens >= 0),
    limit_cost_microunits     bigint NOT NULL CHECK (limit_cost_microunits >= 0),
    limit_pages               bigint NOT NULL CHECK (limit_pages >= 0),
    limit_bytes               bigint NOT NULL CHECK (limit_bytes >= 0),
    limit_concurrency         integer NOT NULL CHECK (limit_concurrency >= 0),
    -- 已结算（spent）与在途预留（reserved）
    spent_tokens              bigint NOT NULL DEFAULT 0 CHECK (spent_tokens >= 0),
    spent_cost_microunits     bigint NOT NULL DEFAULT 0 CHECK (spent_cost_microunits >= 0),
    spent_pages               bigint NOT NULL DEFAULT 0 CHECK (spent_pages >= 0),
    spent_bytes               bigint NOT NULL DEFAULT 0 CHECK (spent_bytes >= 0),
    reserved_tokens           bigint NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0),
    reserved_cost_microunits  bigint NOT NULL DEFAULT 0 CHECK (reserved_cost_microunits >= 0),
    reserved_pages            bigint NOT NULL DEFAULT 0 CHECK (reserved_pages >= 0),
    reserved_bytes            bigint NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
    used_concurrency          integer NOT NULL DEFAULT 0 CHECK (used_concurrency >= 0),
    -- 已发出但取不到价格/usage 的金额：保守按预留最大值计入 spent，单独记在 unsettled。
    unsettled_cost_microunits bigint NOT NULL DEFAULT 0 CHECK (unsettled_cost_microunits >= 0),
    -- 超额（actual > maximum）后冻结后续预留；解冻只能人工/新 run，不在本迁移提供。
    frozen                    boolean NOT NULL DEFAULT false,
    freeze_reason             text CHECK (freeze_reason IS NULL
                                          OR length(freeze_reason) BETWEEN 1 AND 64),
    deadline_at               timestamptz,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    row_version               bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, run_id),
    -- 在途预留单独不得超过限额（即使 spent 尚未发生，也不能提前透支）。
    CONSTRAINT phase0_budget_reserved_within_limit CHECK (
        reserved_tokens <= limit_tokens
        AND reserved_cost_microunits <= limit_cost_microunits
        AND reserved_pages <= limit_pages
        AND reserved_bytes <= limit_bytes
        AND used_concurrency <= limit_concurrency
    ),
    -- 正常路径的硬不变量：spent + reserved 不得超过 limit。
    -- 只有「已记录真实超额并冻结」的行允许越过（TDD02 §7：记录真实值 + 冻结后续）。
    CONSTRAINT phase0_budget_no_overdraft CHECK (
        frozen OR (
            spent_tokens + reserved_tokens <= limit_tokens
            AND spent_cost_microunits + reserved_cost_microunits <= limit_cost_microunits
            AND spent_pages + reserved_pages <= limit_pages
            AND spent_bytes + reserved_bytes <= limit_bytes
        )
    ),
    CONSTRAINT phase0_budget_frozen_has_reason CHECK (
        (NOT frozen) OR freeze_reason IS NOT NULL
    )
);

COMMENT ON TABLE p0_t20.phase0_budget_ledgers IS
    '按 (workspace, run) 的单一行账本：父 run 是成本总归集主体，task 只作归因维度（TDD02 §7）。'
    'physical invariant: phase0_budget_no_overdraft 让"绕过应用直接 UPDATE"也无法在未冻结时透支。';

COMMENT ON COLUMN p0_t20.phase0_budget_ledgers.unsettled_cost_microunits IS
    '已发出但 usage/价格不可得时，按预留 maximum 保守计入的金额（不得当 0 计入，也不得自动释放）。';


-- -----------------------------------------------------------------------------
-- §3 预留：唯一 purpose_key 幂等；状态机由触发器约束
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t20.phase0_budget_reservations (
    workspace_id          uuid NOT NULL,
    run_id                uuid NOT NULL,
    reservation_id        uuid NOT NULL,
    purpose_key           text NOT NULL CHECK (length(purpose_key) BETWEEN 1 AND 128),
    kind                  text NOT NULL CHECK (kind IN ('model', 'tool', 'step')),
    status                text NOT NULL DEFAULT 'reserved'
                          CHECK (status IN ('reserved', 'settled', 'unsettled', 'released')),
    maximum_tokens        bigint NOT NULL DEFAULT 0 CHECK (maximum_tokens >= 0),
    maximum_cost_microunits bigint NOT NULL DEFAULT 0 CHECK (maximum_cost_microunits >= 0),
    maximum_pages         bigint NOT NULL DEFAULT 0 CHECK (maximum_pages >= 0),
    maximum_bytes         bigint NOT NULL DEFAULT 0 CHECK (maximum_bytes >= 0),
    reserved_concurrency  integer NOT NULL DEFAULT 0 CHECK (reserved_concurrency BETWEEN 0 AND 4),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    row_version           bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, run_id, reservation_id),
    -- TDD02 §7：reserve(purpose_key) UNIQUE(workspace_id, run_id, purpose_key)
    CONSTRAINT phase0_budget_reservation_purpose_key UNIQUE (workspace_id, run_id, purpose_key),
    CONSTRAINT phase0_budget_reservation_ledger_fk
        FOREIGN KEY (workspace_id, run_id)
        REFERENCES p0_t20.phase0_budget_ledgers (workspace_id, run_id) ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t20.phase0_budget_reservations IS
    '预留事实：同 (workspace,run,purpose_key) 唯一 → 重复 reserve 幂等复用同一行，'
    '不会重复占用额度；终态只有 settled / unsettled / released 三种。';

CREATE INDEX phase0_budget_reservations_status_idx
    ON p0_t20.phase0_budget_reservations (workspace_id, run_id, status);


-- -----------------------------------------------------------------------------
-- §4 结算：UNIQUE(reservation_id, settlement_key) 防重复扣费
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t20.phase0_budget_settlements (
    workspace_id             uuid NOT NULL,
    run_id                   uuid NOT NULL,
    reservation_id           uuid NOT NULL,
    settlement_key           text NOT NULL CHECK (length(settlement_key) BETWEEN 1 AND 128),
    actual_tokens            bigint NOT NULL DEFAULT 0 CHECK (actual_tokens >= 0),
    actual_cost_microunits   bigint NOT NULL DEFAULT 0 CHECK (actual_cost_microunits >= 0),
    actual_pages             bigint NOT NULL DEFAULT 0 CHECK (actual_pages >= 0),
    actual_bytes             bigint NOT NULL DEFAULT 0 CHECK (actual_bytes >= 0),
    -- 实际记账金额：unavailable 时等于预留 maximum（保守值），不是 0。
    effective_cost_microunits bigint NOT NULL CHECK (effective_cost_microunits >= 0),
    cost_source              text NOT NULL
                             CHECK (cost_source IN ('provider_reported', 'estimated', 'unavailable')),
    provisional              boolean NOT NULL DEFAULT false,
    overage                  boolean NOT NULL DEFAULT false,
    overage_cost_microunits  bigint NOT NULL DEFAULT 0 CHECK (overage_cost_microunits >= 0),
    recorded_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, reservation_id, settlement_key),
    CONSTRAINT phase0_budget_settlement_reservation_fk
        FOREIGN KEY (workspace_id, run_id, reservation_id)
        REFERENCES p0_t20.phase0_budget_reservations (workspace_id, run_id, reservation_id)
        ON DELETE RESTRICT,
    -- 取不到价格/usage 的结算必须显式 provisional，禁止伪装成「已计价」。
    CONSTRAINT phase0_budget_settlement_unavailable_is_provisional CHECK (
        cost_source <> 'unavailable' OR provisional
    ),
    -- 已计价的结算必须与 actual 一致；provisional 才允许用 maximum 代替。
    CONSTRAINT phase0_budget_settlement_effective_matches CHECK (
        provisional OR effective_cost_microunits = actual_cost_microunits
    )
);

COMMENT ON TABLE p0_t20.phase0_budget_settlements IS
    '结算事实（追加写）：同 reservation+settlement_key 唯一 → 重复 settle 幂等；'
    'cost_source=unavailable 时必须 provisional=true 且 effective=预留 maximum。';


-- -----------------------------------------------------------------------------
-- §5 审计事件（追加写）：reserve/settle/release/freeze 都留痕
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t20.phase0_budget_events (
    workspace_id  uuid NOT NULL,
    run_id        uuid NOT NULL,
    event_id      uuid NOT NULL,
    -- 同一事务内 now() 相同，用恒增序列给证据一个全序。
    event_seq     bigint GENERATED ALWAYS AS IDENTITY,
    kind          text NOT NULL
                  CHECK (kind IN ('ensure', 'reserve', 'settle', 'release', 'freeze', 'reject')),
    reservation_id uuid,
    detail        text CHECK (detail IS NULL OR length(detail) <= 512),
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, event_id),
    CONSTRAINT phase0_budget_events_seq_key UNIQUE (event_seq)
);

COMMENT ON TABLE p0_t20.phase0_budget_events IS
    '预算审计（仅追加）：detail 只放脱敏原因码/维度名，不放 prompt、参数或凭据。';

CREATE INDEX phase0_budget_events_run_idx
    ON p0_t20.phase0_budget_events (workspace_id, run_id, recorded_at);


-- -----------------------------------------------------------------------------
-- §6 触发器：状态机 / 不可变身份 / 追加写 / 单调版本
-- -----------------------------------------------------------------------------
CREATE FUNCTION p0_t20.phase0_budget_reservation_enforce_transition() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t20
    AS $fn$
    BEGIN
        IF NEW.workspace_id <> OLD.workspace_id
           OR NEW.run_id <> OLD.run_id
           OR NEW.reservation_id <> OLD.reservation_id
           OR NEW.purpose_key <> OLD.purpose_key
           OR NEW.kind <> OLD.kind
           OR NEW.maximum_tokens <> OLD.maximum_tokens
           OR NEW.maximum_cost_microunits <> OLD.maximum_cost_microunits
           OR NEW.maximum_pages <> OLD.maximum_pages
           OR NEW.maximum_bytes <> OLD.maximum_bytes
           OR NEW.reserved_concurrency <> OLD.reserved_concurrency THEN
            RAISE EXCEPTION '预算预留的身份/额度列不可变（换额度必须新建 purpose_key）'
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.row_version <> OLD.row_version + 1 THEN
            RAISE EXCEPTION 'reservation.row_version 必须严格 +1（% → %）',
                OLD.row_version, NEW.row_version
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.status = OLD.status THEN
            RETURN NEW;
        END IF;

        IF NOT (
            (OLD.status = 'reserved' AND NEW.status IN ('settled', 'unsettled', 'released'))
        ) THEN
            RAISE EXCEPTION '非法预留状态转移 % → %（终态不可复活）', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;

        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_budget_reservation_transition
    BEFORE UPDATE ON p0_t20.phase0_budget_reservations
    FOR EACH ROW EXECUTE FUNCTION p0_t20.phase0_budget_reservation_enforce_transition();


CREATE FUNCTION p0_t20.phase0_budget_ledger_enforce_version() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t20
    AS $fn$
    BEGIN
        IF NEW.row_version <> OLD.row_version + 1 THEN
            RAISE EXCEPTION 'ledger.row_version 必须严格 +1（% → %）',
                OLD.row_version, NEW.row_version
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.limit_tokens <> OLD.limit_tokens
           OR NEW.limit_cost_microunits <> OLD.limit_cost_microunits
           OR NEW.currency <> OLD.currency THEN
            RAISE EXCEPTION '预算限额/币种不可变（调整预算必须新建 run 或显式新版本表）'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_budget_ledger_version
    BEFORE UPDATE ON p0_t20.phase0_budget_ledgers
    FOR EACH ROW EXECUTE FUNCTION p0_t20.phase0_budget_ledger_enforce_version();


CREATE FUNCTION p0_t20.phase0_reject_append_only_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t20
    AS $fn$
    BEGIN
        RAISE EXCEPTION '表 % 只允许追加写，拒绝 %', TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'check_violation';
    END
    $fn$;

CREATE TRIGGER phase0_budget_settlements_append_only
    BEFORE UPDATE OR DELETE ON p0_t20.phase0_budget_settlements
    FOR EACH ROW EXECUTE FUNCTION p0_t20.phase0_reject_append_only_mutation();

CREATE TRIGGER phase0_budget_events_append_only
    BEFORE UPDATE OR DELETE ON p0_t20.phase0_budget_events
    FOR EACH ROW EXECUTE FUNCTION p0_t20.phase0_reject_append_only_mutation();


-- -----------------------------------------------------------------------------
-- §7 留位（不在本迁移实现）
-- -----------------------------------------------------------------------------
-- * task 子额度（workspace→run→task 锁序的第三级）：T23/T24 需要时按 001 的复合键
--   增量建表，本迁移只保留 run 级账本，不提前做通用计费平台（Backlog 估计 2 点）。
-- * 价格快照表：本实验由应用层 PriceSnapshot 输入（整数 microunits/百万 token），
--   落库版本化归 T21/T22 评审后再定 schema。
-- * 冻结解除（unfreeze）：TDD02 §7 只要求冻结与告警；人工解冻必须是显式命令 + 审计，
--   不提供隐式自动解冻。
