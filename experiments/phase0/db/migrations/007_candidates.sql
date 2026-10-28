-- =============================================================================
-- 007_candidates.sql —— P0-T27：来源质量、去重与人工 Review 契约
-- =============================================================================
-- 归属：P0-T27（w1-t14-browserskill）。迁移编号 007 由 Lead 在
--       docs/poc/evidence/wave-w7-record.md §1.1 判给本任务（001–006、008 已占用）。
--
-- 依据：
--   * docs/poc/03_Phase0_Task_Backlog.md P0-T27 卡（L456–471）
--   * docs/tdd/06_Knowledge_Ingestion.md §2（来源/版本/候选）、§7（质量与证据）
--   * docs/tdd/07_Data_API_Security.md §3（candidates / review_decisions 表契约）、
--     §5（审核入库与工具批准独立）
--   * docs/poc/05_Phase0_Acceptance.md PoC-08
--   * ADR-0002（事务与队列边界，仍为 Proposed，不作为已决结论）
--
-- 与 001 §10「后续增量迁移在 p0_t03 内新增独立表」的偏离（显式记录，不静默）：
--   本迁移改用独立 schema ``p0_t27``，理由与 007 之前的 004/008 相同：W7 波次三个
--   子任务各自持有 schema 槽位（p0_t25/p0_t27/p0_t30），而 001 的测试 teardown 会
--   ``DROP SCHEMA p0_t03 CASCADE``；共享 schema 会让并行波次互相抹掉。生产落地时应
--   换回共享 schema 前缀，并把 RLS policy 与 ``p0_app`` 授权补回（本迁移不建角色、
--   不开 RLS：行级隔离已由 P0-T03 在同一实验库证明，抢占全局角色名 p0_app 会破坏
--   并行波次）。
--
-- 本迁移的核心不变量（写入 DDL，而不是只写在代码里）：
--   I1 追加史：sources/source_events/candidates/candidate_facts/candidate_evidence/
--      candidate_events/review_decisions/promotions 一律**只允许 INSERT**；
--      UPDATE / DELETE 由 BEFORE 触发器拒绝（SQLSTATE 23514）。「改状态」= 追加一行
--      事件，当前状态由最新事件推导。
--   I2 去重按 workspace 隔离、按 source 保留：精确重复（同 source 同 URL 同内容）只
--      留 1 个候选；同指纹跨 source 保留各自候选，只并到同一 duplicate_cluster；
--      不同 workspace 的 UNIQUE 键互不相交（跨空间不泄露存在性）。
--   I3 人工 Review：review_decisions 只接受 kind='human' 的主体（CHECK 硬约束），
--      身份/时间/理由三要素 NOT NULL；同一 (candidate, reviewer, decision, reason)
--      唯一 = 重复审批幂等；同一 candidate 至多一条 approved（部分唯一索引）。
--   I4 发布入口：promotions 必须引用**已批准且指向同一 candidate/kb** 的
--      review_decision，并由 ``phase0_promotion_guard()`` 在插入时再核一次
--      「来源未被撤销/过期」+「每个关键事实至少一条未过期已核验证据」。
--   I5 ReviewDecision 不是工具 Approval：本 schema 不建 tool_call/approval 列，
--      promotions 只接 review_decision_id；跨层混用在 Python 层以 code 拒绝。
--
-- 本文件不含 BEGIN/COMMIT（由调用方在同一事务内应用并写台账）；
-- ``CREATE SCHEMA`` 刻意不带 ``IF NOT EXISTS``，重复 apply 会显式失败。
-- 本文件不含任何凭据。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- §1 schema 与迁移台账
-- -----------------------------------------------------------------------------
CREATE SCHEMA p0_t27;

CREATE TABLE p0_t27.phase0_schema_migrations (
    migration_id text PRIMARY KEY CHECK (length(migration_id) BETWEEN 1 AND 64),
    filename     text NOT NULL CHECK (length(filename) BETWEEN 1 AND 200),
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

COMMENT ON TABLE p0_t27.phase0_schema_migrations IS
    'P0-T27 迁移台账：每条迁移一行，sha256 是文件字节摘要（可复验）。';


-- -----------------------------------------------------------------------------
-- §2 隔离根与合成主体（只建本任务验收所需的最小列）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t27.phase0_workspaces (
    workspace_id uuid PRIMARY KEY,
    name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE p0_t27.phase0_workspaces IS
    '最小空间根：只为「跨空间同 URL 不去重」提供可验证的两空间 fixture。';

-- kind 是审核资格的唯一依据：只有 'human' 可以产出 ReviewDecision；
-- 'service'/'machine' 是后台 worker 的合法主体，但不得代替人工审阅。
CREATE TABLE p0_t27.phase0_principals (
    workspace_id uuid NOT NULL
                 REFERENCES p0_t27.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    principal_id uuid NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('human', 'service')),
    status       text NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'disabled')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, principal_id)
);

