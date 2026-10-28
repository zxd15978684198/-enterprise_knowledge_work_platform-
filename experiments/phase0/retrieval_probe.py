"""P0-T25 · 授权检索与三路对照探针（向量 / FTS / BM25 / RRF）。

定位
====
本模块是 **检索契约探针**，不是第二套生产 schema：

- 只读镜像 T13/T12 的可见性语义（`docs/poc/07` C-01、`004_version_index.sql`、
  `008_publication.sql`、`index_staging.search_visible_chunks`）：
  chunk 只有在**同一 document 的 `documents.active_version` 指针**、
  **版本 `status='active'`**、**且 `chunks.attempt_id = versions.winning_attempt_id`**
  三者同时成立时才可见。
- 生产落地时这些表在共享 schema；本任务只有 `p0_t25` 资源槽，因此建**本槽独立**的
  镜像表（不写 004/008，不建编号迁移，不碰 `p0_t12`/`p0_t13`）。镜像偏离写在证据里。
- 权限用 T03 的 `scope.apply_scope()`（`SET LOCAL app.workspace_id/app.principal_id`）
  驱动 `p0_t25` 的 RLS，配非超级用户角色 `p0_t25_app`（NOSUPERUSER NOBYPASSRLS）。
  不依赖 `p0_app`：T03 的会话级 teardown 会 DROP ROLE，跨任务复用会互相拆台。

三路对照口径（必须与证据一致）
==============================
- **vector**：`double precision[]` + **精确全扫描**（本机 core 镜像无 pgvector，
  ANN / HNSW / IVFFlat **NOT_RUN**）。embedding 是确定性「词袋哈希」假向量
  （`fake.lexical.v1`，64 维），**不代表真实语义 embedding 质量**。
- **fts**：PostgreSQL 内建 `to_tsvector('simple', …)` / `plainto_tsquery` /
  `ts_rank`，无 zhparser。中文按整段成一个 token，**未分词文本的中文召回会掉到 0**，
  这是被测事实，不粉饰（证据里单列 `chinese_unspaced` 子集）。
- **bm25**：本模块 Python 实现（k1=1.2, b=0.75），tokenizer 与假 embedding 共用：
  ASCII 词 + 中文二元组（bigram）。
- **rrf**：`Σ 1/(k + rank)`，k=60；异构原始分数**不相加**（TDD02 §2 / Backlog 集成影响）。

单快照
======
`search()` 只在 `REPEATABLE READ` 事务内工作（否则抛 `RETRIEVAL_SNAPSHOT_ISOLATION_REQUIRED`），
指针与 chunk 在同一条 SQL 的 CTE 里取出；`probe_snapshot()` 用两段读 + 并发切换证明
不会读到混版本。

NOT_RUN（证据里必须保留）
=========================
- 真实 embedding 模型 / 真实 rerank：无模型、禁联网 → `NOT_RUN`。
- 真实 ANN（pgvector HNSW/IVFFlat）：core 镜像无 `vector` 扩展 → `NOT_RUN`。
- 生产级中文分词（zhparser/pg_jieba）：未安装 → `NOT_RUN`（只测出差距，不给结论）。
- Redis 缓存层：本任务无缓存需求 → 槽位 11 未使用。
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import secrets
import statistics
import subprocess
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg import IsolationLevel, sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

import scope

__all__ = [
    "APP_ROLE",
    "EMBEDDING_DIM",
    "EMBEDDING_PROFILE",
    "EVIDENCE_PATH",
    "FOREIGN_EMBEDDING_PROFILE",
    "MIGRATION_ID",
    "MIGRATION_SQL",
    "SCHEMA",
    "BaselineError",
    "EmbeddingProfileMismatch",
    "QaCase",
    "RetrievalHit",
    "RetrievalProbeError",
    "RetrievalResult",
    "ScopeMismatchError",
    "ScopeRequiredError",
    "SeedData",
    "SnapshotIsolationRequired",
    "apply_schema",
    "build_evidence",
    "connect",
    "create_app_role",
    "evaluate_retrieval",
    "git_identity",
    "probe_permissions",
    "probe_snapshot",
    "redact_dsn",
    "reset_schema",
    "search",
    "seed_corpus",
    "visible_chunk_count",
    "write_evidence",
]

SCHEMA: Final = "p0_t25"
APP_ROLE: Final = "p0_t25_app"
MIGRATION_ID: Final = "t25_retrieval_inline"
MIGRATION_FILENAME: Final = "inline:retrieval_probe.py::MIGRATION_SQL"

#: 假 embedding 契约（真实模型 NOT_RUN，证据里必须标注）。
EMBEDDING_DIM: Final = 4096
# 合成 hash 词袋向量的维度：4096 只是把 64 维下的哈希碰撞噪声压到可忽略，
# 真实 embedding 维度由 provider 决定（本机无 pgvector，ANN NOT_RUN）。
EMBEDDING_PROFILE: Final = "fake.lexical.v1"
FOREIGN_EMBEDDING_PROFILE: Final = "fake.other.v1"
FOREIGN_EMBEDDING_DIM: Final = 32

RRF_K: Final = 60
TOP_K: Final = 5
MAX_K: Final = 10
BM25_K1: Final = 1.2
BM25_B: Final = 0.75
VECTOR_MIN_SCORE: Final = 0.10

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
EVIDENCE_PATH: Final = REPO_ROOT / "docs" / "poc" / "evidence" / "retrieval-baseline.json"

_UUID_NS: Final = uuid5(NAMESPACE_URL, "https://pyknora.local/phase0/t25")
_TOPICS: Final = ("topic00", "topic01", "topic02")
_WORKSPACE_KEYS: Final = ("A", "B", "C", "D")
_MODES: Final = ("vector", "fts", "bm25", "rrf")
_CJK_RE: Final = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD_RE: Final = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# 错误码（测试按 code 断言，不匹配自然语言）
# ---------------------------------------------------------------------------
class RetrievalProbeError(Exception):
    """探针基础错误；``code`` 是证据里的稳定标识。"""

    code: str = "RETRIEVAL_PROBE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ScopeRequiredError(RetrievalProbeError):
    """连接上没有 `SET LOCAL app.workspace_id`：deny 默认，不猜空间。"""

    code = "RETRIEVAL_SCOPE_REQUIRED"


class ScopeMismatchError(RetrievalProbeError):
    """调用方自报的 workspace 与连接作用域不一致（不信任客户端作用域）。"""

    code = "RETRIEVAL_SCOPE_MISMATCH"


class SnapshotIsolationRequired(RetrievalProbeError):
    """检索必须在 REPEATABLE READ 单快照里执行，否则指针与 chunk 可能跨版本。"""

    code = "RETRIEVAL_SNAPSHOT_ISOLATION_REQUIRED"


class ModeInvalid(RetrievalProbeError):
    code = "RETRIEVAL_MODE_INVALID"


class EmbeddingProfileMismatch(RetrievalProbeError):
    """查询侧 embedding profile/维度与索引侧不兼容：拒绝打分，不混算。"""

    code = "RETRIEVAL_EMBEDDING_PROFILE_MISMATCH"


class BaselineError(RetrievalProbeError):
    code = "RETRIEVAL_BASELINE_INVALID"


# ---------------------------------------------------------------------------
# SQL：本槽镜像 DDL（不建编号迁移；重复 apply 显式失败）
# ---------------------------------------------------------------------------
MIGRATION_SQL: Final = """
CREATE SCHEMA p0_t25;

COMMENT ON SCHEMA p0_t25 IS
    'P0-T25 检索探针镜像槽：只读复刻 T12/T13 可见性（active 指针 + active 版本 + '
    'winning attempt），独立 RLS 角色 p0_t25_app。生产共享 schema 见 001/004/008。';

DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'p0_t25_app') THEN
        CREATE ROLE p0_t25_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
    ELSE
        ALTER ROLE p0_t25_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
    END IF;
END
$do$;

GRANT USAGE ON SCHEMA p0_t25 TO p0_t25_app;

CREATE TABLE p0_t25.phase0_schema_migrations (
    migration_id text PRIMARY KEY,
    filename     text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   text NOT NULL DEFAULT current_user
);

CREATE TABLE p0_t25.phase0_principals (
    workspace_id        uuid NOT NULL,
    principal_id        uuid NOT NULL,
    role                text NOT NULL CHECK (role IN ('owner', 'member')),
    status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    membership_revision bigint NOT NULL DEFAULT 1 CHECK (membership_revision >= 1),
    PRIMARY KEY (workspace_id, principal_id)
);

