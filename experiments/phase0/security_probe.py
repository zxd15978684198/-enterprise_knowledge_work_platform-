"""P0-T29 安全门禁探针：空间/动作越权、撤权、载荷篡改、SSRF、canary 与业务事实状态。

定位（Backlog P0-T29，docs/poc/03_Phase0_Task_Backlog.md L490-505）：
把 Phase 0 已建组件（T03 Scope、T06 审批、T08 Job、T12/T13 索引与发布、T21 trace、
T26 引用）的负例汇成可重复的门禁矩阵，**不**实现生产安全平台、**不**写业务状态。
本模块自包含：只使用独立 schema ``p0_t29``、Redis DB 16、端口 5600，镜像各 owner
服务的判定语义而不导入其 schema / DB 链路。

C-07 BLOCKED：真实身份域（登录、全局 User、subject→membership 映射、生产来源审阅）
未决，本模块只用**合成** Principal/Scope 做越权判定，不构成生产授权路径。
真实 chat 模型链路不可用 → 结果最高 ``PASS（限域）``；pgvector ANN 未参与本任务。

设计约束：
* 每条探测记录 probe_id / category / vector / expected / observed / evidence_ref，
  拒绝证据写到 ``docs/poc/evidence/security-probes.json``；
* 秘密只从环境读取，任何 DTO/日志/异常/证据都不得出现密钥值，canary 扫描覆盖
  SafeTrace 与 to_state_fragment；
* schema 与角色由本模块创建，测试结束 DROP；Redis 只用 ``p0_t29:`` 前缀。

Limit（写入证据的 NOT_RUN）：
* 真实模型/真实身份提供商/真实对象存储签名代理/真实 DNS 重绑定环境：NOT_RUN；
* 本机无 SMTP/邮件等 PII 出口，PII canary 只验证“进入 trace/state”这一面；
* Rerank、pgvector ANN、跨任务 DB 共享不在本任务范围。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4, uuid5

import psycopg
import redis
from psycopg import IsolationLevel, sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

import contracts
import safe_trace
import scope
import tool_registry

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
EVIDENCE_DIR: Final = REPO_ROOT / "docs" / "poc" / "evidence"
MATRIX_PATH: Final = EVIDENCE_DIR / "security-matrix.md"
EVIDENCE_PATH: Final = EVIDENCE_DIR / "security-probes.json"
RAG_BASELINE_PATH: Final = EVIDENCE_DIR / "rag-citations.json"

SCHEMA: Final = "p0_t29"
APP_ROLE: Final = "p0_t29_app"
MIGRATION_ID: Final = "t29_security_inline"
MIGRATION_FILENAME: Final = "inline:security_probe.py::MIGRATION_SQL"

# W9 波次记录指定 Redis DB16，但服务器 CONFIG GET databases = 16（合法索引 0-15），
# DB16 实测不可用（ERR DB index is out of range）；改用最接近且空闲的 DB15。
# 证据 JSON 的 redis.resource_note 记录该偏差；DB0 有其它任务的 t02:probe 键，不触碰。
REDIS_DB: Final = 15
REDIS_RESOURCE_NOTE: Final = (
    "W9 波次记录指定 Redis DB16，但服务器 CONFIG GET databases=16 只支持索引 0-15，"
    "实测 DB16 不可用（ERR DB index is out of range）；改用空闲 DB15，"
    "运行前后验证 DB0 仅保留其它任务的 t02:probe 键、DB1-DB14 无键。"
)
REDIS_PREFIX: Final = "p0_t29:"
CONTROL_PORT: Final = 5600
CONTROL_HOST: Final = "127.0.0.1"

TASK_ID: Final = "P0-T29"
POC_ID: Final = "PoC-01/02/03/04/06/09"
NAMESPACE: Final = uuid5(uuid4(), "pyknora.phase0.p0-t29")

# canary：全部为合成值，禁止替换成任何真实凭据。
SECRET_CANARY: Final = "sk-canary-t29-9f3c1b7e2d5a"
AUTH_CANARY: Final = "Bearer canary-t29-6a1d4e8c"
PII_EMAIL_CANARY: Final = "canary.pii.t29@example.invalid"
PII_PHONE_CANARY: Final = "+86-100-0000-0029"
DSN_CANARY: Final = "postgresql://canary:t29-secret@127.0.0.1:5433/canary"

CANARY_VALUES: Final = (
    SECRET_CANARY,
    AUTH_CANARY,
    PII_EMAIL_CANARY,
    PII_PHONE_CANARY,
    DSN_CANARY,
)

# 配额（总探测预算）
MIN_PROBES: Final[dict[str, int]] = {
    "cross_workspace": 100,
    "revocation": 12,
    "payload": 20,
    "ssrf": 24,
    "canary": 12,
    "business_state": 12,
    "citations": 100,
}
MIN_TOTAL: Final = 100
SESSION_TTL_SECONDS: Final = 600
CURSOR_TTL_SECONDS: Final = 600
SIGNATURE_TTL_SECONDS: Final = 300
REQUEST_TIMEOUT_SECONDS: Final = 1.0
MAX_RESPONSE_BYTES: Final = 2048
MAX_REDIRECTS: Final = 3

_SQLSTATE_RE: Final = re.compile(r"^[0-9A-Z]{5}$")


def _u(tag: str) -> UUID:
    """稳定派生 UUID（种子可复现）。"""

    return uuid5(NAMESPACE, tag)


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class SecurityProbeError(RuntimeError):
    """探针自身或环境错误。"""

    code = "SECURITY_PROBE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class AccessDenied(SecurityProbeError):
    """被被测组件正确拒绝（探针期望的路径）。"""

    code = "ACCESS_DENIED"


class ProbeNotRun(SecurityProbeError):
    """缺少环境，显式标记 NOT_RUN。"""

    code = "PROBE_NOT_RUN"


class SsrfBlocked(SecurityProbeError):
    """出站请求被安全策略拒绝。"""

    code = "SSRF_BLOCKED"


# ---------------------------------------------------------------------------
# DDL：独立 schema p0_t29
# ---------------------------------------------------------------------------

MIGRATION_SQL: Final = f"""
CREATE SCHEMA {SCHEMA};

DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        CREATE ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
    ELSE
        ALTER ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
    END IF;
END
$role$;

GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE};

CREATE TABLE {SCHEMA}.phase0_schema_migrations (
    migration_id text PRIMARY KEY,
    filename     text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

CREATE TABLE {SCHEMA}.phase0_workspaces (
    workspace_id uuid PRIMARY KEY,
    name         text NOT NULL,
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'tombstoned'))
);

CREATE TABLE {SCHEMA}.phase0_principals (
    workspace_id           uuid NOT NULL REFERENCES {SCHEMA}.phase0_workspaces (workspace_id),
    principal_id           uuid NOT NULL,
    kind                   text NOT NULL CHECK (kind IN ('human', 'service')),
    role                   text NOT NULL CHECK (role IN ('owner', 'member')),
    status                 text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    membership_revision    bigint NOT NULL DEFAULT 1 CHECK (membership_revision >= 1),
    PRIMARY KEY (workspace_id, principal_id)
);

CREATE TABLE {SCHEMA}.phase0_kb_grants (
    workspace_id uuid NOT NULL,
    kb_id        uuid NOT NULL,
    principal_id uuid NOT NULL,
    status       text NOT NULL CHECK (status IN ('active', 'revoked')),
    grant_revision bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (workspace_id, kb_id, principal_id),
    FOREIGN KEY (workspace_id, principal_id)
        REFERENCES {SCHEMA}.phase0_principals (workspace_id, principal_id)
);

CREATE TABLE {SCHEMA}.phase0_documents (
    workspace_id   uuid NOT NULL,
    document_id    uuid NOT NULL,
    kb_id          uuid NOT NULL,
    source_id      uuid NOT NULL,
    active_version integer NOT NULL,
    row_version    bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (workspace_id, document_id)
);

CREATE TABLE {SCHEMA}.phase0_document_versions (
    workspace_id        uuid NOT NULL,
    document_id         uuid NOT NULL,
    index_version       integer NOT NULL,
    status              text NOT NULL
                        CHECK (status IN ('staging', 'ready', 'active', 'retired', 'failed')),
    winning_attempt_id  uuid,
    chunk_count         integer NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, document_id, index_version),
    FOREIGN KEY (workspace_id, document_id)
        REFERENCES {SCHEMA}.phase0_documents (workspace_id, document_id)
);

CREATE TABLE {SCHEMA}.phase0_chunks (
    workspace_id        uuid NOT NULL,
    chunk_id            uuid NOT NULL,
    document_id         uuid NOT NULL,
    index_version       integer NOT NULL,
    attempt_id          uuid NOT NULL,
    chunk_ordinal       integer NOT NULL CHECK (chunk_ordinal >= 0),
    kind                text NOT NULL CHECK (kind IN ('title', 'body')),
    text                text NOT NULL,
    source_id           uuid NOT NULL,
    source_version_id   uuid NOT NULL,
    checksum            text NOT NULL,
    PRIMARY KEY (workspace_id, chunk_id),
    FOREIGN KEY (workspace_id, document_id, index_version)
        REFERENCES {SCHEMA}.phase0_document_versions (workspace_id, document_id, index_version),
    UNIQUE (workspace_id, document_id, index_version, chunk_ordinal)
);

CREATE TABLE {SCHEMA}.phase0_runs (
    workspace_id uuid NOT NULL,
    run_id       uuid NOT NULL,
    principal_id uuid NOT NULL,
    status       text NOT NULL DEFAULT 'running',
    PRIMARY KEY (workspace_id, run_id),
    FOREIGN KEY (workspace_id, principal_id)
        REFERENCES {SCHEMA}.phase0_principals (workspace_id, principal_id)
);

CREATE TABLE {SCHEMA}.phase0_run_events (
    workspace_id uuid NOT NULL,
    run_id       uuid NOT NULL,
    event_id     uuid NOT NULL,
    event_type   smallint NOT NULL CHECK (event_type BETWEEN 0 AND 4),
    terminal     boolean NOT NULL DEFAULT false,
    payload      jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    PRIMARY KEY (workspace_id, run_id, event_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES {SCHEMA}.phase0_runs (workspace_id, run_id)
);

CREATE TABLE {SCHEMA}.phase0_execution_bindings (
    workspace_id         uuid NOT NULL,
    execution_binding_id uuid NOT NULL,
    principal_id         uuid NOT NULL,
    operation            text NOT NULL,
    resource_kind        text NOT NULL,
    resource_id          uuid NOT NULL,
    parent_run_id        uuid,
    created_at           timestamptz NOT NULL DEFAULT now(),
    expires_at           timestamptz,
    PRIMARY KEY (workspace_id, execution_binding_id),
    FOREIGN KEY (workspace_id, principal_id)
        REFERENCES {SCHEMA}.phase0_principals (workspace_id, principal_id)
);

CREATE TABLE {SCHEMA}.phase0_jobs (
    workspace_id         uuid NOT NULL,
    job_id               uuid NOT NULL,
    execution_binding_id uuid NOT NULL,
    run_id               uuid NOT NULL,
    kind                 text NOT NULL,
    status               text NOT NULL DEFAULT 'pending',
    lease_epoch          bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (workspace_id, job_id),
    FOREIGN KEY (workspace_id, execution_binding_id)
        REFERENCES {SCHEMA}.phase0_execution_bindings (workspace_id, execution_binding_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES {SCHEMA}.phase0_runs (workspace_id, run_id)
);

CREATE TABLE {SCHEMA}.phase0_approvals (
    workspace_id    uuid NOT NULL,
    approval_id     uuid NOT NULL,
    run_id          uuid NOT NULL,
    intent_key      text NOT NULL,
    operation       text NOT NULL,
    scope_hash      text NOT NULL,
    idempotency_key text NOT NULL,
    requester_id    uuid NOT NULL,
    approver_id     uuid NOT NULL,
    status          text NOT NULL
                    CHECK (status IN (
                        'pending', 'approved', 'rejected', 'consumed', 'revoked'
                    )),
    expires_at      timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, approval_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES {SCHEMA}.phase0_runs (workspace_id, run_id)
);

CREATE TABLE {SCHEMA}.phase0_citations (
    workspace_id uuid NOT NULL,
    citation_id  uuid NOT NULL,
    run_id       uuid NOT NULL,
    chunk_id     uuid NOT NULL,
    handle       integer NOT NULL,
    status       text NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected')),
    PRIMARY KEY (workspace_id, citation_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES {SCHEMA}.phase0_runs (workspace_id, run_id)
);

CREATE TABLE {SCHEMA}.phase0_object_refs (
    workspace_id uuid NOT NULL,
    object_key   text NOT NULL,
    principal_id uuid NOT NULL,
    signature    text NOT NULL,
    method       text NOT NULL,
    purpose      text NOT NULL,
    expires_at   timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, object_key),
    FOREIGN KEY (workspace_id, principal_id)
        REFERENCES {SCHEMA}.phase0_principals (workspace_id, principal_id)
);

CREATE OR REPLACE FUNCTION {SCHEMA}.phase0_current_workspace_id() RETURNS uuid
    LANGUAGE sql STABLE
    SET search_path = pg_catalog, {SCHEMA}
    AS $fn$
        SELECT NULLIF(current_setting('app.workspace_id', true), '')::uuid
    $fn$;

CREATE OR REPLACE FUNCTION {SCHEMA}.phase0_current_principal_id() RETURNS uuid
    LANGUAGE sql STABLE
    SET search_path = pg_catalog, {SCHEMA}
    AS $fn$
        SELECT NULLIF(current_setting('app.principal_id', true), '')::uuid
    $fn$;

CREATE OR REPLACE FUNCTION {SCHEMA}.phase0_principal_active() RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, {SCHEMA}
    AS $fn$
        SELECT COALESCE(
            (
                SELECT p.status = 'active'
                FROM {SCHEMA}.phase0_principals AS p
                WHERE p.workspace_id = {SCHEMA}.phase0_current_workspace_id()
                  AND p.principal_id = {SCHEMA}.phase0_current_principal_id()
            ),
            false
        )
    $fn$;

CREATE OR REPLACE FUNCTION {SCHEMA}.phase0_run_event_guard() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, {SCHEMA}
    AS $fn$
    DECLARE
        claim_principal text;
        claim_workspace text;
    BEGIN
        claim_principal := NEW.payload ->> 'principal_id';
        claim_workspace := NEW.payload ->> 'workspace_id';
        IF claim_principal IS NOT NULL
           AND claim_principal IS DISTINCT FROM current_setting('app.principal_id', true) THEN
            RAISE EXCEPTION 'run event payload may not claim a different principal'
                USING ERRCODE = '42501';
        END IF;
        IF claim_workspace IS NOT NULL
           AND claim_workspace IS DISTINCT FROM current_setting('app.workspace_id', true) THEN
            RAISE EXCEPTION 'run event payload may not claim a different workspace'
                USING ERRCODE = '42501';
        END IF;
        IF NEW.terminal AND EXISTS (
            SELECT 1
            FROM {SCHEMA}.phase0_run_events AS e
            WHERE e.workspace_id = NEW.workspace_id
              AND e.run_id = NEW.run_id
              AND e.terminal
        ) THEN
            RAISE EXCEPTION 'run already has a terminal event' USING ERRCODE = '23505';
        END IF;
        RETURN NEW;
    END;
    $fn$;

CREATE TRIGGER phase0_run_events_guard
    BEFORE INSERT ON {SCHEMA}.phase0_run_events
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.phase0_run_event_guard();

CREATE OR REPLACE FUNCTION {SCHEMA}.phase0_events_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $fn$
    BEGIN
        RAISE EXCEPTION 'run events are append-only' USING ERRCODE = '23000';
    END;
    $fn$;

CREATE TRIGGER phase0_run_events_append_only
    BEFORE UPDATE OR DELETE ON {SCHEMA}.phase0_run_events
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.phase0_events_append_only();

ALTER TABLE {SCHEMA}.phase0_workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_principals FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_kb_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_kb_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_documents FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_document_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_document_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_run_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_run_events FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_execution_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_execution_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_approvals FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_citations ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_citations FORCE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_object_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {SCHEMA}.phase0_object_refs FORCE ROW LEVEL SECURITY;

CREATE POLICY phase0_workspaces_scope ON {SCHEMA}.phase0_workspaces
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id());

CREATE POLICY phase0_principals_scope ON {SCHEMA}.phase0_principals
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id());