COMMENT ON TABLE p0_t27.phase0_principals IS
    '合成主体：kind=human 才可审阅；service 主体只能写候选/证据/事件。'
    '撤销=status=disabled（不删行），生产身份域仍为 T03 C-07 BLOCKED。';


-- -----------------------------------------------------------------------------
-- §3 来源（不可变事实）+ 来源事件（只追加）
-- -----------------------------------------------------------------------------
-- 来源身份 = workspace + canonical_url（TDD06 §2）。canonical_url 只由本层规范化，
-- 不信任采集方传入的 canonical 标记：保留语义 query，不盲删参数。
CREATE TABLE p0_t27.phase0_sources (
    workspace_id   uuid NOT NULL
                   REFERENCES p0_t27.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    source_id      uuid NOT NULL,
    canonical_url  text NOT NULL CHECK (length(canonical_url) BETWEEN 1 AND 2048),
    url_hash       text NOT NULL CHECK (url_hash ~ '^[0-9a-f]{64}$'),
    domain         text NOT NULL CHECK (length(domain) BETWEEN 1 AND 253),
    source_type    text NOT NULL CHECK (source_type IN ('url', 'upload')),
    -- 许可：allowed=可处理；unknown=必须转人工；forbidden=硬拒绝（分数不可覆盖）。
    license        text NOT NULL CHECK (license IN ('allowed', 'unknown', 'forbidden')),
    is_primary     boolean NOT NULL DEFAULT false,
    -- 转载/聚合站：可识别，但不得因此提高质量分（防止转载冒充一手证据）。
    is_reprint     boolean NOT NULL DEFAULT false,
    channel        text NOT NULL CHECK (channel IN ('http', 'browser', 'upload')),
    -- T16 网络策略的落地口径：allow=受控出口放行；deny=策略拒绝；not_checked=未知。
    fetch_policy   text NOT NULL CHECK (fetch_policy IN ('allow', 'deny', 'not_checked')),
    content_hash   text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    published_at   timestamptz,
    first_seen_at  timestamptz NOT NULL,
    expires_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, source_id),
    CONSTRAINT phase0_sources_url_key UNIQUE (workspace_id, url_hash),
    CONSTRAINT phase0_sources_expiry_check
        CHECK (expires_at IS NULL OR expires_at > first_seen_at)
);

COMMENT ON TABLE p0_t27.phase0_sources IS
    '来源不可变事实：workspace+canonical_url 唯一（同空间同 URL 只注册一次）。'
    '生命周期状态不在本表，由 phase0_source_events 的最新事件推导。';

CREATE TABLE p0_t27.phase0_source_events (
    workspace_id uuid NOT NULL,
    source_id    uuid NOT NULL,
    event_seq    bigint GENERATED ALWAYS AS IDENTITY,
    event_id     uuid NOT NULL,
    event_type   text NOT NULL CHECK (event_type IN (
                     'captured',
                     'approved',
                     'rejected',
                     'revoked',
                     'expired',
                     'refreshed'
                 )),
    actor_kind   text NOT NULL CHECK (actor_kind IN ('human', 'service', 'system')),
    actor_ref    text NOT NULL CHECK (length(actor_ref) BETWEEN 1 AND 128),
    reason_code  text NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 64),
    detail       text CHECK (detail IS NULL OR length(detail) <= 512),
    recorded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, event_id),
    CONSTRAINT phase0_source_events_seq_key UNIQUE (event_seq),
    CONSTRAINT phase0_source_events_source_fk
        FOREIGN KEY (workspace_id, source_id)
        REFERENCES p0_t27.phase0_sources (workspace_id, source_id) ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t27.phase0_source_events IS
    '来源审计（只追加）：captured/approved/rejected/revoked/expired/refreshed。'
    'revoked / expired 是终态，追加后该来源下的候选一律禁止 promote。';


