"""P0-T23 DependencyScheduler：TaskGraph 登记、ready/claim lease+epoch、对账。

范围与所有权（AGENTS.md / TDD 01 §5，**不得越界**）：

* **Task 行**（``p0_t23.phase0_tasks`` / ``phase0_task_graphs`` / ``phase0_task_edges``
  / ``phase0_task_claims`` / ``phase0_task_wakes``）只由本模块（实验版
  OrchestrationService）写；其他任务只能读。
* **Job 状态**只在 JobService（T08，schema ``p0_t08``）：本模块只持有 ``job_id``
  引用，通过 :class:`JobPort` 调用 T08；不复制 Job 状态机、不写 Job 表。
* **checkpoint 内容**属于 LangGraph（T05，schema ``p0_t05``）：本模块只存
  ``graph_state_ref`` 指针；写 checkpoint 的动作在 :mod:`task_executor` 的
  ``CheckpointPort`` 后面。
* **Graph 校验**复用 P0-T22 的 :func:`dag_contracts.validate_graph`：非法计划在
  登记前就被拒绝（不进入调度）。

单活前提（T05 结论，必须写在证据里）：``PostgresSaver`` 无法 fence 独立 saver
连接，W3 的 P0-T05 结论是**单活 runner**。因此本模块用 ``epoch`` CAS 防「旧 owner
迟到写」，但**不**声称多实例并发安全；多实例调度需要后续 ADR/T23 扩展重新评审。

状态机（Task 行）：``pending -> running -> succeeded | failed``，
``pending -> blocked``（必需依赖失败/取消），``pending|running -> cancelled``。
``blocked`` 按必需依赖递归传播；``optional=true`` 的边失败**不**阻断下游。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from dag_contracts import (
    DEFAULT_LIMITS,
    GraphLimits,
    GraphStats,
    TaskGraph,
    payload_from_graph,
    validate_graph,
)

__all__ = [
    "CODE_CHECKPOINT_PENDING",
    "CODE_MIGRATION_ALREADY_APPLIED",
    "CODE_NOT_READY",
    "CODE_REVISION_CONFLICT",
    "CODE_STALE_EPOCH",
    "CODE_STALE_STATE_REF",
    "DEFAULT_LEASE_SECONDS",
    "EXECUTOR_FIXED_JOB",
    "EXECUTOR_GRAPH",
    "JOB_STATES",
    "MIGRATION_FILENAME",
    "MIGRATION_ID",
    "REDIS_DB",
    "SCHEMA",
    "ClaimedTask",
    "DispatchReport",
    "JobHandle",
    "JobPort",
    "JobRequest",
    "JobServicePort",
    "JobSnapshot",
    "MigrationAlreadyApplied",
    "MigrationStateError",
    "ReadyReport",
    "ReconcileReport",
    "RegisteredGraph",
    "SchedulerError",
    "StaleEpoch",
    "StaleStateRef",
    "TaskState",
    "apply_migration",
    "attach_state_ref",
    "cancel_task",
    "claim_ready",
    "claim_task",
    "complete_task",
    "connect",
    "dispatch_ready",
    "ensure_redis_slot",
    "fail_task",
    "graph_row",
    "heartbeat",
    "list_events",
    "migration_file",
    "ready_task_ids",
    "read_dsn",
    "reclaim_expired",
    "record_event",
    "record_state_ref_pending",
    "redis_client",
    "register_graph",
    "reset_schema",
    "reconcile",
    "release_claim",
    "schema_digest",
    "set_job_ref",
    "task_state",
    "task_states",
    "tasks_pending_state_ref",
    "wake_once",
    "wake_redis_key",
]

SCHEMA: Final = "p0_t23"
MIGRATION_ID: Final = "006_tasks"
MIGRATION_FILENAME: Final = "006_tasks.sql"

#: 本任务独占的 Redis DB 槽（T08 用 DB 5；本任务只碰 DB 8）。
REDIS_DB: Final = 8
WAKE_REDIS_PREFIX: Final = "p0:t23:wake:"

EXECUTOR_GRAPH: Final = "graph"
EXECUTOR_FIXED_JOB: Final = "fixed_job"

JOB_STATES: Final[tuple[str, ...]] = ("pending", "running", "succeeded", "failed")
TASK_TERMINAL: Final[frozenset[str]] = frozenset({"succeeded", "failed", "blocked", "cancelled"})

DEFAULT_LEASE_SECONDS: Final = 30.0
MAX_LEASE_SECONDS: Final = 3600.0

#: 稳定错误码：T23 的 API/审计直接引用这些字符串。
CODE_MIGRATION_ALREADY_APPLIED: Final = "MIGRATION_ALREADY_APPLIED"
CODE_MIGRATION_STATE_INVALID: Final = "MIGRATION_STATE_INVALID"
CODE_REVISION_CONFLICT: Final = "REVISION_CONFLICT"
CODE_TASK_NOT_FOUND: Final = "TASK_NOT_FOUND"
CODE_NOT_READY: Final = "NOT_READY"
CODE_STALE_EPOCH: Final = "STALE_EPOCH"
CODE_STALE_STATE_REF: Final = "STALE_STATE_REF"
CODE_STATE_REF_CONFLICT: Final = "STATE_REF_CONFLICT"
CODE_CHECKPOINT_PENDING: Final = "CHECKPOINT_UNAVAILABLE"
CODE_NO_JOB_PORT: Final = "JOB_PORT_REQUIRED"
CODE_REDIS_SLOT: Final = "WRONG_REDIS_SLOT"
CODE_GRAPH_INVALID: Final = "GRAPH_INVALID"


class SchedulerError(Exception):
    """带稳定错误码的调度器错误（拒绝时必须有码，不静默成功）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class MigrationAlreadyApplied(SchedulerError):
    """迁移 006 已应用：重复 apply 必须显式失败，不能静默成功。"""


class MigrationStateError(SchedulerError):
    """schema 已存在但台账不一致（禁止在未知状态上再 apply）。"""


class RevisionConflict(SchedulerError):
    """同一 revision 的图内容与已登记内容不同（revision 不可变）。"""


class StaleEpoch(SchedulerError):
    """旧 epoch 的领取/完成/续租被拒（必须留痕后失败）。"""


