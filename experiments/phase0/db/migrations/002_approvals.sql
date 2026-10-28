-- =============================================================================
-- PyKnora Phase 0 · P0-T06 · 迁移 002：审批绑定 / 工具调用意图 / append-only 账本
-- =============================================================================
-- 归属：P0-T06（w4-t06-approval 独占；迁移编号 002 归本任务）。
-- 依据：docs/poc/03_Phase0_Task_Backlog.md（P0-T06 任务卡，lines 99–114）、
--       docs/poc/07_Contract_Closure.md C-04（稳定工具意图、审批与外部提交，NEEDS_POC）、
--       docs/tdd/02_Agent_Harness.md §3/§4/§5（工具生命周期、审批绑定、崩溃恢复）、
--       docs/tdd/04_LangGraph_Workflows.md §7（approval_wait 只等待、不执行外部写）、
--       docs/poc/05_Phase0_Acceptance.md 故障矩阵 F3/F4/F5、
--       docs/adr/0003_Approval_Side_Effects.md（**提议**，本迁移没有把它变成"已接受"）。
--
-- 隔离与契约偏离（需 Lead 复核，未修改任何 ADR/TDD）：
--   本迁移写 schema **p0_t06**，不是 001 文末留位的"p0_t03 内增量"。原因同 003：
--   W4 并行子任务各独占一个 PG schema 槽（p0_t06 / p0_t08 …），且 001 的会话级
--   teardown 会 DROP SCHEMA p0_t03 CASCADE + DROP ROLE p0_app，并行执行会互相抹掉
--   对象。表名/列名/枚举严格按 C-04 §4 与 TDD02 §4 的字段表，生产落地只是把 schema
--   前缀换成共享 schema 的增量迁移（DDL 与语义不变）。
--
-- 唯一写入者（C-04 §3，本迁移用触发器兜底，不靠应用层自觉）：
--   ApprovalManager 只写 phase0_approvals / phase0_approval_events；
--   ToolExecutor   只写 phase0_tool_calls / phase0_tool_receipts；
--   ModelGateway   只写 phase0_response_artifacts；
--   RunService 的 Run 状态不在本 schema（归 T05/T21），本迁移不碰。
--
-- 事务：本文件不含 BEGIN/COMMIT，由调用方（approval_probe.apply_migration）单事务包裹。
-- 秘密：零凭据。下游调用只连 127.0.0.1 的控制站（T04）；本迁移不存任何凭据/参数原文，
--       只存哈希与脱敏摘要（result_summary jsonb 由调用方保证脱敏）。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- §1 schema 与迁移台账
-- -----------------------------------------------------------------------------
CREATE SCHEMA p0_t06;

COMMENT ON SCHEMA p0_t06 IS
    'P0-T06 实验槽：工具意图 / 审批绑定 / append-only 调用账本 / 未知写核查。'
    'schema 槽位隔离见文件头"隔离与契约偏离"。';

CREATE TABLE p0_t06.phase0_schema_migrations (
    migration_id text PRIMARY KEY,
    filename     text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

COMMENT ON TABLE p0_t06.phase0_schema_migrations IS
    '迁移台账：文件名 + 内容 sha256，供空库重建与漂移检测（与 001 §2、003 §1 同构）。';


-- -----------------------------------------------------------------------------
-- §2 最小工作区 / 主体替身（只用于"撤 requester / approver"注入，不是身份域）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t06.phase0_workspaces (
    workspace_id uuid PRIMARY KEY,
    slug         text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,31}$'),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT phase0_workspaces_slug_key UNIQUE (slug)
);

COMMENT ON TABLE p0_t06.phase0_workspaces IS
    '跨 workspace 注入用最小表（C-07 身份域仍 BLOCKED，本表不代表生产身份模型）。';