-- -----------------------------------------------------------------------------
-- §4 精确重复簇（跨 source 保留候选，只并簇）
-- -----------------------------------------------------------------------------
-- 设计要点：同内容跨 source 的两个候选**都保留**（TDD06 §2「重复正文保留各 source」），
-- 但共享一个簇，供近重提示/审计使用。UNIQUE 含 workspace → 跨空间不共享簇 id，
-- 不能靠簇 id 泄露另一空间是否抓过同一内容。
CREATE TABLE p0_t27.phase0_duplicate_clusters (
    workspace_id uuid NOT NULL
                 REFERENCES p0_t27.phase0_workspaces (workspace_id) ON DELETE RESTRICT,
    cluster_id   uuid NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, cluster_id),
    CONSTRAINT phase0_duplicate_clusters_content_key UNIQUE (workspace_id, content_hash)
);

COMMENT ON TABLE p0_t27.phase0_duplicate_clusters IS
    '精确重复簇：按 (workspace, content_hash) 唯一；成员数由 candidates 计数得出，'
    '不维护可变的 member_count（本 schema 拒绝 UPDATE）。';


-- -----------------------------------------------------------------------------
-- §5 候选（不可变捕获事实）
-- -----------------------------------------------------------------------------
-- 一个候选 = 一次「受控采集 + 抽取 profile」的结果。状态不落在本表：
-- phase0_candidate_events 的最新事件才是当前状态，本表 UPDATE 会被触发器拒绝。
CREATE TABLE p0_t27.phase0_candidates (
    workspace_id           uuid NOT NULL
                           REFERENCES p0_t27.phase0_workspaces (workspace_id)
                           ON DELETE RESTRICT,
    candidate_id           uuid NOT NULL,
    source_id              uuid NOT NULL,
    source_version_id      uuid NOT NULL,
    run_id                 uuid,
    canonical_url          text NOT NULL CHECK (length(canonical_url) BETWEEN 1 AND 2048),
    url_hash               text NOT NULL CHECK (url_hash ~ '^[0-9a-f]{64}$'),
    content_hash           text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- 抽取配置不可变；同来源同内容换 profile 视为新候选（TDD06 §2 版本不可变）。
    extraction_profile_hash text NOT NULL
                           CHECK (extraction_profile_hash ~ '^[0-9a-f]{64}$'),
    -- dedup_key = sha256(url_hash || ':' || content_hash)：同 URL 同内容重复投递幂等。
    dedup_key              text NOT NULL CHECK (dedup_key ~ '^[0-9a-f]{64}$'),
    cluster_id             uuid NOT NULL,
    duplicate_of_candidate_id uuid,
    is_duplicate           boolean NOT NULL DEFAULT false,
    -- 近似重复提示：64 位 simhash + 与链首的汉明距离（仅提示，不自动合并）。
    simhash64              text NOT NULL CHECK (simhash64 ~ '^[0-9a-f]{16}$'),
    near_duplicate_of      uuid,
    near_duplicate_distance smallint
                           CHECK (near_duplicate_distance IS NULL
                                  OR near_duplicate_distance BETWEEN 0 AND 64),
    quality_score          smallint NOT NULL CHECK (quality_score BETWEEN 0 AND 100),
    -- 评分构成（权威性/一手性/新鲜度/许可/证据密度），整数分项，可复算。
    quality_components     jsonb NOT NULL,
    -- 评分算法的版本；算法改动必须升版本，旧候选的分数不得被新算法静默重算。
    quality_algorithm      text NOT NULL CHECK (length(quality_algorithm) BETWEEN 1 AND 32),
    evidence_set_hash      text NOT NULL CHECK (evidence_set_hash ~ '^[0-9a-f]{64}$'),
    critical_fact_count    smallint NOT NULL DEFAULT 0 CHECK (critical_fact_count >= 0),
    origin                 text NOT NULL CHECK (origin IN (
                               'ingest',
                               'research',
                               'replan_needs_review'
                           )),
    fetch_policy           text NOT NULL CHECK (fetch_policy IN (
                               'allow',
                               'deny',
                               'not_checked'
                           )),
    created_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, candidate_id),
    -- 精确重复（同 source 同 URL 同内容）：只留 1 个候选。
    CONSTRAINT phase0_candidates_dedup_key UNIQUE (workspace_id, source_id, dedup_key),
    -- 同内容指纹（同 source、不同 URL 也算重复投递）：只留 1 个候选。
    CONSTRAINT phase0_candidates_content_key
        UNIQUE (workspace_id, source_id, content_hash),
    CONSTRAINT phase0_candidates_candidate_key UNIQUE (workspace_id, candidate_id),
    CONSTRAINT phase0_candidates_source_fk
        FOREIGN KEY (workspace_id, source_id)
        REFERENCES p0_t27.phase0_sources (workspace_id, source_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_candidates_cluster_fk
        FOREIGN KEY (workspace_id, cluster_id)
        REFERENCES p0_t27.phase0_duplicate_clusters (workspace_id, cluster_id)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_candidates_duplicate_fk
        FOREIGN KEY (workspace_id, duplicate_of_candidate_id)
        REFERENCES p0_t27.phase0_candidates (workspace_id, candidate_id)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_candidates_near_duplicate_fk
        FOREIGN KEY (workspace_id, near_duplicate_of)
        REFERENCES p0_t27.phase0_candidates (workspace_id, candidate_id)
        ON DELETE RESTRICT,
    -- 重复标记必须与链表头一致：is_duplicate=true ⇔ duplicate_of 非空且不是自己。
    CONSTRAINT phase0_candidates_duplicate_shape CHECK (
        (is_duplicate
             AND duplicate_of_candidate_id IS NOT NULL
             AND duplicate_of_candidate_id <> candidate_id)
        OR (NOT is_duplicate AND duplicate_of_candidate_id IS NULL)
    ),
    CONSTRAINT phase0_candidates_near_shape CHECK (
        (near_duplicate_of IS NULL AND near_duplicate_distance IS NULL)
        OR (near_duplicate_of IS NOT NULL
            AND near_duplicate_distance IS NOT NULL
            AND near_duplicate_of <> candidate_id)
    )
);

