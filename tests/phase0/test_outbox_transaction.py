"""P0-T07 真实验收：Outbox 生产事务、派发 lease、CAS/epoch、五态与死信。

每个用例都打真实 PostgreSQL(5433)/Redis(6380, DB 3)，没有 mock、没有 skip/xfail：
环境不满足时**报错**而不是"跳过"，因为跳过会让 P0-T07 变成假通过。

断言对应的验收条款（逐条列在用例 docstring 里）
------------------------------------------------
* 原子提交：业务 + Job + Outbox 同事务；提交前崩溃/回滚 → 三者都不在；
* 派发：``FOR UPDATE SKIP LOCKED`` + lease，4 worker 无重复，重复投递身份不变；
* 顺序：publish 必须在提交之后——用独立连接可见性 + ``pg_stat_activity`` 证明，
  并用"事务内发布"反例证明检测器真的会失败；
* epoch/CAS：旧 epoch 更新 0 行 → ``stale`` 审计，不静默成功；
* 五态 + 死信 + 重试计数 + 退避可测 + redrive；
* 投递审计只追加；scan query plan 用得上 ``phase0_outbox_claim_idx``。

隔离：只用 schema ``p0_t07``、Redis DB 3；DSN 走 ``scope.read_dsn()``（环境变量或
``.env.example``），凭据不进断言、不进日志。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import outbox_probe as ob
import psycopg
import pytest
import redis

PHASE0_DIR = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
TABLE_ALLOWLIST = (
    "phase0_ingest_targets",
    "phase0_jobs",
    "phase0_outbox",
    "phase0_outbox_delivery_events",
)

#: 子进程：claim → 发布 → **硬退出**（不 mark、不回收、不跑 finally），
#: 用来真实复现"发送成功但未标记就崩"的窗口（P0-T07 注入项）。
_CHILD_DISPATCHER = r"""
import os
import sys
from datetime import UTC, datetime
from uuid import UUID

sys.path.insert(0, sys.argv[1])
import outbox_probe as ob

workspace_id = UUID(sys.argv[2])
conn = ob.connect()
with conn.transaction():
    deliveries = ob.claim(
        conn, workspace_id, limit=1, now=datetime.now(UTC), lease_seconds=120
    )
if not deliveries:
    os._exit(3)
ob.RedisPublisher().publish(deliveries[0].envelope())
os._exit(9)  # 消息已经进了队列，但 outbox 永远停在 dispatching
"""


# ---------------------------------------------------------------------------
# fixtures（conftest.py 属他人文件，DB/Redis fixture 必须写在这里）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def outbox_schema() -> Iterator[str]:
    """会话级：重建 p0_t07 并应用迁移 003；结束时只删 p0_t07。"""

    conn = ob.connect()
    try:
        ob.reset_schema(conn)
        conn.commit()
        digest = ob.apply_migration(conn)
        conn.commit()
    finally:
        conn.close()
    assert len(digest) == 64
    yield ob.SCHEMA
    conn = ob.connect()
    try:
        ob.reset_schema(conn)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def dsn(outbox_schema: str) -> str:
    from scope import read_dsn

    return read_dsn()


@pytest.fixture()
def conn(outbox_schema: str) -> Iterator[psycopg.Connection]:
    connection = ob.connect()
    try:
        yield connection
    finally:
        if not connection.closed:
            connection.close()


@pytest.fixture()
def broker(outbox_schema: str) -> Iterator[redis.Redis]:
    client = ob.redis_client()
    ob.flush_slot(client)
    try:
        yield client
    finally:
        ob.flush_slot(client)
        client.close()


@pytest.fixture()
def workspace() -> UUID:
    return uuid4()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(UTC)


def _enqueue(
    conn: psycopg.Connection,
    workspace_id: UUID,
    dedup_key: str,
    **overrides: object,
) -> tuple[ob.EnqueueRequest, ob.EnqueuedMessage]:
    request = ob.EnqueueRequest(
        workspace_id=workspace_id,
        target_id=uuid4(),
        target_name=f"target-{dedup_key}",
        job_id=uuid4(),
        dedup_key=dedup_key,
        **overrides,  # type: ignore[arg-type]
    )
    return request, ob.append(conn, request)


def _count(conn: psycopg.Connection, table: str, workspace_id: UUID) -> int:
    if table not in TABLE_ALLOWLIST:
        raise AssertionError(f"未登记的表名: {table}")
    row = conn.execute(
        f"SELECT count(*) FROM {ob.SCHEMA}.{table} WHERE workspace_id = %s", (workspace_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _outbox_row(conn: psycopg.Connection, workspace_id: UUID, outbox_id: UUID) -> dict:
    row = conn.execute(
        "SELECT status, lease_epoch, lease_expires_at, delivery_attempt_count, "
        "max_delivery_attempts, last_error_code, available_at, dispatched_at, "
        "redrive_count, row_version "
        f"FROM {ob.SCHEMA}.phase0_outbox WHERE workspace_id = %s AND outbox_id = %s",
        (workspace_id, outbox_id),
    ).fetchone()
    assert row is not None, f"读不到 outbox {outbox_id}"
    keys = (
        "status",
        "lease_epoch",
        "lease_expires_at",
        "delivery_attempt_count",
        "max_delivery_attempts",
        "last_error_code",
        "available_at",
        "dispatched_at",
        "redrive_count",
        "row_version",
    )
    return dict(zip(keys, row, strict=True))


def _events(conn: psycopg.Connection, workspace_id: UUID, outbox_id: UUID) -> list[tuple]:
    return conn.execute(
        "SELECT outcome, from_status, to_status, error_code "
        f"FROM {ob.SCHEMA}.phase0_outbox_delivery_events "
        "WHERE workspace_id = %s AND outbox_id = %s ORDER BY event_seq",
        (workspace_id, outbox_id),
    ).fetchall()


def _job_snapshot(conn: psycopg.Connection, workspace_id: UUID) -> tuple:
    return conn.execute(
        "SELECT job_id, status, lease_epoch, row_version, updated_at "
        f"FROM {ob.SCHEMA}.phase0_jobs WHERE workspace_id = %s ORDER BY job_id",
        (workspace_id,),
    ).fetchall()


class _RecordingPublisher:
    """记录发布事实，并**用全新连接**复核"发布这一刻"outbox 已提交且仍持 lease。

    这条独立复核不依赖 dispatcher 自己的 observer：如果 publish 发生在提交之前，
    新连接看不到该行（READ COMMITTED），``seen_status`` 里就会是 None 而测试失败。
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self.envelopes: list[ob.PublishEnvelope] = []
        self.seen_status: list[tuple[str | None, int | None]] = []
        self._lock = threading.Lock()

    def publish(self, envelope: ob.PublishEnvelope) -> ob.PublishReceipt:
        observer_conn = psycopg.connect(self._dsn, autocommit=True)
        try:
            row = observer_conn.execute(
                "SELECT status, lease_epoch FROM p0_t07.phase0_outbox "
                "WHERE workspace_id = %s AND outbox_id = %s",
                (envelope.workspace_id, envelope.outbox_id),
            ).fetchone()
        finally:
            observer_conn.close()
        with self._lock:
            self.envelopes.append(envelope)
            self.seen_status.append(None if row is None else (row[0], row[1]))
            position = len(self.envelopes)
        return ob.PublishReceipt(
            transport_id=f"recording:{position}", queue="recording", published_at=_now()
        )