class StaleStateRef(SchedulerError):
    """旧 epoch 的 graph 状态引用试图回写（旧 state 不得覆盖事实）。"""


# ---------------------------------------------------------------------------
# 连接 / 迁移
# ---------------------------------------------------------------------------
def read_dsn() -> str:
    """复用 P0-T03 的 DSN 解析（只读 import，不改 scope.py）。"""

    from scope import read_dsn as _read_phase0_dsn

    return _read_phase0_dsn()


def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn or read_dsn(), autocommit=autocommit, row_factory=dict_row)


def _ensure_clean(conn: psycopg.Connection[Any]) -> None:
    """写函数要求 IDLE 事务边界：调用方留下的只读事务先提交，保证本函数真提交。

    否则 ``conn.transaction()`` 会退化成 SAVEPOINT，写操作要等调用方 commit
    才落库——对"事实先落库"的契约是致命的（重启对账会读到旧状态）。
    """

    if conn.info.transaction_status is not TransactionStatus.IDLE:
        conn.commit()


def migration_file() -> Path:
    return Path(__file__).resolve().parent / "db" / "migrations" / MIGRATION_FILENAME


def schema_digest() -> str:
    return hashlib.sha256(migration_file().read_bytes()).hexdigest()


def apply_migration(conn: psycopg.Connection[Any]) -> str:
    """单事务应用迁移 006；重复 apply 抛 :class:`MigrationAlreadyApplied`。"""

    if conn.autocommit:
        raise SchedulerError(CODE_MIGRATION_STATE_INVALID, "apply_migration 需要非 autocommit 连接")
    _ensure_clean(conn)
    digest = schema_digest()
    table = conn.execute(
        "SELECT to_regclass(%s) AS relation", (f"{SCHEMA}.phase0_schema_migrations",)
    ).fetchone()
    if table is not None and table["relation"] is not None:
        applied = conn.execute(
            f"SELECT migration_id FROM {SCHEMA}.phase0_schema_migrations WHERE migration_id = %s",
            (MIGRATION_ID,),
        ).fetchone()
        if applied is not None:
            raise MigrationAlreadyApplied(
                CODE_MIGRATION_ALREADY_APPLIED,
                f"{MIGRATION_FILENAME} 已应用；重复 apply 被拒绝（不重复建表/不静默成功）",
            )
        raise MigrationStateError(
            CODE_MIGRATION_STATE_INVALID,
            f"schema {SCHEMA} 已存在但台账无 {MIGRATION_ID}，拒绝在未知状态上 apply",
        )
    conn.execute(migration_file().read_text(encoding="utf-8"))
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_schema_migrations (migration_id, filename, sha256) "
        "VALUES (%s, %s, %s)",
        (MIGRATION_ID, MIGRATION_FILENAME, digest),
    )
    conn.commit()
    return digest


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    """只删本任务 schema（绝不碰 p0_t03/p0_t05/p0_t07/p0_t08）。"""

    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.commit()


# ---------------------------------------------------------------------------
# 数据载体
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RegisteredGraph:
    workspace_id: UUID
    run_id: UUID
    revision: int
    graph_sha256: str
    node_count: int
    edge_count: int
    depth: int
    created: bool


@dataclass(frozen=True, slots=True)
class TaskState:
    task_id: str
    kind: str
    executor_kind: str
    status: str
    epoch: int
    attempt: int
    weight: int
    required: bool
    lease_owner: str | None
    lease_until: datetime | None
    job_id: UUID | None
    fact: dict[str, Any] | None
    fact_version: int
    graph_state_ref: dict[str, Any] | None
    state_version: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    task_id: str
    epoch: int
    attempt: int
    owner_id: str
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class ReadyReport:
    ready: tuple[str, ...]
    running: tuple[str, ...]
    blocked: tuple[str, ...]
    terminal: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DispatchReport:
    claimed: tuple[str, ...]
    executed: tuple[str, ...]
    job_dispatched: tuple[str, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    reclaimed: tuple[str, ...]
    completed_from_job: tuple[str, ...]
    failed_from_job: tuple[str, ...]
    still_waiting_job: tuple[str, ...]
    ready: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JobRequest:
    workspace_id: UUID
    run_id: UUID
    revision: int
    task_id: str
    epoch: int
    kind: str
    dedup_key: str
    target_id: UUID | None = None
    payload_version: int = 1


@dataclass(frozen=True, slots=True)
class JobHandle:
    workspace_id: UUID
    job_id: UUID
    delivery_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: UUID
    state: str
    applied_version: int | None = None
    error_code: str | None = None


class JobPort(Protocol):
    """JobService(T08) 的最小 port：只走它，不新建第二套作业状态机。"""

    def enqueue(self, request: JobRequest) -> JobHandle: ...

    def status(self, handle: JobHandle) -> JobSnapshot: ...


# ---------------------------------------------------------------------------
# TaskGraph 登记（复用 T22 校验）
# ---------------------------------------------------------------------------
def register_graph(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    graph: TaskGraph,
    optional_dependencies: Collection[tuple[str, str]] = (),
    fixed_job_kinds: Collection[str] = ("fetch", "ingest"),
    limits: GraphLimits | None = None,
) -> RegisteredGraph:
    """先经 T22 校验，再冻结登记 graph + tasks + edges（同一事务）。"""

    _ensure_clean(conn)
    if revision < 1:
        raise SchedulerError(CODE_GRAPH_INVALID, f"revision 必须 >= 1，实际 {revision}")
    validation = validate_graph(graph, limits or DEFAULT_LIMITS)
    stats: GraphStats = validation.raise_if_invalid()
    payload = payload_from_graph(graph)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    optional = {(source, target) for source, target in optional_dependencies}
    known_edges = set(graph.edges)
    unknown_optional = optional - known_edges
    if unknown_optional:
        raise SchedulerError(
            CODE_GRAPH_INVALID, f"optional 依赖不是图里的边：{sorted(unknown_optional)}"
        )
    fixed = frozenset(fixed_job_kinds)

    with conn.transaction():
        existing = conn.execute(
            f"SELECT graph_sha256, node_count, edge_count, depth FROM {SCHEMA}.phase0_task_graphs "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s",
            (workspace_id, run_id, revision),
        ).fetchone()
        if existing is not None:
            if existing["graph_sha256"] != digest:
                raise RevisionConflict(
                    CODE_REVISION_CONFLICT,
                    f"revision {revision} 已登记不同图内容；revision 不可变",
                )
            return RegisteredGraph(
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                graph_sha256=digest,
                node_count=int(existing["node_count"]),
                edge_count=int(existing["edge_count"]),
                depth=int(existing["depth"]),
                created=False,
            )

        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_task_graphs "
            "(workspace_id, run_id, revision, graph_sha256, node_count, edge_count, depth, "
            " topo_order) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                workspace_id,
                run_id,
                revision,
                digest,
                stats.node_count,
                stats.edge_count,
                stats.depth,
                Jsonb(list(stats.topological_order)),
            ),
        )
        for node in graph.nodes:
            executor_kind = EXECUTOR_FIXED_JOB if node.kind in fixed else EXECUTOR_GRAPH
            conn.execute(
                f"INSERT INTO {SCHEMA}.phase0_tasks "
                "(workspace_id, run_id, revision, task_id, kind, executor_kind, weight, required) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    run_id,
                    revision,
                    node.node_id,
                    node.kind,
                    executor_kind,
                    node.weight,
                    node.required,
                ),
            )
        for source, target in graph.edges:
            conn.execute(
                f"INSERT INTO {SCHEMA}.phase0_task_edges "
                "(workspace_id, run_id, revision, from_task, to_task, optional) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (workspace_id, run_id, revision, source, target, (source, target) in optional),
            )
    return RegisteredGraph(
        workspace_id=workspace_id,
        run_id=run_id,
        revision=revision,
        graph_sha256=digest,
        node_count=stats.node_count,
        edge_count=stats.edge_count,
        depth=stats.depth,
        created=True,
    )