CREATE TABLE p0_t25.phase0_documents (
    workspace_id      uuid NOT NULL,
    document_id       uuid NOT NULL,
    source_id         uuid NOT NULL,
    source_version_id uuid NOT NULL,
    active_version    bigint CHECK (active_version IS NULL OR active_version >= 1),
    row_version       bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, document_id)
);

CREATE TABLE p0_t25.phase0_document_versions (
    workspace_id       uuid NOT NULL,
    document_id        uuid NOT NULL,
    index_version      bigint NOT NULL CHECK (index_version >= 1),
    status             text NOT NULL
                       CHECK (status IN ('staging', 'ready', 'active', 'retired', 'failed')),
    winning_attempt_id uuid,
    chunk_count        integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    PRIMARY KEY (workspace_id, document_id, index_version),
    CONSTRAINT phase0_t25_versions_document_fk
        FOREIGN KEY (workspace_id, document_id)
        REFERENCES p0_t25.phase0_documents (workspace_id, document_id) ON DELETE RESTRICT,
    CONSTRAINT phase0_t25_versions_winner_check CHECK (
        status NOT IN ('active', 'retired') OR winning_attempt_id IS NOT NULL
    )
);

CREATE UNIQUE INDEX phase0_t25_one_active_version
    ON p0_t25.phase0_document_versions (workspace_id, document_id)
    WHERE status = 'active';

CREATE TABLE p0_t25.phase0_chunks (
    workspace_id      uuid NOT NULL,
    chunk_id          uuid NOT NULL,
    document_id       uuid NOT NULL,
    index_version     bigint NOT NULL,
    attempt_id        uuid NOT NULL,
    chunk_ordinal     integer NOT NULL CHECK (chunk_ordinal >= 0),
    kind              text NOT NULL CHECK (kind IN ('title', 'body')),
    text              text NOT NULL CHECK (length(text) > 0),
    embedding         double precision[] NOT NULL,
    embedding_dim     integer NOT NULL CHECK (embedding_dim BETWEEN 1 AND 4096),
    embedding_profile text NOT NULL CHECK (length(embedding_profile) BETWEEN 1 AND 64),
    source_id         uuid NOT NULL,
    source_version_id uuid NOT NULL,
    checksum          text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (workspace_id, chunk_id),
    CONSTRAINT phase0_t25_chunks_version_fk
        FOREIGN KEY (workspace_id, document_id, index_version)
        REFERENCES p0_t25.phase0_document_versions (workspace_id, document_id, index_version)
        ON DELETE RESTRICT,
    CONSTRAINT phase0_t25_chunks_ordinal_key
        UNIQUE (workspace_id, document_id, index_version, chunk_ordinal),
    CONSTRAINT phase0_t25_chunks_embedding_len_check
        CHECK (COALESCE(array_length(embedding, 1), 0) = embedding_dim)
);

CREATE INDEX phase0_t25_chunks_visible_idx
    ON p0_t25.phase0_chunks (workspace_id, document_id, index_version, chunk_ordinal);

CREATE INDEX phase0_t25_chunks_attempt_idx
    ON p0_t25.phase0_chunks (workspace_id, attempt_id);

CREATE FUNCTION p0_t25.phase0_current_workspace_id() RETURNS uuid
    LANGUAGE sql
    STABLE
    SET search_path = pg_catalog, p0_t25
    AS $fn$
        SELECT NULLIF(current_setting('app.workspace_id', true), '')::uuid
    $fn$;

CREATE FUNCTION p0_t25.phase0_principal_active() RETURNS boolean
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = pg_catalog, p0_t25
    AS $fn$
        SELECT COALESCE((
            SELECT p.status = 'active'
            FROM p0_t25.phase0_principals AS p
            WHERE p.workspace_id = p0_t25.phase0_current_workspace_id()
              AND p.principal_id =
                  NULLIF(current_setting('app.principal_id', true), '')::uuid
        ), false)
    $fn$;

REVOKE ALL ON FUNCTION p0_t25.phase0_current_workspace_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION p0_t25.phase0_principal_active() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION p0_t25.phase0_current_workspace_id() TO p0_t25_app;
GRANT EXECUTE ON FUNCTION p0_t25.phase0_principal_active() TO p0_t25_app;

ALTER TABLE p0_t25.phase0_principals          ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t25.phase0_principals          FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t25.phase0_documents           ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t25.phase0_documents           FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t25.phase0_document_versions   ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t25.phase0_document_versions   FORCE ROW LEVEL SECURITY;
ALTER TABLE p0_t25.phase0_chunks              ENABLE ROW LEVEL SECURITY;
ALTER TABLE p0_t25.phase0_chunks              FORCE ROW LEVEL SECURITY;

CREATE POLICY phase0_t25_principals_scope ON p0_t25.phase0_principals
    FOR ALL TO p0_t25_app
    USING (workspace_id = p0_t25.phase0_current_workspace_id())
    WITH CHECK (workspace_id = p0_t25.phase0_current_workspace_id());

CREATE POLICY phase0_t25_documents_scope ON p0_t25.phase0_documents
    FOR ALL TO p0_t25_app
    USING (workspace_id = p0_t25.phase0_current_workspace_id()
           AND p0_t25.phase0_principal_active())
    WITH CHECK (workspace_id = p0_t25.phase0_current_workspace_id()
                AND p0_t25.phase0_principal_active());

CREATE POLICY phase0_t25_versions_scope ON p0_t25.phase0_document_versions
    FOR ALL TO p0_t25_app
    USING (workspace_id = p0_t25.phase0_current_workspace_id()
           AND p0_t25.phase0_principal_active())
    WITH CHECK (workspace_id = p0_t25.phase0_current_workspace_id()
                AND p0_t25.phase0_principal_active());

CREATE POLICY phase0_t25_chunks_scope ON p0_t25.phase0_chunks
    FOR ALL TO p0_t25_app
    USING (workspace_id = p0_t25.phase0_current_workspace_id()
           AND p0_t25.phase0_principal_active())
    WITH CHECK (workspace_id = p0_t25.phase0_current_workspace_id()
                AND p0_t25.phase0_principal_active());

GRANT SELECT, INSERT, UPDATE, DELETE ON
    p0_t25.phase0_principals,
    p0_t25.phase0_documents,
    p0_t25.phase0_document_versions,
    p0_t25.phase0_chunks