CREATE POLICY phase0_kb_grants_scope ON {SCHEMA}.phase0_kb_grants
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_documents_scope ON {SCHEMA}.phase0_documents
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_versions_scope ON {SCHEMA}.phase0_document_versions
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_chunks_scope ON {SCHEMA}.phase0_chunks
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_runs_scope ON {SCHEMA}.phase0_runs
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_run_events_scope ON {SCHEMA}.phase0_run_events
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_bindings_scope ON {SCHEMA}.phase0_execution_bindings
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_jobs_scope ON {SCHEMA}.phase0_jobs
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_approvals_scope ON {SCHEMA}.phase0_approvals
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_citations_scope ON {SCHEMA}.phase0_citations
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

CREATE POLICY phase0_object_refs_scope ON {SCHEMA}.phase0_object_refs
    FOR ALL TO {APP_ROLE}
    USING (workspace_id = {SCHEMA}.phase0_current_workspace_id()
           AND {SCHEMA}.phase0_principal_active())
    WITH CHECK (workspace_id = {SCHEMA}.phase0_current_workspace_id()
                AND {SCHEMA}.phase0_principal_active());

GRANT SELECT, INSERT, UPDATE, DELETE ON
    {SCHEMA}.phase0_workspaces,
    {SCHEMA}.phase0_principals,
    {SCHEMA}.phase0_kb_grants,
    {SCHEMA}.phase0_documents,
    {SCHEMA}.phase0_document_versions,
    {SCHEMA}.phase0_chunks,
    {SCHEMA}.phase0_runs,
    {SCHEMA}.phase0_run_events,
    {SCHEMA}.phase0_execution_bindings,
    {SCHEMA}.phase0_jobs,
    {SCHEMA}.phase0_approvals,
    {SCHEMA}.phase0_citations,
    {SCHEMA}.phase0_object_refs
TO {APP_ROLE};

