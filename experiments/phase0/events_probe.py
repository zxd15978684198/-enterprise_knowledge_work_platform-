"""P0-T26 持久层：Run / Message / Citation / EventStore（Message + Run + terminal 同一事务）。

契约依据
--------
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T26：授权→改写/检索/不足拒答→生成→引用校验→
  **Message + Run + terminal 事件一次事务提交**；token 先持久、generation reset。
* ``docs/tdd/07_Data_API_Security.md`` §6：``EventEnvelope`` 字段、允许的 ``event_type``、
  token payload 的 ``message_id/generation/offset/text``、completed 只带最终 message/result refs；
  每 run 最多一个 terminal（completed/failed/cancelled 互斥）；cursor 绑定 workspace/principal/
  run/event_seq 与 expiry，过期→``EVENT_CURSOR_EXPIRED``；P1 续传未交付，P0 请求续传返回
  ``CAPABILITY_UNSUPPORTED``；F10：部分 token 已发后 Message 保存失败不得出现 completed。
* ``docs/tdd/04_LangGraph_Workflows.md`` §3/§7：Graph 只返回执行结果；finalizer 幂等；
  DB 与 checkpointer 不同事务域，不保证恰好一次节点执行。

**限域（写入证据与报告）**：schema 固定为独占 ``p0_t26``（W8 资源槽），不是 001 §10 的
``p0_t03`` 增量；不建 RLS 角色，作用域由查询条件与主体授权表在 SQL 内过滤；真实 chat 模型
NOT_RUN（由 ``rag_probe.ModelPort`` 替身驱动结构/事务/引用校验）。

本模块只负责“写模型 + 事件流 + 终态事务”，检索/图/引用策略在 ``rag_probe``。
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

SCHEMA: Final = "p0_t26"
MIGRATION_ID: Final = "t26_rag_events_inline"
MIGRATION_FILENAME: Final = "inline:events_probe.py::MIGRATION_SQL"
EVENT_SCHEMA_VERSION: Final = 1
TERMINAL_EVENT_TYPES: Final = ("completed", "failed", "cancelled")
EVENT_TYPES: Final = (
    "run_started",
    "step_progress",
    "source_found",
    "candidate_ready",
    "approval_required",
    "token",
    "generation_reset",
    "indexed",
    "enrichment_updated",
    "completed",
    "failed",
    "cancelled",
)
RUN_STATUSES: Final = ("queued", "running", "completed", "failed", "cancelled")
TERMINAL_RUN_STATUSES: Final = ("completed", "failed", "cancelled")
MESSAGE_STATUSES: Final = ("partial", "final", "incomplete")
ANSWER_KINDS: Final = ("grounded", "grounded_partial", "insufficient_evidence", "refused")
REJECTION_REASONS: Final = (
    "unknown_chunk",
    "not_in_evidence",
    "cross_workspace",
    "stale_version",
    "revoked",
    "duplicate",
)
CURSOR_TTL_SECONDS: Final = 3600
MAX_PAYLOAD_TEXT: Final = 4000
MAX_DETAIL: Final = 512

_HEX64: Final = "0123456789abcdef"

MIGRATION_SQL: Final = """
CREATE SCHEMA p0_t26;

COMMENT ON SCHEMA p0_t26 IS
    'P0-T26 独占实验槽：RAG Graph 的 Run/Message/Citation/EventStore 与最小检索语料镜像；'
    '生产共享 schema 见 001/003/004/008 与 TDD07 §3。';

CREATE TABLE p0_t26.phase0_schema_migrations (
    migration_id text PRIMARY KEY,
    filename     text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

-- ---------------------------------------------------------------- 工作区/主体
CREATE TABLE p0_t26.phase0_workspaces (
    workspace_id uuid PRIMARY KEY,
    slug         text NOT NULL UNIQUE CHECK (length(slug) BETWEEN 1 AND 64),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE p0_t26.phase0_principals (
    workspace_id        uuid NOT NULL,
    principal_id        uuid NOT NULL,
    role                text NOT NULL CHECK (role IN ('owner', 'member')),
    status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    membership_revision bigint NOT NULL DEFAULT 1 CHECK (membership_revision >= 1),
    PRIMARY KEY (workspace_id, principal_id),
    CONSTRAINT phase0_t26_principals_workspace_fk
        FOREIGN KEY (workspace_id) REFERENCES p0_t26.phase0_workspaces (workspace_id)
        ON DELETE CASCADE
);

CREATE TABLE p0_t26.phase0_knowledge_bases (
    workspace_id uuid NOT NULL,
    kb_id        uuid NOT NULL,
    name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    PRIMARY KEY (workspace_id, kb_id),
    CONSTRAINT phase0_t26_kb_workspace_fk
        FOREIGN KEY (workspace_id) REFERENCES p0_t26.phase0_workspaces (workspace_id)
        ON DELETE CASCADE,
    CONSTRAINT phase0_t26_kb_name_key UNIQUE (workspace_id, name)
);

CREATE TABLE p0_t26.phase0_kb_grants (
    workspace_id uuid NOT NULL,
    kb_id        uuid NOT NULL,
    principal_id uuid NOT NULL,
    can_read     boolean NOT NULL DEFAULT true,
    granted_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, kb_id, principal_id),
    CONSTRAINT phase0_t26_grant_kb_fk
        FOREIGN KEY (workspace_id, kb_id)
        REFERENCES p0_t26.phase0_knowledge_bases (workspace_id, kb_id) ON DELETE CASCADE,
    CONSTRAINT phase0_t26_grant_principal_fk
        FOREIGN KEY (workspace_id, principal_id)
        REFERENCES p0_t26.phase0_principals (workspace_id, principal_id) ON DELETE CASCADE
);

-- ------------------------------------------------- 最小检索语料（镜像 T12/T13）
CREATE TABLE p0_t26.phase0_documents (
    workspace_id      uuid NOT NULL,
    kb_id             uuid NOT NULL,
    document_id       uuid NOT NULL,
    source_id         uuid NOT NULL,
    source_version_id uuid NOT NULL,
    active_version    bigint CHECK (active_version IS NULL OR active_version >= 1),
    row_version       bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, document_id),
    CONSTRAINT phase0_t26_documents_kb_fk
        FOREIGN KEY (workspace_id, kb_id)
        REFERENCES p0_t26.phase0_knowledge_bases (workspace_id, kb_id) ON DELETE RESTRICT
);

CREATE TABLE p0_t26.phase0_document_versions (
    workspace_id       uuid NOT NULL,
    document_id        uuid NOT NULL,
    index_version      bigint NOT NULL CHECK (index_version >= 1),
    status             text NOT NULL
                       CHECK (status IN ('staging', 'ready', 'active', 'retired', 'failed')),
    winning_attempt_id uuid,
    chunk_count        integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    PRIMARY KEY (workspace_id, document_id, index_version),
    CONSTRAINT phase0_t26_versions_document_fk
        FOREIGN KEY (workspace_id, document_id)
        REFERENCES p0_t26.phase0_documents (workspace_id, document_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_t26_versions_winner_check CHECK (
        status NOT IN ('active', 'retired') OR winning_attempt_id IS NOT NULL
    )
);

CREATE UNIQUE INDEX phase0_t26_one_active_version
    ON p0_t26.phase0_document_versions (workspace_id, document_id)
    WHERE status = 'active';

CREATE TABLE p0_t26.phase0_chunks (
    workspace_id      uuid NOT NULL,
    chunk_id          uuid NOT NULL,
    document_id       uuid NOT NULL,
    index_version     bigint NOT NULL CHECK (index_version >= 1),
    attempt_id        uuid NOT NULL,
    chunk_ordinal     integer NOT NULL CHECK (chunk_ordinal >= 0),
    kind              text NOT NULL CHECK (kind IN ('title', 'body')),
    text              text NOT NULL CHECK (length(text) > 0),
    embedding         double precision[] NOT NULL,
    embedding_dim     integer NOT NULL CHECK (embedding_dim BETWEEN 1 AND 4096),
    embedding_profile text NOT NULL CHECK (length(embedding_profile) BETWEEN 1 AND 64),
    source_id         uuid NOT NULL,
    source_version_id uuid NOT NULL,
    PRIMARY KEY (workspace_id, chunk_id),
    CONSTRAINT phase0_t26_chunks_version_fk
        FOREIGN KEY (workspace_id, document_id, index_version)
        REFERENCES p0_t26.phase0_document_versions (workspace_id, document_id, index_version)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_t26_chunks_ordinal_key
        UNIQUE (workspace_id, document_id, index_version, chunk_ordinal),
    CONSTRAINT phase0_t26_chunks_embedding_dim_check
        CHECK (cardinality(embedding) = embedding_dim)
);

-- ------------------------------------------------------------------- 会话/Run
CREATE TABLE p0_t26.phase0_conversations (
    workspace_id    uuid NOT NULL,
    conversation_id uuid NOT NULL,
    principal_id    uuid NOT NULL,
    title           text CHECK (title IS NULL OR length(title) <= 200),
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, conversation_id),
    CONSTRAINT phase0_t26_conversations_principal_fk
        FOREIGN KEY (workspace_id, principal_id)
        REFERENCES p0_t26.phase0_principals (workspace_id, principal_id) ON DELETE RESTRICT
);