COMMENT ON TABLE p0_t27.phase0_candidates IS
    '候选不可变事实：捕获时的来源/内容/评分/证据集指纹。当前状态由事件推导，'
    '本表 UPDATE/DELETE 被触发器拒绝（I1）。';

CREATE INDEX phase0_candidates_source_idx
    ON p0_t27.phase0_candidates (workspace_id, source_id, created_at);
CREATE INDEX phase0_candidates_cluster_idx
    ON p0_t27.phase0_candidates (workspace_id, cluster_id);
CREATE INDEX phase0_candidates_simhash_idx
    ON p0_t27.phase0_candidates (workspace_id, simhash64);


-- -----------------------------------------------------------------------------
-- §6 结构化关键事实 + 逐事实证据（均只追加）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t27.phase0_candidate_facts (
    workspace_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    fact_id      text NOT NULL CHECK (length(fact_id) BETWEEN 1 AND 64),
    fact_hash    text NOT NULL CHECK (fact_hash ~ '^[0-9a-f]{64}$'),
    -- 关键事实必须至少一条有效证据；非关键事实缺失只降证据密度，不阻断发布。
    is_critical  boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, candidate_id, fact_id),
    CONSTRAINT phase0_candidate_facts_candidate_fk
        FOREIGN KEY (workspace_id, candidate_id)
        REFERENCES p0_t27.phase0_candidates (workspace_id, candidate_id)
        ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t27.phase0_candidate_facts IS
    '候选内结构化关键事实（只追加）：fact_id + fact_hash，正文不入库，只存指纹。';

CREATE TABLE p0_t27.phase0_candidate_evidence (
    workspace_id     uuid NOT NULL,
    evidence_id      uuid NOT NULL,
    candidate_id     uuid NOT NULL,
    fact_id          text NOT NULL,
    source_version_id uuid NOT NULL,
    -- T11 Locator.to_dict() 的 JSON 形态（kind/format + 该 kind 使用的定位字段）。
    locator          jsonb NOT NULL,
    quote_hash       text NOT NULL CHECK (quote_hash ~ '^[0-9a-f]{64}$'),
    quote_length     integer NOT NULL CHECK (quote_length BETWEEN 1 AND 4000),
    observed_at      timestamptz NOT NULL,
    expires_at       timestamptz,
    -- 只有 verified=true 的证据才算「有效证据」；未核验证据不得计入发布条件。
    verified         boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, evidence_id),
    CONSTRAINT phase0_candidate_evidence_fact_fk
        FOREIGN KEY (workspace_id, candidate_id, fact_id)
        REFERENCES p0_t27.phase0_candidate_facts (workspace_id, candidate_id, fact_id)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_candidate_evidence_locator_shape CHECK (
        jsonb_typeof(locator) = 'object'
        AND locator ? 'kind'
        AND locator ? 'format'
        AND length(locator ->> 'kind') BETWEEN 1 AND 32
    ),
    CONSTRAINT phase0_candidate_evidence_expiry_check
        CHECK (expires_at IS NULL OR expires_at > observed_at)
);

