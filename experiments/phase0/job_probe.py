"""P0-T08：作业消费幂等、死信与对账（真实 PostgreSQL + Redis Stream 探针）。

验收依据
--------
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T08「消费幂等、Job 对账与死信redrive」；
* ``docs/poc/07_Contract_Closure.md`` C-08 §6/§8（投递五态、死信、rewake 恢复、
  dispatched 只说明已发送）与 C-09 §8（旧 worker 的 epoch/generation 失配必须
  STALE，不允许复活终态作业）；
* ``docs/poc/05_Phase0_Acceptance.md`` PoC-02 强制 PASS 项（100 Job×10 消息、
  断 Redis / ACK 丢失 / 死信恢复可复验）；
* ``docs/tdd/01_System_Architecture.md`` §6、``docs/tdd/06_Knowledge_Ingestion.md`` §5。

状态机来源与偏离（Lead 已授权「复制改造」，原文件只读且有改动保护）
------------------------------------------------------------------
本模块的投递状态机 **复制自** ``experiments/phase0/db/migrations/003_jobs_outbox.sql``
（P0-T07，schema ``p0_t07``），沿用同一套五态枚举与同一条转移矩阵：

    pending / dispatching / retry_wait / dispatched / dead_letter

四类**改造**（不新增状态、不改枚举名，故仍是一套状态机）：

1. 语义落到**消费侧**：``dispatching`` = 消息已发布但尚未 ACK（reconciler 要修的
   「已发布但长期未确认」正是这一格）；``dispatched`` = 已消费且已 ACK；
   ``pending`` = 待派发；``retry_wait`` = 退避等待；``dead_letter`` = 死信。
2. 身份列由 ``outbox_id`` 改为 ``delivery_id``，表名 ``phase0_deliveries``。
3. 新增 ``acked_at`` / ``repair_count`` 两列（对账修复计数，C-08 §8 rewake）。
4. **DDL 写在代码里**（``SCHEMA_SQL`` + ``ensure_schema``）：本任务的写权允许清单
   只有三个文件，不能再建迁移 004，所以把 003 的 §1–§8 适配进 ``p0_t08`` 的
   ``CREATE SCHEMA IF NOT EXISTS`` 幂等脚本。生产落地时应回写成共享 schema 的
   增量迁移（DDL 不变）；该偏离在交付报告中显式记录，不静默。
   触发器（状态转移矩阵 / 身份列不可变 / epoch 不回退 / row_version 严格 +1 /
   审计表只追加）与 003 保持同样的强制力度。

执行状态不是第二套状态机
------------------------
Job 的执行事实只有两处：``phase0_job_results``（按 ``(job_id, execution_generation)``
追加写，终态永久保留）与 ``phase0_ingest_targets.applied_version``（业务事实）。
它们**没有状态列**，不能被"转移"，只被 CAS 写入；``record_result`` 遇到旧
generation 直接 ``STALE_EXECUTION_EPOCH``，终态作业重复 redrive 不复活（C-09 §7）。

celery_app 入口
---------------
``celery_app`` 是本探针的**门面**：任务名、broker URL、stream/group 与将来 Celery
接入时一致，提供 ``.task`` 装饰器与 ``.send_task``。实验环境没有安装 celery
（``experiments/phase0`` 依赖清单里没有，且本任务禁用 ``--with``——它会把
``sys.prefix`` 改掉、破坏 T02 的环境隔离断言），所以本文件不 import celery；
换入真实 Celery 时只需替换 ``celery_app`` 的构造，调用方（worker/compose 启动
脚本）不用改。真实 Celery broker 行为属于 NOT_RUN，见报告。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import psycopg
import redis
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "BROKER_UNAVAILABLE",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_DELIVERY_ATTEMPTS",
    "DEFAULT_UNACKED_GRACE_SECONDS",
    "DELIVERY_STATES",
    "GROUP",
    "REDIS_DB",
    "SCHEMA",
    "STALE_EXECUTION_EPOCH",
    "STALE_LEASE_EPOCH",
    "STREAM_KEY",
    "TERMINAL_JOB_NO_OP",
    "BrokerUnavailable",
    "ClaimedDelivery",
    "ConsumeOutcome",
    "DeliveryMessage",
    "EnqueueRequest",
    "EnqueuedJob",
    "JobProbeError",
    "RecordOutcome",
    "TransportReceipt",
    "ack_message",
    "backoff_delay_seconds",
    "celery_app",
    "claim",
    "connect",
    "consume_message",
    "enqueue",
    "ensure_schema",
    "ensure_stream",
    "ensure_transport_slot",
    "mark_published",
    "publish",
    "read_dsn",
    "read_messages",
    "record_result",
    "redis_client",
    "requeue",
    "reset_schema",
    "reset_stream",
    "retry",
    "schema_digest",
    "utcnow",
]

# ---------------------------------------------------------------------------
# 常量：与 003（P0-T07）保持同一套枚举与同一张转移矩阵
# ---------------------------------------------------------------------------

SCHEMA: Final = "p0_t08"

#: 与 ``003_jobs_outbox.sql`` §4 完全一致的五态枚举（顺序也一致）。
DELIVERY_STATES: Final[tuple[str, ...]] = (
    "pending",
    "dispatching",
    "retry_wait",
    "dispatched",
    "dead_letter",
)

#: 与 003 §6 的 VALUES 转移矩阵逐条对应（复制改造，不新增边）。
ALLOWED_TRANSITIONS: Final[tuple[tuple[str, str], ...]] = (
    ("pending", "dispatching"),
    ("retry_wait", "dispatching"),
    ("dispatching", "dispatched"),
    ("dispatching", "retry_wait"),
    ("dispatching", "dead_letter"),
    ("retry_wait", "dead_letter"),
    ("dead_letter", "pending"),
)

REDIS_DB: Final = 5
STREAM_KEY: Final = "p0_t08:deliveries"
GROUP: Final = "p0_t08-workers"

DEFAULT_MAX_DELIVERY_ATTEMPTS: Final = 3
DEFAULT_LEASE_SECONDS: Final = 30.0
DEFAULT_UNACKED_GRACE_SECONDS: Final = 10.0
BACKOFF_BASE_SECONDS: Final = 1.0
BACKOFF_CAP_SECONDS: Final = 30.0

#: 明确错误码（不接受"静默成功"）：测试逐条断言这些字符串。
STALE_EXECUTION_EPOCH: Final = "STALE_EXECUTION_EPOCH"
STALE_LEASE_EPOCH: Final = "STALE_LEASE_EPOCH"
BROKER_UNAVAILABLE: Final = "BROKER_UNAVAILABLE"
TERMINAL_JOB_NO_OP: Final = "TERMINAL_JOB_NO_OP"
RECONCILE_UNACKED: Final = "RECONCILE_UNACKED"
RETRY_EXHAUSTED: Final = "RETRY_EXHAUSTED"

_MIGRATION_SOURCE: Final = "experiments/phase0/db/migrations/003_jobs_outbox.sql"

_ENV_REDIS_HOST_RE: Final = re.compile(r"^PHASE0_REDIS_HOST=(\S+)$", re.MULTILINE)
_ENV_REDIS_PORT_RE: Final = re.compile(r"^PHASE0_REDIS_HOST_PORT=(\d+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# schema（复制改造自 003；幂等、单事务、无 BEGIN/COMMIT）
# ---------------------------------------------------------------------------

SCHEMA_SQL: Final[str] = f"""
-- DDL 归属 P0-T08。来源：{_MIGRATION_SOURCE}（P0-T07，schema p0_t07）§1–§8，
-- 复制改造为消费侧：五态与转移矩阵一字不改，身份列 outbox_id→delivery_id，
-- 新增 acked_at / repair_count。生产落地应回写为共享 schema 的增量迁移。
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_ingest_targets (
    workspace_id       uuid NOT NULL,
    target_id          uuid NOT NULL,
    name               text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    desired_version    bigint NOT NULL DEFAULT 1 CHECK (desired_version >= 1),
    applied_version    bigint NOT NULL DEFAULT 0 CHECK (applied_version >= 0),
    applied_generation bigint NOT NULL DEFAULT 0 CHECK (applied_generation >= 0),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, target_id)
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_ingest_jobs (
    workspace_id          uuid NOT NULL,
    job_id                uuid NOT NULL,
    target_id             uuid NOT NULL,
    kind                  text NOT NULL CHECK (kind IN ('fetch', 'ingest')),
    dedup_key             text NOT NULL CHECK (length(dedup_key) BETWEEN 1 AND 256),
    -- C-09：同一 job_id 内的执行代数；旧 generation 的 worker 只能拿到 stale。
    execution_generation  bigint NOT NULL DEFAULT 1 CHECK (execution_generation >= 1),
    retry_of_job_id       uuid,
    retry_sequence        integer NOT NULL DEFAULT 0 CHECK (retry_sequence >= 0),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    row_version           bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, job_id),
    CONSTRAINT phase0_ingest_jobs_dedup_key UNIQUE (workspace_id, kind, dedup_key),
    CONSTRAINT phase0_ingest_jobs_retry_key UNIQUE (workspace_id, retry_of_job_id,
                                                    retry_sequence),
    CONSTRAINT phase0_ingest_jobs_target_fk FOREIGN KEY (workspace_id, target_id)
        REFERENCES {SCHEMA}.phase0_ingest_targets (workspace_id, target_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_deliveries (
    workspace_id           uuid NOT NULL,
    delivery_id            uuid NOT NULL,
    job_id                 uuid NOT NULL,
    operation              text NOT NULL CHECK (operation ~ '^[a-z][a-z0-9_.-]{{0,63}}$'),
    dedup_key              text NOT NULL CHECK (length(dedup_key) BETWEEN 1 AND 256),
    payload_schema_version integer NOT NULL DEFAULT 1 CHECK (payload_schema_version >= 1),
    execution_generation   bigint NOT NULL DEFAULT 1 CHECK (execution_generation >= 1),
    payload_version        bigint NOT NULL DEFAULT 1 CHECK (payload_version >= 1),
    -- 五态枚举与 003 完全一致（复制改造，不改名）。
    status                 text NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'dispatching', 'retry_wait',
                                             'dispatched', 'dead_letter')),
    lease_epoch            bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    lease_expires_at       timestamptz,
    delivery_attempt_count integer NOT NULL DEFAULT 0
                           CHECK (delivery_attempt_count >= 0),
    max_delivery_attempts  integer NOT NULL DEFAULT {DEFAULT_MAX_DELIVERY_ATTEMPTS}
                           CHECK (max_delivery_attempts BETWEEN 1 AND 100),
    repair_count           integer NOT NULL DEFAULT 0 CHECK (repair_count >= 0),
    redrive_count          integer NOT NULL DEFAULT 0 CHECK (redrive_count >= 0),
    last_error_code        text CHECK (last_error_code IS NULL
                                      OR length(last_error_code) BETWEEN 1 AND 64),
    available_at           timestamptz NOT NULL DEFAULT now(),
    last_attempt_at        timestamptz,
    published_at           timestamptz,
    acked_at               timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    row_version            bigint NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (workspace_id, delivery_id),
    CONSTRAINT phase0_deliveries_dedup_key UNIQUE (workspace_id, operation, dedup_key),
    CONSTRAINT phase0_deliveries_job_key UNIQUE (workspace_id, job_id, operation),
    CONSTRAINT phase0_deliveries_job_fk FOREIGN KEY (workspace_id, job_id)
        REFERENCES {SCHEMA}.phase0_ingest_jobs (workspace_id, job_id) ON DELETE RESTRICT,
    -- 与 003 相同的 lease 不变式：只有 dispatching 持有租约。
    CONSTRAINT phase0_deliveries_lease_check CHECK (
        (status = 'dispatching' AND lease_expires_at IS NOT NULL)
        OR (status <> 'dispatching' AND lease_expires_at IS NULL)
    ),
    CONSTRAINT phase0_deliveries_dead_letter_check CHECK (
        status <> 'dead_letter' OR last_error_code IS NOT NULL
    ),
    -- 消费侧新增：dispatched 必须同时有发布与 ACK 时刻，杜绝"伪 ACK"。
    CONSTRAINT phase0_deliveries_dispatched_check CHECK (
        status <> 'dispatched' OR (acked_at IS NOT NULL AND published_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS phase0_deliveries_claim_idx
    ON {SCHEMA}.phase0_deliveries (workspace_id, available_at, created_at, delivery_id)
    WHERE status IN ('pending', 'retry_wait');

CREATE INDEX IF NOT EXISTS phase0_deliveries_lease_idx
    ON {SCHEMA}.phase0_deliveries (workspace_id, lease_expires_at)
    WHERE status = 'dispatching';

-- 幂等收据：一个 Job 只允许一次业务效果（C-09「每 Job 一业务结果」）。
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_consumption_receipts (
    workspace_id        uuid NOT NULL,
    receipt_id          uuid NOT NULL,
    job_id              uuid NOT NULL,
    delivery_id         uuid NOT NULL,
    dedup_key           text NOT NULL CHECK (length(dedup_key) BETWEEN 1 AND 256),
    execution_generation bigint NOT NULL CHECK (execution_generation >= 1),
    consumer_id         text NOT NULL CHECK (length(consumer_id) BETWEEN 1 AND 64),
    applied_version     bigint NOT NULL CHECK (applied_version >= 0),
    recorded_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, receipt_id),
    CONSTRAINT phase0_consumption_receipts_job_key UNIQUE (workspace_id, job_id),
    CONSTRAINT phase0_consumption_receipts_job_fk FOREIGN KEY (workspace_id, job_id)
        REFERENCES {SCHEMA}.phase0_ingest_jobs (workspace_id, job_id) ON DELETE RESTRICT
);

-- 执行事实（不是状态机）：按 generation 追加写，终态永久保留（C-09 §7）。
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_job_results (
    workspace_id         uuid NOT NULL,
    job_id               uuid NOT NULL,
    execution_generation bigint NOT NULL CHECK (execution_generation >= 1),
    outcome              text NOT NULL CHECK (outcome IN ('succeeded', 'failed', 'dead')),
    applied_version      bigint CHECK (applied_version IS NULL OR applied_version >= 0),
    error_code           text CHECK (error_code IS NULL
                                     OR length(error_code) BETWEEN 1 AND 64),
    recorded_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, job_id, execution_generation),
    CONSTRAINT phase0_job_results_job_fk FOREIGN KEY (workspace_id, job_id)
        REFERENCES {SCHEMA}.phase0_ingest_jobs (workspace_id, job_id) ON DELETE RESTRICT
);