REVOKE ALL ON FUNCTION {SCHEMA}.phase0_current_workspace_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION {SCHEMA}.phase0_current_principal_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION {SCHEMA}.phase0_principal_active() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {SCHEMA}.phase0_current_workspace_id() TO {APP_ROLE};
GRANT EXECUTE ON FUNCTION {SCHEMA}.phase0_current_principal_id() TO {APP_ROLE};
GRANT EXECUTE ON FUNCTION {SCHEMA}.phase0_principal_active() TO {APP_ROLE};
"""


# ---------------------------------------------------------------------------
# 连接 / 迁移 / 作用域
# ---------------------------------------------------------------------------


def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection[Any]:
    """打开带 dict_row 的连接；DSN 缺省从 .env.example / 环境变量读取。"""

    return psycopg.connect(
        dsn or scope.read_dsn(), autocommit=autocommit, row_factory=dict_row
    )


def schema_sha256(ddl: str = MIGRATION_SQL) -> str:
    return hashlib.sha256(ddl.encode()).hexdigest()


def apply_schema(
    conn: psycopg.Connection[Any],
    *,
    ddl: str = MIGRATION_SQL,
    migration_id: str = MIGRATION_ID,
) -> str:
    """执行内联 DDL 并写台账（不 commit，调用方决定事务边界）。"""

    if conn.autocommit:
        raise SecurityProbeError("apply_schema 需要非 autocommit 连接")
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        raise SecurityProbeError("apply_schema 需要空闲连接（避免复用未提交事务）")
    digest = schema_sha256(ddl)
    conn.execute(ddl)
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_schema_migrations "
        "(migration_id, filename, sha256) VALUES (%s, %s, %s)",
        (migration_id, MIGRATION_FILENAME, digest),
    )
    return digest


def reset_schema(conn: psycopg.Connection[Any], *, role: str = APP_ROLE) -> bool:
    """删除本任务 schema 与角色；返回是否确有删除动作。"""

    dropped = False
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    existing = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    if existing is not None:
        conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        dropped = True
    conn.commit()
    return dropped


def role_security_attributes(
    conn: psycopg.Connection[Any], *, role: str = APP_ROLE
) -> dict[str, Any]:
    """读角色安全属性；兼容 dict_row（按 key 取值，不 zip 行对象）。"""

    row = conn.execute(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin "
        "FROM pg_roles WHERE rolname = %s",
        (role,),
    ).fetchone()
    if row is None:
        raise SecurityProbeError(f"角色不存在: {role}", code="ROLE_MISSING")
    keys = ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolcanlogin")
    if isinstance(row, Mapping):
        return {key: bool(row[key]) for key in keys}
    return {key: bool(value) for key, value in zip(keys, row, strict=True)}


def create_app_role(
    conn: psycopg.Connection[Any], *, role: str = APP_ROLE
) -> dict[str, Any]:
    """为应用角色设置随机口令（不落盘），返回连接 DSN 与安全属性。"""

    password = secrets.token_urlsafe(24)
    scope.set_role_password(conn, role=role, password=password)
    conn.commit()
    return {
        "dsn": scope.dsn_with_credentials(scope.read_dsn(), user=role, password=password),
        "attributes": role_security_attributes(conn, role=role),
        "password_sha256": hashlib.sha256(password.encode()).hexdigest(),
    }


@contextmanager
def scoped_transaction(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID,
    policy_snapshot_id: UUID | None = None,
) -> Iterator[psycopg.Connection[Any]]:
    """在 REPEATABLE READ 事务内应用 Scope；退出提交并恢复原隔离级别。"""

    if conn.autocommit:
        raise SecurityProbeError("scoped_transaction 需要非 autocommit 连接")
    conn.rollback()
    previous = conn.isolation_level
    conn.isolation_level = IsolationLevel.REPEATABLE_READ
    try:
        with conn.transaction():
            scope.apply_scope(
                conn,
                workspace_id=workspace_id,
                principal_id=principal_id,
                policy_snapshot_id=policy_snapshot_id,
            )
            yield conn
    finally:
        if conn.info.transaction_status is TransactionStatus.IDLE:
            conn.isolation_level = previous


def _scoped_count(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID,
    statement: str,
    params: Sequence[Any] = (),
) -> int:
    """在指定 Scope 下执行只读查询并返回行数（探针的拒绝判定为 0 行）。"""

    with scoped_transaction(
        conn, workspace_id=workspace_id, principal_id=principal_id
    ) as scoped:
        row = scoped.execute(statement, tuple(params)).fetchone()
    if row is None:
        return 0
    value = next(iter(row.values()))
    return int(value or 0)


def _run_user_statement(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID,
    statement: str,
    params: Sequence[Any] = (),
) -> tuple[int, int]:
    """无显式事务地执行语句：先 rollback 清态，SET LOCAL 作用域，执行后回滚。"""

    conn.rollback()
    scope.apply_scope(
        conn, workspace_id=workspace_id, principal_id=principal_id
    )
    try:
        result = conn.execute(statement, tuple(params))
        if result.description is None:
            return 0, result.rowcount
        return len(result.fetchall()), 0
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    probe_id: str
    category: str
    vector: str
    expected: str
    observed: str
    refused: bool
    evidence_ref: str


class ProbeLog:
    """探测记录汇总（不落库，由证据 JSON 与矩阵文档承载）。"""

    def __init__(self) -> None:
        self._records: list[ProbeRecord] = []

    def add(
        self,
        category: str,
        probe_id: str,
        vector: str,
        expected: str,
        observed: str,
        *,
        refused: bool,
        evidence_ref: str = "",
    ) -> None:
        self._records.append(
            ProbeRecord(
                probe_id=probe_id,
                category=category,
                vector=vector,
                expected=expected,
                observed=observed,
                refused=refused,
                evidence_ref=evidence_ref or f"{SCHEMA}.phase0_schema_migrations",
            )
        )

    def records(self) -> tuple[ProbeRecord, ...]:
        return tuple(self._records)

    def summary(self) -> dict[str, Any]:
        categories: dict[str, dict[str, int]] = {}
        for record in self._records:
            bucket = categories.setdefault(
                record.category, {"probes": 0, "refused": 0, "allowed": 0, "breaches": 0}
            )
            bucket["probes"] += 1
            if record.refused:
                bucket["refused"] += 1
            else:
                bucket["allowed"] += 1
            if record.expected == "deny" and not record.refused:
                bucket["breaches"] += 1
            if record.expected == "allow" and record.refused:
                bucket["breaches"] += 1
        total = {
            "probes": len(self._records),
            "refused": sum(1 for record in self._records if record.refused),
            "allowed": sum(1 for record in self._records if not record.refused),
            "breaches": sum(
                int(bucket["breaches"]) for bucket in categories.values()
            ),
        }
        return {"total": total, "categories": categories}

    def breaches(self) -> tuple[ProbeRecord, ...]:
        return tuple(
            record
            for record in self._records
            if (record.expected == "deny" and not record.refused)
            or (record.expected == "allow" and record.refused)
        )


@dataclass(frozen=True, slots=True)
class SeedData:
    workspaces: dict[str, UUID]
    principals: dict[str, UUID]
    principals_by_role: dict[str, UUID]
    documents: dict[str, UUID]
    chunks: dict[str, UUID]
    stale_chunks: tuple[UUID, ...]
    bindings: dict[str, UUID]
    jobs: dict[str, UUID]
    runs: dict[str, UUID]
    citations: dict[str, UUID]
    object_key: str
    object_signature: str
    approval_id: UUID
    intent_key: str
    intent_scope_hash: str
    idempotency_key: str
    session_tokens: dict[str, str]


WS_A: Final = _u("ws:A")
WS_B: Final = _u("ws:B")
WS_C: Final = _u("ws:C")
PRIN_A_OWNER: Final = _u("principal:A:owner")
PRIN_A_MEMBER: Final = _u("principal:A:member")
PRIN_A_REVOKED: Final = _u("principal:A:revoked")
PRIN_B_OWNER: Final = _u("principal:B:owner")
PRIN_C_OWNER: Final = _u("principal:C:owner")
KB_A0: Final = _u("kb:A:0")
KB_A1: Final = _u("kb:A:1")
DOC_A0: Final = _u("document:A:0")
DOC_A1: Final = _u("document:A:1")
DOC_A2: Final = _u("document:A:2")
DOC_A3: Final = _u("document:A:3")
DOC_B0: Final = _u("document:B:0")
DOC_C0: Final = _u("document:C:0")
RUN_A: Final = _u("run:A")
RUN_B: Final = _u("run:B")
EVENT_A: Final = _u("event:A:0")
BINDING_A: Final = _u("binding:A")
JOB_A: Final = _u("job:A")
CITATION_A: Final = _u("citation:A")
APPROVAL_A: Final = _u("approval:A")
OBJECT_KEY_A: Final = "object/a/secret-artifact.txt"

SIGNING_KEY: Final = hashlib.sha256(b"p0-t29-synthetic-signing-key").digest()


def _attempt_id(workspace_id: UUID, document_id: UUID, index_version: int) -> UUID:
    return _u(f"attempt:{workspace_id}:{document_id}:{index_version}")


def _chunk_id(workspace_id: UUID, document_id: UUID, index_version: int, ordinal: int) -> UUID:
    return _u(f"chunk:{workspace_id}:{document_id}:{index_version}:{ordinal}")


def _chunk_text(workspace: str, ordinal: int) -> str:
    kind = "title" if ordinal == 0 else "body"
    return f"[p0-t29] {workspace} chunk ordinal={ordinal} kind={kind}"


def _insert_workspace(conn: psycopg.Connection[Any], workspace_id: UUID, name: str) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_workspaces (workspace_id, name) VALUES (%s, %s)",
        (workspace_id, name),
    )


def _insert_principal(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID,
    role: str,
    status: str = "active",
    revision: int = 1,
) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_principals "
        "(workspace_id, principal_id, kind, role, status, membership_revision) "
        "VALUES (%s, %s, 'human', %s, %s, %s)",
        (workspace_id, principal_id, role, status, revision),
    )


def _insert_document(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    document_id: UUID,
    kb_id: UUID,
    active_version: int,
) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_documents "
        "(workspace_id, document_id, kb_id, source_id, active_version) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            workspace_id,
            document_id,
            kb_id,
            _u(f"source:{workspace_id}:{document_id}"),
            active_version,
        ),
    )


def _insert_version(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    document_id: UUID,
    index_version: int,
    status: str,
    ordinal_count: int = 3,
) -> UUID:
    attempt_id = _attempt_id(workspace_id, document_id, index_version)
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_document_versions "
        "(workspace_id, document_id, index_version, status, winning_attempt_id, chunk_count) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (workspace_id, document_id, index_version, status, attempt_id, ordinal_count),
    )
    return attempt_id


def _insert_chunk(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    document_id: UUID,
    index_version: int,
    ordinal: int,
    tag: str,
) -> UUID:
    chunk_id = _chunk_id(workspace_id, document_id, index_version, ordinal)
    text = _chunk_text(tag, ordinal)
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_chunks "
        "(workspace_id, chunk_id, document_id, index_version, attempt_id, chunk_ordinal, "
        " kind, text, source_id, source_version_id, checksum) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            workspace_id,
            chunk_id,
            document_id,
            index_version,
            _attempt_id(workspace_id, document_id, index_version),
            ordinal,
            "title" if ordinal == 0 else "body",
            text,
            _u(f"source:{workspace_id}:{document_id}"),
            _u(f"source_version:{workspace_id}:{document_id}:{index_version}"),
            hashlib.sha256(text.encode()).hexdigest(),
        ),
    )
    return chunk_id


def seed_corpus(conn: psycopg.Connection[Any]) -> SeedData:
    """构造两空间最小事实集；所有 id 稳定派生，重复执行结果一致。"""

    _insert_workspace(conn, WS_A, "workspace-A")
    _insert_workspace(conn, WS_B, "workspace-B")
    _insert_workspace(conn, WS_C, "workspace-C")
    _insert_principal(conn, workspace_id=WS_A, principal_id=PRIN_A_OWNER, role="owner")
    _insert_principal(
        conn, workspace_id=WS_A, principal_id=PRIN_A_MEMBER, role="member", revision=1
    )
    _insert_principal(
        conn, workspace_id=WS_A, principal_id=PRIN_A_REVOKED, role="member", status="disabled"
    )
    _insert_principal(conn, workspace_id=WS_B, principal_id=PRIN_B_OWNER, role="owner")
    _insert_principal(conn, workspace_id=WS_C, principal_id=PRIN_C_OWNER, role="owner")

    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_kb_grants "
        "(workspace_id, kb_id, principal_id, status) VALUES (%s, %s, %s, 'active')",
        (WS_A, KB_A0, PRIN_A_OWNER),
    )
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_kb_grants "
        "(workspace_id, kb_id, principal_id, status) VALUES (%s, %s, %s, 'active')",
        (WS_A, KB_A0, PRIN_A_MEMBER),
    )
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_kb_grants "
        "(workspace_id, kb_id, principal_id, status) VALUES (%s, %s, %s, 'revoked')",
        (WS_A, KB_A1, PRIN_A_MEMBER),
    )

    documents = {
        "A0": DOC_A0,
        "A1": DOC_A1,
        "A2": DOC_A2,
        "A3": DOC_A3,
        "B0": DOC_B0,
        "C0": DOC_C0,
    }
    _insert_document(conn, workspace_id=WS_A, document_id=DOC_A0, kb_id=KB_A0, active_version=1)
    _insert_document(conn, workspace_id=WS_A, document_id=DOC_A1, kb_id=KB_A0, active_version=2)
    _insert_document(conn, workspace_id=WS_A, document_id=DOC_A2, kb_id=KB_A1, active_version=1)
    _insert_document(conn, workspace_id=WS_A, document_id=DOC_A3, kb_id=KB_A0, active_version=1)
    _insert_document(
        conn, workspace_id=WS_B, document_id=DOC_B0, kb_id=_u("kb:B:0"), active_version=1
    )
    _insert_document(
        conn, workspace_id=WS_C, document_id=DOC_C0, kb_id=_u("kb:C:0"), active_version=1
    )

    for document_id, version, status in (
        (DOC_A0, 1, "active"),
        (DOC_A1, 1, "retired"),
        (DOC_A1, 2, "active"),
        (DOC_A2, 1, "active"),
        (DOC_A3, 1, "active"),
        (DOC_B0, 1, "active"),
        (DOC_C0, 1, "active"),
    ):
        workspace_id = WS_B if document_id == DOC_B0 else (WS_C if document_id == DOC_C0 else WS_A)
        _insert_version(
            conn,
            workspace_id=workspace_id,
            document_id=document_id,
            index_version=version,
            status=status,
        )

    chunks: dict[str, UUID] = {}
    stale: list[UUID] = []
    for key, document_id in documents.items():
        workspace_id = WS_B if key == "B0" else (WS_C if key == "C0" else WS_A)
        versions = [(1, "retired")] if key == "A1" else ([(1, "active")] if key != "A1" else [])
        if key == "A1":
            versions = [(1, "retired"), (2, "active")]
        for version, _status in versions:
            for ordinal in range(3):
                chunk_id = _insert_chunk(
                    conn,
                    workspace_id=workspace_id,
                    document_id=document_id,
                    index_version=version,
                    ordinal=ordinal,
                    tag=f"{key}v{version}c{ordinal}",
                )
                chunks[f"{key}v{version}c{ordinal}"] = chunk_id
                if key == "A1" and version == 1:
                    stale.append(chunk_id)

    _insert_run_and_events(conn)
    bindings = _insert_bindings(conn)
    jobs = _insert_jobs(conn, bindings)
    citations = _insert_citations(conn, chunks)

    intent, idempotency_key = _seed_approval(conn)
    approval_id = APPROVAL_A
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    signature = _sign_object(WS_A, OBJECT_KEY_A, "get", "read", expires_at, PRIN_A_OWNER)
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_object_refs "
        "(workspace_id, object_key, principal_id, signature, method, purpose, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (WS_A, OBJECT_KEY_A, PRIN_A_OWNER, signature, "get", "read", expires_at),
    )

    session_tokens = {
        "live": _mint_token(_token_payload(WS_A, PRIN_A_OWNER, 1, ttl=600)),
        "expired": _mint_token(_token_payload(WS_A, PRIN_A_OWNER, 1, ttl=-1)),
        "tampered": _mint_token(_token_payload(WS_A, PRIN_B_OWNER, 1, ttl=600)),
    }

    return SeedData(
        workspaces={"A": WS_A, "B": WS_B, "C": WS_C},
        principals={
            "a_owner": PRIN_A_OWNER,
            "a_member": PRIN_A_MEMBER,
            "a_revoked": PRIN_A_REVOKED,
            "b_owner": PRIN_B_OWNER,
            "c_owner": PRIN_C_OWNER,
        },
        principals_by_role={"owner": PRIN_A_OWNER, "member": PRIN_A_MEMBER},
        documents=documents,
        chunks=chunks,
        stale_chunks=tuple(stale),
        bindings=bindings,
        jobs=jobs,
        runs={"A": RUN_A, "B": RUN_B},
        citations=citations,
        object_key=OBJECT_KEY_A,
        object_signature=signature,
        approval_id=approval_id,
        intent_key=intent.intent_key,
        intent_scope_hash=intent.scope_hash,
        idempotency_key=idempotency_key,
        session_tokens=session_tokens,
    )


def _insert_run_and_events(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_runs (workspace_id, run_id, principal_id) "
        "VALUES (%s, %s, %s), (%s, %s, %s)",
        (WS_A, RUN_A, PRIN_A_OWNER, WS_B, RUN_B, PRIN_B_OWNER),
    )
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_run_events "
        "(workspace_id, run_id, event_id, event_type, terminal, payload) "
        "VALUES (%s, %s, %s, 0, false, '{}'::jsonb)",
        (WS_A, RUN_A, EVENT_A),
    )


def _insert_bindings(conn: psycopg.Connection[Any]) -> dict[str, UUID]:
    payload = contracts.synthesize_principal(
        workspace_id=WS_A,
        principal_id=PRIN_A_OWNER,
        membership_subject_ref="canary:t29:owner",
        role="owner",
        membership_revision=1,
    )
    binding = contracts.ExecutionBinding.create(
        principal=payload,
        policy_snapshot_id=_u("policy:A"),
        execution_binding_id=BINDING_A,
        operation="document.fetch",
        resource_refs=(
            contracts.ResourceRef(
                workspace_id=WS_A, resource_kind="source", resource_id=_u(f"source:{WS_A}:{DOC_A0}")
            ),
        ),
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_execution_bindings "
        "(workspace_id, execution_binding_id, principal_id, operation, resource_kind, "
        " resource_id, parent_run_id) VALUES (%s, %s, %s, %s, 'source', %s, %s)",
        (
            WS_A,
            binding.execution_binding_id,
            binding.principal_id,
            binding.operation,
            binding.resource_refs[0].resource_id,
            RUN_A,
        ),
    )
    return {"doc_fetch": binding.execution_binding_id}


def _insert_jobs(
    conn: psycopg.Connection[Any], bindings: Mapping[str, UUID]
) -> dict[str, UUID]:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_jobs "
        "(workspace_id, job_id, execution_binding_id, run_id, kind) "
        "VALUES (%s, %s, %s, %s, 'fetch')",
        (WS_A, JOB_A, bindings["doc_fetch"], RUN_A),
    )
    return {"A": JOB_A}


def _insert_citations(
    conn: psycopg.Connection[Any], chunks: Mapping[str, UUID]
) -> dict[str, UUID]:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_citations "
        "(workspace_id, citation_id, run_id, chunk_id, handle, status) "
        "VALUES (%s, %s, %s, %s, 1, 'accepted')",
        (WS_A, CITATION_A, RUN_A, chunks["A0v1c0"]),
    )
    return {"A": CITATION_A}


def _seed_approval(conn: psycopg.Connection[Any]) -> tuple[tool_registry.ToolIntent, str]:
    intent, _validated = _build_intent()
    idempotency_key = f"idem:{hashlib.sha256(intent.scope_hash.encode()).hexdigest()[:16]}"
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_approvals "
        "(workspace_id, approval_id, run_id, intent_key, operation, scope_hash, "
        " idempotency_key, requester_id, approver_id, status, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'approved', %s)",
        (
            WS_A,
            APPROVAL_A,
            intent.run_id,
            intent.intent_key,
            intent.name,
            intent.scope_hash,
            idempotency_key,
            PRIN_A_MEMBER,
            PRIN_A_OWNER,
            datetime.now(UTC) + timedelta(minutes=10),
        ),
    )
    return intent, idempotency_key


# ---------------------------------------------------------------------------
# 会话 / 游标 / 对象签名
# ---------------------------------------------------------------------------


def _hmac(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


@contextmanager
def redis_client(url: str | None = None, *, db: int = REDIS_DB) -> Iterator[redis.Redis]:
    # 默认 Redis 地址必须来自环境/显式参数：不能把 PG DSN 改成 redis://（端口不同）。
    # redis-py 的 from_url 取值优先级是 querystring > URL path > db 关键字参数，
    # 因此必须把 URL path 显式改写成 /<db>；否则环境里的 /0 会覆盖 db 参数（T29 曾跑在 DB0）。
    base = url or _redis_url_from_env()
    client = redis.Redis.from_url(_redis_url_with_db(base, db), decode_responses=True)
    try:
        yield client
    finally:
        client.close()


def _redis_url_from_env() -> str:
    import os

    return os.environ.get("PHASE0_REDIS_URL", "redis://127.0.0.1:6380/0")


def _redis_url_with_db(url: str, db: int) -> str:
    """把 Redis URL 的 DB 索引固定为 db（URL path 优先于 from_url 的 db 参数）。"""

    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parsed._replace(path=f"/{db}"))


def flush_prefix(client: redis.Redis, *, prefix: str = REDIS_PREFIX) -> int:
    removed = 0
    for key in client.scan_iter(match=f"{prefix}*"):
        client.delete(key)
        removed += 1
    return removed


def _token_payload(
    workspace_id: UUID, principal_id: UUID, revision: int, *, ttl: int
) -> dict[str, Any]:
    return {
        "workspace_id": str(workspace_id),
        "principal_id": str(principal_id),
        "membership_revision": revision,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat(),
        "nonce": secrets.token_hex(8),
    }


def _mint_token(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{body}.{_hmac(SIGNING_KEY, body)[:16]}"


def _decode_token(token: str) -> dict[str, Any]:
    body, _, signature = token.rpartition(".")
    if not signature or not hmac.compare_digest(_hmac(SIGNING_KEY, body)[:16], signature):
        raise AccessDenied("session token signature invalid", code="TOKEN_FORGED")
    try:
        padding = "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body + padding))
    except (ValueError, json.JSONDecodeError) as exc:  # pragma: no cover - 防御性
        raise AccessDenied("session token malformed", code="TOKEN_MALFORMED") from exc
    expires_at = datetime.fromisoformat(str(data["expires_at"]))
    if expires_at <= datetime.now(UTC):
        raise AccessDenied("session token expired", code="TOKEN_EXPIRED")
    return data


def issue_session(client: redis.Redis, *, payload: Mapping[str, Any]) -> str:
    token = _mint_token(payload)
    client.set(
        f"{REDIS_PREFIX}session:{token}",
        json.dumps(dict(payload), sort_keys=True),
        ex=SESSION_TTL_SECONDS,
    )
    return token


def _live_scope(
    conn: psycopg.Connection[Any], client: redis.Redis, token: str
) -> dict[str, Any]:
    """服务端每次重读当前 Principal；不信任 token 内的 role/scope 声明。"""

    data = _decode_token(token)
    cached = client.get(f"{REDIS_PREFIX}session:{token}")
    if cached is None:
        raise AccessDenied("session token not issued by this server", code="TOKEN_UNKNOWN")
    row = conn.execute(
        f"SELECT status, role, membership_revision FROM {SCHEMA}.phase0_principals "
        "WHERE workspace_id = %s AND principal_id = %s",
        (UUID(str(data["workspace_id"])), UUID(str(data["principal_id"]))),
    ).fetchone()
    if row is None:
        raise AccessDenied("principal not found", code="PRINCIPAL_MISSING")
    if row["status"] != "active":
        raise AccessDenied("principal disabled", code="PRINCIPAL_DISABLED")
    if int(data["membership_revision"]) != int(row["membership_revision"]):
        raise AccessDenied("membership revision stale", code="MEMBERSHIP_REVISION_STALE")
    return {
        "workspace_id": UUID(str(data["workspace_id"])),
        "principal_id": UUID(str(data["principal_id"])),
        "role": row["role"],
        "membership_revision": int(row["membership_revision"]),
    }


def mint_cursor(
    key: bytes,
    *,
    workspace_id: UUID,
    principal_id: UUID,
    revision: int,
    last_id: UUID,
    ttl: int = CURSOR_TTL_SECONDS,
) -> str:
    payload = {
        "workspace_id": str(workspace_id),
        "principal_id": str(principal_id),
        "membership_revision": revision,
        "last_id": str(last_id),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat(),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    encoded = urllib.parse.quote_plus(body.decode())
    return f"{encoded}.{_hmac(key, encoded)[:32]}"


def read_cursor(
    conn: psycopg.Connection[Any], key: bytes, cursor: str
) -> dict[str, Any]:
    body, _, signature = cursor.partition(".")
    if not signature or not hmac.compare_digest(_hmac(key, body)[:32], signature):
        raise AccessDenied("cursor signature invalid", code="CURSOR_FORGED")
    try:
        data = json.loads(urllib.parse.unquote_plus(body))
    except json.JSONDecodeError as exc:  # pragma: no cover - 防御性
        raise AccessDenied("cursor malformed", code="CURSOR_MALFORMED") from exc
    expires_at = datetime.fromisoformat(str(data["expires_at"]))
    if expires_at <= datetime.now(UTC):
        raise AccessDenied("cursor expired", code="CURSOR_EXPIRED")
    row = conn.execute(
        f"SELECT status, membership_revision FROM {SCHEMA}.phase0_principals "
        "WHERE workspace_id = %s AND principal_id = %s",
        (UUID(str(data["workspace_id"])), UUID(str(data["principal_id"]))),
    ).fetchone()
    if row is None or row["status"] != "active":
        raise AccessDenied("cursor principal revoked", code="CURSOR_REVOKED")
    if int(data["membership_revision"]) != int(row["membership_revision"]):
        raise AccessDenied("cursor stale after membership change", code="CURSOR_STALE")
    return data


def _sign_object(
    workspace_id: UUID,
    object_key: str,
    method: str,
    purpose: str,
    expires_at: datetime,
    principal_id: UUID,
) -> str:
    message = "|".join(
        (
            str(workspace_id),
            object_key,
            method.lower(),
            purpose,
            expires_at.astimezone(UTC).isoformat(),
            str(principal_id),
        )
    )
    return _hmac(SIGNING_KEY, message)[:40]


def authorize_object_access(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID,
    object_key: str,
    method: str,
    purpose: str,
    signature: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """每次访问重新授权 + 固定单对象/方法/用途；签名不是目录前缀能力。"""

    row = conn.execute(
        f"SELECT principal_id, signature, method, purpose, expires_at "
        f"FROM {SCHEMA}.phase0_object_refs WHERE workspace_id = %s AND object_key = %s",
        (workspace_id, object_key),
    ).fetchone()
    if row is None:
        raise AccessDenied("object not visible in scope", code="OBJECT_NOT_IN_SCOPE")
    moment = now or datetime.now(UTC)
    if row["expires_at"] <= moment:
        raise AccessDenied("object signature expired", code="OBJECT_SIGNATURE_EXPIRED")
    expected = _sign_object(
        workspace_id,
        object_key,
        str(row["method"]),
        str(row["purpose"]),
        row["expires_at"],
        UUID(str(row["principal_id"])),
    )
    if not hmac.compare_digest(expected, str(row["signature"])):
        raise AccessDenied("stored signature invalid", code="OBJECT_SIGNATURE_INVALID")
    if not hmac.compare_digest(expected, signature):
        raise AccessDenied("caller signature invalid", code="OBJECT_SIGNATURE_MISMATCH")
    if str(row["method"]).lower() != method.lower():
        raise AccessDenied("method not covered by signature", code="OBJECT_METHOD_MISMATCH")
    if str(row["purpose"]) != purpose:
        raise AccessDenied("purpose not covered by signature", code="OBJECT_PURPOSE_MISMATCH")
    principal = conn.execute(
        f"SELECT status FROM {SCHEMA}.phase0_principals "
        "WHERE workspace_id = %s AND principal_id = %s",
        (workspace_id, principal_id),
    ).fetchone()
    if principal is None or principal["status"] != "active":
        raise AccessDenied("principal revoked", code="PRINCIPAL_DISABLED")
    return {"object_key": object_key, "method": method, "purpose": purpose}


# ---------------------------------------------------------------------------
# 引用 / 审批 / Job 恢复的授权口径（镜像各 owner 服务）
# ---------------------------------------------------------------------------


def visible_chunk_ids(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, principal_id: UUID
) -> list[UUID]:
    """当前可见 chunk：active 版本 + active 状态 + winning attempt + KB 授权。"""

    rows = conn.execute(
        f"SELECT c.chunk_id FROM {SCHEMA}.phase0_chunks AS c "
        f"JOIN {SCHEMA}.phase0_documents AS d "
        "  ON d.workspace_id = c.workspace_id AND d.document_id = c.document_id "
        f"JOIN {SCHEMA}.phase0_document_versions AS v "
        "  ON v.workspace_id = c.workspace_id AND v.document_id = c.document_id "
        " AND v.index_version = c.index_version "
        f"LEFT JOIN {SCHEMA}.phase0_kb_grants AS g "
        "  ON g.workspace_id = d.workspace_id AND g.kb_id = d.kb_id "
        " AND g.principal_id = %s AND g.status = 'active' "
        f"JOIN {SCHEMA}.phase0_principals AS p "
        "  ON p.workspace_id = %s AND p.principal_id = %s AND p.status = 'active' "
        "WHERE c.workspace_id = %s "
        "  AND d.active_version = c.index_version "
        "  AND v.status = 'active' "
        "  AND v.winning_attempt_id = c.attempt_id "
        "  AND (g.principal_id IS NOT NULL OR p.role = 'owner') "
        "ORDER BY c.chunk_id",
        (principal_id, workspace_id, principal_id, workspace_id),
    ).fetchall()
    return [UUID(str(row["chunk_id"])) for row in rows]


def _chunk_visible(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID,
    chunk_id: UUID,
) -> bool:
    visible = visible_chunk_ids(
        conn, workspace_id=workspace_id, principal_id=principal_id
    )
    return chunk_id in set(visible)


@dataclass(frozen=True, slots=True)
class CitationEvidence:
    handle: int
    chunk_id: UUID
    document_id: UUID
    index_version: int


@dataclass(frozen=True, slots=True)
class CitationCheck:
    accepted: tuple[int, ...]
    rejections: tuple[str, ...]


_CHUNK_REF_RE: Final = re.compile(r"chunk:([0-9a-f-]{36})", re.IGNORECASE)
_HANDLE_RE: Final = re.compile(r"\[\[(\d+)\]\]")


def parse_citation_handles(answer_text: str) -> tuple[int, ...]:
    return tuple(int(match) for match in _HANDLE_RE.findall(answer_text))


def authorize_citation(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID,
    evidence: Sequence[CitationEvidence],
    answer_text: str,
) -> CitationCheck:
    accepted: list[int] = []
    rejections: list[str] = []
    by_handle = {item.handle: item for item in evidence}
    seen: set[int] = set()
    for handle in parse_citation_handles(answer_text):
        if handle in seen:
            rejections.append("DUPLICATE_HANDLE")
            continue
        seen.add(handle)
        item = by_handle.get(handle)
        if item is None:
            rejections.append("HANDLE_NOT_IN_EVIDENCE")
            continue
        if not _chunk_visible(
            conn, workspace_id=workspace_id, principal_id=principal_id, chunk_id=item.chunk_id
        ):
            rejections.append("CHUNK_NOT_VISIBLE")
            continue
        accepted.append(handle)
    for raw in _CHUNK_REF_RE.findall(answer_text):
        chunk_id = UUID(raw)
        in_evidence = any(item.chunk_id == chunk_id for item in evidence)
        if not in_evidence:
            rejections.append("CHUNK_REF_NOT_IN_EVIDENCE")
            continue
        if not _chunk_visible(
            conn, workspace_id=workspace_id, principal_id=principal_id, chunk_id=chunk_id
        ):
            rejections.append("CHUNK_REF_NOT_VISIBLE")
    return CitationCheck(accepted=tuple(accepted), rejections=tuple(rejections))


@dataclass(frozen=True, slots=True)
class ApprovalView:
    approval_id: UUID
    intent_key: str
    operation: str
    scope_hash: str
    idempotency_key: str
    run_id: UUID
    requester_id: UUID
    approver_id: UUID
    status: str
    expires_at: datetime


def authorize_approval(
    conn: psycopg.Connection[Any],
    *,
    intent: tool_registry.ToolIntent,
    approval_id: UUID,
    now: datetime | None = None,
) -> UUID:
    """重读审批行并逐项比对；任何字段漂移/状态/期限不满足都拒绝。"""

    row = conn.execute(
        f"SELECT approval_id, intent_key, operation, scope_hash, idempotency_key, run_id, "
        "requester_id, approver_id, status, expires_at "
        f"FROM {SCHEMA}.phase0_approvals WHERE workspace_id = %s AND approval_id = %s",
        (intent.workspace_id, approval_id),
    ).fetchone()
    if row is None:
        raise AccessDenied("approval missing", code="APPROVAL_MISSING")
    view = ApprovalView(
        approval_id=UUID(str(row["approval_id"])),
        intent_key=str(row["intent_key"]),
        operation=str(row["operation"]),
        scope_hash=str(row["scope_hash"]),
        idempotency_key=str(row["idempotency_key"]),
        run_id=UUID(str(row["run_id"])),
        requester_id=UUID(str(row["requester_id"])),
        approver_id=UUID(str(row["approver_id"])),
        status=str(row["status"]),
        expires_at=row["expires_at"],
    )
    moment = now or datetime.now(UTC)
    if not hmac.compare_digest(view.intent_key, intent.intent_key):
        raise AccessDenied("approval intent mismatch", code="APPROVAL_INTENT_MISMATCH")
    if view.operation != intent.name:
        raise AccessDenied("approval operation mismatch", code="APPROVAL_OPERATION_MISMATCH")
    if not hmac.compare_digest(view.scope_hash, intent.scope_hash):
        raise AccessDenied("approval scope mismatch", code="APPROVAL_SCOPE_MISMATCH")
    if intent.run_id is None or view.run_id != intent.run_id:
        raise AccessDenied("approval run mismatch", code="APPROVAL_RUN_MISMATCH")
    if view.idempotency_key != derive_idempotency_key(intent):
        raise AccessDenied("approval idempotency mismatch", code="APPROVAL_IDEMPOTENCY_MISMATCH")
    if view.status != "approved":
        raise AccessDenied("approval not approved", code="APPROVAL_STATUS_INVALID")
    if view.expires_at <= moment:
        raise AccessDenied("approval expired", code="APPROVAL_EXPIRED")
    if view.requester_id == view.approver_id:
        raise AccessDenied("self review is not allowed", code="APPROVAL_SELF_REVIEW")
    for principal_id in (view.requester_id, view.approver_id):
        principal = conn.execute(
            f"SELECT status, role FROM {SCHEMA}.phase0_principals "
            "WHERE workspace_id = %s AND principal_id = %s",
            (intent.workspace_id, principal_id),
        ).fetchone()
        if principal is None or principal["status"] != "active":
            raise AccessDenied("approver or requester disabled", code="APPROVAL_PRINCIPAL_INVALID")
    approver = conn.execute(
        f"SELECT role FROM {SCHEMA}.phase0_principals "
        "WHERE workspace_id = %s AND principal_id = %s",
        (intent.workspace_id, view.approver_id),
    ).fetchone()
    if approver is None or approver["role"] != "owner":
        raise AccessDenied("approver is not an owner", code="APPROVAL_APPROVER_NOT_OWNER")
    return view.approval_id


def derive_idempotency_key(intent: tool_registry.ToolIntent) -> str:
    return f"idem:{hashlib.sha256(intent.scope_hash.encode()).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class RestoreContext:
    workspace_id: UUID
    execution_binding_id: UUID
    principal_id: UUID
    role: str
    membership_revision: int
    payload_role: str | None


def authorize_job_restore(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    job_id: UUID,
    payload: Mapping[str, Any] | None = None,
) -> RestoreContext:
    """无 Run Job 恢复：只凭 DB binding + 当前 Principal，不信 payload 的 role。"""

    claims = contracts.JobPayloadClaims.from_untrusted_payload(payload or {})
    if claims.declared_role is not None:
        # role 仅作可核对输入，绝不参与授权；这里记录以证明“读到但不用”。
        pass
    job = conn.execute(
        f"SELECT workspace_id, execution_binding_id FROM {SCHEMA}.phase0_jobs "
        "WHERE workspace_id = %s AND job_id = %s",
        (workspace_id, job_id),
    ).fetchone()
    if job is None:
        raise AccessDenied("job not visible in scope", code="JOB_NOT_IN_SCOPE")
    if claims.claimed_workspace_id is not None and claims.claimed_workspace_id != workspace_id:
        raise AccessDenied("job payload workspace mismatch", code="JOB_PAYLOAD_MISMATCH")
    binding = conn.execute(
        f"SELECT workspace_id, principal_id, operation, expires_at "
        f"FROM {SCHEMA}.phase0_execution_bindings "
        "WHERE workspace_id = %s AND execution_binding_id = %s",
        (workspace_id, UUID(str(job["execution_binding_id"]))),
    ).fetchone()
    if binding is None:
        raise AccessDenied("execution binding missing", code="BINDING_MISSING")
    principal = conn.execute(
        f"SELECT principal_id, status, role, membership_revision "
        f"FROM {SCHEMA}.phase0_principals "
        "WHERE workspace_id = %s AND principal_id = %s",
        (workspace_id, UUID(str(binding["principal_id"]))),
    ).fetchone()
    if principal is None or principal["status"] != "active":
        raise AccessDenied("execution principal revoked", code="PRINCIPAL_DISABLED")
    if claims.claimed_principal_id is not None and claims.claimed_principal_id != UUID(
        str(principal["principal_id"])
    ):
        raise AccessDenied("job payload principal mismatch", code="JOB_PAYLOAD_MISMATCH")
    return RestoreContext(
        workspace_id=workspace_id,
        execution_binding_id=UUID(str(job["execution_binding_id"])),
        principal_id=UUID(str(principal["principal_id"])),
        role=str(principal["role"]),
        membership_revision=int(principal["membership_revision"]),
        payload_role=claims.declared_role,
    )


# ---------------------------------------------------------------------------
# 探测运行器
# ---------------------------------------------------------------------------


def _sqlstate(exc: BaseException) -> str:
    candidate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    text = str(candidate or "00000")
    return text if _SQLSTATE_RE.match(text) else "00000"


def _classify(
    expected: str, refused: bool, observed: str
) -> bool:
    return (expected == "deny" and refused) or (expected == "allow" and not refused)


def _record(
    log: ProbeLog,
    *,
    category: str,
    probe_id: str,
    vector: str,
    expected: str,
    refused: bool,
    observed: str,
    evidence_ref: str = "",
) -> None:
    log.add(
        category,
        probe_id,
        vector,
        expected,
        observed,
        refused=refused,
        evidence_ref=evidence_ref,
    )


def _expect_denied(
    action: Callable[..., Any], *args: Any, details: str = ""
) -> tuple[bool, str]:
    """执行探测动作：被拒绝（AccessDenied/数据库/HTTPS 错误）即安全。"""

    try:
        action(*args)
    except AccessDenied as exc:
        return True, f"denied:{exc.code}"
    except SsrfBlocked as exc:
        return True, f"blocked:{exc.code}"
    except psycopg.Error as exc:
        return True, f"denied:sqlstate={_sqlstate(exc)}"
    except (contracts.ScopeContractError, tool_registry.ToolContractError) as exc:
        return True, f"denied:{getattr(exc, 'code', type(exc).__name__)}"
    except (OSError, http.client.HTTPException) as exc:
        return True, f"blocked:{type(exc).__name__}"
    except SecurityProbeError as exc:
        return True, f"blocked:{exc.code}"
    return False, details or "allowed"


# -- 1. 跨空间读写 -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrossTarget:
    table: str
    key_columns: tuple[str, ...]
    values: tuple[Any, ...]
    vector: str


CROSS_TARGETS: Final = (
    CrossTarget("phase0_principals", ("principal_id",), (PRIN_A_OWNER,), "victim principal"),
    CrossTarget("phase0_documents", ("document_id",), (DOC_A0,), "victim document"),
    CrossTarget(
        "phase0_document_versions",
        ("document_id", "index_version"),
        (DOC_A1, 1),
        "victim retired version",
    ),
    CrossTarget("phase0_chunks", ("chunk_id",), (_chunk_id(WS_A, DOC_A0, 1, 0),), "victim chunk"),
    CrossTarget("phase0_approvals", ("approval_id",), (APPROVAL_A,), "victim approval"),
    CrossTarget(
        "phase0_run_events",
        ("run_id", "event_id"),
        (RUN_A, EVENT_A),
        "victim run event",
    ),
    CrossTarget("phase0_jobs", ("job_id",), (JOB_A,), "victim job"),
    CrossTarget("phase0_object_refs", ("object_key",), (OBJECT_KEY_A,), "victim object ref"),
    CrossTarget("phase0_citations", ("citation_id",), (CITATION_A,), "victim citation"),
)


def _cross_ops(target: CrossTarget) -> list[tuple[str, str, tuple[Any, ...]]]:
    where = " AND ".join(f"{column} = %s" for column in target.key_columns)
    params = (WS_A, *target.values)
    return [
        (
            "select_one",
            f"SELECT 1 AS hit FROM {SCHEMA}.{target.table} WHERE workspace_id = %s AND {where}",
            params,
        ),
        (
            "select_list",
            f"SELECT workspace_id FROM {SCHEMA}.{target.table} "
            f"WHERE workspace_id = %s AND {where} LIMIT 5",
            params,
        ),
        (
            "update",
            f"UPDATE {SCHEMA}.{target.table} SET workspace_id = workspace_id "
            f"WHERE workspace_id = %s AND {where}",
            params,
        ),
        (
            "delete",
            f"DELETE FROM {SCHEMA}.{target.table} WHERE workspace_id = %s AND {where}",
            params,
        ),
    ]


def probe_cross_workspace(
    conn: psycopg.Connection[Any],
    seed: SeedData,
    log: ProbeLog,
    *,
    rounds_per_op: int = 10,
) -> int:
    """攻击者 B 在自身 Scope 下读/写 A 的资源；期望 0 命中、0 行受影响。"""

    attempted = 0
    control_rows = _scoped_count(
        conn,
        workspace_id=WS_A,
        principal_id=PRIN_A_OWNER,
        statement=f"SELECT count(*) AS hit FROM {SCHEMA}.phase0_chunks",
    )
    _record(
        log,
        category="cross_workspace",
        probe_id="xw-control-owner-visible",
        vector="positive control：A owner 在自己的 Scope 内应看到 A 的 chunk",
        expected="allow",
        refused=control_rows == 0,
        observed=f"rows={control_rows}",
        evidence_ref=f"{SCHEMA}.phase0_chunks",
    )
    for offset in range(rounds_per_op):
        for target in CROSS_TARGETS:
            for op_name, statement, params in _cross_ops(target):
                attempted += 1
                probe_id = f"xw-{target.table}-{op_name}-{offset:02d}"
                try:
                    rows, affected = _run_user_statement(
                        conn,
                        workspace_id=WS_B,
                        principal_id=PRIN_B_OWNER,
                        statement=statement,
                        params=params,
                    )
                except psycopg.Error as exc:
                    _record(
                        log,
                        category="cross_workspace",
                        probe_id=probe_id,
                        vector=f"B 空间 {op_name} A 的 {target.vector}",
                        expected="deny",
                        refused=True,
                        observed=f"denied:sqlstate={_sqlstate(exc)}",
                        evidence_ref=f"{SCHEMA}.{target.table}",
                    )
                    continue
                hit = rows + affected
                _record(
                    log,
                    category="cross_workspace",
                    probe_id=probe_id,
                    vector=f"B 空间 {op_name} A 的 {target.vector}",
                    expected="deny",
                    refused=hit == 0,
                    observed=f"rows={rows} affected={affected}",
                    evidence_ref=f"{SCHEMA}.{target.table}",
                )
    return attempted


# -- 2. 撤权与旧凭据 ---------------------------------------------------------


def probe_revocation(
    admin: psycopg.Connection[Any],
    app: psycopg.Connection[Any],
    seed: SeedData,
    log: ProbeLog,
    *,
    client: redis.Redis,
) -> int:
    """撤权后旧 session/token/游标/审批/Job 恢复立即失效；恢复后按当前状态放行。"""

    attempted = 0
    live_token = seed.session_tokens["live"]

    def set_principal(principal_id: UUID, *, status: str, revision: int) -> None:
        admin.execute(
            f"UPDATE {SCHEMA}.phase0_principals SET status = %s, membership_revision = %s "
            "WHERE workspace_id = %s AND principal_id = %s",
            (status, revision, WS_A, principal_id),
        )
        admin.commit()

    def visible_rows(principal_id: UUID) -> int:
        return _scoped_count(
            app,
            workspace_id=WS_A,
            principal_id=principal_id,
            statement=f"SELECT count(*) AS hit FROM {SCHEMA}.phase0_chunks",
        )

    def baseline() -> None:
        data = _live_scope(admin, client, live_token)
        with scoped_transaction(
            app,
            workspace_id=data["workspace_id"],
            principal_id=data["principal_id"],
        ) as scoped:
            scoped.execute(f"SELECT 1 FROM {SCHEMA}.phase0_chunks LIMIT 1").fetchone()

    def old_scope_reuse() -> None:
        if visible_rows(PRIN_A_OWNER) > 0:
            return  # 越权：撤权后旧 Scope 仍可见
        raise AccessDenied("scope sees no rows", code="NO_VISIBLE_ROWS")

    def old_citation_reuse() -> None:
        check = authorize_citation(
            admin,
            workspace_id=WS_A,
            principal_id=PRIN_A_OWNER,
            evidence=_revocation_evidence(seed),
            answer_text="[[1]]",
        )
        if check.accepted and not check.rejections:
            return  # 越权：撤权后引用仍被接受
        raise AccessDenied("citation rejected", code="CITATION_REJECTED")

    def restored_citation() -> None:
        check = authorize_citation(
            admin,
            workspace_id=WS_A,
            principal_id=PRIN_A_OWNER,
            evidence=_revocation_evidence(seed),
            answer_text="[[1]]",
        )
        if check.rejections or not check.accepted:
            raise AccessDenied("citation rejected after restore", code="CITATION_REJECTED")

    def old_job_restore() -> None:
        authorize_job_restore(admin, workspace_id=WS_A, job_id=JOB_A)

    def old_approval() -> None:
        intent, _ = _build_intent()
        authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)

    def old_cursor() -> None:
        cursor = mint_cursor(
            SIGNING_KEY,
            workspace_id=WS_A,
            principal_id=PRIN_A_OWNER,
            revision=1,
            last_id=seed.chunks["A0v1c0"],
        )
        read_cursor(admin, SIGNING_KEY, cursor)

    def revoked_kb_grant() -> None:
        rows = _scoped_count(
            app,
            workspace_id=WS_A,
            principal_id=PRIN_A_MEMBER,
            statement=(
                f"SELECT count(*) AS hit FROM {SCHEMA}.phase0_chunks AS c "
                f"JOIN {SCHEMA}.phase0_documents AS d "
                "  ON d.workspace_id = c.workspace_id AND d.document_id = c.document_id "
                f"JOIN {SCHEMA}.phase0_kb_grants AS g "
                "  ON g.workspace_id = d.workspace_id AND g.kb_id = d.kb_id "
                " AND g.principal_id = %s AND g.status = 'active' "
                "WHERE c.workspace_id = %s AND d.kb_id = %s"
            ),
        )
        if rows > 0:
            return  # 越权：KB 授权撤销后仍可见
        raise AccessDenied("revoked grant hides rows", code="NO_VISIBLE_ROWS")

    def forged_scope_header() -> None:
        claims = contracts.JobPayloadClaims.from_untrusted_payload(
            {"workspace_id": str(WS_B), "principal_id": str(PRIN_B_OWNER)}
        )
        if claims.claimed_workspace_id == WS_A:
            return  # 越权：服务端把客户端声明当成授权事实
        raise AccessDenied("client scope claim ignored", code="SCOPE_CLAIM_IGNORED")

    def restored_scope(token: str) -> None:
        data = _live_scope(admin, client, token)
        with scoped_transaction(
            app,
            workspace_id=data["workspace_id"],
            principal_id=data["principal_id"],
        ) as scoped:
            scoped.execute(f"SELECT 1 FROM {SCHEMA}.phase0_chunks LIMIT 1").fetchone()

    def restored_job() -> None:
        authorize_job_restore(admin, workspace_id=WS_A, job_id=JOB_A)

    # 1) 撤权前正向对照。
    attempted += 1
    refused, observed = _expect_denied(baseline)
    _record(
        log,
        category="revocation",
        probe_id="rev-baseline-live",
        vector="撤权前 session 可读（正向对照）",
        expected="allow",
        refused=refused,
        observed=observed,
        evidence_ref=f"{SCHEMA}.phase0_principals",
    )

    # 2) 撤权窗口：principal disabled，旧凭据继续使用。
    set_principal(PRIN_A_OWNER, status="disabled", revision=2)
    window_cases: list[tuple[str, str, Callable[[], Any]]] = [
        ("rev-disabled-scope", "撤权后复用旧 Scope 读 chunk", old_scope_reuse),
        ("rev-disabled-citation", "撤权后复用旧 citation", old_citation_reuse),
        ("rev-disabled-job", "撤权后 Job 恢复", old_job_restore),
        ("rev-stale-token", "撤权后旧 session token",
         lambda: _live_scope(admin, client, live_token)),
        ("rev-expired-token", "过期 session token",
         lambda: _live_scope(admin, client, seed.session_tokens["expired"])),
        ("rev-tampered-token", "自签未登记 session（攻击者本地伪造）",
         lambda: _live_scope(admin, client, seed.session_tokens["tampered"])),
        ("rev-stale-approval", "撤权后复用旧审批", old_approval),
        ("rev-old-cursor", "撤权后旧游标", old_cursor),
        ("rev-kb-grant", "KB 授权撤销后检索", revoked_kb_grant),
        ("rev-forged-scope-header", "body 自报 workspace/principal", forged_scope_header),
    ]
    for probe_id, vector, action in window_cases:
        attempted += 1
        refused, observed = _expect_denied(action)
        _record(
            log,
            category="revocation",
            probe_id=probe_id,
            vector=vector,
            expected="deny",
            refused=refused,
            observed=observed,
            evidence_ref=f"{SCHEMA}.phase0_principals",
        )

    # 3) 恢复权限并签发新 session（membership_revision=3）。
    set_principal(PRIN_A_OWNER, status="active", revision=3)
    fresh_token = issue_session(
        client, payload=_token_payload(WS_A, PRIN_A_OWNER, 3, ttl=600)
    )
    restored_cases: list[tuple[str, str, Callable[[], Any]]] = [
        ("rev-restored-scope", "恢复后新 session 放行", lambda: restored_scope(fresh_token)),
        ("rev-restored-citation", "恢复后新引用放行", restored_citation),
        ("rev-restored-job", "恢复后 Job 恢复放行", restored_job),
    ]
    for probe_id, vector, action in restored_cases:
        attempted += 1
        refused, observed = _expect_denied(action)
        _record(
            log,
            category="revocation",
            probe_id=probe_id,
            vector=vector,
            expected="allow",
            refused=refused,
            observed=observed,
            evidence_ref=f"{SCHEMA}.phase0_principals",
        )
    return attempted


def _revocation_evidence(seed: SeedData) -> list[CitationEvidence]:
    return [
        CitationEvidence(
            handle=1,
            chunk_id=seed.chunks["A0v1c0"],
            document_id=DOC_A0,
            index_version=1,
        )
    ]


def _build_intent(
    *, run_id: UUID = RUN_A, request_id: UUID | None = None
) -> tuple[tool_registry.ToolIntent, tool_registry.ValidatedArguments]:
    artifact_id = request_id or _u("request:A:1")
    principal = contracts.synthesize_principal(
        workspace_id=WS_A,
        principal_id=PRIN_A_OWNER,
        membership_subject_ref="canary:t29:owner",
        role="owner",
        membership_revision=1,
    )
    workspace_scope = contracts.issue_scope(principal, policy_snapshot_id=_u("policy:A"))
    registry = tool_registry.build_phase0_registry()
    return tool_registry.prepare_intent(
        registry,
        scope=workspace_scope,
        resource_refs=(
            contracts.ResourceRef(
                workspace_id=WS_A,
                resource_kind="source",
                resource_id=_u(f"source:{WS_A}:{DOC_A0}"),
            ),
        ),
        response_artifact_id=artifact_id,
        call_ordinal=0,
        name="site.idem_write",
        arguments={"idempotency_key": "idem-p0-t29-0001", "body": {"text": "canary"}},
        run_id=run_id,
    )


def _set_approval(
    admin: psycopg.Connection[Any], *, intent: tool_registry.ToolIntent, **fields: Any
) -> None:
    columns = ", ".join(f"{name} = %s" for name in fields)
    values = list(fields.values())
    admin.execute(
        f"UPDATE {SCHEMA}.phase0_approvals SET {columns} "
        "WHERE workspace_id = %s AND approval_id = %s",
        (*values, WS_A, APPROVAL_A),
    )
    admin.commit()


def _reset_approval(admin: psycopg.Connection[Any], *, intent: tool_registry.ToolIntent) -> None:
    admin.execute(
        f"UPDATE {SCHEMA}.phase0_approvals SET intent_key = %s, operation = %s, "
        "scope_hash = %s, idempotency_key = %s, run_id = %s, requester_id = %s, "
        "approver_id = %s, status = 'approved', expires_at = %s "
        "WHERE workspace_id = %s AND approval_id = %s",
        (
            intent.intent_key,
            intent.name,
            intent.scope_hash,
            derive_idempotency_key(intent),
            intent.run_id,
            PRIN_A_MEMBER,
            PRIN_A_OWNER,
            datetime.now(UTC) + timedelta(minutes=10),
            WS_A,
            APPROVAL_A,
        ),
    )
    admin.commit()


def probe_payload_tampering(
    admin: psycopg.Connection[Any], seed: SeedData, log: ProbeLog
) -> int:
    """篡改审批参数/scope/有效期/幂等键/主体/工具版本，两条路径都必须拒绝。"""

    intent, _validated = _build_intent()
    attempted = 0

    def run_path(action: Callable[[], Any]) -> tuple[bool, str]:
        return _expect_denied(action)

    approval_cases: list[tuple[str, str, Callable[[], None]]] = []

    def case_control() -> None:
        approval = tool_registry.ApprovalRef(
            approval_id=APPROVAL_A,
            intent_key=intent.intent_key,
            status="approved",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        tool_registry.assert_executable(intent, approval=approval)

    def case_operation() -> None:
        approval = tool_registry.ApprovalRef(
            approval_id=APPROVAL_A,
            intent_key=intent.intent_key,
            status="approved",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        other, _ = _build_intent(run_id=RUN_A, request_id=_u("request:A:2"))
        tool_registry.assert_executable(other, approval=approval)

    def case_expired() -> None:
        approval = tool_registry.ApprovalRef(
            approval_id=APPROVAL_A,
            intent_key=intent.intent_key,
            status="approved",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        tool_registry.assert_executable(intent, approval=approval)

    def case_status() -> None:
        approval = tool_registry.ApprovalRef(
            approval_id=APPROVAL_A,
            intent_key=intent.intent_key,
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        tool_registry.assert_executable(intent, approval=approval)

    approval_cases.extend(
        [
            ("pl-approval-control", "正向对照：完整审批放行", case_control),
            ("pl-approval-operation", "工具操作/参数版本漂移", case_operation),
            ("pl-approval-expired", "审批已过期", case_expired),
            ("pl-approval-status", "审批未批准（pending）", case_status),
        ]
    )

    for probe_id, vector, action in approval_cases:
        attempted += 1
        refused, observed = run_path(action)
        _record(
            log,
            category="payload",
            probe_id=probe_id,
            vector=vector,
            expected="allow" if probe_id.endswith("control") else "deny",
            refused=refused,
            observed=observed,
            evidence_ref=f"{SCHEMA}.phase0_approvals",
        )

    db_variants: list[tuple[str, str, Callable[[], Any]]] = []

    def db_control() -> None:
        _reset_approval(admin, intent=intent)
        authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)

    def db_intent() -> None:
        _set_approval(admin, intent=intent, intent_key=hashlib.sha256(b"forged").hexdigest())
        authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)

    def db_operation() -> None:
        _set_approval(admin, intent=intent, operation="site.read_counter")
        authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)

    def db_scope() -> None:
        _set_approval(admin, intent=intent, scope_hash=hashlib.sha256(b"forged-scope").hexdigest())
        authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)

    def db_idempotency() -> None:
        _set_approval(admin, intent=intent, idempotency_key="idem:forged")
        authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)

    def db_run() -> None:
        _set_approval(admin, intent=intent, run_id=RUN_B)
        authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)

    def db_status() -> None:
        _set_approval(admin, intent=intent, status="revoked")
        authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)

    def db_expired() -> None:
        _set_approval(
            admin, intent=intent, expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)

    def db_missing() -> None:
        authorize_approval(admin, intent=intent, approval_id=uuid4())

    def db_approver_disabled() -> None:
        admin.execute(
            f"UPDATE {SCHEMA}.phase0_principals SET status = 'disabled' "
            "WHERE workspace_id = %s AND principal_id = %s",
            (WS_A, PRIN_A_OWNER),
        )
        admin.commit()
        try:
            authorize_approval(admin, intent=intent, approval_id=APPROVAL_A)
        finally:
            admin.execute(
                f"UPDATE {SCHEMA}.phase0_principals SET status = 'active' "
                "WHERE workspace_id = %s AND principal_id = %s",
                (WS_A, PRIN_A_OWNER),
            )
            admin.commit()

    db_variants.extend(
        [
            ("pl-db-control", "DB 审批逐项比对正向对照", db_control),
            ("pl-db-intent", "篡改审批 intent_key", db_intent),
            ("pl-db-operation", "篡改审批 operation", db_operation),
            ("pl-db-scope", "篡改审批 scope_hash", db_scope),
            ("pl-db-idempotency", "篡改幂等键", db_idempotency),
            ("pl-db-run", "审批绑定到其它 run", db_run),
            ("pl-db-status", "审批状态非 approved", db_status),
            ("pl-db-expired", "审批过期", db_expired),
            ("pl-db-missing", "审批记录不存在", db_missing),
            ("pl-db-approver-disabled", "审批人撤权", db_approver_disabled),
        ]
    )

    for probe_id, vector, action in db_variants:
        attempted += 1
        refused, observed = run_path(action)
        if probe_id != "pl-db-control":
            _reset_approval(admin, intent=intent)
        _record(
            log,
            category="payload",
            probe_id=probe_id,
            vector=vector,
            expected="allow" if probe_id.endswith("control") else "deny",
            refused=refused,
            observed=observed,
            evidence_ref=f"{SCHEMA}.phase0_approvals",
        )

    citation_evidence = [
        CitationEvidence(
            handle=1, chunk_id=seed.chunks["A0v1c0"], document_id=DOC_A0, index_version=1
        )
    ]
    citation_cases: list[tuple[str, str, Callable[[], Any], str]] = [
        (
            "pl-citation-control",
            "正向对照：合法 handle 放行",
            lambda: authorize_citation(
                admin,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=citation_evidence,
                answer_text="[[1]]",
            ),
            "allow",
        ),
        (
            "pl-citation-cross-space",
            "篡改引用指向 B 空间 chunk",
            lambda: authorize_citation(
                admin,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=citation_evidence,
                answer_text=f"chunk:{seed.chunks['B0v1c0']}",
            ),
            "deny",
        ),
        (
            "pl-citation-stale",
            "篡改引用指向 retired 旧版本 chunk",
            lambda: authorize_citation(
                admin,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=citation_evidence,
                answer_text=f"chunk:{seed.stale_chunks[0]}",
            ),
            "deny",
        ),
        (
            "pl-citation-unknown",
            "篡改引用指向不存在 chunk",
            lambda: authorize_citation(
                admin,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=citation_evidence,
                answer_text=f"chunk:{uuid4()}",
            ),
            "deny",
        ),
        (
            "pl-citation-handle",
            "handle 超出本轮 evidence",
            lambda: authorize_citation(
                admin,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=citation_evidence,
                answer_text="[[9]]",
            ),
            "deny",
        ),
        (
            "pl-citation-duplicate",
            "重复 handle",
            lambda: authorize_citation(
                admin,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=citation_evidence,
                answer_text="[[1]] [[1]]",
            ),
            "deny",
        ),
    ]
    for probe_id, vector, action, expected in citation_cases:
        attempted += 1
        refused, observed = _expect_denied(_citation_guard, action)
        _record(
            log,
            category="payload",
            probe_id=probe_id,
            vector=vector,
            expected=expected,
            refused=refused,
            observed=observed,
            evidence_ref=f"{SCHEMA}.phase0_citations",
        )
    return attempted


def _citation_guard(action: Callable[[], CitationCheck]) -> None:
    """安全 = 引用被拒绝或至少出现 rejection；越权 = 全部接受且无 rejection。"""

    check = action()
    if check.accepted and not check.rejections:
        return
    raise AccessDenied("citation rejected", code="CITATION_REJECTED")


# -- 4. SSRF -----------------------------------------------------------------


class _ControlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 覆盖基类签名
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = self.path.split("?")[0]
        if path == "/":
            self._respond(200, b"p0-t29-control-ok")
        elif path == "/slow":
            time.sleep(1.5)
            self._respond(200, b"slow")
        elif path == "/huge":
            self._respond(200, b"x" * (MAX_RESPONSE_BYTES + 4096))
        elif path.startswith("/redirect/"):
            try:
                hops = int(path.rsplit("/", 1)[1])
            except ValueError:
                hops = 0
            if hops <= 0:
                self._respond(200, b"redirect-end")
            else:
                self._redirect(f"http://{CONTROL_HOST}:{CONTROL_PORT}/redirect/{hops - 1}")
        elif path == "/redirect-metadata":
            self._redirect("http://169.254.169.254/latest/meta-data/")
        elif path == "/redirect-private":
            self._redirect("http://10.0.0.5/secret")
        elif path == "/redirect-loop":
            self._redirect(f"http://{CONTROL_HOST}:{CONTROL_PORT}/redirect-loop")
        elif path == "/redirect-file":
            self._redirect("file:///etc/passwd")
        else:
            self._respond(404, b"not-found")

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()


class _ControlServer:
    def __init__(self, *, host: str = CONTROL_HOST, port: int = CONTROL_PORT) -> None:
        self._server = ThreadingHTTPServer((host, port), _ControlHandler)
        self._server.daemon_threads = True
        self._server.handle_error = lambda *args: None
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _ControlServer:
        self._thread.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """把连接固定到已校验的目的 IP，避免 DNS 重绑定。"""

    def __init__(
        self, host: str, port: int, *, pinned_ip: str, timeout: float
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


def _validate_outbound_url(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    allowed_ports: frozenset[int],
    dns_resolver: Callable[[str, int], list[str]] | None = None,
) -> tuple[str, int, str, str]:
    """每跳校验：scheme/userinfo/host/port/私网地址；返回 (host, port, ip, path)。"""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise SsrfBlocked(f"scheme not allowed: {parsed.scheme}", code="SCHEME")
    if parsed.username or parsed.password:
        raise SsrfBlocked("userinfo is not allowed", code="USERINFO")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SsrfBlocked("missing host", code="HOST")
    if host not in allowed_hosts:
        raise SsrfBlocked(f"host not allowlisted: {host}", code="HOST")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in allowed_ports:
        raise SsrfBlocked(f"port not allowlisted: {port}", code="PORT")
    candidates: list[str]
    if dns_resolver is not None:
        candidates = list(dns_resolver(host, port))
    elif re.fullmatch(r"[0-9.]+|\[[0-9a-f:]+\]", host):
        candidates = [host.strip("[]")]
    else:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        candidates = [str(info[4][0]) for info in infos]
    if not candidates:
        raise SsrfBlocked("dns resolution failed", code="DNS")
    # 白名单内的控制站允许私网地址（本地替身）；其它主机已在 host 检查处拒绝。
    # 优先 IPv4：localhost 在 Windows 上可能先解析出 ::1，而控制站只监听 127.0.0.1。
    pinned = next((item for item in candidates if ":" not in item), candidates[0])
    ipaddress.ip_address(pinned)
    return host, port, pinned, parsed.path or "/"


def safe_fetch(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    allowed_ports: frozenset[int],
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    max_redirects: int = MAX_REDIRECTS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    dns_resolver: Callable[[str, int], list[str]] | None = None,
) -> dict[str, Any]:
    """受控出站 fetch：每个重定向跳都重新校验并固定 IP，限制体积与跳数。"""

    current = url
    hops: list[str] = []
    for hop_index in range(max_redirects + 1):
        host, port, pinned_ip, path = _validate_outbound_url(
            current,
            allowed_hosts=allowed_hosts,
            allowed_ports=allowed_ports,
            dns_resolver=dns_resolver,
        )
        connection = _PinnedHTTPConnection(host, port, pinned_ip=pinned_ip, timeout=timeout)
        try:
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            status = response.status
            location = response.getheader("Location")
            body = response.read(max_bytes + 1)
        finally:
            connection.close()
        hops.append(f"{host}:{port}{path} -> {status}")
        if 300 <= status < 400:
            if not location:
                raise SsrfBlocked("redirect without location", code="REDIRECT")
            if hop_index + 1 > max_redirects:
                raise SsrfBlocked("too many redirects", code="REDIRECT_LIMIT")
            current = urllib.parse.urljoin(current, location)
            continue
        if status >= 400:
            raise SsrfBlocked(f"http status {status}", code="HTTP_STATUS")
        if len(body) > max_bytes:
            raise SsrfBlocked("response body too large", code="BODY_TOO_LARGE")
        return {"status": status, "bytes": len(body), "hops": hops, "body_excerpt": body[:32]}
    raise SsrfBlocked("redirect limit exceeded", code="REDIRECT_LIMIT")


_SSRF_VECTORS: Final = (
    ("ssrf-allow-literal", "allow", f"http://127.0.0.1:{CONTROL_PORT}/"),
    ("ssrf-allow-localhost", "allow", f"http://localhost:{CONTROL_PORT}/"),
    ("ssrf-allow-redirect", "allow", f"http://127.0.0.1:{CONTROL_PORT}/redirect/2"),
    ("ssrf-block-scheme-file", "deny", "file:///etc/passwd"),
    ("ssrf-block-scheme-gopher", "deny", "gopher://127.0.0.1:6379/_PING"),
    ("ssrf-block-scheme-ftp", "deny", "ftp://127.0.0.1/"),
    ("ssrf-block-userinfo", "deny", f"http://pyknora:secret@127.0.0.1:{CONTROL_PORT}/"),
    ("ssrf-block-port", "deny", "http://127.0.0.1:22/"),
    ("ssrf-block-port-pg", "deny", "http://127.0.0.1:5433/"),
    ("ssrf-block-metadata", "deny", "http://169.254.169.254/latest/meta-data/"),
    ("ssrf-block-private-10", "deny", "http://10.0.0.5/"),
    ("ssrf-block-private-172", "deny", "http://172.16.3.4/"),
    ("ssrf-block-private-192", "deny", "http://192.168.1.10/"),
    ("ssrf-block-loopback-alt", "deny", f"http://127.0.0.2:{CONTROL_PORT}/"),
    ("ssrf-block-unspecified", "deny", f"http://0.0.0.0:{CONTROL_PORT}/"),
    ("ssrf-block-ipv6", "deny", f"http://[::1]:{CONTROL_PORT}/"),
    ("ssrf-block-redirect-metadata", "deny", f"http://127.0.0.1:{CONTROL_PORT}/redirect-metadata"),
    (
        "ssrf-block-redirect-private",
        "deny",
        f"http://127.0.0.1:{CONTROL_PORT}/redirect-private",
    ),
    ("ssrf-block-redirect-loop", "deny", f"http://127.0.0.1:{CONTROL_PORT}/redirect-loop"),
    ("ssrf-block-redirect-file", "deny", f"http://127.0.0.1:{CONTROL_PORT}/redirect-file"),
    ("ssrf-block-external", "deny", "http://example.com/"),
    ("ssrf-block-decimal-ip", "deny", f"http://2130706433:{CONTROL_PORT}/"),
    ("ssrf-block-slow", "deny", f"http://127.0.0.1:{CONTROL_PORT}/slow"),
    ("ssrf-block-huge", "deny", f"http://127.0.0.1:{CONTROL_PORT}/huge"),
)

_ALLOWED_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
_ALLOWED_PORTS: Final = frozenset({CONTROL_PORT})


def probe_ssrf(
    log: ProbeLog,
    *,
    dns_resolver: Callable[[str, int], list[str]] | None = None,
) -> int:
    """本地控制站的每跳 SSRF 校验：私网/元数据/重定向/慢响应/超大响应全部拦截。"""

    attempted = 0
    with _ControlServer():
        for probe_id, expected, url in _SSRF_VECTORS:
            attempted += 1
            try:
                result = safe_fetch(
                    url,
                    allowed_hosts=_ALLOWED_HOSTS,
                    allowed_ports=_ALLOWED_PORTS,
                    dns_resolver=dns_resolver,
                )
            except SsrfBlocked as exc:
                refused, observed = True, f"blocked:{exc.code}"
            except (OSError, http.client.HTTPException) as exc:
                refused, observed = True, f"blocked:{type(exc).__name__}"
            else:
                refused, observed = False, f"reached:{result['status']} hops={len(result['hops'])}"
            _record(
                log,
                category="ssrf",
                probe_id=probe_id,
                vector=f"出站请求 {url}",
                expected=expected,
                refused=refused,
                observed=observed,
                evidence_ref=f"{SCHEMA}.phase0_object_refs",
            )
    return attempted


# -- 5. canary 脱敏 ----------------------------------------------------------


def probe_canary(log: ProbeLog) -> int:
    """canary 全 sink 扫描：trace 事件、状态片段、DTO、异常都不得出现秘密/PII。"""

    secrets = (SECRET_CANARY, AUTH_CANARY, DSN_CANARY, "phase0-local-only")
    canaries = (PII_EMAIL_CANARY, PII_PHONE_CANARY)
    attempted = 0

    def new_trace() -> safe_trace.SafeTrace:
        return safe_trace.SafeTrace(run_id=uuid4(), secrets=secrets, canaries=canaries)

    def scan(trace: safe_trace.SafeTrace) -> bool:
        text = json.dumps(trace.as_list(), ensure_ascii=False, default=str)
        text += json.dumps(trace.to_state_fragment(), ensure_ascii=False, default=str)
        return any(value in text for value in CANARY_VALUES)

    def trace_leak(fields: Mapping[str, Any], *, action: str = "provider_call") -> None:
        trace = new_trace()
        try:
            trace.record(node="model_gateway", action=action, **dict(fields))
        except safe_trace.TraceFieldRejected as exc:
            raise AccessDenied("trace field rejected", code=exc.code) from exc
        if scan(trace):
            return  # 泄漏
        raise AccessDenied("trace clean", code="CANARY_CLEAN")

    def dto_leak(payload: Mapping[str, Any]) -> None:
        """安全 DTO 路径：trace event 序列化 + state fragment 必须已脱敏。"""

        trace = new_trace()
        note = " ".join(str(value) for value in payload.values())
        trace.record(node="model_gateway", action="provider_call", note_code=note)
        dto = [event.as_dict() for event in trace.events()]
        dto.append(trace.to_state_fragment())
        text = json.dumps(dto, ensure_ascii=False, sort_keys=True, default=str)
        if any(value in text for value in CANARY_VALUES):
            return  # 泄漏
        raise AccessDenied("dto clean", code="CANARY_CLEAN")

    def error_leak() -> None:
        trace = new_trace()
        trace.record_error(
            node="model_gateway",
            action="provider_call",
            error=RuntimeError(f"upstream failure {SECRET_CANARY}"),
        )
        if scan(trace):
            return
        raise AccessDenied("error trace clean", code="CANARY_CLEAN")

    def dsn_redaction() -> None:
        redacted = redact_dsn(DSN_CANARY)
        if "t29-secret" in redacted:
            return  # 泄漏
        raise AccessDenied("dsn redacted", code="DSN_REDACTED")

    def hmac_digest() -> None:
        digest = _hmac(SIGNING_KEY, SECRET_CANARY)
        if SECRET_CANARY in digest:
            return
        raise AccessDenied("digest clean", code="CANARY_CLEAN")

    def forbidden_field() -> None:
        trace = new_trace()
        try:
            trace.record(node="model_gateway", action="provider_call", prompt="hello")
        except safe_trace.TraceFieldRejected as exc:
            raise AccessDenied("field rejected", code=exc.code) from exc
        return  # 越权：敏感字段被接受

    cases: list[tuple[str, str, Callable[[], None]]] = [
        (
            "canary-trace-secret",
            "secret 写入 trace note_code",
            lambda: trace_leak({"note_code": f"{SECRET_CANARY} leaked"}),
        ),
        (
            "canary-trace-auth",
            "Authorization 值写入 trace",
            lambda: trace_leak({"note_code": f"{AUTH_CANARY} leaked"}),
        ),
        (
            "canary-trace-pii-email",
            "PII 邮箱写入 trace",
            lambda: trace_leak({"note_code": f"owner={PII_EMAIL_CANARY}"}),
        ),
        (
            "canary-trace-pii-phone",
            "PII 手机号写入 trace",
            lambda: trace_leak({"note_code": f"phone={PII_PHONE_CANARY}"}),
        ),
        (
            "canary-trace-dsn",
            "DSN 写入 trace",
            lambda: trace_leak({"note_code": f"dsn={DSN_CANARY}"}),
        ),
        ("canary-forbidden-field", "直接写 prompt 字段", forbidden_field),
        (
            "canary-state-fragment",
            "state fragment 含 secret",
            lambda: trace_leak({"error_code": SECRET_CANARY}),
        ),
        ("canary-error-message", "异常消息回显 secret", error_leak),
        (
            "canary-dto",
            "安全 DTO（trace 序列化 + state fragment）不含 secret/Auth",
            lambda: dto_leak(
                {"model": "fake", "note_code": SECRET_CANARY, "authorization": AUTH_CANARY}
            ),
        ),
        ("canary-dsn-redaction", "DSN 证据脱敏", dsn_redaction),
        ("canary-hmac-digest", "HMAC 摘要不含明文", hmac_digest),
        (
            "canary-correlation-id",
            "可记录相关摘要前缀",
            lambda: trace_leak({"request_digest_prefix": hashlib.sha256(b"x").hexdigest()[:8]}),
        ),
    ]
    for probe_id, vector, action in cases:
        attempted += 1
        refused, observed = _expect_denied(action)
        _record(
            log,
            category="canary",
            probe_id=probe_id,
            vector=vector,
            expected="deny",
            refused=refused,
            observed=observed,
            evidence_ref="safe_trace.SafeTrace",
        )

    full_trace = new_trace()
    full_trace.record(node="model_gateway", action="provider_call", note_code="ok")
    attempted += 1
    leaked = scan(full_trace)
    _record(
        log,
        category="canary",
        probe_id="canary-full-sink-scan",
        vector="trace 事件 + state fragment 全量扫描 canary",
        expected="deny",
        refused=not leaked,
        observed=f"canary_hits={int(leaked)}",
        evidence_ref="safe_trace.SafeTrace.to_state_fragment",
    )
    return attempted


# -- 6. 业务事实状态不得绕过授权 ---------------------------------------------


def probe_business_state(
    admin: psycopg.Connection[Any],
    app: psycopg.Connection[Any],
    seed: SeedData,
    log: ProbeLog,
) -> int:
    """检索/引用/终态事件/Job 恢复都在后端按 Principal 校验，不信任客户端声明。"""

    attempted = 0
    evidence = [
        CitationEvidence(
            handle=1, chunk_id=seed.chunks["A0v1c0"], document_id=DOC_A0, index_version=1
        )
    ]

    def no_scope_event() -> None:
        app.rollback()
        app.execute(
            f"INSERT INTO {SCHEMA}.phase0_run_events "
            "(workspace_id, run_id, event_id, event_type) VALUES (%s, %s, %s, 1)",
            (WS_A, RUN_A, uuid4()),
        )

    def cross_scope_event() -> None:
        with scoped_transaction(app, workspace_id=WS_B, principal_id=PRIN_B_OWNER):
            app.execute(
                f"INSERT INTO {SCHEMA}.phase0_run_events "
                "(workspace_id, run_id, event_id, event_type) VALUES (%s, %s, %s, 1)",
                (WS_A, RUN_A, uuid4()),
            )

    def forged_principal_event() -> None:
        with scoped_transaction(app, workspace_id=WS_B, principal_id=PRIN_B_OWNER):
            app.execute(
                f"INSERT INTO {SCHEMA}.phase0_run_events "
                "(workspace_id, run_id, event_id, event_type, payload) "
                "VALUES (%s, %s, %s, 1, %s::jsonb)",
                (WS_B, RUN_B, uuid4(), json.dumps({"principal_id": str(PRIN_A_OWNER)})),
            )

    def terminal_event() -> None:
        # 第一条终态事件允许写入，第二条必须被守卫拒绝。
        with scoped_transaction(app, workspace_id=WS_A, principal_id=PRIN_A_OWNER):
            app.execute(
                f"INSERT INTO {SCHEMA}.phase0_run_events "
                "(workspace_id, run_id, event_id, event_type, terminal) "
                "VALUES (%s, %s, %s, 4, true)",
                (WS_A, RUN_A, uuid4()),
            )
        with scoped_transaction(app, workspace_id=WS_A, principal_id=PRIN_A_OWNER):
            app.execute(
                f"INSERT INTO {SCHEMA}.phase0_run_events "
                "(workspace_id, run_id, event_id, event_type, terminal) "
                "VALUES (%s, %s, %s, 4, true)",
                (WS_A, RUN_A, uuid4()),
            )

    def citation_after_revoke() -> None:
        check = authorize_citation(
            admin,
            workspace_id=WS_A,
            principal_id=PRIN_A_REVOKED,
            evidence=evidence,
            answer_text="[[1]]",
        )
        if check.accepted and not check.rejections:
            return  # 越权：撤权后引用仍被接受
        raise AccessDenied("citation rejected", code="CITATION_REJECTED")

    def cross_scope_citation_insert() -> None:
        with scoped_transaction(app, workspace_id=WS_B, principal_id=PRIN_B_OWNER):
            app.execute(
                f"INSERT INTO {SCHEMA}.phase0_citations "
                "(workspace_id, citation_id, run_id, chunk_id, handle, status) "
                "VALUES (%s, %s, %s, %s, 2, 'pending')",
                (WS_A, uuid4(), RUN_A, seed.chunks["A0v1c0"]),
            )

    def object_cross_workspace() -> None:
        authorize_object_access(
            admin,
            workspace_id=WS_B,
            principal_id=PRIN_B_OWNER,
            object_key=OBJECT_KEY_A,
            method="get",
            purpose="read",
            signature=seed.object_signature,
        )

    def object_expired() -> None:
        authorize_object_access(
            admin,
            workspace_id=WS_A,
            principal_id=PRIN_A_OWNER,
            object_key=OBJECT_KEY_A,
            method="get",
            purpose="read",
            signature=seed.object_signature,
            now=datetime.now(UTC) + timedelta(hours=1),
        )

    def object_method() -> None:
        authorize_object_access(
            admin,
            workspace_id=WS_A,
            principal_id=PRIN_A_OWNER,
            object_key=OBJECT_KEY_A,
            method="delete",
            purpose="read",
            signature=seed.object_signature,
        )

    def object_forged_signature() -> None:
        authorize_object_access(
            admin,
            workspace_id=WS_A,
            principal_id=PRIN_A_OWNER,
            object_key=OBJECT_KEY_A,
            method="get",
            purpose="read",
            signature="0" * 40,
        )

    def object_forged_key() -> None:
        authorize_object_access(
            admin,
            workspace_id=WS_A,
            principal_id=PRIN_A_OWNER,
            object_key="object/a/../secret-artifact.txt",
            method="get",
            purpose="read",
            signature=seed.object_signature,
        )

    def job_cross_workspace() -> None:
        authorize_job_restore(admin, workspace_id=WS_B, job_id=JOB_A)

    def job_payload_workspace() -> None:
        authorize_job_restore(
            admin,
            workspace_id=WS_A,
            job_id=JOB_A,
            payload={"workspace_id": str(WS_B)},
        )

    def job_payload_principal() -> None:
        authorize_job_restore(
            admin,
            workspace_id=WS_A,
            job_id=JOB_A,
            payload={"principal_id": str(PRIN_A_MEMBER)},
        )

    def retrieval_after_kb_revoke() -> None:
        rows = _scoped_count(
            app,
            workspace_id=WS_A,
            principal_id=PRIN_A_MEMBER,
            statement=(
                f"SELECT count(*) AS hit FROM {SCHEMA}.phase0_chunks AS c "
                f"JOIN {SCHEMA}.phase0_documents AS d "
                "  ON d.workspace_id = c.workspace_id AND d.document_id = c.document_id "
                f"JOIN {SCHEMA}.phase0_kb_grants AS g "
                "  ON g.workspace_id = d.workspace_id AND g.kb_id = d.kb_id "
                " AND g.principal_id = %s AND g.status = 'active' "
                "WHERE c.workspace_id = %s AND d.kb_id = %s"
            ),
        )
        if rows > 0:
            return  # 越权：KB 授权撤销后仍可见
        raise AccessDenied("revoked kb grant hidden", code="NO_VISIBLE_ROWS")

    cases: list[tuple[str, str, str, Callable[[], Any]]] = [
        ("bs-event-no-scope", "deny", "无 Scope 写终态事件", no_scope_event),
        ("bs-event-cross-workspace", "deny", "B Scope 写 A 的事件", cross_scope_event),
        ("bs-event-forged-principal", "deny", "payload 自报 principal", forged_principal_event),
        ("bs-event-terminal", "deny", "重复终态事件", terminal_event),
        ("bs-citation-revoked", "deny", "撤权 principal 引用", citation_after_revoke),
        (
            "bs-citation-cross-insert",
            "deny",
            "B Scope 插入 A 的引用",
            cross_scope_citation_insert,
        ),
        ("bs-object-cross-workspace", "deny", "B 空间取 A 的对象签名", object_cross_workspace),
        ("bs-object-expired", "deny", "过期对象签名", object_expired),
        ("bs-object-method", "deny", "签名未覆盖的方法", object_method),
        ("bs-object-forged", "deny", "伪造对象签名", object_forged_signature),
        ("bs-object-forged-key", "deny", "路径穿越 object_key", object_forged_key),
        ("bs-job-cross-workspace", "deny", "B 空间恢复 A 的 Job", job_cross_workspace),
        ("bs-job-payload-workspace", "deny", "Job payload 篡改 workspace", job_payload_workspace),
        ("bs-job-payload-principal", "deny", "Job payload 篡改 principal", job_payload_principal),
        ("bs-kb-revoked-retrieval", "deny", "KB 授权撤销后检索", retrieval_after_kb_revoke),
    ]

    for probe_id, expected, vector, action in cases:
        attempted += 1
        refused, observed = _expect_denied(action)
        try:
            app.rollback()
            admin.rollback()
        except psycopg.Error:  # pragma: no cover - 清理兜底
            pass
        _record(
            log,
            category="business_state",
            probe_id=probe_id,
            vector=vector,
            expected=expected,
            refused=refused,
            observed=observed,
            evidence_ref=f"{SCHEMA}.phase0_run_events",
        )
    return attempted


# -- 7. 伪造/越权引用（100 例） ----------------------------------------------


def probe_citations(
    conn: psycopg.Connection[Any],
    seed: SeedData,
    log: ProbeLog,
    *,
    rounds: int = 10,
) -> int:
    """T26 口径的引用校验：未知 handle、跨空间 chunk、旧版本、未授权、重复都拒绝。"""

    attempted = 0
    owner_visible = visible_chunk_ids(conn, workspace_id=WS_A, principal_id=PRIN_A_OWNER)
    member_visible = visible_chunk_ids(conn, workspace_id=WS_A, principal_id=PRIN_A_MEMBER)
    evidence = [
        CitationEvidence(handle=index + 1, chunk_id=chunk_id, document_id=DOC_A0, index_version=1)
        for index, chunk_id in enumerate(owner_visible[:6])
    ]
    member_evidence = [
        CitationEvidence(handle=index + 1, chunk_id=chunk_id, document_id=DOC_A0, index_version=1)
        for index, chunk_id in enumerate(member_visible[:6])
    ]
    if len(evidence) < 6 or len(member_evidence) < 1:
        raise SecurityProbeError(
            f"可见 chunk 不足: owner={len(evidence)} member={len(member_evidence)}",
            code="SEED_INSUFFICIENT",
        )

    def control() -> None:
        check = authorize_citation(
            conn,
            workspace_id=WS_A,
            principal_id=PRIN_A_OWNER,
            evidence=evidence,
            answer_text="[[1]] [[2]]",
        )
        if check.rejections or len(check.accepted) != 2:
            raise AccessDenied("合法引用被拒", code="CITATION_CONTROL_FAILED")

    variants: list[tuple[str, str, Callable[[], CitationCheck]]] = [
        (
            "ci-unknown-handle",
            "handle 超出本轮 evidence",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=evidence,
                answer_text="[[99]]",
            ),
        ),
        (
            "ci-unknown-chunk-ref",
            "chunk 引用不存在",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=evidence,
                answer_text=f"chunk:{uuid4()}",
            ),
        ),
        (
            "ci-cross-workspace-chunk",
            "chunk 属于 B 空间",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=evidence,
                answer_text=f"chunk:{seed.chunks['B0v1c0']}",
            ),
        ),
        (
            "ci-retired-version",
            "chunk 属于 retired 旧版本",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=evidence,
                answer_text=f"chunk:{seed.stale_chunks[0]}",
            ),
        ),
        (
            "ci-revoked-kb-grant",
            "chunk 属于已撤销 KB 授权",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_MEMBER,
                evidence=member_evidence,
                answer_text=f"chunk:{seed.chunks['A2v1c0']}",
            ),
        ),
        (
            "ci-not-in-evidence",
            "chunk 可见但不在本轮 evidence",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=evidence[:2],
                answer_text=f"chunk:{owner_visible[5]}",
            ),
        ),
        (
            "ci-duplicate-handle",
            "重复 handle",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=evidence,
                answer_text="[[1]] [[1]]",
            ),
        ),
        (
            "ci-out-of-range",
            "handle 大于 evidence 长度",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=evidence,
                answer_text=f"[[{len(evidence) + 5}]]",
            ),
        ),
        (
            "ci-mixed-valid-forged",
            "合法 handle + 伪造 chunk 混排",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=evidence,
                answer_text=f"[[1]] chunk:{uuid4()}",
            ),
        ),
        (
            "ci-zero-handle",
            "handle 0（越界）",
            lambda: authorize_citation(
                conn,
                workspace_id=WS_A,
                principal_id=PRIN_A_OWNER,
                evidence=evidence,
                answer_text="[[0]]",
            ),
        ),
    ]

    attempted += 1
    refused, observed = _expect_denied(control)
    _record(
        log,
        category="citations",
        probe_id="ci-control",
        vector="正向对照：合法 handle 被接受",
        expected="allow",
        refused=refused,
        observed=observed,
        evidence_ref=f"{SCHEMA}.phase0_citations",
    )

    for index in range(rounds):
        for probe_id, vector, action in variants:
            attempted += 1
            refused, observed = _expect_denied(_citation_guard, action)
            _record(
                log,
                category="citations",
                probe_id=f"{probe_id}-{index:02d}",
                vector=vector,
                expected="deny",
                refused=refused,
                observed=observed,
                evidence_ref=f"{SCHEMA}.phase0_citations",
            )
    return attempted


# Redis 原子计数（T29 自己的资源槽，仅证明 Redis 可用与原子性）
# ---------------------------------------------------------------------------


def probe_redis(db_url: str, log: ProbeLog, *, rounds: int = 10) -> dict[str, Any]:
    with redis_client(db_url) as client:
        flush_prefix(client)
        key = f"{REDIS_PREFIX}counter"
        ok = 0
        for index in range(rounds):
            value = client.incr(key)
            if value == index + 1:
                ok += 1
        _record(
            log,
            category="canary",
            probe_id="redis-atomic-counter",
            vector=f"Redis 原子计数（T29 资源槽 DB{REDIS_DB}；W9 指定 DB16 超上限）",
            expected="allow",
            refused=ok != rounds,
            observed=f"rounds={rounds} ok={ok} value={client.get(key)}",
            evidence_ref=f"redis:db{REDIS_DB}:{key}",
        )
        removed = flush_prefix(client)
        return {"db": REDIS_DB, "rounds": rounds, "atomic_ok": ok == rounds, "cleaned": removed}


# ---------------------------------------------------------------------------
# schema 自检
# ---------------------------------------------------------------------------


TENANT_TABLES: Final = (
    "phase0_principals",
    "phase0_kb_grants",
    "phase0_documents",
    "phase0_document_versions",
    "phase0_chunks",
    "phase0_runs",
    "phase0_run_events",
    "phase0_execution_bindings",
    "phase0_jobs",
    "phase0_approvals",
    "phase0_citations",
    "phase0_object_refs",
)


def verify_schema(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
        "FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind = 'r' ORDER BY c.relname",
        (SCHEMA,),
    ).fetchall()
    tables = {
        str(row["relname"]): [bool(row["relrowsecurity"]), bool(row["relforcerowsecurity"])]
        for row in rows
    }
    policies = conn.execute(
        "SELECT polname, polcmd, pg_get_expr(polqual, polrelid) AS qual, "
        "pg_get_expr(polwithcheck, polrelid) AS with_check "
        "FROM pg_policy AS p JOIN pg_class AS c ON c.oid = p.polrelid "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s ORDER BY polname",
        (SCHEMA,),
    ).fetchall()
    functions = conn.execute(
        "SELECT p.proname, p.prosecdef, p.provolatile "
        "FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace "
        "WHERE n.nspname = %s ORDER BY p.proname",
        (SCHEMA,),
    ).fetchall()
    ledger = conn.execute(
        f"SELECT migration_id, filename, sha256 FROM {SCHEMA}.phase0_schema_migrations "
        "ORDER BY applied_at DESC LIMIT 1"
    ).fetchone()
    return {
        "tables": tables,
        "policies": [
            {
                "name": str(row["polname"]),
                "cmd": str(row["polcmd"]),
                "qual": str(row["qual"] or ""),
                "with_check": str(row["with_check"] or ""),
            }
            for row in policies
        ],
        "functions": [
            {
                "name": str(row["proname"]),
                "security_definer": bool(row["prosecdef"]),
                "volatility": str(row["provolatile"]),
            }
            for row in functions
        ],
        "ledger": (
            {
                "migration_id": str(ledger["migration_id"]),
                "filename": str(ledger["filename"]),
                "sha256": str(ledger["sha256"]),
            }
            if ledger is not None
            else None
        ),
    }


def verify_role(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    attributes = role_security_attributes(conn, role=APP_ROLE)
    owners = conn.execute(
        "SELECT count(*) AS owned FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "JOIN pg_roles AS r ON r.oid = c.relowner "
        "WHERE n.nspname = %s AND r.rolname = %s",
        (SCHEMA, APP_ROLE),
    ).fetchone()
    actor = conn.execute("SELECT current_user AS actor, session_user AS session").fetchone()
    return {
        "attributes": attributes,
        "owns_tables": int(owners["owned"]) if owners is not None else -1,
        "current_user": str(actor["actor"]) if actor is not None else "",
        "session_user": str(actor["session"]) if actor is not None else "",
    }


# ---------------------------------------------------------------------------
# 引用基线（T26）输入完整性
# ---------------------------------------------------------------------------


def check_rag_baseline(path: Path = RAG_BASELINE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"available": False, "reason": "missing rag-citations.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    sample_count = int(payload.get("sample_count") or 0)
    return {
        "available": True,
        "sample_count": sample_count,
        "cases_embedded": len(cases),
        "citation_accuracy": payload.get("citation_accuracy"),
        "fabricated_citations": payload.get("fabricated_citations"),
        "unauthorized_citations": payload.get("unauthorized_citations"),
        "note": (
            "T26 基线是结构测量（ModelPort 替身），不能当作安全验收；"
            "T29 用自建种子重放 100 例伪造/越权引用"
        ),
    }


# ---------------------------------------------------------------------------
# 汇总与证据
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SecurityRun:
    log: ProbeLog
    seed: SeedData
    evidence: dict[str, Any]
    evidence_path: Path
    matrix_path: Path
    schema_digest: str
    schema_snapshot: dict[str, Any]
    role_snapshot: dict[str, Any]
    redis: dict[str, Any]
    rag_baseline: dict[str, Any]
    git: dict[str, Any]

    @property
    def summary(self) -> dict[str, Any]:
        return self.evidence["summary"]


def _git_identity(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    import subprocess

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return {
            "available": True,
            "branch": branch,
            "head": head,
            "dirty": bool(status.strip()),
            "dirty_sha256": hashlib.sha256(status.encode()).hexdigest(),
        }
    except (OSError, subprocess.SubprocessError):
        return {"available": False}


def redact_dsn(dsn: str) -> str:
    parsed = urllib.parse.urlsplit(dsn)
    if parsed.password is None:
        return dsn
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{parsed.username}:***@{host}{port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def write_json(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    raw = path.read_bytes()
    return {
        "path": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def run_security_probes(
    *,
    dsn: str | None = None,
    redis_url: str | None = None,
    evidence_path: Path = EVIDENCE_PATH,
    matrix_path: Path = MATRIX_PATH,
) -> SecurityRun:
    """完整流程：建 schema → 种子 → 七类探测 → 证据 JSON + 矩阵文档（不清理）。"""

    resolved_dsn = dsn or scope.read_dsn()
    resolved_redis = redis_url or _redis_url_from_env()
    admin = connect(resolved_dsn, autocommit=True)
    log = ProbeLog()
    try:
        reset_schema(admin)
        admin.execute(MIGRATION_SQL)
        digest = hashlib.sha256(MIGRATION_SQL.encode()).hexdigest()
        admin.execute(
            f"INSERT INTO {SCHEMA}.phase0_schema_migrations "
            "(migration_id, filename, sha256) VALUES (%s, %s, %s)",
            (MIGRATION_ID, MIGRATION_FILENAME, digest),
        )
        seed = seed_corpus(admin)
        admin.commit()

        role_info = create_app_role(admin)
        app = connect(role_info["dsn"], autocommit=False)
        try:
            schema_snapshot = verify_schema(admin)
            role_snapshot = verify_role(admin)
            with redis_client(resolved_redis) as client:
                flush_prefix(client)
                tokens = {
                    "live": issue_session(
                        client, payload=_token_payload(WS_A, PRIN_A_OWNER, 1, ttl=600)
                    ),
                    "expired": _mint_token(_token_payload(WS_A, PRIN_A_OWNER, 1, ttl=-1)),
                    "tampered": _mint_token(
                        _token_payload(WS_B, PRIN_B_OWNER, 1, ttl=600)
                    ),
                }
                seed = _with_tokens(seed, tokens)
            probe_cross_workspace(app, seed, log)
            with redis_client(resolved_redis) as client:
                probe_revocation(admin, app, seed, log, client=client)
            probe_payload_tampering(admin, seed, log)
            probe_ssrf(log)
            probe_canary(log)
            probe_business_state(admin, app, seed, log)
            probe_citations(admin, seed, log)
            redis_evidence = probe_redis(resolved_redis, log)
        finally:
            app.close()

        summary = log.summary()
        breaches = log.breaches()
        evidence = {
            "task": TASK_ID,
            "poc": POC_ID,
            "generated_at": datetime.now(UTC).isoformat(),
            "git": _git_identity(),
            "dsn": redact_dsn(resolved_dsn),
            "redis": {
                "url": _redis_url_with_db(resolved_redis, REDIS_DB),
                "db": REDIS_DB,
                "resource_note": REDIS_RESOURCE_NOTE,
                "evidence": redis_evidence,
            },
            "schema": {
                "name": SCHEMA,
                "migration_id": MIGRATION_ID,
                "ddl_sha256": digest,
                "snapshot": schema_snapshot,
            },
            "role": role_snapshot,
            "rag_baseline": check_rag_baseline(),
            "seed": {
                "workspaces": {key: str(value) for key, value in seed.workspaces.items()},
                "documents": len(seed.documents),
                "chunks": len(seed.chunks),
                "stale_chunks": len(seed.stale_chunks),
                "jobs": len(seed.jobs),
                "runs": len(seed.runs),
            },
            "budget": {"min_probes": MIN_PROBES, "min_total": MIN_TOTAL},
            "summary": summary,
            "breaches": [
                {
                    "probe_id": record.probe_id,
                    "category": record.category,
                    "vector": record.vector,
                    "expected": record.expected,
                    "observed": record.observed,
                }
                for record in breaches
            ],
            "probes": [
                {
                    "probe_id": record.probe_id,
                    "category": record.category,
                    "vector": record.vector,
                    "expected": record.expected,
                    "observed": record.observed,
                    "refused": record.refused,
                    "evidence_ref": record.evidence_ref,
                }
                for record in log.records()
            ],
            "not_run": [
                {
                    "item": "真实 chat 模型链路",
                    "reason": "禁止联网/禁止真实 key；本任务不含模型语义段",
                },
                {
                    "item": "真实身份提供商登录",
                    "reason": "C-07 BLOCKED：身份域产品+安全+架构决策未定",
                },
                {
                    "item": "真实对象存储签名代理",
                    "reason": "无 MinIO/S3 环境；只用合成 HMAC 签名口径",
                },
                {
                    "item": "真实 DNS 重绑定攻击",
                    "reason": "本机无受控 DNS；用 host 白名单 + IP 校验替身",
                },
                {
                    "item": "pgvector ANN 检索越权",
                    "reason": "ANN 属 T25 范围且本任务镜像精确扫描口径",
                },
            ],
        }
        evidence_info = write_json(evidence_path, evidence)
        evidence["written"] = evidence_info
        matrix_text = render_matrix_markdown(evidence)
        matrix_path.parent.mkdir(parents=True, exist_ok=True)
        matrix_path.write_text(matrix_text, encoding="utf-8")
        return SecurityRun(
            log=log,
            seed=seed,
            evidence=evidence,
            evidence_path=evidence_path,
            matrix_path=matrix_path,
            schema_digest=digest,
            schema_snapshot=schema_snapshot,
            role_snapshot=role_snapshot,
            redis=redis_evidence,
            rag_baseline=evidence["rag_baseline"],
            git=evidence["git"],
        )
    finally:
        admin.close()


def _with_tokens(seed: SeedData, tokens: Mapping[str, str]) -> SeedData:
    return replace(seed, session_tokens=dict(tokens))


SAFETY_PROPERTIES: Final = {
    "cross_workspace": "跨空间读/写/检索/引用/恢复/工具调用一律拒绝；越权成功 = 0",
    "revocation": "撤权后旧 session/token/游标/审批/Job 立即失效，恢复后须重新授权",
    "payload": "body/payload/审批参数/引用篡改不得扩大授权",
    "ssrf": "出站每跳校验，禁止到达非白名单地址（含重定向与内网）",
    "canary": "secret/PII 不进入 trace/state/DTO/异常/日志等任何 sink",
    "business_state": "业务事实状态（终态事件、恢复、引用）写入必须后端重新授权",
    "citations": "伪造/越权/跨空间引用不得被接受，且必须留下拒绝记录",
}


def render_matrix_markdown(evidence: Mapping[str, Any]) -> str:
    """把证据 JSON 渲染为可复核的 `security-matrix.md`（五章）。"""

    summary = evidence["summary"]
    total = summary["total"]
    categories = summary["categories"]
    git_info = evidence["git"]
    snapshot_json = json.dumps(
        evidence["schema"]["snapshot"], ensure_ascii=False, sort_keys=True
    )
    role_json = json.dumps(evidence["role"], ensure_ascii=False, sort_keys=True)
    seed_json = json.dumps(evidence["seed"], ensure_ascii=False, sort_keys=True)
    redis_json = json.dumps(
        evidence["redis"]["evidence"], ensure_ascii=False, sort_keys=True
    )
    baseline_json = json.dumps(
        evidence["rag_baseline"], ensure_ascii=False, sort_keys=True
    )
    lines: list[str] = [
        "# P0-T29 安全门禁矩阵（P0 全链授权与内容安全回归）",
        "",
        f"- 任务：{evidence['task']}（PoC {evidence['poc']}）",
        f"- 生成时间：`{evidence['generated_at']}`",
        f"- 探针执行：**{total['probes']}** 条；拒绝 {total['refused']}；"
        f"越权/漏放 **{total['breaches']}**",
        "- 证据 JSON：`docs/poc/evidence/security-probes.json`",
        f"- 基线：branch `{git_info.get('branch')}` head `{git_info.get('head')}` "
        f"dirty={git_info.get('dirty')}（dirty_sha256 `{git_info.get('dirty_sha256')}`）",
        "",
        "## 1. 矩阵总表",
        "",
        "| 类别 | 安全性质 | 探测数 | 拒绝 | 放行 | 越权/漏放 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for category in sorted(categories):
        bucket = categories[category]
        lines.append(
            f"| `{category}` | {SAFETY_PROPERTIES.get(category, '-')} | "
            f"{bucket['probes']} | {bucket['refused']} | {bucket['allowed']} | "
            f"{bucket['breaches']} |"
        )
    lines.append(
        f"| **合计** | 门禁条件：越权/漏放 = 0 | {total['probes']} | {total['refused']} | "
        f"{total['allowed']} | {total['breaches']} |"
    )
    lines += [
        "",
        f"- 预算下限：每类 {evidence['budget']['min_probes']}；总量 "
        f"{evidence['budget']['min_total']}（实测 {total['probes']}）。",
        "",
        "## 2. 探测向量与实测",
        "",
        "| 类别 | 探测 ID | 探测向量 | 期望 | 实测 | 拒绝 | 证据位置 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in evidence["probes"]:
        refused = "是" if record["refused"] else "否"
        lines.append(
            f"| `{record['category']}` | `{record['probe_id']}` | {record['vector']} | "
            f"{record['expected']} | {record['observed']} | {refused} | "
            f"`{record['evidence_ref']}` |"
        )
    lines.append("")

    lines += ["## 3. 拒绝证据", ""]
    breaches = evidence["breaches"]
    if breaches:
        lines += ["### 3.1 越权/漏放（必须为空）", ""]
        for record in breaches:
            lines.append(
                f"- `{record['probe_id']}`（{record['category']}）：{record['vector']} → "
                f"实测 {record['observed']}"
            )
        lines.append("")
    else:
        lines += ["- 越权/漏放记录：**0 条**（全部探测符合期望）。", ""]
    lines += [
        "### 3.2 schema 与角色快照（拒绝来自数据库强制而非口头声明）",
        "",
        f"- schema `{evidence['schema']['name']}`，迁移 `{evidence['schema']['migration_id']}`，"
        f"DDL sha256 `{evidence['schema']['ddl_sha256']}`",
        f"- schema 快照：```{snapshot_json}```",
        f"- 应用角色属性：```{role_json}```",
        "",
        "### 3.3 种子与 Redis 事实",
        "",
        f"- seed：```{seed_json}```",
        f"- Redis（DB{evidence['redis']['db']}）：```{redis_json}```",
        "",
    ]

    lines += ["## 4. 覆盖缺口与 NOT_RUN", ""]
    for item in evidence["not_run"]:
        lines.append(f"- `NOT_RUN` **{item['item']}**：{item['reason']}")
    lines.append(
        "- `RESOURCE_DEVIATION` **Redis 槽位**："
        f"{evidence['redis']['resource_note']}"
    )
    lines += [
        "- 结论口径：无真实模型/身份提供商，本报告最高判定为 `PASS（限域）`；"
        "不得据此宣称生产安全已验收。",
        "- T26 引用基线只用于输入完整性（`sample_count=100`、结构测量），"
        "不作为安全验收依据；伪造/越权引用矩阵由本任务自建种子实测。",
        f"- T26 基线元数据：```{baseline_json}```",
        "",
        "## 5. 对 T31 的输入",
        "",
        "- 复用本报告探测向量与「安全 = 拒绝」判定约定，构建 P0 回归门禁：跨空间矩阵、"
        "撤权窗口、载荷/审批篡改、SSRF 每跳校验、canary 全 sink、业务事实状态授权。",
        "- 固化两个判定助手：`_expect_denied`（系统抛拒绝 = 安全）与 "
        "`_citation_guard`（全量接受 = 越权），避免双向 raise 造成的假 PASS。",
        "- 生产化前置项：受限 worker 角色（SECURITY DEFINER owner 不得是超级用户）、"
        "T12/T13 真表 RLS、真实 IdP/MinIO/模型链路的端到端负例。",
        "",
    ]
    return "\n".join(lines) + "\n"