CREATE TABLE p0_t26.phase0_runs (
    workspace_id    uuid NOT NULL,
    run_id          uuid NOT NULL,
    conversation_id uuid NOT NULL,
    principal_id    uuid NOT NULL,
    graph_type      text NOT NULL DEFAULT 'rag' CHECK (graph_type IN ('rag')),
    graph_version   text NOT NULL DEFAULT 'rag-v1'
                    CHECK (length(graph_version) BETWEEN 1 AND 32),
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    generation      bigint NOT NULL DEFAULT 1 CHECK (generation >= 1),
    answer_kind     text CHECK (
                        answer_kind IS NULL
                        OR answer_kind IN ('grounded', 'grounded_partial',
                                           'insufficient_evidence', 'refused')
                    ),
    result_hash     text CHECK (result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'),
    terminal_at     timestamptz,
    row_version     bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id),
    CONSTRAINT phase0_t26_runs_conversation_fk
        FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES p0_t26.phase0_conversations (workspace_id, conversation_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_t26_runs_principal_fk
        FOREIGN KEY (workspace_id, principal_id)
        REFERENCES p0_t26.phase0_principals (workspace_id, principal_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_t26_runs_terminal_shape_check CHECK (
        (status IN ('completed', 'failed', 'cancelled')) = (terminal_at IS NOT NULL)
    ),
    CONSTRAINT phase0_t26_runs_completed_answer_kind_check CHECK (
        status <> 'completed' OR answer_kind IS NOT NULL
    )
);

CREATE TABLE p0_t26.phase0_messages (
    workspace_id uuid NOT NULL,
    message_id   uuid NOT NULL,
    run_id       uuid NOT NULL,
    role         text NOT NULL CHECK (role IN ('user', 'assistant')),
    content      text NOT NULL DEFAULT '',
    status       text NOT NULL DEFAULT 'partial'
                 CHECK (status IN ('partial', 'final', 'incomplete')),
    generation   bigint NOT NULL DEFAULT 1 CHECK (generation >= 1),
    citations    jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(citations) = 'array'),
    result_hash  text CHECK (result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'),
    row_version  bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    created_at   timestamptz NOT NULL DEFAULT now(),
    finalized_at timestamptz,
    PRIMARY KEY (workspace_id, message_id),
    CONSTRAINT phase0_t26_messages_run_fk
        FOREIGN KEY (workspace_id, run_id)
        REFERENCES p0_t26.phase0_runs (workspace_id, run_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_t26_messages_generation_key
        UNIQUE (workspace_id, run_id, role, generation),
    CONSTRAINT phase0_t26_messages_final_shape_check CHECK (
        (status = 'final') = (finalized_at IS NOT NULL)
    ),
    CONSTRAINT phase0_t26_messages_user_final_check CHECK (
        role <> 'user' OR status = 'final'
    )
);

CREATE TABLE p0_t26.phase0_message_citations (
    workspace_id  uuid NOT NULL,
    message_id    uuid NOT NULL,
    ordinal       integer NOT NULL CHECK (ordinal >= 0),
    chunk_id      uuid NOT NULL,
    document_id   uuid NOT NULL,
    index_version bigint NOT NULL CHECK (index_version >= 1),
    rank          integer NOT NULL CHECK (rank >= 1),
    quote_hash    text NOT NULL CHECK (quote_hash ~ '^[0-9a-f]{64}$'),
    verified      boolean NOT NULL DEFAULT true CHECK (verified),
    PRIMARY KEY (workspace_id, message_id, ordinal),
    CONSTRAINT phase0_t26_citations_message_fk
        FOREIGN KEY (workspace_id, message_id)
        REFERENCES p0_t26.phase0_messages (workspace_id, message_id) ON DELETE CASCADE,
    CONSTRAINT phase0_t26_citations_chunk_fk
        FOREIGN KEY (workspace_id, chunk_id)
        REFERENCES p0_t26.phase0_chunks (workspace_id, chunk_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_t26_citations_chunk_key
        UNIQUE (workspace_id, message_id, chunk_id)
);

-- --------------------------------------------------------------- 事件流/游标
CREATE TABLE p0_t26.phase0_run_events (
    workspace_id uuid NOT NULL,
    run_id       uuid NOT NULL,
    event_seq    bigint NOT NULL CHECK (event_seq >= 1),
    event_id     uuid NOT NULL,
    generation   bigint NOT NULL CHECK (generation >= 1),
    event_type   text NOT NULL CHECK (
                     event_type IN ('run_started', 'step_progress', 'source_found',
                                    'candidate_ready', 'approval_required', 'token',
                                    'generation_reset', 'indexed', 'enrichment_updated',
                                    'completed', 'failed', 'cancelled')
                 ),
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, event_seq),
    CONSTRAINT phase0_t26_events_run_fk
        FOREIGN KEY (workspace_id, run_id)
        REFERENCES p0_t26.phase0_runs (workspace_id, run_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_t26_events_event_id_key UNIQUE (workspace_id, event_id)
);

-- 每 run 最多一个 terminal（completed/failed/cancelled 互斥），DB 层强制。
CREATE UNIQUE INDEX phase0_t26_run_terminal_unique
    ON p0_t26.phase0_run_events (workspace_id, run_id)
    WHERE event_type IN ('completed', 'failed', 'cancelled');

CREATE TABLE p0_t26.phase0_event_counters (
    workspace_id uuid NOT NULL,
    run_id       uuid NOT NULL,
    last_seq     bigint NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    PRIMARY KEY (workspace_id, run_id),
    CONSTRAINT phase0_t26_counters_run_fk
        FOREIGN KEY (workspace_id, run_id)
        REFERENCES p0_t26.phase0_runs (workspace_id, run_id) ON DELETE CASCADE
);

CREATE TABLE p0_t26.phase0_event_cursors (
    workspace_id uuid NOT NULL,
    run_id       uuid NOT NULL,
    cursor_id    uuid NOT NULL,
    principal_id uuid NOT NULL,
    after_seq    bigint NOT NULL CHECK (after_seq >= 0),
    issued_at    timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, run_id, cursor_id),
    CONSTRAINT phase0_t26_cursors_run_fk
        FOREIGN KEY (workspace_id, run_id)
        REFERENCES p0_t26.phase0_runs (workspace_id, run_id) ON DELETE CASCADE,
    CONSTRAINT phase0_t26_cursors_principal_fk
        FOREIGN KEY (workspace_id, principal_id)
        REFERENCES p0_t26.phase0_principals (workspace_id, principal_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_t26_cursors_expiry_check CHECK (expires_at > issued_at)
);

CREATE TABLE p0_t26.phase0_citation_rejections (
    workspace_id  uuid NOT NULL,
    run_id        uuid NOT NULL,
    rejection_seq bigint GENERATED ALWAYS AS IDENTITY,
    reason        text NOT NULL CHECK (
                      reason IN ('unknown_chunk', 'not_in_evidence', 'cross_workspace',
                                 'stale_version', 'revoked', 'duplicate')
                  ),
    chunk_id      uuid,
    detail        text CHECK (detail IS NULL OR length(detail) <= 512),
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, rejection_seq),
    CONSTRAINT phase0_t26_rejections_run_fk
        FOREIGN KEY (workspace_id, run_id)
        REFERENCES p0_t26.phase0_runs (workspace_id, run_id) ON DELETE CASCADE
);

-- --------------------------------------------------------------- 触发器：不变量
CREATE FUNCTION p0_t26.phase0_t26_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '事件/审计只允许追加写，拒绝 %', TG_OP
        USING ERRCODE = 'check_violation';
END
$$;

CREATE TRIGGER phase0_t26_events_append_only
    BEFORE UPDATE OR DELETE ON p0_t26.phase0_run_events
    FOR EACH ROW EXECUTE FUNCTION p0_t26.phase0_t26_reject_mutation();

CREATE TRIGGER phase0_t26_citations_append_only
    BEFORE UPDATE OR DELETE ON p0_t26.phase0_message_citations
    FOR EACH ROW EXECUTE FUNCTION p0_t26.phase0_t26_reject_mutation();

CREATE TRIGGER phase0_t26_rejections_append_only
    BEFORE UPDATE OR DELETE ON p0_t26.phase0_citation_rejections
    FOR EACH ROW EXECUTE FUNCTION p0_t26.phase0_t26_reject_mutation();

CREATE FUNCTION p0_t26.phase0_t26_runs_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id <> OLD.workspace_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.principal_id <> OLD.principal_id
       OR NEW.graph_type <> OLD.graph_type
       OR NEW.graph_version <> OLD.graph_version THEN
        RAISE EXCEPTION 'Run 身份列不可变（run=%）', OLD.run_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'Run row_version 必须严格 +1（旧=% 新=%）',
            OLD.row_version, NEW.row_version USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.generation < OLD.generation THEN
        RAISE EXCEPTION 'Run generation 不得回退（旧=% 新=%）',
            OLD.generation, NEW.generation USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status IN ('completed', 'failed', 'cancelled')
       AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'Run 终态不可再转移（% → %）', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;
    IF NOT (
        (OLD.status = 'queued' AND NEW.status IN ('running', 'completed', 'failed',
                                                  'cancelled'))
        OR (OLD.status = 'running' AND NEW.status IN ('completed', 'failed', 'cancelled'))
    ) THEN
        RAISE EXCEPTION '非法 Run 状态转移 % → %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER phase0_t26_runs_transition
    BEFORE UPDATE ON p0_t26.phase0_runs
    FOR EACH ROW EXECUTE FUNCTION p0_t26.phase0_t26_runs_guard();

CREATE FUNCTION p0_t26.phase0_t26_messages_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id <> OLD.workspace_id
       OR NEW.message_id <> OLD.message_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.role <> OLD.role THEN
        RAISE EXCEPTION 'Message 身份列不可变（message=%）', OLD.message_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'Message row_version 必须严格 +1（旧=% 新=%）',
            OLD.row_version, NEW.row_version USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.generation < OLD.generation THEN
        RAISE EXCEPTION 'Message generation 不得回退（旧=% 新=%）',
            OLD.generation, NEW.generation USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status IN ('final', 'incomplete') THEN
        RAISE EXCEPTION 'Message 终态不可再改写（% / message=%）',
            OLD.status, OLD.message_id USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status <> OLD.status
       AND NOT (OLD.status = 'partial' AND NEW.status IN ('final', 'incomplete')) THEN
        RAISE EXCEPTION '非法 Message 状态转移 % → %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'partial' AND NEW.status = 'partial'
       AND NEW.content IS DISTINCT FROM OLD.content
       AND NEW.generation <> OLD.generation THEN
        RAISE EXCEPTION 'Message 内容只允许在本 generation 内追加（旧=% 新=%）',
            OLD.generation, NEW.generation USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER phase0_t26_messages_guard
    BEFORE UPDATE ON p0_t26.phase0_messages
    FOR EACH ROW EXECUTE FUNCTION p0_t26.phase0_t26_messages_guard();
"""


class EventsProbeError(RuntimeError):
    """持久层错误的基类：``code`` 进入证据与日志，不打印凭据。"""

    code: str = "EVENTS_PROBE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class SchemaAlreadyApplied(EventsProbeError):
    code = "SCHEMA_ALREADY_APPLIED"


class SchemaNotApplied(EventsProbeError):
    code = "SCHEMA_NOT_APPLIED"


class RunNotFound(EventsProbeError):
    code = "RUN_NOT_FOUND"


class MessageNotFound(EventsProbeError):
    code = "MESSAGE_NOT_FOUND"


class StaleGeneration(EventsProbeError):
    code = "STALE_GENERATION"


class RunAlreadyTerminal(EventsProbeError):
    code = "RUN_ALREADY_TERMINAL"

    def __init__(self, message: str, *, terminal_type: str) -> None:
        super().__init__(message)
        self.terminal_type = terminal_type


class IdempotencyConflict(EventsProbeError):
    code = "IDEMPOTENCY_CONFLICT"


class Unauthorized(EventsProbeError):
    code = "UNAUTHORIZED"


class CursorExpired(EventsProbeError):
    code = "EVENT_CURSOR_EXPIRED"

    def __init__(self, message: str, *, run_status: str, result_url: str) -> None:
        super().__init__(message)
        self.run_status = run_status
        self.result_url = result_url


class CursorScopeMismatch(EventsProbeError):
    code = "EVENT_CURSOR_SCOPE_MISMATCH"


class CapabilityUnsupported(EventsProbeError):
    code = "CAPABILITY_UNSUPPORTED"


class EventValidationError(EventsProbeError):
    code = "EVENT_VALIDATION_ERROR"


class TerminalStateError(EventsProbeError):
    code = "TERMINAL_STATE_ERROR"


def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection[Any]:
    """连接实验库（默认事务模式；``row_factory=dict_row``）。"""

    if dsn is None:
        from scope import read_dsn  # 局部导入：避免模块级循环与启动期读 env

        dsn = read_dsn()
    return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)


def schema_sha256(ddl: str = MIGRATION_SQL) -> str:
    return hashlib.sha256(ddl.encode("utf-8")).hexdigest()


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    """删除本任务独占 schema；调用方负责使用 autocommit 连接。"""

    if not conn.autocommit:  # pragma: no cover - 防御性
        raise EventsProbeError("reset_schema 需要 autocommit=True 的连接")
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))


def apply_schema(conn: psycopg.Connection[Any]) -> str:
    """单事务应用 DDL 并写台账；重复应用显式失败（不静默跳过）。"""

    if conn.autocommit:
        raise EventValidationError("apply_schema 需要 autocommit=False 的连接")
    digest = schema_sha256()
    with conn.transaction():
        exists = conn.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (SCHEMA,)
        ).fetchone()
        if exists is None:
            conn.execute(MIGRATION_SQL)
        else:
            ledger = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = 'phase0_schema_migrations'",
                (SCHEMA,),
            ).fetchone()
            if ledger is not None:
                row = conn.execute(
                    f"SELECT sha256 FROM {SCHEMA}.phase0_schema_migrations "
                    "WHERE migration_id = %s",
                    (MIGRATION_ID,),
                ).fetchone()
                if row is not None:
                    raise SchemaAlreadyApplied(
                        f"迁移 {MIGRATION_ID} 已应用（sha256={row['sha256'][:12]}…）"
                    )
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_schema_migrations (migration_id, filename, sha256) "
            "VALUES (%s, %s, %s)",
            (MIGRATION_ID, MIGRATION_FILENAME, digest),
        )
    return digest


def ensure_schema(conn: psycopg.Connection[Any]) -> str:
    """幂等建 schema：返回 ``'applied'`` 或 ``'present'``（供测试/图初始化）。"""

    if conn.autocommit:
        raise EventValidationError("ensure_schema 需要 autocommit=False 的连接")
    exists = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (SCHEMA,)
    ).fetchone()
    conn.commit()
    if exists is None:
        apply_schema(conn)
        conn.commit()
        return "applied"
    row = conn.execute(
        f"SELECT 1 FROM {SCHEMA}.phase0_schema_migrations WHERE migration_id = %s",
        (MIGRATION_ID,),
    ).fetchone()
    conn.commit()
    if row is None:
        raise SchemaNotApplied(f"schema {SCHEMA} 存在但没有 {MIGRATION_ID} 台账")
    return "present"


def _require_transaction(conn: psycopg.Connection[Any]) -> None:
    if conn.autocommit:
        raise EventValidationError("该操作必须在事务内执行（autocommit=False）")


def _require_uuid(value: Any, *, name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise EventValidationError(f"{name} 不是 uuid: {value!r}") from exc


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def result_hash(
    *,
    run_id: uuid.UUID,
    generation: int,
    content: str,
    citations: Sequence[Mapping[str, Any]] = (),
) -> str:
    """最终结果的稳定 hash：同一输入重入得到同一值（幂等复放的判据）。"""

    normalized = [
        {
            "chunk_id": str(item["chunk_id"]),
            "document_id": str(item["document_id"]),
            "index_version": int(item["index_version"]),
            "rank": int(item["rank"]),
        }
        for item in citations
    ]
    return _sha256_text(
        _canonical(
            {
                "run_id": str(run_id),
                "generation": generation,
                "content": content,
                "citations": normalized,
            }
        )
    )


@dataclass(frozen=True, slots=True)
class RunState:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    principal_id: uuid.UUID
    status: str
    generation: int
    answer_kind: str | None
    result_hash: str | None
    terminal_at: datetime | None
    row_version: int

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES


@dataclass(frozen=True, slots=True)
class MessageState:
    workspace_id: uuid.UUID
    message_id: uuid.UUID
    run_id: uuid.UUID
    role: str
    content: str
    status: str
    generation: int
    citations: tuple[Mapping[str, Any], ...]
    result_hash: str | None
    row_version: int
    finalized_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunEvent:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    event_seq: int
    event_id: uuid.UUID
    generation: int
    event_type: str
    payload: Mapping[str, Any]
    occurred_at: datetime

    def envelope(self) -> dict[str, Any]:
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": str(self.event_id),
            "event_seq": self.event_seq,
            "workspace_id": str(self.workspace_id),
            "run_id": str(self.run_id),
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class CitationSpec:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    index_version: int
    rank: int
    quote_hash: str


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    run: RunState
    assistant_message: MessageState
    terminal_event: RunEvent
    citations_written: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class CursorState:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    cursor_id: uuid.UUID
    principal_id: uuid.UUID
    after_seq: int
    expires_at: datetime


def _run_from_row(row: Mapping[str, Any]) -> RunState:
    return RunState(
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        conversation_id=row["conversation_id"],
        principal_id=row["principal_id"],
        status=row["status"],
        generation=int(row["generation"]),
        answer_kind=row["answer_kind"],
        result_hash=row["result_hash"],
        terminal_at=row["terminal_at"],
        row_version=int(row["row_version"]),
    )


def _message_from_row(row: Mapping[str, Any]) -> MessageState:
    citations = row["citations"]
    if isinstance(citations, str):  # pragma: no cover - jsonb 已由 psycopg 解码
        citations = json.loads(citations)
    return MessageState(
        workspace_id=row["workspace_id"],
        message_id=row["message_id"],
        run_id=row["run_id"],
        role=row["role"],
        content=row["content"],
        status=row["status"],
        generation=int(row["generation"]),
        citations=tuple(citations or ()),
        result_hash=row["result_hash"],
        row_version=int(row["row_version"]),
        finalized_at=row["finalized_at"],
    )


def _event_from_row(row: Mapping[str, Any]) -> RunEvent:
    payload = row["payload"]
    if isinstance(payload, str):  # pragma: no cover
        payload = json.loads(payload)
    return RunEvent(
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        event_seq=int(row["event_seq"]),
        event_id=row["event_id"],
        generation=int(row["generation"]),
        event_type=row["event_type"],
        payload=payload or {},
        occurred_at=row["occurred_at"],
    )


# ---------------------------------------------------------------------------
# 读取：Run / Message / 事件
# ---------------------------------------------------------------------------
def read_run(conn: psycopg.Connection[Any], workspace_id: uuid.UUID, run_id: uuid.UUID) -> RunState:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_runs WHERE workspace_id = %s AND run_id = %s",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        raise RunNotFound(f"run 不存在: {run_id}")
    return _run_from_row(row)


def lock_run(
    conn: psycopg.Connection[Any], workspace_id: uuid.UUID, run_id: uuid.UUID
) -> RunState:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_runs WHERE workspace_id = %s AND run_id = %s "
        "FOR UPDATE",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        raise RunNotFound(f"run 不存在: {run_id}")
    return _run_from_row(row)


def read_message(
    conn: psycopg.Connection[Any], workspace_id: uuid.UUID, message_id: uuid.UUID
) -> MessageState:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_messages WHERE workspace_id = %s AND message_id = %s",
        (workspace_id, message_id),
    ).fetchone()
    if row is None:
        raise MessageNotFound(f"message 不存在: {message_id}")
    return _message_from_row(row)


def lock_message(
    conn: psycopg.Connection[Any], workspace_id: uuid.UUID, message_id: uuid.UUID
) -> MessageState:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_messages WHERE workspace_id = %s AND message_id = %s "
        "FOR UPDATE",
        (workspace_id, message_id),
    ).fetchone()
    if row is None:
        raise MessageNotFound(f"message 不存在: {message_id}")
    return _message_from_row(row)


def list_messages(
    conn: psycopg.Connection[Any], workspace_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[MessageState, ...]:
    rows = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_messages WHERE workspace_id = %s AND run_id = %s "
        "ORDER BY created_at, message_id",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(_message_from_row(row) for row in rows)


def list_events(
    conn: psycopg.Connection[Any],
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    after_seq: int = 0,
    limit: int = 1000,
    event_type: str | None = None,
) -> tuple[RunEvent, ...]:
    if limit < 1 or limit > 10000:
        raise EventValidationError("limit 必须在 [1, 10000]")
    clause = "AND event_type = %s" if event_type is not None else ""
    params: list[Any] = [workspace_id, run_id, after_seq]
    if event_type is not None:
        params.append(event_type)
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_run_events "
        f"WHERE workspace_id = %s AND run_id = %s AND event_seq > %s {clause} "
        "ORDER BY event_seq LIMIT %s",
        params,
    ).fetchall()
    return tuple(_event_from_row(row) for row in rows)


def read_terminal(
    conn: psycopg.Connection[Any], workspace_id: uuid.UUID, run_id: uuid.UUID
) -> RunEvent | None:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_run_events "
        "WHERE workspace_id = %s AND run_id = %s "
        "AND event_type IN ('completed', 'failed', 'cancelled')",
        (workspace_id, run_id),
    ).fetchone()
    return None if row is None else _event_from_row(row)


def count_events(
    conn: psycopg.Connection[Any],
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    event_type: str | None = None,
) -> int:
    clause = "AND event_type = %s" if event_type is not None else ""
    params: list[Any] = [workspace_id, run_id]
    if event_type is not None:
        params.append(event_type)
    row = conn.execute(
        f"SELECT count(*) AS total FROM {SCHEMA}.phase0_run_events "
        f"WHERE workspace_id = %s AND run_id = %s {clause}",
        params,
    ).fetchone()
    return int(row["total"]) if row else 0


def count_terminal(
    conn: psycopg.Connection[Any], workspace_id: uuid.UUID, run_id: uuid.UUID
) -> int:
    row = conn.execute(
        f"SELECT count(*) AS total FROM {SCHEMA}.phase0_run_events "
        "WHERE workspace_id = %s AND run_id = %s "
        "AND event_type IN ('completed', 'failed', 'cancelled')",
        (workspace_id, run_id),
    ).fetchone()
    return int(row["total"]) if row else 0


# ---------------------------------------------------------------------------
# 写入：事件序号 + 追加
# ---------------------------------------------------------------------------
def allocate_event_seq(
    conn: psycopg.Connection[Any], workspace_id: uuid.UUID, run_id: uuid.UUID
) -> int:
    """每 run 一个提交顺序：锁计数器行再 +1（TDD07 §6）。"""

    row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_event_counters (workspace_id, run_id, last_seq) "
        "VALUES (%s, %s, 1) "
        "ON CONFLICT (workspace_id, run_id) "
        "DO UPDATE SET last_seq = phase0_event_counters.last_seq + 1 "
        "RETURNING last_seq",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:  # pragma: no cover - RETURNING 恒有行
        raise EventsProbeError("事件序号分配失败")
    return int(row["last_seq"])


def append_event(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    generation: int = 1,
) -> RunEvent:
    _require_transaction(conn)
    if event_type not in EVENT_TYPES:
        raise EventValidationError(f"未知 event_type: {event_type!r}")
    if generation < 1:
        raise EventValidationError("generation 必须 >= 1")
    body = dict(payload or {})
    if len(_canonical(body)) > MAX_PAYLOAD_TEXT * 4:  # 粗粒度上限，防止事件塞大对象
        raise EventValidationError("事件 payload 过大（只存引用与摘要）")
    seq = allocate_event_seq(conn, workspace_id, run_id)
    row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_run_events "
        "(workspace_id, run_id, event_seq, event_id, generation, event_type, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING *",
        (
            workspace_id,
            run_id,
            seq,
            uuid.uuid4(),
            generation,
            event_type,
            _canonical(body),
        ),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise EventsProbeError("事件写入失败")
    return _event_from_row(row)


# ---------------------------------------------------------------------------
# 创建 run / 会话 / 消息
# ---------------------------------------------------------------------------
def ensure_conversation(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    conversation_id: uuid.UUID,
    title: str | None = None,
) -> None:
    row = conn.execute(
        f"SELECT principal_id FROM {SCHEMA}.phase0_conversations "
        "WHERE workspace_id = %s AND conversation_id = %s",
        (workspace_id, conversation_id),
    ).fetchone()
    if row is not None:
        if row["principal_id"] != principal_id:
            raise Unauthorized("会话属于其他主体，拒绝复用")
        return
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_conversations "
        "(workspace_id, conversation_id, principal_id, title) VALUES (%s, %s, %s, %s)",
        (workspace_id, conversation_id, principal_id, title),
    )


def require_active_membership(
    conn: psycopg.Connection[Any], workspace_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    row = conn.execute(
        f"SELECT status FROM {SCHEMA}.phase0_principals "
        "WHERE workspace_id = %s AND principal_id = %s",
        (workspace_id, principal_id),
    ).fetchone()
    if row is None or row["status"] != "active":
        raise Unauthorized(f"主体 {principal_id} 在 workspace {workspace_id} 无有效成员资格")


def create_run(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    conversation_id: uuid.UUID,
    query: str,
    run_id: uuid.UUID | None = None,
    user_message_id: uuid.UUID | None = None,
    generation: int = 1,
) -> tuple[RunState, MessageState]:
    """登记 run 与用户消息（同一事务）；``run_started`` 事件同事务。"""

    _require_transaction(conn)
    if not query or not query.strip():
        raise EventValidationError("query 必须非空")
    require_active_membership(conn, workspace_id, principal_id)
    ensure_conversation(
        conn,
        workspace_id=workspace_id,
        principal_id=principal_id,
        conversation_id=conversation_id,
    )
    run_id = run_id or uuid.uuid4()
    user_message_id = user_message_id or uuid.uuid4()
    row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_runs "
        "(workspace_id, run_id, conversation_id, principal_id, status, generation) "
        "VALUES (%s, %s, %s, %s, 'running', %s) RETURNING *",
        (workspace_id, run_id, conversation_id, principal_id, generation),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise EventsProbeError("run 写入失败")
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_messages "
        "(workspace_id, message_id, run_id, role, content, status, generation, finalized_at) "
        "VALUES (%s, %s, %s, 'user', %s, 'final', %s, now())",
        (workspace_id, user_message_id, run_id, query, generation),
    )
    append_event(
        conn,
        workspace_id=workspace_id,
        run_id=run_id,
        event_type="run_started",
        payload={
            "message_id": str(user_message_id),
            "query_hash": _sha256_text(query),
            "query_chars": len(query),
        },
        generation=generation,
    )
    return read_run(conn, workspace_id, run_id), read_message(conn, workspace_id, user_message_id)


def ensure_assistant_message(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    generation: int,
    message_id: uuid.UUID | None = None,
) -> MessageState:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_messages "
        "WHERE workspace_id = %s AND run_id = %s AND role = 'assistant' AND generation = %s",
        (workspace_id, run_id, generation),
    ).fetchone()
    if row is not None:
        return _message_from_row(row)
    message_id = message_id or uuid.uuid4()
    row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_messages "
        "(workspace_id, message_id, run_id, role, content, status, generation) "
        "VALUES (%s, %s, %s, 'assistant', '', 'partial', %s) RETURNING *",
        (workspace_id, message_id, run_id, generation),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise EventsProbeError("assistant message 写入失败")
    return _message_from_row(row)


# ---------------------------------------------------------------------------
# token 先持久（部分流 / generation fence）
# ---------------------------------------------------------------------------
def persist_tokens(
    conn: psycopg.Connection[Any],
    *,
    run: RunState,
    message: MessageState,
    generation: int,
    texts: Sequence[str],
) -> MessageState:
    """把一批 token 追加进 message 草稿并写 ``token`` 事件（调用方提交）。

    只在本 generation 内允许；run 已终态或 generation 漂移都显式失败，
    保证旧 generation 的 token 不会拼进新回答（TDD07 §6 F10）。
    """

    _require_transaction(conn)
    if not texts:
        raise EventValidationError("texts 不能为空")
    if generation != message.generation:
        raise StaleGeneration(
            f"消息 generation={message.generation} 与调用方 generation={generation} 不一致"
        )
    current_run = lock_run(conn, run.workspace_id, run.run_id)
    if current_run.terminal:
        raise RunAlreadyTerminal(
            f"run 已终态（{current_run.status}），拒绝继续追加 token",
            terminal_type=current_run.status,
        )
    if current_run.generation != generation:
        raise StaleGeneration(
            f"run generation={current_run.generation} 已取代 {generation}（generation reset）"
        )
    locked = lock_message(conn, message.workspace_id, message.message_id)
    if locked.status != "partial":
        raise TerminalStateError(f"message 状态={locked.status}，不再接受 token")
    offset = len(locked.content)
    pieces: list[str] = []
    for text in texts:
        if not isinstance(text, str) or text == "":
            raise EventValidationError("token 必须是非空字符串")
        append_event(
            conn,
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            event_type="token",
            payload={
                "message_id": str(locked.message_id),
                "generation": generation,
                "offset": offset,
                "text": text,
            },
            generation=generation,
        )
        pieces.append(text)
        offset += len(text)
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_messages "
        "SET content = content || %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND message_id = %s AND status = 'partial' "
        "AND generation = %s RETURNING *",
        ("".join(pieces), message.workspace_id, message.message_id, generation),
    ).fetchone()
    if row is None:
        raise StaleGeneration("token 追加 0 行（message 已终态或 generation 漂移）")
    return _message_from_row(row)


# ---------------------------------------------------------------------------
# 终态事务：Message + Run + terminal 事件一次提交
# ---------------------------------------------------------------------------
def _same_result(run: RunState, hash_value: str) -> bool:
    return run.result_hash is not None and run.result_hash == hash_value


def _terminal_message(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
) -> MessageState:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_messages "
        "WHERE workspace_id = %s AND run_id = %s AND role = 'assistant' "
        "ORDER BY generation DESC LIMIT 1",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        raise MessageNotFound("run 没有 assistant message，无法复放终态")
    return _message_from_row(row)


def finalize(
    conn: psycopg.Connection[Any],
    *,
    run: RunState,
    message: MessageState,
    generation: int,
    content: str,
    citations: Sequence[CitationSpec] = (),
    answer_kind: str = "grounded",
    uncertainty_note: str | None = None,
) -> FinalizeResult:
    """一次事务提交：最终 Message + Run completed + 唯一 terminal 事件。

    * 幂等：同 generation/内容/引用的重复调用返回 ``replayed=True``，不写第二次；
    * 旧 generation：``STALE_GENERATION``（0 行更新）；
    * 已有非 completed terminal（取消/失败）：``RUN_ALREADY_TERMINAL``；
    * 同 run 不同结果：``IDEMPOTENCY_CONFLICT``。

    调用方提交事务；提交失败则三者都不存在（无伪 completed）。
    """

    _require_transaction(conn)
    current = lock_run(conn, run.workspace_id, run.run_id)
    existing_terminal = read_terminal(conn, run.workspace_id, run.run_id)
    if existing_terminal is not None:
        peek = [
            {
                "chunk_id": item.chunk_id,
                "document_id": item.document_id,
                "index_version": int(item.index_version),
                "rank": int(item.rank),
            }
            for item in citations
        ]
        peek_hash = result_hash(
            run_id=run.run_id, generation=generation, content=content, citations=peek
        )
        if existing_terminal.event_type == "completed" and _same_result(current, peek_hash):
            assistant = _terminal_message(
                conn, workspace_id=run.workspace_id, run_id=run.run_id
            )
            return FinalizeResult(
                run=current,
                assistant_message=assistant,
                terminal_event=existing_terminal,
                citations_written=0,
                replayed=True,
            )
        if current.generation != generation:
            raise StaleGeneration(
                f"run generation={current.generation} 已取代 {generation}（终态已存在）"
            )
        raise RunAlreadyTerminal(
            f"run 已有 terminal={existing_terminal.event_type}，不能用不同类型覆盖",
            terminal_type=existing_terminal.event_type,
        )
    if answer_kind not in ANSWER_KINDS:
        raise EventValidationError(f"未知 answer_kind: {answer_kind!r}")
    if answer_kind == "grounded" and not citations:
        raise EventValidationError("grounded 回答必须至少有一条引用")
    if not content.strip():
        raise EventValidationError("最终内容不能为空")
    if uncertainty_note is not None and len(uncertainty_note) > MAX_DETAIL:
        raise EventValidationError("uncertainty_note 过长")
    normalized = [
        CitationSpec(
            chunk_id=_require_uuid(item.chunk_id, name="chunk_id"),
            document_id=_require_uuid(item.document_id, name="document_id"),
            index_version=int(item.index_version),
            rank=int(item.rank),
            quote_hash=item.quote_hash,
        )
        for item in citations
    ]
    for item in normalized:
        if len(item.quote_hash) != 64 or any(ch not in _HEX64 for ch in item.quote_hash):
            raise EventValidationError("quote_hash 必须是 sha256 十六进制")
        if item.index_version < 1 or item.rank < 1:
            raise EventValidationError("index_version/rank 必须 >= 1")
    hash_value = result_hash(
        run_id=run.run_id,
        generation=generation,
        content=content,
        citations=[
            {
                "chunk_id": item.chunk_id,
                "document_id": item.document_id,
                "index_version": item.index_version,
                "rank": item.rank,
            }
            for item in normalized
        ],
    )

    current = lock_run(conn, run.workspace_id, run.run_id)
    existing_terminal = read_terminal(conn, run.workspace_id, run.run_id)
    if existing_terminal is not None:
        if existing_terminal.event_type == "completed" and _same_result(current, hash_value):
            assistant = _terminal_message(
                conn, workspace_id=run.workspace_id, run_id=run.run_id
            )
            return FinalizeResult(
                run=current,
                assistant_message=assistant,
                terminal_event=existing_terminal,
                citations_written=0,
                replayed=True,
            )
        if current.generation != generation:
            raise StaleGeneration(
                f"run generation={current.generation} 已取代 {generation}（终态已存在）"
            )
        raise RunAlreadyTerminal(
            f"run 已有 terminal={existing_terminal.event_type}，不能用不同类型覆盖",
            terminal_type=existing_terminal.event_type,
        )
    if current.generation != generation:
        raise StaleGeneration(
            f"run generation={current.generation} 已取代 {generation}，拒绝提交"
        )
    if current.terminal:
        raise RunAlreadyTerminal(
            f"run 已终态（{current.status}）", terminal_type=current.status
        )

    locked = lock_message(conn, message.workspace_id, message.message_id)
    if locked.generation != generation:
        raise StaleGeneration(
            f"message generation={locked.generation} 与最终提交 generation={generation} 不一致"
        )
    if locked.status != "partial":
        raise TerminalStateError(f"message 状态={locked.status}，不允许 finalize")

    citations_written = _write_citations(
        conn,
        workspace_id=message.workspace_id,
        message_id=message.message_id,
        citations=normalized,
    )
    citations_json = [
        {
            "ordinal": index,
            "chunk_id": str(item.chunk_id),
            "document_id": str(item.document_id),
            "index_version": item.index_version,
            "rank": item.rank,
            "quote_hash": item.quote_hash,
        }
        for index, item in enumerate(normalized)
    ]
    message_row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_messages "
        "SET status = 'final', content = %s, citations = %s::jsonb, result_hash = %s, "
        "finalized_at = now(), row_version = row_version + 1 "
        "WHERE workspace_id = %s AND message_id = %s AND status = 'partial' "
        "AND generation = %s RETURNING *",
        (
            content,
            _canonical(citations_json),
            hash_value,
            message.workspace_id,
            message.message_id,
            generation,
        ),
    ).fetchone()
    if message_row is None:
        raise StaleGeneration("最终消息更新 0 行（并发 finalize 或 generation 漂移）")
    run_row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_runs "
        "SET status = 'completed', answer_kind = %s, result_hash = %s, "
        "terminal_at = now(), updated_at = now(), row_version = row_version + 1 "
        "WHERE workspace_id = %s AND run_id = %s AND status IN ('queued', 'running') "
        "RETURNING *",
        (answer_kind, hash_value, run.workspace_id, run.run_id),
    ).fetchone()
    if run_row is None:
        raise RunAlreadyTerminal("Run 终态更新 0 行（并发终态）", terminal_type="completed")
    terminal = append_event(
        conn,
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        event_type="completed",
        payload={
            "message_id": str(message.message_id),
            "generation": generation,
            "result_hash": hash_value,
            "answer_kind": answer_kind,
            "citations": len(normalized),
            "uncertainty_note": uncertainty_note,
        },
        generation=generation,
    )
    return FinalizeResult(
        run=_run_from_row(run_row),
        assistant_message=_message_from_row(message_row),
        terminal_event=terminal,
        citations_written=citations_written,
        replayed=False,
    )


def _write_citations(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    message_id: uuid.UUID,
    citations: Sequence[CitationSpec],
) -> int:
    seen: set[uuid.UUID] = set()
    written = 0
    for ordinal, item in enumerate(citations):
        if item.chunk_id in seen:
            raise EventValidationError(f"同一消息重复引用 chunk {item.chunk_id}")
        seen.add(item.chunk_id)
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_message_citations "
            "(workspace_id, message_id, ordinal, chunk_id, document_id, index_version, "
            "rank, quote_hash, verified) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)",
            (
                workspace_id,
                message_id,
                ordinal,
                item.chunk_id,
                item.document_id,
                item.index_version,
                item.rank,
                item.quote_hash,
            ),
        )
        written += 1
    return written


# ---------------------------------------------------------------------------
# 失败 / 取消 / generation reset
# ---------------------------------------------------------------------------
def _finish_with_terminal(
    conn: psycopg.Connection[Any],
    *,
    run: RunState,
    terminal_type: str,
    payload: Mapping[str, Any],
    new_status: str,
    generation: int,
) -> FinalizeResult:
    current = lock_run(conn, run.workspace_id, run.run_id)
    existing = read_terminal(conn, run.workspace_id, run.run_id)
    if existing is not None:
        assistant = _terminal_message(conn, workspace_id=run.workspace_id, run_id=run.run_id)
        return FinalizeResult(
            run=current,
            assistant_message=assistant,
            terminal_event=existing,
            citations_written=0,
            replayed=True,
        )
    if current.generation != generation and generation is not None:
        raise StaleGeneration(
            f"run generation={current.generation} 已取代 {generation}"
        )
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_messages SET status = 'incomplete', "
        "row_version = row_version + 1, finalized_at = NULL "
        "WHERE workspace_id = %s AND run_id = %s AND status = 'partial'",
        (run.workspace_id, run.run_id),
    )
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_runs "
        "SET status = %s, terminal_at = now(), updated_at = now(), "
        "row_version = row_version + 1 "
        "WHERE workspace_id = %s AND run_id = %s AND status IN ('queued', 'running') "
        "RETURNING *",
        (new_status, run.workspace_id, run.run_id),
    ).fetchone()
    if row is None:
        raise RunAlreadyTerminal(
            f"Run 已终态（{current.status}），不能再写 {terminal_type}",
            terminal_type=current.status,
        )
    terminal = append_event(
        conn,
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        event_type=terminal_type,
        payload=payload,
        generation=generation,
    )
    assistant = _terminal_message(conn, workspace_id=run.workspace_id, run_id=run.run_id)
    return FinalizeResult(
        run=_run_from_row(row),
        assistant_message=assistant,
        terminal_event=terminal,
        citations_written=0,
        replayed=False,
    )


def finalize_failure(
    conn: psycopg.Connection[Any],
    *,
    run: RunState,
    error_code: str,
    detail: str | None = None,
    generation: int = 1,
) -> FinalizeResult:
    """永久失败：Run failed + failed 事件（同事务）；草稿标 incomplete。"""

    if not error_code or len(error_code) > 64:
        raise EventValidationError("error_code 必须 1..64 字符")
    if detail is not None and len(detail) > MAX_DETAIL:
        raise EventValidationError("detail 过长")
    return _finish_with_terminal(
        conn,
        run=run,
        terminal_type="failed",
        payload={"error_code": error_code, "detail": detail},
        new_status="failed",
        generation=generation,
    )


def cancel_run(
    conn: psycopg.Connection[Any],
    *,
    run: RunState,
    reason: str | None = None,
    generation: int = 1,
) -> FinalizeResult:
    """取消：Run cancelled + cancelled 事件；已提交的 token 草稿保留但标 incomplete。"""

    if reason is not None and len(reason) > MAX_DETAIL:
        raise EventValidationError("reason 过长")
    return _finish_with_terminal(
        conn,
        run=run,
        terminal_type="cancelled",
        payload={"reason": reason},
        new_status="cancelled",
        generation=generation,
    )


def bump_generation(
    conn: psycopg.Connection[Any],
    *,
    run: RunState,
    expected_generation: int,
    reason: str = "model_crash",
) -> tuple[RunState, MessageState]:
    """新 generation：旧 token 作废、旧草稿标 incomplete、写 generation_reset 事件。"""

    _require_transaction(conn)
    current = lock_run(conn, run.workspace_id, run.run_id)
    if current.terminal:
        raise RunAlreadyTerminal(
            f"run 已终态（{current.status}），不能 bump generation",
            terminal_type=current.status,
        )
    if current.generation != expected_generation:
        raise StaleGeneration(
            f"期望 generation={expected_generation}，实际={current.generation}"
        )
    new_generation = current.generation + 1
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_messages SET status = 'incomplete', row_version = row_version + 1 "
        "WHERE workspace_id = %s AND run_id = %s AND role = 'assistant' "
        "AND generation = %s AND status = 'partial'",
        (run.workspace_id, run.run_id, expected_generation),
    )
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_runs "
        "SET generation = generation + 1, updated_at = now(), row_version = row_version + 1 "
        "WHERE workspace_id = %s AND run_id = %s AND generation = %s "
        "AND status IN ('queued', 'running') RETURNING *",
        (run.workspace_id, run.run_id, expected_generation),
    ).fetchone()
    if row is None:
        raise StaleGeneration("generation bump 0 行（并发终态或 generation 漂移）")
    new_run = _run_from_row(row)
    message = ensure_assistant_message(
        conn,
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        generation=new_generation,
    )
    append_event(
        conn,
        workspace_id=run.workspace_id,
        run_id=run.run_id,
        event_type="generation_reset",
        payload={
            "from_generation": expected_generation,
            "to_generation": new_generation,
            "reason": reason,
            "message_id": str(message.message_id),
        },
        generation=new_generation,
    )
    return new_run, message


# ---------------------------------------------------------------------------
# 授权：引用/检索前的权限核对（由 rag_probe 调用）
# ---------------------------------------------------------------------------
def principal_can_read_kb(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    kb_id: uuid.UUID,
) -> bool:
    row = conn.execute(
        f"SELECT can_read FROM {SCHEMA}.phase0_kb_grants "
        "WHERE workspace_id = %s AND kb_id = %s AND principal_id = %s",
        (workspace_id, kb_id, principal_id),
    ).fetchone()
    return bool(row and row["can_read"])


def chunk_visibility(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    chunk_id: uuid.UUID,
) -> Mapping[str, Any] | None:
    """引用校验查询：只在「主体有 grant + 活动版本 + active 指针」时返回行。

    这是与检索同一条可见性口径（filter-in-query），不允许先取后过滤。
    """

    return conn.execute(
        f"SELECT c.chunk_id, c.document_id, c.index_version, c.workspace_id, "
        "c.kind, c.text, c.embedding, c.embedding_dim, c.embedding_profile, "
        "c.chunk_ordinal, d.kb_id "
        f"FROM {SCHEMA}.phase0_chunks AS c "
        f"JOIN {SCHEMA}.phase0_documents AS d "
        "  ON d.workspace_id = c.workspace_id AND d.document_id = c.document_id "
        " AND d.active_version = c.index_version "
        f"JOIN {SCHEMA}.phase0_document_versions AS v "
        "  ON v.workspace_id = c.workspace_id AND v.document_id = c.document_id "
        " AND v.index_version = c.index_version AND v.status = 'active' "
        " AND v.winning_attempt_id = c.attempt_id "
        f"JOIN {SCHEMA}.phase0_kb_grants AS g "
        "  ON g.workspace_id = c.workspace_id AND g.kb_id = d.kb_id "
        " AND g.principal_id = %s AND g.can_read "
        "WHERE c.workspace_id = %s AND c.chunk_id = %s",
        (principal_id, workspace_id, chunk_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# cursor：绑定 workspace/principal/run/seq + expiry（TDD07 §5/§6）
# ---------------------------------------------------------------------------
def _cursor_signature(cursor_id: uuid.UUID, expires_at: datetime, run_id: uuid.UUID) -> str:
    raw = f"{cursor_id}:{int(expires_at.timestamp())}:{run_id}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _encode_cursor(
    cursor: CursorState,
) -> str:
    payload = {
        "v": 1,
        "cid": str(cursor.cursor_id),
        "ws": str(cursor.workspace_id),
        "run": str(cursor.run_id),
        "seq": cursor.after_seq,
        "exp": int(cursor.expires_at.timestamp()),
    }
    raw = base64.urlsafe_b64encode(_canonical(payload).encode()).decode().rstrip("=")
    return f"{raw}.{_cursor_signature(cursor.cursor_id, cursor.expires_at, cursor.run_id)}"


def _decode_cursor(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 1:
        raise EventValidationError("cursor 格式非法")
    raw, signature = token.split(".", 1)
    padded = raw + "=" * (-len(raw) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise EventValidationError("cursor 不是合法 base64/json") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise EventValidationError("cursor 版本非法")
    try:
        cursor_id = uuid.UUID(str(payload["cid"]))
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        run_id = uuid.UUID(str(payload["run"]))
        workspace_id = uuid.UUID(str(payload["ws"]))
        after_seq = int(payload["seq"])
    except (KeyError, ValueError, TypeError) as exc:
        raise EventValidationError("cursor 字段非法") from exc
    if _cursor_signature(cursor_id, expires_at, run_id) != signature:
        raise EventValidationError("cursor 签名不匹配（可能被篡改）")
    return {
        "cursor_id": cursor_id,
        "workspace_id": workspace_id,
        "run_id": run_id,
        "after_seq": after_seq,
        "expires_at": expires_at,
    }


def issue_cursor(
    conn: psycopg.Connection[Any],
    *,
    run: RunState,
    principal_id: uuid.UUID,
    after_seq: int = 0,
    ttl_seconds: int = CURSOR_TTL_SECONDS,
) -> tuple[str, CursorState]:
    _require_transaction(conn)
    if not 1 <= ttl_seconds <= 7 * 24 * 3600:
        raise EventValidationError("ttl_seconds 必须在 [1, 604800]")
    require_active_membership(conn, run.workspace_id, principal_id)
    cursor_id = uuid.uuid4()
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
    row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_event_cursors "
        "(workspace_id, run_id, cursor_id, principal_id, after_seq, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
        (run.workspace_id, run.run_id, cursor_id, principal_id, after_seq, expires_at),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise EventsProbeError("cursor 写入失败")
    state = CursorState(
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        cursor_id=row["cursor_id"],
        principal_id=row["principal_id"],
        after_seq=int(row["after_seq"]),
        expires_at=row["expires_at"],
    )
    return _encode_cursor(state), state


def read_events_with_cursor(
    conn: psycopg.Connection[Any],
    *,
    cursor_token: str,
    principal_id: uuid.UUID,
    limit: int = 500,
    now: datetime | None = None,
) -> tuple[tuple[RunEvent, ...], str]:
    """P0 在线流读取：cursor 有效期内增量重放 + 追新（不含 P1 公共续传）。"""

    _require_transaction(conn)
    decoded = _decode_cursor(cursor_token)
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_event_cursors "
        "WHERE workspace_id = %s AND run_id = %s AND cursor_id = %s",
        (decoded["workspace_id"], decoded["run_id"], decoded["cursor_id"]),
    ).fetchone()
    if row is None:
        raise CursorScopeMismatch("cursor 不存在或已撤销")
    if row["principal_id"] != principal_id:
        raise CursorScopeMismatch("cursor 绑定的主体与当前主体不一致")
    if row["after_seq"] != decoded["after_seq"]:
        raise CursorScopeMismatch("cursor 序号与登记事实不一致（重放/篡改）")
    current = now or datetime.now(tz=UTC)
    expires_at = row["expires_at"]
    if expires_at <= current:
        run = read_run(conn, row["workspace_id"], row["run_id"])
        raise CursorExpired(
            f"事件游标已过期（expires_at={expires_at.isoformat()}）；"
            "P0 不交付 Last-Event-ID 公共续传，请查询最终结果",
            run_status=run.status,
            result_url=f"/api/v1/runs/{run.run_id}",
        )
    require_active_membership(conn, row["workspace_id"], principal_id)
    events = list_events(
        conn,
        row["workspace_id"],
        row["run_id"],
        after_seq=int(row["after_seq"]),
        limit=limit,
    )
    next_after = events[-1].event_seq if events else int(row["after_seq"])
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_event_cursors SET after_seq = %s "
        "WHERE workspace_id = %s AND run_id = %s AND cursor_id = %s",
        (next_after, row["workspace_id"], row["run_id"], row["cursor_id"]),
    )
    next_state = CursorState(
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        cursor_id=row["cursor_id"],
        principal_id=row["principal_id"],
        after_seq=next_after,
        expires_at=row["expires_at"],
    )
    return events, _encode_cursor(next_state)


def resume_events(*_args: Any, **_kwargs: Any) -> None:
    """P0 不交付公共断线续传（TDD07 §6）：显式拒绝，不伪装能力。"""

    raise CapabilityUnsupported(
        "P0 只提供在线流与最终结果查询；Last-Event-ID 续传属 P1，"
        "请改用 GET /runs/{run_id} 获取最终快照"
    )


def event_cursor(event: RunEvent, *, ttl_seconds: int = CURSOR_TTL_SECONDS) -> str:
    """每个持久事件的签名 id（客户端去重/排序用；不用于 P1 续传）。"""

    expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
    payload = {
        "v": 1,
        "eid": str(event.event_id),
        "ws": str(event.workspace_id),
        "run": str(event.run_id),
        "seq": event.event_seq,
        "exp": int(expires_at.timestamp()),
    }
    raw = base64.urlsafe_b64encode(_canonical(payload).encode()).decode().rstrip("=")
    sig = hashlib.sha256(
        f"{event.event_id}:{int(expires_at.timestamp())}:{event.run_id}".encode()
    ).hexdigest()[:32]
    return f"{raw}.{sig}"


def format_sse(events: Sequence[RunEvent], *, cursor: str | None = None) -> str:
    """``text/event-stream`` 帧：id 行 + event 行 + data 行（TDD07 §6 示例）。"""

    frames: list[str] = []
    for event in events:
        frames.append(f"id: {event_cursor(event)}")
        frames.append(f"event: {event.event_type}")
        frames.append(f"data: {_canonical(event.envelope())}")
        frames.append("")
    if cursor is not None:
        frames.append(": ping")
        frames.append("")
    return "\n".join(frames)


def record_citation_rejection(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    reason: str,
    chunk_id: uuid.UUID | None,
    detail: str | None = None,
) -> None:
    _require_transaction(conn)
    if reason not in REJECTION_REASONS:
        raise EventValidationError(f"未知拒绝原因: {reason!r}")
    if detail is not None and len(detail) > MAX_DETAIL:
        raise EventValidationError("detail 过长")
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_citation_rejections "
        "(workspace_id, run_id, reason, chunk_id, detail) VALUES (%s, %s, %s, %s, %s)",
        (workspace_id, run_id, reason, chunk_id, detail),
    )


def count_rejections(
    conn: psycopg.Connection[Any],
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    reason: str | None = None,
) -> int:
    clause = "AND reason = %s" if reason is not None else ""
    params: list[Any] = [workspace_id, run_id]
    if reason is not None:
        params.append(reason)
    row = conn.execute(
        f"SELECT count(*) AS total FROM {SCHEMA}.phase0_citation_rejections "
        f"WHERE workspace_id = %s AND run_id = %s {clause}",
        params,
    ).fetchone()
    return int(row["total"]) if row else 0


__all__ = [
    "ANSWER_KINDS",
    "CURSOR_TTL_SECONDS",
    "CitationSpec",
    "CursorExpired",
    "CursorScopeMismatch",
    "CursorState",
    "EventsProbeError",
    "EVENT_SCHEMA_VERSION",
    "EVENT_TYPES",
    "EventValidationError",
    "FinalizeResult",
    "IdempotencyConflict",
    "MessageNotFound",
    "MessageState",
    "MIGRATION_ID",
    "MIGRATION_SQL",
    "REJECTION_REASONS",
    "RunAlreadyTerminal",
    "RunEvent",
    "RunNotFound",
    "RunState",
    "SCHEMA",
    "SchemaAlreadyApplied",
    "StaleGeneration",
    "TerminalStateError",
    "Unauthorized",
    "append_event",
    "apply_schema",
    "bump_generation",
    "cancel_run",
    "chunk_visibility",
    "connect",
    "count_events",
    "count_rejections",
    "count_terminal",
    "create_run",
    "ensure_assistant_message",
    "ensure_conversation",
    "ensure_schema",
    "event_cursor",
    "finalize",
    "finalize_failure",
    "format_sse",
    "issue_cursor",
    "list_events",
    "list_messages",
    "lock_message",
    "lock_run",
    "persist_tokens",
    "principal_can_read_kb",
    "read_events_with_cursor",
    "read_message",
    "read_run",
    "read_terminal",
    "record_citation_rejection",
    "require_active_membership",
    "reset_schema",
    "result_hash",
    "resume_events",
    "schema_sha256",
]
