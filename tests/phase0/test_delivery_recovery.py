"""P0-T08 真实验收：消费幂等、旧 epoch CAS、重试/死信、对账修复、Redis 断连。

每条用例对应 Backlog P0-T08 的一项量化验收，全部打真实 PostgreSQL（schema
``p0_t08``）与 Redis（DB 5）；没有 mock、没有 skip/xfail：环境缺失就报错，
因为跳过会让本任务变成假通过。

对照关系
--------
1. ``test_duplicate_delivery_applies_business_effect_once`` —— 同一 Job 重复投递
   （同一幂等键）只产生一次业务效果，第二次记为 duplicate。
2. ``test_stale_generation_cannot_overwrite_result`` —— 旧 generation/epoch 不得覆盖
   新结果，冲突返回 ``STALE_EXECUTION_EPOCH``。
3. ``test_retry_backoff_then_dead_letter_with_reason`` —— 退避 1/2/4s，超过上限进
   ``dead_letter``，对账器可列出并带原因。
4. ``test_reconciler_repairs_published_but_unacked`` +
   ``test_two_reconcilers_do_not_double_repair`` —— 「已发布但长期未确认」被发现并
   修复（``repair_count`` 记一次），并发对账器不重复修，不假定恰好一次投递。
5. ``test_redis_outage_fails_explicitly_and_keeps_delivery_recoverable`` —— 断 Redis
   时消费显式失败（``BROKER_UNAVAILABLE``）并回 ``retry_wait``，不静默丢消息；
   同时证明「业务结果先提交、再 ACK」。

隔离：只写 schema ``p0_t08`` 与 Redis DB 5；Redis 只删除自己的 stream 键，
绝不 FLUSHALL（会伤到其它波次槽）。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import job_probe as jp
import job_reconciler as rec
import psycopg
import pytest
import redis

PHASE0_DIR = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
T07_MIGRATION = PHASE0_DIR / "db" / "migrations" / "003_jobs_outbox.sql"


# ---------------------------------------------------------------------------
# fixtures（conftest.py 属他人文件，DB/Redis fixture 必须写在这里）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def installed_schema() -> Iterator[str]:
    conn = jp.connect()
    try:
        jp.reset_schema(conn)
        digest = jp.ensure_schema(conn)
    finally:
        conn.close()
    assert len(digest) == 64
    yield jp.SCHEMA
    conn = jp.connect()
    try:
        jp.reset_schema(conn)
    finally:
        conn.close()


@pytest.fixture()
def conn(installed_schema: str) -> Iterator[psycopg.Connection]:
    connection = jp.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def broker(installed_schema: str) -> Iterator[redis.Redis]:
    client = jp.redis_client()
    jp.reset_stream(client)
    jp.ensure_stream(client)
    try:
        yield client
    finally:
        jp.reset_stream(client)
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
    workspace: UUID,
    dedup_key: str,
    **overrides: object,
) -> jp.EnqueuedJob:
    request = jp.EnqueueRequest(
        workspace_id=workspace,
        target_id=uuid4(),
        job_id=uuid4(),
        dedup_key=dedup_key,
        target_name=f"target-{dedup_key}",
        **overrides,  # type: ignore[arg-type]
    )
    return jp.enqueue(conn, request)


def _delivery_row(conn: psycopg.Connection, workspace: UUID, delivery_id: UUID) -> dict:
    row = conn.execute(
        "SELECT status, lease_epoch, delivery_attempt_count, last_error_code, "
        "       published_at, acked_at, repair_count, available_at, row_version "
        f"FROM {jp.SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s AND delivery_id = %s",
        (workspace, delivery_id),
    ).fetchone()
    assert row is not None, f"投递 {delivery_id} 不存在"
    return dict(row)


def _target_row(conn: psycopg.Connection, workspace: UUID, target_id: UUID) -> dict:
    row = conn.execute(
        "SELECT applied_version, applied_generation, desired_version "
        f"FROM {jp.SCHEMA}.phase0_ingest_targets "
        "WHERE workspace_id = %s AND target_id = %s",
        (workspace, target_id),
    ).fetchone()
    assert row is not None
    return dict(row)


def _job_generation(conn: psycopg.Connection, workspace: UUID, job_id: UUID) -> int:
    row = conn.execute(
        f"SELECT execution_generation FROM {jp.SCHEMA}.phase0_ingest_jobs "
        "WHERE workspace_id = %s AND job_id = %s",
        (workspace, job_id),
    ).fetchone()
    assert row is not None
    return int(row["execution_generation"])


def _count(conn: psycopg.Connection, table: str, workspace: UUID) -> int:
    assert table in {
        "phase0_ingest_targets",
        "phase0_ingest_jobs",
        "phase0_deliveries",
        "phase0_consumption_receipts",
        "phase0_job_results",
        "phase0_delivery_events",
    }
    row = conn.execute(
        f"SELECT count(*) AS n FROM {jp.SCHEMA}.{table} WHERE workspace_id = %s",
        (workspace,),
    ).fetchone()
    assert row is not None
    return int(row["n"])


def _outcomes(conn: psycopg.Connection, workspace: UUID, delivery_id: UUID) -> list[str]:
    rows = conn.execute(
        "SELECT outcome FROM "
        f"{jp.SCHEMA}.phase0_delivery_events "
        "WHERE workspace_id = %s AND delivery_id = %s ORDER BY event_seq",
        (workspace, delivery_id),
    ).fetchall()
    return [row["outcome"] for row in rows]


def _claim_one(
    conn: psycopg.Connection,
    workspace: UUID,
    *,
    now: datetime | None = None,
    lease_seconds: float = jp.DEFAULT_LEASE_SECONDS,
) -> jp.ClaimedDelivery:
    claimed = jp.claim(conn, workspace, limit=1, now=now or _now(),
                       lease_seconds=lease_seconds)
    conn.commit()
    assert len(claimed) == 1, f"期望领到 1 条，实际 {len(claimed)}"
    return claimed[0]


def _dispatch_and_commit(
    conn: psycopg.Connection, broker: redis.Redis, delivery: jp.ClaimedDelivery,
    *, now: datetime | None = None,
) -> jp.TransportReceipt:
    receipt = jp.publish(broker, delivery)
    outcome = jp.mark_published(conn, delivery, now=now or _now())
    conn.commit()
    assert outcome.updated is True, outcome
    return receipt


def _read_one(broker: redis.Redis, consumer: str = "w1") -> jp.DeliveryMessage:
    messages = jp.read_messages(broker, consumer=consumer, count=10)
    assert len(messages) == 1, f"期望 1 条消息，实际 {len(messages)}"
    return messages[0]


def _read_all(broker: redis.Redis, consumer: str, expected: int) -> list[jp.DeliveryMessage]:
    collected: list[jp.DeliveryMessage] = []
    while len(collected) < expected:
        batch = jp.read_messages(broker, consumer=consumer, count=min(200, expected))
        if not batch:
            break
        collected.extend(batch)
    return collected


def _dead_redis() -> redis.Redis:
    return redis.Redis.from_url(
        "redis://127.0.0.1:1/5",
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )


# ---------------------------------------------------------------------------
# 验收 1：重复投递只产生一次业务效果
# ---------------------------------------------------------------------------
def test_duplicate_delivery_applies_business_effect_once(
    conn: psycopg.Connection, broker: redis.Redis, workspace: UUID
) -> None:
    enqueued = _enqueue(conn, workspace, "dup-1")
    conn.commit()

    delivery = _claim_one(conn, workspace)
    assert delivery.execution_generation == 1
    _dispatch_and_commit(conn, broker, delivery)

    first = jp.consume_message(conn, _read_one(broker), consumer_id="w1", broker=broker)
    assert first.outcome == "applied"
    assert first.business_applied is True
    assert first.duplicate is False
    assert _target_row(conn, workspace, enqueued_delivery_target(conn, workspace,
                                                                 enqueued.job_id))[
        "applied_version"
    ] == 1
    assert _count(conn, "phase0_consumption_receipts", workspace) == 1
    assert _count(conn, "phase0_job_results", workspace) == 1

    # broker 重投同一条消息（同一 delivery/epoch，新 stream id）：必须只记 duplicate。
    jp.publish(broker, delivery)
    second = jp.consume_message(conn, _read_one(broker), consumer_id="w1", broker=broker)
    assert second.outcome == "duplicate"
    assert second.duplicate is True
    assert second.business_applied is False

    assert _count(conn, "phase0_consumption_receipts", workspace) == 1
    assert _count(conn, "phase0_job_results", workspace) == 1
    assert _target_row(conn, workspace, enqueued_delivery_target(conn, workspace,
                                                                 enqueued.job_id))[
        "applied_version"
    ] == 1
    row = _delivery_row(conn, workspace, delivery.delivery_id)
    assert row["status"] == "dispatched"
    assert row["acked_at"] is not None
    assert _outcomes(conn, workspace, delivery.delivery_id) == [
        "claimed",
        "applied",
        "acked",
        "duplicate",
    ]


def enqueued_delivery_target(
    conn: psycopg.Connection, workspace: UUID, job_id: UUID
) -> UUID:
    row = conn.execute(
        f"SELECT target_id FROM {jp.SCHEMA}.phase0_ingest_jobs "
        "WHERE workspace_id = %s AND job_id = %s",
        (workspace, job_id),
    ).fetchone()
    assert row is not None
    return row["target_id"]


# ---------------------------------------------------------------------------
# 验收 2：旧 epoch 不得覆盖新结果
# ---------------------------------------------------------------------------
def test_stale_generation_cannot_overwrite_result(
    conn: psycopg.Connection, broker: redis.Redis, workspace: UUID
) -> None:
    enqueued = _enqueue(conn, workspace, "stale-1")
    conn.commit()

    # 终态重试把 Job 推进到 generation 2（C-09：新 job_id/new generation）。
    conn.execute(
        f"UPDATE {jp.SCHEMA}.phase0_ingest_jobs "
        "SET execution_generation = 2, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND job_id = %s",
        (workspace, enqueued.job_id),
    )
    conn.commit()

    stale = jp.record_result(
        conn, workspace, enqueued.job_id, generation=1, outcome="succeeded",
        applied_version=9,
    )
    assert stale.recorded is False
    assert stale.error_code == jp.STALE_EXECUTION_EPOCH

    fresh = jp.record_result(
        conn, workspace, enqueued.job_id, generation=2, outcome="succeeded",
        applied_version=1,
    )
    assert fresh.recorded is True

    # 同一 generation 再来一次：duplicate（终态不覆盖、不复活）。
    again = jp.record_result(
        conn, workspace, enqueued.job_id, generation=2, outcome="succeeded",
        applied_version=7,
    )
    assert again.recorded is False
    assert again.outcome == "duplicate"

    rows = conn.execute(
        "SELECT execution_generation, outcome, applied_version "
        f"FROM {jp.SCHEMA}.phase0_job_results WHERE workspace_id = %s",
        (workspace,),
    ).fetchall()
    assert [(row["execution_generation"], row["applied_version"]) for row in rows] == [(2, 1)]

    # 消费路径同样受保护：claim 后才推进 generation → 旧 worker 只能 stale。
    second = _enqueue(conn, workspace, "stale-2")
    conn.commit()
    delivery = _claim_one(conn, workspace)
    _dispatch_and_commit(conn, broker, delivery)
    message = _read_one(broker)
    conn.execute(
        f"UPDATE {jp.SCHEMA}.phase0_ingest_jobs "
        "SET execution_generation = 2, row_version = row_version + 1 "
        "WHERE workspace_id = %s AND job_id = %s",
        (workspace, second.job_id),
    )
    conn.commit()

    outcome = jp.consume_message(conn, message, consumer_id="w1", broker=broker)
    assert outcome.outcome == "stale"
    assert outcome.stale is True
    assert outcome.error_code == jp.STALE_EXECUTION_EPOCH
    assert outcome.business_applied is False
    receipts = conn.execute(
        f"SELECT count(*) AS n FROM {jp.SCHEMA}.phase0_consumption_receipts "
        "WHERE workspace_id = %s AND job_id = %s",
        (workspace, second.job_id),
    ).fetchone()
    assert receipts == {"n": 0}
    assert "stale" in _outcomes(conn, workspace, delivery.delivery_id)


# ---------------------------------------------------------------------------
# 验收 3：重试上限、退避与死信原因
# ---------------------------------------------------------------------------
def test_retry_backoff_then_dead_letter_with_reason(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    enqueued = _enqueue(conn, workspace, "retry-1", max_delivery_attempts=3)
    conn.commit()

    assert jp.backoff_delay_seconds(1) == 1.0
    assert jp.backoff_delay_seconds(2) == 2.0
    assert jp.backoff_delay_seconds(3) == 4.0
    assert jp.backoff_delay_seconds(10) == jp.BACKOFF_CAP_SECONDS

    now = _now()
    for expected_attempt, expected_delay in ((1, 1.0), (2, 2.0)):
        delivery = jp.claim(conn, workspace, limit=1, now=now)
        conn.commit()
        assert len(delivery) == 1
        assert delivery[0].delivery_attempt_count == expected_attempt
        outcome = jp.retry(conn, delivery[0], error_code="CONSUMER_ERROR", now=now)
        conn.commit()
        assert outcome.status == "retry_wait"
        assert outcome.available_at == now + timedelta(seconds=expected_delay)
        now = outcome.available_at + timedelta(milliseconds=1)

    third = jp.claim(conn, workspace, limit=1, now=now)
    conn.commit()
    assert third[0].delivery_attempt_count == 3
    exhausted = jp.retry(conn, third[0], error_code="CONSUMER_ERROR", now=now)
    conn.commit()
    assert exhausted.status == "dead_letter"
    assert exhausted.error_code == jp.RETRY_EXHAUSTED

    row = _delivery_row(conn, workspace, enqueued.delivery_id)
    assert row["status"] == "dead_letter"
    assert row["last_error_code"] == jp.RETRY_EXHAUSTED
    assert jp.claim(conn, workspace, limit=1, now=now + timedelta(hours=1)) == []

    dead = rec.list_dead_letters(conn, workspace)
    assert len(dead) == 1
    assert dead[0].delivery_id == enqueued.delivery_id
    assert dead[0].reason == jp.RETRY_EXHAUSTED
    assert dead[0].delivery_attempt_count == 3
    assert dead[0].max_delivery_attempts == 3
    assert "dead_letter" in _outcomes(conn, workspace, enqueued.delivery_id)


# ---------------------------------------------------------------------------
# 验收 4：对账器修复「已发布但长期未确认」
# ---------------------------------------------------------------------------
def test_reconciler_repairs_published_but_unacked(
    conn: psycopg.Connection, broker: redis.Redis, workspace: UUID
) -> None:
    enqueued = [_enqueue(conn, workspace, f"unacked-{index}") for index in range(3)]
    conn.commit()

    start = _now()
    made: list[jp.ClaimedDelivery] = []
    for _ in range(3):
        delivery = jp.claim(conn, workspace, limit=1, now=start, lease_seconds=1.0)
        conn.commit()
        assert len(delivery) == 1
        jp.publish(broker, delivery[0])  # 已发出
        jp.mark_published(conn, delivery[0], now=start)  # 但永远没有 ACK
        conn.commit()
        made.append(delivery[0])

    later = start + timedelta(seconds=30)
    stuck = rec.list_stuck_deliveries(conn, workspace, now=later, grace_seconds=5.0)
    assert len(stuck) == 3
    assert all(item["published_at"] is not None for item in stuck)

    outcomes = rec.repair_stuck_deliveries(conn, workspace, now=later, grace_seconds=5.0)
    conn.commit()
    assert len(outcomes) == 3
    assert all(item.repaired for item in outcomes)
    assert {item.reason for item in outcomes} == {"published_but_unacked"}
    for item in outcomes:
        assert item.repair_count == 1
        assert item.status == "retry_wait"

    # 幂等：同一时刻再跑一遍没有任何可修的
    assert rec.repair_stuck_deliveries(conn, workspace, now=later, grace_seconds=5.0) == []

    for delivery in made:
        row = _delivery_row(conn, workspace, delivery.delivery_id)
        assert row["status"] == "retry_wait"
        assert row["repair_count"] == 1
        assert row["last_error_code"] == jp.RECONCILE_UNACKED
        assert row["lease_epoch"] == delivery.lease_epoch + 1
        assert "repaired" in _outcomes(conn, workspace, delivery.delivery_id)

    # 修复后可以重新领取；broker 里同时有修复前发的旧消息（epoch=1）与重发的
    # 新消息（epoch=3）：旧 epoch 必须被当成 stale 拒绝，新 epoch 才产生业务效果。
    resumed = jp.claim(conn, workspace, limit=10, now=later + timedelta(seconds=1))
    conn.commit()
    assert len(resumed) == 3
    for delivery in resumed:
        assert delivery.lease_epoch == 3
        _dispatch_and_commit(conn, broker, delivery)
    messages = _read_all(broker, "w1", expected=6)
    assert len(messages) == 6
    stale = 0
    applied = 0
    for message in messages:
        outcome = jp.consume_message(conn, message, consumer_id="w1", broker=broker)
        stale += int(outcome.stale)
        applied += int(outcome.business_applied)
    assert stale == 3
    assert applied == 3
    assert _count(conn, "phase0_consumption_receipts", workspace) == 3
    assert _count(conn, "phase0_job_results", workspace) == 3
    # 再重复投递同一批 delivery：只能被识别为 duplicate。
    for delivery in resumed:
        jp.publish(broker, delivery)
    replay = _read_all(broker, "w1", expected=3)
    assert len(replay) == 3
    duplicates = 0
    for message in replay:
        outcome = jp.consume_message(conn, message, consumer_id="w1", broker=broker)
        duplicates += int(outcome.duplicate)
        assert outcome.business_applied is False
    assert duplicates == 3
    assert _count(conn, "phase0_consumption_receipts", workspace) == 3
    assert _count(conn, "phase0_job_results", workspace) == 3
    for item in enqueued:
        row = _delivery_row(conn, workspace, item.delivery_id)
        assert row["status"] == "dispatched"
        assert row["repair_count"] == 1


def test_reconciler_handles_claimed_but_unpublished(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    """claim 后 worker 立刻死的窗口：没有 published_at，对账器仍须回收并标原因。"""

    enqueued = _enqueue(conn, workspace, "claimed-dead-1")
    conn.commit()
    start = _now()
    delivery = jp.claim(conn, workspace, limit=1, now=start, lease_seconds=1.0)
    conn.commit()
    assert len(delivery) == 1  # 只领取，绝不 publish：模拟 claim 后进程消失

    later = start + timedelta(seconds=30)
    stuck = rec.list_stuck_deliveries(conn, workspace, now=later, grace_seconds=5.0)
    assert len(stuck) == 1
    assert stuck[0]["published_at"] is None

    outcomes = rec.repair_stuck_deliveries(conn, workspace, now=later, grace_seconds=5.0)
    conn.commit()
    assert len(outcomes) == 1
    assert outcomes[0].reason == "claimed_but_unpublished"
    assert outcomes[0].status == "retry_wait"
    row = _delivery_row(conn, workspace, enqueued.delivery_id)
    assert row["repair_count"] == 1
    assert row["last_error_code"] == jp.RECONCILE_UNACKED
    # 回收后可以正常重派（epoch 再 +1）
    again = jp.claim(conn, workspace, limit=1, now=later + timedelta(seconds=1))
    conn.commit()
    assert len(again) == 1
    assert again[0].lease_epoch == outcomes[0].lease_epoch + 1


def test_two_reconcilers_do_not_double_repair(
    conn: psycopg.Connection, broker: redis.Redis, workspace: UUID
) -> None:
    enqueued = [_enqueue(conn, workspace, f"race-{index}") for index in range(4)]
    conn.commit()
    start = _now()
    for _ in range(4):
        delivery = jp.claim(conn, workspace, limit=1, now=start, lease_seconds=1.0)
        conn.commit()
        assert len(delivery) == 1
        jp.publish(broker, delivery[0])
        jp.mark_published(conn, delivery[0], now=start)
        conn.commit()

    later = start + timedelta(seconds=30)
    barrier = threading.Barrier(2)
    repaired: list[rec.RepairOutcome] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def reconcile_worker() -> None:
        connection = jp.connect()
        try:
            barrier.wait(timeout=15)
            with connection.transaction():
                results = rec.repair_stuck_deliveries(
                    connection, workspace, now=later, grace_seconds=5.0
                )
            with lock:
                repaired.extend(results)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常必须回传
            with lock:
                errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=reconcile_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(repaired) == 4
    assert len({item.delivery_id for item in repaired}) == 4
    for item in enqueued:
        row = _delivery_row(conn, workspace, item.delivery_id)
        assert row["repair_count"] == 1  # 只修一次


# ---------------------------------------------------------------------------
# 验收 5：断 Redis 时显式失败、不丢消息
# ---------------------------------------------------------------------------
def test_redis_outage_fails_explicitly_and_keeps_delivery_recoverable(
    conn: psycopg.Connection, broker: redis.Redis, workspace: UUID
) -> None:
    enqueued = _enqueue(conn, workspace, "outage-1")
    conn.commit()
    delivery = _claim_one(conn, workspace)
    _dispatch_and_commit(conn, broker, delivery)
    message = _read_one(broker)

    with pytest.raises(jp.BrokerUnavailable) as excinfo:
        jp.consume_message(conn, message, consumer_id="w1", broker=_dead_redis())
    assert excinfo.value.code == jp.BROKER_UNAVAILABLE

    # 显式失败：投递回 retry_wait 且原因可见，消息没有从 broker 消失（PEL 仍挂着）。
    row = _delivery_row(conn, workspace, delivery.delivery_id)
    assert row["status"] == "retry_wait"
    assert row["last_error_code"] == jp.BROKER_UNAVAILABLE
    assert row["acked_at"] is None
    assert jp.pending_count(broker) == 1
    outcomes = _outcomes(conn, workspace, delivery.delivery_id)
    assert outcomes == ["claimed", "applied", "requeued"]

    # 「结果先提交、再 ACK」：ACK 失败不影响已提交的业务事实。
    assert _count(conn, "phase0_consumption_receipts", workspace) == 1
    assert _count(conn, "phase0_job_results", workspace) == 1
    target = enqueued_delivery_target(conn, workspace, enqueued.job_id)
    assert _target_row(conn, workspace, target)["applied_version"] == 1

    # broker 恢复：重新领取、重新投递、消费识别为 duplicate，最终 dispatched。
    resumed_at = row["available_at"] + timedelta(milliseconds=1)
    again = jp.claim(conn, workspace, limit=1, now=resumed_at)
    conn.commit()
    assert len(again) == 1
    assert again[0].lease_epoch == delivery.lease_epoch + 1
    _dispatch_and_commit(conn, broker, again[0], now=resumed_at)
    recovered = jp.consume_message(conn, _read_one(broker), consumer_id="w1", broker=broker)
    assert recovered.outcome == "duplicate"
    assert recovered.business_applied is False
    assert _count(conn, "phase0_consumption_receipts", workspace) == 1
    final = _delivery_row(conn, workspace, delivery.delivery_id)
    assert final["status"] == "dispatched"
    assert final["acked_at"] is not None
    assert final["last_error_code"] is None


# ---------------------------------------------------------------------------
# 死信 redrive：可恢复但绝不复活已成功作业
# ---------------------------------------------------------------------------
def test_dead_letter_redrive_and_terminal_guard(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    enqueued = _enqueue(conn, workspace, "redrive-1", max_delivery_attempts=1)
    conn.commit()
    now = _now()
    delivery = jp.claim(conn, workspace, limit=1, now=now)
    conn.commit()
    dead = jp.retry(conn, delivery[0], error_code="POISON", now=now)
    conn.commit()
    assert dead.status == "dead_letter"

    row = _delivery_row(conn, workspace, enqueued.delivery_id)
    conflict = rec.redrive_dead_letter(
        conn,
        workspace_id=workspace,
        delivery_id=enqueued.delivery_id,
        expected_row_version=int(row["row_version"]) - 1,
        reason="ops retry",
        now=now,
    )
    assert conflict.updated is False
    assert conflict.error_code == "VERSION_CONFLICT"

    redriven = rec.redrive_dead_letter(
        conn,
        workspace_id=workspace,
        delivery_id=enqueued.delivery_id,
        expected_row_version=int(row["row_version"]),
        reason="ops retry after broker fix",
        now=now,
    )
    conn.commit()
    assert redriven.updated is True
    assert redriven.status == "pending"
    assert redriven.redrive_count == 1
    row = _delivery_row(conn, workspace, enqueued.delivery_id)
    assert row["status"] == "pending"
    assert "redrive" in _outcomes(conn, workspace, enqueued.delivery_id)

    # 已成功作业不复活：先制造 succeeded 结果，再让另一条投递进死信。
    second = _enqueue(conn, workspace, "terminal-1", max_delivery_attempts=1)
    conn.commit()
    result = jp.record_result(
        conn, workspace, second.job_id, generation=1, outcome="succeeded",
        applied_version=1,
    )
    assert result.recorded is True
    later_now = _now()
    claimed = jp.claim(conn, workspace, limit=10, now=later_now)
    conn.commit()
    target = next(item for item in claimed if item.delivery_id == second.delivery_id)
    terminated = jp.retry(conn, target, error_code="POISON", now=later_now)
    conn.commit()
    assert terminated.status == "dead_letter"
    assert _delivery_row(conn, workspace, second.delivery_id)["status"] == "dead_letter"
    row = _delivery_row(conn, workspace, second.delivery_id)
    blocked = rec.redrive_dead_letter(
        conn,
        workspace_id=workspace,
        delivery_id=second.delivery_id,
        expected_row_version=int(row["row_version"]),
        reason="must not revive",
        now=now,
    )
    conn.commit()
    assert blocked.updated is False
    assert blocked.error_code == jp.TERMINAL_JOB_NO_OP
    assert _delivery_row(conn, workspace, second.delivery_id)["status"] == "dead_letter"


# ---------------------------------------------------------------------------
# 契约与不变式
# ---------------------------------------------------------------------------
def test_five_states_match_t07_migration(conn: psycopg.Connection, workspace: UUID) -> None:
    assert jp.DELIVERY_STATES == (
        "pending",
        "dispatching",
        "retry_wait",
        "dispatched",
        "dead_letter",
    )
    assert jp.ALLOWED_TRANSITIONS == (
        ("pending", "dispatching"),
        ("retry_wait", "dispatching"),
        ("dispatching", "dispatched"),
        ("dispatching", "retry_wait"),
        ("dispatching", "dead_letter"),
        ("retry_wait", "dead_letter"),
        ("dead_letter", "pending"),
    )

    sql_text = T07_MIGRATION.read_text(encoding="utf-8")
    matches = re.findall(r"CHECK \(status IN \(([^)]*)\)", sql_text, re.S)
    five_state = [tuple(re.findall(r"'([a-z_]+)'", item)) for item in matches]
    five_state = [states for states in five_state if "dispatching" in states]
    assert len(five_state) == 1, f"003 里应恰有一处五态 CHECK，实际 {len(five_state)}"
    assert five_state[0] == jp.DELIVERY_STATES  # 同一套枚举，不是第二套状态机

    # DB 层拒绝把非五态值写进投递状态。
    enqueued = _enqueue(conn, workspace, "vocab-1")
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"UPDATE {jp.SCHEMA}.phase0_deliveries SET status = 'succeeded', "
            "row_version = row_version + 1 WHERE workspace_id = %s AND delivery_id = %s",
            (workspace, enqueued.delivery_id),
        )
        conn.commit()
    conn.rollback()
    assert _delivery_row(conn, workspace, enqueued.delivery_id)["status"] == "pending"


def test_schema_install_is_idempotent_and_events_append_only(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    first = jp.ensure_schema(conn)
    second = jp.ensure_schema(conn)
    assert first == second == jp.schema_digest()

    enqueued = _enqueue(conn, workspace, "audit-1")
    conn.commit()
    # available_at 由数据库 now() 生成，留 1s 余量避免应用/数据库时钟抖动。
    claimed = jp.claim(conn, workspace, limit=1, now=_now() + timedelta(seconds=1))
    conn.commit()
    assert len(claimed) == 1  # 没有事件行时下面的 append-only 断言会假通过

    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        conn.execute(
            f"UPDATE {jp.SCHEMA}.phase0_delivery_events SET outcome = 'acked' "
            "WHERE workspace_id = %s",
            (workspace,),
        )
        conn.commit()
    assert "只允许追加写" in str(excinfo.value)
    conn.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            f"DELETE FROM {jp.SCHEMA}.phase0_delivery_events WHERE workspace_id = %s",
            (workspace,),
        )
        conn.commit()
    conn.rollback()
    assert "claimed" in _outcomes(conn, workspace, enqueued.delivery_id)


def test_two_workers_claim_disjoint_deliveries(
    conn: psycopg.Connection, workspace: UUID
) -> None:
    for index in range(10):
        _enqueue(conn, workspace, f"workers-{index}")
    conn.commit()

    claimed: list[UUID] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def worker() -> None:
        connection = jp.connect()
        try:
            barrier.wait(timeout=15)
            for _ in range(100):
                with connection.transaction():
                    batch = jp.claim(connection, workspace, limit=3, now=_now())
                with lock:
                    claimed.extend(item.delivery_id for item in batch)
                    done = len(claimed)
                if not batch and done >= 10:
                    return
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(claimed) == 10
    assert len(set(claimed)) == 10


# ---------------------------------------------------------------------------
# celery_app 入口与资源槽保护
# ---------------------------------------------------------------------------
def test_celery_app_entry_and_redis_slot_guard(broker: redis.Redis) -> None:
    assert jp.celery_app.main == "pyknora.ingest"
    assert jp.celery_app.is_shim is True  # 真实 Celery 未准入，见报告
    assert set(jp.celery_app.tasks) == {
        "pyknora.ingest.consume",
        "pyknora.ingest.reconcile",
    }
    assert jp.celery_app.conf["task_acks_late"] is True

    delivery_id = str(uuid4())
    stream_id = jp.celery_app.send_task(
        "pyknora.ingest.consume", kwargs={"delivery_id": delivery_id}, client=broker
    )
    assert stream_id
    assert broker.xlen(jp.STREAM_KEY) == 1
    assert jp.celery_app.broker_url.endswith("/5")

    with pytest.raises(jp.JobProbeError):
        jp.celery_app.send_task("pyknora.ingest.unknown", client=broker)

    other_slot = redis.Redis.from_url("redis://127.0.0.1:6380/0", decode_responses=True)
    try:
        with pytest.raises(jp.JobProbeError) as excinfo:
            jp.ensure_transport_slot(other_slot)
        assert excinfo.value.code == "WRONG_REDIS_SLOT"
        with pytest.raises(jp.JobProbeError):
            jp.reset_stream(other_slot)
    finally:
        other_slot.close()


# ---------------------------------------------------------------------------
# 卡片量化项：100 Job × 10 条消息 → 每 Job 恰一次业务结果
# ---------------------------------------------------------------------------
def test_hundred_jobs_times_ten_messages_yield_one_result_each(
    conn: psycopg.Connection, broker: redis.Redis, workspace: UUID
) -> None:
    job_count = 100
    duplicates_per_job = 10

    for index in range(job_count):
        _enqueue(conn, workspace, f"bulk-{index}")
    conn.commit()
    assert _count(conn, "phase0_deliveries", workspace) == job_count

    now = _now()
    claimed: list[jp.ClaimedDelivery] = []
    while len(claimed) < job_count:
        batch = jp.claim(conn, workspace, limit=50, now=now)
        if not batch:
            break
        claimed.extend(batch)
        for delivery in batch:
            jp.publish(broker, delivery)
            jp.mark_published(conn, delivery, now=now)
        conn.commit()
    assert len(claimed) == job_count

    # 每条投递再重复 9 次（同一 delivery/epoch，模拟 broker 重投与 at-least-once）。
    for delivery in claimed:
        for _ in range(duplicates_per_job - 1):
            jp.publish(broker, delivery)

    messages = _read_all(broker, "w1", expected=job_count * duplicates_per_job)
    assert len(messages) == job_count * duplicates_per_job

    applied = 0
    duplicated = 0
    for message in messages:
        outcome = jp.consume_message(conn, message, consumer_id="w1", broker=broker)
        applied += int(outcome.business_applied)
        duplicated += int(outcome.duplicate)
    assert applied == job_count
    assert duplicated == job_count * (duplicates_per_job - 1)

    assert _count(conn, "phase0_consumption_receipts", workspace) == job_count
    assert _count(conn, "phase0_job_results", workspace) == job_count
    residuals = conn.execute(
        f"SELECT status, count(*) AS n FROM {jp.SCHEMA}.phase0_deliveries "
        "WHERE workspace_id = %s GROUP BY status",
        (workspace,),
    ).fetchall()
    assert {row["status"]: int(row["n"]) for row in residuals} == {"dispatched": job_count}
    applied_rows = conn.execute(
        f"SELECT DISTINCT applied_version FROM {jp.SCHEMA}.phase0_ingest_targets "
        "WHERE workspace_id = %s",
        (workspace,),
    ).fetchall()
    assert [row["applied_version"] for row in applied_rows] == [1]