COMMENT ON TABLE p0_t27.phase0_candidate_evidence IS
    '逐事实证据（只追加）：引用 source_version_id + T11 locator + quote_hash。'
    '「有效证据」= verified AND 未过期；未核验/过期证据不得支撑发布。';

CREATE INDEX phase0_candidate_evidence_candidate_idx
    ON p0_t27.phase0_candidate_evidence (workspace_id, candidate_id, fact_id);


-- -----------------------------------------------------------------------------
-- §7 候选事件（只追加）——当前状态的唯一来源
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t27.phase0_candidate_events (
    workspace_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    event_seq    bigint GENERATED ALWAYS AS IDENTITY,
    event_id     uuid NOT NULL,
    event_type   text NOT NULL CHECK (event_type IN (
                     'captured',
                     'admitted',
                     'rejected',
                     'needs_review',
                     'review_requested',
                     'approved',
                     'declined',
                     'promoted',
                     'promotion_blocked',
                     'expired',
                     'superseded'
                 )),
    -- machine 保留给「被拒绝的自动审核尝试」留痕，不能产出 approved。
    actor_kind   text NOT NULL CHECK (actor_kind IN ('human', 'service', 'system', 'machine')),
    actor_ref    text NOT NULL CHECK (length(actor_ref) BETWEEN 1 AND 128),
    reason_code  text NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 64),
    -- 幂等键：同一次转换重复提交只留一行（例如重复 approve 不产生第二条 approved）。
    dedup_key    text CHECK (dedup_key IS NULL OR length(dedup_key) BETWEEN 1 AND 128),
    detail       text CHECK (detail IS NULL OR length(detail) <= 512),
    recorded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, event_id),
    CONSTRAINT phase0_candidate_events_seq_key UNIQUE (event_seq),
    CONSTRAINT phase0_candidate_events_dedup_key UNIQUE (workspace_id, dedup_key),
    CONSTRAINT phase0_candidate_events_candidate_fk
        FOREIGN KEY (workspace_id, candidate_id)
        REFERENCES p0_t27.phase0_candidates (workspace_id, candidate_id)
        ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t27.phase0_candidate_events IS
    '候选审计（只追加）：状态机由最新事件推导；dedup_key 保证重复提交不产生第二条同义事件。';

CREATE INDEX phase0_candidate_events_candidate_idx
    ON p0_t27.phase0_candidate_events (workspace_id, candidate_id, event_seq);


-- -----------------------------------------------------------------------------
-- §8 人工 ReviewDecision（只追加；只允许 human）
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t27.phase0_review_decisions (
    workspace_id        uuid NOT NULL,
    review_decision_id  uuid NOT NULL,
    candidate_id        uuid,
    source_version_id   uuid NOT NULL,
    kb_id               uuid NOT NULL,
    reviewer_principal_id uuid NOT NULL,
    reviewer_kind       text NOT NULL CHECK (reviewer_kind = 'human'),
    decision            text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    -- 三要素之二：理由码（稳定枚举）+ 自由文本理由（必填，不可只给码）。
    reason_code         text NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 64),
    reason_text         text NOT NULL CHECK (length(btrim(reason_text)) BETWEEN 1 AND 1000),
    -- 三要素之二：决策时间必须由审批人当时给出，不能留空由入库时间顶替。
    decided_at          timestamptz NOT NULL,
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, review_decision_id),
    -- 重复审批幂等：同 candidate/reviewer/decision/reason 只留一行。
    CONSTRAINT phase0_review_decisions_idem_key
        UNIQUE (workspace_id, candidate_id, reviewer_principal_id, decision, reason_code),
    CONSTRAINT phase0_review_decisions_candidate_fk
        FOREIGN KEY (workspace_id, candidate_id)
        REFERENCES p0_t27.phase0_candidates (workspace_id, candidate_id)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_review_decisions_reviewer_fk
        FOREIGN KEY (workspace_id, reviewer_principal_id)
        REFERENCES p0_t27.phase0_principals (workspace_id, principal_id)
        ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t27.phase0_review_decisions IS
    '人工审阅决定（只追加）：CHECK 限定 reviewer_kind=human；'
    '身份/时间/理由三要素 NOT NULL；同一 candidate 至多一条 approved。'
    'decision 只作用于知识入库，**不**替代敏感工具审批（TDD07 §5）。';