class _FailingPublisher:
    """broker 拒绝：一律 ``PublishFailed``，计数供断言。"""

    def __init__(self, error_code: str = "BROKER_UNAVAILABLE") -> None:
        self.error_code = error_code
        self.calls = 0

    def publish(self, envelope: ob.PublishEnvelope) -> ob.PublishReceipt:
        self.calls += 1
        raise ob.PublishFailed("broker 拒绝接收", error_code=self.error_code)


def _dead_redis() -> redis.Redis:
    """指向关闭端口的客户端：连接立即被拒（不会挂住测试）。"""

    return redis.Redis.from_url(
        "redis://127.0.0.1:1/3",
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )


# ---------------------------------------------------------------------------
# 一、原子提交与提交前失败
# ---------------------------------------------------------------------------
def test_append_registers_business_job_and_outbox_in_one_commit(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """C-01 §6 / C-08 §6：业务事实 + Job + Outbox 一次提交后三者同时可见。"""

    request, message = _enqueue(conn, workspace, "atomic-1")
    assert message.job_created is True
    assert message.outbox_created is True
    before = _now()
    conn.commit()

    assert _count(conn, "phase0_ingest_targets", workspace) == 1
    assert _count(conn, "phase0_jobs", workspace) == 1
    assert _count(conn, "phase0_outbox", workspace) == 1
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "pending"
    assert row["lease_epoch"] == 0
    assert row["delivery_attempt_count"] == 0
    assert row["available_at"] <= before  # 已提交即可被领取
    assert row["lease_expires_at"] is None
    assert _events(conn, workspace, message.outbox_id) == []
    target = conn.execute(
        f"SELECT name FROM {ob.SCHEMA}.phase0_ingest_targets "
        "WHERE workspace_id = %s AND target_id = %s",
        (workspace, request.target_id),
    ).fetchone()
    assert target == (request.target_name,)


def test_rollback_before_commit_leaves_nothing_claimable(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """回滚 = 提交前失败：三张表都不留事实，"可执行消息数"为 0。"""

    _, message = _enqueue(conn, workspace, "rollback-1")
    conn.rollback()

    assert _count(conn, "phase0_ingest_targets", workspace) == 0
    assert _count(conn, "phase0_jobs", workspace) == 0
    assert _count(conn, "phase0_outbox", workspace) == 0
    assert ob.claim(conn, workspace, limit=10, now=_now()) == ()
    conn.commit()


def test_backend_killed_before_commit_leaves_no_partial_fact(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """真崩溃而不是 rollback：服务端终止后端（连接级 kill）后三表仍全空。"""

    victim = ob.connect()
    try:
        _, message = _enqueue(victim, workspace, "crash-1")
        pid = victim.info.backend_pid
        supervisor = ob.connect(autocommit=True)
        try:
            terminated = supervisor.execute(
                "SELECT pg_terminate_backend(%s)", (pid,)
            ).fetchone()
            assert terminated == (True,)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                alive = supervisor.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)
                ).fetchone()
                if alive is None:
                    break
                time.sleep(0.05)
            assert (
                supervisor.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)
                ).fetchone()
                is None
            )
        finally:
            supervisor.close()
    finally:
        victim.close()

    assert _count(conn, "phase0_ingest_targets", workspace) == 0
    assert _count(conn, "phase0_jobs", workspace) == 0
    assert _count(conn, "phase0_outbox", workspace) == 0
    assert ob.claim(conn, workspace, limit=10, now=_now()) == ()
    assert message.outbox_created is True  # 崩溃前确实写进去过
    conn.commit()