-- 审计（追加写，触发器拒绝 UPDATE/DELETE）：重复、stale、重试、死信、修复都留痕。
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_delivery_events (
    workspace_id  uuid NOT NULL,
    event_id      uuid NOT NULL,
    event_seq     bigint GENERATED ALWAYS AS IDENTITY,
    delivery_id   uuid NOT NULL,
    job_id        uuid NOT NULL,
    lease_epoch   bigint NOT NULL CHECK (lease_epoch >= 0),
    from_status   text CHECK (from_status IS NULL
                              OR from_status IN ('pending', 'dispatching', 'retry_wait',
                                                 'dispatched', 'dead_letter')),
    to_status     text NOT NULL
                  CHECK (to_status IN ('pending', 'dispatching', 'retry_wait',
                                       'dispatched', 'dead_letter')),
    outcome       text NOT NULL
                  CHECK (outcome IN ('claimed', 'applied', 'duplicate', 'stale',
                                     'acked', 'requeued', 'retry_wait', 'dead_letter',
                                     'repaired', 'redrive')),
    error_code    text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    detail        text CHECK (detail IS NULL OR length(detail) <= 512),
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, event_id),
    CONSTRAINT phase0_delivery_events_seq_key UNIQUE (event_seq),
    CONSTRAINT phase0_delivery_events_delivery_fk FOREIGN KEY (workspace_id, delivery_id)
        REFERENCES {SCHEMA}.phase0_deliveries (workspace_id, delivery_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS phase0_delivery_events_delivery_idx
    ON {SCHEMA}.phase0_delivery_events (workspace_id, delivery_id, event_seq);

-- §6 状态转移（复制 003 的同名约束力度）
CREATE OR REPLACE FUNCTION {SCHEMA}.phase0_delivery_enforce_transition()
RETURNS trigger AS $$
DECLARE
    is_allowed boolean;
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.delivery_id IS DISTINCT FROM OLD.delivery_id
       OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.operation IS DISTINCT FROM OLD.operation
       OR NEW.dedup_key IS DISTINCT FROM OLD.dedup_key THEN
        RAISE EXCEPTION '投递身份列不可变（workspace/delivery/job/operation/dedup_key）'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.lease_epoch < OLD.lease_epoch THEN
        RAISE EXCEPTION 'lease_epoch 不得回退：% -> %', OLD.lease_epoch, NEW.lease_epoch
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.row_version <> OLD.row_version + 1 THEN
        RAISE EXCEPTION 'row_version 必须严格 +1：% -> %', OLD.row_version, NEW.row_version
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM (VALUES
            ('pending', 'dispatching'),
            ('retry_wait', 'dispatching'),
            ('dispatching', 'dispatched'),
            ('dispatching', 'retry_wait'),
            ('dispatching', 'dead_letter'),
            ('retry_wait', 'dead_letter'),
            ('dead_letter', 'pending')
        ) AS allowed(from_status, to_status)
        WHERE allowed.from_status = OLD.status AND allowed.to_status = NEW.status
    ) INTO is_allowed;
    IF NOT is_allowed THEN
        RAISE EXCEPTION '非法投递状态转移 % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'dispatching' AND NEW.lease_epoch <> OLD.lease_epoch + 1 THEN
        RAISE EXCEPTION 'claim 必须令 lease_epoch + 1：% -> %',
            OLD.lease_epoch, NEW.lease_epoch USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS phase0_deliveries_transition ON {SCHEMA}.phase0_deliveries;