def graph_row(conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int):
    return conn.execute(
        f"SELECT graph_sha256, node_count, edge_count, depth, topo_order, registered_at "
        f"FROM {SCHEMA}.phase0_task_graphs "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s",
        (workspace_id, run_id, revision),
    ).fetchone()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def _task_state(row: Mapping[str, Any]) -> TaskState:
    return TaskState(
        task_id=str(row["task_id"]),
        kind=str(row["kind"]),
        executor_kind=str(row["executor_kind"]),
        status=str(row["status"]),
        epoch=int(row["epoch"]),
        attempt=int(row["attempt"]),
        weight=int(row["weight"]),
        required=bool(row["required"]),
        lease_owner=row["lease_owner"],
        lease_until=row["lease_until"],
        job_id=row["job_id"],
        fact=row["fact"],
        fact_version=int(row["fact_version"]),
        graph_state_ref=row["graph_state_ref"],
        state_version=int(row["state_version"]),
        last_error=row["last_error"],
    )


_TASK_COLUMNS: Final = (
    "task_id, kind, executor_kind, status, epoch, attempt, weight, required, lease_owner, "
    "lease_until, job_id, fact, fact_version, graph_state_ref, state_version, last_error"
)


def task_states(
    conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int
) -> dict[str, TaskState]:
    rows = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM {SCHEMA}.phase0_tasks "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s ORDER BY task_id",
        (workspace_id, run_id, revision),
    ).fetchall()
    return {str(row["task_id"]): _task_state(row) for row in rows}


def task_state(
    conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int, task_id: str
) -> TaskState:
    row = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM {SCHEMA}.phase0_tasks "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s",
        (workspace_id, run_id, revision, task_id),
    ).fetchone()
    if row is None:
        raise SchedulerError(CODE_TASK_NOT_FOUND, f"task {task_id} 不在 {revision} 图里")
    return _task_state(row)


_READY_SQL: Final = f"""
SELECT t.task_id
FROM {SCHEMA}.phase0_tasks t
WHERE t.workspace_id = %s AND t.run_id = %s AND t.revision = %s
  AND t.status = 'pending'
  AND t.job_id IS NULL
  AND NOT EXISTS (
    SELECT 1
    FROM {SCHEMA}.phase0_task_edges e
    JOIN {SCHEMA}.phase0_tasks d
      ON d.workspace_id = e.workspace_id AND d.run_id = e.run_id
     AND d.revision = e.revision AND d.task_id = e.from_task
    WHERE e.workspace_id = t.workspace_id AND e.run_id = t.run_id
      AND e.revision = t.revision AND e.to_task = t.task_id
      AND (
        (NOT e.optional AND d.status <> 'succeeded')
        OR (e.optional AND d.status NOT IN ('succeeded', 'failed', 'blocked', 'cancelled'))
      )
  )
ORDER BY t.task_id
"""


def _is_ready(conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int,
              task_id: str) -> bool:
    row = conn.execute(
        f"SELECT 1 AS ok FROM ({_READY_SQL}) AS ready WHERE ready.task_id = %s",
        (workspace_id, run_id, revision, task_id),
    ).fetchone()
    return row is not None


def ready_task_ids(
    conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int
) -> tuple[str, ...]:
    rows = conn.execute(_READY_SQL, (workspace_id, run_id, revision)).fetchall()
    return tuple(str(row["task_id"]) for row in rows)


def ready_report(
    conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int
) -> ReadyReport:
    states = task_states(conn, workspace_id, run_id, revision)
    ready = ready_task_ids(conn, workspace_id, run_id, revision)
    running = tuple(sorted(t for t, s in states.items() if s.status == "running"))
    blocked = tuple(sorted(t for t, s in states.items() if s.status == "blocked"))
    terminal = tuple(sorted(t for t, s in states.items() if s.status in TASK_TERMINAL))
    return ReadyReport(ready=ready, running=running, blocked=blocked, terminal=terminal)


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
def record_event(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    owner_id: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_task_claim_events "
        "(workspace_id, run_id, revision, task_id, epoch, owner_id, outcome, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, run_id, revision, task_id, epoch, owner_id, outcome, detail),
    )