def test_job_without_outbox_cannot_commit(conn: psycopg.Connection, workspace: UUID) -> None:
    """"已提交 Job 全部有 Outbox"由延迟约束触发器在 COMMIT 时强制，而非事后 count。"""

    target_id = uuid4()
    job_id = uuid4()
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        conn.execute(
            f"INSERT INTO {ob.SCHEMA}.phase0_ingest_targets (workspace_id, target_id, name) "
            "VALUES (%s, %s, %s)",
            (workspace, target_id, "only-job"),
        )
        conn.execute(
            f"INSERT INTO {ob.SCHEMA}.phase0_jobs "
            "(workspace_id, job_id, target_id, kind, dedup_key) VALUES (%s, %s, %s, 'ingest', %s)",
            (workspace, job_id, target_id, "half-write"),
        )
        conn.commit()
    assert "没有 Outbox" in str(excinfo.value)
    conn.rollback()

    assert _count(conn, "phase0_jobs", workspace) == 0
    assert _count(conn, "phase0_outbox", workspace) == 0
    assert _count(conn, "phase0_ingest_targets", workspace) == 0


def test_duplicate_enqueue_keeps_job_and_outbox_identity(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """C-08 §7：同 dedup_key 重复登记复用身份（客户端换 job_id 也不行）。"""

    request, first = _enqueue(conn, workspace, "dup-1")
    conn.commit()
    second_request = ob.EnqueueRequest(
        workspace_id=workspace,
        target_id=uuid4(),
        target_name="target-dup-1",
        job_id=uuid4(),
        dedup_key="dup-1",
    )
    second = ob.append(conn, second_request)
    conn.commit()

    assert second_request.job_id != first.job_id
    assert second.job_created is False
    assert second.outbox_created is False
    assert second.job_id == first.job_id
    assert second.outbox_id == first.outbox_id
    assert second.dedup_key == request.dedup_key
    assert _count(conn, "phase0_jobs", workspace) == 1
    assert _count(conn, "phase0_outbox", workspace) == 1


# ---------------------------------------------------------------------------
# 二、publish 必须在提交之后（含反例）
# ---------------------------------------------------------------------------
def test_publish_after_commit_is_proven_by_independent_observers(
    conn: psycopg.Connection, workspace: UUID, dsn: str
) -> None:
    """顺序证据：claim 事务提交后才发布；发布时后端 idle、无事务、行已可见。"""

    _, message = _enqueue(conn, workspace, "order-1")
    conn.commit()

    publisher = _RecordingPublisher(dsn)
    dispatcher = ob.OutboxDispatcher(
        workspace_id=workspace, publisher=publisher, dsn=dsn, batch_limit=4
    )
    try:
        report = dispatcher.run_once()
    finally:
        dispatcher.close()

    assert len(report.claimed) == 1
    delivery = report.claimed[0]
    observation = report.observations[0]
    assert observation.visible is True
    assert observation.observed_status == "dispatching"
    assert observation.observed_epoch == delivery.lease_epoch == 1
    assert observation.dispatcher_state == "idle"
    assert observation.dispatcher_in_transaction is False
    assert report.publish_backend_states == ("idle",)
    # 发布器自己用新连接复核：消息发出时行已提交且处于 dispatching
    assert publisher.seen_status == [("dispatching", 1)]
    assert len(publisher.envelopes) == 1
    assert publisher.envelopes[0].outbox_id == message.outbox_id
    # 发布之后才 CAS 成 dispatched
    assert [outcome.status for outcome in report.outcomes] == ["dispatched"]
    assert report.outcomes[0].updated is True
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "dispatched"
    assert row["dispatched_at"] is not None
    assert row["lease_expires_at"] is None
    outcomes = [event[0] for event in _events(conn, workspace, message.outbox_id)]
    assert outcomes == ["claimed", "dispatched"]


def test_publish_inside_transaction_is_caught_by_observer(
    conn: psycopg.Connection, workspace: UUID, dsn: str
) -> None:
    """负向对照：在 claim 事务内发布 → 观测器必须当场失败（检测器本身也被测到）。"""

    _, message = _enqueue(conn, workspace, "in-txn-1")
    deliveries = ob.claim(conn, workspace, limit=1, now=_now())  # 故意不提交
    assert len(deliveries) == 1

    observer = ob.PublishObserver(dsn=dsn)
    try:
        with pytest.raises(ob.PublishOrderViolation) as excinfo:
            observer.observe(deliveries[0], dispatcher_pid=conn.info.backend_pid)
    finally:
        observer.close()

    violation = excinfo.value
    assert violation.code == "PUBLISH_NOT_AFTER_COMMIT"
    assert violation.reason == "row_not_visible_after_commit"
    assert violation.observation.visible is False
    assert violation.observation.observed_status is None  # 未提交的行对独立连接不可见
    assert violation.observation.observed_epoch is None
    assert violation.observation.dispatcher_in_transaction is True
    assert violation.observation.dispatcher_state == "idle in transaction"
    conn.rollback()
    # 事务内发布失败后不能留下"伪成功"的 dispatched
    assert _count(conn, "phase0_outbox", workspace) == 0
    assert _count(conn, "phase0_jobs", workspace) == 0


def test_two_dispatchers_contend_for_one_outbox(
    conn: psycopg.Connection, workspace: UUID, dsn: str
) -> None:
    """两个 dispatcher 同时抢同一条 Outbox：只有一方派发，消息只发一次。"""

    _, message = _enqueue(conn, workspace, "contend-1")
    conn.commit()

    publisher = _RecordingPublisher(dsn)
    barrier = threading.Barrier(2)
    reports: list[ob.DispatchReport] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        dispatcher = ob.OutboxDispatcher(
            workspace_id=workspace, publisher=publisher, dsn=dsn, batch_limit=4
        )
        try:
            barrier.wait(timeout=15)
            report = dispatcher.run_once()
        except BaseException as exc:  # noqa: BLE001 - 线程内异常必须回传主线程
            with lock:
                errors.append(exc)
        else:
            with lock:
                reports.append(report)
        finally:
            dispatcher.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(reports) == 2
    assert sum(len(report.claimed) for report in reports) == 1
    assert len(publisher.envelopes) == 1
    assert sum(len(report.published) for report in reports) == 1
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "dispatched"
    assert row["delivery_attempt_count"] == 1
    assert row["lease_epoch"] == 1


# ---------------------------------------------------------------------------
# 三、并发领取：SKIP LOCKED 不重复
# ---------------------------------------------------------------------------
def test_four_workers_claim_disjoint_rows(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """≥4 worker 并发 claim：同一行不被两人领到，并集恰为全部待派发行。"""

    expected: set[UUID] = set()
    for index in range(20):
        _, message = _enqueue(conn, workspace, f"worker-{index}")
        expected.add(message.outbox_id)
    conn.commit()

    workers = 4
    barrier = threading.Barrier(workers)
    claimed: list[UUID] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        connection = ob.connect()
        try:
            barrier.wait(timeout=20)
            for _ in range(200):
                with connection.transaction():
                    deliveries = ob.claim(connection, workspace, limit=3, now=_now())
                with lock:
                    claimed.extend(item.outbox_id for item in deliveries)
                    done = len(claimed)
                if not deliveries and done >= len(expected):
                    return
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert len(claimed) == len(expected), f"领取 {len(claimed)} 条，期望 {len(expected)} 条"
    assert len(set(claimed)) == len(expected), "同一 outbox 被多个 worker 领取（重复处理）"
    assert set(claimed) == expected
    rows = conn.execute(
        "SELECT status, lease_epoch, delivery_attempt_count "
        f"FROM {ob.SCHEMA}.phase0_outbox WHERE workspace_id = %s",
        (workspace,),
    ).fetchall()
    assert len(rows) == len(expected)
    assert all(row == ("dispatching", 1, 1) for row in rows)
    events = conn.execute(
        "SELECT count(*) FROM "
        f"{ob.SCHEMA}.phase0_outbox_delivery_events WHERE workspace_id = %s "
        "AND outcome = 'claimed'",
        (workspace,),
    ).fetchone()
    assert events == (len(expected),)


# ---------------------------------------------------------------------------
# 四、lease 回收 / 旧 epoch / 死信 / redrive
# ---------------------------------------------------------------------------
def test_expired_lease_is_reclaimable_and_reuses_identity(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """租约过期可回收（发成未标记/进程死亡），重派沿用 outbox_id/job_id 并 attempt+1。"""

    _, message = _enqueue(conn, workspace, "lease-1")
    conn.commit()
    base = _now()

    first = ob.claim(conn, workspace, limit=1, now=base, lease_seconds=5)
    conn.commit()
    assert len(first) == 1
    assert first[0].lease_epoch == 1
    assert first[0].lease_expires_at == base + timedelta(seconds=5)
    # 租约未到期：不给别人领
    assert ob.claim(conn, workspace, limit=1, now=base + timedelta(seconds=4)) == ()
    conn.commit()

    later = base + timedelta(seconds=10)
    reclaimed = ob.reclaim_expired_leases(conn, workspace, now=later)
    conn.commit()
    assert [outcome.outcome for outcome in reclaimed] == ["reclaimed"]
    assert reclaimed[0].outbox_id == message.outbox_id
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "retry_wait"
    assert row["lease_epoch"] == 2
    assert row["available_at"] == later + timedelta(seconds=ob.BACKOFF_BASE_SECONDS)
    assert row["lease_expires_at"] is None

    second = ob.claim(
        conn, workspace, limit=1, now=row["available_at"] + timedelta(seconds=1)
    )
    conn.commit()
    assert len(second) == 1
    assert second[0].outbox_id == message.outbox_id
    assert second[0].job_id == first[0].job_id
    assert second[0].lease_epoch == 3
    assert second[0].delivery_attempt_count == 2
    assert second[0].envelope().job_id == message.job_id


def test_stale_epoch_update_is_rejected_and_recorded(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """旧 worker 用旧 epoch 标记：0 行 + stale 审计，绝不静默成功。"""

    _, message = _enqueue(conn, workspace, "stale-1")
    conn.commit()
    base = _now()
    first = ob.claim(conn, workspace, limit=1, now=base, lease_seconds=1)
    conn.commit()
    assert first[0].lease_epoch == 1

    reclaimed = ob.reclaim_expired_leases(conn, workspace, now=base + timedelta(seconds=5))
    conn.commit()
    assert reclaimed[0].outcome == "reclaimed"

    stale = ob.mark_dispatched(conn, first[0])
    conn.commit()
    assert stale.updated is False
    assert stale.stale is True
    assert stale.outcome == "stale"
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "retry_wait"  # 没有被旧 worker 改成 dispatched
    assert row["lease_epoch"] == 2
    stale_events = [event for event in _events(conn, workspace, message.outbox_id)
                    if event[0] == "stale"]
    assert len(stale_events) == 1
    assert stale_events[0][3] == "STALE_LEASE_EPOCH"

    # 退避到期后由**新** dispatcher 重新 claim（epoch 继续 +1）；
    # 迟到的旧 worker 仍然只能拿到 stale，不能把行抢回自己名下。
    late_claim = ob.claim(
        conn, workspace, limit=1, now=row["available_at"] + timedelta(seconds=1)
    )
    conn.commit()
    assert len(late_claim) == 1
    assert late_claim[0].lease_epoch == 3
    old_retry = ob.retry(conn, first[0], error_code="BROKER_UNAVAILABLE")
    conn.commit()
    assert old_retry.stale is True
    assert _outbox_row(conn, workspace, message.outbox_id)["status"] == "dispatching"


def test_retry_backoff_then_dead_letter_then_redrive(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """退避可复算（5/10/20…、上限 300）、次数耗尽进 dead_letter、redrive 后旧 epoch 作废。"""

    assert ob.backoff_delay_seconds(1) == 5.0
    assert ob.backoff_delay_seconds(2) == 10.0
    assert ob.backoff_delay_seconds(3) == 20.0
    assert ob.backoff_delay_seconds(4) == 40.0
    assert ob.backoff_delay_seconds(10) == ob.BACKOFF_CAP_SECONDS

    _, message = _enqueue(conn, workspace, "dead-1", max_delivery_attempts=3)
    conn.commit()
    base = _now()

    first = ob.claim(conn, workspace, limit=1, now=base, lease_seconds=30)
    conn.commit()
    first_retry = ob.retry(conn, first[0], error_code="BROKER_UNAVAILABLE", now=base)
    conn.commit()
    assert first_retry.outcome == "retry_wait"
    assert first_retry.available_at == base + timedelta(seconds=5)
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert (row["status"], row["last_error_code"]) == ("retry_wait", "BROKER_UNAVAILABLE")
    assert row["lease_epoch"] == 2

    second_at = first_retry.available_at + timedelta(seconds=1)
    second = ob.claim(conn, workspace, limit=1, now=second_at, lease_seconds=30)
    conn.commit()
    assert second[0].delivery_attempt_count == 2
    second_retry = ob.retry(conn, second[0], error_code="BROKER_UNAVAILABLE", now=second_at)
    conn.commit()
    assert second_retry.available_at == second_at + timedelta(seconds=10)

    third_at = second_retry.available_at + timedelta(seconds=1)
    third = ob.claim(conn, workspace, limit=1, now=third_at, lease_seconds=30)
    conn.commit()
    assert third[0].delivery_attempt_count == 3
    terminated = ob.retry(conn, third[0], error_code="BROKER_UNAVAILABLE", now=third_at)
    conn.commit()
    assert terminated.outcome == "dead_letter"
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "dead_letter"
    assert row["delivery_attempt_count"] == 3
    assert row["last_error_code"] == "BROKER_UNAVAILABLE"
    assert row["lease_expires_at"] is None

    # 死信不接受 claim（必须先 redrive）
    assert ob.claim(conn, workspace, limit=1, now=third_at + timedelta(hours=1)) == ()
    conn.commit()

    # 旧 epoch 的 redrive 冲突：row_version 不匹配 → stale，不静默成功
    conflict = ob.redrive(
        conn,
        workspace_id=workspace,
        outbox_id=message.outbox_id,
        expected_row_version=1,
        reason="stale-redrive",
    )
    conn.commit()
    assert (conflict.updated, conflict.stale) == (False, True)

    current = _outbox_row(conn, workspace, message.outbox_id)
    redriven = ob.redrive(
        conn,
        workspace_id=workspace,
        outbox_id=message.outbox_id,
        expected_row_version=current["row_version"],
        reason="broker 恢复后人工重派",
        now=third_at,
    )
    conn.commit()
    assert (redriven.updated, redriven.stale) == (True, False)
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "pending"
    assert row["redrive_count"] == 1
    assert row["lease_epoch"] == current["lease_epoch"] + 1
    # 旧 epoch（dead_letter 前那次领取）已被作废
    assert ob.mark_dispatched(conn, third[0]).stale is True
    conn.commit()
    assert _outbox_row(conn, workspace, message.outbox_id)["status"] == "pending"
    outcomes = [event[0] for event in _events(conn, workspace, message.outbox_id)]
    assert outcomes == [
        "claimed",
        "retry_wait",
        "claimed",
        "retry_wait",
        "claimed",
        "dead_letter",
        "stale",
        "redrive",
        "stale",
    ]


# ---------------------------------------------------------------------------
# 五、broker 不可用 / dispatcher 边界
# ---------------------------------------------------------------------------
def test_redis_outage_keeps_message_recoverable(
    conn: psycopg.Connection, workspace: UUID, dsn: str
) -> None:
    """断 Redis：消息不丢（retry_wait + error_code + 退避），Job 事实不被改动。"""

    _, message = _enqueue(conn, workspace, "outage-1")
    conn.commit()
    jobs_before = _job_snapshot(conn, workspace)

    started = _now()
    dispatcher = ob.OutboxDispatcher(
        workspace_id=workspace,
        publisher=ob.RedisPublisher(client=_dead_redis()),
        dsn=dsn,
        batch_limit=4,
    )
    try:
        report = dispatcher.run_once()
    finally:
        dispatcher.close()

    assert report.published == ()
    assert report.failures == ((message.outbox_id, "BROKER_UNAVAILABLE"),)
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "retry_wait"
    assert row["last_error_code"] == "BROKER_UNAVAILABLE"
    assert row["delivery_attempt_count"] == 1
    assert row["available_at"] >= started
    assert row["lease_expires_at"] is None
    assert _job_snapshot(conn, workspace) == jobs_before  # dispatcher 不改 Job 状态
    outcomes = [event[0] for event in _events(conn, workspace, message.outbox_id)]
    assert outcomes == ["claimed", "retry_wait"]


def test_dispatcher_never_writes_job_execution_state(
    conn: psycopg.Connection, workspace: UUID, dsn: str, broker: redis.Redis
) -> None:
    """C-08 §3：dispatcher 只写 Outbox/审计；Job 的 status/row_version 一律不变。"""

    _, first = _enqueue(conn, workspace, "readonly-1")
    _, second = _enqueue(conn, workspace, "readonly-2")
    conn.commit()
    jobs_before = _job_snapshot(conn, workspace)
    assert len(jobs_before) == 2

    dispatcher = ob.OutboxDispatcher(
        workspace_id=workspace, publisher=ob.RedisPublisher(client=broker), dsn=dsn
    )
    try:
        report = dispatcher.run_once()
    finally:
        dispatcher.close()

    assert len(report.published) == 2
    assert _job_snapshot(conn, workspace) == jobs_before
    assert _outbox_row(conn, workspace, first.outbox_id)["status"] == "dispatched"
    assert _outbox_row(conn, workspace, second.outbox_id)["status"] == "dispatched"
    messages = broker.lrange(ob.REDIS_QUEUE_KEY, 0, -1)
    assert len(messages) == 2
    payloads = [json.loads(item) for item in messages]
    assert {payload["job_id"] for payload in payloads} == {
        str(first.job_id),
        str(second.job_id),
    }
    assert all("workspace_id" in payload for payload in payloads)
    assert all("payload_schema_version" in payload for payload in payloads)


def test_fifty_rounds_commit_rollback_with_redis_outage(
    conn: psycopg.Connection, workspace: UUID, dsn: str, broker: redis.Redis
) -> None:
    """PoC-02 强制项：50 轮 commit/rollback 混合 + 断 Redis 后仍可恢复。

    断言：回滚的 25 条**可执行数为 0**；提交的 25 条各自 1 个 Outbox；断 Redis 后全部
    retry_wait（不丢、不伪成功）；Redis 恢复后全部 dispatched 且无残留状态。
    """

    committed: set[UUID] = set()
    rolled_back: set[UUID] = set()
    for index in range(50):
        _, message = _enqueue(conn, workspace, f"poc02-{index}")
        if index % 2 == 0:
            conn.commit()
            committed.add(message.outbox_id)
        else:
            conn.rollback()
            rolled_back.add(message.outbox_id)

    # available_at 由数据库 now()（事务开始时刻）决定，不能拿循环前的 Python 时钟比较；
    # 取循环结束后的时刻并留 1s 余量，避免应用与数据库的时钟抖动。
    base = _now() + timedelta(seconds=1)

    assert len(committed) == 25
    assert len(rolled_back) == 25
    assert _count(conn, "phase0_jobs", workspace) == 25
    assert _count(conn, "phase0_outbox", workspace) == 25
    assert _count(conn, "phase0_ingest_targets", workspace) == 25
    rollback_left = conn.execute(
        f"SELECT count(*) FROM {ob.SCHEMA}.phase0_outbox "
        "WHERE workspace_id = %s AND outbox_id = ANY(%s::uuid[])",
        (workspace, list(rolled_back)),
    ).fetchone()
    assert rollback_left == (0,)
    claimable = conn.execute(
        f"SELECT count(*) FROM {ob.SCHEMA}.phase0_outbox "
        "WHERE workspace_id = %s AND status IN ('pending', 'retry_wait') "
        "AND available_at <= %s",
        (workspace, base),
    ).fetchone()
    assert claimable == (25,)
    orphan = conn.execute(
        "SELECT count(*) FROM "
        f"{ob.SCHEMA}.phase0_jobs AS j WHERE j.workspace_id = %s AND NOT EXISTS ("
        f"SELECT 1 FROM {ob.SCHEMA}.phase0_outbox AS o "
        "WHERE o.workspace_id = j.workspace_id AND o.job_id = j.job_id)",
        (workspace,),
    ).fetchone()
    assert orphan == (0,)

    cold = ob.OutboxDispatcher(
        workspace_id=workspace, publisher=ob.RedisPublisher(client=_dead_redis()), dsn=dsn,
        batch_limit=50,
    )
    try:
        cold_report = cold.run_once(now=base)
    finally:
        cold.close()
    assert len(cold_report.claimed) == 25
    assert len(cold_report.published) == 0
    assert {code for _, code in cold_report.failures} == {"BROKER_UNAVAILABLE"}
    statuses = conn.execute(
        f"SELECT DISTINCT status FROM {ob.SCHEMA}.phase0_outbox WHERE workspace_id = %s",
        (workspace,),
    ).fetchall()
    assert statuses == [("retry_wait",)]
    assert (
        conn.execute(
            f"SELECT count(*) FROM {ob.SCHEMA}.phase0_outbox "
            "WHERE workspace_id = %s AND last_error_code = 'BROKER_UNAVAILABLE'",
            (workspace,),
        ).fetchone()
        == (25,)
    )

    warm = ob.OutboxDispatcher(
        workspace_id=workspace, publisher=ob.RedisPublisher(client=broker), dsn=dsn,
        batch_limit=50,
    )
    try:
        warm_report = warm.run_once(now=base + timedelta(seconds=120))
    finally:
        warm.close()
    assert len(warm_report.published) == 25
    assert {envelope.outbox_id for envelope in warm_report.published} == committed
    assert {outcome.outcome for outcome in warm_report.outcomes} == {"dispatched"}
    statuses = conn.execute(
        f"SELECT DISTINCT status FROM {ob.SCHEMA}.phase0_outbox WHERE workspace_id = %s",
        (workspace,),
    ).fetchall()
    assert statuses == [("dispatched",)]
    assert len(broker.lrange(ob.REDIS_QUEUE_KEY, 0, -1)) == 25


def test_dispatcher_killed_after_publish_is_recoverable(
    conn: psycopg.Connection, workspace: UUID, broker: redis.Redis
) -> None:
    """发送成功但未标记就崩：消息在队列里，Outbox 停在 dispatching，回收后同身份重发。"""

    _, message = _enqueue(conn, workspace, "kill-1")
    conn.commit()

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD_DISPATCHER, str(PHASE0_DIR), str(workspace)],
        capture_output=True,
        text=True,
        timeout=120,
        env=os.environ.copy(),
        check=False,
    )
    assert completed.returncode == 9, completed.stderr
    assert broker.llen(ob.REDIS_QUEUE_KEY) == 1
    first_message = broker.lrange(ob.REDIS_QUEUE_KEY, 0, -1)[0]

    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "dispatching"  # 消息已发出，但没有 mark
    assert row["delivery_attempt_count"] == 1
    assert row["lease_epoch"] == 1
    assert row["dispatched_at"] is None

    later = _now() + timedelta(seconds=200)
    reclaimed = ob.reclaim_expired_leases(conn, workspace, now=later)
    conn.commit()
    assert [outcome.outcome for outcome in reclaimed] == ["reclaimed"]
    row = _outbox_row(conn, workspace, message.outbox_id)
    assert row["status"] == "retry_wait"
    assert row["lease_epoch"] == 2

    redelivery = ob.claim(
        conn, workspace, limit=1, now=row["available_at"] + timedelta(seconds=1)
    )
    conn.commit()
    assert len(redelivery) == 1
    assert redelivery[0].outbox_id == message.outbox_id
    assert redelivery[0].job_id == message.job_id
    assert redelivery[0].delivery_attempt_count == 2
    envelope = redelivery[0].envelope()
    assert envelope.job_id == message.job_id
    assert envelope.outbox_id == message.outbox_id
    assert envelope.dedup_key == "kill-1"
    ob.RedisPublisher(client=broker).publish(envelope)
    assert ob.mark_dispatched(conn, redelivery[0]).updated is True
    conn.commit()

    second_message = broker.lrange(ob.REDIS_QUEUE_KEY, 0, -1)[0]
    payloads = [json.loads(item) for item in (first_message, second_message)]
    assert [payload["job_id"] for payload in payloads] == [str(message.job_id)] * 2
    assert [payload["outbox_id"] for payload in payloads] == [str(message.outbox_id)] * 2
    assert [payload["delivery_attempt"] for payload in payloads] == [1, 2]
    assert _outbox_row(conn, workspace, message.outbox_id)["status"] == "dispatched"


# ---------------------------------------------------------------------------
# 六、DB 层不变式与查询计划
# ---------------------------------------------------------------------------
def test_transition_matrix_is_enforced_by_database(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """五态转移在 DB 层强制：pending → dispatched（跳过派发的伪成功）必须失败。"""

    assert set(ob.ALLOWED_TRANSITIONS) == {
        ("pending", "dispatching"),
        ("retry_wait", "dispatching"),
        ("dispatching", "dispatched"),
        ("dispatching", "retry_wait"),
        ("dispatching", "dead_letter"),
        ("retry_wait", "dead_letter"),
        ("dead_letter", "pending"),
    }
    assert set(ob.OUTBOX_STATES) == {
        "pending",
        "dispatching",
        "retry_wait",
        "dispatched",
        "dead_letter",
    }

    _, message = _enqueue(conn, workspace, "matrix-1")
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        conn.execute(
            f"UPDATE {ob.SCHEMA}.phase0_outbox SET status = 'dispatched', "
            "dispatched_at = now(), row_version = row_version + 1 "
            "WHERE workspace_id = %s AND outbox_id = %s",
            (workspace, message.outbox_id),
        )
        conn.commit()
    assert "非法 Outbox 状态转移 pending → dispatched" in str(excinfo.value)
    conn.rollback()
    assert _outbox_row(conn, workspace, message.outbox_id)["status"] == "pending"

    # claim 必须换 epoch，否则"旧 worker 一定用旧值"的前提不成立
    with pytest.raises(psycopg.errors.CheckViolation) as epoch_error:
        conn.execute(
            f"UPDATE {ob.SCHEMA}.phase0_outbox SET status = 'dispatching', "
            "lease_expires_at = now() + interval '30 seconds', "
            "row_version = row_version + 1 WHERE workspace_id = %s AND outbox_id = %s",
            (workspace, message.outbox_id),
        )
        conn.commit()
    assert "claim 必须令 lease_epoch + 1" in str(epoch_error.value)
    conn.rollback()


def test_delivery_events_are_append_only(conn: psycopg.Connection, workspace: UUID) -> None:
    """投递审计只追加：UPDATE/DELETE 都被触发器拒绝（证据不可被改写）。"""

    _, message = _enqueue(conn, workspace, "audit-1")
    conn.commit()
    deliveries = ob.claim(conn, workspace, limit=1, now=_now())
    conn.commit()
    assert len(deliveries) == 1
    for statement in (
        f"UPDATE {ob.SCHEMA}.phase0_outbox_delivery_events SET outcome = 'dispatched' "
        "WHERE workspace_id = %s",
        f"DELETE FROM {ob.SCHEMA}.phase0_outbox_delivery_events WHERE workspace_id = %s",
    ):
        with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
            conn.execute(statement, (workspace,))
            conn.commit()
        assert "只允许追加写" in str(excinfo.value)
        conn.rollback()
    events = _events(conn, workspace, message.outbox_id)
    assert [event[0] for event in events] == ["claimed"]


def test_claim_query_plan_uses_partial_index(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """DoD scan query plan：500 条待派发时，claim 走 phase0_outbox_claim_idx 而非全表扫。"""

    conn.execute(
        f"INSERT INTO {ob.SCHEMA}.phase0_ingest_targets (workspace_id, target_id, name) "
        "SELECT %s, gen_random_uuid(), 'seed-' || g FROM generate_series(1, 500) AS g",
        (workspace,),
    )
    conn.execute(
        f"INSERT INTO {ob.SCHEMA}.phase0_jobs (workspace_id, job_id, target_id, kind, dedup_key) "
        "SELECT t.workspace_id, gen_random_uuid(), t.target_id, 'ingest', 'seed-' || t.name "
        f"FROM {ob.SCHEMA}.phase0_ingest_targets AS t WHERE t.workspace_id = %s",
        (workspace,),
    )
    conn.execute(
        f"INSERT INTO {ob.SCHEMA}.phase0_outbox "
        "(workspace_id, outbox_id, job_id, operation, dedup_key) "
        "SELECT j.workspace_id, gen_random_uuid(), j.job_id, 'ingest.execute', j.dedup_key "
        f"FROM {ob.SCHEMA}.phase0_jobs AS j WHERE j.workspace_id = %s",
        (workspace,),
    )
    conn.commit()

    plan = ob.explain_claim(conn, workspace, limit=20, now=_now())
    nodes = list(ob.iter_plan_nodes(plan))
    index_names = [node.get("Index Name") for node in nodes if node.get("Index Name")]
    node_types = [node["Node Type"] for node in nodes]
    assert "phase0_outbox_claim_idx" in index_names, (node_types, index_names)
    seq_scans = [
        node
        for node in nodes
        if node["Node Type"] == "Seq Scan" and node.get("Relation Name") == "phase0_outbox"
    ]
    assert seq_scans == [], (node_types, seq_scans)
    assert "Limit" in node_types

    claimed = ob.claim(conn, workspace, limit=20, now=_now())
    conn.commit()
    assert len(claimed) == 20
    assert all(delivery.lease_epoch == 1 for delivery in claimed)


def test_outbox_states_match_contract_vocabulary(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """五态 CHECK 与契约枚举一致（迁移 003 与 outbox_probe 不漂移）。"""

    _, message = _enqueue(conn, workspace, "vocab-1")
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"UPDATE {ob.SCHEMA}.phase0_outbox SET status = 'succeeded', "
            "row_version = row_version + 1 WHERE workspace_id = %s AND outbox_id = %s",
            (workspace, message.outbox_id),
        )
        conn.commit()
    conn.rollback()
    assert _outbox_row(conn, workspace, message.outbox_id)["status"] == "pending"
