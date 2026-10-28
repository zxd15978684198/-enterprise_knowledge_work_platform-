-- =============================================================================
-- PyKnora Phase 0 · P0-T03 · 迁移 001：合成身份 / Scope / 最小 PG 契约
-- =============================================================================
-- 归属：P0-T03（w2-t03-scope 独占）。后续任务只能增量迁移，不得复制本文件基表。
-- 依据：docs/poc/03_Phase0_Task_Backlog.md（P0-T03 任务卡）、
--       docs/poc/07_Contract_Closure.md C-06（无 Run Job 身份传播，DEFINED）、
--       C-07（身份域/来源审阅，BLOCKED）、C-10（asset_bindings，NEEDS_POC）、
--       docs/tdd/01_System_Architecture.md §4/§5、docs/tdd/07_Data_API_Security.md §3/§4。
--
-- 隔离：本迁移只写 schema p0_t03，不触碰 public 或 T02/T04 的资源槽。
-- 依赖：全部对象以 p0_t03. 前缀显式限定，不依赖会话 search_path（避免污染 public）。
-- 事务：本文件不含 BEGIN/COMMIT，由调用方（scope.apply_migration）用单事务包裹。
-- 秘密：本文件零凭据（p0_app 角色无密码；密码只在运行期由测试随机生成并 ALTER ROLE）。
-- BLOCKED 说明（C-07）：principal 的 membership_subject_ref 只是**合成 opaque 引用**，
--   不建全局 User / subject→membership 映射 / 登录认证 / 来源审阅（ReviewDecision）表。
--   生产身份域、P0 review endpoint/角色、approval_id ↔ review_decision_id 映射需
--   产品 + 安全 + 架构三项决策，未决前不得据此宣称生产可用。
-- 留位说明（C-10）：asset_bindings 的唯一归属 / 延迟 FK / 插入环不在本迁移实现
--   （见文末 §7 注释），只留 schema 注释与增量迁移契约，避免与 T11/T12 平行覆盖。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- §1 schema 与角色（deny 默认：RLS 只在显式 policy 命中时放行）
-- -----------------------------------------------------------------------------
CREATE SCHEMA p0_t03;

COMMENT ON SCHEMA p0_t03 IS
    'P0-T03 实验槽：合成 Principal/Scope/execution binding 最小契约。'
    '隔离于 public；asset_bindings(C-10) 留位见迁移 001 §7；'
    '生产身份域与来源审阅为 BLOCKED(C-07)。';

-- 实验用应用角色：非超级用户、无 BYPASSRLS、非 owner，用于证明 RLS 真实生效。
-- 只建角色，不设密码（运行期由测试随机生成，绝不入 Git）；重复执行时强制复位属性。
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'p0_app') THEN
        CREATE ROLE p0_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
    ELSE
        ALTER ROLE p0_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
    END IF;
END
$do$;

GRANT USAGE ON SCHEMA p0_t03 TO p0_app;


-- -----------------------------------------------------------------------------
-- §2 迁移台账（无 policy 且不授权 p0_app → 应用角色连读都被拒）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t03.phase0_schema_migrations (
    migration_id text PRIMARY KEY,
    filename     text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

COMMENT ON TABLE p0_t03.phase0_schema_migrations IS
    '迁移台账：记录文件名与内容 sha256，供“空库重建”与漂移检测；'
    '不授权 p0_app，也不建 policy → 应用角色无权读取。';


-- -----------------------------------------------------------------------------
-- §3 隔离根与合成主体
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t03.phase0_workspaces (
    workspace_id    uuid PRIMARY KEY,
    name            text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'tombstoned')),
    policy_revision bigint NOT NULL DEFAULT 1 CHECK (policy_revision >= 1),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    row_version     bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1)
);

CREATE TABLE p0_t03.phase0_principals (
    workspace_id            uuid NOT NULL
                            REFERENCES p0_t03.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    principal_id            uuid NOT NULL,
    -- 身份域受控 opaque 引用（合成值，不含姓名/邮箱等 PII）；生产映射为 BLOCKED(C-07)。
    membership_subject_ref  text NOT NULL CHECK (length(membership_subject_ref) BETWEEN 1 AND 128),
    kind                    text NOT NULL CHECK (kind IN ('human', 'service')),
    role                    text NOT NULL CHECK (role IN ('owner', 'member')),
    -- 撤销 = status 置 disabled（不删除行，保留审计引用）。
    status                  text NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'disabled')),
    membership_revision     bigint NOT NULL DEFAULT 1 CHECK (membership_revision >= 1),
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    row_version             bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, principal_id),
    CONSTRAINT phase0_principals_subject_ref_key
        UNIQUE (workspace_id, membership_subject_ref)
);