CREATE TRIGGER phase0_deliveries_transition
    BEFORE UPDATE ON {SCHEMA}.phase0_deliveries
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.phase0_delivery_enforce_transition();

-- §7 审计只追加
CREATE OR REPLACE FUNCTION {SCHEMA}.phase0_reject_event_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '投递审计表只允许追加写，拒绝 %', TG_OP
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS phase0_delivery_events_append_only
    ON {SCHEMA}.phase0_delivery_events;
CREATE TRIGGER phase0_delivery_events_append_only
    BEFORE UPDATE OR DELETE ON {SCHEMA}.phase0_delivery_events
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.phase0_reject_event_mutation();

-- §8 结果写入门禁：旧 generation 的 worker 在 DB 层就被拒（C-09 §8 STALE_ATTEMPT）
CREATE OR REPLACE FUNCTION {SCHEMA}.phase0_job_results_guard()
RETURNS trigger AS $$
DECLARE
    current_generation bigint;
BEGIN
    SELECT execution_generation INTO current_generation
      FROM {SCHEMA}.phase0_ingest_jobs
     WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id;
    IF current_generation IS NULL THEN
        RAISE EXCEPTION '结果没有对应 Job（workspace=% job=%）',
            NEW.workspace_id, NEW.job_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF NEW.execution_generation <> current_generation THEN
        RAISE EXCEPTION 'STALE_EXECUTION_EPOCH generation=% 当前=%',
            NEW.execution_generation, current_generation USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS phase0_job_results_guard ON {SCHEMA}.phase0_job_results;
CREATE TRIGGER phase0_job_results_guard
    BEFORE INSERT ON {SCHEMA}.phase0_job_results
    FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.phase0_job_results_guard();