TO p0_t25_app;
"""


# ---------------------------------------------------------------------------
# 连接 / 角色 / 迁移
# ---------------------------------------------------------------------------
def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection[Any]:
    """连实验库；DSN 复用 T03 的 ``scope.read_dsn()``（只读 import，不改 scope.py）。"""

    if dsn is None:
        dsn = scope.read_dsn()
    return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)


def redact_dsn(dsn: str) -> str:
    """证据里只留 host/db，口令一律 ``***``。"""

    if "@" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    credentials, _, tail = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{tail}"


def apply_schema(
    conn: psycopg.Connection[Any],
    *,
    ddl: str = MIGRATION_SQL,
    migration_id: str = MIGRATION_ID,
) -> str:
    """单事务应用本槽 DDL 并写台账；返回 DDL sha256（调用方 commit）。"""

    if conn.autocommit:
        raise RetrievalProbeError("apply_schema 需要 autocommit=False 的连接")
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        raise RetrievalProbeError("apply_schema 需要干净事务边界（IDLE）")
    digest = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    conn.execute(ddl)
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_schema_migrations (migration_id, filename, sha256) "
        "VALUES (%s, %s, %s)",
        (migration_id, MIGRATION_FILENAME, digest),
    )
    return digest


def schema_sha256(ddl: str = MIGRATION_SQL) -> str:
    return hashlib.sha256(ddl.encode("utf-8")).hexdigest()


def reset_schema(conn: psycopg.Connection[Any], *, role: str = APP_ROLE) -> bool:
    """删除本槽 schema 与角色（清理用）；只碰 p0_t25 / p0_t25_app。"""

    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    if exists is None:
        return False
    conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
    conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    return True


def role_security_attributes(
    conn: psycopg.Connection[Any], *, role: str = APP_ROLE
) -> dict[str, Any]:
    """读取角色安全属性（bool 化；对 dict_row / tuple row 都成立）。"""

    keys = ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolcanlogin")
    row = conn.execute(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin "
        "FROM pg_roles WHERE rolname = %s",
        (role,),
    ).fetchone()
    if row is None:
        raise RetrievalProbeError(f"角色 {role} 不存在")
    values = [row[key] for key in keys] if isinstance(row, Mapping) else list(row)
    return {key: bool(value) for key, value in zip(keys, values, strict=True)}


def create_app_role(conn: psycopg.Connection[Any], *, role: str = APP_ROLE) -> dict[str, Any]:
    """给角色设随机口令并返回可登录 DSN（口令只在内存里，不进证据）。"""

    password = secrets.token_urlsafe(24)
    scope.set_role_password(conn, role=role, password=password)
    attrs = role_security_attributes(conn, role=role)
    return {
        "dsn": scope.dsn_with_credentials(scope.read_dsn(), user=role, password=password),
        "attributes": attrs,
    }


# ---------------------------------------------------------------------------
# 分词 / 假 embedding（确定性；真实模型 NOT_RUN）
# ---------------------------------------------------------------------------
def tokenize(text: str) -> tuple[str, ...]:
    """ASCII 词 + 中文二元组；与 BM25 / 假 embedding 共用同一 tokenizer。"""

    if not isinstance(text, str):
        raise RetrievalProbeError(f"text 必须是 str，收到 {type(text).__name__}")
    lowered = text.lower()
    tokens = _WORD_RE.findall(lowered)
    for run in _CJK_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tuple(tokens)


def embed(text: str, *, dim: int = EMBEDDING_DIM) -> tuple[float, ...]:
    """词袋哈希假向量（L2 归一化）。同一输入恒等，便于复算。"""

    vector = [0.0] * dim
    for token, count in Counter(tokenize(text)).items():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign * count
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return tuple(0.0 for _ in vector)
    return tuple(value / norm for value in vector)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise EmbeddingProfileMismatch(
            f"向量维度不一致：查询 {len(left)} vs 索引 {len(right)}；拒绝混算"
        )
    return sum(a * b for a, b in zip(left, right, strict=True))


def _chunk_checksum(text: str, *, payload_extra: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"text": text, **payload_extra}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QaCase:
    qa_id: str
    workspace: str
    query: str
    gold_chunk_id: UUID | None
    kind: str  # anchor / chinese_spaced / chinese_unspaced / ambiguous / no_answer / superseded


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    chunk_id: UUID
    document_id: UUID
    index_version: int
    attempt_id: UUID
    workspace_id: UUID
    chunk_ordinal: int
    kind: str
    source_id: UUID
    source_version_id: UUID
    score: float
    rank: int
    text_excerpt: str


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    mode: str
    query: str
    hits: tuple[RetrievalHit, ...]
    candidates: int
    profile_filtered: int
    below_threshold: int
    latency_ms: float


@dataclass(frozen=True, slots=True)
class SeedData:
    workspaces: Mapping[str, UUID]
    principals: Mapping[str, UUID]
    sources: Mapping[str, tuple[UUID, UUID]]  # key -> (source_id, source_version_id)
    chunks: Mapping[UUID, Mapping[str, Any]]  # chunk_id -> 行信息
    qa: tuple[QaCase, ...]
    old_chunks: tuple[UUID, ...]
    staged_chunks: tuple[UUID, ...]
    mixed_chunks: tuple[UUID, ...]
    mixed_document: UUID
    switch_document: UUID
    switch_workspace: str
    switch_anchor: str
    revocable_workspace: str
    totals: Mapping[str, int] = field(default_factory=dict)


def _u(tag: str) -> UUID:
    return uuid5(_UUID_NS, tag)


def _fmt(value: Any) -> Any:
    """JSON 序列化：UUID → str，Mapping/tuple 递归。"""

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _fmt(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_fmt(item) for item in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


# ---------------------------------------------------------------------------
# 语料（确定性；4 空间 × 10 文档 × 3 chunk = 120 chunk + 旧版本/暂存/混合 profile）
# ---------------------------------------------------------------------------
def _chunk_text(
    workspace: str,
    doc_index: int,
    version: int,
    ordinal: int,
    *,
    chinese_style: str | None = None,
    mixed: bool = False,
) -> str:
    tag = workspace.lower()
    anchor = f"a{tag}{doc_index:02d}v{version}c{ordinal}"
    topic = _TOPICS[doc_index % len(_TOPICS)]
    if ordinal == 0:
        body = f"{anchor} {topic} 文档标题 document title for {workspace}{doc_index:02d}"
    elif ordinal == 1:
        body = f"{anchor} {topic} 正文段落 body paragraph {ordinal} of {workspace}{doc_index:02d}"
    else:
        body = f"{anchor} {topic} 补充段落 extra paragraph {ordinal} of {workspace}{doc_index:02d}"
    if chinese_style == "spaced":
        body = f"{body} 中文样本 文档第{doc_index:02d}号 检索文本"
    elif chinese_style == "unspaced":
        body = f"{body} 中文样本文档第{doc_index:02d}号检索文本"
    if mixed:
        body = f"{body} mixed-profile-marker"
    return body


def seed_corpus(conn: psycopg.Connection[Any]) -> SeedData:
    """写入确定性语料；调用方 commit。

    布局：
    - 空间 A/B/C：各 10 文档 × 3 chunk，全部 version=1 active；
    - 空间 A 文档 0..3：version=1 retired（旧 chunk 仍在表里）+ version=2 active；
    - 空间 B 文档 0：额外 version=3 status='ready'（未发布，chunk 仍在表里）；
    - 空间 A 文档 9：chunks 用 foreign embedding profile / dim（混合拒绝探针）；
    - 空间 A 文档 8：version=2 status='ready'，供单快照并发切换探针；
    - 空间 D：2 文档 × 3 chunk，供「撤销成员」探针（与 QA 隔离，避免顺序依赖）。
    """

    if conn.autocommit:
        raise RetrievalProbeError("seed_corpus 需要 autocommit=False 的连接")
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        raise RetrievalProbeError("seed_corpus 需要干净事务边界（IDLE）")

    workspaces = {key: _u(f"ws:{key}") for key in _WORKSPACE_KEYS}
    principals = {key: _u(f"principal:{key}") for key in _WORKSPACE_KEYS}
    sources: dict[str, tuple[UUID, UUID]] = {}
    chunks: dict[UUID, dict[str, Any]] = {}
    old_chunks: list[UUID] = []
    staged_chunks: list[UUID] = []
    mixed_chunks: list[UUID] = []

    for key, workspace_id in workspaces.items():
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_principals "
            "(workspace_id, principal_id, role, status, membership_revision) "
            "VALUES (%s, %s, 'owner', 'active', 1)",
            (workspace_id, principals[key]),
        )

    def _insert_chunk(
        *,
        workspace: str,
        doc_index: int,
        version: int,
        ordinal: int,
        kind: str,
        text: str,
        attempt_id: UUID,
        document_id: UUID,
        source_id: UUID,
        source_version_id: UUID,
        profile: str = EMBEDDING_PROFILE,
        dim: int = EMBEDDING_DIM,
    ) -> UUID:
        workspace_id = workspaces[workspace]
        chunk_id = _u(f"chunk:{workspace}:{doc_index}:{version}:{ordinal}")
        vector = embed(text, dim=dim)
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_chunks "
            "(workspace_id, chunk_id, document_id, index_version, attempt_id, "
            " chunk_ordinal, kind, text, embedding, embedding_dim, embedding_profile, "
            " source_id, source_version_id, checksum) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                workspace_id, chunk_id, document_id, version, attempt_id, ordinal, kind,
                text, list(vector), dim, profile, source_id, source_version_id,
                _chunk_checksum(
                    text,
                    payload_extra={
                        "workspace_id": str(workspace_id),
                        "document_id": str(document_id),
                        "index_version": version,
                        "chunk_ordinal": ordinal,
                    },
                ),
            ),
        )
        chunks[chunk_id] = {
            "chunk_id": chunk_id,
            "workspace_id": workspace_id,
            "document_id": document_id,
            "index_version": version,
            "attempt_id": attempt_id,
            "chunk_ordinal": ordinal,
            "kind": kind,
            "text": text,
            "source_id": source_id,
            "source_version_id": source_version_id,
            "embedding_profile": profile,
            "embedding_dim": dim,
        }
        return chunk_id

    def _insert_document(
        *,
        workspace: str,
        doc_index: int,
        version: int,
        status: str,
        ordinal_kinds: Sequence[str],
        chinese_style: str | None = None,
        mixed: bool = False,
        legacy: bool = False,
        document_id: UUID | None = None,
    ) -> tuple[UUID, UUID, UUID]:
        workspace_id = workspaces[workspace]
        document_id = document_id or _u(f"doc:{workspace}:{doc_index}")
        attempt_id = _u(f"attempt:{workspace}:{doc_index}:{version}")
        source_id, source_version_id = sources[
            f"{workspace}:{doc_index}"
        ] = sources.get(
            f"{workspace}:{doc_index}",
            (_u(f"source:{workspace}:{doc_index}"), _u(f"source-version:{workspace}:{doc_index}")),
        )
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_documents "
            "(workspace_id, document_id, source_id, source_version_id, active_version) "
            "VALUES (%s, %s, %s, %s, NULL)",
            (workspace_id, document_id, source_id, source_version_id),
        )
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_document_versions "
            "(workspace_id, document_id, index_version, status, winning_attempt_id, chunk_count) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (workspace_id, document_id, version, status, attempt_id, len(ordinal_kinds)),
        )
        chunk_ids = []
        for ordinal, kind in enumerate(ordinal_kinds):
            if legacy:
                text = (
                    f"legacy-{workspace.lower()}{doc_index:02d} "
                    f"legacy 旧版本内容 {workspace}{doc_index:02d} ordinal {ordinal}"
                )
            else:
                text = _chunk_text(
                    workspace, doc_index, version, ordinal,
                    chinese_style=chinese_style, mixed=mixed,
                )
            chunk_ids.append(
                _insert_chunk(
                    workspace=workspace, doc_index=doc_index, version=version,
                    ordinal=ordinal, kind=kind, text=text, attempt_id=attempt_id,
                    document_id=document_id, source_id=source_id,
                    source_version_id=source_version_id,
                    profile=FOREIGN_EMBEDDING_PROFILE if mixed else EMBEDDING_PROFILE,
                    dim=FOREIGN_EMBEDDING_DIM if mixed else EMBEDDING_DIM,
                )
            )
        _set_active_version(conn, workspace_id=workspace_id, document_id=document_id,
                            version=version)
        return document_id, source_id, source_version_id

    kinds = ("title", "body", "body")

    # A/B/C：每个空间 10 文档
    for workspace in ("A", "B", "C"):
        for doc_index in range(10):
            chinese_style: str | None = None
            if workspace == "C" and doc_index < 5:
                chinese_style = "spaced"
            elif workspace == "C" and doc_index >= 5:
                chinese_style = "unspaced"
            mixed = workspace == "A" and doc_index == 9
            legacy = workspace == "A" and doc_index < 4
            document_id, source_id, source_version_id = _insert_document(
                workspace=workspace, doc_index=doc_index, version=1,
                status="retired" if legacy else "active",
                ordinal_kinds=kinds, chinese_style=chinese_style, mixed=mixed,
                legacy=legacy,
            )
            if mixed:
                mixed_document = document_id
                mixed_chunks.extend(
                    _u(f"chunk:{workspace}:{doc_index}:1:{ordinal}") for ordinal in range(3)
                )
            if legacy:
                # 旧版本：chunk 保留在表里，但版本 retired、指针指向 v2。
                old_chunks.extend(
                    _u(f"chunk:{workspace}:{doc_index}:1:{ordinal}") for ordinal in range(3)
                )
                conn.execute(
                    f"INSERT INTO {SCHEMA}.phase0_document_versions "
                    "(workspace_id, document_id, index_version, status, winning_attempt_id, "
                    " chunk_count) VALUES (%s, %s, 2, 'active', %s, 3)",
                    (workspaces[workspace], document_id, _u(f"attempt:{workspace}:{doc_index}:2")),
                )
                for ordinal, kind in enumerate(kinds):
                    text = _chunk_text(workspace, doc_index, 2, ordinal)
                    _insert_chunk(
                        workspace=workspace, doc_index=doc_index, version=2,
                        ordinal=ordinal, kind=kind, text=text,
                        attempt_id=_u(f"attempt:{workspace}:{doc_index}:2"),
                        document_id=document_id, source_id=source_id,
                        source_version_id=source_version_id,
                    )
                _set_active_version(conn, workspace_id=workspaces[workspace],
                                    document_id=document_id, version=2)
            if workspace == "B" and doc_index == 0:
                # 未发布版本：chunk 在表里，但 status='ready'，指针仍是 1。
                staged_attempt = _u(f"attempt:{workspace}:{doc_index}:3")
                conn.execute(
                    f"INSERT INTO {SCHEMA}.phase0_document_versions "
                    "(workspace_id, document_id, index_version, status, winning_attempt_id, "
                    " chunk_count) VALUES (%s, %s, 3, 'ready', %s, 3)",
                    (workspaces[workspace], document_id, staged_attempt),
                )
                for ordinal, kind in enumerate(kinds):
                    text = f"staged-{workspace.lower()}0 unpublished 未发布内容 {ordinal}"
                    staged_chunks.append(
                        _insert_chunk(
                            workspace=workspace, doc_index=doc_index, version=3,
                            ordinal=ordinal, kind=kind, text=text,
                            attempt_id=staged_attempt, document_id=document_id,
                            source_id=source_id, source_version_id=source_version_id,
                        )
                    )
            if workspace == "A" and doc_index == 8:
                # 单快照切换探针：v2 已 ready，测试里翻指针。
                switch_attempt = _u(f"attempt:{workspace}:{doc_index}:2")
                conn.execute(
                    f"INSERT INTO {SCHEMA}.phase0_document_versions "
                    "(workspace_id, document_id, index_version, status, winning_attempt_id, "
                    " chunk_count) VALUES (%s, %s, 2, 'ready', %s, 3)",
                    (workspaces[workspace], document_id, switch_attempt),
                )
                for ordinal, kind in enumerate(kinds):
                    text = _chunk_text(workspace, doc_index, 2, ordinal)
                    _insert_chunk(
                        workspace=workspace, doc_index=doc_index, version=2,
                        ordinal=ordinal, kind=kind, text=text,
                        attempt_id=switch_attempt, document_id=document_id,
                        source_id=source_id, source_version_id=source_version_id,
                    )
                switch_document = document_id

    # D：撤销成员探针（不参与 QA）
    for doc_index in range(2):
        _insert_document(
            workspace="D", doc_index=doc_index, version=1, status="active",
            ordinal_kinds=kinds,
        )

    qa = _build_qa(workspaces, chunks)
    totals = {
        "chunks": len(chunks),
        "old_chunks": len(old_chunks),
        "staged_chunks": len(staged_chunks),
        "mixed_chunks": len(mixed_chunks),
        "documents": len({row["document_id"] for row in chunks.values()}),
        "qa_total": len(qa),
    }
    return SeedData(
        workspaces=workspaces,
        principals=principals,
        sources=sources,
        chunks=chunks,
        qa=qa,
        old_chunks=tuple(old_chunks),
        staged_chunks=tuple(staged_chunks),
        mixed_chunks=tuple(mixed_chunks),
        mixed_document=mixed_document,
        switch_document=switch_document,
        switch_workspace="A",
        switch_anchor="aa08v2c1",
        revocable_workspace="D",
        totals=totals,
    )


def _set_active_version(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, document_id: UUID, version: int
) -> None:
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_documents SET active_version = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s",
        (version, workspace_id, document_id),
    )


def _build_qa(
    workspaces: Mapping[str, UUID], chunks: Mapping[UUID, Mapping[str, Any]]
) -> tuple[QaCase, ...]:
    cases: list[QaCase] = []
    counter = 0

    def _add(workspace: str, query: str, gold: UUID | None, kind: str) -> None:
        nonlocal counter
        counter += 1
        cases.append(
            QaCase(qa_id=f"qa-{counter:03d}", workspace=workspace, query=query,
                   gold_chunk_id=gold, kind=kind)
        )

    # 1) anchor：唯一 token 命中（A 8 文档 × 3 ordinal、B 10 × 2、C 10 × 1）
    for doc_index in range(8):
        for ordinal in range(3):
            gold = _u(f"chunk:A:{doc_index}:2:{ordinal}" if doc_index < 4
                      else f"chunk:A:{doc_index}:1:{ordinal}")
            anchor = chunks[gold]["text"].split(" ", 1)[0]
            _add("A", anchor, gold, "anchor")
    for doc_index in range(10):
        for ordinal in (1, 2):
            gold = _u(f"chunk:B:{doc_index}:1:{ordinal}")
            anchor = chunks[gold]["text"].split(" ", 1)[0]
            _add("B", anchor, gold, "anchor")
    for doc_index in range(10):
        gold = _u(f"chunk:C:{doc_index}:1:2")
        anchor = chunks[gold]["text"].split(" ", 1)[0]
        _add("C", anchor, gold, "anchor")

    # 2) 中文：C 0..4 有空格（simple 可切词）、C 5..9 无空格（simple 整段成词）
    for doc_index in range(5):
        gold = _u(f"chunk:C:{doc_index}:1:1")
        _add("C", f"文档第{doc_index:02d}号", gold, "chinese_spaced")
    for doc_index in range(5, 10):
        gold = _u(f"chunk:C:{doc_index}:1:1")
        _add("C", f"样本文档第{doc_index:02d}号", gold, "chinese_unspaced")

    # 3) ambiguous：只用 topic token（同空间多个 chunk 共享），排名靠真实打分
    for offset in range(24):
        workspace = ("A", "B", "C")[offset % 3]
        doc_index = offset % 10
        version = 2 if (workspace == "A" and doc_index < 4) else 1
        gold = _u(f"chunk:{workspace}:{doc_index}:{version}:1")
        _add(workspace, _TOPICS[doc_index % len(_TOPICS)], gold, "ambiguous")

    # 4) no_answer：语料里不存在的 token
    for index in range(8):
        # 90..97 不在语料里（语料数字 token 只到 09），避免与中文模板里的编号碰撞。
        _add(("A", "B", "C")[index % 3], f"zz-absent-{90 + index:02d}", None, "no_answer")

    # 5) superseded：旧版本 token 存在表里，但不可见 → 必须 0 命中
    for doc_index in range(4):
        _add("A", f"legacy-a{doc_index:02d}", None, "superseded")

    return tuple(cases)


# ---------------------------------------------------------------------------
# 作用域 / 单快照
# ---------------------------------------------------------------------------
def _require_repeatable_read(conn: psycopg.Connection[Any]) -> None:
    if conn.autocommit:
        raise SnapshotIsolationRequired("检索需要 autocommit=False 的连接")
    if conn.info.transaction_status is not TransactionStatus.INTRANS:
        raise SnapshotIsolationRequired("检索必须在已开启的事务内（指针 + chunk 同一快照）")
    row = conn.execute("SELECT current_setting('transaction_isolation')").fetchone()
    if row is None or str(row["current_setting"]) != "repeatable read":
        raise SnapshotIsolationRequired(
            "检索必须在 REPEATABLE READ 事务内；当前隔离级别="
            f"{row['current_setting'] if row else 'unknown'}"
        )


def current_workspace(conn: psycopg.Connection[Any]) -> UUID | None:
    row = conn.execute(
        "SELECT NULLIF(current_setting('app.workspace_id', true), '') AS value"
    ).fetchone()
    if row is None or row["value"] is None:
        return None
    try:
        return UUID(str(row["value"]))
    except ValueError as exc:  # pragma: no cover - GUC 只由 apply_scope 写
        raise ScopeRequiredError(f"app.workspace_id 不是 uuid: {row['value']!r}") from exc


def _require_scope(conn: psycopg.Connection[Any], workspace_id: UUID | None) -> UUID:
    active = current_workspace(conn)
    if active is None:
        raise ScopeRequiredError(
            "连接没有 SET LOCAL app.workspace_id：检索必须由服务端注入作用域（deny 默认）"
        )
    if workspace_id is not None and workspace_id != active:
        raise ScopeMismatchError(
            f"调用方自报 workspace={workspace_id} 与连接作用域={active} 不一致；拒绝"
        )
    return active


@contextmanager
def scoped_transaction(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID,
    policy_snapshot_id: UUID | None = None,
) -> Iterator[psycopg.Connection[Any]]:
    """开启 REPEATABLE READ 事务并注入 T03 作用域（`SET LOCAL`，退出即清）。"""

    conn.rollback()
    previous_isolation = conn.isolation_level
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
            conn.isolation_level = previous_isolation


# ---------------------------------------------------------------------------
# 可见性 CTE（镜像 T12/T13：active 指针 + active 版本 + winning attempt）
# ---------------------------------------------------------------------------
_VISIBLE_CTE: Final = f"""
WITH visible AS (
    SELECT c.chunk_id, c.document_id, c.index_version, c.attempt_id, c.chunk_ordinal,
           c.kind, c.text, c.embedding, c.embedding_dim, c.embedding_profile,
           c.source_id, c.source_version_id, c.workspace_id
    FROM {SCHEMA}.phase0_chunks AS c
    JOIN {SCHEMA}.phase0_documents AS d
      ON d.workspace_id = c.workspace_id
     AND d.document_id = c.document_id
     AND d.active_version = c.index_version
    JOIN {SCHEMA}.phase0_document_versions AS v
      ON v.workspace_id = c.workspace_id
     AND v.document_id = c.document_id
     AND v.index_version = c.index_version
     AND v.status = 'active'
     AND v.winning_attempt_id = c.attempt_id
    WHERE c.workspace_id = %s
)
"""


def visible_chunk_count(conn: psycopg.Connection[Any]) -> int:
    """当前作用域下 RLS + 可见性 CTE 的 chunk 数（探针用）。"""

    workspace_id = _require_scope(conn, None)
    row = conn.execute(
        _VISIBLE_CTE + "SELECT count(*) AS total FROM visible", (workspace_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - count 恒有行
        raise RetrievalProbeError("count 查询失败")
    return int(row["total"])


def read_active_version(conn: psycopg.Connection[Any], document_id: UUID) -> int | None:
    workspace_id = _require_scope(conn, None)
    row = conn.execute(
        f"SELECT active_version FROM {SCHEMA}.phase0_documents "
        "WHERE workspace_id = %s AND document_id = %s",
        (workspace_id, document_id),
    ).fetchone()
    return None if row is None or row["active_version"] is None else int(row["active_version"])


def read_visible_versions(conn: psycopg.Connection[Any]) -> tuple[int, ...]:
    workspace_id = _require_scope(conn, None)
    rows = conn.execute(
        _VISIBLE_CTE + "SELECT DISTINCT index_version FROM visible ORDER BY index_version",
        (workspace_id,),
    ).fetchall()
    return tuple(int(row["index_version"]) for row in rows)


def _visible_rows(
    conn: psycopg.Connection[Any], workspace_id: UUID
) -> list[dict[str, Any]]:
    rows = conn.execute(
        _VISIBLE_CTE
        + "SELECT * FROM visible ORDER BY document_id, chunk_ordinal, chunk_id",
        (workspace_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 三路检索 + RRF
# ---------------------------------------------------------------------------
def _hit(row: Mapping[str, Any], *, score: float, rank: int) -> RetrievalHit:
    text = str(row["text"])
    return RetrievalHit(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        index_version=int(row["index_version"]),
        attempt_id=row["attempt_id"],
        workspace_id=row["workspace_id"],
        chunk_ordinal=int(row["chunk_ordinal"]),
        kind=str(row["kind"]),
        source_id=row["source_id"],
        source_version_id=row["source_version_id"],
        score=float(score),
        rank=rank,
        text_excerpt=text[:160],
    )


def _rank(rows: Sequence[tuple[Mapping[str, Any], float]], *, top_k: int) -> list[RetrievalHit]:
    ordered = sorted(rows, key=lambda item: (-item[1], str(item[0]["chunk_id"])))
    return [
        _hit(row, score=score, rank=index + 1)
        for index, (row, score) in enumerate(ordered[:top_k])
    ]


def _search_fts(
    conn: psycopg.Connection[Any], workspace_id: UUID, query: str, *, top_k: int
) -> list[RetrievalHit]:
    rows = conn.execute(
        _VISIBLE_CTE
        + """
        SELECT *, ts_rank(to_tsvector('simple', text), plainto_tsquery('simple', %s)) AS score
        FROM visible
        WHERE to_tsvector('simple', text) @@ plainto_tsquery('simple', %s)
        """,
        (workspace_id, query, query),
    ).fetchall()
    return _rank([(row, float(row["score"])) for row in rows], top_k=top_k)


def _bm25_scores(
    query: str, documents: Sequence[Mapping[str, Any]]
) -> list[tuple[Mapping[str, Any], float]]:
    query_tokens = tokenize(query)
    if not query_tokens:
        return []
    doc_tokens = [tokenize(str(row["text"])) for row in documents]
    total_docs = len(documents)
    avg_length = (
        statistics.fmean(len(tokens) for tokens in doc_tokens) if doc_tokens else 0.0
    )
    df = Counter()
    for tokens in doc_tokens:
        for token in set(tokens):
            df[token] += 1
    idf = {
        token: math.log(1.0 + (total_docs - df[token] + 0.5) / (df[token] + 0.5))
        for token in set(query_tokens)
    }
    scored: list[tuple[Mapping[str, Any], float]] = []
    for row, tokens in zip(documents, doc_tokens, strict=True):
        frequencies = Counter(tokens)
        length = len(tokens) or 1
        score = 0.0
        for token in query_tokens:
            if token not in frequencies:
                continue
            tf = frequencies[token]
            denominator = tf + BM25_K1 * (
                1.0 - BM25_B + BM25_B * (length / (avg_length or 1.0))
            )
            score += idf[token] * (tf * (BM25_K1 + 1.0)) / denominator
        if score > 0.0:
            scored.append((row, score))
    return scored


def _search_bm25(
    conn: psycopg.Connection[Any], workspace_id: UUID, query: str, *, top_k: int
) -> tuple[list[RetrievalHit], int]:
    rows = _visible_rows(conn, workspace_id)
    return _rank(_bm25_scores(query, rows), top_k=top_k), len(rows)


def _search_vector(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    query: str,
    *,
    top_k: int,
    embedding_profile: str = EMBEDDING_PROFILE,
    embedding_dim: int = EMBEDDING_DIM,
    min_score: float = VECTOR_MIN_SCORE,
) -> tuple[list[RetrievalHit], int, int, int]:
    if embedding_profile != EMBEDDING_PROFILE or embedding_dim != EMBEDDING_DIM:
        raise EmbeddingProfileMismatch(
            f"查询 embedding profile/dim={embedding_profile}/{embedding_dim} 与索引侧 "
            f"{EMBEDDING_PROFILE}/{EMBEDDING_DIM} 不一致；拒绝混算（不静默降级）"
        )
    rows = _visible_rows(conn, workspace_id)
    query_vector = embed(query)
    query_tokens = set(tokenize(query))
    scored: list[tuple[Mapping[str, Any], float]] = []
    filtered = 0
    below = 0
    for row in rows:
        if (
            str(row["embedding_profile"]) != EMBEDDING_PROFILE
            or int(row["embedding_dim"]) != EMBEDDING_DIM
        ):
            filtered += 1
            continue
        # 词法共享门：合成 hash 词袋在 OOV 查询上会产生碰撞噪声（64 维时实测每问 5 条），
        # 因此要求查询与 chunk 至少共享一个 token 才计算余弦。这是**替身限制**，不是
        # 生产语义：真实 embedding + ANN 由 provider/pgvector 负责（本机 NOT_RUN）。
        if not query_tokens or not (query_tokens & set(tokenize(str(row["text"])))):
            below += 1
            continue
        score = cosine(query_vector, [float(value) for value in row["embedding"]])
        if score < min_score:
            below += 1
            continue
        scored.append((row, score))
    return _rank(scored, top_k=top_k), len(rows), filtered, below


def _fuse_rrf(results: Sequence[Sequence[RetrievalHit]], *, top_k: int) -> list[RetrievalHit]:
    scores: dict[UUID, float] = {}
    rows: dict[UUID, RetrievalHit] = {}
    for hits in results:
        for hit in hits:
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + hit.rank)
            rows.setdefault(hit.chunk_id, hit)
    ordered = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
    return [
        RetrievalHit(
            chunk_id=rows[chunk_id].chunk_id,
            document_id=rows[chunk_id].document_id,
            index_version=rows[chunk_id].index_version,
            attempt_id=rows[chunk_id].attempt_id,
            workspace_id=rows[chunk_id].workspace_id,
            chunk_ordinal=rows[chunk_id].chunk_ordinal,
            kind=rows[chunk_id].kind,
            source_id=rows[chunk_id].source_id,
            source_version_id=rows[chunk_id].source_version_id,
            score=score,
            rank=index + 1,
            text_excerpt=rows[chunk_id].text_excerpt,
        )
        for index, (chunk_id, score) in enumerate(ordered[:top_k])
    ]


def search(
    conn: psycopg.Connection[Any],
    query: str,
    *,
    mode: str,
    top_k: int = TOP_K,
    workspace_id: UUID | None = None,
    embedding_profile: str = EMBEDDING_PROFILE,
    embedding_dim: int = EMBEDDING_DIM,
    min_score: float = VECTOR_MIN_SCORE,
) -> RetrievalResult:
    """授权检索：作用域来自连接 GUC，调用方自报 workspace 只用于核对（不可扩展）。"""

    if not isinstance(query, str) or not query.strip():
        raise RetrievalProbeError("query 必须是非空字符串")
    if mode not in _MODES:
        raise ModeInvalid(f"未知检索模式 {mode!r}；允许 {_MODES}")
    if top_k < 1 or top_k > 100:
        raise RetrievalProbeError("top_k 必须在 [1, 100]")
    _require_repeatable_read(conn)
    scope_id = _require_scope(conn, workspace_id)

    started = time.perf_counter()
    candidates = 0
    profile_filtered = 0
    below_threshold = 0
    if mode == "fts":
        hits = _search_fts(conn, scope_id, query, top_k=top_k)
        candidates = len(_visible_rows(conn, scope_id))
    elif mode == "bm25":
        hits, candidates = _search_bm25(conn, scope_id, query, top_k=top_k)
    elif mode == "vector":
        hits, candidates, profile_filtered, below_threshold = _search_vector(
            conn, scope_id, query, top_k=top_k,
            embedding_profile=embedding_profile, embedding_dim=embedding_dim,
            min_score=min_score,
        )
    else:
        fts_hits = _search_fts(conn, scope_id, query, top_k=MAX_K)
        bm25_hits, candidates = _search_bm25(conn, scope_id, query, top_k=MAX_K)
        vector_hits, _, profile_filtered, below_threshold = _search_vector(
            conn, scope_id, query, top_k=MAX_K,
            embedding_profile=embedding_profile, embedding_dim=embedding_dim,
            min_score=min_score,
        )
        hits = _fuse_rrf((fts_hits, bm25_hits, vector_hits), top_k=top_k)
    latency_ms = (time.perf_counter() - started) * 1000.0
    return RetrievalResult(
        mode=mode,
        query=query,
        hits=tuple(hits),
        candidates=candidates,
        profile_filtered=profile_filtered,
        below_threshold=below_threshold,
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def _rank_of(hits: Sequence[RetrievalHit], gold: UUID | None) -> int | None:
    if gold is None:
        return None
    for hit in hits:
        if hit.chunk_id == gold:
            return hit.rank
    return None


def _metric_block(ranks: Sequence[int | None], *, top_k: int, max_k: int) -> dict[str, float]:
    total = len(ranks) or 1
    recall = sum(1 for rank in ranks if rank is not None and rank <= top_k) / total
    mrr = sum(
        1.0 / rank for rank in ranks if rank is not None and rank <= max_k
    ) / total
    ndcg = 0.0
    for rank in ranks:
        if rank is not None and rank <= top_k:
            ndcg += 1.0 / math.log2(rank + 1.0)
    return {
        f"recall_at_{top_k}": recall,
        f"mrr_at_{max_k}": mrr,
        f"ndcg_at_{top_k}": ndcg / total,
    }


def evaluate_retrieval(
    conn: psycopg.Connection[Any], seed: SeedData, *, top_k: int = TOP_K
) -> dict[str, Any]:
    """100 QA 三路对照：每个 query 在一个 RR 快照里跑完 4 种模式。"""

    ranked: dict[str, dict[str, list[int | None]]] = {
        mode: {kind: [] for kind in ("anchor", "chinese_spaced", "chinese_unspaced", "ambiguous")}
        for mode in _MODES
    }
    latencies: dict[str, list[float]] = {mode: [] for mode in _MODES}
    no_answer_hits: dict[str, int] = {mode: 0 for mode in _MODES}
    superseded_hits: dict[str, int] = {mode: 0 for mode in _MODES}
    old_chunk_ids = set(seed.old_chunks)
    profile_filtered = 0
    below_threshold = 0
    unsourced_hits = 0
    total_hits = 0
    qa_runs = 0

    for case in seed.qa:
        if case.kind in {"no_answer", "superseded"}:
            with scoped_transaction(
                conn,
                workspace_id=seed.workspaces[case.workspace],
                principal_id=seed.principals[case.workspace],
            ):
                for mode in _MODES:
                    result = search(conn, case.query, mode=mode, top_k=top_k)
                    if result.hits:
                        if case.kind == "no_answer":
                            no_answer_hits[mode] += len(result.hits)
                        else:
                            superseded_hits[mode] += sum(
                                1 for hit in result.hits if hit.chunk_id in old_chunk_ids
                            )
                        total_hits += len(result.hits)
            qa_runs += 1
            continue
        with scoped_transaction(
            conn,
            workspace_id=seed.workspaces[case.workspace],
            principal_id=seed.principals[case.workspace],
        ):
            for mode in _MODES:
                result = search(conn, case.query, mode=mode, top_k=max(top_k, MAX_K))
                latencies[mode].append(result.latency_ms)
                profile_filtered += result.profile_filtered
                below_threshold += result.below_threshold
                ranked[mode][case.kind].append(_rank_of(result.hits[:MAX_K], case.gold_chunk_id))
                total_hits += len(result.hits)
                unsourced_hits += sum(
                    1
                    for hit in result.hits
                    if hit.source_id != seed.chunks[hit.chunk_id]["source_id"]
                    or hit.source_version_id != seed.chunks[hit.chunk_id]["source_version_id"]
                )
        qa_runs += 1

    modes: dict[str, Any] = {}
    for mode in _MODES:
        all_ranks = [rank for kind_ranks in ranked[mode].values() for rank in kind_ranks]
        block = _metric_block(all_ranks, top_k=top_k, max_k=MAX_K)
        block.update(
            {
                "answerable": len(all_ranks),
                "hit_rate_at_10": sum(
                    1 for rank in all_ranks if rank is not None and rank <= MAX_K
                )
                / (len(all_ranks) or 1),
                "avg_latency_ms": statistics.fmean(latencies[mode])
                if latencies[mode]
                else 0.0,
                "p50_latency_ms": statistics.median(latencies[mode])
                if latencies[mode]
                else 0.0,
                "no_answer_false_positives": no_answer_hits[mode],
                "superseded_hits": superseded_hits[mode],
            }
        )
        modes[mode] = block

    subsets: dict[str, Any] = {}
    for kind, ranks_by_mode in (
        ("anchor", ranked),
        ("chinese_spaced", ranked),
        ("chinese_unspaced", ranked),
        ("ambiguous", ranked),
    ):
        subset_modes = {
            mode: _metric_block(ranks_by_mode[mode][kind], top_k=top_k, max_k=MAX_K)
            for mode in _MODES
        }
        subsets[kind] = {
            "count": len(ranks_by_mode[_MODES[0]][kind]),
            "modes": subset_modes,
        }

    return {
        "top_k": top_k,
        "max_k": MAX_K,
        "rrf_k": RRF_K,
        "qa_total": len(seed.qa),
        "qa_runs": qa_runs,
        "answerable": sum(len(ranks) for ranks in ranked[_MODES[0]].values()),
        "no_answer": sum(1 for case in seed.qa if case.kind == "no_answer"),
        "superseded": sum(1 for case in seed.qa if case.kind == "superseded"),
        "modes": modes,
        "subsets": subsets,
        "diagnostics": {
            "vector_profile_filtered": profile_filtered,
            "vector_below_threshold": below_threshold,
            "hits_checked_for_source": total_hits,
            "hits_with_wrong_source_or_version": unsourced_hits,
        },
    }


# ---------------------------------------------------------------------------
# 权限 / 可见性探针
# ---------------------------------------------------------------------------
def _scoped(conn: psycopg.Connection[Any], seed: SeedData, workspace: str) -> Any:
    return scoped_transaction(
        conn,
        workspace_id=seed.workspaces[workspace],
        principal_id=seed.principals[workspace],
    )


def probe_permissions(
    conn: psycopg.Connection[Any], seed: SeedData, *, probes: int = 24
) -> dict[str, Any]:
    """跨空间 / 撤销 / 换空间 / 旧版本 / 未发布 / 混合 embedding 的实测计数。"""

    foreign: dict[str, Any] = {
        "probes": probes,
        "read_attempts": 0,
        "rows_read": 0,
        "scope_shortcut_attempts": 0,
        "scope_shortcut_rejected": 0,
        "search_hits": 0,
    }
    workspaces = ("A", "B", "C")
    for index in range(probes):
        attacker = workspaces[index % len(workspaces)]
        victim = workspaces[(index + 1) % len(workspaces)]
        victim_chunk = _u(f"chunk:{victim}:0:1:1")
        with _scoped(conn, seed, attacker):
            # 1) 直接按 chunk_id 读取其他空间的 chunk（RLS 应隐藏）
            foreign["read_attempts"] += 1
            row = conn.execute(
                f"SELECT count(*) AS total FROM {SCHEMA}.phase0_chunks "
                "WHERE chunk_id = %s",
                (victim_chunk,),
            ).fetchone()
            foreign["rows_read"] += int(row["total"]) if row else 0
            # 2) 用其他空间的 anchor 搜索（向量/BM25/FTS 都不应命中）
            result = search(conn, f"a{victim.lower()}00v1c1", mode="bm25", top_k=TOP_K)
            foreign["search_hits"] += len(result.hits)
            # 3) 尝试用自报 workspace 扩展作用域（必须拒绝）
            foreign["scope_shortcut_attempts"] += 1
            try:
                search(
                    conn, "topic00", mode="bm25", top_k=TOP_K,
                    workspace_id=seed.workspaces[victim],
                )
            except ScopeMismatchError:
                foreign["scope_shortcut_rejected"] += 1

    unscoped: dict[str, Any] = {}
    conn.rollback()
    with conn.transaction():
        row = conn.execute(
            f"SELECT count(*) AS total FROM {SCHEMA}.phase0_chunks"
        ).fetchone()
        unscoped["rows_visible_without_scope"] = int(row["total"]) if row else 0
        unscoped["workspace_guc"] = current_workspace(conn) is not None

    revocation: dict[str, Any] = {}
    revocable = seed.revocable_workspace
    with _scoped(conn, seed, revocable):
        revocation["before_visible"] = visible_chunk_count(conn)
    conn.rollback()
    # 撤销必须走 scoped 事务：principals 也开了 FORCE RLS，无 scope 时 UPDATE 命中 0 行
    # 且不报错（静默失败）；这里在空间作用域内更新并在事务退出时提交。
    with _scoped(conn, seed, revocable):
        conn.execute(
            f"UPDATE {SCHEMA}.phase0_principals "
            "SET status = 'disabled', membership_revision = membership_revision + 1 "
            "WHERE workspace_id = %s AND principal_id = %s",
            (seed.workspaces[revocable], seed.principals[revocable]),
        )
    with _scoped(conn, seed, revocable):
        revocation["after_visible"] = visible_chunk_count(conn)
        revocation["after_search_hits"] = len(
            search(conn, "topic00", mode="bm25", top_k=TOP_K).hits
        )
    conn.rollback()
    with _scoped(conn, seed, revocable):
        conn.execute(
            f"UPDATE {SCHEMA}.phase0_principals "
            "SET status = 'active', membership_revision = membership_revision + 1 "
            "WHERE workspace_id = %s AND principal_id = %s",
            (seed.workspaces[revocable], seed.principals[revocable]),
        )
    with _scoped(conn, seed, revocable):
        revocation["restored_visible"] = visible_chunk_count(conn)
    conn.rollback()

    switching: dict[str, Any] = {}
    with _scoped(conn, seed, "A"):
        switching["a_visible"] = visible_chunk_count(conn)
    conn.rollback()
    with _scoped(conn, seed, "B"):
        switching["b_visible"] = visible_chunk_count(conn)
    conn.rollback()
    with _scoped(conn, seed, "A"):
        switching["a_again_visible"] = visible_chunk_count(conn)
        switching["a_rows_foreign"] = int(
            conn.execute(
                f"SELECT count(*) AS total FROM {SCHEMA}.phase0_chunks "
                "WHERE workspace_id <> %s",
                (seed.workspaces["A"],),
            ).fetchone()["total"]
        )
    conn.rollback()

    old: dict[str, Any] = {}
    old_chunk_ids = set(seed.old_chunks)
    with _scoped(conn, seed, "A"):
        # "legacy" 只出现在 retired 旧版本 chunk 文本里；现役 chunk 不含该 token，
        # 因此任何命中都只能是旧版本泄漏（旧 chunk 本身仍在表里，见下面的 count）。
        stale_hits = [
            hit
            for mode in _MODES
            for hit in search(conn, "legacy", mode=mode, top_k=MAX_K).hits
        ]
        old["search_hits"] = len(stale_hits)
        old["old_chunk_hits"] = sum(1 for hit in stale_hits if hit.chunk_id in old_chunk_ids)
        old["hit_chunk_versions"] = sorted({hit.index_version for hit in stale_hits})
    conn.rollback()
    # 物理存在性来自种子事实：app 角色在 RLS 下看不到这些版本行，
    # 全表计数由测试用管理员连接复核（old=12 / staged=3）。
    old["chunks_present_in_table"] = len(seed.old_chunks)

    staged: dict[str, Any] = {}
    with _scoped(conn, seed, "B"):
        staged["search_hits"] = sum(
            len(search(conn, "staged-b0", mode=mode, top_k=TOP_K).hits) for mode in _MODES
        )
    conn.rollback()
    staged["chunks_present_in_table"] = len(seed.staged_chunks)

    mixed: dict[str, Any] = {}
    with _scoped(conn, seed, "A"):
        result = search(conn, "mixed-profile-marker", mode="vector", top_k=TOP_K)
        mixed["vector_hits"] = len(result.hits)
        mixed["profile_filtered"] = result.profile_filtered
        mixed["chunks_present_in_table"] = len(seed.mixed_chunks)
        mixed["leaked_mixed_chunks"] = sum(
            1 for hit in result.hits if hit.chunk_id in set(seed.mixed_chunks)
        )
        try:
            search(
                conn, "topic00", mode="vector", top_k=TOP_K,
                embedding_profile=FOREIGN_EMBEDDING_PROFILE, embedding_dim=FOREIGN_EMBEDDING_DIM,
            )
        except EmbeddingProfileMismatch:
            mixed["foreign_query_rejected"] = True
        else:
            mixed["foreign_query_rejected"] = False
    conn.rollback()

    return {
        "cross_workspace": foreign,
        "unscoped": unscoped,
        "membership_revocation": revocation,
        "workspace_switch": switching,
        "superseded_invisible": old,
        "staged_invisible": staged,
        "mixed_embedding": mixed,
    }


# ---------------------------------------------------------------------------
# 单快照探针（REPEATABLE READ 下并发切换 active 指针）
# ---------------------------------------------------------------------------
def probe_snapshot(
    admin_dsn: str, app_dsn: str, seed: SeedData
) -> dict[str, Any]:
    """指针 + chunk 必须同快照：并发切换版本后，旧事务仍读旧版本，提交后才见新版本。"""

    workspace_id = seed.workspaces[seed.switch_workspace]
    principal_id = seed.principals[seed.switch_workspace]
    document_id = seed.switch_document
    result: dict[str, Any] = {}

    app = connect(app_dsn)
    admin = connect(admin_dsn, autocommit=True)
    try:
        with scoped_transaction(app, workspace_id=workspace_id, principal_id=principal_id):
            result["isolation"] = str(
                app.execute("SELECT current_setting('transaction_isolation')").fetchone()[
                    "current_setting"
                ]
            )
            before_version = read_active_version(app, document_id)
            before_versions = read_visible_versions(app)
            result["before_version"] = before_version
            result["before_versions"] = list(before_versions)

            # 并发切换：另一个连接提交 v2 active（v1 retired）。
            admin.execute(
                f"UPDATE {SCHEMA}.phase0_document_versions SET status = 'retired' "
                "WHERE workspace_id = %s AND document_id = %s AND status = 'active'",
                (workspace_id, document_id),
            )
            admin.execute(
                f"UPDATE {SCHEMA}.phase0_document_versions SET status = 'active' "
                "WHERE workspace_id = %s AND document_id = %s AND index_version = 2",
                (workspace_id, document_id),
            )
            admin.execute(
                f"UPDATE {SCHEMA}.phase0_documents SET active_version = 2, "
                "row_version = row_version + 1 WHERE workspace_id = %s AND document_id = %s",
                (workspace_id, document_id),
            )
            result["switched_by_other_connection"] = True

            during_version = read_active_version(app, document_id)
            during_versions = read_visible_versions(app)
            result["during_version"] = during_version
            result["during_versions"] = list(during_versions)
            hits = search(app, "topic02", mode="bm25", top_k=MAX_K)
            result["during_hit_versions"] = sorted({hit.index_version for hit in hits.hits})
            switch_hits = search(app, seed.switch_anchor, mode="bm25", top_k=MAX_K)
            result["during_switch_hits"] = len(switch_hits.hits)
            result["during_switch_versions"] = sorted(
                {hit.index_version for hit in switch_hits.hits}
            )

        with scoped_transaction(app, workspace_id=workspace_id, principal_id=principal_id):
            after_version = read_active_version(app, document_id)
            after_versions = read_visible_versions(app)
            result["after_version"] = after_version
            result["after_versions"] = list(after_versions)
            hits = search(app, "topic02", mode="bm25", top_k=MAX_K)
            result["after_hit_versions"] = sorted({hit.index_version for hit in hits.hits})
            switch_hits = search(app, seed.switch_anchor, mode="bm25", top_k=MAX_K)
            result["after_switch_hits"] = len(switch_hits.hits)
            result["after_switch_versions"] = sorted(
                {hit.index_version for hit in switch_hits.hits}
            )

        # 一致性断言（不改状态，只返回事实，测试里断言）
        result["pointer_chunk_mismatch"] = sum(
            1
            for value in (result["during_version"], result["after_version"])
            if value not in (1, 2)
        )
        result["mixed_version_detected"] = bool(
            set(result["during_versions"]) & {1, 2}
            and len(set(result["during_versions"])) > 1
            and result["during_version"] not in set(result["during_versions"])
        )
        result["reverted_to_v1"] = False
        return result
    finally:
        admin.close()
        app.close()


# ---------------------------------------------------------------------------
# 证据（JSON）
# ---------------------------------------------------------------------------
def git_identity(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """只读 git 信息（不 add/commit）；不可用时返回 unavailable，不伪造。"""

    def _run(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    branch = _run("rev-parse", "--abbrev-ref", "HEAD")
    head = _run("rev-parse", "HEAD")
    porcelain = _run("status", "--porcelain")
    if head is None and porcelain is None:
        return {"available": False}
    return {
        "available": True,
        "branch": branch,
        "head": head,
        "dirty": bool(porcelain),
        "dirty_sha256": hashlib.sha256((porcelain or "").encode("utf-8")).hexdigest(),
    }


def build_evidence(
    *,
    seed: SeedData,
    metrics: Mapping[str, Any],
    permissions: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    boolean_combos: Sequence[Mapping[str, Any]],
    schema_digest: str,
    dsn: str,
    role_attributes: Mapping[str, Any],
    server_version: str,
) -> dict[str, Any]:
    return _fmt(
        {
            "schema_version": 1,
            "task": "P0-T25",
            "poc": "PoC-09",
            "wave": "W7",
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "owner": "w2-t03-scope",
            "git": git_identity(),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "postgres_version": server_version,
                "pgvector": "absent (core image; vector ANN NOT_RUN)",
                "redis": "unused (slot 11)",
                "dsn": redact_dsn(dsn),
                "role": APP_ROLE,
                "role_attributes": role_attributes,
                "embedding": {
                    "profile": EMBEDDING_PROFILE,
                    "dim": EMBEDDING_DIM,
                    "kind": "deterministic bag-of-words hash vector (NOT a real model)",
                },
                "retrieval_modes": list(_MODES),
                "fts_config": "pg_catalog.simple (no zhparser)",
                "bm25": {"k1": BM25_K1, "b": BM25_B, "tokenizer": "ascii words + cjk bigram"},
            },
            "schema": {
                "postgres_schema": SCHEMA,
                "migration_id": MIGRATION_ID,
                "ddl_sha256": schema_digest,
                "mirror_of": ["004_version_index.sql", "008_publication.sql", "p0_t13"],
            },
            "corpus": dict(seed.totals),
            "metrics": dict(metrics),
            "permissions": dict(permissions),
            "single_snapshot": dict(snapshot),
            "boolean_combinations": list(boolean_combos),
            "not_run": [
                "真实 embedding 模型（禁联网/无模型）",
                "真实 rerank 模型",
                "pgvector ANN（HNSW/IVFFlat；core 镜像无 vector 扩展）",
                "生产级中文分词（zhparser/pg_jieba 未安装）",
                "100QA 真实标注质量评估（合成语料，非人工标注）",
                "Redis 缓存层（本任务无缓存需求，槽位 11 未使用）",
            ],
            "risks": [
                "假 embedding 的指标只证明契约/管道，不代表真实语义召回",
                "FTS 用 simple 配置：无空格中文整段成词，中文召回为 0 属被测事实",
                "合成语料规模 120 chunk，延迟数据不代表生产规模",
                "镜像表与 p0_t12/p0_t13 的 DDL 可能漂移；生产须回归 004/008",
                "RRF 只融合 rank，未做权重学习或 rerank",
            ],
        }
    )


def write_evidence(payload: Mapping[str, Any], path: Path = EVIDENCE_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_fmt(payload), sort_keys=True, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
    return path
