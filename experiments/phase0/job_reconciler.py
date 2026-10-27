"""P0-T08 对账器：发现「已发布但长期未确认」的卡住投递并修复，列出死信。

设计要点
--------
* **不假定恰好一次投递**（C-08 §8）：本模块只把卡住的投递放回 ``retry_wait`` 并
  ``repair_count + 1``、``lease_epoch + 1``，让旧 worker 即使复活也拿不到旧 epoch；
  被重新派发的消息可能再被消费一次，业务侧由 ``job_probe`` 的幂等收据挡住。
* **并发安全**：先 ``SELECT ... FOR UPDATE SKIP LOCKED`` 再 CAS
  ``WHERE status='dispatching' AND lease_epoch=<读到的值>``；两个对账器同时跑时
  只有一个能修复成功，另一个的 CAS 命中 0 行 → 不计入 ``repaired``。
* **只读与修复边界**：对账器不改业务事实、不写 Job 结果，只动投递状态与审计
  （C-08 §3：broker ACK 不写业务终态）。修复动作全部留审计行（``repaired``）。
* **死信不自动复活**：``redrive_dead_letter`` 是运维命令，必须给
  ``expected_row_version``（乐观锁）与 ``reason``（审计），且 Job 已有 succeeded
  结果时直接拒绝 ``TERMINAL_JOB_NO_OP``（C-09 §7「成功 Job 重复 redrive 不复活」）。

状态机仍是 ``job_probe`` 那一套五态（复制自 003），本模块不新增状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import psycopg

import job_probe as jp

__all__ = [
    "DEFAULT_RECONCILE_GRACE_SECONDS",
    "DeadLetter",
    "ReconcileReport",
    "RepairOutcome",
    "RedriveOutcome",
    "list_dead_letters",
    "list_stuck_deliveries",
    "reconcile_once",
    "redrive_dead_letter",
    "repair_stuck_deliveries",
]

#: 比 lease 再多等一会儿才算"长期未确认"，避免与正常处理竞争。
DEFAULT_RECONCILE_GRACE_SECONDS: Final = 10.0
DEFAULT_RECONCILE_LIMIT: Final = 100


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    delivery_id: UUID
    job_id: UUID
    repaired: bool
    previous_status: str
    status: str
    reason: str
    repair_count: int
    lease_epoch: int
    delivery_attempt_count: int


@dataclass(frozen=True, slots=True)
class DeadLetter:
    delivery_id: UUID
    job_id: UUID
    dedup_key: str
    delivery_attempt_count: int
    max_delivery_attempts: int
    reason: str
    last_error_code: str | None
    repair_count: int
    row_version: int


@dataclass(frozen=True, slots=True)
class RedriveOutcome:
    delivery_id: UUID
    updated: bool
    status: str
    error_code: str | None
    redrive_count: int
    event_id: UUID | None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    scanned: int = 0
    repaired: int = 0
    repaired_ids: tuple[UUID, ...] = ()
    dead_lettered_ids: tuple[UUID, ...] = ()
    dead_letters: tuple[DeadLetter, ...] = ()


def _now(moment: datetime | None) -> datetime:
    if moment is None:
        return datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise jp.JobProbeError("now 必须带时区")
    return moment.astimezone(UTC)


def _insert_reconcile_event(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    delivery_id: UUID,
    job_id: UUID,
    lease_epoch: int,
    from_status: str,
    to_status: str,
    outcome: str,
    error_code: str | None,
    detail: str,
) -> UUID:
    event_id = jp.uuid4()
    conn.execute(
        f"INSERT INTO {jp.SCHEMA}.phase0_delivery_events "
        "(workspace_id, event_id, delivery_id, job_id, lease_epoch, from_status, "
        " to_status, outcome, error_code, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (workspace_id, event_id, delivery_id, job_id, lease_epoch, from_status,
         to_status, outcome, error_code, detail[:512]),
    )
    return event_id


def list_stuck_deliveries(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    *,
    now: datetime,
    grace_seconds: float = DEFAULT_RECONCILE_GRACE_SECONDS,
    limit: int = DEFAULT_RECONCILE_LIMIT,
) -> list[dict[str, Any]]:
    """只读：列出已发布但长期未确认（或领取后没发布就死掉）的投递。"""

    moment = _now(now)
    cutoff = moment - timedelta(seconds=grace_seconds)
    rows = conn.execute(
        f"SELECT delivery_id, job_id, status, lease_epoch, published_at, "
        f"       lease_expires_at, delivery_attempt_count, max_delivery_attempts, "
        f"       repair_count, row_version "
        f"FROM {jp.SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s AND status = 'dispatching' "
        "  AND lease_expires_at <= %s "
        "ORDER BY lease_expires_at, delivery_id LIMIT %s",
        (workspace_id, cutoff, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def repair_stuck_deliveries(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    *,
    now: datetime | None = None,
    grace_seconds: float = DEFAULT_RECONCILE_GRACE_SECONDS,
    limit: int = DEFAULT_RECONCILE_LIMIT,
) -> list[RepairOutcome]:
    """修复卡住的投递；返回本条对账器**真正抢到**的修复结果。

    ``SELECT ... FOR UPDATE SKIP LOCKED`` 保证并发对账器不重复处理同一行；
    逐行 CAS（status + lease_epoch）再确认一次，命中 0 行就不计入。
    """

    moment = _now(now)
    if grace_seconds < 0:
        raise jp.JobProbeError("grace_seconds 必须 >= 0")
    cutoff = moment - timedelta(seconds=grace_seconds)

    rows = conn.execute(
        f"SELECT delivery_id, job_id, status, lease_epoch, published_at, "
        f"       delivery_attempt_count, max_delivery_attempts, repair_count "
        f"FROM {jp.SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s AND status = 'dispatching' "
        "  AND lease_expires_at <= %s "
        "ORDER BY lease_expires_at, delivery_id "
        "LIMIT %s FOR UPDATE SKIP LOCKED",
        (workspace_id, cutoff, limit),
    ).fetchall()

    outcomes: list[RepairOutcome] = []
    for row in rows:
        attempt = int(row["delivery_attempt_count"])
        max_attempts = int(row["max_delivery_attempts"])
        exhausted = attempt >= max_attempts
        target = "dead_letter" if exhausted else "retry_wait"
        reason = (
            "published_but_unacked"
            if row["published_at"] is not None
            else "claimed_but_unpublished"
        )
        code = jp.RETRY_EXHAUSTED if exhausted else jp.RECONCILE_UNACKED
        updated = conn.execute(
            f"UPDATE {jp.SCHEMA}.phase0_deliveries "
            "SET status = %s, lease_expires_at = NULL, available_at = %s, "
            "    lease_epoch = lease_epoch + 1, repair_count = repair_count + 1, "
            "    last_error_code = %s, row_version = row_version + 1 "
            "WHERE workspace_id = %s AND delivery_id = %s "
            "  AND status = 'dispatching' AND lease_epoch = %s "
            "RETURNING status, lease_epoch, repair_count",
            (target, moment, code, workspace_id, row["delivery_id"],
             row["lease_epoch"]),
        ).fetchone()
        if updated is None:  # 另一个对账器/worker 抢先：不计入 repaired
            continue
        _insert_reconcile_event(
            conn,
            workspace_id=workspace_id,
            delivery_id=row["delivery_id"],
            job_id=row["job_id"],
            lease_epoch=int(updated["lease_epoch"]),
            from_status="dispatching",
            to_status=target,
            outcome="repaired",
            error_code=code,
            detail=f"{reason} attempt={attempt}/{max_attempts}",
        )
        outcomes.append(
            RepairOutcome(
                delivery_id=row["delivery_id"],
                job_id=row["job_id"],
                repaired=True,
                previous_status="dispatching",
                status=str(updated["status"]),
                reason=reason,
                repair_count=int(updated["repair_count"]),
                lease_epoch=int(updated["lease_epoch"]),
                delivery_attempt_count=attempt,
            )
        )
    return outcomes


def list_dead_letters(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    *,
    limit: int = DEFAULT_RECONCILE_LIMIT,
) -> list[DeadLetter]:
    """列出死信（含原因与次数，C-08 §6「保留原行/原因」）。"""

    rows = conn.execute(
        f"SELECT delivery_id, job_id, dedup_key, delivery_attempt_count, "
        f"       max_delivery_attempts, last_error_code, repair_count, row_version "
        f"FROM {jp.SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s AND status = 'dead_letter' "
        "ORDER BY available_at, delivery_id LIMIT %s",
        (workspace_id, limit),
    ).fetchall()
    return [
        DeadLetter(
            delivery_id=row["delivery_id"],
            job_id=row["job_id"],
            dedup_key=row["dedup_key"],
            delivery_attempt_count=int(row["delivery_attempt_count"]),
            max_delivery_attempts=int(row["max_delivery_attempts"]),
            reason=str(row["last_error_code"] or "UNKNOWN"),
            last_error_code=row["last_error_code"],
            repair_count=int(row["repair_count"]),
            row_version=int(row["row_version"]),
        )
        for row in rows
    ]


def redrive_dead_letter(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    delivery_id: UUID,
    expected_row_version: int,
    reason: str,
    now: datetime | None = None,
) -> RedriveOutcome:
    """运维重派发：dead_letter → pending；Job 已成功时拒绝（不复活终态）。"""

    moment = _now(now)
    if not reason or len(reason) > 200:
        raise jp.JobProbeError("reason 必须非空且 <= 200 字符")

    row = conn.execute(
        f"SELECT d.status, d.row_version, d.job_id, d.lease_epoch, d.redrive_count, "
        f"       EXISTS ("
        f"         SELECT 1 FROM {jp.SCHEMA}.phase0_job_results AS r "
        "          WHERE r.workspace_id = d.workspace_id AND r.job_id = d.job_id "
        "            AND r.outcome = 'succeeded') AS job_succeeded "
        f"FROM {jp.SCHEMA}.phase0_deliveries AS d "
        "WHERE d.workspace_id = %s AND d.delivery_id = %s",
        (workspace_id, delivery_id),
    ).fetchone()
    if row is None:
        return RedriveOutcome(delivery_id=delivery_id, updated=False, status="missing",
                              error_code="DELIVERY_NOT_FOUND", redrive_count=0,
                              event_id=None)
    if bool(row["job_succeeded"]):
        return RedriveOutcome(delivery_id=delivery_id, updated=False,
                              status=row["status"],
                              error_code=jp.TERMINAL_JOB_NO_OP,
                              redrive_count=int(row["redrive_count"]), event_id=None)
    if row["status"] != "dead_letter":
        return RedriveOutcome(delivery_id=delivery_id, updated=False,
                              status=row["status"],
                              error_code="NOT_DEAD_LETTER",
                              redrive_count=int(row["redrive_count"]), event_id=None)
    if int(row["row_version"]) != expected_row_version:
        return RedriveOutcome(delivery_id=delivery_id, updated=False,
                              status=row["status"], error_code="VERSION_CONFLICT",
                              redrive_count=int(row["redrive_count"]), event_id=None)

    updated = conn.execute(
        f"UPDATE {jp.SCHEMA}.phase0_deliveries "
        "SET status = 'pending', available_at = %s, lease_epoch = lease_epoch + 1, "
        "    lease_expires_at = NULL, last_error_code = NULL, "
        "    redrive_count = redrive_count + 1, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND delivery_id = %s AND status = 'dead_letter' "
        "  AND row_version = %s "
        "RETURNING status, lease_epoch, redrive_count",
        (moment, workspace_id, delivery_id, expected_row_version),
    ).fetchone()
    if updated is None:  # 并发 redrive：第二次必然 0 行
        return RedriveOutcome(delivery_id=delivery_id, updated=False,
                              status=row["status"], error_code="VERSION_CONFLICT",
                              redrive_count=int(row["redrive_count"]), event_id=None)
    event_id = _insert_reconcile_event(
        conn,
        workspace_id=workspace_id,
        delivery_id=delivery_id,
        job_id=row["job_id"],
        lease_epoch=int(updated["lease_epoch"]),
        from_status="dead_letter",
        to_status="pending",
        outcome="redrive",
        error_code=None,
        detail=f"operator_redrive: {reason}",
    )
    return RedriveOutcome(delivery_id=delivery_id, updated=True,
                          status=str(updated["status"]), error_code=None,
                          redrive_count=int(updated["redrive_count"]), event_id=event_id)


def reconcile_once(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID | None = None,
    now: datetime | None = None,
    grace_seconds: float = DEFAULT_RECONCILE_GRACE_SECONDS,
    limit: int = DEFAULT_RECONCILE_LIMIT,
) -> ReconcileReport:
    """对账一轮：修复卡住投递 + 汇报死信；``workspace_id=None`` 时扫描全部空间。"""

    moment = _now(now)
    if workspace_id is None:
        rows = conn.execute(
            f"SELECT DISTINCT workspace_id FROM {jp.SCHEMA}.phase0_deliveries "
            "WHERE status = 'dispatching'"
        ).fetchall()
        workspaces = [row["workspace_id"] for row in rows]
    else:
        workspaces = [workspace_id]

    scanned = 0
    repaired_ids: list[UUID] = []
    dead_lettered: list[UUID] = []
    dead_letters: list[DeadLetter] = []
    for ws in workspaces:
        stuck = list_stuck_deliveries(conn, ws, now=moment, grace_seconds=grace_seconds,
                                      limit=limit)
        scanned += len(stuck)
        for outcome in repair_stuck_deliveries(conn, ws, now=moment,
                                               grace_seconds=grace_seconds, limit=limit):
            repaired_ids.append(outcome.delivery_id)
            if outcome.status == "dead_letter":
                dead_lettered.append(outcome.delivery_id)
        dead_letters.extend(list_dead_letters(conn, ws, limit=limit))
    return ReconcileReport(
        scanned=scanned,
        repaired=len(repaired_ids),
        repaired_ids=tuple(repaired_ids),
        dead_lettered_ids=tuple(dead_lettered),
        dead_letters=tuple(dead_letters),
    )