CREATE TABLE p0_t06.phase0_principals (
    workspace_id uuid NOT NULL
                 REFERENCES p0_t06.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    principal_id uuid NOT NULL,
    role         text NOT NULL CHECK (role IN ('requester', 'approver', 'member')),
    -- 撤权用 NULL/时间戳表达：不删除行，保留审计引用（对应 T03 的 status=disabled 语义）。
    revoked_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, principal_id)
);

COMMENT ON TABLE p0_t06.phase0_principals IS
    '合成主体替身：revoked_at 非空即视为撤权（EXECUTOR 执行前必须重读，不信审批时快照）。';


-- -----------------------------------------------------------------------------
-- §3 模型响应 artifact（C-04 §4：模型步骤键由已持久输入分配，不从内存计数生成）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t06.phase0_response_artifacts (
    workspace_id         uuid NOT NULL,
    response_artifact_id uuid NOT NULL,
    run_id               uuid NOT NULL,
    -- 稳定模型步骤键：由已持久输入/逻辑步骤分配（C-04 §4）。重复登记必须命中同一行。
    model_step_key       text NOT NULL CHECK (length(model_step_key) BETWEEN 1 AND 200),
    schema_version       integer NOT NULL CHECK (schema_version >= 1),
    -- 只存摘要，不存响应原文（秘密/大对象不得进入本表）。
    response_digest      text NOT NULL CHECK (response_digest ~ '^[0-9a-f]{64}$'),
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, response_artifact_id),
    CONSTRAINT phase0_response_artifacts_step_key UNIQUE (workspace_id, run_id, model_step_key)
);

COMMENT ON TABLE p0_t06.phase0_response_artifacts IS
    'C-04 §4 响应绑定：UNIQUE(workspace,run,model_step_key) 保证重入读回同一 artifact，'
    '不会因为重入而生成第二批工具意图。原文/凭据不落本表，只有 digest。';


-- -----------------------------------------------------------------------------
-- §4 ToolCall 意图（C-04 §4/§7）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t06.phase0_tool_calls (
    workspace_id           uuid NOT NULL,
    tool_call_id           uuid NOT NULL,
    run_id                 uuid NOT NULL,
    response_artifact_id   uuid NOT NULL,
    -- 0-based：同一响应内的第几个调用。C-04 §7 要求与 artifact 组成唯一键。
    call_ordinal           integer NOT NULL CHECK (call_ordinal >= 0),
    name                   text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    tool_version           text NOT NULL CHECK (length(tool_version) BETWEEN 1 AND 64),
    -- 规范化参数哈希与 scope 哈希：审批和执行都必须命中同一个值。
    arguments_hash         text NOT NULL CHECK (arguments_hash ~ '^[0-9a-f]{64}$'),
    scope_hash             text NOT NULL CHECK (scope_hash ~ '^[0-9a-f]{64}$'),
    canonicalization_version text NOT NULL CHECK (canonicalization_version ~ '^v[0-9]+$'),
    -- intent_key = SHA256(canonical(schema_version,run_id,response_artifact_id,
    --   call_ordinal,name,tool_version,arguments_hash,scope_hash))（C-04 §4）。
    intent_key             text NOT NULL CHECK (intent_key ~ '^[0-9a-f]{64}$'),
    status                 text NOT NULL DEFAULT 'prepared'
                           CHECK (status IN ('prepared', 'awaiting_approval', 'executing',
                                             'succeeded', 'failed', 'unknown_outcome',
                                             'cancelled')),
    attempt_count          integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    -- executing 租约：过期且无法证明未发出 → unknown_outcome（TDD02 §5）。
    executing_owner        text CHECK (executing_owner IS NULL OR length(executing_owner) <= 128),
    executing_lease_expires_at timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    row_version            bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, tool_call_id),
    -- C-04 §7 第一条：同一 run 内 intent_key 唯一——换参不得绕过意图唯一键。
    CONSTRAINT phase0_tool_calls_intent_key UNIQUE (workspace_id, run_id, intent_key),
    -- C-04 §7 第二条：同一响应内的 ordinal 唯一，且必须与 intent_key 核对。
    CONSTRAINT phase0_tool_calls_ordinal_key
        UNIQUE (workspace_id, run_id, response_artifact_id, call_ordinal),
    CONSTRAINT phase0_tool_calls_artifact_fk
        FOREIGN KEY (workspace_id, response_artifact_id)
        REFERENCES p0_t06.phase0_response_artifacts (workspace_id, response_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_tool_calls_executing_lease_check CHECK (
        (status = 'executing')
        = (executing_lease_expires_at IS NOT NULL AND executing_owner IS NOT NULL)
    )
);