COMMENT ON TABLE p0_t03.phase0_principals IS
    '合成 Principal（P0 只做空间内主体）。成员撤销=status=disabled；'
    'role 只作 P0 Owner/Member 展示，真实权限每次按当前行重算，不由 policy_snapshot 授予。';

CREATE TABLE p0_t03.phase0_policy_snapshots (
    workspace_id       uuid NOT NULL
                       REFERENCES p0_t03.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    policy_snapshot_id uuid NOT NULL,
    revision           bigint NOT NULL CHECK (revision >= 1),
    rules_hash         text NOT NULL CHECK (rules_hash ~ '^[0-9a-f]{64}$'),
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, policy_snapshot_id),
    CONSTRAINT phase0_policy_snapshots_revision_key UNIQUE (workspace_id, revision)
);

COMMENT ON TABLE p0_t03.phase0_policy_snapshots IS
    '策略快照仅作审计输入（TDD01 §4）；不包含可授予历史权限的规则本体。';


-- -----------------------------------------------------------------------------
-- §4 最小业务事实表（participating in composite FK；workspace_id 参与唯一键）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t03.phase0_sources (
    workspace_id   uuid NOT NULL
                   REFERENCES p0_t03.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    source_id      uuid NOT NULL,
    canonical_url  text NOT NULL CHECK (length(canonical_url) BETWEEN 1 AND 2048),
    canonical_hash text NOT NULL CHECK (canonical_hash ~ '^[0-9a-f]{64}$'),
    source_type    text NOT NULL CHECK (source_type IN ('url', 'upload')),
    status         text NOT NULL DEFAULT 'captured'
                   CHECK (status IN ('captured', 'approved', 'rejected', 'revoked')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    row_version    bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, source_id),
    CONSTRAINT phase0_sources_canonical_hash_key UNIQUE (workspace_id, canonical_hash)
);

COMMENT ON TABLE p0_t03.phase0_sources IS
    '来源最小事实（C-07 BLOCKED：status 的 approved/rejected 只表示合成状态位，'
    '不代表已实现来源审阅端点/ReviewDecision 记录）。';

CREATE TABLE p0_t03.phase0_knowledge_bases (
    workspace_id       uuid NOT NULL
                       REFERENCES p0_t03.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    kb_id              uuid NOT NULL,
    name               text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    owner_principal_id uuid NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    row_version        bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, kb_id),
    CONSTRAINT phase0_kb_owner_fk FOREIGN KEY (workspace_id, owner_principal_id)
        REFERENCES p0_t03.phase0_principals (workspace_id, principal_id) ON DELETE RESTRICT
);


