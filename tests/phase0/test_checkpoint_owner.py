"""P0-T05：原生 checkpoint 的单写者与并发恢复验收。

这组用例针对 ``experiments/phase0/checkpoint_probe.py`` 暴露的实验 API，逐条覆盖
Backlog P0-T05 的量化验收：至多一个有效 writer、``run/thread`` 绑定 100% 一致、
≥50 轮失锁→重连、并发争抢、连接中断重连、恢复后读到最新一致 checkpoint。

关键点在于**对照组**：``fencing=False`` 的实验不是"另一个测试"，而是证明
"只靠 advisory lock 释放 / lease 过期并不能阻止旧 writer"（契约 C-03 的缺口），
再由 ``fencing=True`` 证明 DB 侧 epoch CAS 能补上这个缺口。没有对照组，"被拒"
可能只是恰好没跑去写。
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import checkpoint_probe as probe  # noqa: E402

PROBE_SOURCE = PROJECT / "checkpoint_probe.py"
CHURN_ROUNDS = 50


@pytest.fixture(scope="module")
def harness() -> Iterator[probe.Harness]:
    """独占 ``p0_t05`` schema 的探针环境；模块开始时重建，结束时释放连接。"""

    with closing(probe.Harness()) as instance:
        instance.reset_schema()
        yield instance


# ---------------------------------------------------------------------------
# 验收 2：run/thread 绑定一致率 100%
# ---------------------------------------------------------------------------
def test_run_thread_binding_is_one_to_one_at_100_percent(harness: probe.Harness) -> None:
    report = probe.run_binding_consistency(harness, runs=100)

    assert report.runs == 100
    assert report.consistency.checked == 100
    assert report.consistency.consistent == 100
    assert report.consistency.rate == 100.0
    assert report.consistency.mismatches == ()
    # 三种非法重绑都必须被拒：同 thread 换 run、同 run 换 thread、重复绑定
    assert report.rebind_attempts == 300
    assert report.rebind_rejections == 300


# ---------------------------------------------------------------------------
# 验收 1 + 2：50 轮失锁→重连，每轮旧 writer 被拒、新 writer 能写、序列不回退
# ---------------------------------------------------------------------------
def test_fifty_rounds_lock_loss_and_reconnect_keep_single_writer(
    harness: probe.Harness,
) -> None:
    report = probe.run_owner_churn_cycles(harness, rounds=CHURN_ROUNDS, fencing=True)

    assert report.rounds == CHURN_ROUNDS
    assert len(report.records) == CHURN_ROUNDS
    for record in report.records:
        # 旧 invocation 的锁连接已经被 terminate，它的写入必须被判过期
        assert record.stale_code == probe.CODE_STALE_EPOCH, f"round {record.round}"
        # 新 writer 接上后必须能写
        assert record.fresh_code == probe.CODE_ACCEPTED, f"round {record.round}"
        # 每轮 epoch 恰好 +1：旧的失锁、新的唯一接管
        assert record.epoch_after == record.epoch_before + 1
        assert record.invocation_after != record.invocation_before
        # checkpoint 序列只前进一格，没有回退也没有空洞
        assert record.fresh_seq == record.seq_before + 1, f"round {record.round}"
        # 接管完成后锁的持有者是唯一的；kill 之后是否立刻归零取决于 PG 回收
        # backend 的时机（terminate 是异步的），所以只在这里断言上界。
        assert record.lock_holders_after_acquire == 1, f"round {record.round}"
        assert record.lock_holders_after_kill in (0, 1)
        assert record.lock_wait_ms >= 0.0

    assert report.accepted == CHURN_ROUNDS + 1
    assert report.rejected == CHURN_ROUNDS
    assert report.final_seq == CHURN_ROUNDS + 1
    assert report.expected_final_seq == report.final_seq
    assert report.sequence_violations == ()
    assert report.epoch_violations == ()
    # 每轮都用**新连接**回读，50 次全部落在最后被接受的那条上
    assert report.restore_checks == CHURN_ROUNDS
    assert report.restore_mismatches == ()
    harness.assert_single_live_writer(report.binding)
    # 收尾时所有 invocation 都已 close，因此"有效 writer"归零才是对的；
    # 不变量的内容是"任何时刻都不超过 1"，由上面的逐轮 lock_holders 断言覆盖。
    assert harness.live_writers(report.binding) == 0


# ---------------------------------------------------------------------------
# 验收 1 的对照组：没有 DB 侧 fence 时，旧 writer 照样写得进去（C-03 缺口）
# ---------------------------------------------------------------------------
def test_without_db_fence_the_stale_writer_still_writes(harness: probe.Harness) -> None:
    control = probe.run_owner_churn_cycles(harness, rounds=10, fencing=False)

    stale_accepted = [r for r in control.records if r.stale_code == probe.CODE_ACCEPTED]
    assert len(stale_accepted) == 10
    # 锁早已被 terminate、epoch 早已被推进，旧 writer 仍然全部写入成功
    assert all(r.lock_holders_after_kill == 0 for r in control.records)
    assert control.rejected == 0
    # 对照组同时说明：checkpoint 序列的 epoch 非降序**不足以**发现旧 writer
    assert control.epoch_violations == ()


def test_epoch_cas_protects_when_the_lock_is_bypassed(harness: probe.Harness) -> None:
    report = probe.run_epoch_race(harness, contenders=4)

    # 4 个 host 绕开锁直接 CAS 抢 owner_epoch：只允许一个赢
    assert len(report.claim_winners) == 1
    assert sorted(report.claim_loser_codes) == [probe.CODE_OWNER_CONFLICT] * 3
    assert report.write_codes == (probe.CODE_ACCEPTED,)
    assert report.checkpoint_count == 1
    assert report.epoch_violations == ()
    assert report.final_epoch == 1


# ---------------------------------------------------------------------------
# 验收 3：两个 host 并发争抢同一 thread
# ---------------------------------------------------------------------------
def test_two_writers_race_for_the_same_thread(harness: probe.Harness) -> None:
    report = probe.run_two_writer_contention(harness, contenders=2)

    # 阶段一：advisory lock 恰好一个赢家
    assert len(report.lock_winners) == 1
    assert len(report.lock_losers) == 1
    assert report.lock_loser_codes == (probe.CODE_LOCK_HELD,)
    # 阶段二：赢家拿到 owner_epoch 并真正写入
    assert report.claim_winners == report.lock_winners
    assert report.write_codes == (probe.CODE_ACCEPTED,)
    # 阶段三：跳过锁、直接拿过期 epoch 的 host 被 CAS 拦住
    assert report.claim_loser_codes == (probe.CODE_OWNER_CONFLICT,)
    assert report.checkpoint_count == 1
    assert report.epoch_violations == ()


# ---------------------------------------------------------------------------
# 验收 3：writer 连接中断 → 自动重连 → 仍被 fence
# ---------------------------------------------------------------------------
def test_writer_reconnect_after_connection_loss_is_fenced(harness: probe.Harness) -> None:
    fenced = probe.run_reconnect_after_writer_loss(harness, fencing=True)

    assert fenced.first_code == probe.CODE_CONNECTION_LOST
    # 重连确实换了 backend，不是复用同一条连接
    assert fenced.writer_pid_after is not None
    assert fenced.writer_pid_before is not None
    assert fenced.writer_pid_after != fenced.writer_pid_before
    assert fenced.second_code == probe.CODE_STALE_EPOCH
    assert fenced.stale_write_accepted is False
    assert fenced.checkpoint_count == 1
    assert fenced.epoch_violations == ()

    control = probe.run_reconnect_after_writer_loss(harness, fencing=False)
    # 同一故障时序，去掉 epoch fence 之后旧 invocation 重连就能继续写
    assert control.stale_write_accepted is True
    assert control.checkpoint_count > 1


# ---------------------------------------------------------------------------
# 验收 3：恢复后读到最新一致的 checkpoint
# ---------------------------------------------------------------------------
def test_restore_reads_the_latest_consistent_checkpoint(harness: probe.Harness) -> None:
    binding = harness.create_run()
    first = harness.new_invocation(binding)
    try:
        first.acquire_lock()
        first.claim_owner(0)
        bootstrap = first.write({"revision": 1})
        assert bootstrap.accepted

        successor = harness.new_invocation(binding)
        try:
            first.kill_lock(harness)
            successor.acquire_lock()
            successor.claim_owner(1)

            stale = first.write({"revision": 2, "writer": "stale"})
            assert stale.code == probe.CODE_STALE_EPOCH
            assert stale.accepted is False

            fresh = successor.write({"revision": 2, "writer": "fresh"})
            assert fresh.accepted

            restored = harness.restore_read(binding)
            assert restored is not None
            assert restored.seq == fresh.seq == 2
            assert restored.checkpoint_id == fresh.checkpoint_id
            assert restored.consistent
            assert restored.run_owner_invocation_id == successor.invocation_id
            # claim_owner(0) → epoch 1，claim_owner(1) → epoch 2
            assert restored.owner_epoch == restored.run_owner_epoch == 2

            latest = harness.latest_checkpoint(binding)
            assert latest is not None
            assert latest.checkpoint == {"revision": 2, "writer": "fresh"}
            assert harness.sequence_violations(binding) == ()
        finally:
            successor.close()
    finally:
        first.close()


# ---------------------------------------------------------------------------
# 验收 5 的实现侧：不兼容 graph/schema 版本必须拒绝，原 thread 不被覆盖
# ---------------------------------------------------------------------------
def test_incompatible_graph_version_is_rejected_and_thread_preserved(
    harness: probe.Harness,
) -> None:
    binding = harness.create_run(graph_version="gv-1", state_schema_version=1)
    baseline = harness.new_invocation(binding, declared_graph_version="gv-1",
                                      declared_state_schema_version=1)
    try:
        baseline.acquire_lock()
        baseline.claim_owner(0)
        assert baseline.write({"rev": 1}).accepted

        stranger = harness.new_invocation(
            binding, declared_graph_version="gv-2", declared_state_schema_version=1
        )
        try:
            baseline.kill_lock(harness)
            stranger.acquire_lock()
            stranger.claim_owner(1)

            rejected = stranger.write({"rev": 2})
            assert rejected.accepted is False
            assert rejected.code == probe.CODE_INCOMPATIBLE
            # 原 thread 的 checkpoint 与版本记录都没有被改写
            rows = harness.checkpoints(binding)
            assert [row.seq for row in rows] == [1]
            assert rows[0].checkpoint == {"rev": 1}
            row = harness.run_row(binding)
            assert row is not None
            assert row["graph_version"] == "gv-1"
            assert row["state_schema_version"] == 1
        finally:
            stranger.close()
    finally:
        baseline.close()


def test_incompatible_state_schema_version_is_rejected(harness: probe.Harness) -> None:
    binding = harness.create_run(graph_version="gv-join", state_schema_version=1)
    baseline = harness.new_invocation(
        binding, declared_graph_version="gv-join", declared_state_schema_version=1
    )
    try:
        baseline.acquire_lock()
        baseline.claim_owner(0)
        assert baseline.write({"rev": 1}).accepted

        # graph_version 相同、state_schema_version 不同：同样是不可恢复的不兼容
        future = harness.new_invocation(
            binding, declared_graph_version="gv-join", declared_state_schema_version=2
        )
        try:
            baseline.kill_lock(harness)
            future.acquire_lock()
            future.claim_owner(1)
            rejected = future.write({"rev": 2})
            assert rejected.accepted is False
            assert rejected.code == probe.CODE_INCOMPATIBLE
            # 报错必须能说清是哪个版本对不上，而不是一句"恢复失败"
            assert "gv-join/2" in rejected.detail
            assert "gv-join/1" in rejected.detail
            assert [row.seq for row in harness.checkpoints(binding)] == [1]
            row = harness.run_row(binding)
            assert row is not None
            assert row["state_schema_version"] == 1
        finally:
            future.close()
    finally:
        baseline.close()


# ---------------------------------------------------------------------------
# 验收 4 的实现侧：探针里不允许出现固定 sleep（同步只能靠事件/DB 侧等待）
# ---------------------------------------------------------------------------
def test_probe_contains_no_wall_clock_sleep() -> None:
    tree = ast.parse(PROBE_SOURCE.read_text(encoding="utf-8"))

    sleeps: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "sleep":
                sleeps.append(node.lineno)
            elif isinstance(func, ast.Name) and func.id == "sleep":
                sleeps.append(node.lineno)
    assert sleeps == [], f"探针里出现固定 sleep：{sleeps}"


def test_single_live_writer_and_attempt_ledger_agree(harness: probe.Harness) -> None:
    report = probe.run_owner_churn_cycles(harness, rounds=3, fencing=True)

    harness.assert_single_live_writer(report.binding)
    assert harness.live_writers(report.binding) <= 1
    row = harness.run_row(report.binding)
    assert row is not None
    assert row["owner_invocation_id"] == report.records[-1].invocation_after
    assert row["last_checkpoint_seq"] == report.final_seq
    assert row["last_checkpoint_id"] == report.final_checkpoint_id

    counts = harness.attempt_counts(report.binding)
    assert counts[f"fencing=True rejected:{probe.CODE_STALE_EPOCH}"] == 3
    assert counts[f"fencing=True accepted:{probe.CODE_ACCEPTED}"] == 4
