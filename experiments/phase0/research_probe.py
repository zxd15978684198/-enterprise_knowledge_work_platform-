"""P0-T28：Research 全链控制站集成（Planner → Research → 摄取 → 引用）。

本模块**不重写子系统**，只做集成编排：用 LangGraph 的 ``StateGraph`` 把已有 port 串起来——

* Planner（T22 ``planner_probe.ProbePlanner``）产出 DAG，``dag_contracts`` 校验；
* 依赖调度（T23 ``task_scheduler``，schema ``p0_t23``）：
  register_graph / claim_ready / complete_task；
* 读取三通道（T04 控制站 HTTP、本地固定搜索适配、B-SK JS 适配/用户浏览器快照适配）；
* 审批中断（T21 原生 ``interrupt()`` + T06 对齐口径：intent/审批状态/过期/撤销/幂等消费）；
* 预算账本（T20 ``budget_ledger``，schema ``p0_t20``）；
* 工具门禁（T19 ``tool_registry``：``site.idem_write`` 需审批，审批不匹配/过期一律拒绝）；
* 业务事实与引用（本任务 schema ``p0_t28``）：Message/引用/Job/terminal 事件**一次事务**提交，
  复用 T26 的事件类型与「每 run 唯一 terminal」口径（不复制它的 schema）。

关键契约（与 TDD 04 §5 / 06 §2·7、C-01/C-02/C-07 对齐）：

1. Graph 只掌握**控制流**；业务事实由本模块的 owner 函数在共享事务里写。
2. terminal（completed/failed/cancelled）每 run 最多一条：DB 部分唯一索引 + 追加写触发器兜底。
3. 外部写（控制站幂等写）先带稳定 idempotency_key 执行，再在同一事务里写候选发布事实；
   重放（迟到 resume / 重跑）不得产生第二次外部副作用（控制站 ``/idem/count`` 断言）。
4. ``NOT_COPY``：不改 001–008 迁移、不碰他人 schema；``p0_t28`` 在用例结束前 DROP。

限域：本机没有真实 chat 模型，``ModelPort`` 用确定性替身；真实模型链路一律 ``NOT_RUN``。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import events_probe as ep
import psycopg
import scope as scope_module
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

SCHEMA: Final = "p0_t28"
MIGRATION_ID: Final = "t28_research_inline"
MIGRATION_FILENAME: Final = "inline:research_probe.py::MIGRATION_SQL"
REDIS_DB: Final = 15
CONTROL_SITE_PORTS: Final[tuple[int, ...]] = (5500, 5501)
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
EVIDENCE_PATH: Final = REPO_ROOT / "docs" / "poc" / "evidence" / "research-batch.json"

CHANNELS: Final[tuple[str, ...]] = ("search", "http", "js", "user_browser")
TASK_KINDS: Final[tuple[str, ...]] = (
    "search",
    "fetch_http",
    "browse_js",
    "user_browser",
    "analyze",
    "ingest",
)
FIXED_JOB_KINDS: Final[tuple[str, ...]] = ("fetch_http", "ingest")

RUN_STATUSES: Final[tuple[str, ...]] = (
    "queued",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
)
TERMINAL_RUN_STATUSES: Final[frozenset[str]] = frozenset({"succeeded", "failed", "cancelled"})
CANDIDATE_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "approved",
    "rejected",
    "published",
    "failed",
)
APPROVAL_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "approved",
    "rejected",
    "revoked",
    "consumed",
)
EVENT_TYPES: Final[tuple[str, ...]] = ep.EVENT_TYPES
TERMINAL_EVENT_TYPES: Final[tuple[str, ...]] = ep.TERMINAL_EVENT_TYPES
ANSWER_KINDS: Final[tuple[str, ...]] = ep.ANSWER_KINDS

MAX_GRAPH_STEPS: Final = 48
MAX_TASKS_PER_RUN: Final = 12
DEFAULT_APPROVAL_TTL_SECONDS: Final = 900
BSK_LIVE_ENV: Final = "BSK_LIVE"
MAX_DETAIL: Final = 512
MAX_QUOTE: Final = 400
MIGRATION_LOCK_ID: Final = 0x7A28

EVIDENCE_SCHEMA_VERSION: Final = 1

MIGRATION_SQL: Final = """
CREATE SCHEMA IF NOT EXISTS p0_t28;

CREATE TABLE IF NOT EXISTS p0_t28.phase0_t28_migrations (
    migration_id text PRIMARY KEY,
    filename text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now(),
    applied_by text NOT NULL DEFAULT current_user
);

CREATE TABLE IF NOT EXISTS p0_t28.phase0_principals (
    workspace_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    membership_subject_ref text NOT NULL CHECK (length(membership_subject_ref) BETWEEN 1 AND 128),
    kind text NOT NULL CHECK (kind IN ('human', 'service')),
    role text NOT NULL CHECK (role IN ('owner', 'member')),
    status text NOT NULL CHECK (status IN ('active', 'disabled')),
    membership_revision integer NOT NULL CHECK (membership_revision >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, principal_id)
);