COMMENT ON TABLE p0_t06.phase0_tool_calls IS
    'C-04 §4 工具意图：intent_key + (artifact,ordinal) 双唯一键，'
    '换参/换 ordinal 都无法绕过；身份列由触发器冻结。';

CREATE INDEX phase0_tool_calls_status_idx
    ON p0_t06.phase0_tool_calls (workspace_id, status, updated_at);

-- 身份列不可变 + 状态机（C-04 §7：禁止换参绕过意图唯一键）
CREATE FUNCTION p0_t06.phase0_tool_call_enforce() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t06
    AS $fn$
    BEGIN
        IF NEW.workspace_id <> OLD.workspace_id
           OR NEW.tool_call_id <> OLD.tool_call_id
           OR NEW.run_id <> OLD.run_id
           OR NEW.response_artifact_id <> OLD.response_artifact_id
           OR NEW.call_ordinal <> OLD.call_ordinal
           OR NEW.name <> OLD.name
           OR NEW.tool_version <> OLD.tool_version
           OR NEW.arguments_hash <> OLD.arguments_hash
           OR NEW.scope_hash <> OLD.scope_hash
           OR NEW.canonicalization_version <> OLD.canonicalization_version
           OR NEW.intent_key <> OLD.intent_key THEN
            RAISE EXCEPTION 'ToolCall 身份列不可变（C-04 §7：换参必须新建意图/审批）'
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.row_version <> OLD.row_version + 1 THEN
            RAISE EXCEPTION 'row_version 必须严格 +1（% → %）', OLD.row_version, NEW.row_version
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.status = OLD.status THEN
            RETURN NEW;
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM (VALUES
                ('prepared',         'awaiting_approval'),
                ('prepared',         'executing'),
                ('prepared',         'cancelled'),
                ('awaiting_approval','executing'),
                ('awaiting_approval','cancelled'),
                ('awaiting_approval','failed'),
                ('executing',        'succeeded'),
                ('executing',        'failed'),
                ('executing',        'unknown_outcome'),
                ('unknown_outcome',  'succeeded'),
                ('unknown_outcome',  'failed'),
                ('failed',           'prepared')
            ) AS allowed (from_status, to_status)
            WHERE allowed.from_status = OLD.status
              AND allowed.to_status = NEW.status
        ) THEN
            RAISE EXCEPTION '非法 ToolCall 状态转移 % → %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;

        -- executing 只能从"已批准"或"已有执行意图"的路径进入，且必须带租约；
        -- 具体审批校验在应用层（DB 看不到审批语义），这里只保证结构上不出现裸 executing。
        IF NEW.status = 'executing' AND NEW.executing_lease_expires_at IS NULL THEN
            RAISE EXCEPTION '进入 executing 必须带 lease（否则无法判定未知窗口）'
                USING ERRCODE = 'check_violation';
        END IF;

        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_tool_calls_enforce
    BEFORE UPDATE ON p0_t06.phase0_tool_calls
    FOR EACH ROW EXECUTE FUNCTION p0_t06.phase0_tool_call_enforce();


