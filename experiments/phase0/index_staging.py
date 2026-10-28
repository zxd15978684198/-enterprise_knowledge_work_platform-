"""P0-T12：版本 staging、Chunk/Embedding 与引用模型（真实 PostgreSQL 探针）。

验收依据
--------
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T12；
* ``docs/poc/07_Contract_Closure.md`` C-01 §4/§6/§7（build 键
  ``(workspace, document_id, index_version)``、chunk 键含 attempt、staging 完成不可见、
  manifest checksum、row_version）与 C-09 §6/§8（``execution_generation``、同 version
  新 attempt、旧 attempt 的 epoch 失配必须 STALE）、C-10（asset_bindings 不在本任务）；
* ``docs/poc/05_Phase0_Acceptance.md`` PoC-03 强制项：staging 未发布新检索 0、
  重复批次行数不增长、旧 attempt 写当前分区 0、manifest 缺任一必需项不能 ready。

本模块做什么 / 不做什么
----------------------
做：不可见版本分区（``phase0_document_versions``）、attempt fencing（generation +
attempt CAS）、chunk 序号唯一与结构化 locator、确定性假 embedding、manifest 必需项
与 checksum、以及**只读可见性探针** ``search_visible_chunks``。

不做：**不切换 active 指针**（那是 T13）、不做对象存储真实读写（MinIO/S3 本机没有，
用可注入的 ``InMemoryObjectStore`` 明确标记）、不做 pgvector 向量检索（本机
``pg_available_extensions`` 无 ``vector``，已实测）。这三项在交付报告里按
NOT_RUN / PARTIAL 记录，不冒充通过。

对 T11 的复用
-------------
``from_parsed_document`` 直接吃 ``simple_reader.ParseResult`` 的 block：
``Locator`` 原样进库（``locator.to_dict()``），``chunk_ordinal`` 按块顺序分配。
没有把 locator 降级成偏移量——T11 的 offset-free schema 是硬约束（``locator_map``
会拒绝带 offset/start/end 的 locator）。

状态机边界
----------
本模块只做 T12 的**版本生命周期** ``staging → ready → (T13) active → retired`` 与
``staging/failed → ready``；它和 T08 的投递五态（pending/dispatching/retry_wait/
dispatched/dead_letter）是两套不同域的状态，不互相复用、也不复制。T13 才允许写
``phase0_documents.active_version``，本模块任何函数都不写它。

事务边界
--------
所有函数都假设调用方给了 ``autocommit=False`` 的连接，并且**不替调用方提交**；
事务由用例/编排层负责（与 T07/T08 一致）。SQL 里的触发器负责最后一道不变式。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from locator_map import Locator
from simple_reader import ParseResult

__all__ = [
    "EMBEDDING_DIM",
    "EMBEDDING_PROFILE",
    "MIGRATION_ID",
    "MIGRATION_PATH",
    "REQUIRED_MANIFEST_KEYS",
    "SCHEMA",
    "Attempt",
    "BatchResult",
    "ChunkInput",
    "DocumentState",
    "EmbeddingCountMismatch",
    "EmbeddingDimensionMismatch",
    "InMemoryObjectStore",
    "IndexStagingError",
    "ManifestIncomplete",
    "ManifestRecord",
    "ObjectMissing",
    "ObjectStore",
    "StaleAttemptError",
    "StaleGenerationError",
    "VersionNotStageable",
    "VersionState",
    "apply_migration",
    "begin_attempt",
    "bump_desired_generation",
    "build_manifest",
    "canonical_checksum",
    "chunk_checksum",
    "connect",
    "content_checksum",
    "create_source",
    "deterministic_embedding",
    "ensure_document",
    "from_parsed_document",
    "get_attempt",
    "get_document",
    "get_manifest",
    "get_version",
    "list_chunks",
    "mark_version_ready",
    "migration_sha256",
    "reset_schema",
    "search_visible_chunks",
    "stage_chunk_batch",
    "write_manifest",
]

SCHEMA: Final = "p0_t12"
MIGRATION_ID: Final = "004_version_index"
MIGRATION_PATH: Final = (
    Path(__file__).resolve().parent / "db" / "migrations" / "004_version_index.sql"
)

#: 确定性假 embedding 的固定维度与 profile 名（真实模型 NOT_RUN）。
EMBEDDING_DIM: Final = 8
EMBEDDING_PROFILE: Final = "fake.deterministic.v1"

#: manifest 必需项（缺任一 → 只能 draft，版本不能 ready）。
REQUIRED_MANIFEST_KEYS: Final[tuple[str, ...]] = (
    "schema",
    "chunk_count",
    "chunk_digest",
    "embedding_profile",
    "embedding_dim",
    "source_id",
    "source_version_id",
    "object_key",
    "object_sha256",
    "locator_schema",
    "format",
)

_HEX64 = "0123456789abcdef"
_MAX_DETAIL = 512


# ---------------------------------------------------------------------------
# 错误码（测试按 code 断言，不匹配自然语言）
# ---------------------------------------------------------------------------
class IndexStagingError(Exception):
    code: str = "INDEX_STAGING_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class StaleAttemptError(IndexStagingError):
    """旧 attempt 想写不是自己持有的分区（C-09 STALE_ATTEMPT）。"""

    code = "STALE_ATTEMPT"


class StaleGenerationError(IndexStagingError):
    """desired generation 已推进，旧 attempt 的写入一律拒绝。"""

    code = "STALE_GENERATION"


class VersionNotStageable(IndexStagingError):
    code = "VERSION_NOT_STAGEABLE"


class ManifestIncomplete(IndexStagingError):
    code = "MANIFEST_INCOMPLETE"


class EmbeddingDimensionMismatch(IndexStagingError):
    code = "EMBEDDING_DIMENSION_MISMATCH"


class EmbeddingCountMismatch(IndexStagingError):
    code = "EMBEDDING_COUNT_MISMATCH"


class ObjectMissing(IndexStagingError):
    code = "OBJECT_MISSING"


class BatchRejected(IndexStagingError):
    code = "BATCH_REJECTED"


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SourceRef:
    workspace_id: UUID
    source_id: UUID
    source_version_id: UUID
    content_sha256: str
    object_key: str
    file_name: str
    format: str


@dataclass(frozen=True, slots=True)
class DocumentState:
    workspace_id: UUID
    document_id: UUID
    source_id: UUID
    source_version_id: UUID
    desired_generation: int
    active_version: int | None


@dataclass(frozen=True, slots=True)
class VersionState:
    workspace_id: UUID
    document_id: UUID
    index_version: int
    status: str
    current_attempt_id: UUID | None
    winning_attempt_id: UUID | None
    manifest_checksum: str | None
    chunk_count: int
    row_version: int


@dataclass(frozen=True, slots=True)
class Attempt:
    workspace_id: UUID
    attempt_id: UUID
    document_id: UUID
    index_version: int
    generation: int
    status: str


@dataclass(frozen=True, slots=True)
class ChunkInput:
    """一个待写入 chunk：文本 + T11 的 offset-free locator。"""

    kind: str
    text: str
    locator: Locator

    def __post_init__(self) -> None:
        if not isinstance(self.locator, Locator):
            raise BatchRejected("ChunkInput.locator 必须是 locator_map.Locator")
        if not isinstance(self.text, str) or not self.text:
            raise BatchRejected("ChunkInput.text 必须是非空字符串")
        if not isinstance(self.kind, str) or not 1 <= len(self.kind) <= 32:
            raise BatchRejected("ChunkInput.kind 长度必须在 [1, 32]")


@dataclass(frozen=True, slots=True)
class BatchResult:
    inserted: int
    skipped: int
    chunk_count: int
    attempt_id: UUID


@dataclass(frozen=True, slots=True)
class ManifestRecord:
    workspace_id: UUID
    document_id: UUID
    index_version: int
    attempt_id: UUID
    status: str
    checksum: str | None
    missing_items: tuple[str, ...]
    manifest: Mapping[str, Any]


# ---------------------------------------------------------------------------
# 连接 / 迁移
# ---------------------------------------------------------------------------
def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection[Any]:
    """连实验库；DSN 复用 T03 的 ``scope.read_dsn``（只读 import，不改 scope.py）。"""

    if dsn is None:
        from scope import read_dsn

        dsn = read_dsn()
    return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)


def migration_sha256(path: Path = MIGRATION_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migration(
    conn: psycopg.Connection[Any],
    *,
    path: Path = MIGRATION_PATH,
    migration_id: str = MIGRATION_ID,
) -> str:
    """在单事务内应用迁移 004 并写台账；调用方 commit。

    迁移文件不含 BEGIN/COMMIT；``CREATE SCHEMA`` 刻意不带 ``IF NOT EXISTS``，
    所以**重复 apply 会显式失败**（psycopg.errors.DuplicateSchema），不会被静默跳过。
    """

    if conn.autocommit:
        raise IndexStagingError("apply_migration 需要 autocommit=False 的连接")
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        raise IndexStagingError("apply_migration 需要干净事务边界（IDLE）")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute(raw.decode("utf-8"))
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_schema_migrations (migration_id, filename, sha256) "
        "VALUES (%s, %s, %s)",
        (migration_id, path.name, digest),
    )
    return digest


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    """删除本任务 schema（清理用）；只碰 p0_t12。"""

    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))
    conn.commit()


# ---------------------------------------------------------------------------
# 对象存储：真实 MinIO/S3 不可用 → 可注入假存储，明确标注
# ---------------------------------------------------------------------------
class ObjectStore(Protocol):
    backend: str

    def put(self, key: str, data: bytes) -> str: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


class InMemoryObjectStore:
    """**假存储**：只在进程内保存 bytes。

    本机没有 MinIO/S3（compose 只有 postgres+redis），真实对象存储的读写/签名/
    生命周期一律 **NOT_RUN**；这里的作用只是给 manifest 一个可验证的 object ref
    （存在性 + sha256），证明"对象缺失不能 ready"这条规则本身成立。
    """

    backend: Final = "in-memory-fake (真实 MinIO/S3 NOT_RUN)"

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        if not key or len(key) > 512:
            raise IndexStagingError(f"object key 不合法：{key!r}")
        self._objects[key] = bytes(data)
        return content_checksum(data)

    def get(self, key: str) -> bytes:
        if key not in self._objects:
            raise ObjectMissing(f"对象不存在：{key}")
        return self._objects[key]

    def exists(self, key: str) -> bool:
        return key in self._objects

    def delete(self, key: str) -> None:
        """测试用：模拟对象丢失/被清理。"""

        self._objects.pop(key, None)


# ---------------------------------------------------------------------------
# 哈希 / 确定性 embedding
# ---------------------------------------------------------------------------
def content_checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_checksum(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def deterministic_embedding(text: str, *, dim: int = EMBEDDING_DIM) -> tuple[float, ...]:
    """固定维度的确定性假 embedding：同输入同输出、跨进程可复现。

    不是真实模型：向量没有语义，只用来验证"同输入同输出 / 维度不符拒绝 /
    维度与 embedding_dim 一致"这些一致性规则。真实模型 **NOT_RUN**。
    """

    if not isinstance(text, str):
        raise IndexStagingError("text 必须是 str")
    if dim < 1 or dim > 4096:
        raise IndexStagingError(f"dim 必须在 [1, 4096]，收到 {dim}")
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        digest = hashlib.sha256(
            f"{EMBEDDING_PROFILE}|{counter}|{text}".encode()
        ).digest()
        for offset in range(0, 32, 4):
            raw = int.from_bytes(digest[offset : offset + 4], "big")
            values.append(round(raw / 0xFFFFFFFF * 2 - 1, 12))
        counter += 1
    return tuple(values[:dim])


def chunk_checksum(*, text_sha256: str, locator: Mapping[str, Any], source_id: UUID,
                   source_version_id: UUID) -> str:
    return canonical_checksum(
        {
            "text_sha256": text_sha256,
            "locator": dict(locator),
            "source_id": str(source_id),
            "source_version_id": str(source_version_id),
        }
    )


# ---------------------------------------------------------------------------
# 来源 / 文档 / 期望代数
# ---------------------------------------------------------------------------
def create_source(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    source_id: UUID,
    source_version_id: UUID,
    content: bytes,
    file_name: str,
    format: str,
    store: ObjectStore,
    object_key: str | None = None,
) -> SourceRef:
    """登记来源身份 + 把对象字节放进（假）存储；object_key 记在 sources 行。"""

    if not isinstance(content, bytes) or not content:
        raise IndexStagingError("content 必须是非空 bytes")
    digest = content_checksum(content)
    key = object_key or f"p0_t12/{workspace_id}/{source_id}/{source_version_id}"
    store_digest = store.put(key, content)
    if store_digest != digest:  # pragma: no cover - 假存储不会算错
        raise IndexStagingError("对象存储 checksum 与本地校验不一致")
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_sources "
        "(workspace_id, source_id, source_version_id, content_sha256, object_key, "
        " file_name, format) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, source_id, source_version_id, digest, key, file_name, format),
    )
    return SourceRef(
        workspace_id=workspace_id,
        source_id=source_id,
        source_version_id=source_version_id,
        content_sha256=digest,
        object_key=key,
        file_name=file_name,
        format=format,
    )


def ensure_document(
    conn: psycopg.Connection[Any],
    *,
    document_id: UUID,
    source: SourceRef,
    desired_generation: int = 1,
) -> DocumentState:
    """建立文档与 index_version=desired_generation 的版本行（幂等）。"""

    if desired_generation < 1:
        raise IndexStagingError("desired_generation 必须 >= 1")
    inserted = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_documents "
        "(workspace_id, document_id, source_id, source_version_id, desired_generation) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (workspace_id, document_id) DO NOTHING "
        "RETURNING document_id",
        (source.workspace_id, document_id, source.source_id, source.source_version_id,
         desired_generation),
    ).fetchone()
    if inserted is not None:
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_document_versions "
            "(workspace_id, document_id, index_version, status) "
            "VALUES (%s, %s, %s, 'staging') ON CONFLICT DO NOTHING",
            (source.workspace_id, document_id, desired_generation),
        )
    return _document_state(conn, source.workspace_id, document_id)


def bump_desired_generation(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    document_id: UUID,
    now: datetime | None = None,
) -> DocumentState:
    """推进"想要哪一版"：desired_generation + 1，并建立新版本行（旧 attempt 立即变 stale）。"""

    moment = now or datetime.now(UTC)
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_documents "
        "SET desired_generation = desired_generation + 1, updated_at = %s, "
        "    row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s "
        "RETURNING desired_generation",
        (moment, workspace_id, document_id),
    ).fetchone()
    if row is None:
        raise IndexStagingError(f"document {document_id} 不存在", code="DOCUMENT_NOT_FOUND")
    generation = int(row["desired_generation"])
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_document_versions "
        "(workspace_id, document_id, index_version, status) VALUES (%s, %s, %s, 'staging') "
        "ON CONFLICT DO NOTHING",
        (workspace_id, document_id, generation),
    )
    return _document_state(conn, workspace_id, document_id)


# ---------------------------------------------------------------------------
# attempt 生命周期（fencing）
# ---------------------------------------------------------------------------
def begin_attempt(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    document_id: UUID,
    generation: int,
    attempt_id: UUID | None = None,
    now: datetime | None = None,
) -> Attempt:
    """为一个版本开启新 attempt；切换 attempt 会清空旧 staging 分区（C-09 同版本重试）。

    前置：``documents.desired_generation == generation`` 且版本可 staging
    （``staging``/``failed``；``ready`` 及以上必须走新 generation）。
    旧 attempt 置 ``aborted`` 并删除它写的 chunk/manifest，``chunk_count`` 归零——
    这样"同文档同版本 chunk 序号唯一"不会因为换 attempt 而残留旧行。
    """

    moment = now or datetime.now(UTC)
    attempt = Attempt(
        workspace_id=workspace_id,
        attempt_id=attempt_id or uuid4(),
        document_id=document_id,
        index_version=generation,
        generation=generation,
        status="running",
    )
    document = conn.execute(
        f"SELECT desired_generation FROM {SCHEMA}.phase0_documents "
        "WHERE workspace_id = %s AND document_id = %s FOR UPDATE",
        (workspace_id, document_id),
    ).fetchone()
    if document is None:
        raise IndexStagingError(f"document {document_id} 不存在", code="DOCUMENT_NOT_FOUND")
    version = conn.execute(
        f"SELECT status, current_attempt_id, chunk_count, row_version "
        f"FROM {SCHEMA}.phase0_document_versions "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "FOR UPDATE",
        (workspace_id, document_id, generation),
    ).fetchone()
    if version is None:
        raise IndexStagingError(
            f"版本 {generation} 不存在", code="VERSION_NOT_FOUND"
        )
    if int(document["desired_generation"]) != generation:
        _record_event(
            conn, workspace_id=workspace_id, document_id=document_id,
            index_version=generation, attempt_id=attempt.attempt_id,
            outcome="stale_generation", error_code="STALE_GENERATION",
            detail=f"desired={document['desired_generation']} attempt_generation={generation}",
        )
        raise StaleGenerationError(
            f"desired_generation={document['desired_generation']} 已取代 {generation}"
        )
    if version["status"] not in ("staging", "failed"):
        raise VersionNotStageable(
            f"版本 {generation} 状态为 {version['status']}，不能再开 attempt"
            "（ready 及以上必须走新 generation）"
        )

    previous = version["current_attempt_id"]
    if previous is not None and previous != attempt.attempt_id:
        conn.execute(
            f"UPDATE {SCHEMA}.phase0_index_attempts "
            "SET status = 'aborted', finished_at = %s, row_version = row_version + 1 "
            "WHERE workspace_id = %s AND attempt_id = %s AND status = 'running'",
            (moment, workspace_id, previous),
        )
        conn.execute(
            f"DELETE FROM {SCHEMA}.phase0_index_chunks "
            "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
            (workspace_id, document_id, generation),
        )
        conn.execute(
            f"DELETE FROM {SCHEMA}.phase0_index_manifests "
            "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
            (workspace_id, document_id, generation),
        )
        _record_event(
            conn, workspace_id=workspace_id, document_id=document_id,
            index_version=generation, attempt_id=previous,
            outcome="attempt_aborted", error_code=None,
            detail=f"superseded_by={attempt.attempt_id}",
        )

    conn.execute(
        f"UPDATE {SCHEMA}.phase0_document_versions "
        "SET current_attempt_id = %s, status = 'staging', chunk_count = 0, "
        "    winning_attempt_id = NULL, manifest_checksum = NULL, updated_at = %s, "
        "    row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
        (attempt.attempt_id, moment, workspace_id, document_id, generation),
    )
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_index_attempts "
        "(workspace_id, attempt_id, document_id, index_version, generation, status, "
        " started_at) VALUES (%s, %s, %s, %s, %s, 'running', %s)",
        (workspace_id, attempt.attempt_id, document_id, generation, generation, moment),
    )
    _record_event(
        conn, workspace_id=workspace_id, document_id=document_id,
        index_version=generation, attempt_id=attempt.attempt_id,
        outcome="attempt_started", error_code=None,
        detail=f"generation={generation}",
    )
    return attempt


def fail_attempt(
    conn: psycopg.Connection[Any],
    attempt: Attempt,
    *,
    error_code: str,
    detail: str | None = None,
    now: datetime | None = None,
) -> None:
    """把 attempt 标 failed、版本标 failed；数据保留供诊断（不进入 ready）。"""

    moment = now or datetime.now(UTC)
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_index_attempts "
        "SET status = 'failed', finished_at = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND attempt_id = %s AND status = 'running'",
        (moment, attempt.workspace_id, attempt.attempt_id),
    )
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_document_versions "
        "SET status = 'failed', updated_at = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "  AND status = 'staging' AND current_attempt_id = %s",
        (moment, attempt.workspace_id, attempt.document_id, attempt.index_version,
         attempt.attempt_id),
    )
    _record_event(
        conn, workspace_id=attempt.workspace_id, document_id=attempt.document_id,
        index_version=attempt.index_version, attempt_id=attempt.attempt_id,
        outcome="batch_rejected", error_code=error_code, detail=detail,
    )


# ---------------------------------------------------------------------------
# chunk staging（fencing + 序号唯一 + embedding 一致性）
# ---------------------------------------------------------------------------
def stage_chunk_batch(
    conn: psycopg.Connection[Any],
    attempt: Attempt,
    chunks: Sequence[ChunkInput],
    *,
    embeddings: Sequence[Sequence[float]] | None = None,
    embedding_profile: str = EMBEDDING_PROFILE,
    embedding_dim: int = EMBEDDING_DIM,
) -> BatchResult:
    """把一批 chunk 写进不可见分区。

    - **fencing**：先锁版本行并核对 ``current_attempt_id``/``desired_generation``，
      不匹配直接 ``STALE_ATTEMPT``/``STALE_GENERATION``，一行都不写；
    - **序号唯一**：``ON CONFLICT (workspace, document, version, chunk_ordinal)
      DO NOTHING``，重复批次 ``inserted=0``、行数不增长；
    - **embedding**：可用注入向量（测维度/数量拒绝），默认用确定性假 embedding。
    """

    if not chunks:
        raise BatchRejected("空批次不允许（会把 chunk_count 语义搞乱）")
    ordinals = [index for index, _ in enumerate(chunks)]
    seen: set[int] = set()
    for ordinal in ordinals:
        if ordinal in seen:  # pragma: no cover - 由 enumerate 保证
            raise BatchRejected(f"批次内 chunk_ordinal 重复：{ordinal}")
        seen.add(ordinal)

    vectors = _resolve_embeddings(
        chunks, embeddings, embedding_dim=embedding_dim
    )
    if embedding_profile != EMBEDDING_PROFILE and not 1 <= len(embedding_profile) <= 64:
        # 允许自定义 profile 名，但必须与向量维度一起写进 chunk/manifest。
        raise IndexStagingError("embedding_profile 长度必须在 [1, 64]")

    version = conn.execute(
        f"SELECT v.status, v.current_attempt_id, v.chunk_count, v.row_version, "
        f"       d.desired_generation, d.source_id, d.source_version_id "
        f"FROM {SCHEMA}.phase0_document_versions AS v "
        f"JOIN {SCHEMA}.phase0_documents AS d "
        "  ON d.workspace_id = v.workspace_id AND d.document_id = v.document_id "
        "WHERE v.workspace_id = %s AND v.document_id = %s AND v.index_version = %s "
        "FOR UPDATE OF v",
        (attempt.workspace_id, attempt.document_id, attempt.index_version),
    ).fetchone()
    if version is None:
        raise IndexStagingError("版本不存在", code="VERSION_NOT_FOUND")
    if version["status"] != "staging" or version["current_attempt_id"] != attempt.attempt_id:
        _record_event(
            conn, workspace_id=attempt.workspace_id, document_id=attempt.document_id,
            index_version=attempt.index_version, attempt_id=attempt.attempt_id,
            outcome="stale_attempt", error_code="STALE_ATTEMPT",
            detail=f"current={version['current_attempt_id']} status={version['status']}",
        )
        raise StaleAttemptError(
            f"attempt {attempt.attempt_id} 不再是版本当前 attempt"
            f"（current={version['current_attempt_id']}, status={version['status']}）"
        )
    if int(version["desired_generation"]) != attempt.generation:
        _record_event(
            conn, workspace_id=attempt.workspace_id, document_id=attempt.document_id,
            index_version=attempt.index_version, attempt_id=attempt.attempt_id,
            outcome="stale_generation", error_code="STALE_GENERATION",
            detail=f"desired={version['desired_generation']} generation={attempt.generation}",
        )
        raise StaleGenerationError(
            f"desired_generation={version['desired_generation']} 已取代 {attempt.generation}"
        )

    source_id = version["source_id"]
    source_version_id = version["source_version_id"]
    inserted = 0
    for index, chunk in enumerate(chunks):
        locator = chunk.locator.to_dict()
        text_sha = content_checksum(chunk.text.encode("utf-8"))
        checksum = chunk_checksum(
            text_sha256=text_sha, locator=locator,
            source_id=source_id, source_version_id=source_version_id,
        )
        row = conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_index_chunks "
            "(workspace_id, chunk_id, document_id, index_version, attempt_id, generation, "
            " chunk_ordinal, kind, text, text_sha256, locator, checksum, source_id, "
            " source_version_id, embedding, embedding_dim, embedding_profile) "
            "SELECT d.workspace_id, %(chunk_id)s, d.document_id, v.index_version, "
            "       %(attempt_id)s, d.desired_generation, %(ordinal)s, %(kind)s, "
            "       %(text)s, %(text_sha256)s, %(locator)s::jsonb, %(checksum)s, "
            "       d.source_id, d.source_version_id, %(embedding)s::double precision[], "
            "       %(embedding_dim)s, %(profile)s "
            f"FROM {SCHEMA}.phase0_document_versions AS v "
            f"JOIN {SCHEMA}.phase0_documents AS d "
            "  ON d.workspace_id = v.workspace_id AND d.document_id = v.document_id "
            "WHERE v.workspace_id = %(workspace_id)s AND v.document_id = %(document_id)s "
            "  AND v.index_version = %(index_version)s "
            "  AND v.status = 'staging' AND v.current_attempt_id = %(attempt_id)s "
            "  AND d.desired_generation = %(generation)s "
            "ON CONFLICT (workspace_id, document_id, index_version, chunk_ordinal) "
            "DO NOTHING RETURNING chunk_id",
            {
                "chunk_id": uuid4(),
                "attempt_id": attempt.attempt_id,
                "ordinal": index,
                "kind": chunk.kind,
                "text": chunk.text,
                "text_sha256": text_sha,
                "locator": json.dumps(locator, ensure_ascii=False, sort_keys=True),
                "checksum": checksum,
                "embedding": list(vectors[index]),
                "embedding_dim": embedding_dim,
                "profile": embedding_profile,
                "workspace_id": attempt.workspace_id,
                "document_id": attempt.document_id,
                "index_version": attempt.index_version,
                "generation": attempt.generation,
            },
        ).fetchone()
        if row is not None:
            inserted += 1

    skipped = len(chunks) - inserted
    new_count = int(version["chunk_count"]) + inserted
    updated = conn.execute(
        f"UPDATE {SCHEMA}.phase0_document_versions "
        "SET chunk_count = %s, updated_at = now(), row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "  AND status = 'staging' AND current_attempt_id = %s "
        "RETURNING chunk_count",
        (new_count, attempt.workspace_id, attempt.document_id, attempt.index_version,
         attempt.attempt_id),
    ).fetchone()
    if updated is None:  # pragma: no cover - 行锁内不应发生
        raise StaleAttemptError("写 chunk 期间 attempt 被取代（回滚）")
    _record_event(
        conn, workspace_id=attempt.workspace_id, document_id=attempt.document_id,
        index_version=attempt.index_version, attempt_id=attempt.attempt_id,
        outcome="chunks_staged", error_code=None,
        detail=f"inserted={inserted} skipped={skipped}",
    )
    return BatchResult(
        inserted=inserted,
        skipped=skipped,
        chunk_count=int(updated["chunk_count"]),
        attempt_id=attempt.attempt_id,
    )


def _resolve_embeddings(
    chunks: Sequence[ChunkInput],
    embeddings: Sequence[Sequence[float]] | None,
    *,
    embedding_dim: int,
) -> list[tuple[float, ...]]:
    if embedding_dim < 1 or embedding_dim > 4096:
        raise EmbeddingDimensionMismatch(f"embedding_dim 必须在 [1, 4096]，收到 {embedding_dim}")
    if embeddings is None:
        return [deterministic_embedding(chunk.text, dim=embedding_dim) for chunk in chunks]
    if len(embeddings) != len(chunks):
        raise EmbeddingCountMismatch(
            f"embedding 数 {len(embeddings)} 与 chunk 数 {len(chunks)} 不一致"
        )
    prepared: list[tuple[float, ...]] = []
    for index, vector in enumerate(embeddings):
        values = tuple(float(value) for value in vector)
        if len(values) != embedding_dim:
            raise EmbeddingDimensionMismatch(
                f"第 {index} 个向量维度 {len(values)} != {embedding_dim}"
            )
        prepared.append(values)
    return prepared


# ---------------------------------------------------------------------------
# manifest 与 ready
# ---------------------------------------------------------------------------
def build_manifest(
    conn: psycopg.Connection[Any],
    attempt: Attempt,
    *,
    store: ObjectStore,
    embedding_profile: str = EMBEDDING_PROFILE,
    embedding_dim: int = EMBEDDING_DIM,
    now: datetime | None = None,
) -> dict[str, Any]:
    """汇总事实生成 manifest（缺项不在这里删，缺失由 write_manifest 判定）。"""

    moment = now or datetime.now(UTC)
    source = conn.execute(
        f"SELECT s.source_id, s.source_version_id, s.content_sha256, s.object_key, "
        f"       s.file_name, s.format "
        f"FROM {SCHEMA}.phase0_sources AS s "
        f"JOIN {SCHEMA}.phase0_documents AS d "
        "  ON d.workspace_id = s.workspace_id "
        " AND d.source_id = s.source_id AND d.source_version_id = s.source_version_id "
        "WHERE d.workspace_id = %s AND d.document_id = %s",
        (attempt.workspace_id, attempt.document_id),
    ).fetchone()
    if source is None:
        raise IndexStagingError("document 没有来源身份", code="SOURCE_NOT_FOUND")
    chunks = conn.execute(
        f"SELECT chunk_ordinal, checksum, embedding_dim, embedding_profile "
        f"FROM {SCHEMA}.phase0_index_chunks "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "ORDER BY chunk_ordinal",
        (attempt.workspace_id, attempt.document_id, attempt.index_version),
    ).fetchall()
    chunk_digest = canonical_checksum(
        {"chunks": [f"{row['chunk_ordinal']}:{row['checksum']}" for row in chunks]}
    )
    if not store.exists(source["object_key"]):
        raise ObjectMissing(
            f"对象 {source['object_key']} 不在存储后端"
            f"（backend={store.backend}，真实对象存储 NOT_RUN）"
        )
    stored_digest = content_checksum(store.get(source["object_key"]))
    if stored_digest != source["content_sha256"]:
        raise IndexStagingError(
            f"对象内容 sha256 与登记值不一致：{stored_digest} != {source['content_sha256']}",
            code="OBJECT_CORRUPT",
        )
    manifest: dict[str, Any] = {
        "schema": 1,
        "workspace_id": str(attempt.workspace_id),
        "document_id": str(attempt.document_id),
        "index_version": attempt.index_version,
        "attempt_id": str(attempt.attempt_id),
        "chunk_count": len(chunks),
        "chunk_digest": chunk_digest,
        "embedding_profile": embedding_profile,
        "embedding_dim": embedding_dim,
        "source_id": str(source["source_id"]),
        "source_version_id": str(source["source_version_id"]),
        "source_sha256": source["content_sha256"],
        "object_key": source["object_key"],
        "object_sha256": source["content_sha256"],
        "object_bytes": len(store.get(source["object_key"])),
        "object_store_backend": store.backend,
        "locator_schema": 1,
        "format": source["format"],
        "file_name": source["file_name"],
        "produced_at": moment.isoformat(timespec="microseconds"),
        "chunk_embeddings": {
            row["chunk_ordinal"]: {"dim": int(row["embedding_dim"]),
                                   "profile": row["embedding_profile"]}
            for row in chunks
        },
    }
    return manifest


def write_manifest(
    conn: psycopg.Connection[Any],
    attempt: Attempt,
    manifest: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> ManifestRecord:
    """写 manifest：缺任一必需项 → ``draft`` + ``missing_items``；齐全 → ``ready``。

    不抛异常：缺失是**可审计的事实**（行留在库里），"不能 ready" 由
    ``mark_version_ready`` 与数据库触发器两道关一起保证。
    """

    _lock_version(conn, attempt)
    missing = tuple(key for key in REQUIRED_MANIFEST_KEYS if key not in manifest)
    status = "ready" if not missing else "draft"
    checksum = canonical_checksum(dict(manifest)) if status == "ready" else None
    row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_index_manifests "
        "(workspace_id, document_id, index_version, attempt_id, status, manifest, "
        " checksum, missing_items) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (workspace_id, document_id, index_version) DO UPDATE SET "
        "  attempt_id = EXCLUDED.attempt_id, status = EXCLUDED.status, "
        "  manifest = EXCLUDED.manifest, checksum = EXCLUDED.checksum, "
        "  missing_items = EXCLUDED.missing_items, updated_at = now(), "
        "  row_version = phase0_index_manifests.row_version + 1 "
        "RETURNING status, checksum, missing_items",
        (attempt.workspace_id, attempt.document_id, attempt.index_version,
         attempt.attempt_id, status,
         json.dumps(dict(manifest), ensure_ascii=False, sort_keys=True),
         checksum, list(missing)),
    ).fetchone()
    _record_event(
        conn, workspace_id=attempt.workspace_id, document_id=attempt.document_id,
        index_version=attempt.index_version, attempt_id=attempt.attempt_id,
        outcome="manifest_ready" if status == "ready" else "manifest_draft",
        error_code=None if status == "ready" else "MANIFEST_INCOMPLETE",
        detail=None if status == "ready" else f"missing={list(missing)}",
    )
    return ManifestRecord(
        workspace_id=attempt.workspace_id,
        document_id=attempt.document_id,
        index_version=attempt.index_version,
        attempt_id=attempt.attempt_id,
        status=str(row["status"]),
        checksum=row["checksum"],
        missing_items=tuple(row["missing_items"] or ()),
        manifest=dict(manifest),
    )


def mark_version_ready(
    conn: psycopg.Connection[Any],
    attempt: Attempt,
    *,
    now: datetime | None = None,
) -> VersionState:
    """``staging → ready``：要求同 attempt 的 ready manifest、chunk_count 一致。

    旧 attempt / 旧 generation 在锁内先被判 stale（明确错误码），不会出现"部分成功"。
    """

    moment = now or datetime.now(UTC)
    version = _lock_version(conn, attempt)
    if version["status"] != "staging" or version["current_attempt_id"] != attempt.attempt_id:
        _record_event(
            conn, workspace_id=attempt.workspace_id, document_id=attempt.document_id,
            index_version=attempt.index_version, attempt_id=attempt.attempt_id,
            outcome="stale_attempt", error_code="STALE_ATTEMPT",
            detail=f"status={version['status']} current={version['current_attempt_id']}",
        )
        raise StaleAttemptError(f"attempt {attempt.attempt_id} 不是当前 staging attempt")
    document = conn.execute(
        f"SELECT desired_generation FROM {SCHEMA}.phase0_documents "
        "WHERE workspace_id = %s AND document_id = %s",
        (attempt.workspace_id, attempt.document_id),
    ).fetchone()
    if int(document["desired_generation"]) != attempt.generation:
        _record_event(
            conn, workspace_id=attempt.workspace_id, document_id=attempt.document_id,
            index_version=attempt.index_version, attempt_id=attempt.attempt_id,
            outcome="stale_generation", error_code="STALE_GENERATION",
            detail=f"desired={document['desired_generation']} generation={attempt.generation}",
        )
        raise StaleGenerationError(
            f"desired_generation={document['desired_generation']} 已取代 {attempt.generation}"
        )

    manifest = conn.execute(
        f"SELECT status, checksum, missing_items FROM {SCHEMA}.phase0_index_manifests "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "  AND attempt_id = %s",
        (attempt.workspace_id, attempt.document_id, attempt.index_version,
         attempt.attempt_id),
    ).fetchone()
    if manifest is None:
        raise ManifestIncomplete("没有 manifest，版本不能 ready")
    if manifest["status"] != "ready":
        missing = list(manifest["missing_items"] or ())
        _record_event(
            conn, workspace_id=attempt.workspace_id, document_id=attempt.document_id,
            index_version=attempt.index_version, attempt_id=attempt.attempt_id,
            outcome="batch_rejected", error_code="MANIFEST_INCOMPLETE",
            detail=f"missing={missing}",
        )
        raise ManifestIncomplete(f"manifest 未 ready，缺必需项：{missing}")
    actual_chunks = conn.execute(
        f"SELECT count(*) AS n FROM {SCHEMA}.phase0_index_chunks "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
        (attempt.workspace_id, attempt.document_id, attempt.index_version),
    ).fetchone()
    if int(actual_chunks["n"]) != int(version["chunk_count"]):
        raise ManifestIncomplete(
            f"chunk_count={version['chunk_count']} 与实际 {actual_chunks['n']} 不一致"
        )

    updated = conn.execute(
        f"UPDATE {SCHEMA}.phase0_document_versions "
        "SET status = 'ready', winning_attempt_id = %s, manifest_checksum = %s, "
        "    updated_at = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "  AND status = 'staging' AND current_attempt_id = %s "
        "RETURNING row_version",
        (attempt.attempt_id, manifest["checksum"], moment, attempt.workspace_id,
         attempt.document_id, attempt.index_version, attempt.attempt_id),
    ).fetchone()
    if updated is None:  # pragma: no cover - 行锁内不应发生
        raise StaleAttemptError("ready 期间 attempt 被取代")
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_index_attempts "
        "SET status = 'succeeded', finished_at = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND attempt_id = %s",
        (moment, attempt.workspace_id, attempt.attempt_id),
    )
    _record_event(
        conn, workspace_id=attempt.workspace_id, document_id=attempt.document_id,
        index_version=attempt.index_version, attempt_id=attempt.attempt_id,
        outcome="version_ready", error_code=None,
        detail=f"manifest={manifest['checksum']}",
    )
    return get_version(conn, attempt.workspace_id, attempt.document_id,
                       attempt.index_version)


# ---------------------------------------------------------------------------
# 可见性探针（只读；active 指针归 T13）
# ---------------------------------------------------------------------------
def search_visible_chunks(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """可见性探针：只有 ``documents.active_version`` 指针指向且版本 ``active`` 才可见。

    T12 不写 active 指针，所以 staging/ready 完成后这里必须为 0；T13 激活后同一查询
    才应返回行。这是**可见性**断言，不是检索质量声明（用 ILIKE，不做排序质量）。
    """

    if not isinstance(query, str) or not query:
        raise IndexStagingError("query 必须是非空字符串")
    if limit < 1:
        raise IndexStagingError("limit 必须 >= 1")
    rows = conn.execute(
        f"SELECT c.chunk_id, c.document_id, c.index_version, c.chunk_ordinal, "
        f"       c.text, c.locator, c.checksum "
        f"FROM {SCHEMA}.phase0_index_chunks AS c "
        f"JOIN {SCHEMA}.phase0_documents AS d "
        "  ON d.workspace_id = c.workspace_id AND d.document_id = c.document_id "
        " AND d.active_version = c.index_version "
        f"JOIN {SCHEMA}.phase0_document_versions AS v "
        "  ON v.workspace_id = c.workspace_id AND v.document_id = c.document_id "
        " AND v.index_version = c.index_version AND v.status = 'active' "
        "WHERE c.workspace_id = %s AND c.text ILIKE %s "
        "ORDER BY c.document_id, c.chunk_ordinal LIMIT %s",
        (workspace_id, f"%{query}%", limit),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 读取助手（测试与 T13 复用；只读）
# ---------------------------------------------------------------------------
def get_document(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID
) -> DocumentState:
    return _document_state(conn, workspace_id, document_id)


def get_version(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID,
    index_version: int,
) -> VersionState:
    row = conn.execute(
        f"SELECT workspace_id, document_id, index_version, status, current_attempt_id, "
        f"       winning_attempt_id, manifest_checksum, chunk_count, row_version "
        f"FROM {SCHEMA}.phase0_document_versions "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
        (workspace_id, document_id, index_version),
    ).fetchone()
    if row is None:
        raise IndexStagingError("版本不存在", code="VERSION_NOT_FOUND")
    return VersionState(
        workspace_id=row["workspace_id"], document_id=row["document_id"],
        index_version=int(row["index_version"]), status=str(row["status"]),
        current_attempt_id=row["current_attempt_id"],
        winning_attempt_id=row["winning_attempt_id"],
        manifest_checksum=row["manifest_checksum"],
        chunk_count=int(row["chunk_count"]), row_version=int(row["row_version"]),
    )


def get_attempt(
    conn: psycopg.Connection[Any], workspace_id: UUID, attempt_id: UUID
) -> Attempt:
    row = conn.execute(
        f"SELECT workspace_id, attempt_id, document_id, index_version, generation, status "
        f"FROM {SCHEMA}.phase0_index_attempts "
        "WHERE workspace_id = %s AND attempt_id = %s",
        (workspace_id, attempt_id),
    ).fetchone()
    if row is None:
        raise IndexStagingError("attempt 不存在", code="ATTEMPT_NOT_FOUND")
    return Attempt(
        workspace_id=row["workspace_id"], attempt_id=row["attempt_id"],
        document_id=row["document_id"], index_version=int(row["index_version"]),
        generation=int(row["generation"]), status=str(row["status"]),
    )


def get_manifest(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID,
    index_version: int,
) -> ManifestRecord | None:
    row = conn.execute(
        f"SELECT workspace_id, document_id, index_version, attempt_id, status, checksum, "
        f"       missing_items, manifest "
        f"FROM {SCHEMA}.phase0_index_manifests "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
        (workspace_id, document_id, index_version),
    ).fetchone()
    if row is None:
        return None
    return ManifestRecord(
        workspace_id=row["workspace_id"], document_id=row["document_id"],
        index_version=int(row["index_version"]), attempt_id=row["attempt_id"],
        status=str(row["status"]), checksum=row["checksum"],
        missing_items=tuple(row["missing_items"] or ()), manifest=row["manifest"],
    )


def list_chunks(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID,
    index_version: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT chunk_id, chunk_ordinal, kind, text, text_sha256, locator, checksum, "
        f"       source_id, source_version_id, attempt_id, generation, embedding, "
        f"       embedding_dim, embedding_profile "
        f"FROM {SCHEMA}.phase0_index_chunks "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "ORDER BY chunk_ordinal",
        (workspace_id, document_id, index_version),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# T11 → chunk 输入
# ---------------------------------------------------------------------------
def from_parsed_document(result: ParseResult) -> list[ChunkInput]:
    """把 T11 ``ParseResult`` 的块直接变成 chunk 输入（一文本块一 chunk）。

    locator 原样沿用（offset-free）；空文本块与无用 kind 丢弃，其余按原顺序编号。
    """

    if not isinstance(result, ParseResult):
        raise BatchRejected("需要 simple_reader.ParseResult")
    if not result.ok:
        raise BatchRejected(f"解析失败，不能 staging：{result.error}")
    chunks: list[ChunkInput] = []
    for block in result.blocks:
        text = block.text.strip()
        if not text:
            continue
        chunks.append(ChunkInput(kind=block.kind, text=text, locator=block.locator))
    if not chunks:
        raise BatchRejected("解析结果没有可 staging 的文本块")
    return chunks


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _document_state(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID
) -> DocumentState:
    row = conn.execute(
        f"SELECT workspace_id, document_id, source_id, source_version_id, "
        f"       desired_generation, active_version "
        f"FROM {SCHEMA}.phase0_documents WHERE workspace_id = %s AND document_id = %s",
        (workspace_id, document_id),
    ).fetchone()
    if row is None:
        raise IndexStagingError(f"document {document_id} 不存在", code="DOCUMENT_NOT_FOUND")
    return DocumentState(
        workspace_id=row["workspace_id"], document_id=row["document_id"],
        source_id=row["source_id"], source_version_id=row["source_version_id"],
        desired_generation=int(row["desired_generation"]),
        active_version=None if row["active_version"] is None else int(row["active_version"]),
    )


def _lock_version(conn: psycopg.Connection[Any], attempt: Attempt) -> Mapping[str, Any]:
    row = conn.execute(
        f"SELECT status, current_attempt_id, chunk_count, row_version "
        f"FROM {SCHEMA}.phase0_document_versions "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "FOR UPDATE",
        (attempt.workspace_id, attempt.document_id, attempt.index_version),
    ).fetchone()
    if row is None:
        raise IndexStagingError("版本不存在", code="VERSION_NOT_FOUND")
    return row


def _record_event(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    document_id: UUID,
    index_version: int | None,
    attempt_id: UUID | None,
    outcome: str,
    error_code: str | None,
    detail: str | None,
) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_index_events "
        "(workspace_id, event_id, document_id, index_version, attempt_id, outcome, "
        " error_code, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, uuid4(), document_id, index_version,
         None if attempt_id is None else attempt_id, outcome, error_code,
         None if detail is None else detail[:_MAX_DETAIL]),
    )


def iter_required_manifest_keys() -> Iterable[str]:
    return iter(REQUIRED_MANIFEST_KEYS)