"""


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class JobProbeError(Exception):
    """探针的统一错误基类：带稳定 ``code``，测试据此断言而不是匹配自然语言。"""

    code: str = "JOB_PROBE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class BrokerUnavailable(JobProbeError):
    """Redis 不可用：必须显式失败，不允许静默丢消息（验收 5）。"""

    code = BROKER_UNAVAILABLE


# ---------------------------------------------------------------------------
# 数据载体
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnqueueRequest:
    workspace_id: UUID
    target_id: UUID
    job_id: UUID
    dedup_key: str
    target_name: str = "phase0-target"
    kind: str = "ingest"
    operation: str = "ingest.execute"
    payload_version: int = 1
    delivery_id: UUID | None = None
    max_delivery_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS
    execution_generation: int = 1

    def resolved_delivery_id(self) -> UUID:
        return self.delivery_id if self.delivery_id is not None else uuid4()


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    workspace_id: UUID
    job_id: UUID
    delivery_id: UUID
    dedup_key: str
    created: bool
    delivery_created: bool
    status: str
    execution_generation: int


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    workspace_id: UUID
    delivery_id: UUID
    job_id: UUID
    operation: str
    dedup_key: str
    payload_schema_version: int
    payload_version: int
    execution_generation: int
    lease_epoch: int
    delivery_attempt_count: int
    max_delivery_attempts: int

    def message_fields(self) -> dict[str, str]:
        return {
            "workspace_id": str(self.workspace_id),
            "delivery_id": str(self.delivery_id),
            "job_id": str(self.job_id),
            "operation": self.operation,
            "dedup_key": self.dedup_key,
            "execution_generation": str(self.execution_generation),
            "lease_epoch": str(self.lease_epoch),
            "attempt": str(self.delivery_attempt_count),
            "payload_version": str(self.payload_version),
        }


@dataclass(frozen=True, slots=True)
class DeliveryMessage:
    stream_id: str
    fields: Mapping[str, str]
    workspace_id: UUID
    delivery_id: UUID
    job_id: UUID
    lease_epoch: int
    execution_generation: int


@dataclass(frozen=True, slots=True)
class TransportReceipt:
    stream_id: str
    stream: str


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    recorded: bool
    outcome: str
    error_code: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ConsumeOutcome:
    delivery_id: UUID
    job_id: UUID
    outcome: str
    business_applied: bool
    duplicate: bool
    stale: bool
    error_code: str | None
    event_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    workspace_id: UUID
    delivery_id: UUID
    previous_status: str
    status: str
    updated: bool
    stale: bool
    outcome: str
    lease_epoch: int
    delivery_attempt_count: int
    available_at: datetime | None
    error_code: str | None = None


# ---------------------------------------------------------------------------
# 连接 / schema 安装
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC)


def read_dsn() -> str:
    """复用 T03 的 DSN 解析（只读 import，不改 scope.py）。"""

    from scope import read_dsn as _read_phase0_dsn

    return _read_phase0_dsn()


def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn or read_dsn(), autocommit=autocommit, row_factory=dict_row)


def schema_digest() -> str:
    return hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()


def ensure_schema(conn: psycopg.Connection[Any]) -> str:
    """幂等安装 p0_t08（单事务）。返回 DDL 的 sha256，便于证据串。"""

    if conn.autocommit:
        raise JobProbeError("ensure_schema 需要 autocommit=False 的连接")
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        raise JobProbeError("ensure_schema 需要干净事务边界（IDLE）")
    conn.execute(SCHEMA_SQL)
    conn.commit()
    return schema_digest()


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    """删除本任务 schema；只碰 p0_t08。"""

    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))
    conn.commit()


# ---------------------------------------------------------------------------
# Redis（DB 5 独占槽；只删自己的键，绝不 FLUSHALL）
# ---------------------------------------------------------------------------


def _env_file() -> Path:
    return Path(__file__).resolve().parent / ".env.example"


def read_redis_url(env_file: Path | None = None) -> str:
    base = os.environ.get("PHASE0_T08_REDIS_URL") or os.environ.get("PHASE0_REDIS_URL")
    if base is None:
        path = env_file if env_file is not None else _env_file()
        text = path.read_text(encoding="utf-8")
        host = _ENV_REDIS_HOST_RE.search(text)
        port = _ENV_REDIS_PORT_RE.search(text)
        if host is None or port is None:
            raise JobProbeError(f"{path} 缺少 PHASE0_REDIS_HOST / PHASE0_REDIS_HOST_PORT")
        base = f"redis://{host.group(1).strip()}:{port.group(1).strip()}/0"
    head, _, _ = base.rpartition("/")
    return f"{head}/{REDIS_DB}"


def redis_client(url: str | None = None) -> redis.Redis:
    return redis.Redis.from_url(
        url or read_redis_url(),
        decode_responses=True,
        socket_connect_timeout=1.0,
        # 读超时放宽到 30s：1000 条消息连续 XACK 时 2s 会被测试机负载打成假超时，
        # 那会把"broker 不可用"误判成故障（连接失败仍有 1s 的连接超时兜底）。
        socket_timeout=30.0,
    )


def ensure_transport_slot(client: redis.Redis) -> int:
    """拒绝非 DB 5 连接：别的波次槽不能被本任务 flush。"""

    db = client.connection_pool.connection_kwargs.get("db")
    if db != REDIS_DB:
        raise JobProbeError(
            f"T08 只允许操作 Redis DB {REDIS_DB}，当前 db={db!r}",
            code="WRONG_REDIS_SLOT",
        )
    return db


def ensure_stream(client: redis.Redis) -> None:
    ensure_transport_slot(client)
    try:
        client.xgroup_create(STREAM_KEY, GROUP, id="0", mkstream=True)
    except redis.exceptions.ResponseError as exc:  # BUSYGROUP：组已存在
        if "BUSYGROUP" not in str(exc):
            raise BrokerUnavailable(f"无法创建消费组：{exc}") from exc


def reset_stream(client: redis.Redis) -> None:
    """只删除本任务的 stream 键（不 flushdb、不 FLUSHALL）。"""

    ensure_transport_slot(client)
    client.delete(STREAM_KEY)


def _broker_guard(func: Callable[..., Any]) -> Callable[..., Any]:
    """把 redis 连接错误统一映射成 BrokerUnavailable（显式失败）。"""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except redis.exceptions.RedisError as exc:
            raise BrokerUnavailable(f"broker 不可用：{exc}") from exc

    return wrapper


# ---------------------------------------------------------------------------
# 生产：业务 + Job + 投递登记（调用方保证同一事务）
# ---------------------------------------------------------------------------


def enqueue(conn: psycopg.Connection[Any], request: EnqueueRequest) -> EnqueuedJob:
    """在同一事务里登记 target / job / delivery；重复登记保持身份不变。"""

    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_ingest_targets "
        "(workspace_id, target_id, name, desired_version) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (workspace_id, target_id) DO NOTHING",
        (request.workspace_id, request.target_id, request.target_name,
         request.payload_version),
    )
    job_row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_ingest_jobs "
        "(workspace_id, job_id, target_id, kind, dedup_key, execution_generation) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (workspace_id, kind, dedup_key) DO NOTHING RETURNING job_id",
        (request.workspace_id, request.job_id, request.target_id, request.kind,
         request.dedup_key, request.execution_generation),
    ).fetchone()
    job_created = job_row is not None
    job_id = request.job_id
    if not job_created:
        existing = conn.execute(
            f"SELECT job_id FROM {SCHEMA}.phase0_ingest_jobs "
            "WHERE workspace_id = %s AND kind = %s AND dedup_key = %s",
            (request.workspace_id, request.kind, request.dedup_key),
        ).fetchone()
        if existing is None:  # pragma: no cover - UNIQUE 冲突后必然可查
            raise JobProbeError("dedup 冲突后读不到既有 Job")
        job_id = existing["job_id"]

    delivery_row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_deliveries "
        "(workspace_id, delivery_id, job_id, operation, dedup_key, payload_schema_version, "
        " execution_generation, payload_version, max_delivery_attempts) "
        "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s) "
        "ON CONFLICT (workspace_id, operation, dedup_key) DO NOTHING "
        "RETURNING delivery_id",
        (request.workspace_id, request.resolved_delivery_id(), job_id, request.operation,
         request.dedup_key, request.execution_generation, request.payload_version,
         request.max_delivery_attempts),
    ).fetchone()
    delivery_created = delivery_row is not None
    if delivery_created:
        delivery_id = delivery_row["delivery_id"]
    else:
        existing = conn.execute(
            f"SELECT delivery_id FROM {SCHEMA}.phase0_deliveries "
            "WHERE workspace_id = %s AND operation = %s AND dedup_key = %s",
            (request.workspace_id, request.operation, request.dedup_key),
        ).fetchone()
        if existing is None:  # pragma: no cover
            raise JobProbeError("dedup 冲突后读不到既有 delivery")
        delivery_id = existing["delivery_id"]

    row = conn.execute(
        f"SELECT status, execution_generation FROM {SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s AND delivery_id = %s",
        (request.workspace_id, delivery_id),
    ).fetchone()
    return EnqueuedJob(
        workspace_id=request.workspace_id,
        job_id=job_id,
        delivery_id=delivery_id,
        dedup_key=request.dedup_key,
        created=job_created,
        delivery_created=delivery_created,
        status=row["status"],
        execution_generation=row["execution_generation"],
    )


# ---------------------------------------------------------------------------
# 领取：SELECT ... FOR UPDATE SKIP LOCKED + lease（与 003 同一写法）
# ---------------------------------------------------------------------------


def claim(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    *,
    limit: int = 10,
    now: datetime | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> list[ClaimedDelivery]:
    if limit < 1:
        raise JobProbeError("limit 必须 >= 1")
    moment = now or utcnow()
    rows = conn.execute(
        f"SELECT delivery_id, job_id, operation, dedup_key, payload_schema_version, "
        f"       payload_version, execution_generation, status, lease_epoch, "
        f"       delivery_attempt_count, max_delivery_attempts "
        f"FROM {SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s AND status IN ('pending', 'retry_wait') "
        "  AND available_at <= %s "
        "ORDER BY available_at, created_at, delivery_id "
        "LIMIT %s FOR UPDATE SKIP LOCKED",
        (workspace_id, moment, limit),
    ).fetchall()

    claimed: list[ClaimedDelivery] = []
    for row in rows:
        epoch = row["lease_epoch"] + 1
        attempt = row["delivery_attempt_count"] + 1
        updated = conn.execute(
            f"UPDATE {SCHEMA}.phase0_deliveries "
            "SET status = 'dispatching', lease_epoch = %s, delivery_attempt_count = %s, "
            "    lease_expires_at = %s, last_attempt_at = %s, "
            "    row_version = row_version + 1 "
            "WHERE workspace_id = %s AND delivery_id = %s "
            "RETURNING delivery_id, lease_epoch, delivery_attempt_count",
            (epoch, attempt, moment + timedelta(seconds=lease_seconds), moment,
             workspace_id, row["delivery_id"]),
        ).fetchone()
        if updated is None:  # pragma: no cover - 行被锁住时不至于消失
            continue
        _insert_event(
            conn,
            workspace_id=workspace_id,
            delivery_id=row["delivery_id"],
            job_id=row["job_id"],
            lease_epoch=epoch,
            from_status=row["status"],
            to_status="dispatching",
            outcome="claimed",
            detail=f"attempt={attempt}",
        )
        claimed.append(
            ClaimedDelivery(
                workspace_id=workspace_id,
                delivery_id=row["delivery_id"],
                job_id=row["job_id"],
                operation=row["operation"],
                dedup_key=row["dedup_key"],
                payload_schema_version=row["payload_schema_version"],
                payload_version=row["payload_version"],
                execution_generation=row["execution_generation"],
                lease_epoch=epoch,
                delivery_attempt_count=attempt,
                max_delivery_attempts=row["max_delivery_attempts"],
            )
        )
    return claimed


# ---------------------------------------------------------------------------
# 传输：Redis Stream（publish 必须在事务提交之后调用）
# ---------------------------------------------------------------------------


def publish(client: redis.Redis, delivery: ClaimedDelivery) -> TransportReceipt:
    ensure_transport_slot(client)
    stream_id = _broker_guard(client.xadd)(
        STREAM_KEY,
        dict(delivery.message_fields()),
        maxlen=None,
    )
    return TransportReceipt(stream_id=str(stream_id), stream=STREAM_KEY)


def read_messages(
    client: redis.Redis,
    *,
    consumer: str,
    count: int = 10,
    block_ms: int = 0,
) -> list[DeliveryMessage]:
    ensure_transport_slot(client)
    ensure_stream(client)
    batch = _broker_guard(client.xreadgroup)(
        GROUP, consumer, {STREAM_KEY: ">"}, count=count, block=block_ms or None
    )
    messages: list[DeliveryMessage] = []
    for _stream, entries in batch or []:
        for stream_id, fields in entries:
            messages.append(
                DeliveryMessage(
                    stream_id=str(stream_id),
                    fields=fields,
                    workspace_id=UUID(fields["workspace_id"]),
                    delivery_id=UUID(fields["delivery_id"]),
                    job_id=UUID(fields["job_id"]),
                    lease_epoch=int(fields["lease_epoch"]),
                    execution_generation=int(fields["execution_generation"]),
                )
            )
    return messages


def ack_message(client: redis.Redis, stream_id: str) -> int:
    ensure_transport_slot(client)
    return int(_broker_guard(client.xack)(STREAM_KEY, GROUP, stream_id))


def pending_count(client: redis.Redis) -> int:
    """未 ACK（PEL）条数：reconciler 的"已发布但未确认"在 broker 侧的对照证据。"""

    ensure_transport_slot(client)
    groups = _broker_guard(client.xpending)(STREAM_KEY, GROUP)
    if not groups:
        return 0
    if isinstance(groups, dict):
        return int(groups.get("pending", 0))
    return int(groups[0])  # pragma: no cover - redis-py 返回 dict


def requeue(conn: psycopg.Connection[Any], delivery: ClaimedDelivery, *,
            error_code: str = BROKER_UNAVAILABLE, now: datetime | None = None
            ) -> DeliveryOutcome:
    """broker 失败：投递回到 retry_wait（显式失败，绝不静默丢）。"""

    moment = now or utcnow()
    return retry(conn, delivery, error_code=error_code, now=moment)


def mark_published(
    conn: psycopg.Connection[Any],
    delivery: ClaimedDelivery,
    *,
    now: datetime | None = None,
) -> DeliveryOutcome:
    """CAS 记录"已发到 broker"（仍处 dispatching，等待 ACK）。"""

    moment = now or utcnow()
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_deliveries "
        "SET published_at = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND delivery_id = %s "
        "  AND status = 'dispatching' AND lease_epoch = %s "
        "RETURNING status, lease_epoch, delivery_attempt_count, available_at",
        (moment, delivery.workspace_id, delivery.delivery_id, delivery.lease_epoch),
    ).fetchone()
    if row is None:
        _record_stale(conn, delivery, attempted="mark_published")
        return DeliveryOutcome(
            workspace_id=delivery.workspace_id, delivery_id=delivery.delivery_id,
            previous_status="dispatching", status="dispatching", updated=False,
            stale=True, outcome="stale", lease_epoch=delivery.lease_epoch,
            delivery_attempt_count=delivery.delivery_attempt_count,
            available_at=None, error_code=STALE_LEASE_EPOCH,
        )
    return DeliveryOutcome(
        workspace_id=delivery.workspace_id, delivery_id=delivery.delivery_id,
        previous_status="dispatching", status=row["status"], updated=True, stale=False,
        outcome="published", lease_epoch=row["lease_epoch"],
        delivery_attempt_count=row["delivery_attempt_count"],
        available_at=row["available_at"],
    )


# ---------------------------------------------------------------------------
# 结果写入（CAS by generation）+ 消费主流程
# ---------------------------------------------------------------------------


def record_result(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    job_id: UUID,
    *,
    generation: int,
    outcome: str,
    applied_version: int | None = None,
    error_code: str | None = None,
) -> RecordOutcome:
    """按 generation 追加执行事实；旧 generation 在 DB 层被拒（触发器）。"""

    try:
        with conn.transaction():
            row = conn.execute(
                f"INSERT INTO {SCHEMA}.phase0_job_results "
                "(workspace_id, job_id, execution_generation, outcome, applied_version, "
                " error_code) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (workspace_id, job_id, execution_generation) DO NOTHING "
                "RETURNING execution_generation",
                (workspace_id, job_id, generation, outcome, applied_version, error_code),
            ).fetchone()
    except psycopg.errors.CheckViolation as exc:
        return RecordOutcome(recorded=False, outcome="stale",
                             error_code=STALE_EXECUTION_EPOCH, detail=str(exc).strip())
    if row is None:
        return RecordOutcome(recorded=False, outcome="duplicate", detail="同一 generation 已有结果")
    return RecordOutcome(recorded=True, outcome=outcome)


def _current_generation(
    conn: psycopg.Connection[Any], workspace_id: UUID, job_id: UUID
) -> int | None:
    row = conn.execute(
        f"SELECT execution_generation FROM {SCHEMA}.phase0_ingest_jobs "
        "WHERE workspace_id = %s AND job_id = %s",
        (workspace_id, job_id),
    ).fetchone()
    return None if row is None else int(row["execution_generation"])


def consume_message(
    conn: psycopg.Connection[Any],
    message: DeliveryMessage,
    *,
    consumer_id: str,
    broker: redis.Redis,
    now: datetime | None = None,
) -> ConsumeOutcome:
    """消费一条消息：先提交业务与收据，再 ACK，最后 CAS 标记 dispatched。

    步骤顺序本身就是契约（C-08 §6「结果先提交再 ACK」）：
    1. 幂等收据 INSERT..ON CONFLICT → 冲突即 duplicate（记审计、不动业务）；
    2. 同一事务写业务事实 + Job 结果（generation 由触发器把关）；
    3. ``conn.commit()`` —— 业务结果先落地；
    4. 事务外 ACK（网络 I/O）；ACK 失败 → 投递回 retry_wait + BROKER_UNAVAILABLE；
    5. 新短事务 CAS 标记 dispatched（epoch 失配 → stale，不静默成功）。
    """

    moment = now or utcnow()
    delivery = conn.execute(
        f"SELECT delivery_id, job_id, status, lease_epoch, delivery_attempt_count, "
        f"       max_delivery_attempts, operation, dedup_key, payload_version, "
        f"       execution_generation "
        f"FROM {SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s AND delivery_id = %s",
        (message.workspace_id, message.delivery_id),
    ).fetchone()
    if delivery is None:
        raise JobProbeError(f"消息指向不存在的投递 {message.delivery_id}")

    receipt = conn.execute(
        f"SELECT receipt_id FROM {SCHEMA}.phase0_consumption_receipts "
        "WHERE workspace_id = %s AND job_id = %s",
        (message.workspace_id, delivery["job_id"]),
    ).fetchone()
    if receipt is not None:
        # 已产生过业务效果：第二次投递只记 duplicate，不再执行业务。
        event_id = _insert_event(
            conn, workspace_id=message.workspace_id, delivery_id=delivery["delivery_id"],
            job_id=delivery["job_id"], lease_epoch=delivery["lease_epoch"],
            from_status=delivery["status"], to_status=delivery["status"], outcome="duplicate",
            detail=f"receipt={receipt['receipt_id']}",
        )
        conn.commit()
        _ack_or_requeue(conn, broker, message, delivery, consumer_id=consumer_id, now=moment)
        _finish_if_dispatching(conn, message, delivery, consumer_id=consumer_id, now=moment)
        return ConsumeOutcome(
            delivery_id=delivery["delivery_id"], job_id=delivery["job_id"],
            outcome="duplicate", business_applied=False, duplicate=True, stale=False,
            error_code=None, event_id=event_id,
        )

    if delivery["status"] != "dispatching" or delivery["lease_epoch"] != message.lease_epoch:
        return _stale_consume(conn, broker, message, delivery, consumer_id=consumer_id,
                              now=moment)

    current_generation = _current_generation(conn, message.workspace_id, delivery["job_id"])
    if current_generation != delivery["execution_generation"]:
        # 领取后 Job 被终态重试推进到新 generation：旧 worker 无权写结果。
        event_id = _insert_event(
            conn, workspace_id=message.workspace_id, delivery_id=delivery["delivery_id"],
            job_id=delivery["job_id"], lease_epoch=delivery["lease_epoch"],
            from_status="dispatching", to_status="dispatching", outcome="stale",
            error_code=STALE_EXECUTION_EPOCH,
            detail=f"claimed_generation={delivery['execution_generation']} "
                   f"current={current_generation}",
        )
        conn.commit()
        _ack_or_requeue(conn, broker, message, delivery, consumer_id=consumer_id, now=moment)
        return ConsumeOutcome(
            delivery_id=delivery["delivery_id"], job_id=delivery["job_id"],
            outcome="stale", business_applied=False, duplicate=False, stale=True,
            error_code=STALE_EXECUTION_EPOCH, event_id=event_id,
        )

    receipt_row = conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_consumption_receipts "
        "(workspace_id, receipt_id, job_id, delivery_id, dedup_key, execution_generation, "
        " consumer_id, applied_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (workspace_id, job_id) DO NOTHING RETURNING receipt_id",
        (message.workspace_id, uuid4(), delivery["job_id"], delivery["delivery_id"],
         delivery["dedup_key"], delivery["execution_generation"], consumer_id,
         delivery["payload_version"]),
    ).fetchone()
    if receipt_row is None:
        event_id = _insert_event(
            conn, workspace_id=message.workspace_id, delivery_id=delivery["delivery_id"],
            job_id=delivery["job_id"], lease_epoch=delivery["lease_epoch"],
            from_status="dispatching", to_status="dispatching", outcome="duplicate",
            detail="并发收据冲突（ON CONFLICT）",
        )
        conn.commit()
        _ack_or_requeue(conn, broker, message, delivery, consumer_id=consumer_id, now=moment)
        _finish_if_dispatching(conn, message, delivery, consumer_id=consumer_id, now=moment)
        return ConsumeOutcome(
            delivery_id=delivery["delivery_id"], job_id=delivery["job_id"],
            outcome="duplicate", business_applied=False, duplicate=True, stale=False,
            error_code=None, event_id=event_id,
        )

    # 业务事实：单调推进 applied_version / applied_generation（CAS，不回退）。
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_ingest_targets "
        "SET applied_version = GREATEST(applied_version, %s), "
        "    applied_generation = GREATEST(applied_generation, %s), "
        "    updated_at = %s "
        "WHERE workspace_id = %s AND target_id = ("
        f"   SELECT target_id FROM {SCHEMA}.phase0_ingest_jobs "
        "    WHERE workspace_id = %s AND job_id = %s)",
        (delivery["payload_version"], delivery["execution_generation"], moment,
         message.workspace_id, message.workspace_id, delivery["job_id"]),
    )
    record = record_result(
        conn, message.workspace_id, delivery["job_id"],
        generation=delivery["execution_generation"], outcome="succeeded",
        applied_version=delivery["payload_version"],
    )
    if not record.recorded:
        conn.rollback()
        raise JobProbeError(
            f"结果写入被拒：{record.error_code} {record.detail or ''}".strip(),
            code=record.error_code or "RESULT_REJECTED",
        )
    event_id = _insert_event(
        conn, workspace_id=message.workspace_id, delivery_id=delivery["delivery_id"],
        job_id=delivery["job_id"], lease_epoch=delivery["lease_epoch"],
        from_status="dispatching", to_status="dispatching", outcome="applied",
        detail=f"version={delivery['payload_version']} "
               f"generation={delivery['execution_generation']}",
    )
    conn.commit()  # ← 业务结果先提交（ACK 之前）

    _ack_or_requeue(conn, broker, message, delivery, consumer_id=consumer_id, now=moment)

    acked = conn.execute(
        f"UPDATE {SCHEMA}.phase0_deliveries "
        "SET status = 'dispatched', lease_expires_at = NULL, acked_at = %s, "
        "    last_error_code = NULL, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND delivery_id = %s "
        "  AND status = 'dispatching' AND lease_epoch = %s "
        "RETURNING status, lease_epoch",
        (moment, message.workspace_id, delivery["delivery_id"], delivery["lease_epoch"]),
    ).fetchone()
    if acked is None:
        stale_event = _insert_event(
            conn, workspace_id=message.workspace_id, delivery_id=delivery["delivery_id"],
            job_id=delivery["job_id"], lease_epoch=delivery["lease_epoch"],
            from_status="dispatching", to_status="dispatching", outcome="stale",
            error_code=STALE_LEASE_EPOCH, detail="ACK 后标记 dispatched 时 epoch 失配",
        )
        conn.commit()
        return ConsumeOutcome(
            delivery_id=delivery["delivery_id"], job_id=delivery["job_id"],
            outcome="stale", business_applied=True, duplicate=False, stale=True,
            error_code=STALE_LEASE_EPOCH, event_id=stale_event,
        )
    _insert_event(
        conn, workspace_id=message.workspace_id, delivery_id=delivery["delivery_id"],
        job_id=delivery["job_id"], lease_epoch=delivery["lease_epoch"],
        from_status="dispatching", to_status="dispatched", outcome="acked",
        detail=f"consumer={consumer_id}",
    )
    conn.commit()
    return ConsumeOutcome(
        delivery_id=delivery["delivery_id"], job_id=delivery["job_id"],
        outcome="applied", business_applied=True, duplicate=False, stale=False,
        error_code=None, event_id=event_id,
    )


def _finish_if_dispatching(
    conn: psycopg.Connection[Any],
    message: DeliveryMessage,
    delivery: Mapping[str, Any],
    *,
    consumer_id: str,
    now: datetime,
) -> bool:
    """重复投递路径的收尾：若仍持有本条 epoch 的租约，则 CAS 置 dispatched。

    已 dispatched（正常情况）或 epoch 已易主的投递不改状态——重复消息只产生
    duplicate 审计，不会把终态再"ACK 一次"。
    """

    if delivery["status"] != "dispatching":
        return False
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_deliveries "
        "SET status = 'dispatched', lease_expires_at = NULL, acked_at = %s, "
        "    last_error_code = NULL, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND delivery_id = %s "
        "  AND status = 'dispatching' AND lease_epoch = %s "
        "RETURNING lease_epoch",
        (now, message.workspace_id, delivery["delivery_id"], message.lease_epoch),
    ).fetchone()
    if row is None:
        conn.commit()
        return False
    _insert_event(
        conn, workspace_id=message.workspace_id, delivery_id=delivery["delivery_id"],
        job_id=delivery["job_id"], lease_epoch=int(row["lease_epoch"]),
        from_status="dispatching", to_status="dispatched", outcome="acked",
        detail=f"consumer={consumer_id} duplicate-path",
    )
    conn.commit()
    return True


def _ack_or_requeue(
    conn: psycopg.Connection[Any],
    broker: redis.Redis,
    message: DeliveryMessage,
    delivery: Mapping[str, Any],
    *,
    consumer_id: str,
    now: datetime,
) -> None:
    """ACK；broker 不可用则显式失败并把投递放回 retry_wait（不静默丢）。"""

    try:
        ack_message(broker, message.stream_id)
    except BrokerUnavailable:
        conn.execute(
            f"UPDATE {SCHEMA}.phase0_deliveries "
            "SET status = 'retry_wait', lease_expires_at = NULL, "
            "    available_at = %s, last_error_code = %s, "
            "    row_version = row_version + 1 "
            "WHERE workspace_id = %s AND delivery_id = %s "
            "  AND status = 'dispatching' AND lease_epoch = %s",
            (now + timedelta(seconds=backoff_delay_seconds(
                int(delivery["delivery_attempt_count"]))),
             BROKER_UNAVAILABLE, message.workspace_id, delivery["delivery_id"],
             delivery["lease_epoch"]),
        )
        _insert_event(
            conn, workspace_id=message.workspace_id, delivery_id=delivery["delivery_id"],
            job_id=delivery["job_id"], lease_epoch=delivery["lease_epoch"],
            from_status="dispatching", to_status="retry_wait", outcome="requeued",
            error_code=BROKER_UNAVAILABLE, detail=f"consumer={consumer_id}",
        )
        conn.commit()
        raise


def _stale_consume(
    conn: psycopg.Connection[Any],
    broker: redis.Redis,
    message: DeliveryMessage,
    delivery: Mapping[str, Any],
    *,
    consumer_id: str,
    now: datetime,
) -> ConsumeOutcome:
    event_id = _insert_event(
        conn, workspace_id=message.workspace_id, delivery_id=delivery["delivery_id"],
        job_id=delivery["job_id"], lease_epoch=delivery["lease_epoch"],
        from_status=delivery["status"], to_status=delivery["status"], outcome="stale",
        error_code=STALE_LEASE_EPOCH,
        detail=f"status={delivery['status']} epoch={delivery['lease_epoch']} "
               f"message_epoch={message.lease_epoch}",
    )
    conn.commit()
    with contextlib.suppress(BrokerUnavailable):
        # 本就要拒绝这条消息；ACK 失败不能把 stale 变成重复投递。
        ack_message(broker, message.stream_id)
    return ConsumeOutcome(
        delivery_id=delivery["delivery_id"], job_id=delivery["job_id"], outcome="stale",
        business_applied=False, duplicate=False, stale=True,
        error_code=STALE_LEASE_EPOCH, event_id=event_id,
    )


# ---------------------------------------------------------------------------
# 重试 / 死信（同一张五态矩阵，不新增状态）
# ---------------------------------------------------------------------------


def backoff_delay_seconds(attempt: int) -> float:
    if attempt < 1:
        raise JobProbeError("attempt 必须 >= 1")
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)


def retry(
    conn: psycopg.Connection[Any],
    delivery: ClaimedDelivery,
    *,
    error_code: str,
    now: datetime | None = None,
) -> DeliveryOutcome:
    """失败重试：未耗尽 → retry_wait + 退避；耗尽 → dead_letter + 原因。"""

    moment = now or utcnow()
    current = conn.execute(
        f"SELECT status, lease_epoch, delivery_attempt_count, max_delivery_attempts "
        f"FROM {SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s AND delivery_id = %s FOR UPDATE",
        (delivery.workspace_id, delivery.delivery_id),
    ).fetchone()
    if current is None:  # pragma: no cover
        raise JobProbeError(f"投递不存在 {delivery.delivery_id}")
    if current["status"] != "dispatching" or current["lease_epoch"] != delivery.lease_epoch:
        _record_stale(conn, delivery, attempted="retry")
        return DeliveryOutcome(
            workspace_id=delivery.workspace_id, delivery_id=delivery.delivery_id,
            previous_status=current["status"], status=current["status"], updated=False,
            stale=True, outcome="stale", lease_epoch=current["lease_epoch"],
            delivery_attempt_count=current["delivery_attempt_count"],
            available_at=None, error_code=STALE_LEASE_EPOCH,
        )

    attempts = int(current["delivery_attempt_count"])
    exhausted = attempts >= int(current["max_delivery_attempts"])
    target = "dead_letter" if exhausted else "retry_wait"
    available_at = None if exhausted else moment + timedelta(
        seconds=backoff_delay_seconds(attempts)
    )
    code = RETRY_EXHAUSTED if exhausted else error_code
    row = conn.execute(
        f"UPDATE {SCHEMA}.phase0_deliveries "
        "SET status = %s, lease_expires_at = NULL, available_at = %s, "
        "    last_error_code = %s, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND delivery_id = %s "
        "  AND status = 'dispatching' AND lease_epoch = %s "
        "RETURNING status, lease_epoch, delivery_attempt_count, available_at",
        (target, available_at or moment, code, delivery.workspace_id,
         delivery.delivery_id, delivery.lease_epoch),
    ).fetchone()
    if row is None:  # pragma: no cover - FOR UPDATE 后不应发生
        _record_stale(conn, delivery, attempted="retry-cas")
        return DeliveryOutcome(
            workspace_id=delivery.workspace_id, delivery_id=delivery.delivery_id,
            previous_status="dispatching", status="dispatching", updated=False,
            stale=True, outcome="stale", lease_epoch=delivery.lease_epoch,
            delivery_attempt_count=delivery.delivery_attempt_count,
            available_at=None, error_code=STALE_LEASE_EPOCH,
        )
    _insert_event(
        conn, workspace_id=delivery.workspace_id, delivery_id=delivery.delivery_id,
        job_id=delivery.job_id, lease_epoch=delivery.lease_epoch,
        from_status="dispatching", to_status=target, outcome=target,
        error_code=code,
        detail=f"attempt={attempts}/{int(current['max_delivery_attempts'])}",
    )
    return DeliveryOutcome(
        workspace_id=delivery.workspace_id, delivery_id=delivery.delivery_id,
        previous_status="dispatching", status=row["status"], updated=True, stale=False,
        outcome=target, lease_epoch=row["lease_epoch"],
        delivery_attempt_count=row["delivery_attempt_count"],
        available_at=row["available_at"], error_code=code,
    )


# ---------------------------------------------------------------------------
# 审计辅助
# ---------------------------------------------------------------------------


def _insert_event(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    delivery_id: UUID,
    job_id: UUID,
    lease_epoch: int,
    from_status: str | None,
    to_status: str,
    outcome: str,
    error_code: str | None = None,
    detail: str | None = None,
) -> UUID:
    event_id = uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_delivery_events "
        "(workspace_id, event_id, delivery_id, job_id, lease_epoch, from_status, "
        " to_status, outcome, error_code, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, event_id, delivery_id, job_id, lease_epoch, from_status,
         to_status, outcome, error_code, (detail or "")[:512] or None),
    )
    return event_id


def _record_stale(
    conn: psycopg.Connection[Any], delivery: ClaimedDelivery, *, attempted: str
) -> None:
    current = conn.execute(
        f"SELECT status, lease_epoch FROM {SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s AND delivery_id = %s",
        (delivery.workspace_id, delivery.delivery_id),
    ).fetchone()
    if current is None:
        return
    _insert_event(
        conn, workspace_id=delivery.workspace_id, delivery_id=delivery.delivery_id,
        job_id=delivery.job_id, lease_epoch=int(current["lease_epoch"]),
        from_status=current["status"], to_status=current["status"], outcome="stale",
        error_code=STALE_LEASE_EPOCH,
        detail=f"{attempted} epoch={delivery.lease_epoch} current={current['lease_epoch']}",
    )


# ---------------------------------------------------------------------------
# celery_app 门面（见模块文档：真实 Celery 未安装，依赖准入归 T02 评审）
# ---------------------------------------------------------------------------


class CeleryAppEntry:
    """与 Celery ``app`` 同调用面的门面：``.task`` 注册、``.send_task`` 投递。

    换成真实 Celery 时，``celery_app`` 改为 ``Celery("pyknora.ingest", broker=...)``，
    任务名/参数保持不变；本门面不假装自己是 Celery（``is_shim`` 为 True）。
    """

    is_shim: Final = True

    def __init__(self, main: str, broker_url: str, result_backend: str) -> None:
        self.main = main
        self.broker_url = broker_url
        self.result_backend = result_backend
        self.stream = STREAM_KEY
        self.group = GROUP
        self._tasks: dict[str, Callable[..., Any]] = {}
        self.conf: dict[str, Any] = {
            "task_acks_late": True,
            "task_reject_on_worker_lost": True,
            "worker_prefetch_multiplier": 1,
            "broker_transport_options": {"visibility_timeout": int(DEFAULT_LEASE_SECONDS)},
            "task_default_queue": GROUP,
        }

    def task(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(func: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._tasks:
                raise JobProbeError(f"任务重名：{name}")
            self._tasks[name] = func
            return func

        return register

    @property
    def tasks(self) -> Mapping[str, Callable[..., Any]]:
        return dict(self._tasks)

    def send_task(
        self,
        name: str,
        *,
        kwargs: Mapping[str, Any] | None = None,
        client: redis.Redis | None = None,
    ) -> str:
        if name not in self._tasks:
            raise JobProbeError(f"未注册的任务：{name}", code="UNKNOWN_TASK")
        fields = {str(key): str(value) for key, value in (kwargs or {}).items()}
        fields["task"] = name
        client = client if client is not None else redis_client(self.broker_url)
        try:
            return str(ensure_transport_slot(client) and client.xadd(self.stream, fields))
        except redis.exceptions.RedisError as exc:
            raise BrokerUnavailable(f"send_task 失败：{exc}") from exc


def _build_celery_app() -> CeleryAppEntry:
    return CeleryAppEntry(
        main="pyknora.ingest",
        broker_url=read_redis_url(),
        result_backend=read_redis_url(),
    )


celery_app: Final = _build_celery_app()


@celery_app.task("pyknora.ingest.consume")
def consume_task(*, dsn: str | None = None, consumer_id: str = "worker-1",
                 now: datetime | None = None) -> list[dict[str, Any]]:
    """真实 worker 的入口形态：读一批消息并逐条消费（供 compose/指南调用）。"""

    broker = redis_client()
    with connect(dsn) as conn:
        ensure_stream(broker)
        handled: list[dict[str, Any]] = []
        for message in read_messages(broker, consumer=consumer_id):
            outcome = consume_message(conn, message, consumer_id=consumer_id, broker=broker,
                                      now=now)
            handled.append({"delivery_id": str(outcome.delivery_id),
                            "outcome": outcome.outcome})
        return handled


@celery_app.task("pyknora.ingest.reconcile")
def reconcile_task(*, dsn: str | None = None, workspace_id: str | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """真实 worker 的对账入口（实现见 job_reconciler）。"""

    from job_reconciler import reconcile_once

    with connect(dsn) as conn:
        report = reconcile_once(conn, workspace_id=UUID(workspace_id) if workspace_id else None,
                                now=now)
        return {
            "scanned": report.scanned,
            "repaired": report.repaired,
            "dead_letters": len(report.dead_letters),
        }


def iter_delivery_states() -> Iterable[str]:
    return iter(DELIVERY_STATES)
