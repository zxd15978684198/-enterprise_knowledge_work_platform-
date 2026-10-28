"""P0-T13 原子发布：一次事务切活动指针，读者只见已发布授权版本。

为什么需要它
------------
P0-T12 的 staging 只保证「写全了、ready 了」，**不保证可见**：``phase0_documents``
的 ``active_version`` 指针归本模块独占写。发布必须是一个事务里的三件事：

1. 旧 active 版本 ``active → retired``；
2. 新版本 ``ready → active``（004 的触发器只允许这条边进入 active）；
3. ``documents.active_version`` 指针切换（``row_version`` 严格 +1）。

任何一步失败都整笔回滚，读者要么看到旧版本的全部 chunk、要么看到新版本的全部
chunk，不存在「指针已切但版本未 active」或「版本已 active 但指针未切」的中间态。

契约与依据
----------
* P0-T13 卡（``docs/poc/03`` L218–233）：锁 document → 验 desired/generation/
  attempt/manifest → 同 UoW 转换 active/Job/event → 提交；reader 单 snapshot join。
* C-01 §6：staging 不可见，一次提交；提交前任何失败全回滚；**不在锁内做远端 I/O**
  （本模块只碰 PG，不做对象存储写与消息投递）。
* C-01 与 W6 记录 §1.3：Job 的唯一写入者归发布路径，**事务内**写、不二次投递。
* C-09：旧 attempt/旧 generation 的写入沿用 T12 的 ``STALE_ATTEMPT`` /
  ``STALE_GENERATION`` 口径，不新造第二套错误码；失败不得被静默吞掉。
* 迁移 ``008_publication.sql``（schema ``p0_t13``）承载发布事实、发布内 Job、
  发布审计；版本状态机仍由 ``004`` 的 ``p0_t12`` 持有，这里不复刻第二套。

设计取舍（写进证据，不在代码里偷偷决定）
----------------------------------------
* **发布事实不可变**：``phase0_publications`` 一行 ⟺ 一次已提交的指针切换
  （与切换同事务写入）。失败尝试只写 events，因此「没有半次发布」是结构性的。
* **回滚 = 新版本行重新发布历史内容**：004 的状态机只允许 ``ready → active``，
  不允许 ``retired → active``，也不允许改写历史版本行。因此 ``rollback()`` 以
  ``desired_generation + 1`` 建新版本、把目标版本（历史版本）的 chunk 逐条重放、
  重新 ready 后再走同一条 ``_activate()`` 路径。证据里用 chunk checksum 集合相等
  证明「内容回滚一致」，而不是指针回退（指针回退会破坏版本不可变）。
* **幂等**：``(workspace, document, idempotency_key)`` 唯一；重复请求不切指针、
  不二次投递 Job，只追加一条 ``idempotent_replay`` 审计。
* **旧 worker 防护**：发布前 ``FOR UPDATE`` 锁 ``documents`` 行与目标版本行；发布
  之后旧 attempt 再写会命中 004 的 chunk fencing（``STALE_ATTEMPT``）。
* **同步点**：``barrier`` 只需实现 ``hit(name) -> str``（测试传
  :class:`faults.FaultBarrier`）；本模块**不 import** faults（它是测试 fixture），
  生产路径 ``barrier=None`` 时不产生任何阻塞。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

import index_staging as ji
from locator_map import Locator

__all__ = [
    "DEFAULT_JOB_KIND",
    "EVENT_OUTCOMES",
    "MANIFEST_KEY_COUNT",
    "MIGRATION_ID",
    "MIGRATION_PATH",
    "PUBLICATION_KINDS",
    "SCHEMA",
    "SYNC_POINTS",
    "IdempotencyConflict",
    "IndexPublicationError",
    "ManifestMismatch",
    "PublicationConflict",
    "PublicationFault",
    "PublicationOutcome",
    "PublicationRequest",
    "PublicationState",
    "RejectedPublication",
    "RollbackRequest",
    "RollbackTargetInvalid",
    "VersionNotReady",
    "apply_migration",
    "connect",
    "ensure_staging_schema",
    "list_events",
    "list_jobs",
    "list_publications",
    "migration_sha256",
    "publish",
    "publish_committed",
    "publication_state",
    "read_visibility",
    "reset_schema",
    "rollback",
    "rollback_committed",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SCHEMA: Final = "p0_t13"
MIGRATION_ID: Final = "008_publication"
MIGRATION_PATH: Final = (
    Path(__file__).resolve().parent / "db" / "migrations" / "008_publication.sql"
)

PUBLICATION_KINDS: Final[tuple[str, ...]] = ("publish", "rollback")
EVENT_OUTCOMES: Final[tuple[str, ...]] = (
    "validated",
    "committed",
    "rejected",
    "idempotent_replay",
    "stale_attempt",
    "stale_generation",
    "conflict",
    "rolled_back",
)
#: 本模块在发布事务内的同步点（测试用 FaultBarrier 注入；生产路径不注册）。
#: ``before_activation_commit`` 沿用 ``docs/poc/06`` §5 的断点名，其余两个是本任务
#: 为「切换语句已执行、事务未提交」这个窗口补的观察点。
SYNC_POINTS: Final[tuple[str, ...]] = (
    "before_validation",
    "before_activation_commit",
    "after_activation_before_commit",
)
DEFAULT_JOB_KIND: Final = "index.enrichment"
MANIFEST_KEY_COUNT: Final = len(ji.REQUIRED_MANIFEST_KEYS)
MAX_IDEMPOTENCY_KEY_LENGTH: Final = 128
MAX_REASON_LENGTH: Final = 256
MAX_DETAIL_LENGTH: Final = 512


# ---------------------------------------------------------------------------
# 错误（沿用 C-09 口径：stale 直接用 T12 的异常类型与 code）
# ---------------------------------------------------------------------------
class IndexPublicationError(Exception):
    """发布路径的基础错误；``code`` 是证据与日志里的稳定标识。"""

    code: str = "INDEX_PUBLICATION_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class RejectedPublication(IndexPublicationError):
    """请求本身不合法（文档不存在、key 超长等），不产生任何状态变化。"""

    code = "PUBLICATION_REJECTED"


class VersionNotReady(IndexPublicationError):
    code = "VERSION_NOT_READY"


class ManifestMismatch(IndexPublicationError):
    code = "MANIFEST_MISMATCH"


class IdempotencyConflict(IndexPublicationError):
    """同一幂等键被用于不同目标；拒绝而不是静默复用。"""

    code = "IDEMPOTENCY_CONFLICT"


class PublicationConflict(IndexPublicationError):
    """并发/状态竞争导致 CAS 失败（0 行更新或目标版本已非 ready）。"""

    code = "PUBLICATION_CONFLICT"


class RollbackTargetInvalid(IndexPublicationError):
    code = "ROLLBACK_TARGET_INVALID"


class PublicationFault(IndexPublicationError):
    """注入的同步点超时/故障：按失败处理，调用方必须回滚。"""

    code = "PUBLICATION_FAULT"


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
class SyncPoint(Protocol):
    """同步点协议：``faults.FaultBarrier`` 满足它，测试之外的实现可自定义。

    返回 ``"timeout"`` 表示该同步点未被放行（本模块按故障处理）；
    返回 ``"idle"`` 表示没有布置，直接放行。
    """

    def hit(self, name: str) -> str: ...


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    """一次发布请求：目标版本 + 幂等键（可选地钉死 attempt/manifest）。"""

    workspace_id: UUID
    document_id: UUID
    index_version: int
    idempotency_key: str
    expected_attempt_id: UUID | None = None
    expected_manifest_checksum: str | None = None


@dataclass(frozen=True, slots=True)
class RollbackRequest:
    """回滚请求：把 ``target_version`` 的内容以新版本行重新发布。"""

    workspace_id: UUID
    document_id: UUID
    target_version: int
    idempotency_key: str
    reason: str
    expected_active_version: int | None = None


@dataclass(frozen=True, slots=True)
class PublicationOutcome:
    """发布结果；``committed=False`` 表示调用方还需要 commit（本函数不提交）。"""

    workspace_id: UUID
    publication_id: UUID
    document_id: UUID
    kind: str
    from_version: int | None
    to_version: int
    rollback_of_version: int | None
    winning_attempt_id: UUID
    manifest_checksum: str
    chunk_count: int
    active_version: int
    document_row_version: int
    job_id: UUID
    job_created: bool
    replayed: bool
    committed: bool
    visible_chunk_count: int


@dataclass(frozen=True, slots=True)
class PublicationState:
    """一次快照（证据用）：指针、期望代数、各版本状态、可见 chunk checksum。"""

    workspace_id: UUID
    document_id: UUID
    desired_generation: int
    active_version: int | None
    document_row_version: int
    versions: tuple[dict[str, Any], ...]
    visible_checksums: tuple[str, ...]
    publication_count: int
    job_count: int


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
    """在单事务内应用迁移 008 并写台账；调用方 commit。

    不加 ``IF NOT EXISTS``：重复 apply 会 ``psycopg.errors.DuplicateSchema`` 显式失败，
    不会被静默跳过。
    """

    if conn.autocommit:
        raise RejectedPublication("apply_migration 需要 autocommit=False 的连接")
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        raise RejectedPublication("apply_migration 需要干净事务边界（IDLE）")
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
    """删除本任务 schema（清理用）；只碰 p0_t13，不动 p0_t12。"""

    if not conn.autocommit:
        conn.commit()
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))
    conn.commit()


def ensure_staging_schema(conn: psycopg.Connection[Any]) -> str:
    """保证 004（``p0_t12``）存在；返回 ``"present"`` 或 ``"applied"``。

    T13 的发布必须落在真实版本行上，而 004 属 T12（文件只读）。这里用 T12 自己的
    ``apply_migration`` API 建 schema，绝不改 004 的内容。
    """

    if not conn.autocommit:
        conn.commit()
    present = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (ji.SCHEMA,)
    ).fetchone()
    conn.commit()  # 上面这条 SELECT 已经开了事务；apply_migration 要求 IDLE
    if present is not None:
        return "present"
    ji.apply_migration(conn)
    conn.commit()
    return "applied"


# ---------------------------------------------------------------------------
# 发布（核心）
# ---------------------------------------------------------------------------
def publish(
    conn: psycopg.Connection[Any],
    request: PublicationRequest,
    *,
    barrier: SyncPoint | None = None,
    now: datetime | None = None,
) -> PublicationOutcome:
    """切换活动指针到 ``request.index_version``；**不提交事务**，由调用方 commit。

    失败以异常表达（``ji.StaleAttemptError`` / ``ji.StaleGenerationError`` 等），
    调用方必须 ``rollback()``；本函数在抛错前已写入的审计行会随事务一起丢弃，
    因此「失败尝试的审计」只在**提交过的**事务里才有意义 —— 证据里用
    ``outcome='rejected'`` 的已提交事务来记录被拒绝的发布尝试（见测试）。
    """

    moment = now or datetime.now(UTC)
    _require_request(request)
    _require_transaction(conn)

    existing = _publication_by_key(
        conn, request.workspace_id, request.document_id, request.idempotency_key
    )
    if existing is not None:
        _assert_replay_target(existing, kind="publish", to_version=request.index_version)
        _record_event(
            conn, workspace_id=request.workspace_id, document_id=request.document_id,
            publication_id=existing["publication_id"], index_version=request.index_version,
            outcome="idempotent_replay", error_code=None,
            detail=f"key={request.idempotency_key} to={request.index_version}",
        )
        return _outcome_from_row(conn, existing, replayed=True)

    _hit(barrier, "before_validation")

    document = _lock_document(conn, request.workspace_id, request.document_id)
    if document is None:
        raise RejectedPublication(f"document {request.document_id} 不存在")
    if int(document["desired_generation"]) != request.index_version:
        _record_event(
            conn, workspace_id=request.workspace_id, document_id=request.document_id,
            index_version=request.index_version, outcome="stale_generation",
            error_code=ji.StaleGenerationError.code,
            detail=f"desired={document['desired_generation']} request={request.index_version}",
        )
        raise ji.StaleGenerationError(
            f"desired_generation={document['desired_generation']} 已取代 {request.index_version}"
        )

    version = _lock_version(
        conn, request.workspace_id, request.document_id, request.index_version
    )
    if version is None:
        raise VersionNotReady(f"版本 {request.index_version} 不存在")
    if str(version["status"]) == "active":
        published = _publication_by_target(
            conn, request.workspace_id, request.document_id, request.index_version
        )
        if published is None:  # pragma: no cover - 指针/事实不一致的兜底
            raise PublicationConflict("版本已 active 但没有发布事实")
        _record_event(
            conn, workspace_id=request.workspace_id, document_id=request.document_id,
            publication_id=published["publication_id"], index_version=request.index_version,
            outcome="idempotent_replay", error_code=None,
            detail="目标版本已是 active，不再切换指针",
        )
        return _outcome_from_row(conn, published, replayed=True)
    if str(version["status"]) != "ready":
        raise VersionNotReady(f"版本 {request.index_version} 状态为 {version['status']}")

    if (
        request.expected_attempt_id is not None
        and version["winning_attempt_id"] != request.expected_attempt_id
    ):
        _record_event(
            conn, workspace_id=request.workspace_id, document_id=request.document_id,
            index_version=request.index_version, outcome="stale_attempt",
            error_code=ji.StaleAttemptError.code,
            detail=f"expected={request.expected_attempt_id} "
                   f"winner={version['winning_attempt_id']}",
        )
        raise ji.StaleAttemptError(
            f"attempt {request.expected_attempt_id} 不是版本 {request.index_version} 的 winner"
        )

    manifest_checksum = _require_ready_manifest(
        conn, request.workspace_id, request.document_id, request.index_version, version
    )
    if (
        request.expected_manifest_checksum is not None
        and manifest_checksum != request.expected_manifest_checksum
    ):
        raise ManifestMismatch(
            f"manifest checksum 不匹配：期望 {request.expected_manifest_checksum}，"
            f"实际 {manifest_checksum}"
        )
    _require_chunk_count(conn, request.workspace_id, request.document_id,
                         request.index_version, version)

    return _activate(
        conn,
        workspace_id=request.workspace_id,
        document_id=request.document_id,
        index_version=request.index_version,
        version=version,
        desired_generation=int(document["desired_generation"]),
        manifest_checksum=manifest_checksum,
        kind="publish",
        rollback_of_version=None,
        reason=None,
        idempotency_key=request.idempotency_key,
        barrier=barrier,
        moment=moment,
    )


def publish_committed(
    conn: psycopg.Connection[Any],
    request: PublicationRequest,
    *,
    barrier: SyncPoint | None = None,
    now: datetime | None = None,
) -> PublicationOutcome:
    """``publish`` + 立即 commit（便利入口；故障注入测试请直接用 ``publish``）。"""

    outcome = publish(conn, request, barrier=barrier, now=now)
    conn.commit()
    return outcome


def rollback(
    conn: psycopg.Connection[Any],
    request: RollbackRequest,
    *,
    store: ji.ObjectStore,
    barrier: SyncPoint | None = None,
    now: datetime | None = None,
) -> PublicationOutcome:
    """把 ``target_version`` 的内容以**新版本行**重新发布；**不提交事务**。

    为什么不是把 ``active_version`` 拨回旧数字：004 的状态机只允许
    ``ready → active``（``retired → active`` 非法），且历史版本行不可改写。
    版本不可变的正确回滚语义 = 用新代数重放旧内容，于是指针仍然"向前"走，
    而读者看到的内容回到历史版本。证据用 chunk checksum 集合相等证明内容一致。
    """

    moment = now or datetime.now(UTC)
    _require_rollback_request(request)
    _require_transaction(conn)

    existing = _publication_by_key(
        conn, request.workspace_id, request.document_id, request.idempotency_key
    )
    if existing is not None:
        _assert_replay_target(
            existing, kind="rollback", to_version=None,
            rollback_of_version=request.target_version,
        )
        _record_event(
            conn, workspace_id=request.workspace_id, document_id=request.document_id,
            publication_id=existing["publication_id"], index_version=request.target_version,
            outcome="idempotent_replay", error_code=None,
            detail=f"key={request.idempotency_key} rollback_of={request.target_version}",
        )
        return _outcome_from_row(conn, existing, replayed=True)

    _hit(barrier, "before_validation")

    document = _lock_document(conn, request.workspace_id, request.document_id)
    if document is None:
        raise RejectedPublication(f"document {request.document_id} 不存在")
    active = document["active_version"]
    if active is None:
        raise RollbackTargetInvalid("文档还没有 active 版本，无可回滚")
    if (
        request.expected_active_version is not None
        and int(active) != request.expected_active_version
    ):
        raise PublicationConflict(
            f"active_version 已变为 {active}（期望 {request.expected_active_version}）"
        )
    if request.target_version >= int(active):
        raise RollbackTargetInvalid(
            f"目标版本 {request.target_version} 不是 active {active} 之前的历史版本"
        )

    target = _version_row(conn, request.workspace_id, request.document_id,
                          request.target_version)
    if target is None:
        raise RollbackTargetInvalid(f"历史版本 {request.target_version} 不存在")
    if str(target["status"]) not in ("retired", "ready", "active"):
        raise RollbackTargetInvalid(
            f"历史版本 {request.target_version} 状态为 {target['status']}，内容不完整"
        )
    if target["manifest_checksum"] is None:
        raise RollbackTargetInvalid(f"历史版本 {request.target_version} 没有 manifest checksum")
    target_checksums = _chunk_checksums(conn, request.workspace_id, request.document_id,
                                        request.target_version)
    if len(target_checksums) != int(target["chunk_count"]):
        raise RollbackTargetInvalid("历史版本 chunk_count 与实际行数不一致")

    new_version = int(document["desired_generation"]) + 1
    ji.bump_desired_generation(
        conn, workspace_id=request.workspace_id, document_id=request.document_id, now=moment
    )
    attempt = ji.begin_attempt(
        conn,
        workspace_id=request.workspace_id,
        document_id=request.document_id,
        generation=new_version,
        now=moment,
    )
    replay_chunks = [
        ji.ChunkInput(kind=str(row["kind"]), text=str(row["text"]),
                      locator=Locator.from_dict(row["locator"]))
        for row in _chunk_rows(conn, request.workspace_id, request.document_id,
                               request.target_version)
    ]
    ji.stage_chunk_batch(conn, attempt, replay_chunks)
    manifest = ji.build_manifest(conn, attempt, store=store)
    record = ji.write_manifest(conn, attempt, manifest)
    if record.status != "ready":  # pragma: no cover - 重放历史 chunk 不应缺项
        raise ManifestMismatch(f"回滚 manifest 未 ready，缺 {record.missing_items}")
    version = ji.mark_version_ready(conn, attempt, now=moment)
    replayed_checksums = _chunk_checksums(
        conn, request.workspace_id, request.document_id, new_version
    )
    if sorted(replayed_checksums) != sorted(target_checksums):  # pragma: no cover - 内容自检
        raise ManifestMismatch("回滚重放的 chunk 集合与历史版本不一致")

    outcome = _activate(
        conn,
        workspace_id=request.workspace_id,
        document_id=request.document_id,
        index_version=new_version,
        version=_version_row(conn, request.workspace_id, request.document_id, new_version),
        desired_generation=new_version,
        manifest_checksum=str(version.manifest_checksum),
        kind="rollback",
        rollback_of_version=request.target_version,
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        barrier=barrier,
        moment=moment,
    )
    _record_event(
        conn, workspace_id=request.workspace_id, document_id=request.document_id,
        publication_id=outcome.publication_id, index_version=outcome.to_version,
        outcome="rolled_back", error_code=None,
        detail=f"rollback_of={request.target_version} reason={request.reason}",
    )
    return outcome


def rollback_committed(
    conn: psycopg.Connection[Any],
    request: RollbackRequest,
    *,
    store: ji.ObjectStore,
    barrier: SyncPoint | None = None,
    now: datetime | None = None,
) -> PublicationOutcome:
    """``rollback`` + 立即 commit。"""

    outcome = rollback(conn, request, store=store, barrier=barrier, now=now)
    conn.commit()
    return outcome


# ---------------------------------------------------------------------------
# 读取助手（证据与测试用）
# ---------------------------------------------------------------------------
def list_publications(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_publications "
        "WHERE workspace_id = %s AND document_id = %s ORDER BY committed_at, to_version",
        (workspace_id, document_id),
    ).fetchall()
    return [dict(row) for row in rows]


def list_jobs(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT j.* FROM {SCHEMA}.phase0_publication_jobs AS j "
        f"JOIN {SCHEMA}.phase0_publications AS p "
        "  ON p.workspace_id = j.workspace_id AND p.publication_id = j.publication_id "
        "WHERE j.workspace_id = %s AND p.document_id = %s ORDER BY j.created_at, j.job_id",
        (workspace_id, document_id),
    ).fetchall()
    return [dict(row) for row in rows]


def list_events(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_publication_events "
        "WHERE workspace_id = %s AND document_id = %s ORDER BY event_seq",
        (workspace_id, document_id),
    ).fetchall()
    return [dict(row) for row in rows]


def read_visibility(
    conn: psycopg.Connection[Any], workspace_id: UUID, *, limit: int = 10_000
) -> list[dict[str, Any]]:
    """直接复用 T12 的可见性探针（``%`` 匹配全部文本），不另造第二套查询。"""

    return ji.search_visible_chunks(conn, workspace_id=workspace_id, query="%", limit=limit)


def publication_state(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID
) -> PublicationState:
    document = ji.get_document(conn, workspace_id, document_id)
    versions = conn.execute(
        f"SELECT index_version, status, current_attempt_id, winning_attempt_id, "
        f"       manifest_checksum, chunk_count, row_version "
        f"FROM {ji.SCHEMA}.phase0_document_versions "
        "WHERE workspace_id = %s AND document_id = %s ORDER BY index_version",
        (workspace_id, document_id),
    ).fetchall()
    row_version = conn.execute(
        f"SELECT row_version FROM {ji.SCHEMA}.phase0_documents "
        "WHERE workspace_id = %s AND document_id = %s",
        (workspace_id, document_id),
    ).fetchone()
    visible = read_visibility(conn, workspace_id)
    return PublicationState(
        workspace_id=workspace_id,
        document_id=document_id,
        desired_generation=document.desired_generation,
        active_version=document.active_version,
        document_row_version=int(row_version["row_version"]),
        versions=tuple(dict(row) for row in versions),
        visible_checksums=tuple(sorted(str(row["checksum"]) for row in visible)),
        publication_count=len(list_publications(conn, workspace_id, document_id)),
        job_count=len(list_jobs(conn, workspace_id, document_id)),
    )


# ---------------------------------------------------------------------------
# 内部：切换
# ---------------------------------------------------------------------------
def _activate(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    document_id: UUID,
    index_version: int,
    version: dict[str, Any] | None,
    desired_generation: int,
    manifest_checksum: str,
    kind: str,
    rollback_of_version: int | None,
    reason: str | None,
    idempotency_key: str,
    barrier: SyncPoint | None,
    moment: datetime,
) -> PublicationOutcome:
    """三件事 + 事实 + Job + 审计，全部在本事务内；返回时**尚未提交**。"""

    if version is None:  # pragma: no cover - 调用方已校验
        raise PublicationConflict(f"版本 {index_version} 在切换前消失")
    if str(version["status"]) != "ready":  # pragma: no cover - 调用方已校验
        raise PublicationConflict(f"版本 {index_version} 状态为 {version['status']}，不能激活")
    if index_version != desired_generation:  # pragma: no cover - 调用方已校验
        raise PublicationConflict(
            f"generation {desired_generation} 与版本 {index_version} 不一致"
        )
    winning_attempt_id = version["winning_attempt_id"]
    if winning_attempt_id is None:  # pragma: no cover - ready 触发器已保证
        raise PublicationConflict(f"版本 {index_version} 没有 winning attempt")

    previous = _active_version(conn, workspace_id, document_id)
    if previous is not None:
        retired = conn.execute(
            f"UPDATE {ji.SCHEMA}.phase0_document_versions "
            "SET status = 'retired', updated_at = %s, row_version = row_version + 1 "
            "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
            "  AND status = 'active' RETURNING row_version",
            (moment, workspace_id, document_id, previous),
        ).fetchone()
        if retired is None:
            raise PublicationConflict(f"旧 active 版本 {previous} 已在别处切换")

    _hit(barrier, "before_activation_commit")

    activated = conn.execute(
        f"UPDATE {ji.SCHEMA}.phase0_document_versions "
        "SET status = 'active', updated_at = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        "  AND status = 'ready' RETURNING row_version",
        (moment, workspace_id, document_id, index_version),
    ).fetchone()
    if activated is None:
        raise PublicationConflict(f"版本 {index_version} 已不是 ready，切换被并发打断")

    publication_id = uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_publications ("
        "    workspace_id, publication_id, document_id, kind, to_version, from_version, "
        "    rollback_of_version, winning_attempt_id, manifest_checksum, chunk_count, "
        "    idempotency_key, reason, committed_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, publication_id, document_id, kind, index_version, previous,
         rollback_of_version, winning_attempt_id, manifest_checksum,
         int(version["chunk_count"]), idempotency_key, reason, moment),
    )

    job_kind = DEFAULT_JOB_KIND
    job_id = uuid4()
    dedup_key = f"{kind}:{document_id}:{index_version}"
    inserted = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_publication_jobs ("
        "    workspace_id, job_id, publication_id, kind, status, dedup_key, created_at"
        ") VALUES (%s, %s, %s, %s, 'pending', %s, %s) "
        "ON CONFLICT (workspace_id, dedup_key) DO NOTHING RETURNING job_id",
        (workspace_id, job_id, publication_id, job_kind, dedup_key, moment),
    ).fetchone()
    if inserted is not None:
        job_created = True
    else:  # 同一 dedup_key 已存在：不二次投递，返回既有 job
        job_created = False
        existing_job = conn.execute(
            f"SELECT job_id FROM {SCHEMA}.phase0_publication_jobs "
            "WHERE workspace_id = %s AND dedup_key = %s",
            (workspace_id, dedup_key),
        ).fetchone()
        if existing_job is None:  # pragma: no cover - ON CONFLICT 后的兜底
            raise PublicationConflict(f"Job dedup_key {dedup_key} 冲突但查不到既有行")
        job_id = existing_job["job_id"]

    current_row = conn.execute(
        f"SELECT row_version FROM {ji.SCHEMA}.phase0_documents "
        "WHERE workspace_id = %s AND document_id = %s",
        (workspace_id, document_id),
    ).fetchone()
    pointer = conn.execute(
        f"UPDATE {ji.SCHEMA}.phase0_documents "
        "SET active_version = %s, updated_at = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND document_id = %s AND row_version = %s "
        "RETURNING row_version",
        (index_version, moment, workspace_id, document_id, int(current_row["row_version"])),
    ).fetchone()
    if pointer is None:
        raise PublicationConflict("活动指针 CAS 失败（documents.row_version 被并发推进）")

    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_publication_events ("
        "    workspace_id, event_id, publication_id, document_id, index_version, "
        "    outcome, error_code, detail, recorded_at"
        ") VALUES (%s, %s, %s, %s, %s, 'committed', NULL, %s, %s)",
        (workspace_id, uuid4(), publication_id, document_id, index_version,
         _clip(f"from={previous} to={index_version} kind={kind}"), moment),
    )

    _hit(barrier, "after_activation_before_commit")

    visible = read_visibility(conn, workspace_id)
    visible_for_document = [row for row in visible if row["document_id"] == document_id]
    active_rows = [
        row for row in visible_for_document if int(row["index_version"]) == index_version
    ]
    if len(active_rows) != int(version["chunk_count"]):
        raise PublicationConflict(
            f"提交前可见性校验失败：版本 {index_version} 期望 "
            f"{version['chunk_count']} 条，实际 {len(active_rows)} 条"
        )
    if len(active_rows) != len(visible_for_document):
        raise PublicationConflict("提交前可见性校验失败：存在混版本的可见 chunk")

    return PublicationOutcome(
        workspace_id=workspace_id,
        publication_id=publication_id,
        document_id=document_id,
        kind=kind,
        from_version=previous,
        to_version=index_version,
        rollback_of_version=rollback_of_version,
        winning_attempt_id=winning_attempt_id,
        manifest_checksum=manifest_checksum,
        chunk_count=int(version["chunk_count"]),
        active_version=index_version,
        document_row_version=int(pointer["row_version"]),
        job_id=job_id,
        job_created=job_created,
        replayed=False,
        committed=False,
        visible_chunk_count=len(active_rows),
    )


# ---------------------------------------------------------------------------
# 内部：校验与读取
# ---------------------------------------------------------------------------
def _require_transaction(conn: psycopg.Connection[Any]) -> None:
    if conn.autocommit:
        raise RejectedPublication("发布必须在显式事务里执行（autocommit=False）")
    if conn.info.transaction_status is TransactionStatus.IDLE:
        raise RejectedPublication("发布必须在已开启的事务里执行（事务外发布即破坏原子性）")


def _require_request(request: PublicationRequest) -> None:
    if not isinstance(request, PublicationRequest):
        raise RejectedPublication("需要 PublicationRequest")
    _require_uuid(request.workspace_id, "workspace_id")
    _require_uuid(request.document_id, "document_id")
    if not isinstance(request.index_version, int) or request.index_version < 1:
        raise RejectedPublication("index_version 必须是 >= 1 的整数")
    _require_key(request.idempotency_key)


def _require_rollback_request(request: RollbackRequest) -> None:
    if not isinstance(request, RollbackRequest):
        raise RejectedPublication("需要 RollbackRequest")
    _require_uuid(request.workspace_id, "workspace_id")
    _require_uuid(request.document_id, "document_id")
    if not isinstance(request.target_version, int) or request.target_version < 1:
        raise RollbackTargetInvalid("target_version 必须是 >= 1 的整数")
    _require_key(request.idempotency_key)
    if not isinstance(request.reason, str) or not request.reason.strip():
        raise RejectedPublication("回滚必须给出非空 reason")
    if len(request.reason) > MAX_REASON_LENGTH:
        raise RejectedPublication(f"reason 过长（>{MAX_REASON_LENGTH}）")


def _require_uuid(value: object, name: str) -> None:
    if not isinstance(value, UUID):
        raise RejectedPublication(f"{name} 必须是 UUID")


def _require_key(key: object) -> None:
    if not isinstance(key, str) or not key.strip():
        raise IdempotencyConflict("idempotency_key 必须是非空字符串")
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise IdempotencyConflict(f"idempotency_key 过长（>{MAX_IDEMPOTENCY_KEY_LENGTH}）")


def _lock_document(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT workspace_id, document_id, desired_generation, active_version, row_version "
        f"FROM {ji.SCHEMA}.phase0_documents "
        "WHERE workspace_id = %s AND document_id = %s FOR UPDATE",
        (workspace_id, document_id),
    ).fetchone()
    return None if row is None else dict(row)


def _lock_version(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID, index_version: int
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT index_version, status, current_attempt_id, winning_attempt_id, "
        f"       manifest_checksum, chunk_count, row_version "
        f"FROM {ji.SCHEMA}.phase0_document_versions "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s FOR UPDATE",
        (workspace_id, document_id, index_version),
    ).fetchone()
    return None if row is None else dict(row)


def _version_row(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID, index_version: int
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT index_version, status, winning_attempt_id, manifest_checksum, "
        f"       chunk_count, row_version "
        f"FROM {ji.SCHEMA}.phase0_document_versions "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
        (workspace_id, document_id, index_version),
    ).fetchone()
    return None if row is None else dict(row)


def _active_version(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID
) -> int | None:
    row = conn.execute(
        f"SELECT index_version FROM {ji.SCHEMA}.phase0_document_versions "
        "WHERE workspace_id = %s AND document_id = %s AND status = 'active'",
        (workspace_id, document_id),
    ).fetchone()
    return None if row is None else int(row["index_version"])


def _chunk_rows(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID, index_version: int
) -> list[dict[str, Any]]:
    return ji.list_chunks(conn, workspace_id, document_id, index_version)


def _chunk_checksums(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID, index_version: int
) -> list[str]:
    return sorted(str(row["checksum"]) for row in _chunk_rows(
        conn, workspace_id, document_id, index_version))


def _chunk_count(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID, index_version: int
) -> int:
    row = conn.execute(
        f"SELECT count(*) AS n FROM {ji.SCHEMA}.phase0_index_chunks "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
        (workspace_id, document_id, index_version),
    ).fetchone()
    return int(row["n"])


def _require_ready_manifest(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    document_id: UUID,
    index_version: int,
    version: dict[str, Any],
) -> str:
    """校验「同 attempt 的 ready manifest + checksum 与版本一致」，返回 checksum。"""

    if version["manifest_checksum"] is None or version["winning_attempt_id"] is None:
        raise not_ready(index_version)
    row = conn.execute(
        f"SELECT status, checksum, attempt_id, missing_items "
        f"FROM {ji.SCHEMA}.phase0_index_manifests "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s",
        (workspace_id, document_id, index_version),
    ).fetchone()
    if row is None:
        raise not_ready(index_version)
    if str(row["status"]) != "ready":
        raise not_ready(index_version)
    if row["attempt_id"] != version["winning_attempt_id"]:
        raise not_ready(index_version)
    if row["checksum"] != version["manifest_checksum"]:
        raise ManifestMismatch(
            f"manifest checksum 与版本记录不一致：manifest={row['checksum']} "
            f"version={version['manifest_checksum']}"
        )
    keys = conn.execute(
        f"SELECT count(*) AS n FROM (SELECT jsonb_object_keys(manifest) AS key "
        f"FROM {ji.SCHEMA}.phase0_index_manifests "
        "WHERE workspace_id = %s AND document_id = %s AND index_version = %s "
        ") AS keys WHERE key = ANY(%s)",
        (workspace_id, document_id, index_version, list(ji.REQUIRED_MANIFEST_KEYS)),
    ).fetchone()
    if int(keys["n"]) != MANIFEST_KEY_COUNT:
        raise ManifestMismatch("manifest 缺必需项，不能发布")
    return str(row["checksum"])


def not_ready(index_version: int) -> IndexPublicationError:
    return ManifestMismatch(f"版本 {index_version} 的 manifest/attempt 不满足发布前置")


def _require_chunk_count(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    document_id: UUID,
    index_version: int,
    version: dict[str, Any],
) -> None:
    actual = _chunk_count(conn, workspace_id, document_id, index_version)
    if actual != int(version["chunk_count"]) or actual == 0:
        raise ManifestMismatch(
            f"chunk_count 不一致：版本 {version['chunk_count']}，实际 {actual}"
        )


def _publication_by_key(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID, key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_publications "
        "WHERE workspace_id = %s AND document_id = %s AND idempotency_key = %s",
        (workspace_id, document_id, key),
    ).fetchone()
    return None if row is None else dict(row)


def _publication_by_target(
    conn: psycopg.Connection[Any], workspace_id: UUID, document_id: UUID, to_version: int
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.phase0_publications "
        "WHERE workspace_id = %s AND document_id = %s AND to_version = %s",
        (workspace_id, document_id, to_version),
    ).fetchone()
    return None if row is None else dict(row)


def _assert_replay_target(
    existing: dict[str, Any],
    *,
    kind: str,
    to_version: int | None,
    rollback_of_version: int | None = None,
) -> None:
    if str(existing["kind"]) != kind:
        raise IdempotencyConflict(
            f"幂等键已用于 {existing['kind']}（#{existing['to_version']}），"
            f"拒绝用于 {kind}"
        )
    if kind == "publish":
        if int(existing["to_version"]) != to_version:
            raise IdempotencyConflict(
                f"幂等键已用于版本 {existing['to_version']}，拒绝用于 {to_version}"
            )
    elif int(existing["rollback_of_version"] or 0) != int(rollback_of_version or 0):
        raise IdempotencyConflict(
            f"幂等键已用于回滚到 {existing['rollback_of_version']}，"
            f"拒绝用于 {rollback_of_version}"
        )


def _outcome_from_row(
    conn: psycopg.Connection[Any], row: dict[str, Any], *, replayed: bool
) -> PublicationOutcome:
    state = publication_state(conn, row["workspace_id"], row["document_id"])
    return PublicationOutcome(
        workspace_id=row["workspace_id"],
        publication_id=row["publication_id"],
        document_id=row["document_id"],
        kind=str(row["kind"]),
        from_version=None if row["from_version"] is None else int(row["from_version"]),
        to_version=int(row["to_version"]),
        rollback_of_version=(
            None if row["rollback_of_version"] is None else int(row["rollback_of_version"])
        ),
        winning_attempt_id=row["winning_attempt_id"],
        manifest_checksum=str(row["manifest_checksum"]),
        chunk_count=int(row["chunk_count"]),
        active_version=state.active_version or 0,
        document_row_version=state.document_row_version,
        job_id=_job_id_for(conn, row["workspace_id"], row["publication_id"]),
        job_created=False,
        replayed=replayed,
        committed=True,
        visible_chunk_count=len(state.visible_checksums),
    )


def _job_id_for(
    conn: psycopg.Connection[Any], workspace_id: UUID, publication_id: UUID
) -> UUID:
    row = conn.execute(
        f"SELECT job_id FROM {SCHEMA}.phase0_publication_jobs "
        "WHERE workspace_id = %s AND publication_id = %s ORDER BY created_at LIMIT 1",
        (workspace_id, publication_id),
    ).fetchone()
    if row is None:  # pragma: no cover - 每次发布都会写 Job
        raise PublicationConflict(f"发布 {publication_id} 没有对应 Job")
    return row["job_id"]


def _record_event(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    document_id: UUID,
    outcome: str,
    index_version: int | None = None,
    publication_id: UUID | None = None,
    error_code: str | None = None,
    detail: str | None = None,
) -> None:
    if outcome not in EVENT_OUTCOMES:  # pragma: no cover - 内部常量
        raise RejectedPublication(f"未知 outcome {outcome}")
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_publication_events ("
        "    workspace_id, event_id, publication_id, document_id, index_version, "
        "    outcome, error_code, detail"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, uuid4(), publication_id, document_id, index_version,
         outcome, error_code, _clip(detail)),
    )


def _hit(barrier: SyncPoint | None, name: str) -> None:
    if name not in SYNC_POINTS:  # pragma: no cover - 内部常量
        raise RejectedPublication(f"未知同步点 {name}")
    if barrier is None:
        return
    state = barrier.hit(name)
    if state == "timeout":
        raise PublicationFault(f"同步点 {name} 未被放行（超时），按故障回滚")


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:MAX_DETAIL_LENGTH]