-- -----------------------------------------------------------------------------
-- §5 不可变 execution binding（C-06：无 Run Job 的身份传播来源）
-- -----------------------------------------------------------------------------
-- 所有 FK 均含 workspace_id；Binding 一旦创建不允许改主体/操作/资源范围，
-- 只能随资源/成员状态一起“失效”，新增重试必须新建 Binding（C-09）。
CREATE TABLE p0_t03.phase0_execution_bindings (
    workspace_id         uuid NOT NULL
                         REFERENCES p0_t03.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    execution_binding_id uuid NOT NULL,
    principal_id         uuid NOT NULL,
    policy_snapshot_id   uuid NOT NULL,
    operation            text NOT NULL CHECK (operation ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    resource_scope_hash  text NOT NULL CHECK (resource_scope_hash ~ '^[0-9a-f]{64}$'),
    binding_hash         text NOT NULL CHECK (binding_hash ~ '^[0-9a-f]{64}$'),
    parent_run_id        uuid,
    parent_task_id       uuid,
    task_graph_revision  integer,
    created_at           timestamptz NOT NULL DEFAULT now(),
    expires_at           timestamptz,
    PRIMARY KEY (workspace_id, execution_binding_id),
    CONSTRAINT phase0_binding_principal_fk FOREIGN KEY (workspace_id, principal_id)
        REFERENCES p0_t03.phase0_principals (workspace_id, principal_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_binding_policy_fk FOREIGN KEY (workspace_id, policy_snapshot_id)
        REFERENCES p0_t03.phase0_policy_snapshots (workspace_id, policy_snapshot_id)
        ON DELETE RESTRICT,
    -- C-06 §7：parent_task 有值则 parent_run/revision 必有值。
    CONSTRAINT phase0_binding_parent_check CHECK (
        (parent_task_id IS NULL AND task_graph_revision IS NULL)
        OR (parent_run_id IS NOT NULL
            AND parent_task_id IS NOT NULL
            AND task_graph_revision IS NOT NULL
            AND task_graph_revision >= 1)
    ),
    CONSTRAINT phase0_binding_expiry_check CHECK (expires_at IS NULL OR expires_at > created_at)
);

COMMENT ON TABLE p0_t03.phase0_execution_bindings IS
    'C-06 不可变 execution binding：主体/操作/资源范围由 DB 事实决定；'
    'Job payload / 队列消息 / checkpoint 不得构造或替换本表内容。';

-- typed resource_refs：按 kind 严格 schema（每种 kind 恰有对应列非空，其余为空）。
-- 本迁移只覆盖 source / kb（其 owner 表已存在）；source_version / document /
-- document_version 随 T11/T12 的增量迁移新增列与复合 FK，不复制本表。
CREATE TABLE p0_t03.phase0_execution_binding_resources (
    workspace_id         uuid NOT NULL,
    execution_binding_id uuid NOT NULL,
    resource_ordinal     smallint NOT NULL CHECK (resource_ordinal >= 0),
    resource_kind        text NOT NULL CHECK (resource_kind IN ('source', 'kb')),
    source_id            uuid,
    kb_id                uuid,
    PRIMARY KEY (workspace_id, execution_binding_id, resource_ordinal),
    CONSTRAINT phase0_binding_resources_binding_fk
        FOREIGN KEY (workspace_id, execution_binding_id)
        REFERENCES p0_t03.phase0_execution_bindings (workspace_id, execution_binding_id)
        ON DELETE CASCADE,
    CONSTRAINT phase0_binding_resources_source_fk
        FOREIGN KEY (workspace_id, source_id)
        REFERENCES p0_t03.phase0_sources (workspace_id, source_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_binding_resources_kb_fk
        FOREIGN KEY (workspace_id, kb_id)
        REFERENCES p0_t03.phase0_knowledge_bases (workspace_id, kb_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_binding_resources_target_check CHECK (
        (resource_kind = 'source' AND source_id IS NOT NULL AND kb_id IS NULL)
        OR (resource_kind = 'kb' AND kb_id IS NOT NULL AND source_id IS NULL)
    )
);

CREATE UNIQUE INDEX phase0_binding_resources_source_key
    ON p0_t03.phase0_execution_binding_resources (workspace_id, execution_binding_id, source_id)
    WHERE source_id IS NOT NULL;

CREATE UNIQUE INDEX phase0_binding_resources_kb_key
    ON p0_t03.phase0_execution_binding_resources (workspace_id, execution_binding_id, kb_id)
    WHERE kb_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- §6 Job：execution_binding_id 必填（C-06），run_id 可空但 owner 至少一个
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t03.phase0_jobs (
    workspace_id         uuid NOT NULL
                         REFERENCES p0_t03.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    job_id               uuid NOT NULL,
    execution_binding_id uuid NOT NULL,
    run_id               uuid,
    source_id            uuid,
    kind                 text NOT NULL CHECK (kind IN ('fetch', 'ingest')),
    dedup_key            text NOT NULL CHECK (length(dedup_key) BETWEEN 1 AND 256),
    payload_schema_version integer NOT NULL DEFAULT 1 CHECK (payload_schema_version >= 1),
    status               text NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'running', 'succeeded',
                                           'failed', 'cancelled', 'superseded')),
    current_attempt_id   uuid,
    lease_epoch          bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, job_id),
    CONSTRAINT phase0_jobs_dedup_key UNIQUE (workspace_id, kind, dedup_key),
    CONSTRAINT phase0_jobs_binding_fk FOREIGN KEY (workspace_id, execution_binding_id)
        REFERENCES p0_t03.phase0_execution_bindings (workspace_id, execution_binding_id)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_jobs_source_fk FOREIGN KEY (workspace_id, source_id)
        REFERENCES p0_t03.phase0_sources (workspace_id, source_id) ON DELETE RESTRICT,
    -- C-06 §4：Job 所有者仍至少 source/document/run 之一（本迁移只有 source/run）。
    CONSTRAINT phase0_jobs_owner_check CHECK (source_id IS NOT NULL OR run_id IS NOT NULL)
);

COMMENT ON TABLE p0_t03.phase0_jobs IS
    '最小 Job 事实：execution_binding_id 必填；run_id 可空（fetch/ingest 类作业）。'
    'Worker 只凭 job_id + DB binding 重新授权，不读消息里的 role/workspace 作为授权依据。';

CREATE INDEX phase0_jobs_status_idx ON p0_t03.phase0_jobs (workspace_id, status, created_at);


-- -----------------------------------------------------------------------------
-- §7 RLS：ENABLE + FORCE，只对 p0_app 建显式 policy；无 policy 的表明拒绝
-- -----------------------------------------------------------------------------
CREATE FUNCTION p0_t03.phase0_current_workspace_id() RETURNS uuid
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, p0_t03
    AS $fn$
        SELECT NULLIF(current_setting('app.workspace_id', true), '')::uuid
    $fn$;

COMMENT ON FUNCTION p0_t03.phase0_current_workspace_id() IS
    '读事务内 SET LOCAL app.workspace_id；未设置或已清空（''）时返回 NULL → RLS 拒绝。';

-- 作用域内的主体是否仍被允许（撤销即时生效的 DB 层兜底）。
-- principal GUC 为空 = 受信的 workspace 级扫描（如 authorize_job 读 binding），放行；
-- principal GUC 有值 = 必须存在且 status='active'，否则 USING/WITH CHECK 全部失败。
-- SECURITY DEFINER + 超级用户 owner：内部读 principals 时绕过 RLS，避免策略递归。
CREATE FUNCTION p0_t03.phase0_current_principal_permitted() RETURNS boolean
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = pg_catalog, p0_t03
    AS $fn$
        SELECT COALESCE(
            (
                SELECT p.status = 'active'
                FROM p0_t03.phase0_principals AS p
                WHERE p.workspace_id = p0_t03.phase0_current_workspace_id()
                  AND p.principal_id = NULLIF(current_setting('app.principal_id', true), '')::uuid
            ),
            NULLIF(current_setting('app.principal_id', true), '') IS NULL
        )
    $fn$;

COMMENT ON FUNCTION p0_t03.phase0_current_principal_permitted() IS
    '撤销兜底：principal GUC 有值时必须 active；为空时视为受信 workspace 级扫描。';

REVOKE ALL ON FUNCTION p0_t03.phase0_current_workspace_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION p0_t03.phase0_current_workspace_id() TO p0_app;
REVOKE ALL ON FUNCTION p0_t03.phase0_current_principal_permitted() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION p0_t03.phase0_current_principal_permitted() TO p0_app;

ALTER TABLE p0_t03.phase0_workspaces                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_workspaces                   FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_principals                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_principals                   FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_policy_snapshots             ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_policy_snapshots             FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_sources                      ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_sources                      FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_knowledge_bases              ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_knowledge_bases              FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_execution_bindings           ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_execution_bindings           FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_execution_binding_resources  ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_execution_binding_resources  FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_jobs                         ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_jobs                         FORCE ROW LEVEL SECURITY;

CREATE POLICY phase0_workspaces_scope ON p0_t03.phase0_workspaces
    FOR ALL TO p0_app
    USING (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    )
    WITH CHECK (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    );

CREATE POLICY phase0_principals_scope ON p0_t03.phase0_principals
    FOR ALL TO p0_app
    USING (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    )
    WITH CHECK (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    );

CREATE POLICY phase0_policy_snapshots_scope ON p0_t03.phase0_policy_snapshots
    FOR ALL TO p0_app
    USING (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    )
    WITH CHECK (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    );

CREATE POLICY phase0_sources_scope ON p0_t03.phase0_sources
    FOR ALL TO p0_app
    USING (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    )
    WITH CHECK (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    );

CREATE POLICY phase0_knowledge_bases_scope ON p0_t03.phase0_knowledge_bases
    FOR ALL TO p0_app
    USING (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    )
    WITH CHECK (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    );

CREATE POLICY phase0_execution_bindings_scope ON p0_t03.phase0_execution_bindings
    FOR ALL TO p0_app
    USING (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    )
    WITH CHECK (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    );

CREATE POLICY phase0_execution_binding_resources_scope
    ON p0_t03.phase0_execution_binding_resources
    FOR ALL TO p0_app
    USING (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    )
    WITH CHECK (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    );

CREATE POLICY phase0_jobs_scope ON p0_t03.phase0_jobs
    FOR ALL TO p0_app
    USING (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    )
    WITH CHECK (
        workspace_id = p0_t03.phase0_current_workspace_id()
        AND p0_t03.phase0_current_principal_permitted()
    );

-- deny 探针表：授予 p0_app 读写权限，但**故意不建 policy**。
-- 断言：SELECT 命中 0 行、INSERT/UPDATE 被 RLS 拒绝 → 证明“无 policy 即拒绝”，
-- 排除“RLS 没开只是没权限”的假象。
CREATE TABLE p0_t03.phase0_deny_probe (
    workspace_id uuid NOT NULL,
    probe_id     uuid NOT NULL,
    note         text NOT NULL,
    PRIMARY KEY (workspace_id, probe_id)
);
ALTER TABLE p0_t03.phase0_deny_probe ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t03.phase0_deny_probe FORCE ROW LEVEL SECURITY;


-- -----------------------------------------------------------------------------
-- §8 应用角色权限（只给 DML；DDL/迁移台账/deny 探针按最小必要给）
-- -----------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON
    p0_t03.phase0_workspaces,
    p0_t03.phase0_principals,
    p0_t03.phase0_policy_snapshots,
    p0_t03.phase0_sources,
    p0_t03.phase0_knowledge_bases,
    p0_t03.phase0_execution_bindings,
    p0_t03.phase0_execution_binding_resources,
    p0_t03.phase0_jobs
TO p0_app;

-- deny 探针：显式给 DML，才能把“被拒”归因于 RLS 而不是缺权限。
GRANT SELECT, INSERT, UPDATE, DELETE ON p0_t03.phase0_deny_probe TO p0_app;


-- -----------------------------------------------------------------------------
-- §9 无 Run Job 的受控路由查询（实验专用、不含内容）
-- -----------------------------------------------------------------------------
-- Worker 只知道 job_id，必须先解析出 workspace/binding 才能 SET LOCAL。
-- 这里用 SECURITY DEFINER 的窄函数（只返回 3 个标识符，不返回业务内容）代替
-- 生产上应分离的 worker 角色 + 独立 policy：
--   * 生产必须用受限作业角色并单独评审（T29），不能沿用超级用户 owner；
--   * C-07 身份域未决前，本函数不构成生产授权路径。
CREATE FUNCTION p0_t03.phase0_lookup_job_route(p_job_id uuid)
    RETURNS TABLE (workspace_id uuid, execution_binding_id uuid, run_id uuid)
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = pg_catalog, p0_t03
    AS $fn$
        SELECT j.workspace_id, j.execution_binding_id, j.run_id
        FROM p0_t03.phase0_jobs AS j
        WHERE j.job_id = p_job_id
    $fn$;

COMMENT ON FUNCTION p0_t03.phase0_lookup_job_route(uuid) IS
    '实验窄路由：job_id → (workspace, execution_binding, run_id)。'
    'SECURITY DEFINER + 只返回标识符；生产需改受限 worker 角色（T29 评审）。';

REVOKE ALL ON FUNCTION p0_t03.phase0_lookup_job_route(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION p0_t03.phase0_lookup_job_route(uuid) TO p0_app;


-- -----------------------------------------------------------------------------
-- §10 留位（不在本迁移实现）
-- -----------------------------------------------------------------------------
-- C-10（NEEDS_POC）：asset_bindings 作为资产唯一归属来源、延迟复合 FK、
--   assets ↔ 版本 snapshot/manifest 的插入环，需 T11/T12 真实 PG 证据后再定 DDL。
--   本迁移**不建** phase0_assets / phase0_asset_bindings，避免与 T11/T12 平行覆盖；
--   后续增量迁移在 p0_t03 内新增独立表，不得复制 phase0_sources 等基表。
-- C-07（BLOCKED）：生产身份域（全局 User、subject→membership、登录认证）与
--   P0 来源审阅端点/角色、PRD approval_id ↔ 实验 review_decision_id 映射未决；
--   本迁移只有合成 principal 与 source.status 状态位，不建 review_decisions。
-- C-09（DEFINED）：jobs.retry_of_job_id / retry_sequence / execution_generation 由 T08 增量补列。
-- C-08（DEFINED）：outbox 五态表由 T07 增量新建。