-- -----------------------------------------------------------------------------
-- §5 Approval（C-04 §4 / TDD02 §4：who + action + params hash + scope + 有效期 + 幂等键）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t06.phase0_approvals (
    workspace_id         uuid NOT NULL,
    approval_id          uuid NOT NULL,
    run_id               uuid NOT NULL,
    tool_call_id         uuid NOT NULL,
    -- who：请求者与审批者必须分别记录，执行前两者都要重新校验当前资格。
    requester_principal_id uuid NOT NULL,
    approver_principal_id  uuid NOT NULL,
    -- action：工具名 + 版本（版本变化即 APPROVAL_MISMATCH）。
    name                 text NOT NULL,
    tool_version         text NOT NULL,
    -- params hash + scope hash：与 ToolCall 上的值必须逐字相等。
    arguments_hash       text NOT NULL CHECK (arguments_hash ~ '^[0-9a-f]{64}$'),
    scope_hash           text NOT NULL CHECK (scope_hash ~ '^[0-9a-f]{64}$'),
    -- typed resource_refs（脱敏：只存 kind/id，不存正文）。
    resource_refs        jsonb NOT NULL DEFAULT '[]'::jsonb,
    policy_snapshot_id   uuid,
    policy_version       text,
    -- 稳定幂等键：由持久 tool_call_id 派生（C-04 §4），跨重试不变。
    idempotency_key      text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'),
    expires_at           timestamptz NOT NULL,
    status               text NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'approved', 'rejected', 'consumed',
                                           'revoked', 'expired')),
    decided_at           timestamptz,
    consumed_at          timestamptz,
    revoked_at           timestamptz,
    -- 批准后写 resume Outbox 的引用（本 schema 不建 Outbox 表，只记引用；跨域事务归 T07）。
    resume_outbox_id     uuid,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    row_version          bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, approval_id),
    CONSTRAINT phase0_approvals_tool_call_fk
        FOREIGN KEY (workspace_id, tool_call_id)
        REFERENCES p0_t06.phase0_tool_calls (workspace_id, tool_call_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_approvals_requester_fk
        FOREIGN KEY (workspace_id, requester_principal_id)
        REFERENCES p0_t06.phase0_principals (workspace_id, principal_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_approvals_approver_fk
        FOREIGN KEY (workspace_id, approver_principal_id)
        REFERENCES p0_t06.phase0_principals (workspace_id, principal_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_approvals_expiry_check CHECK (expires_at > created_at)
);

COMMENT ON TABLE p0_t06.phase0_approvals IS
    'C-04 §4 审批绑定：who(action 的 name/version)/params_hash/scope_hash/有效期/幂等键 '
    '全部是执行前的强制校验项；消费后不可回退，换参必须新建 approval。';

-- TDD02 §4.1：pending/approved 状态下同一 (tool_call, params, scope, tool_version)
-- 只允许一条有效审批；expired/revoked 之后允许新 approval_id（旧的保留审计）。
CREATE UNIQUE INDEX phase0_approvals_active_key
    ON p0_t06.phase0_approvals (workspace_id, tool_call_id, arguments_hash, scope_hash, tool_version)
    WHERE status IN ('pending', 'approved');

CREATE INDEX phase0_approvals_status_idx
    ON p0_t06.phase0_approvals (workspace_id, status, expires_at);

-- 绑定列不可变 + 状态机 + 消费/撤销/过期不可回退
CREATE FUNCTION p0_t06.phase0_approval_enforce() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t06
    AS $fn$
    BEGIN
        IF NEW.workspace_id <> OLD.workspace_id
           OR NEW.approval_id <> OLD.approval_id
           OR NEW.run_id <> OLD.run_id
           OR NEW.tool_call_id <> OLD.tool_call_id
           OR NEW.requester_principal_id <> OLD.requester_principal_id
           OR NEW.approver_principal_id <> OLD.approver_principal_id
           OR NEW.name <> OLD.name
           OR NEW.tool_version <> OLD.tool_version
           OR NEW.arguments_hash <> OLD.arguments_hash
           OR NEW.scope_hash <> OLD.scope_hash
           OR NEW.resource_refs <> OLD.resource_refs
           OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
           OR NEW.idempotency_key <> OLD.idempotency_key
           OR NEW.expires_at <> OLD.expires_at THEN
            RAISE EXCEPTION 'Approval 绑定列不可变（C-04 §4/§5：改绑必须新建审批）'
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.row_version <> OLD.row_version + 1 THEN
            RAISE EXCEPTION 'row_version 必须严格 +1（% → %）', OLD.row_version, NEW.row_version
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.status = OLD.status THEN
            RETURN NEW;
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM (VALUES
                ('pending',  'approved'),
                ('pending',  'rejected'),
                ('pending',  'revoked'),
                ('pending',  'expired'),
                ('approved', 'consumed'),
                ('approved', 'revoked'),
                ('approved', 'expired')
            ) AS allowed (from_status, to_status)
            WHERE allowed.from_status = OLD.status
              AND allowed.to_status = NEW.status
        ) THEN
            RAISE EXCEPTION '非法 Approval 状态转移 % → %（consumed/rejected/revoked/expired 为终态）',
                OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.status = 'consumed' AND NEW.consumed_at IS NULL THEN
            RAISE EXCEPTION 'consumed 必须记录 consumed_at' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.status = 'revoked' AND NEW.revoked_at IS NULL THEN
            RAISE EXCEPTION 'revoked 必须记录 revoked_at' USING ERRCODE = 'check_violation';
        END IF;

        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_approvals_enforce
    BEFORE UPDATE ON p0_t06.phase0_approvals
    FOR EACH ROW EXECUTE FUNCTION p0_t06.phase0_approval_enforce();


-- -----------------------------------------------------------------------------
-- §6 审批事件（append-only 审计：who 在何时做了什么决定）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t06.phase0_approval_events (
    event_id        bigserial PRIMARY KEY,
    workspace_id    uuid NOT NULL,
    approval_id     uuid NOT NULL,
    actor_principal_id uuid,
    action          text NOT NULL CHECK (action IN ('created', 'approved', 'rejected',
                                                    'revoked', 'expired', 'consumed')),
    reason_code     text CHECK (reason_code IS NULL OR reason_code ~ '^[A-Z][A-Z0-9_]{2,47}$'),
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT phase0_approval_events_approval_fk
        FOREIGN KEY (workspace_id, approval_id)
        REFERENCES p0_t06.phase0_approvals (workspace_id, approval_id) ON DELETE RESTRICT
);

CREATE INDEX phase0_approval_events_idx
    ON p0_t06.phase0_approval_events (workspace_id, approval_id, recorded_at);


-- -----------------------------------------------------------------------------
-- §7 调用账本（append-only；C-04 §6/§8 的 receipt 事实）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t06.phase0_tool_receipts (
    workspace_id          uuid NOT NULL,
    receipt_id            uuid NOT NULL,
    tool_call_id          uuid NOT NULL,
    approval_id           uuid,
    -- 稳定意图键：恢复时按它读回原结果，绝不新生成 key 掩盖重复。
    intent_key            text NOT NULL CHECK (intent_key ~ '^[0-9a-f]{64}$'),
    attempt_no            integer NOT NULL CHECK (attempt_no >= 0),
    arguments_hash        text NOT NULL CHECK (arguments_hash ~ '^[0-9a-f]{64}$'),
    scope_hash            text NOT NULL CHECK (scope_hash ~ '^[0-9a-f]{64}$'),
    tool_version          text NOT NULL,
    -- 结果分类：succeeded / failed / unknown_outcome / replayed / rejected。
    --   replayed  = 同一意图命中已有成功 receipt（没有发生新的下游调用）
    --   rejected  = 执行前校验失败（零下游调用）
    outcome               text NOT NULL CHECK (outcome IN ('succeeded', 'failed',
                                                          'unknown_outcome', 'replayed',
                                                          'rejected')),
    -- 关键：是否真的向外部发出过请求。unknown_outcome 也据此分叉（TDD02 §5）。
    downstream_sent       boolean NOT NULL,
    external_idempotency_key text,
    result_digest         text CHECK (result_digest IS NULL OR result_digest ~ '^[0-9a-f]{64}$'),
    result_summary        jsonb NOT NULL DEFAULT '{}'::jsonb,
    duration_ms           integer NOT NULL CHECK (duration_ms >= 0),
    error_code            text CHECK (error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{2,47}$'),
    recorded_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, receipt_id),
    CONSTRAINT phase0_tool_receipts_attempt_key
        UNIQUE (workspace_id, tool_call_id, attempt_no),
    CONSTRAINT phase0_tool_receipts_tool_call_fk
        FOREIGN KEY (workspace_id, tool_call_id)
        REFERENCES p0_t06.phase0_tool_calls (workspace_id, tool_call_id) ON DELETE RESTRICT,
    -- 未发出请求就不该有外部幂等键；发出过则必须有（否则恢复无法查单）。
    CONSTRAINT phase0_tool_receipts_key_check CHECK (
        (downstream_sent AND external_idempotency_key IS NOT NULL)
        OR ((NOT downstream_sent) AND external_idempotency_key IS NULL)
    ),
    -- 只有 succeeded/replayed 才能带结果摘要里的"已应用"标记由应用层保证；
    -- 这里强制 unknown_outcome 必须 downstream_sent（未发出就不是未知写）。
    CONSTRAINT phase0_tool_receipts_unknown_check CHECK (
        outcome <> 'unknown_outcome' OR downstream_sent
    )
);

COMMENT ON TABLE p0_t06.phase0_tool_receipts IS
    'C-04 §6/§8 append-only 调用账本：每次调用一条，含 intent_key/参数哈希/结果摘要/耗时。'
    'UNIQUE(workspace,tool_call_id,attempt_no) 让重复写入显式失败而不是静默覆盖。';

CREATE INDEX phase0_tool_receipts_intent_idx
    ON p0_t06.phase0_tool_receipts (workspace_id, intent_key, recorded_at);

-- append-only 强制（触发器，与角色无关；superuser 也不能绕过）
CREATE FUNCTION p0_t06.phase0_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t06
    AS $fn$
    BEGIN
        RAISE EXCEPTION '审计/账本表只允许追加写，拒绝 %', TG_OP
            USING ERRCODE = 'check_violation';
    END
    $fn$;

CREATE TRIGGER phase0_tool_receipts_append_only
    BEFORE UPDATE OR DELETE ON p0_t06.phase0_tool_receipts
    FOR EACH ROW EXECUTE FUNCTION p0_t06.phase0_reject_mutation();

CREATE TRIGGER phase0_approval_events_append_only
    BEFORE UPDATE OR DELETE ON p0_t06.phase0_approval_events
    FOR EACH ROW EXECUTE FUNCTION p0_t06.phase0_reject_mutation();


-- -----------------------------------------------------------------------------
-- §8 留位（不在本迁移实现）
-- -----------------------------------------------------------------------------
-- * 预算预留/结算（TDD02 §7，归 P0-T20）：本迁移不建 reservation 表，receipt 只留
--   duration_ms 供 T20 计账。
-- * Outbox / resume 派发（C-08，归 P0-T07）：approvals.resume_outbox_id 只存引用，
--   跨域事务由应用用例编排（见 07_Contract_Closure.md C-01）。
-- * 生产身份域（C-07 BLOCKED）：phase0_principals 是撤权注入用的替身，不是身份模型。
-- * Run/Task 事实（p0_t05 / p0_t07 / T23）：本 schema 只存 run_id 引用，不建 Run 表。