-- 同一 candidate 至多一条 approved：批准是终态，回退必须新候选/新决定流。
CREATE UNIQUE INDEX phase0_review_decisions_one_approval
    ON p0_t27.phase0_review_decisions (workspace_id, candidate_id)
    WHERE decision = 'approved' AND candidate_id IS NOT NULL;

CREATE INDEX phase0_review_decisions_candidate_idx
    ON p0_t27.phase0_review_decisions (workspace_id, candidate_id, decided_at);


-- -----------------------------------------------------------------------------
-- §9 发布事实（只追加）——唯一入口必须携带有效 ReviewDecision
-- -----------------------------------------------------------------------------
CREATE TABLE p0_t27.phase0_promotions (
    workspace_id       uuid NOT NULL,
    promotion_id       uuid NOT NULL,
    candidate_id       uuid NOT NULL,
    review_decision_id uuid NOT NULL,
    kb_id              uuid NOT NULL,
    evidence_count     smallint NOT NULL CHECK (evidence_count >= 1),
    promoted_at        timestamptz NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, promotion_id),
    -- 一个候选只发布一次（重复请求返回既有行，不新增事实）。
    CONSTRAINT phase0_promotions_candidate_key UNIQUE (workspace_id, candidate_id),
    CONSTRAINT phase0_promotions_candidate_fk
        FOREIGN KEY (workspace_id, candidate_id)
        REFERENCES p0_t27.phase0_candidates (workspace_id, candidate_id)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_promotions_decision_fk
        FOREIGN KEY (workspace_id, review_decision_id)
        REFERENCES p0_t27.phase0_review_decisions (workspace_id, review_decision_id)
        ON DELETE RESTRICT
);

COMMENT ON TABLE p0_t27.phase0_promotions IS
    '入库事实（只追加）：必须引用 review_decision_id；插入时由 '
    'phase0_promotion_guard() 复核审批有效性、来源状态与证据完整性。';


-- -----------------------------------------------------------------------------
-- §10 只追加：UPDATE / DELETE 一律拒绝（SQLSTATE 23514）
-- -----------------------------------------------------------------------------
CREATE FUNCTION p0_t27.phase0_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t27
    AS $fn$
    BEGIN
        RAISE EXCEPTION 'P0-T27 追加史：拒绝 % ON %（改状态请追加事件行）',
            TG_OP, TG_TABLE_NAME
            USING ERRCODE = 'check_violation';
    END
    $fn$;

CREATE TRIGGER phase0_sources_append_only
    BEFORE UPDATE OR DELETE ON p0_t27.phase0_sources
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_reject_mutation();
CREATE TRIGGER phase0_source_events_append_only
    BEFORE UPDATE OR DELETE ON p0_t27.phase0_source_events
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_reject_mutation();
CREATE TRIGGER phase0_duplicate_clusters_append_only
    BEFORE UPDATE OR DELETE ON p0_t27.phase0_duplicate_clusters
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_reject_mutation();
CREATE TRIGGER phase0_candidates_append_only
    BEFORE UPDATE OR DELETE ON p0_t27.phase0_candidates
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_reject_mutation();
CREATE TRIGGER phase0_candidate_facts_append_only
    BEFORE UPDATE OR DELETE ON p0_t27.phase0_candidate_facts
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_reject_mutation();
CREATE TRIGGER phase0_candidate_evidence_append_only
    BEFORE UPDATE OR DELETE ON p0_t27.phase0_candidate_evidence
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_reject_mutation();
CREATE TRIGGER phase0_candidate_events_append_only
    BEFORE UPDATE OR DELETE ON p0_t27.phase0_candidate_events
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_reject_mutation();
CREATE TRIGGER phase0_review_decisions_append_only
    BEFORE UPDATE OR DELETE ON p0_t27.phase0_review_decisions
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_reject_mutation();
CREATE TRIGGER phase0_promotions_append_only
    BEFORE UPDATE OR DELETE ON p0_t27.phase0_promotions
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_reject_mutation();