CREATE TABLE IF NOT EXISTS p0_t28.phase0_runs (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    goal text NOT NULL CHECK (length(goal) BETWEEN 1 AND 512),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'waiting_approval', 'succeeded', 'failed',
                          'cancelled')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    plan_hash text CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    step integer NOT NULL DEFAULT 0 CHECK (step >= 0),
    generation integer NOT NULL DEFAULT 1 CHECK (generation >= 1),
    budget_mode text NOT NULL DEFAULT 'normal' CHECK (budget_mode IN ('normal', 'tight')),
    answer_kind text CHECK (answer_kind IS NULL OR answer_kind IN
        ('grounded', 'grounded_partial', 'insufficient_evidence', 'refused')),
    result_hash text CHECK (result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'),
    error_code text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    terminal_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    row_version integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, run_id),
    CHECK ((status IN ('succeeded', 'failed', 'cancelled')) = (terminal_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS p0_t28.phase0_sources (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    source_id uuid NOT NULL,
    task_id text NOT NULL CHECK (length(task_id) BETWEEN 1 AND 64),
    channel text NOT NULL CHECK (channel IN ('search', 'http', 'js', 'user_browser')),
    url text NOT NULL CHECK (length(url) BETWEEN 1 AND 512),
    license text NOT NULL CHECK (length(license) BETWEEN 1 AND 64),
    body_sha256 text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    fetched_at timestamptz NOT NULL DEFAULT now(),
    snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (workspace_id, source_id),
    UNIQUE (workspace_id, run_id, channel, body_sha256)
);

CREATE TABLE IF NOT EXISTS p0_t28.phase0_evidence (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    source_id uuid NOT NULL,
    quote text NOT NULL CHECK (length(quote) BETWEEN 1 AND 400),
    locator text NOT NULL CHECK (length(locator) BETWEEN 1 AND 200),
    quote_sha256 text NOT NULL CHECK (quote_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, evidence_id),
    UNIQUE (workspace_id, run_id, quote_sha256),
    FOREIGN KEY (workspace_id, source_id)
        REFERENCES p0_t28.phase0_sources (workspace_id, source_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS p0_t28.phase0_candidates (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    task_id text NOT NULL CHECK (length(task_id) BETWEEN 1 AND 64),
    dedupe_key text NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 160),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
    requires_approval boolean NOT NULL DEFAULT true,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'published', 'failed')),
    approval_id uuid,
    published_at timestamptz,
    job_id uuid,
    rejection_reason text CHECK (rejection_reason IS NULL
                                 OR length(rejection_reason) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    row_version integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, candidate_id),
    UNIQUE (workspace_id, run_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS p0_t28.phase0_citations (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, candidate_id, evidence_id),
    UNIQUE (workspace_id, run_id, candidate_id, ordinal),
    FOREIGN KEY (workspace_id, candidate_id)
        REFERENCES p0_t28.phase0_candidates (workspace_id, candidate_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace_id, evidence_id)
        REFERENCES p0_t28.phase0_evidence (workspace_id, evidence_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS p0_t28.phase0_approvals (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    approval_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    intent_key text NOT NULL CHECK (length(intent_key) BETWEEN 1 AND 128),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'revoked', 'consumed')),
    approver_principal_id uuid,
    reason text CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, approval_id),
    UNIQUE (workspace_id, run_id, intent_key),
    FOREIGN KEY (workspace_id, candidate_id)
        REFERENCES p0_t28.phase0_candidates (workspace_id, candidate_id) ON DELETE RESTRICT,
    CHECK ((status <> 'pending') = (decided_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS p0_t28.phase0_jobs (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    job_id uuid NOT NULL,
    candidate_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('ingest', 'enrichment')),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    dedupe_key text NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 160),
    error_code text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    row_version integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, job_id),
    UNIQUE (workspace_id, dedupe_key),
    FOREIGN KEY (workspace_id, candidate_id)
        REFERENCES p0_t28.phase0_candidates (workspace_id, candidate_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS p0_t28.phase0_events (
    workspace_id uuid NOT NULL,
    event_id uuid NOT NULL,
    run_id uuid NOT NULL,
    event_seq bigint GENERATED ALWAYS AS IDENTITY,
    event_type text NOT NULL CHECK (event_type IN (
        'run_started', 'step_progress', 'source_found', 'candidate_ready',
        'approval_required', 'token', 'generation_reset', 'indexed',
        'enrichment_updated', 'completed', 'failed', 'cancelled')),
    generation integer NOT NULL DEFAULT 1 CHECK (generation >= 1),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, event_seq),
    UNIQUE (workspace_id, event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS phase0_t28_run_terminal_unique
    ON p0_t28.phase0_events (workspace_id, run_id)
    WHERE event_type IN ('completed', 'failed', 'cancelled');

CREATE INDEX IF NOT EXISTS phase0_t28_events_run_idx
    ON p0_t28.phase0_events (workspace_id, run_id, event_seq);

CREATE INDEX IF NOT EXISTS phase0_t28_sources_run_idx
    ON p0_t28.phase0_sources (workspace_id, run_id, channel);

CREATE INDEX IF NOT EXISTS phase0_t28_candidates_run_idx
    ON p0_t28.phase0_candidates (workspace_id, run_id, status);

CREATE OR REPLACE FUNCTION p0_t28.phase0_t28_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'P0-T28 事实只允许追加写，拒绝 % 于 %', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'check_violation';
END;
$$;

DROP TRIGGER IF EXISTS phase0_t28_events_append_only ON p0_t28.phase0_events;
CREATE TRIGGER phase0_t28_events_append_only
    BEFORE UPDATE OR DELETE ON p0_t28.phase0_events
    FOR EACH ROW EXECUTE FUNCTION p0_t28.phase0_t28_reject_mutation();

DROP TRIGGER IF EXISTS phase0_t28_citations_append_only ON p0_t28.phase0_citations;
CREATE TRIGGER phase0_t28_citations_append_only
    BEFORE UPDATE OR DELETE ON p0_t28.phase0_citations
    FOR EACH ROW EXECUTE FUNCTION p0_t28.phase0_t28_reject_mutation();

DROP TRIGGER IF EXISTS phase0_t28_sources_append_only ON p0_t28.phase0_sources;
CREATE TRIGGER phase0_t28_sources_append_only
    BEFORE UPDATE OR DELETE ON p0_t28.phase0_sources
    FOR EACH ROW EXECUTE FUNCTION p0_t28.phase0_t28_reject_mutation();

DROP TRIGGER IF EXISTS phase0_t28_evidence_append_only ON p0_t28.phase0_evidence;
CREATE TRIGGER phase0_t28_evidence_append_only
    BEFORE UPDATE OR DELETE ON p0_t28.phase0_evidence
    FOR EACH ROW EXECUTE FUNCTION p0_t28.phase0_t28_reject_mutation();

CREATE OR REPLACE FUNCTION p0_t28.phase0_t28_runs_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    allowed boolean;
BEGIN
    IF NEW.workspace_id <> OLD.workspace_id OR NEW.run_id <> OLD.run_id
       OR NEW.goal <> OLD.goal THEN
        RAISE EXCEPTION 'P0-T28 run 身份列不可变' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'P0-T28 run row_version 必须严格 +1（% -> %）',
            OLD.row_version, NEW.row_version USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status IN ('succeeded', 'failed', 'cancelled') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'P0-T28 run 终态不可改写（% -> %）', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.generation < OLD.generation THEN
        RAISE EXCEPTION 'P0-T28 run generation 不得回退' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status <> OLD.status THEN
        SELECT NEW.status = ANY (t.allowed_from) INTO allowed
        FROM (VALUES
            ('queued', ARRAY['running', 'cancelled', 'failed']),
            ('running', ARRAY['waiting_approval', 'succeeded', 'failed', 'cancelled']),
            ('waiting_approval', ARRAY['running', 'succeeded', 'failed', 'cancelled'])
        ) AS t(old_status, allowed_from)
        WHERE t.old_status = OLD.status;
        IF allowed IS NULL OR NOT allowed THEN
            RAISE EXCEPTION 'P0-T28 非法 run 状态转移 % -> %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS phase0_t28_runs_guard ON p0_t28.phase0_runs;
CREATE TRIGGER phase0_t28_runs_guard
    BEFORE UPDATE ON p0_t28.phase0_runs
    FOR EACH ROW EXECUTE FUNCTION p0_t28.phase0_t28_runs_guard();

CREATE OR REPLACE FUNCTION p0_t28.phase0_t28_candidates_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    approved integer;
BEGIN
    IF NEW.workspace_id <> OLD.workspace_id OR NEW.candidate_id <> OLD.candidate_id
       OR NEW.dedupe_key <> OLD.dedupe_key OR NEW.content_sha256 <> OLD.content_sha256 THEN
        RAISE EXCEPTION 'P0-T28 candidate 身份列不可变' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'P0-T28 candidate row_version 必须严格 +1'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'published' AND NEW.status <> 'published' THEN
        RAISE EXCEPTION 'P0-T28 已发布候选不可回退（% -> %）', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'published' AND OLD.status <> 'published' THEN
        IF NEW.published_at IS NULL OR NEW.job_id IS NULL THEN
            RAISE EXCEPTION 'P0-T28 发布必须同时写 published_at 与 job_id'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.requires_approval THEN
            SELECT count(*) INTO approved FROM p0_t28.phase0_approvals a
            WHERE a.workspace_id = NEW.workspace_id AND a.candidate_id = NEW.candidate_id
              AND a.status IN ('approved', 'consumed');
            IF approved = 0 THEN
                RAISE EXCEPTION 'P0-T28 无有效审批不得发布（candidate=%）', NEW.candidate_id
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS phase0_t28_candidates_guard ON p0_t28.phase0_candidates;
CREATE TRIGGER phase0_t28_candidates_guard
    BEFORE UPDATE ON p0_t28.phase0_candidates
    FOR EACH ROW EXECUTE FUNCTION p0_t28.phase0_t28_candidates_guard();

CREATE OR REPLACE FUNCTION p0_t28.phase0_t28_candidate_requires_evidence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'published' AND NOT EXISTS (
        SELECT 1 FROM p0_t28.phase0_citations c
        WHERE c.workspace_id = NEW.workspace_id AND c.candidate_id = NEW.candidate_id
    ) THEN
        RAISE EXCEPTION 'P0-T28 发布候选必须有至少一条证据引用（candidate=%）', NEW.candidate_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS phase0_t28_candidate_requires_evidence ON p0_t28.phase0_candidates;
CREATE CONSTRAINT TRIGGER phase0_t28_candidate_requires_evidence
    AFTER UPDATE ON p0_t28.phase0_candidates
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION p0_t28.phase0_t28_candidate_requires_evidence();

CREATE OR REPLACE FUNCTION p0_t28.phase0_t28_approvals_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id <> OLD.workspace_id OR NEW.approval_id <> OLD.approval_id
       OR NEW.intent_key <> OLD.intent_key OR NEW.candidate_id <> OLD.candidate_id THEN
        RAISE EXCEPTION 'P0-T28 approval 身份列不可变' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status IN ('rejected', 'revoked', 'consumed') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'P0-T28 审批终态不可改写（% -> %）', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS phase0_t28_approvals_guard ON p0_t28.phase0_approvals;
CREATE TRIGGER phase0_t28_approvals_guard
    BEFORE UPDATE ON p0_t28.phase0_approvals
    FOR EACH ROW EXECUTE FUNCTION p0_t28.phase0_t28_approvals_guard();
"""


class ResearchError(RuntimeError):
    """研究链路的可分类错误；``code`` 进入 terminal 事件与证据。"""

    code: str = "RESEARCH_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class SchemaAlreadyApplied(ResearchError):
    code = "SCHEMA_ALREADY_APPLIED"


class SchemaNotApplied(ResearchError):
    code = "SCHEMA_NOT_APPLIED"


class RunNotFound(ResearchError):
    code = "RUN_NOT_FOUND"


class StaleGeneration(ResearchError):
    code = "STALE_GENERATION"


class RunAlreadyTerminal(ResearchError):
    code = "RUN_ALREADY_TERMINAL"


class ApprovalRequired(ResearchError):
    code = "TOOL_APPROVAL_REQUIRED"


class ApprovalMismatch(ResearchError):
    code = "TOOL_APPROVAL_MISMATCH"


class ApprovalExpired(ResearchError):
    code = "TOOL_APPROVAL_EXPIRED"


class ApprovalRevoked(ResearchError):
    code = "TOOL_APPROVAL_REVOKED"


class CandidateNotPublishable(ResearchError):
    code = "CANDIDATE_NOT_PUBLISHABLE"


class ChannelUnavailable(ResearchError):
    code = "CHANNEL_UNAVAILABLE"


class BudgetDenied(ResearchError):
    code = "BUDGET_EXCEEDED"


class UnauthorizedToolCall(ResearchError):
    code = "UNAUTHORIZED_TOOL_CALL"


class CapabilityUnsupported(ResearchError):
    code = "CAPABILITY_UNSUPPORTED"


# ---------------------------------------------------------------------------
# 连接 / 迁移
# ---------------------------------------------------------------------------


def read_dsn() -> str:
    return scope_module.read_dsn()


def connect(*, dsn: str | None = None, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn or read_dsn(), autocommit=autocommit, row_factory=dict_row)


def migration_sha256(ddl: str = MIGRATION_SQL) -> str:
    return hashlib.sha256(ddl.encode("utf-8")).hexdigest()


def _require_idle(conn: psycopg.Connection[Any]) -> None:
    if conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ResearchError("apply_schema 需要非 autocommit 且事务 IDLE 的连接")


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    """只删本任务 schema（绝不碰其它任务的 schema）。"""

    if not conn.autocommit:
        raise ResearchError("reset_schema 需要 autocommit 连接")
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    # LangGraph 的 checkpoint 表也落在本 schema，DROP CASCADE 一并清理。


def apply_schema(conn: psycopg.Connection[Any]) -> str:
    _require_idle(conn)
    digest = migration_sha256()
    existing = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = %s "
        "AND table_name = 'phase0_t28_migrations'",
        (SCHEMA,),
    ).fetchone()
    if existing is not None:
        raise SchemaAlreadyApplied(f"{SCHEMA} 已有迁移台账；重复 apply 被拒绝")
    conn.execute(MIGRATION_SQL)
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_t28_migrations (migration_id, filename, sha256) "
        "VALUES (%s, %s, %s)",
        (MIGRATION_ID, MIGRATION_FILENAME, digest),
    )
    conn.commit()
    return digest


def ensure_schema(conn: psycopg.Connection[Any]) -> str:
    row = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (SCHEMA,)
    ).fetchone()
    if row is None:
        return apply_schema(conn)
    conn.commit()
    return "present"


def ensure_checkpoint_schema(*, dsn: str | None = None) -> tuple[str, ...]:
    """在 ``p0_t28`` 下建 LangGraph 原生 checkpoint 表（search_path 限定）。"""

    from langgraph.checkpoint.postgres import PostgresSaver

    with closing(psycopg.connect(dsn or read_dsn(), autocommit=True)) as admin:
        admin.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    with closing(_saver_connection(dsn)) as conn:
        PostgresSaver(conn).setup()
    with closing(
        psycopg.connect(dsn or read_dsn(), autocommit=True, row_factory=dict_row)
    ) as admin:
        rows = admin.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s "
            "AND table_name LIKE 'checkpoint%%' ORDER BY 1",
            (SCHEMA,),
        ).fetchall()
    return tuple(str(row["table_name"]) for row in rows)


def _saver_connection(dsn: str | None = None) -> psycopg.Connection[Any]:
    return psycopg.connect(
        dsn or read_dsn(), autocommit=True, options=f"-c search_path={SCHEMA}"
    )


# ---------------------------------------------------------------------------
# 事件与 run 生命周期
# ---------------------------------------------------------------------------


def result_hash(*, run_id: uuid.UUID, content: str) -> str:
    return hashlib.sha256(f"{run_id}:{content}".encode()).hexdigest()


def _event_payload(payload: Mapping[str, Any] | None = None) -> str:
    return json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True)


def append_event(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    generation: int = 1,
) -> uuid.UUID:
    if event_type not in EVENT_TYPES:
        raise ResearchError(f"未知事件类型: {event_type}", code="EVENT_TYPE_INVALID")
    event_id = uuid.uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_events "
        "(workspace_id, event_id, run_id, event_type, generation, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
        (workspace_id, event_id, run_id, event_type, generation, _event_payload(payload)),
    )
    return event_id


def create_run(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    goal: str,
    budget_mode: str = "normal",
) -> RunState:
    """建 run；幂等（同 run_id 已存在则返回既有行）。"""

    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_runs WHERE workspace_id = %s AND run_id = %s FOR UPDATE",
        (workspace_id, run_id),
    ).fetchone()
    if row is not None:
        return _run_state(row)
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_runs (workspace_id, run_id, goal, status, budget_mode) "
        "VALUES (%s, %s, %s, 'queued', %s)",
        (workspace_id, run_id, goal, budget_mode),
    )
    append_event(conn, workspace_id=workspace_id, run_id=run_id, event_type="run_started",
                 payload={"goal": goal, "budget_mode": budget_mode})
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_runs WHERE workspace_id = %s AND run_id = %s",
        (workspace_id, run_id),
    ).fetchone()
    assert row is not None
    return _run_state(row)


def read_run(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> RunState:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_runs WHERE workspace_id = %s AND run_id = %s",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        raise RunNotFound(f"run {run_id} 不存在")
    return _run_state(row)


def lock_run(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> RunState:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_runs WHERE workspace_id = %s AND run_id = %s FOR UPDATE",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        raise RunNotFound(f"run {run_id} 不存在")
    return _run_state(row)


def set_run_status(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    status: str,
    expected_row_version: int | None = None,
    step: int | None = None,
    plan_hash: str | None = None,
) -> RunState:
    """CAS 迁移 run 状态（``expected_row_version`` 不符 → ``STALE_GENERATION`` 语义拒绝）。"""

    current = lock_run(conn, workspace_id=workspace_id, run_id=run_id)
    if current.status in TERMINAL_RUN_STATUSES:
        raise RunAlreadyTerminal(f"run {run_id} 已终态（{current.status}）")
    if expected_row_version is not None and current.row_version != expected_row_version:
        raise StaleGeneration(
            f"run {run_id} row_version={current.row_version} 与期望 {expected_row_version} 不符"
        )
    terminal = status in TERMINAL_RUN_STATUSES
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_runs SET status = %s, row_version = row_version + 1, "
        "step = COALESCE(%s, step), plan_hash = COALESCE(%s, plan_hash), "
        "updated_at = now(), terminal_at = CASE WHEN %s THEN now() ELSE terminal_at END "
        "WHERE workspace_id = %s AND run_id = %s RETURNING *",
        (status, step, plan_hash, terminal, workspace_id, run_id),
    ).fetchone()
    assert row is not None
    return _run_state(row)


def bump_step(conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_runs SET step = step + 1, row_version = row_version + 1, "
        "updated_at = now() WHERE workspace_id = %s AND run_id = %s "
        "AND status NOT IN ('succeeded', 'failed', 'cancelled')",
        (workspace_id, run_id),
    )


# ---------------------------------------------------------------------------
# 来源 / 证据 / 候选
# ---------------------------------------------------------------------------


def record_source(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: str,
    channel: str,
    url: str,
    body: str,
    license_name: str,
    snapshot: Mapping[str, Any] | None = None,
) -> SourceRecord:
    """追加一条来源事实；同一 run/channel/正文哈希只落一行（精确重复不重索引）。"""

    if channel not in CHANNELS:
        raise ResearchError(f"未知读取通道: {channel}", code="CHANNEL_INVALID")
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_sources WHERE workspace_id = %s AND run_id = %s "
        "AND channel = %s AND body_sha256 = %s",
        (workspace_id, run_id, channel, body_sha),
    ).fetchone()
    if row is not None:
        return _source_record(row)
    source_id = uuid.uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_sources (workspace_id, run_id, source_id, task_id, channel,"
        " url, license, body_sha256, snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (workspace_id, run_id, source_id, task_id, channel, url, license_name, body_sha,
         _event_payload(snapshot)),
    )
    append_event(conn, workspace_id=workspace_id, run_id=run_id, event_type="source_found",
                 payload={"source_id": str(source_id), "channel": channel, "url": url,
                          "sha256": body_sha})
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_sources WHERE workspace_id = %s AND source_id = %s",
        (workspace_id, source_id),
    ).fetchone()
    assert row is not None
    return _source_record(row)


def record_evidence(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    source_id: uuid.UUID,
    quote: str,
    locator: str,
) -> EvidenceRecord:
    quote_sha = hashlib.sha256(quote.encode("utf-8")).hexdigest()
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_evidence WHERE workspace_id = %s AND run_id = %s "
        "AND quote_sha256 = %s",
        (workspace_id, run_id, quote_sha),
    ).fetchone()
    if row is not None:
        return _evidence_record(row)
    evidence_id = uuid.uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_evidence (workspace_id, run_id, evidence_id, source_id,"
        " quote, locator, quote_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, run_id, evidence_id, source_id, quote[:MAX_QUOTE], locator, quote_sha),
    )
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_evidence WHERE workspace_id = %s AND evidence_id = %s",
        (workspace_id, evidence_id),
    ).fetchone()
    assert row is not None
    return _evidence_record(row)


def build_candidate(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: str,
    title: str,
    body: str,
    evidence_ids: Sequence[uuid.UUID],
    requires_approval: bool = True,
) -> CandidateRecord:
    """建候选 + 写引用（可重复调用：同 dedupe_key 返回既有行，不重复写）。"""

    if not evidence_ids:
        raise CandidateNotPublishable("候选必须至少有一条证据，拒绝无证据候选", code="NO_EVIDENCE")
    content_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    dedupe_key = f"{task_id}:{content_sha[:32]}"
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_candidates WHERE workspace_id = %s AND run_id = %s "
        "AND dedupe_key = %s",
        (workspace_id, run_id, dedupe_key),
    ).fetchone()
    if row is not None:
        return _candidate_record(row)
    candidate_id = uuid.uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_candidates (workspace_id, run_id, candidate_id, task_id,"
        " dedupe_key, content_sha256, title, body, requires_approval, status)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
        (workspace_id, run_id, candidate_id, task_id, dedupe_key, content_sha, title[:200],
         body[:4000], requires_approval),
    )
    for ordinal, evidence_id in enumerate(dict.fromkeys(evidence_ids), start=1):
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_citations (workspace_id, run_id, candidate_id,"
            " evidence_id, ordinal) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (workspace_id, run_id, candidate_id, evidence_id, ordinal),
        )
    append_event(conn, workspace_id=workspace_id, run_id=run_id, event_type="candidate_ready",
                 payload={"candidate_id": str(candidate_id), "dedupe_key": dedupe_key,
                          "evidence_count": len(set(evidence_ids))})
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_candidates WHERE workspace_id = %s AND candidate_id = %s",
        (workspace_id, candidate_id),
    ).fetchone()
    assert row is not None
    return _candidate_record(row)


def record_approval_request(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_id: uuid.UUID,
    intent_key: str,
    ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
) -> ApprovalRecord:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_approvals WHERE workspace_id = %s AND run_id = %s "
        "AND intent_key = %s",
        (workspace_id, run_id, intent_key),
    ).fetchone()
    if row is not None:
        return _approval_record(row)
    approval_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_approvals (workspace_id, run_id, approval_id,"
        " candidate_id, intent_key, status, expires_at) VALUES (%s, %s, %s, %s, %s, 'pending', %s)",
        (workspace_id, run_id, approval_id, candidate_id, intent_key, expires_at),
    )
    append_event(conn, workspace_id=workspace_id, run_id=run_id, event_type="approval_required",
                 payload={"approval_id": str(approval_id), "candidate_id": str(candidate_id),
                          "intent_key": intent_key})
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_approvals WHERE workspace_id = %s AND approval_id = %s",
        (workspace_id, approval_id),
    ).fetchone()
    assert row is not None
    return _approval_record(row)


def decide_approval(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    approval_id: uuid.UUID,
    decision: str,
    approver_principal_id: uuid.UUID | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> ApprovalRecord:
    """审批决策；重复决策保持首个结果（幂等），过期/撤销返回明确错误码。"""

    if decision not in ("approved", "rejected"):
        raise ResearchError(f"非法审批决策: {decision}", code="APPROVAL_DECISION_INVALID")
    current = _lock_approval(conn, workspace_id=workspace_id, approval_id=approval_id)
    if current.status == decision:
        return current
    if current.status in ("revoked", "consumed", "rejected"):
        raise ApprovalMismatch(f"审批 {approval_id} 已是 {current.status}，不能再改")
    moment = now or datetime.now(UTC)
    if current.expires_at <= moment:
        raise ApprovalExpired(f"审批 {approval_id} 已过期（{current.expires_at.isoformat()}）")
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_approvals SET status = %s, approver_principal_id = %s,"
        " reason = %s, decided_at = %s WHERE workspace_id = %s AND approval_id = %s RETURNING *",
        (decision, approver_principal_id, reason, moment, workspace_id, approval_id),
    ).fetchone()
    assert row is not None
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_candidates SET status = %s, approval_id = %s,"
        " rejection_reason = CASE WHEN %s = 'rejected' THEN %s ELSE rejection_reason END,"
        " row_version = row_version + 1"
        " WHERE workspace_id = %s AND candidate_id = %s",
        (
            decision,
            approval_id,
            decision,
            (reason or "human_rejected")[:64],
            workspace_id,
            current.candidate_id,
        ),
    )
    return _approval_record(row)


def revoke_approval(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    approval_id: uuid.UUID,
    reason: str = "revoked",
) -> ApprovalRecord:
    current = _lock_approval(conn, workspace_id=workspace_id, approval_id=approval_id)
    if current.status == "revoked":
        return current
    if current.status == "consumed":
        raise ApprovalMismatch("已消费的审批不能撤销")
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_approvals SET status = 'revoked', reason = %s, decided_at = now()"
        f" WHERE workspace_id = %s AND approval_id = %s RETURNING *",
        (reason[:256], workspace_id, approval_id),
    ).fetchone()
    assert row is not None
    return _approval_record(row)


def _lock_approval(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, approval_id: uuid.UUID
) -> ApprovalRecord:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_approvals WHERE workspace_id = %s AND approval_id = %s "
        "FOR UPDATE",
        (workspace_id, approval_id),
    ).fetchone()
    if row is None:
        raise ResearchError(f"审批 {approval_id} 不存在", code="APPROVAL_NOT_FOUND")
    return _approval_record(row)


# ---------------------------------------------------------------------------
# 发布（业务事实 + Job + 引用 + 事件，一次事务）
# ---------------------------------------------------------------------------


def publish_candidate(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_id: uuid.UUID,
    approval: ApprovalRecord | None = None,
    require_approval: bool | None = None,
) -> PublishResult:
    """把已审核候选发布为业务事实。

    一次事务内：candidate→published、写 Job（C-01：Job 只由发布路径写）、引用已在本事务外
    写好的证据行、写 ``indexed`` 事件。调用方负责 commit；回滚则四者都不在。
    """

    candidate = _lock_candidate(conn, workspace_id=workspace_id, candidate_id=candidate_id)
    if candidate.status == "published":
        job_row = conn.execute(
            f"SELECT * FROM {SCHEMA}.phase0_jobs WHERE workspace_id = %s AND candidate_id = %s",
            (workspace_id, candidate_id),
        ).fetchone()
        assert job_row is not None
        return PublishResult(candidate=candidate, job=_job_record(job_row), replayed=True)
    if candidate.status not in ("pending", "approved"):
        raise CandidateNotPublishable(f"候选 {candidate_id} 状态为 {candidate.status}，不可发布")
    needs_approval = candidate.requires_approval if require_approval is None else require_approval
    if needs_approval:
        if approval is None:
            raise ApprovalRequired("候选需要审批，未提供审批引用")
        if approval.candidate_id != candidate_id:
            raise ApprovalMismatch("审批与候选不匹配")
        moment = datetime.now(UTC)
        if approval.status != "approved":
            raise ApprovalMismatch(f"审批状态为 {approval.status}，不可执行")
        if approval.expires_at <= moment:
            raise ApprovalExpired("审批已过期，拒绝发布")
    job_id = uuid.uuid4()
    dedupe_key = f"ingest:{candidate.dedupe_key}"
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_jobs (workspace_id, run_id, job_id, candidate_id, kind,"
        " status, dedupe_key) VALUES (%s, %s, %s, %s, 'ingest', 'pending', %s)"
        " ON CONFLICT (workspace_id, dedupe_key) DO NOTHING",
        (workspace_id, run_id, job_id, candidate_id, dedupe_key),
    )
    job_row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_jobs WHERE workspace_id = %s AND dedupe_key = %s",
        (workspace_id, dedupe_key),
    ).fetchone()
    assert job_row is not None
    moment = datetime.now(UTC)
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_candidates SET status = 'published', published_at = %s,"
        " job_id = %s, approval_id = COALESCE(%s, approval_id), row_version = row_version + 1"
        " WHERE workspace_id = %s AND candidate_id = %s AND status IN ('pending', 'approved')"
        " RETURNING *",
        (moment, job_row["job_id"], approval.approval_id if approval else None,
         workspace_id, candidate_id),
    ).fetchone()
    if row is None:
        raise CandidateNotPublishable("候选发布 CAS 失败（并发发布会话已抢先）")
    if approval is not None:
        conn.execute(
            f"UPDATE {SCHEMA}.phase0_approvals SET status = 'consumed' "
            "WHERE workspace_id = %s AND approval_id = %s AND status = 'approved'",
            (workspace_id, approval.approval_id),
        )
    append_event(conn, workspace_id=workspace_id, run_id=run_id, event_type="indexed",
                 payload={"candidate_id": str(candidate_id), "job_id": str(job_row["job_id"]),
                          "content_sha256": candidate.content_sha256})
    return PublishResult(candidate=_candidate_record(row), job=_job_record(job_row), replayed=False)


# ---------------------------------------------------------------------------
# 终态（一次事务）
# ---------------------------------------------------------------------------


def finalize_run(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    answer_kind: str,
    content: str,
    generation: int = 1,
) -> FinalizeOutcome:
    """一次事务写：run→succeeded、唯一 ``completed`` 事件、result_hash。

    已有 terminal 时：同内容 → 复放；不同内容 → ``RUN_ALREADY_TERMINAL``。
    """

    if answer_kind not in ANSWER_KINDS:
        raise ResearchError(f"未知 answer_kind: {answer_kind}", code="ANSWER_KIND_INVALID")
    run = lock_run(conn, workspace_id=workspace_id, run_id=run_id)
    digest = result_hash(run_id=run_id, content=content)
    if run.status in TERMINAL_RUN_STATUSES:
        if run.status == "succeeded" and run.result_hash == digest:
            return FinalizeOutcome(run=run, answer_kind=answer_kind, replayed=True, event_id=None)
        raise RunAlreadyTerminal(f"run {run_id} 已终态（{run.status}）")
    if run.generation != generation:
        raise StaleGeneration(f"run generation={run.generation}，拒绝旧 generation {generation}")
    event_id = append_event(
        conn, workspace_id=workspace_id, run_id=run_id, event_type="completed",
        generation=generation,
        payload={"answer_kind": answer_kind, "result_hash": digest, "content_sha256":
                 hashlib.sha256(content.encode("utf-8")).hexdigest()},
    )
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_runs SET status = 'succeeded', answer_kind = %s,"
        " result_hash = %s, terminal_at = now(), row_version = row_version + 1, updated_at = now()"
        " WHERE workspace_id = %s AND run_id = %s RETURNING *",
        (answer_kind, digest, workspace_id, run_id),
    ).fetchone()
    assert row is not None
    return FinalizeOutcome(run=_run_state(row), answer_kind=answer_kind, replayed=False,
                           event_id=event_id)


def fail_run(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    error_code: str,
    detail: str | None = None,
    generation: int = 1,
) -> FinalizeOutcome:
    run = lock_run(conn, workspace_id=workspace_id, run_id=run_id)
    if run.status in TERMINAL_RUN_STATUSES:
        return FinalizeOutcome(run=run, answer_kind=None, replayed=True, event_id=None)
    event_id = append_event(
        conn, workspace_id=workspace_id, run_id=run_id, event_type="failed", generation=generation,
        payload={"error_code": error_code[:64], "detail": (detail or "")[:MAX_DETAIL]},
    )
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_runs SET status = 'failed', error_code = %s, terminal_at = now(),"
        " row_version = row_version + 1, updated_at = now()"
        " WHERE workspace_id = %s AND run_id = %s RETURNING *",
        (error_code[:64], workspace_id, run_id),
    ).fetchone()
    assert row is not None
    return FinalizeOutcome(run=_run_state(row), answer_kind=None, replayed=False, event_id=event_id)


def cancel_run(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    reason: str = "user_cancelled",
    generation: int = 1,
) -> FinalizeOutcome:
    run = lock_run(conn, workspace_id=workspace_id, run_id=run_id)
    if run.status in TERMINAL_RUN_STATUSES:
        return FinalizeOutcome(run=run, answer_kind=None, replayed=True, event_id=None)
    event_id = append_event(
        conn, workspace_id=workspace_id, run_id=run_id, event_type="cancelled",
        generation=generation, payload={"reason": reason[:MAX_DETAIL]},
    )
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_runs SET status = 'cancelled', error_code = 'RUN_CANCELLED',"
        " terminal_at = now(), row_version = row_version + 1, updated_at = now()"
        " WHERE workspace_id = %s AND run_id = %s RETURNING *",
        (workspace_id, run_id),
    ).fetchone()
    assert row is not None
    return FinalizeOutcome(run=_run_state(row), answer_kind=None, replayed=False, event_id=event_id)


def count_events(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, run_id: uuid.UUID,
    event_type: str | None = None,
) -> int:
    if event_type is None:
        row = conn.execute(
            f"SELECT count(*) AS n FROM {SCHEMA}.phase0_events"
            " WHERE workspace_id = %s AND run_id = %s",
            (workspace_id, run_id),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT count(*) AS n FROM {SCHEMA}.phase0_events"
            " WHERE workspace_id = %s AND run_id = %s"
            " AND event_type = %s", (workspace_id, run_id, event_type),
        ).fetchone()
    assert row is not None
    return int(row["n"])


def list_events(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[EventRecord, ...]:
    rows = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_events WHERE workspace_id = %s AND run_id = %s "
        "ORDER BY event_seq", (workspace_id, run_id),
    ).fetchall()
    return tuple(_event_record(row) for row in rows)


def list_sources(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[SourceRecord, ...]:
    rows = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_sources WHERE workspace_id = %s AND run_id = %s "
        "ORDER BY fetched_at, source_id", (workspace_id, run_id),
    ).fetchall()
    return tuple(_source_record(row) for row in rows)


def list_candidates(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[CandidateRecord, ...]:
    rows = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_candidates WHERE workspace_id = %s AND run_id = %s "
        "ORDER BY created_at, candidate_id", (workspace_id, run_id),
    ).fetchall()
    return tuple(_candidate_record(row) for row in rows)


def list_citations(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[CitationRecord, ...]:
    rows = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_citations WHERE workspace_id = %s AND run_id = %s "
        "ORDER BY candidate_id, ordinal", (workspace_id, run_id),
    ).fetchall()
    return tuple(_citation_record(row) for row in rows)


def list_jobs(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[JobRecord, ...]:
    rows = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_jobs WHERE workspace_id = %s AND run_id = %s "
        "ORDER BY created_at, job_id", (workspace_id, run_id),
    ).fetchall()
    return tuple(_job_record(row) for row in rows)


def read_approval(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, approval_id: uuid.UUID
) -> ApprovalRecord:
    return _lock_approval(conn, workspace_id=workspace_id, approval_id=approval_id)


def fail_job(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, job_id: uuid.UUID, error_code: str
) -> JobRecord:
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_jobs SET status = 'failed', error_code = %s,"
        " row_version = row_version + 1, updated_at = now()"
        " WHERE workspace_id = %s AND job_id = %s AND status IN ('pending', 'running') RETURNING *",
        (error_code[:64], workspace_id, job_id),
    ).fetchone()
    if row is None:
        raise ResearchError(f"job {job_id} 不可失败（已终态）", code="JOB_TERMINAL")
    return _job_record(row)


# ---------------------------------------------------------------------------
# 数据类 / 行映射
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunState:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    goal: str
    status: str
    revision: int
    plan_hash: str | None
    step: int
    generation: int
    budget_mode: str
    answer_kind: str | None
    result_hash: str | None
    error_code: str | None
    terminal_at: datetime | None
    row_version: int

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "run_id": str(self.run_id),
            "status": self.status,
            "revision": self.revision,
            "plan_hash": self.plan_hash,
            "step": self.step,
            "generation": self.generation,
            "answer_kind": self.answer_kind,
            "result_hash": self.result_hash,
            "error_code": self.error_code,
            "terminal_at": self.terminal_at.isoformat() if self.terminal_at else None,
            "row_version": self.row_version,
        }


@dataclass(frozen=True)
class EventRecord:
    event_seq: int
    event_id: uuid.UUID
    event_type: str
    generation: int
    payload: Mapping[str, Any]
    occurred_at: datetime

    def envelope(self, *, run_id: uuid.UUID, workspace_id: uuid.UUID) -> dict[str, Any]:
        return {
            "schema_version": ep.EVENT_SCHEMA_VERSION,
            "event_id": str(self.event_id),
            "event_seq": self.event_seq,
            "workspace_id": str(workspace_id),
            "run_id": str(run_id),
            "event_type": self.event_type,
            "generation": self.generation,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class SourceRecord:
    source_id: uuid.UUID
    task_id: str
    channel: str
    url: str
    license: str
    body_sha256: str
    fetched_at: datetime
    snapshot: Mapping[str, Any]


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: uuid.UUID
    source_id: uuid.UUID
    quote: str
    locator: str
    quote_sha256: str


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: uuid.UUID
    run_id: uuid.UUID
    task_id: str
    dedupe_key: str
    content_sha256: str
    title: str
    body: str
    requires_approval: bool
    status: str
    approval_id: uuid.UUID | None
    published_at: datetime | None
    job_id: uuid.UUID | None
    rejection_reason: str | None
    row_version: int

    @property
    def published(self) -> bool:
        return self.status == "published"


@dataclass(frozen=True)
class CitationRecord:
    candidate_id: uuid.UUID
    evidence_id: uuid.UUID
    ordinal: int


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: uuid.UUID
    run_id: uuid.UUID
    candidate_id: uuid.UUID
    intent_key: str
    status: str
    approver_principal_id: uuid.UUID | None
    reason: str | None
    expires_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True)
class JobRecord:
    job_id: uuid.UUID
    candidate_id: uuid.UUID
    kind: str
    status: str
    dedupe_key: str
    error_code: str | None


@dataclass(frozen=True)
class PublishResult:
    candidate: CandidateRecord
    job: JobRecord
    replayed: bool


@dataclass(frozen=True)
class FinalizeOutcome:
    run: RunState
    answer_kind: str | None
    replayed: bool
    event_id: uuid.UUID | None


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    kind: str
    channel: str | None
    status: str
    fact: Mapping[str, Any] = field(default_factory=dict)


def _run_state(row: Mapping[str, Any]) -> RunState:
    return RunState(
        workspace_id=row["workspace_id"], run_id=row["run_id"], goal=row["goal"],
        status=row["status"], revision=int(row["revision"]), plan_hash=row["plan_hash"],
        step=int(row["step"]), generation=int(row["generation"]), budget_mode=row["budget_mode"],
        answer_kind=row["answer_kind"], result_hash=row["result_hash"],
        error_code=row["error_code"], terminal_at=row["terminal_at"],
        row_version=int(row["row_version"]),
    )


def _event_record(row: Mapping[str, Any]) -> EventRecord:
    return EventRecord(
        event_seq=int(row["event_seq"]), event_id=row["event_id"],
        event_type=row["event_type"], generation=int(row["generation"]),
        payload=row["payload"], occurred_at=row["occurred_at"],
    )


def _source_record(row: Mapping[str, Any]) -> SourceRecord:
    return SourceRecord(
        source_id=row["source_id"], task_id=row["task_id"], channel=row["channel"],
        url=row["url"], license=row["license"], body_sha256=row["body_sha256"],
        fetched_at=row["fetched_at"], snapshot=row["snapshot"],
    )


def _evidence_record(row: Mapping[str, Any]) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=row["evidence_id"], source_id=row["source_id"], quote=row["quote"],
        locator=row["locator"], quote_sha256=row["quote_sha256"],
    )


def _candidate_record(row: Mapping[str, Any]) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=row["candidate_id"], run_id=row["run_id"], task_id=row["task_id"],
        dedupe_key=row["dedupe_key"], content_sha256=row["content_sha256"],
        title=row["title"], body=row["body"], requires_approval=bool(row["requires_approval"]),
        status=row["status"], approval_id=row["approval_id"], published_at=row["published_at"],
        job_id=row["job_id"], rejection_reason=row["rejection_reason"],
        row_version=int(row["row_version"]),
    )


def _citation_record(row: Mapping[str, Any]) -> CitationRecord:
    return CitationRecord(
        candidate_id=row["candidate_id"], evidence_id=row["evidence_id"],
        ordinal=int(row["ordinal"]),
    )


def _approval_record(row: Mapping[str, Any]) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row["approval_id"], run_id=row["run_id"],
        candidate_id=row["candidate_id"], intent_key=row["intent_key"], status=row["status"],
        approver_principal_id=row["approver_principal_id"], reason=row["reason"],
        expires_at=row["expires_at"], decided_at=row["decided_at"],
    )


def _job_record(row: Mapping[str, Any]) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"], candidate_id=row["candidate_id"], kind=row["kind"],
        status=row["status"], dedupe_key=row["dedupe_key"], error_code=row["error_code"],
    )


def _lock_candidate(
    conn: psycopg.Connection[Any], *, workspace_id: uuid.UUID, candidate_id: uuid.UUID
) -> CandidateRecord:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_candidates WHERE workspace_id = %s AND candidate_id = %s"
        " FOR UPDATE",
        (workspace_id, candidate_id),
    ).fetchone()
    if row is None:
        raise ResearchError(f"候选 {candidate_id} 不存在", code="CANDIDATE_NOT_FOUND")
    return _candidate_record(row)
