"""P0-T07：Outbox 生产事务与派发 lease（真实 PostgreSQL + Redis 实验探针）。

验收依据
--------
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T07 任务卡；
* ``docs/poc/07_Contract_Closure.md`` C-01（发布事务中的 Job 唯一写入者）、
  C-08（Outbox 五态 / 死信 / 派发恢复）、C-09（终态作业重试，实现归 T08）；
* ``docs/poc/05_Phase0_Acceptance.md`` PoC-02 强制 PASS 项（rollback 50 轮执行 0、
  断 Redis / 发成未标记 / 死信 redrive 可复验）；
* ``docs/tdd/01_System_Architecture.md`` §6、``docs/tdd/07_Data_API_Security.md`` §3、
  ``docs/adr/0002_Outbox_Version_Publication.md``（提议，本模块没有把它变成已接受）。

契约事实与命名映射（冲突写在这里，不改 ADR/TDD）
------------------------------------------------
* **五态以契约为准**：C-08 §4 固定 ``pending / dispatching / retry_wait /
  dispatched / dead_letter``。任务卡写的「待派发 / 已派发 / 执行中 / 成功 / 死信」
  与契约不是一一对应：
  - 待派发 = ``pending``；已派发 = ``dispatched``；
  - 执行中 = ``dispatching``（持 lease 正在**发消息**，不是 Job 在执行）；
  - 成功 = **Job 侧事实**（``jobs.status='succeeded'``）。Outbox 只有 ``dispatched``，
    因为 C-08 §8 明确「dispatched 只说明已发送，不说明 Job 已完成」；
  - 重试等待 = ``retry_wait``（契约独有，任务卡未列）；死信 = ``dead_letter``。
  本模块与迁移 003 只实现契约五态，差异留给证据/报告，不静默扩展枚举。

实现边界（与 T08/T12/T13 的分工）
---------------------------------
* 本模块只做**派发侧**：``append``（同事务登记）/ ``claim``（SKIP LOCKED + lease）
  / ``mark_dispatched`` / ``retry`` / ``reclaim_expired_leases`` / ``redrive``。
* **消费者幂等、attempt/heartbeat、Job 对账、retry_terminal（C-09）归 T08**；
  dispatcher 从不 UPDATE ``phase0_jobs``，所以"已派发"永远不会被冒充成"Job 完成"。
* 派发不做 Job 状态门禁：终态 Job 的残留 Outbox 仍会派发，消费端 no-op（C-08 §8）；
  否则取消/失败会让 Outbox 永久 pending，而 T07 无权改 Job 终态。

顺序证据（publish 必须在事务提交之后）
--------------------------------------
不靠"代码看起来是这么写的"，而是做成**可观测事实**：

1. ``claim`` 在调用方给的短事务里执行；``OutboxDispatcher.run_once`` 离开
   ``with conn.transaction()`` 后才进入发布循环，并在发布前断言本连接
   ``transaction_status is IDLE``；
2. 每次发布前，``PublishObserver`` 用**独立连接**读同一行：必须已可见、状态为
   ``dispatching``、``lease_epoch`` 与本次领取一致。事务内未提交的行对独立连接
   不可见（READ COMMITTED），所以顺序错了会当场抛 ``PublishOrderViolation``；
3. 同一观测还从 ``pg_stat_activity`` 读发布方后端的 ``state`` / ``xact_start``：
   必须是 ``idle`` 且 ``xact_start IS NULL``，即"发布时没有任何事务开着"，这也
   就是"无长事务持网络 I/O"的直接证据；
4. 反例（负向对照）在测试里用同一套观测器构造：在 claim 事务内直接发布，必须被
   第 2/3 条抓住并失败——检测器本身也被测到，而不是只被测代码"自证"。

隔离与契约偏离（Lead 需复核）
-----------------------------
迁移 003 写 schema **p0_t07**，而不是 001 §10 留位写的"p0_t03 内增量"。原因：W3
并行子任务各独占一个 PG schema 槽，且 001 的会话级 teardown 会
``DROP SCHEMA p0_t03 CASCADE`` + ``DROP ROLE p0_app``，并行执行会互相抹掉对象。
表名/列名/枚举严格按 C-08，生产落地只是把 schema 前缀换成共享 schema 的增量迁移，
DDL 与语义不变。Redis 独占 **DB 3**（``REDIS_DB``）；``flush_slot`` 只清 DB 3，
绝不碰 0 或其它任务库。

所有结论必须由真实 PG/Redis 证明；本模块不提供 mock，也没有"预计通过"分支。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid4

import psycopg
import redis
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from scope import read_dsn as _read_phase0_dsn

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_DELIVERY_ATTEMPTS",
    "MIGRATION_ID",
    "MIGRATION_PATH",
    "OUTBOX_STATES",
    "REDIS_DB",
    "SCHEMA",
    "ClaimedDelivery",
    "DispatchOutcome",
    "DispatchReport",
    "EnqueuedMessage",
    "EnqueueRequest",
    "OutboxDispatcher",
    "OutboxSetupError",
    "PublishEnvelope",
    "PublishFailed",
    "PublishObservation",
    "PublishOrderViolation",
    "PublishReceipt",
    "RedisPublisher",
    "append",
    "apply_migration",
    "backoff_delay_seconds",
    "claim",
    "connect",
    "explain_claim",
    "flush_slot",
    "iter_plan_nodes",
    "mark_dispatched",
    "migration_sha256",
    "read_redis_url",
    "reclaim_expired_leases",
    "redrive",
    "redis_client",
    "reset_schema",
    "retry",
]

SCHEMA: Final = "p0_t07"
MIGRATION_ID: Final = "003_jobs_outbox"
MIGRATION_PATH: Final = (
    Path(__file__).resolve().parent / "db" / "migrations" / "003_jobs_outbox.sql"
)

#: T07 独占的 Redis 逻辑库（W3 资源槽；0 号库属于 T02/T04）。
REDIS_DB: Final = 3
REDIS_QUEUE_KEY: Final = "p0_t07:outbox:publish"

OutboxStatus = Literal["pending", "dispatching", "retry_wait", "dispatched", "dead_letter"]

#: C-08 §4 固定枚举（顺序即证据表里的展示顺序）。
OUTBOX_STATES: Final[tuple[str, ...]] = (
    "pending",
    "dispatching",
    "retry_wait",
    "dispatched",
    "dead_letter",
)

#: 与迁移 003 §6 触发器**镜像**的转移矩阵；测试交叉核对两边一致，防静默漂移。
ALLOWED_TRANSITIONS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("pending", "dispatching"),
        ("retry_wait", "dispatching"),
        ("dispatching", "dispatched"),
        ("dispatching", "retry_wait"),
        ("dispatching", "dead_letter"),
        ("retry_wait", "dead_letter"),
        ("dead_letter", "pending"),
    }
)

DEFAULT_MAX_DELIVERY_ATTEMPTS: Final = 5
DEFAULT_LEASE_SECONDS: Final = 30.0
BACKOFF_BASE_SECONDS: Final = 5.0
BACKOFF_CAP_SECONDS: Final = 300.0

_OPERATION_RE: Final = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_ENV_REDIS_HOST_RE: Final = re.compile(r"^PHASE0_REDIS_HOST=(.+)$", re.MULTILINE)
_ENV_REDIS_PORT_RE: Final = re.compile(r"^PHASE0_REDIS_HOST_PORT=(.+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------
class OutboxSetupError(RuntimeError):
    """环境/用法错误（不是授权或业务结论）。"""

    code = "OUTBOX_SETUP_ERROR"


class PublishFailed(RuntimeError):
    """broker/队列发布失败；``error_code`` 会落进 ``outbox.last_error_code``。"""

    code = "PUBLISH_FAILED"

    def __init__(self, message: str, *, error_code: str = "BROKER_UNAVAILABLE") -> None:
        super().__init__(message)
        self.error_code = error_code


class PublishOrderViolation(RuntimeError):
    """发布发生在事务提交之前 / 发布时仍有事务开着（C-08 §6 顺序被破坏）。"""

    code = "PUBLISH_NOT_AFTER_COMMIT"

    def __init__(self, reason: str, observation: PublishObservation) -> None:
        super().__init__(f"publish 顺序违规（{reason}）：{observation.describe()}")
        self.reason = reason
        self.observation = observation


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EnqueueRequest:
    """一次"业务 + Job + Outbox"登记请求（同事务，C-01 §6 / C-08 §6）。"""

    workspace_id: UUID
    target_id: UUID
    target_name: str
    job_id: UUID
    dedup_key: str
    kind: Literal["fetch", "ingest"] = "ingest"
    operation: str = "ingest.execute"
    outbox_id: UUID | None = None
    max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS

    def __post_init__(self) -> None:
        for name in ("workspace_id", "target_id", "job_id"):
            if not isinstance(getattr(self, name), UUID):
                raise OutboxSetupError(f"{name} 必须是 uuid.UUID")
        if self.outbox_id is not None and not isinstance(self.outbox_id, UUID):
            raise OutboxSetupError("outbox_id 必须是 uuid.UUID 或 None")
        if not isinstance(self.target_name, str) or not 1 <= len(self.target_name) <= 200:
            raise OutboxSetupError("target_name 长度必须在 [1, 200]")
        if not isinstance(self.dedup_key, str) or not 1 <= len(self.dedup_key) <= 256:
            raise OutboxSetupError("dedup_key 长度必须在 [1, 256]")
        if self.kind not in ("fetch", "ingest"):
            raise OutboxSetupError(f"kind 必须是 fetch/ingest，收到 {self.kind!r}")
        if _OPERATION_RE.fullmatch(self.operation) is None:
            raise OutboxSetupError(f"operation 不合法: {self.operation!r}")
        if not 1 <= self.max_delivery_attempts <= 100:
            raise OutboxSetupError("max_delivery_attempts 必须在 [1, 100]")

    @property
    def resolved_outbox_id(self) -> UUID:
        return self.outbox_id or self.job_id


@dataclass(frozen=True, slots=True)
class EnqueuedMessage:
    """登记结果；``*_created`` 为 False 表示命中唯一键、复用既有事实（幂等）。"""

    workspace_id: UUID
    job_id: UUID
    outbox_id: UUID
    operation: str
    dedup_key: str
    status: str
    lease_epoch: int
    delivery_attempt_count: int
    max_delivery_attempts: int
    job_created: bool
    outbox_created: bool


@dataclass(frozen=True, slots=True)
class PublishEnvelope:
    """发给 broker 的业务消息：只带可信引用，不带内容、不带凭据。

    ``job_id`` / ``outbox_id`` 是稳定业务键；``delivery_attempt`` 只是本次投递次数，
    消费者不得用它当幂等键（幂等键是 job_id，见 C-08 §7 与 T08）。
    """

    workspace_id: UUID
    outbox_id: UUID
    job_id: UUID
    operation: str
    dedup_key: str
    delivery_attempt: int
    lease_epoch: int
    payload_schema_version: int
    payload_refs: tuple[tuple[str, str], ...] = ()

    def as_message(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "outbox_id": str(self.outbox_id),
            "job_id": str(self.job_id),
            "operation": self.operation,
            "dedup_key": self.dedup_key,
            "delivery_attempt": self.delivery_attempt,
            "payload_schema_version": self.payload_schema_version,
            "payload_refs": [{"kind": kind, "id": value} for kind, value in self.payload_refs],
        }


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    """一次 claim 的结果（持 lease，``lease_epoch`` 是本次 fencing 值）。"""

    workspace_id: UUID
    outbox_id: UUID
    job_id: UUID
    target_id: UUID
    operation: str
    dedup_key: str
    job_status: str
    previous_status: str
    lease_epoch: int
    delivery_attempt_count: int
    max_delivery_attempts: int
    payload_schema_version: int
    available_at: datetime
    lease_expires_at: datetime
    claimed_at: datetime

    def envelope(self) -> PublishEnvelope:
        return PublishEnvelope(
            workspace_id=self.workspace_id,
            outbox_id=self.outbox_id,
            job_id=self.job_id,
            operation=self.operation,
            dedup_key=self.dedup_key,
            delivery_attempt=self.delivery_attempt_count,
            lease_epoch=self.lease_epoch,
            payload_schema_version=self.payload_schema_version,
            payload_refs=(("job", str(self.job_id)), ("target", str(self.target_id))),
        )


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """CAS 结果；``updated=False`` 时 ``stale`` 必须为 True（不许静默成功）。"""

    workspace_id: UUID
    outbox_id: UUID
    lease_epoch: int
    previous_status: str | None
    status: str | None
    updated: bool
    stale: bool
    outcome: str
    event_id: UUID | None
    delivery_attempt_count: int
    available_at: datetime | None


@dataclass(frozen=True, slots=True)
class PublishObservation:
    """发布时刻的独立观测（顺序证据）。"""

    outbox_id: UUID
    expected_epoch: int
    visible: bool
    observed_status: str | None
    observed_epoch: int | None
    dispatcher_state: str | None
    dispatcher_in_transaction: bool
    observed_at: datetime

    def describe(self) -> str:
        return (
            f"outbox={self.outbox_id} expected_epoch={self.expected_epoch} "
            f"visible={self.visible} status={self.observed_status} "
            f"observed_epoch={self.observed_epoch} "
            f"dispatcher_state={self.dispatcher_state!r} "
            f"dispatcher_in_transaction={self.dispatcher_in_transaction}"
        )


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    """broker 侧回执；``transport_id`` 只做传输关联，**不是**业务 task_id。"""

    transport_id: str
    queue: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """一次 ``run_once`` 的全部事实，测试据此断言而不是"看日志猜"。"""

    claimed: tuple[ClaimedDelivery, ...] = ()
    published: tuple[PublishEnvelope, ...] = ()
    outcomes: tuple[DispatchOutcome, ...] = ()
    observations: tuple[PublishObservation, ...] = ()
    failures: tuple[tuple[UUID, str], ...] = ()
    claim_seconds: float = 0.0
    publish_backend_states: tuple[str, ...] = ()
    dispatcher_pid: int | None = None


# ---------------------------------------------------------------------------
# 连接 / 迁移 / Redis
# ---------------------------------------------------------------------------
def connect(*, dsn: str | None = None, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn or _read_phase0_dsn(), autocommit=autocommit)


def migration_sha256(path: Path = MIGRATION_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migration(
    conn: psycopg.Connection[Any],
    *,
    path: Path = MIGRATION_PATH,
    migration_id: str = MIGRATION_ID,
) -> str:
    """单事务应用迁移 003 并写本 schema 台账；调用方负责 commit。

    与 ``scope.apply_migration`` 同构，但台账写 ``p0_t07.phase0_schema_migrations``
    （T03 的台账在 p0_t03，两套 schema 各自独立）；迁移文件不含 BEGIN/COMMIT。
    """

    if conn.autocommit:
        raise OutboxSetupError("apply_migration 需要 autocommit=False 的连接")
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        raise OutboxSetupError("apply_migration 需要干净事务边界（IDLE）")
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
    """删除本任务 schema（清理用）；只碰 p0_t07。"""

    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))


def read_redis_url(env_file: Path | None = None) -> str:
    """Redis URL：``PHASE0_T07_REDIS_URL`` > ``PHASE0_REDIS_URL`` > ``.env.example``。

    无论来源，**逻辑库一律强制为 DB 3**（T07 资源槽），避免误清 T02/T04 的 0 号库。
    """

    base = _lookup_redis_url()
    if base is None:
        path = env_file if env_file is not None else MIGRATION_PATH.parents[2] / ".env.example"
        text = path.read_text(encoding="utf-8")
        host_match = _ENV_REDIS_HOST_RE.search(text)
        port_match = _ENV_REDIS_PORT_RE.search(text)
        if host_match is None or port_match is None:
            raise OutboxSetupError(f"{path} 缺少 PHASE0_REDIS_HOST / PHASE0_REDIS_HOST_PORT")
        base = f"redis://{host_match.group(1).strip()}:{port_match.group(1).strip()}/0"
    head, _, _ = base.rpartition("/")
    return f"{head}/{REDIS_DB}"


def _lookup_redis_url() -> str | None:
    for name in ("PHASE0_T07_REDIS_URL", "PHASE0_REDIS_URL"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


def redis_client(url: str | None = None) -> redis.Redis:
    """T07 专用客户端：decode_responses，短超时（失败要快，不拖测试）。"""

    return redis.Redis.from_url(
        url or read_redis_url(),
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=2.0,
    )


def flush_slot(client: redis.Redis) -> None:
    """只清 T07 独占的 DB 3；连接不是该库时拒绝（不猜、不越界）。"""

    db = client.connection_pool.connection_kwargs.get("db")
    if db != REDIS_DB:
        raise OutboxSetupError(
            f"flush 只允许作用于 T07 独占的 Redis DB {REDIS_DB}，当前连接 db={db!r}"
        )
    client.flushdb()


# ---------------------------------------------------------------------------
# append：业务 + Job + Outbox 同一事务
# ---------------------------------------------------------------------------
def append(conn: psycopg.Connection[Any], request: EnqueueRequest) -> EnqueuedMessage:
    """C-08 §4 ``append(uow, event)``：在调用方事务内写业务事实 + Job + Outbox。

    **不在本函数里提交**：事务边界属于编排用例（C-01 §6）。所以"提交前崩溃"就是
    调用方不 commit 而连接断开/被终止——三张表要么一起留下，要么一起消失。
    重复调用命中唯一键时复用既有 job_id / outbox_id（``*_created=False``），
    消息身份不变（C-08 §7）。
    """

    _require_non_autocommit(conn, "append")
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_ingest_targets (workspace_id, target_id, name) "
        "VALUES (%s, %s, %s) ON CONFLICT (workspace_id, target_id) DO NOTHING",
        (request.workspace_id, request.target_id, request.target_name),
    )

    job_row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_jobs (workspace_id, job_id, target_id, kind, dedup_key) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (workspace_id, kind, dedup_key) DO NOTHING RETURNING job_id",
        (request.workspace_id, request.job_id, request.target_id, request.kind, request.dedup_key),
    ).fetchone()
    job_created = job_row is not None
    job_id = request.job_id
    if not job_created:
        existing = conn.execute(
            f"SELECT job_id FROM {SCHEMA}.phase0_jobs "
            "WHERE workspace_id = %s AND kind = %s AND dedup_key = %s",
            (request.workspace_id, request.kind, request.dedup_key),
        ).fetchone()
        if existing is None:  # pragma: no cover - UNIQUE 冲突后必然能查到
            raise OutboxSetupError("dedup_key 冲突但读不到既有 Job")
        job_id = existing[0]

    outbox_row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_outbox "
        "(workspace_id, outbox_id, job_id, operation, dedup_key, max_delivery_attempts) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (workspace_id, operation, dedup_key) DO NOTHING RETURNING outbox_id",
        (
            request.workspace_id,
            request.resolved_outbox_id,
            job_id,
            request.operation,
            request.dedup_key,
            request.max_delivery_attempts,
        ),
    ).fetchone()
    outbox_created = outbox_row is not None
    if outbox_created:
        outbox_id = outbox_row[0]
    else:
        existing = conn.execute(
            f"SELECT outbox_id FROM {SCHEMA}.phase0_outbox "
            "WHERE workspace_id = %s AND operation = %s AND dedup_key = %s",
            (request.workspace_id, request.operation, request.dedup_key),
        ).fetchone()
        if existing is None:  # pragma: no cover
            raise OutboxSetupError("dedup_key 冲突但读不到既有 Outbox")
        outbox_id = existing[0]

    row = conn.execute(
        f"SELECT status, lease_epoch, delivery_attempt_count, max_delivery_attempts "
        f"FROM {SCHEMA}.phase0_outbox WHERE workspace_id = %s AND outbox_id = %s",
        (request.workspace_id, outbox_id),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise OutboxSetupError("登记后读不到 Outbox 行")
    return EnqueuedMessage(
        workspace_id=request.workspace_id,
        job_id=job_id,
        outbox_id=outbox_id,
        operation=request.operation,
        dedup_key=request.dedup_key,
        status=row[0],
        lease_epoch=row[1],
        delivery_attempt_count=row[2],
        max_delivery_attempts=row[3],
        job_created=job_created,
        outbox_created=outbox_created,
    )


# ---------------------------------------------------------------------------
# claim：SKIP LOCKED + lease
# ---------------------------------------------------------------------------
_CLAIM_SELECT_SQL: Final = (
    "SELECT o.workspace_id, o.outbox_id, o.job_id, o.operation, o.dedup_key, o.status, "
    "o.lease_epoch, o.delivery_attempt_count, o.max_delivery_attempts, "
    "o.payload_schema_version, o.available_at, o.lease_expires_at, "
    "j.target_id, j.status AS job_status "
    f"FROM {SCHEMA}.phase0_outbox AS o "
    f"JOIN {SCHEMA}.phase0_jobs AS j "
    "ON j.workspace_id = o.workspace_id AND j.job_id = o.job_id "
    "WHERE o.workspace_id = %(workspace_id)s "
    "AND o.status IN ('pending', 'retry_wait') "
    "AND o.available_at <= %(now)s "
    "ORDER BY o.available_at, o.created_at, o.outbox_id "
    "LIMIT %(limit)s "
    "FOR UPDATE OF o SKIP LOCKED"
)

_CLAIM_UPDATE_SQL: Final = (
    f"UPDATE {SCHEMA}.phase0_outbox AS o SET "
    "status = 'dispatching', "
    "lease_epoch = o.lease_epoch + 1, "
    "lease_expires_at = %(lease_expires_at)s, "
    "delivery_attempt_count = o.delivery_attempt_count + 1, "
    "last_attempt_at = %(now)s, "
    "row_version = o.row_version + 1 "
    "WHERE o.workspace_id = %(workspace_id)s AND o.outbox_id = ANY(%(outbox_ids)s::uuid[]) "
    "RETURNING o.workspace_id, o.outbox_id, o.job_id, o.operation, o.dedup_key, "
    "o.lease_epoch, o.delivery_attempt_count, o.max_delivery_attempts, "
    "o.payload_schema_version, o.available_at, o.lease_expires_at"
)


def claim(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    *,
    limit: int,
    now: datetime,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> tuple[ClaimedDelivery, ...]:
    """在调用方事务内领取可派发行：``FOR UPDATE SKIP LOCKED`` + 递增 lease epoch。

    两条语句在同一事务：先 ``SELECT ... FOR UPDATE OF o SKIP LOCKED`` 锁候选行（并
    记下 OLD 状态给审计用），再 ``UPDATE`` 置 ``dispatching``。行锁持到提交，所以并发
    worker 的候选集互斥——被锁的行直接跳过，不会重复领取。
    """

    _require_non_autocommit(conn, "claim")
    _require_limit(limit)
    if not isinstance(workspace_id, UUID):
        raise OutboxSetupError("workspace_id 必须是 uuid.UUID")
    now = _require_aware(now, "now")
    if lease_seconds <= 0:
        raise OutboxSetupError("lease_seconds 必须 > 0")
    lease_until = now + timedelta(seconds=lease_seconds)
    with conn.cursor(row_factory=dict_row) as cur:
        candidates = cur.execute(
            _CLAIM_SELECT_SQL,
            {"workspace_id": workspace_id, "now": now, "limit": limit},
        ).fetchall()
        if not candidates:
            return ()
        updated = cur.execute(
            _CLAIM_UPDATE_SQL,
            {
                "workspace_id": workspace_id,
                "outbox_ids": [row["outbox_id"] for row in candidates],
                "lease_expires_at": lease_until,
                "now": now,
            },
        ).fetchall()

    old_status = {row["outbox_id"]: row["status"] for row in candidates}
    updated_by_id = {row["outbox_id"]: row for row in updated}
    events: list[tuple[Any, ...]] = []
    deliveries: list[ClaimedDelivery] = []
    for candidate in candidates:
        row = updated_by_id[candidate["outbox_id"]]
        events.append(
            (
                workspace_id,
                uuid4(),
                row["outbox_id"],
                row["lease_epoch"],
                old_status[row["outbox_id"]],
                "dispatching",
                "claimed",
                None,
                None,
            )
        )
        deliveries.append(
            ClaimedDelivery(
                workspace_id=workspace_id,
                outbox_id=row["outbox_id"],
                job_id=row["job_id"],
                target_id=candidate["target_id"],
                operation=row["operation"],
                dedup_key=row["dedup_key"],
                job_status=candidate["job_status"],
                previous_status=old_status[row["outbox_id"]],
                lease_epoch=row["lease_epoch"],
                delivery_attempt_count=row["delivery_attempt_count"],
                max_delivery_attempts=row["max_delivery_attempts"],
                payload_schema_version=row["payload_schema_version"],
                available_at=row["available_at"],
                lease_expires_at=row["lease_expires_at"],
                claimed_at=now,
            )
        )
    _insert_events(conn, events)
    return tuple(deliveries)


def mark_dispatched(
    conn: psycopg.Connection[Any],
    delivery: ClaimedDelivery,
    *,
    now: datetime | None = None,
) -> DispatchOutcome:
    """CAS：``WHERE status='dispatching' AND lease_epoch=<本次>`` → ``dispatched``。

    0 行即"旧 epoch / 已被回收"：**不静默成功**——写一条 ``outcome='stale'`` 审计行并
    返回 ``stale=True``，调用方据此放弃本次结果、不改任何业务事实。
    """

    _require_non_autocommit(conn, "mark_dispatched")
    now = _require_aware(now or datetime.now(UTC), "now")
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            f"UPDATE {SCHEMA}.phase0_outbox SET status = 'dispatched', "
            "dispatched_at = %(now)s, lease_expires_at = NULL, last_error_code = NULL, "
            "row_version = row_version + 1 "
            "WHERE workspace_id = %(workspace_id)s AND outbox_id = %(outbox_id)s "
            "AND status = 'dispatching' AND lease_epoch = %(lease_epoch)s "
            "RETURNING delivery_attempt_count, available_at",
            {
                "workspace_id": delivery.workspace_id,
                "outbox_id": delivery.outbox_id,
                "lease_epoch": delivery.lease_epoch,
                "now": now,
            },
        ).fetchone()
    if row is None:
        return _record_stale(conn, delivery, attempted="dispatched")
    event_id = _insert_events(
        conn,
        [
            (
                delivery.workspace_id,
                uuid4(),
                delivery.outbox_id,
                delivery.lease_epoch,
                "dispatching",
                "dispatched",
                "dispatched",
                None,
                None,
            )
        ],
    )[0]
    return DispatchOutcome(
        workspace_id=delivery.workspace_id,
        outbox_id=delivery.outbox_id,
        lease_epoch=delivery.lease_epoch,
        previous_status="dispatching",
        status="dispatched",
        updated=True,
        stale=False,
        outcome="dispatched",
        event_id=event_id,
        delivery_attempt_count=row["delivery_attempt_count"],
        available_at=row["available_at"],
    )


def retry(
    conn: psycopg.Connection[Any],
    delivery: ClaimedDelivery,
    *,
    error_code: str,
    now: datetime | None = None,
    detail: str | None = None,
) -> DispatchOutcome:
    """CAS：本次领取失败 → ``retry_wait``（指数退避）或耗尽 → ``dead_letter``。

    退避公式可测：``min(BASE * 2 ** (attempt - 1), CAP)``，attempt 是**已发生**的投递
    次数；不引入随机抖动，保证证据可复算（生产若加抖动必须另行取证，不能偷偷改）。
    """

    _require_non_autocommit(conn, "retry")
    now = _require_aware(now or datetime.now(UTC), "now")
    _require_error_code(error_code)
    with conn.cursor(row_factory=dict_row) as cur:
        locked = cur.execute(
            f"SELECT status, lease_epoch, delivery_attempt_count, max_delivery_attempts "
            f"FROM {SCHEMA}.phase0_outbox "
            "WHERE workspace_id = %s AND outbox_id = %s FOR UPDATE",
            (delivery.workspace_id, delivery.outbox_id),
        ).fetchone()
    if (
        locked is None
        or locked["status"] != "dispatching"
        or locked["lease_epoch"] != delivery.lease_epoch
    ):
        return _record_stale(conn, delivery, attempted="retry_wait")
    attempts = locked["delivery_attempt_count"]
    if attempts >= locked["max_delivery_attempts"]:
        return _terminate_dead_letter(
            conn, delivery, attempts=attempts, error_code=error_code, now=now, detail=detail
        )
    available_at = now + timedelta(seconds=backoff_delay_seconds(attempts))
    with conn.cursor() as cur:
        updated = cur.execute(
            f"UPDATE {SCHEMA}.phase0_outbox SET status = 'retry_wait', "
            "available_at = %(available_at)s, lease_epoch = lease_epoch + 1, "
            "lease_expires_at = NULL, last_error_code = %(error_code)s, "
            "row_version = row_version + 1 "
            "WHERE workspace_id = %(workspace_id)s AND outbox_id = %(outbox_id)s "
            "AND status = 'dispatching' AND lease_epoch = %(lease_epoch)s",
            {
                "available_at": available_at,
                "error_code": error_code,
                "workspace_id": delivery.workspace_id,
                "outbox_id": delivery.outbox_id,
                "lease_epoch": delivery.lease_epoch,
            },
        )
        if updated.rowcount != 1:  # pragma: no cover - 行锁内不可能失败
            return _record_stale(conn, delivery, attempted="retry_wait")
    event_id = _insert_events(
        conn,
        [
            (
                delivery.workspace_id,
                uuid4(),
                delivery.outbox_id,
                delivery.lease_epoch + 1,
                "dispatching",
                "retry_wait",
                "retry_wait",
                error_code,
                _clip(detail),
            )
        ],
    )[0]
    return DispatchOutcome(
        workspace_id=delivery.workspace_id,
        outbox_id=delivery.outbox_id,
        lease_epoch=delivery.lease_epoch + 1,
        previous_status="dispatching",
        status="retry_wait",
        updated=True,
        stale=False,
        outcome="retry_wait",
        event_id=event_id,
        delivery_attempt_count=attempts,
        available_at=available_at,
    )


def reclaim_expired_leases(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    *,
    now: datetime,
) -> tuple[DispatchOutcome, ...]:
    """回收租约过期的 ``dispatching`` 行（发成未标记 / 进程死亡的恢复路径）。

    逐行决定：次数未耗尽 → ``retry_wait``（同一退避公式）；已耗尽 → ``dead_letter``。
    ``lease_epoch`` 一律 +1，所以那个"死掉但可能还活着"的旧派发者回来标记时必然 stale。
    """

    _require_non_autocommit(conn, "reclaim_expired_leases")
    now = _require_aware(now, "now")
    with conn.cursor(row_factory=dict_row) as cur:
        expired = cur.execute(
            f"SELECT outbox_id, lease_epoch, delivery_attempt_count, "
            "max_delivery_attempts, last_error_code "
            f"FROM {SCHEMA}.phase0_outbox "
            "WHERE workspace_id = %s AND status = 'dispatching' AND lease_expires_at <= %s "
            "ORDER BY lease_expires_at, outbox_id FOR UPDATE SKIP LOCKED",
            (workspace_id, now),
        ).fetchall()

    outcomes: list[DispatchOutcome] = []
    for row in expired:
        handle = _LeaseKey(workspace_id, row["outbox_id"], row["lease_epoch"])
        if row["delivery_attempt_count"] >= row["max_delivery_attempts"]:
            outcomes.append(
                _terminate_dead_letter(
                    conn,
                    handle,
                    attempts=row["delivery_attempt_count"],
                    error_code=row["last_error_code"] or "LEASE_EXPIRED",
                    now=now,
                    detail="lease 过期且投递次数已耗尽",
                )
            )
            continue
        available_at = now + timedelta(seconds=backoff_delay_seconds(row["delivery_attempt_count"]))
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA}.phase0_outbox SET status = 'retry_wait', "
                "available_at = %(available_at)s, lease_epoch = lease_epoch + 1, "
                "lease_expires_at = NULL, row_version = row_version + 1 "
                "WHERE workspace_id = %(workspace_id)s AND outbox_id = %(outbox_id)s "
                "AND status = 'dispatching' AND lease_epoch = %(lease_epoch)s",
                {
                    "available_at": available_at,
                    "workspace_id": workspace_id,
                    "outbox_id": row["outbox_id"],
                    "lease_epoch": row["lease_epoch"],
                },
            )
        event_id = _insert_events(
            conn,
            [
                (
                    workspace_id,
                    uuid4(),
                    row["outbox_id"],
                    row["lease_epoch"] + 1,
                    "dispatching",
                    "retry_wait",
                    "reclaimed",
                    "LEASE_EXPIRED",
                    "租约过期回收",
                )
            ],
        )[0]
        outcomes.append(
            DispatchOutcome(
                workspace_id=workspace_id,
                outbox_id=row["outbox_id"],
                lease_epoch=row["lease_epoch"] + 1,
                previous_status="dispatching",
                status="retry_wait",
                updated=True,
                stale=False,
                outcome="reclaimed",
                event_id=event_id,
                delivery_attempt_count=row["delivery_attempt_count"],
                available_at=available_at,
            )
        )
    return tuple(outcomes)


def redrive(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    outbox_id: UUID,
    expected_row_version: int,
    reason: str,
    now: datetime | None = None,
) -> DispatchOutcome:
    """人工重派发：``dead_letter → pending``（C-08 §8），沿用 outbox_id/job_id。

    ``expected_row_version`` 是乐观锁：并发/重复 redrive 第二次必然 0 行 → 返回
    ``stale=True`` 并留审计，不静默成功。``lease_epoch`` +1 让旧派发者的 epoch 作废。
    """

    _require_non_autocommit(conn, "redrive")
    now = _require_aware(now or datetime.now(UTC), "now")
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            f"UPDATE {SCHEMA}.phase0_outbox SET status = 'pending', available_at = %(now)s, "
            "lease_epoch = lease_epoch + 1, lease_expires_at = NULL, "
            "redrive_count = redrive_count + 1, last_error_code = NULL, "
            "row_version = row_version + 1 "
            "WHERE workspace_id = %(workspace_id)s AND outbox_id = %(outbox_id)s "
            "AND status = 'dead_letter' AND row_version = %(row_version)s "
            "RETURNING lease_epoch, delivery_attempt_count, available_at",
            {
                "now": now,
                "workspace_id": workspace_id,
                "outbox_id": outbox_id,
                "row_version": expected_row_version,
            },
        ).fetchone()
        if row is None:
            current = cur.execute(
                f"SELECT status, lease_epoch, delivery_attempt_count, available_at "
                f"FROM {SCHEMA}.phase0_outbox WHERE workspace_id = %s AND outbox_id = %s",
                (workspace_id, outbox_id),
            ).fetchone()
    if row is None:
        if current is None:
            # 行不存在：不写事件（事件表有复合 FK，不能指向不存在的 outbox）。
            return DispatchOutcome(
                workspace_id=workspace_id,
                outbox_id=outbox_id,
                lease_epoch=0,
                previous_status=None,
                status=None,
                updated=False,
                stale=True,
                outcome="stale",
                event_id=None,
                delivery_attempt_count=0,
                available_at=None,
            )
        event_id = _insert_events(
            conn,
            [
                (
                    workspace_id,
                    uuid4(),
                    outbox_id,
                    current["lease_epoch"],
                    current["status"],
                    current["status"],
                    "stale",
                    "REDRIVE_CONFLICT",
                    _clip(f"row_version 期望 {expected_row_version} 未命中"),
                )
            ],
        )[0]
        return DispatchOutcome(
            workspace_id=workspace_id,
            outbox_id=outbox_id,
            lease_epoch=current["lease_epoch"],
            previous_status=current["status"],
            status=current["status"],
            updated=False,
            stale=True,
            outcome="stale",
            event_id=event_id,
            delivery_attempt_count=current["delivery_attempt_count"],
            available_at=current["available_at"],
        )
    event_id = _insert_events(
        conn,
        [
            (
                workspace_id,
                uuid4(),
                outbox_id,
                row["lease_epoch"],
                "dead_letter",
                "pending",
                "redrive",
                None,
                _clip(reason),
            )
        ],
    )[0]
    return DispatchOutcome(
        workspace_id=workspace_id,
        outbox_id=outbox_id,
        lease_epoch=row["lease_epoch"],
        previous_status="dead_letter",
        status="pending",
        updated=True,
        stale=False,
        outcome="redrive",
        event_id=event_id,
        delivery_attempt_count=row["delivery_attempt_count"],
        available_at=row["available_at"],
    )


def backoff_delay_seconds(
    attempt: int,
    *,
    base: float = BACKOFF_BASE_SECONDS,
    cap: float = BACKOFF_CAP_SECONDS,
) -> float:
    """``min(base * 2 ** (attempt - 1), cap)``；attempt 从 1 开始（首次失败等 base）。"""

    if attempt < 1:
        raise OutboxSetupError(f"attempt 必须 >= 1，收到 {attempt}")
    return min(base * (2 ** (attempt - 1)), cap)


# ---------------------------------------------------------------------------
# 发布侧：顺序观测器、Redis publisher、dispatcher
# ---------------------------------------------------------------------------
class Publisher(Protocol):
    def publish(self, envelope: PublishEnvelope) -> PublishReceipt: ...


class PublishObserver:
    """用**独立连接**观测"发布这一刻"的事实，作为顺序证据。

    ``inspect`` 只观测不判定；``observe`` 在任一不变量不成立时抛
    :class:`PublishOrderViolation`。所以"事务内 publish"必然当场失败，而不是事后靠
    日志推断。
    """

    _VISIBILITY_SQL: Final = (
        f"SELECT status, lease_epoch FROM {SCHEMA}.phase0_outbox "
        "WHERE workspace_id = %s AND outbox_id = %s"
    )
    _BACKEND_SQL: Final = "SELECT state, xact_start FROM pg_stat_activity WHERE pid = %s"

    def __init__(self, *, dsn: str | None = None) -> None:
        self._dsn = dsn or _read_phase0_dsn()
        self._conn: psycopg.Connection[Any] | None = None

    def _connection(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, autocommit=True)
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def inspect(
        self, delivery: ClaimedDelivery, *, dispatcher_pid: int | None = None
    ) -> PublishObservation:
        conn = self._connection()
        row = conn.execute(
            self._VISIBILITY_SQL, (delivery.workspace_id, delivery.outbox_id)
        ).fetchone()
        state: str | None = None
        in_transaction = False
        if dispatcher_pid is not None:
            activity = conn.execute(self._BACKEND_SQL, (dispatcher_pid,)).fetchone()
            if activity is not None:
                state = activity[0]
                in_transaction = activity[1] is not None
        return PublishObservation(
            outbox_id=delivery.outbox_id,
            expected_epoch=delivery.lease_epoch,
            visible=(
                row is not None and row[1] == delivery.lease_epoch and row[0] == "dispatching"
            ),
            observed_status=None if row is None else row[0],
            observed_epoch=None if row is None else row[1],
            dispatcher_state=state,
            dispatcher_in_transaction=in_transaction,
            observed_at=datetime.now(UTC),
        )

    def observe(
        self, delivery: ClaimedDelivery, *, dispatcher_pid: int | None = None
    ) -> PublishObservation:
        observation = self.inspect(delivery, dispatcher_pid=dispatcher_pid)
        if not observation.visible:
            raise PublishOrderViolation("row_not_visible_after_commit", observation)
        if observation.dispatcher_in_transaction:
            raise PublishOrderViolation("dispatcher_transaction_open", observation)
        return observation


class RedisPublisher:
    """把消息 ``LPUSH`` 进 T07 的 Redis 队列；失败一律转 ``PublishFailed``。

    ``PublishReceipt.transport_id`` 是 broker 侧传输关联（队列位置 + 随机串），与业务
    ``job_id`` 分离（P0-T07 集成影响：transport_id 不得占用业务 task_id）。
    """

    def __init__(
        self,
        client: redis.Redis | None = None,
        *,
        queue: str = REDIS_QUEUE_KEY,
    ) -> None:
        self._client = client or redis_client()
        self._queue = queue

    @property
    def queue(self) -> str:
        return self._queue

    def publish(self, envelope: PublishEnvelope) -> PublishReceipt:
        body = json.dumps(envelope.as_message(), ensure_ascii=False, sort_keys=True)
        try:
            position = self._client.lpush(self._queue, body)
        except redis.exceptions.RedisError as exc:  # 连接失败 / broker 拒绝
            raise PublishFailed(
                f"Redis 发布失败: {type(exc).__name__}: {exc}",
                error_code="BROKER_UNAVAILABLE",
            ) from exc
        return PublishReceipt(
            transport_id=f"redis:{self._queue}:{position}",
            queue=self._queue,
            published_at=datetime.now(UTC),
        )


class OutboxDispatcher:
    """派发器：claim(短事务) → 观测 → **事务外** publish → CAS 标记(新短事务)。

    只写 Outbox 与投递审计，从不 UPDATE ``phase0_jobs``（C-08 §3：Job 执行状态归
    JobService）。心跳/消费幂等/对账属 T08，不在这里提前实现。
    """

    def __init__(
        self,
        *,
        workspace_id: UUID,
        publisher: Publisher,
        dsn: str | None = None,
        batch_limit: int = 8,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        observer: PublishObserver | None = None,
    ) -> None:
        self._dsn = dsn or _read_phase0_dsn()
        self._workspace_id = workspace_id
        self._publisher = publisher
        self._batch_limit = batch_limit
        self._lease_seconds = lease_seconds
        self._observer = observer or PublishObserver(dsn=self._dsn)

    @property
    def observer(self) -> PublishObserver:
        return self._observer

    def close(self) -> None:
        self._observer.close()

    def run_once(self, *, now: datetime | None = None) -> DispatchReport:
        now = _require_aware(now or datetime.now(UTC), "now")
        conn = psycopg.connect(self._dsn)
        try:
            pid = conn.info.backend_pid
            started = time.monotonic()
            with conn.transaction():
                claimed = claim(
                    conn,
                    self._workspace_id,
                    limit=self._batch_limit,
                    now=now,
                    lease_seconds=self._lease_seconds,
                )
            claim_seconds = time.monotonic() - started

            if conn.info.transaction_status is not TransactionStatus.IDLE:
                raise OutboxSetupError("claim 事务结束后连接不在 IDLE：不允许开始发布")

            observations: list[PublishObservation] = []
            published: list[PublishEnvelope] = []
            outcomes: list[DispatchOutcome] = []
            failures: list[tuple[UUID, str]] = []
            for delivery in claimed:
                observation = self._observer.observe(delivery, dispatcher_pid=pid)
                observations.append(observation)
                envelope = delivery.envelope()
                try:
                    self._publisher.publish(envelope)
                except PublishFailed as exc:
                    failures.append((delivery.outbox_id, exc.error_code))
                    with conn.transaction():
                        outcomes.append(retry(conn, delivery, error_code=exc.error_code, now=now))
                else:
                    with conn.transaction():
                        outcomes.append(mark_dispatched(conn, delivery, now=now))
                    published.append(envelope)
            return DispatchReport(
                claimed=claimed,
                published=tuple(published),
                outcomes=tuple(outcomes),
                observations=tuple(observations),
                failures=tuple(failures),
                claim_seconds=claim_seconds,
                publish_backend_states=tuple(
                    observation.dispatcher_state or "" for observation in observations
                ),
                dispatcher_pid=pid,
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 查询计划（DoD：scan query plan）
# ---------------------------------------------------------------------------
def explain_claim(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    *,
    limit: int,
    now: datetime,
) -> Any:
    """``EXPLAIN (FORMAT JSON)`` 真实的 claim SELECT（不加 ANALYZE：不执行、不加锁）。"""

    row = conn.execute(
        "EXPLAIN (FORMAT JSON) " + _CLAIM_SELECT_SQL,
        {"workspace_id": workspace_id, "now": _require_aware(now, "now"), "limit": limit},
    ).fetchone()
    if row is None:  # pragma: no cover
        raise OutboxSetupError("EXPLAIN 没有返回计划")
    return row[0]


def iter_plan_nodes(plan: Any) -> Iterator[Mapping[str, Any]]:
    """深度遍历 EXPLAIN JSON，产出所有带 ``Node Type`` 的节点。"""

    if isinstance(plan, list | tuple):
        for item in plan:
            yield from iter_plan_nodes(item)
        return
    if not isinstance(plan, Mapping):
        return
    if "Node Type" in plan:
        yield plan
        for child in plan.get("Plans") or []:
            yield from iter_plan_nodes(child)
        return
    for value in plan.values():
        yield from iter_plan_nodes(value)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _LeaseKey:
    """只带 (workspace, outbox, epoch) 的最小句柄，供回收路径复用死信逻辑。"""

    workspace_id: UUID
    outbox_id: UUID
    lease_epoch: int


def _require_non_autocommit(conn: psycopg.Connection[Any], where: str) -> None:
    if conn.autocommit:
        raise OutboxSetupError(
            f"{where} 需要 autocommit=False 的连接（autocommit 下 SKIP LOCKED / CAS "
            "语句各自独立提交，lease 语义全部失效）"
        )


def _require_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise OutboxSetupError(f"limit 必须是 [1, 1000] 的整数，收到 {limit!r}")


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise OutboxSetupError(f"{name} 必须是 datetime，收到 {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise OutboxSetupError(f"{name} 必须带时区（推荐 datetime.now(UTC)）: {value!r}")
    return value.astimezone(UTC)


def _require_error_code(code: str) -> None:
    if not isinstance(code, str) or not 1 <= len(code) <= 64:
        raise OutboxSetupError(f"error_code 长度必须在 [1, 64]: {code!r}")


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:512]


def _insert_events(
    conn: psycopg.Connection[Any],
    rows: Iterable[tuple[Any, ...]],
) -> list[UUID]:
    payload = list(rows)
    if not payload:
        return []
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {SCHEMA}.phase0_outbox_delivery_events "
            "(workspace_id, event_id, outbox_id, lease_epoch, from_status, to_status, "
            "outcome, error_code, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            payload,
        )
    return [row[1] for row in payload]


def _record_stale(
    conn: psycopg.Connection[Any],
    delivery: ClaimedDelivery | _LeaseKey,
    *,
    attempted: str,
) -> DispatchOutcome:
    """旧 epoch 的 CAS 落空：读当前事实 + 写 stale 审计行，绝不静默成功。"""

    with conn.cursor(row_factory=dict_row) as cur:
        current = cur.execute(
            f"SELECT status, lease_epoch, delivery_attempt_count, available_at "
            f"FROM {SCHEMA}.phase0_outbox WHERE workspace_id = %s AND outbox_id = %s",
            (delivery.workspace_id, delivery.outbox_id),
        ).fetchone()
    event_id = _insert_events(
        conn,
        [
            (
                delivery.workspace_id,
                uuid4(),
                delivery.outbox_id,
                delivery.lease_epoch,
                None if current is None else current["status"],
                attempted,
                "stale",
                "STALE_LEASE_EPOCH",
                _clip(f"CAS 命中 0 行；期望 epoch={delivery.lease_epoch}，attempted={attempted}"),
            )
        ],
    )[0]
    return DispatchOutcome(
        workspace_id=delivery.workspace_id,
        outbox_id=delivery.outbox_id,
        lease_epoch=delivery.lease_epoch,
        previous_status=None if current is None else current["status"],
        status=None if current is None else current["status"],
        updated=False,
        stale=True,
        outcome="stale",
        event_id=event_id,
        delivery_attempt_count=0 if current is None else current["delivery_attempt_count"],
        available_at=None if current is None else current["available_at"],
    )


def _terminate_dead_letter(
    conn: psycopg.Connection[Any],
    delivery: ClaimedDelivery | _LeaseKey,
    *,
    attempts: int,
    error_code: str,
    now: datetime,
    detail: str | None,
) -> DispatchOutcome:
    """转 ``dead_letter``：保留原行、原因与次数（C-08 §6 "确定 poison / 耗尽"）。"""

    with conn.cursor(row_factory=dict_row) as cur:
        updated = cur.execute(
            f"UPDATE {SCHEMA}.phase0_outbox SET status = 'dead_letter', "
            "available_at = %(now)s, lease_expires_at = NULL, "
            "lease_epoch = lease_epoch + 1, last_error_code = %(error_code)s, "
            "row_version = row_version + 1 "
            "WHERE workspace_id = %(workspace_id)s AND outbox_id = %(outbox_id)s "
            "AND status = 'dispatching' AND lease_epoch = %(lease_epoch)s "
            "RETURNING lease_epoch",
            {
                "now": now,
                "error_code": error_code,
                "workspace_id": delivery.workspace_id,
                "outbox_id": delivery.outbox_id,
                "lease_epoch": delivery.lease_epoch,
            },
        ).fetchone()
    if updated is None:
        return _record_stale(conn, delivery, attempted="dead_letter")
    event_id = _insert_events(
        conn,
        [
            (
                delivery.workspace_id,
                uuid4(),
                delivery.outbox_id,
                updated["lease_epoch"],
                "dispatching",
                "dead_letter",
                "dead_letter",
                error_code,
                _clip(detail),
            )
        ],
    )[0]
    return DispatchOutcome(
        workspace_id=delivery.workspace_id,
        outbox_id=delivery.outbox_id,
        lease_epoch=updated["lease_epoch"],
        previous_status="dispatching",
        status="dead_letter",
        updated=True,
        stale=False,
        outcome="dead_letter",
        event_id=event_id,
        delivery_attempt_count=attempts,
        available_at=now,
    )