-- -----------------------------------------------------------------------------
-- §11 状态推导与发布闸门（唯一允许「读历史、判当前」的地方）
-- -----------------------------------------------------------------------------
-- 来源当前状态 = 最新 source_event；无事件视为 'captured'。
CREATE FUNCTION p0_t27.phase0_source_state(p_workspace uuid, p_source uuid)
    RETURNS text
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, p0_t27
    AS $fn$
        SELECT COALESCE(
            (
                SELECT e.event_type
                FROM p0_t27.phase0_source_events AS e
                WHERE e.workspace_id = p_workspace AND e.source_id = p_source
                ORDER BY e.event_seq DESC
                LIMIT 1
            ),
            'captured'
        )
    $fn$;

COMMENT ON FUNCTION p0_t27.phase0_source_state(uuid, uuid) IS
    '来源当前状态：最新事件；无事件=captured。revoked/expired/rejected 为阻断态。';

-- 候选当前状态 = 最新 candidate_event；无事件视为 'captured'。
CREATE FUNCTION p0_t27.phase0_candidate_state(p_workspace uuid, p_candidate uuid)
    RETURNS text
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, p0_t27
    AS $fn$
        SELECT COALESCE(
            (
                SELECT e.event_type
                FROM p0_t27.phase0_candidate_events AS e
                WHERE e.workspace_id = p_workspace AND e.candidate_id = p_candidate
                ORDER BY e.event_seq DESC
                LIMIT 1
            ),
            'captured'
        )
    $fn$;

COMMENT ON FUNCTION p0_t27.phase0_candidate_state(uuid, uuid) IS
    '候选当前状态：最新事件；无事件=captured。';

-- 本 schema 的"现在"：默认 ``now()``；测试可在事务内 ``SET LOCAL p0_t27.now = ...``
-- 显式推进时间，用于复验"过期证据在 promote 时被拒"而不必 sleep。
-- 该 GUC 只影响本 schema 的有效性判定，不参与任何授权/审计。
CREATE FUNCTION p0_t27.phase0_now() RETURNS timestamptz
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, p0_t27
    AS $fn$
        SELECT COALESCE(
            NULLIF(current_setting('p0_t27.now', true), '')::timestamptz,
            now()
        )
    $fn$;

COMMENT ON FUNCTION p0_t27.phase0_now() IS
    'schema 时间源：默认 now()；p0_t27.now GUC 仅供测试推进时间，不参与授权。';

-- 有效证据判定：已核验 + 未过期。单一函数，供闸门与测试共用同一口径。
CREATE FUNCTION p0_t27.phase0_evidence_is_valid(
    p_verified boolean,
    p_expires_at timestamptz,
    p_observed_at timestamptz
) RETURNS boolean
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, p0_t27
    AS $fn$
        SELECT p_verified
            AND (p_expires_at IS NULL OR p_expires_at > p0_t27.phase0_now())
            AND p_observed_at <= p0_t27.phase0_now()
    $fn$;

COMMENT ON FUNCTION p0_t27.phase0_evidence_is_valid(boolean, timestamptz, timestamptz) IS
    '有效证据 = verified AND 未过期 AND 观测时间不晚于现在（禁止未来证据）。';