def list_events(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    *,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    sql = (
        f"SELECT task_id, epoch, owner_id, outcome, detail, created_at "
        f"FROM {SCHEMA}.phase0_task_claim_events "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s"
    )
    params: list[Any] = [workspace_id, run_id, revision]
    if task_id is not None:
        sql += " AND task_id = %s"
        params.append(task_id)
    sql += " ORDER BY event_id"
    return list(conn.execute(sql, params).fetchall())


def _audit_event(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    owner_id: str,
    outcome: str,
    detail: str,
) -> None:
    """在**独立已提交事务**里写审计：被拒绝操作自己的回滚不能把留痕一起抹掉。"""

    with conn.transaction():
        record_event(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=epoch,
            owner_id=owner_id,
            outcome=outcome,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# 领取：CAS + lease + epoch
# ---------------------------------------------------------------------------
def _claim_locked_row(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    owner_id: str,
    lease_seconds: float,
    moment: datetime,
) -> ClaimedTask | None:
    lease_until = moment + timedelta(seconds=lease_seconds)
    updated = conn.execute(
        f"UPDATE {SCHEMA}.phase0_tasks "
        "SET status = 'running', epoch = epoch + 1, attempt = attempt + 1, "
        "    lease_owner = %s, lease_until = %s, last_error = NULL, updated_at = %s "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
        "  AND status = 'pending' "
        "RETURNING epoch, attempt",
        (owner_id, lease_until, moment, workspace_id, run_id, revision, task_id),
    ).fetchone()
    if updated is None:
        return None
    epoch = int(updated["epoch"])
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_task_claims "
        "(workspace_id, run_id, revision, task_id, epoch, owner_id, claimed_at, lease_until) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, run_id, revision, task_id, epoch, owner_id, moment, lease_until),
    )
    record_event(
        conn,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=revision,
        task_id=task_id,
        epoch=epoch,
        owner_id=owner_id,
        outcome="claimed",
        detail=f"attempt={int(updated['attempt'])}",
    )
    return ClaimedTask(
        task_id=task_id,
        epoch=epoch,
        attempt=int(updated["attempt"]),
        owner_id=owner_id,
        lease_until=lease_until,
    )


def claim_ready(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    owner_id: str,
    limit: int = 10,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> tuple[ClaimedTask, ...]:
    """``FOR UPDATE SKIP LOCKED`` 抢 ready 集：每个逻辑节点每 epoch 只领一次。"""

    _ensure_clean(conn)
    if limit < 1:
        raise SchedulerError(CODE_NOT_READY, "limit 必须 >= 1")
    if not 0 < lease_seconds <= MAX_LEASE_SECONDS:
        raise SchedulerError(CODE_NOT_READY, f"lease_seconds 越界：{lease_seconds}")
    moment = now or datetime.now(UTC)
    claimed: list[ClaimedTask] = []
    with conn.transaction():
        rows = conn.execute(
            _READY_SQL + " LIMIT %s FOR UPDATE OF t SKIP LOCKED",
            (workspace_id, run_id, revision, limit),
        ).fetchall()
        for row in rows:
            task = _claim_locked_row(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=str(row["task_id"]),
                owner_id=owner_id,
                lease_seconds=lease_seconds,
                moment=moment,
            )
            if task is not None:
                claimed.append(task)
    return tuple(claimed)


def claim_task(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    owner_id: str,
    expected_epoch: int | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> ClaimedTask:
    """显式领取一个 ready 节点；``expected_epoch`` 不符即拒绝并留痕。"""

    _ensure_clean(conn)
    moment = now or datetime.now(UTC)
    stale_detail: str | None = None
    claimed: ClaimedTask | None = None
    with conn.transaction():
        row = conn.execute(
            f"SELECT epoch, status FROM {SCHEMA}.phase0_tasks "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "FOR UPDATE",
            (workspace_id, run_id, revision, task_id),
        ).fetchone()
        if row is None:
            raise SchedulerError(CODE_TASK_NOT_FOUND, task_id)
        current_epoch = int(row["epoch"])
        if expected_epoch is not None and current_epoch != expected_epoch:
            # 审计不能写在会被回滚的事务里：先退出事务，再独立提交留痕后抛错。
            stale_detail = f"claim_expected={expected_epoch} current={current_epoch}"
        elif not _is_ready(conn, workspace_id, run_id, revision, task_id):
            raise SchedulerError(
                CODE_NOT_READY, f"task {task_id} 依赖未满足（status={row['status']}），拒绝提前执行"
            )
        else:
            claimed = _claim_locked_row(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task_id,
                owner_id=owner_id,
                lease_seconds=lease_seconds,
                moment=moment,
            )
    if stale_detail is not None:
        _audit_stale_epoch(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=expected_epoch if expected_epoch is not None else 0,
            owner_id=owner_id,
            detail=stale_detail,
        )
        raise StaleEpoch(
            CODE_STALE_EPOCH,
            f"task {task_id} 当前 epoch 与期望 {expected_epoch} 不一致",
        )
    if claimed is None:  # pragma: no cover - 行锁内 pending 必然可领
        raise SchedulerError(CODE_NOT_READY, f"task {task_id} 领取失败")
    return claimed


def heartbeat(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    owner_id: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> datetime:
    """续租；旧 epoch / 非 owner / 已被回收一律 CAS 失败并留痕。"""

    _ensure_clean(conn)
    moment = now or datetime.now(UTC)
    lease_until = moment + timedelta(seconds=lease_seconds)
    updated = conn.execute(
        f"UPDATE {SCHEMA}.phase0_tasks SET lease_until = %s, updated_at = %s "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
        "  AND status = 'running' AND epoch = %s AND lease_owner = %s AND lease_until > %s "
        "RETURNING lease_until",
        (lease_until, moment, workspace_id, run_id, revision, task_id, epoch, owner_id, moment),
    ).fetchone()
    if updated is None:
        record_event(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=epoch,
            owner_id=owner_id,
            outcome="stale_epoch",
            detail="heartbeat rejected",
        )
        conn.commit()
        raise StaleEpoch(CODE_STALE_EPOCH, f"task {task_id} 续租失败（epoch={epoch}）")
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_task_claims SET lease_until = %s "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
        "  AND epoch = %s AND status = 'active'",
        (lease_until, workspace_id, run_id, revision, task_id, epoch),
    )
    conn.commit()
    return updated["lease_until"]


def reclaim_expired(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """回收 lease 过期的 running（回到 pending，epoch 不变，下次领取 +1）。"""

    _ensure_clean(conn)
    moment = now or datetime.now(UTC)
    with conn.transaction():
        rows = conn.execute(
            f"UPDATE {SCHEMA}.phase0_tasks "
            "SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = %s "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s "
            "  AND status = 'running' AND job_id IS NULL AND lease_until < %s "
            "RETURNING task_id, epoch, lease_owner",
            (moment, workspace_id, run_id, revision, moment),
        ).fetchall()
        reclaimed: list[str] = []
        for row in rows:
            task_id = str(row["task_id"])
            epoch = int(row["epoch"])
            conn.execute(
                f"UPDATE {SCHEMA}.phase0_task_claims "
                "SET status = 'expired', closed_at = %s "
                "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
                "  AND epoch = %s AND status = 'active'",
                (moment, workspace_id, run_id, revision, task_id, epoch),
            )
            record_event(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task_id,
                epoch=epoch,
                owner_id=str(row["lease_owner"] or "unknown"),
                outcome="lease_expired",
                detail=f"reclaimed_at={moment.isoformat()}",
            )
            reclaimed.append(task_id)
    return tuple(reclaimed)


def release_claim(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    owner_id: str,
    detail: str = "released",
) -> None:
    """把一次领取放回 pending（用于 Job port 缺失等可重试的配置错误）。"""

    _ensure_clean(conn)
    with conn.transaction():
        updated = conn.execute(
            f"UPDATE {SCHEMA}.phase0_tasks "
            "SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = now() "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "  AND status = 'running' AND epoch = %s AND lease_owner = %s "
            "RETURNING task_id",
            (workspace_id, run_id, revision, task_id, epoch, owner_id),
        ).fetchone()
        if updated is None:
            raise StaleEpoch(CODE_STALE_EPOCH, f"task {task_id} release 时 epoch 已过期")
        conn.execute(
            f"UPDATE {SCHEMA}.phase0_task_claims SET status = 'released', closed_at = now() "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "  AND epoch = %s AND status = 'active'",
            (workspace_id, run_id, revision, task_id, epoch),
        )
        record_event(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=epoch,
            owner_id=owner_id,
            outcome="released",
            detail=detail,
        )


# ---------------------------------------------------------------------------
# 完成 / 失败 / 取消 / 阻断传播
# ---------------------------------------------------------------------------
def _audit_stale_epoch(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    owner_id: str,
    detail: str,
) -> None:
    """拒绝路径的留痕必须**独立提交**：被拒操作自己的事务回滚不能抹掉审计。"""

    with conn.transaction():
        record_event(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=epoch,
            owner_id=owner_id,
            outcome="stale_epoch",
            detail=detail,
        )


def _reload_state(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
) -> TaskState:
    row = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM {SCHEMA}.phase0_tasks "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s",
        (workspace_id, run_id, revision, task_id),
    ).fetchone()
    if row is None:
        raise SchedulerError(CODE_TASK_NOT_FOUND, task_id)
    return _task_state(row)


def complete_task(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    fact: Mapping[str, Any],
    owner_id: str = "unknown",
    now: datetime | None = None,
) -> TaskState:
    """事实先落库：CAS 到 succeeded 并写 fact；重复完成同一 epoch 幂等返回。"""

    _ensure_clean(conn)
    moment = now or datetime.now(UTC)
    stale_detail: str | None = None
    result: TaskState | None = None
    with conn.transaction():
        updated = conn.execute(
            f"UPDATE {SCHEMA}.phase0_tasks "
            "SET status = 'succeeded', fact = %s, fact_version = fact_version + 1, "
            "    lease_owner = NULL, lease_until = NULL, last_error = NULL, updated_at = %s "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "  AND status = 'running' AND epoch = %s "
            "RETURNING " + _TASK_COLUMNS,
            (Jsonb(dict(fact)), moment, workspace_id, run_id, revision, task_id, epoch),
        ).fetchone()
        if updated is None:
            state = _reload_state(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task_id,
            )
            if state.status == "succeeded" and state.epoch == epoch:
                result = state
            else:
                stale_detail = f"current_status={state.status} current_epoch={state.epoch}"
        else:
            conn.execute(
                f"UPDATE {SCHEMA}.phase0_task_claims SET status = 'released', closed_at = %s "
                "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
                "  AND epoch = %s AND status = 'active'",
                (moment, workspace_id, run_id, revision, task_id, epoch),
            )
            record_event(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task_id,
                epoch=epoch,
                owner_id=owner_id,
                outcome="completed",
                detail=f"fact_version={int(updated['fact_version'])}",
            )
            result = _task_state(updated)
    if stale_detail is not None:
        _audit_stale_epoch(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=epoch,
            owner_id=owner_id,
            detail=stale_detail,
        )
        raise StaleEpoch(
            CODE_STALE_EPOCH, f"task {task_id} 拒绝 epoch={epoch} 的完成写入（{stale_detail}）"
        )
    assert result is not None
    return result


def _block_dependents(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    root_task_id: str,
    reason: str,
    now: datetime,
) -> tuple[str, ...]:
    """必需依赖失败 → 递归 blocked；optional 边不传播（下游继续）。"""

    blocked: list[str] = []
    frontier = [root_task_id]
    seen = {root_task_id}
    while frontier:
        current = frontier.pop(0)
        rows = conn.execute(
            f"SELECT e.to_task FROM {SCHEMA}.phase0_task_edges e "
            f"JOIN {SCHEMA}.phase0_tasks t "
            "  ON t.workspace_id = e.workspace_id AND t.run_id = e.run_id "
            " AND t.revision = e.revision AND t.task_id = e.to_task "
            "WHERE e.workspace_id = %s AND e.run_id = %s AND e.revision = %s "
            "  AND e.from_task = %s AND e.optional = false AND t.status = 'pending'",
            (workspace_id, run_id, revision, current),
        ).fetchall()
        for row in rows:
            task_id = str(row["to_task"])
            if task_id in seen:
                continue
            seen.add(task_id)
            conn.execute(
                f"UPDATE {SCHEMA}.phase0_tasks "
                "SET status = 'blocked', last_error = %s, updated_at = %s "
                "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
                "  AND status = 'pending'",
                (f"blocked_by:{reason}", now, workspace_id, run_id, revision, task_id),
            )
            record_event(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task_id,
                epoch=0,
                owner_id="scheduler",
                outcome="blocked",
                detail=f"reason={reason} root={root_task_id}",
            )
            blocked.append(task_id)
            frontier.append(task_id)
    return tuple(blocked)


def fail_task(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    error_code: str,
    owner_id: str = "unknown",
    now: datetime | None = None,
) -> tuple[str, ...]:
    """失败先落库，再按必需依赖传播 blocked；返回被阻断的节点。"""

    _ensure_clean(conn)
    moment = now or datetime.now(UTC)
    stale_detail: str | None = None
    blocked: tuple[str, ...] = ()
    with conn.transaction():
        updated = conn.execute(
            f"UPDATE {SCHEMA}.phase0_tasks "
            "SET status = 'failed', last_error = %s, lease_owner = NULL, lease_until = NULL, "
            "    updated_at = %s "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "  AND status = 'running' AND epoch = %s RETURNING task_id",
            (error_code, moment, workspace_id, run_id, revision, task_id, epoch),
        ).fetchone()
        if updated is None:
            state = _reload_state(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task_id,
            )
            if state.status == "failed" and state.epoch == epoch:
                return ()  # 重复失败：幂等，不重复传播
            stale_detail = f"current_status={state.status} current_epoch={state.epoch}"
        else:
            conn.execute(
                f"UPDATE {SCHEMA}.phase0_task_claims SET status = 'released', closed_at = %s "
                "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
                "  AND epoch = %s AND status = 'active'",
                (moment, workspace_id, run_id, revision, task_id, epoch),
            )
            record_event(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task_id,
                epoch=epoch,
                owner_id=owner_id,
                outcome="failed",
                detail=error_code,
            )
            blocked = _block_dependents(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                root_task_id=task_id,
                reason=error_code,
                now=moment,
            )
    if stale_detail is not None:
        _audit_stale_epoch(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=epoch,
            owner_id=owner_id,
            detail=stale_detail,
        )
        raise StaleEpoch(
            CODE_STALE_EPOCH, f"task {task_id} 拒绝 epoch={epoch} 的失败写入（{stale_detail}）"
        )
    return blocked


def cancel_task(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    reason: str = "cancelled",
    now: datetime | None = None,
) -> tuple[str, ...]:
    """取消 pending/running 节点；必需依赖者递归 blocked（迟到的 Job 结果不再成功）。"""

    _ensure_clean(conn)
    moment = now or datetime.now(UTC)
    with conn.transaction():
        row = conn.execute(
            f"UPDATE {SCHEMA}.phase0_tasks "
            "SET status = 'cancelled', lease_owner = NULL, lease_until = NULL, last_error = %s, "
            "    updated_at = %s "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "  AND status IN ('pending', 'running') RETURNING epoch",
            (reason, moment, workspace_id, run_id, revision, task_id),
        ).fetchone()
        if row is None:
            raise SchedulerError(CODE_NOT_READY, f"task {task_id} 不在可取消状态")
        conn.execute(
            f"UPDATE {SCHEMA}.phase0_task_claims SET status = 'released', closed_at = %s "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "  AND status = 'active'",
            (moment, workspace_id, run_id, revision, task_id),
        )
        record_event(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=int(row["epoch"]),
            owner_id="scheduler",
            outcome="released",
            detail=f"cancelled:{reason}",
        )
        return _block_dependents(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            root_task_id=task_id,
            reason=f"cancelled:{reason}",
            now=moment,
        )


# ---------------------------------------------------------------------------
# Graph 状态引用（payload 归 LangGraph；这里只存指针 + epoch CAS）
# ---------------------------------------------------------------------------
def attach_state_ref(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    state_ref: Mapping[str, Any],
    owner_id: str = "unknown",
) -> TaskState:
    """事实之后写 graph 指针；旧 epoch / 已有不同指针一律拒绝并留痕。"""

    _ensure_clean(conn)
    with conn.transaction():
        updated = conn.execute(
            f"UPDATE {SCHEMA}.phase0_tasks "
            "SET graph_state_ref = %s, state_version = state_version + 1, updated_at = now() "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "  AND status = 'succeeded' AND epoch = %s AND graph_state_ref IS NULL "
            "RETURNING " + _TASK_COLUMNS,
            (Jsonb(dict(state_ref)), workspace_id, run_id, revision, task_id, epoch),
        ).fetchone()
        if updated is None:
            state = _reload_state(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task_id,
            )
            if state.graph_state_ref is not None and state.graph_state_ref == dict(state_ref):
                return state  # 幂等重放
            stale_detail = (
                f"current_status={state.status} current_epoch={state.epoch} "
                f"has_ref={state.graph_state_ref is not None}"
            )
        else:
            stale_detail = None
            record_event(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task_id,
                epoch=epoch,
                owner_id=owner_id,
                outcome="state_ref_attached",
                detail=f"state_version={int(updated['state_version'])}",
            )
    if stale_detail is not None:
        _audit_event(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=epoch,
            owner_id=owner_id,
            outcome="stale_state_ref",
            detail=stale_detail,
        )
        raise StaleStateRef(
            CODE_STALE_STATE_REF,
            f"task {task_id} 拒绝旧/冲突状态引用（epoch={epoch}）：{stale_detail}",
        )
    if updated is None:
        return _reload_state(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
        )
    return _task_state(updated)


def record_state_ref_pending(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    detail: str,
    owner_id: str = "unknown",
) -> None:
    """事实已提交但 checkpoint 写失败：只留痕，不回滚事实、不改 succeeded。"""

    _ensure_clean(conn)
    with conn.transaction():
        conn.execute(
            f"UPDATE {SCHEMA}.phase0_tasks SET last_error = %s "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "  AND status = 'succeeded' AND graph_state_ref IS NULL",
            (f"{CODE_CHECKPOINT_PENDING}:{detail}"[:512], workspace_id, run_id, revision, task_id),
        )
        record_event(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=epoch,
            owner_id=owner_id,
            outcome="state_ref_pending",
            detail=detail,
        )


def tasks_pending_state_ref(
    conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int
) -> tuple[TaskState, ...]:
    rows = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM {SCHEMA}.phase0_tasks "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s "
        "  AND status = 'succeeded' AND graph_state_ref IS NULL ORDER BY task_id",
        (workspace_id, run_id, revision),
    ).fetchall()
    return tuple(_task_state(row) for row in rows)


# ---------------------------------------------------------------------------
# wake 去重（DB 台账为权威；Redis DB 8 只做通知去重）
# ---------------------------------------------------------------------------
def wake_redis_key(
    workspace_id: UUID, run_id: UUID, revision: int, task_id: str, epoch: int, wake_key: str
) -> str:
    return f"{WAKE_REDIS_PREFIX}{workspace_id}:{run_id}:{revision}:{task_id}:{epoch}:{wake_key}"


def redis_client(url: str | None = None) -> Any:
    import redis as redis_module

    base = url or os.environ.get("PHASE0_T23_REDIS_URL") or _redis_url_from_env_example()
    head, _, _ = base.rpartition("/")
    client = redis_module.Redis.from_url(
        f"{head}/{REDIS_DB}", decode_responses=True, socket_connect_timeout=1.0, socket_timeout=5.0
    )
    ensure_redis_slot(client)
    return client


def _redis_url_from_env_example() -> str:
    path = Path(__file__).resolve().parent / ".env.example"
    text = path.read_text(encoding="utf-8")
    host = None
    port = None
    for line in text.splitlines():
        if line.startswith("PHASE0_REDIS_HOST="):
            host = line.split("=", 1)[1].strip()
        elif line.startswith("PHASE0_REDIS_HOST_PORT="):
            port = line.split("=", 1)[1].strip()
    if host is None or port is None:
        raise SchedulerError(CODE_REDIS_SLOT, f"{path} 缺少 Redis host/port")
    return f"redis://{host}:{port}/0"


def ensure_redis_slot(client: Any) -> int:
    """拒绝非 DB 8 连接：别的波次槽不能被本任务写/删。"""

    db = client.connection_pool.connection_kwargs.get("db")
    if db != REDIS_DB:
        raise SchedulerError(CODE_REDIS_SLOT, f"T23 只允许 Redis DB {REDIS_DB}，当前 db={db!r}")
    return db


def wake_once(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    wake_key: str,
    broker: Any | None = None,
    owner_id: str = "reconciler",
) -> bool:
    """Job 重复完成只唤醒一次：DB 唯一键插入 0 行 → False（并留 wake_duplicate）。"""

    _ensure_clean(conn)
    with conn.transaction():
        row = conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_task_wakes "
            "(workspace_id, run_id, revision, task_id, epoch, wake_key) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (workspace_id, run_id, revision, task_id, epoch, wake_key) DO NOTHING "
            "RETURNING wake_key",
            (workspace_id, run_id, revision, task_id, epoch, wake_key),
        ).fetchone()
        created = row is not None
        record_event(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task_id,
            epoch=epoch,
            owner_id=owner_id,
            outcome="wake" if created else "wake_duplicate",
            detail=wake_key,
        )
    if created and broker is not None:
        ensure_redis_slot(broker)
        broker.set(
            wake_redis_key(workspace_id, run_id, revision, task_id, epoch, wake_key),
            "1",
            nx=True,
            ex=3600,
        )
    return created


# ---------------------------------------------------------------------------
# Job port（T08 适配器；只调用 JobService，不建第二套作业状态机）
# ---------------------------------------------------------------------------
class JobServicePort:
    """T08 JobService 的适配器：写路径走 T08 公开函数，``status`` 只做跨域只读查询。

    * ``enqueue`` / ``record_result`` 委托 ``job_probe``（Job 状态机仍然只有一套）；
    * ``status`` 只读 schema ``p0_t08`` 的 job_results / deliveries，用于 T23 对账；
    * 本类不写 p0_t08 的状态列，也不把 Job 状态复制进 p0_t23。
    """

    def __init__(self, conn: psycopg.Connection[Any], *, job_module: Any | None = None) -> None:
        self.conn = conn
        self._module = job_module

    def _job(self) -> Any:
        if self._module is None:
            import job_probe as job_module

            self._module = job_module
        return self._module

    def enqueue(self, request: JobRequest) -> JobHandle:
        from uuid import uuid4

        jp = self._job()
        enqueued = jp.enqueue(
            self.conn,
            jp.EnqueueRequest(
                workspace_id=request.workspace_id,
                target_id=request.target_id or uuid4(),
                job_id=uuid4(),
                dedup_key=request.dedup_key[:256],
                target_name=f"p0-t23-{request.task_id}",
                kind=request.kind,
                payload_version=request.payload_version,
            ),
        )
        self.conn.commit()
        return JobHandle(
            workspace_id=request.workspace_id,
            job_id=enqueued.job_id,
            delivery_id=enqueued.delivery_id,
        )

    def status(self, handle: JobHandle) -> JobSnapshot:
        jp = self._job()
        schema = jp.SCHEMA
        row = self.conn.execute(
            f"SELECT j.job_id, "
            f"  (SELECT r.outcome FROM {schema}.phase0_job_results r "
            "    WHERE r.workspace_id = j.workspace_id AND r.job_id = j.job_id "
            "    ORDER BY r.execution_generation DESC LIMIT 1) AS outcome, "
            f"  (SELECT r.applied_version FROM {schema}.phase0_job_results r "
            "    WHERE r.workspace_id = j.workspace_id AND r.job_id = j.job_id "
            "    ORDER BY r.execution_generation DESC LIMIT 1) AS applied_version, "
            f"  (SELECT r.error_code FROM {schema}.phase0_job_results r "
            "    WHERE r.workspace_id = j.workspace_id AND r.job_id = j.job_id "
            "    ORDER BY r.execution_generation DESC LIMIT 1) AS error_code, "
            f"  EXISTS(SELECT 1 FROM {schema}.phase0_deliveries d "
            "         WHERE d.workspace_id = j.workspace_id AND d.job_id = j.job_id "
            "           AND d.status = 'dispatching') AS dispatching "
            f"FROM {schema}.phase0_ingest_jobs j "
            "WHERE j.workspace_id = %s AND j.job_id = %s",
            (handle.workspace_id, handle.job_id),
        ).fetchone()
        if row is None:
            return JobSnapshot(job_id=handle.job_id, state="pending", error_code="JOB_NOT_FOUND")
        outcome = row["outcome"]
        if outcome == "succeeded":
            return JobSnapshot(
                job_id=handle.job_id,
                state="succeeded",
                applied_version=int(row["applied_version"] or 0),
            )
        if outcome in ("failed", "dead"):
            return JobSnapshot(
                job_id=handle.job_id, state="failed", error_code=row["error_code"] or "JOB_FAILED"
            )
        return JobSnapshot(
            job_id=handle.job_id, state="running" if row["dispatching"] else "pending"
        )

    def record_result(
        self, handle: JobHandle, *, outcome: str, applied_version: int | None = None
    ) -> bool:
        """写 Job 结果（worker/测试用）：委托 T08 ``record_result``，不自己改状态。"""

        jp = self._job()
        recorded = jp.record_result(
            self.conn,
            handle.workspace_id,
            handle.job_id,
            generation=1,
            outcome=outcome,
            applied_version=applied_version,
        )
        self.conn.commit()
        return bool(recorded.recorded)


# ---------------------------------------------------------------------------
# 调度：ready -> claim -> graph executor / Job port
# ---------------------------------------------------------------------------
def set_job_ref(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    epoch: int,
    job_id: UUID,
) -> None:
    _ensure_clean(conn)
    with conn.transaction():
        updated = conn.execute(
            f"UPDATE {SCHEMA}.phase0_tasks SET job_id = %s, updated_at = now() "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND task_id = %s "
            "  AND status = 'running' AND epoch = %s RETURNING task_id",
            (job_id, workspace_id, run_id, revision, task_id, epoch),
        ).fetchone()
        if updated is None:
            raise StaleEpoch(CODE_STALE_EPOCH, f"task {task_id} 写 job_id 时 epoch 已过期")


def dispatch_ready(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    owner_id: str,
    executor: Any | None = None,
    job_port: JobPort | None = None,
    job_request_factory: Any | None = None,
    limit: int = 10,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> DispatchReport:
    """只执行 ready：graph 节点走 executor，fixed_job 节点走 T08 Job port。"""

    claims = claim_ready(
        conn,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=revision,
        owner_id=owner_id,
        limit=limit,
        lease_seconds=lease_seconds,
        now=now,
    )
    claimed: list[str] = []
    executed: list[str] = []
    job_dispatched: list[str] = []
    failures: list[str] = []
    states = task_states(conn, workspace_id, run_id, revision)
    for claim in claims:
        claimed.append(claim.task_id)
        state = states[claim.task_id]
        if state.executor_kind == EXECUTOR_FIXED_JOB:
            if job_port is None:
                release_claim(
                    conn,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    revision=revision,
                    task_id=claim.task_id,
                    epoch=claim.epoch,
                    owner_id=owner_id,
                    detail="job port missing",
                )
                failures.append(claim.task_id)
                continue
            request = (
                job_request_factory(state, claim)
                if job_request_factory is not None
                else JobRequest(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    revision=revision,
                    task_id=claim.task_id,
                    epoch=claim.epoch,
                    kind=state.kind,
                    dedup_key=f"p0_t23:{workspace_id}:{run_id}:{revision}:{claim.task_id}",
                )
            )
            handle = job_port.enqueue(request)
            set_job_ref(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=claim.task_id,
                epoch=claim.epoch,
                job_id=handle.job_id,
            )
            job_dispatched.append(claim.task_id)
            continue
        if executor is None:
            release_claim(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=claim.task_id,
                epoch=claim.epoch,
                owner_id=owner_id,
                detail="executor missing",
            )
            failures.append(claim.task_id)
            continue
        try:
            executor.execute(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task=state,
                claim=claim,
            )
        except SchedulerError:
            raise
        except Exception as exc:  # noqa: BLE001 - 执行失败必须落库，不能吞
            fail_task(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=claim.task_id,
                epoch=claim.epoch,
                error_code=f"EXECUTOR_ERROR:{type(exc).__name__}",
                owner_id=owner_id,
            )
            failures.append(claim.task_id)
            continue
        executed.append(claim.task_id)
    return DispatchReport(
        claimed=tuple(claimed),
        executed=tuple(executed),
        job_dispatched=tuple(job_dispatched),
        failures=tuple(failures),
    )


# ---------------------------------------------------------------------------
# 对账：重启后按 Task 事实 + Job 状态重算
# ---------------------------------------------------------------------------
def reconcile(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    job_port: JobPort | None = None,
    now: datetime | None = None,
) -> ReconcileReport:
    """回收卡住的 running，按 Job 事实补全，并重算 ready 集。"""

    _ensure_clean(conn)
    completed: list[str] = []
    failed: list[str] = []
    waiting: list[str] = []
    if job_port is not None:
        for state in task_states(conn, workspace_id, run_id, revision).values():
            if state.status != "running" or state.job_id is None:
                continue
            snapshot = job_port.status(JobHandle(workspace_id=workspace_id, job_id=state.job_id))
            if snapshot.state == "succeeded":
                complete_task(
                    conn,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    revision=revision,
                    task_id=state.task_id,
                    epoch=state.epoch,
                    fact={
                        "job_id": str(state.job_id),
                        "outcome": "succeeded",
                        "applied_version": snapshot.applied_version,
                    },
                    owner_id="reconciler",
                )
                completed.append(state.task_id)
            elif snapshot.state == "failed":
                fail_task(
                    conn,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    revision=revision,
                    task_id=state.task_id,
                    epoch=state.epoch,
                    error_code=snapshot.error_code or "JOB_FAILED",
                    owner_id="reconciler",
                )
                failed.append(state.task_id)
            else:
                waiting.append(state.task_id)
    reclaimed = reclaim_expired(
        conn, workspace_id=workspace_id, run_id=run_id, revision=revision, now=now
    )
    ready = ready_task_ids(conn, workspace_id, run_id, revision)
    return ReconcileReport(
        reclaimed=reclaimed,
        completed_from_job=tuple(completed),
        failed_from_job=tuple(failed),
        still_waiting_job=tuple(waiting),
        ready=ready,
    )