-- 发布闸门：I4 的数据库层兜底。任何路径（含绕过 Python 的直插）都要过这一关。
CREATE FUNCTION p0_t27.phase0_promotion_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t27
    AS $fn$
    DECLARE
        decision_row p0_t27.phase0_review_decisions%ROWTYPE;
        candidate_state text;
        source_state text;
        source_id uuid;
        missing_facts integer;
    BEGIN
        -- (1) 审批必须存在、已批准、且指向同一 candidate 与同一 kb。
        SELECT * INTO decision_row
        FROM p0_t27.phase0_review_decisions AS d
        WHERE d.workspace_id = NEW.workspace_id
          AND d.review_decision_id = NEW.review_decision_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION '发布必须携带 review_decision_id（未找到决定）'
                USING ERRCODE = 'check_violation';
        END IF;
        IF decision_row.decision <> 'approved' THEN
            RAISE EXCEPTION '发布只接 approved 的 ReviewDecision（当前 %）',
                decision_row.decision USING ERRCODE = 'check_violation';
        END IF;
        IF decision_row.candidate_id IS DISTINCT FROM NEW.candidate_id THEN
            RAISE EXCEPTION 'ReviewDecision 不属于该候选'
                USING ERRCODE = 'check_violation';
        END IF;
        IF decision_row.kb_id <> NEW.kb_id THEN
            RAISE EXCEPTION 'ReviewDecision 的目标 KB 与发布目标不一致'
                USING ERRCODE = 'check_violation';
        END IF;

        -- (2) 候选与来源不得处于阻断态（撤销/过期/拒绝/已取代）。
        SELECT s.source_id INTO source_id
        FROM p0_t27.phase0_candidates AS c
        JOIN p0_t27.phase0_sources AS s
          ON s.workspace_id = c.workspace_id AND s.source_id = c.source_id
        WHERE c.workspace_id = NEW.workspace_id AND c.candidate_id = NEW.candidate_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION '候选不存在' USING ERRCODE = 'check_violation';
        END IF;
        candidate_state := p0_t27.phase0_candidate_state(NEW.workspace_id, NEW.candidate_id);
        IF candidate_state IN ('rejected', 'expired', 'superseded', 'declined',
                                'promotion_blocked') THEN
            RAISE EXCEPTION '候选状态 % 禁止发布', candidate_state
                USING ERRCODE = 'check_violation';
        END IF;
        source_state := p0_t27.phase0_source_state(NEW.workspace_id, source_id);
        IF source_state NOT IN ('approved') THEN
            RAISE EXCEPTION '来源状态 % 禁止发布（需 approved）', source_state
                USING ERRCODE = 'check_violation';
        END IF;

        -- (3) 每个关键事实至少一条有效证据（verified + 未过期）。
        SELECT count(*) INTO missing_facts
        FROM p0_t27.phase0_candidate_facts AS f
        WHERE f.workspace_id = NEW.workspace_id
          AND f.candidate_id = NEW.candidate_id
          AND f.is_critical
          AND NOT EXISTS (
              SELECT 1
              FROM p0_t27.phase0_candidate_evidence AS e
              WHERE e.workspace_id = f.workspace_id
                AND e.candidate_id = f.candidate_id
                AND e.fact_id = f.fact_id
                AND p0_t27.phase0_evidence_is_valid(e.verified, e.expires_at, e.observed_at)
          );
        IF missing_facts > 0 THEN
            RAISE EXCEPTION '有 % 个关键事实缺少有效证据，禁止发布', missing_facts
                USING ERRCODE = 'check_violation';
        END IF;

        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_promotions_guard
    BEFORE INSERT ON p0_t27.phase0_promotions
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_promotion_guard();


-- -----------------------------------------------------------------------------
-- §12 机器主体不得产出审阅结论（显式拦截，错误信息带稳定 code）
-- -----------------------------------------------------------------------------
-- 说明：reviewer_kind 的 CHECK 已能拒绝 'service'；本触发器再拦一次
-- 「principal 表里其实是 service，但把 reviewer_kind 谎填为 human」的伪造路径。
CREATE FUNCTION p0_t27.phase0_review_reject_service() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, p0_t27
    AS $fn$
    DECLARE
        principal_kind text;
    BEGIN
        SELECT p.kind INTO principal_kind
        FROM p0_t27.phase0_principals AS p
        WHERE p.workspace_id = NEW.workspace_id
          AND p.principal_id = NEW.reviewer_principal_id;
        IF principal_kind IS NULL THEN
            RAISE EXCEPTION 'REVIEWER_UNKNOWN：审阅主体不存在'
                USING ERRCODE = 'check_violation';
        END IF;
        IF principal_kind <> 'human' THEN
            RAISE EXCEPTION 'REVIEWER_NOT_HUMAN：kind=% 的主体不得产出 ReviewDecision',
                principal_kind USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.decided_at > now() THEN
            RAISE EXCEPTION 'REVIEW_TIME_IN_FUTURE：审批时间不得晚于入库时刻'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END
    $fn$;

CREATE TRIGGER phase0_review_decisions_human_only
    BEFORE INSERT ON p0_t27.phase0_review_decisions
    FOR EACH ROW EXECUTE FUNCTION p0_t27.phase0_review_reject_service();


-- -----------------------------------------------------------------------------
-- §13 留位（不在本迁移实现）
-- -----------------------------------------------------------------------------
-- * RLS / p0_app 授权 / 会员模型：归 T03/T25，本 schema 不抢占全局角色名。
-- * 向量写入、chunk 与索引可见性：归 T12/T13，本任务「不直接写向量」（Task 卡步骤）。
-- * 近重复的文本相似度算法与阈值：本迁移只存 simhash64 与提示距离，阈值 U，
--   必须在 quality_algorithm 版本里显式声明，不做跨任务静默调参。
-- * ReviewDecision ↔ 工具 Approval 的映射：C-07 BLOCKED，未决前不建映射表；
--   promotions 只接受 review_decision_id，混用由 Python 层以 code 拒绝。
