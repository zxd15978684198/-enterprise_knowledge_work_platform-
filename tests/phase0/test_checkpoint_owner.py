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


# ---------------------------------------------------------------------------
# 验收 6（补 §6.1-E）：进程级反证——写进程还活着、锁已丢，原生 saver 仍在写
# ---------------------------------------------------------------------------
def test_process_level_native_saver_accepts_write_after_lock_loss_is_fail_evidence(
    harness: probe.Harness,
) -> None:
    """进程级反证：写进程仍存活、锁 backend 已被终止，原生 saver 仍然写成功。

    这是 ``poc01-owner.md`` §6.1-E 指出的**负面证据**：本用例“通过”的含义是
    “已实测并记录原生 saver 在失锁后仍被接受”，**不是**“原生路径已经被 fence”。
    同一存活进程内，替身（fenced）路径在同一故障场景下被 ``STALE_OWNER_EPOCH``
    拒绝；两条事实并置，说明 P0-T05 只能落到“单活 runner”的限域结论。
    证据写到 ``docs/poc/evidence/poc01_owner_process_level.json``。
    """

    import json
    import os
    import queue
    import subprocess
    import textwrap
    import threading
    from datetime import datetime

    child_script = textwrap.dedent(
        '''
        import json
        import os
        import sys
        import uuid
        from datetime import datetime
        from urllib.parse import quote
        from uuid import UUID

        sys.path.insert(0, os.environ["PYKNORA_PHASE0_DIR"])

        import psycopg

        import checkpoint_probe as probe


        def emit(event, **fields):
            fields["event"] = event
            fields["ts"] = datetime.now().astimezone().isoformat()
            print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


        def native_checkpoint(step):
            return {
                "v": 1,
                "id": str(uuid.uuid4()),
                "ts": datetime.now().astimezone().isoformat(),
                "channel_values": {
                    "messages": [
                        {"role": "user", "content": "p0-t05-r process level " + str(step)}
                    ]
                },
                "channel_versions": {"messages": step},
                "versions_seen": {"__start__": {"messages": step}},
            }


        dsn = os.environ["PYKNORA_P0_T05_DSN"]
        binding = probe.RunBinding(
            workspace_id=UUID(os.environ["PYKNORA_P0_T05_WORKSPACE"]),
            run_id=UUID(os.environ["PYKNORA_P0_T05_RUN"]),
            thread_id=UUID(os.environ["PYKNORA_P0_T05_THREAD"]),
        )
        option = "options=" + quote("-csearch_path=" + probe.SCHEMA, safe="")
        search_path_dsn = dsn + ("&" if "?" in dsn else "?") + option
        config = {"configurable": {"thread_id": str(binding.thread_id), "checkpoint_ns": ""}}
        metadata = {"source": "input", "step": 0, "writes": None, "parents": {}}

        try:
            import langgraph.checkpoint.postgres as native_postgres
        except Exception as exc:
            emit("fatal", stage="import_native_saver", error=repr(exc))
            raise SystemExit(2) from None

        saver_conn = None
        invocation = probe.Invocation(dsn, binding, fencing=True)
        try:
            invocation.acquire_lock()
            claim = invocation.claim_owner(0)
            bootstrap = invocation.write({"process": "child", "step": "bootstrap"})
            saver_conn = psycopg.connect(search_path_dsn, autocommit=True)
            saver = native_postgres.PostgresSaver(saver_conn)
            saver.setup()
            baseline = native_checkpoint(0)
            saver.put(config, baseline, metadata, {})
            saver_pid = int(saver_conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            emit(
                "ready",
                child_pid=os.getpid(),
                nonce=os.environ["PYKNORA_P0_T05_NONCE"],
                lock_backend_pid=invocation.lock_pid,
                saver_backend_pid=saver_pid,
                invocation_id=str(invocation.invocation_id),
                owner_epoch=claim.owner_epoch,
                bootstrap_accepted=bootstrap.accepted,
                bootstrap_seq=bootstrap.seq,
                baseline_native_checkpoint_id=baseline["id"],
            )
            for line in sys.stdin:
                command = line.strip()
                if not command:
                    continue
                if command == "native_put":
                    checkpoint = native_checkpoint(1)
                    try:
                        saver.put(config, checkpoint, metadata, {})
                    except Exception as exc:
                        emit(
                            "native_put",
                            accepted=False,
                            code="NATIVE_SAVER_ERROR",
                            detail=repr(exc),
                        )
                    else:
                        emit(
                            "native_put",
                            accepted=True,
                            code=probe.CODE_ACCEPTED,
                            checkpoint_id=checkpoint["id"],
                        )
                elif command == "surrogate_put":
                    result = invocation.write({"process": "child", "step": "after_loss"})
                    emit(
                        "surrogate_put",
                        accepted=result.accepted,
                        code=result.code,
                        seq=result.seq,
                        checkpoint_id=(
                            str(result.checkpoint_id) if result.checkpoint_id else None
                        ),
                        detail=result.detail,
                    )
                elif command == "exit":
                    break
                else:
                    emit("unknown_command", command=command)
            emit("bye", child_pid=os.getpid())
        finally:
            probe.close_quietly(saver_conn)
            invocation.close()
        '''
    )

    repo_root = PROJECT.parents[1]
    # 证据目录可能被测试会话重定向到临时目录（见 conftest 的证据目录重定向）：
    # 只有单独运行本用例时才落在仓库 tracked 路径。
    evidence_dir = Path(
        os.environ.get("PYKNORA_EVIDENCE_DIR") or (repo_root / "docs" / "poc" / "evidence")
    )
    evidence_path = evidence_dir / "poc01_owner_process_level.json"
    binding = harness.create_run()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYKNORA_PHASE0_DIR"] = str(PROJECT)
    # DSN 与绑定只走环境变量：既不进 argv，也不写进证据文件（口令不落盘）
    env["PYKNORA_P0_T05_DSN"] = harness.dsn
    env["PYKNORA_P0_T05_WORKSPACE"] = str(binding.workspace_id)
    env["PYKNORA_P0_T05_RUN"] = str(binding.run_id)
    env["PYKNORA_P0_T05_THREAD"] = str(binding.thread_id)
    env["PYKNORA_P0_T05_NONCE"] = os.urandom(8).hex()
    nonce = env["PYKNORA_P0_T05_NONCE"]
    command = [sys.executable, "-c", child_script]
    child = subprocess.Popen(
        command,
        cwd=str(repo_root),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    events: queue.Queue[str | None] = queue.Queue()
    stderr_lines: list[str] = []

    def pump_stdout() -> None:
        assert child.stdout is not None
        for line in iter(child.stdout.readline, ""):
            events.put(line)
        events.put(None)

    def pump_stderr() -> None:
        assert child.stderr is not None
        for line in iter(child.stderr.readline, ""):
            stderr_lines.append(line.rstrip())

    stdout_thread = threading.Thread(target=pump_stdout, daemon=True)
    stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def child_stderr() -> str:
        return "\n".join(stderr_lines[-20:])

    def read_event(timeout: float = 60.0) -> dict[str, object]:
        try:
            line = events.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"子进程 {timeout}s 无回报；stderr={child_stderr()}") from None
        assert line is not None, f"子进程 stdout 已关闭；stderr={child_stderr()}"
        return json.loads(line)

    def send(text: str) -> None:
        assert child.stdin is not None
        child.stdin.write(text + "\n")
        child.stdin.flush()

    record: dict[str, object] = {
        "ready": None,
        "child_exit_code": None,
        "child_alive_at_native_put": None,
        "lock_terminated_at": None,
        "takeover": {},
        "native_saver": None,
        "fenced_surrogate": None,
        "timeline": {},
    }
    successor: probe.Invocation | None = None
    started_at = datetime.now().astimezone().isoformat()
    try:
        ready = read_event()
        record["ready"] = ready
        assert ready["event"] == "ready", f"子进程未就绪：{ready}"
        # Windows venv 的 python.exe 是转启动器：Popen.pid 是启动器，实际执行 probe 的
        # 是它的子进程。用一次性 nonce 证明“回报事件的进程就是我们拉起的进程”。
        assert ready["nonce"] == nonce, "nonce 不匹配，无法确认这是本次拉起的进程"
        assert child.poll() is None, "子进程回报 ready 后立即退出"
        lock_pid = int(ready["lock_backend_pid"])
        saver_pid = int(ready["saver_backend_pid"])
        assert lock_pid != saver_pid, "锁连接与 saver 连接必须是不同的 backend"
        assert ready["bootstrap_accepted"] is True
        assert int(ready["owner_epoch"]) == 1
        thread_key = str(binding.thread_id)
        native_before = int(
            harness.admin.execute(
                f"SELECT count(*) FROM {probe.SCHEMA}.checkpoints WHERE thread_id = %s",
                (thread_key,),
            ).fetchone()[0]
        )
        assert native_before >= 1, "子进程的基线原生 checkpoint 没有落库"

        # 故障注入：只终止锁 backend，写进程本身不动——这正是进程级证据的前提
        assert harness.terminate_backend(lock_pid) is True
        record["lock_terminated_at"] = datetime.now().astimezone().isoformat()

        # 接管判据：阻塞拿锁成功返回，而不是采信 pg_locks 快照（§6.1-A）
        successor = harness.new_invocation(binding)
        lock_wait_ms = successor.acquire_lock()
        assert successor.holds_lock()
        assert successor.lock_pid != lock_pid
        new_claim = successor.claim_owner(int(ready["owner_epoch"]))
        assert new_claim.owner_epoch == int(ready["owner_epoch"]) + 1
        record["takeover"] = {
            "lock_wait_ms": lock_wait_ms,
            "lock_backend_pid": successor.lock_pid,
            "owner_epoch": new_claim.owner_epoch,
            "invocation_id": str(new_claim.invocation_id),
        }

        assert child.poll() is None, "原生写入前子进程已退出，进程级证据不成立"
        record["child_alive_at_native_put"] = True
        send("native_put")
        native_event = read_event()
        native_put_at = datetime.now().astimezone().isoformat()
        assert native_event["event"] == "native_put", native_event
        assert native_event["accepted"] is True, (
            "原生 saver 在失锁后被拒，C-03 缺口未复现；证据链需重查"
        )
        native_after = int(
            harness.admin.execute(
                f"SELECT count(*) FROM {probe.SCHEMA}.checkpoints WHERE thread_id = %s",
                (thread_key,),
            ).fetchone()[0]
        )
        assert native_after == native_before + 1
        committed = harness.admin.execute(
            f"SELECT checkpoint FROM {probe.SCHEMA}.checkpoints "
            "WHERE thread_id = %s AND checkpoint_id = %s",
            (thread_key, str(native_event["checkpoint_id"])),
        ).fetchone()
        assert committed is not None
        assert committed[0]["id"] == native_event["checkpoint_id"]
        record["native_saver"] = {
            "accepted": native_event["accepted"],
            "code": native_event["code"],
            "checkpoint_id": native_event["checkpoint_id"],
            "checkpoints_before": native_before,
            "checkpoints_after": native_after,
            "committed_row_found": True,
        }

        # 同一存活进程、同一故障场景：替身 fenced 路径必须被 STALE_OWNER_EPOCH 拒绝
        send("surrogate_put")
        surrogate_event = read_event()
        assert surrogate_event["event"] == "surrogate_put", surrogate_event
        assert surrogate_event["accepted"] is False
        assert surrogate_event["code"] == probe.CODE_STALE_EPOCH
        record["fenced_surrogate"] = {
            "accepted": surrogate_event["accepted"],
            "code": surrogate_event["code"],
            "detail": surrogate_event["detail"],
            "seq": surrogate_event["seq"],
        }

        send("exit")
        bye = read_event()
        assert bye["event"] == "bye", bye
        assert child.wait(timeout=60) == 0
        record["child_exit_code"] = 0
        record["timeline"] = {
            "started_at": started_at,
            "lock_terminated_at": record["lock_terminated_at"],
            "native_put_at": native_put_at,
        }
    finally:
        if child.poll() is None:
            child.kill()
        if record["child_exit_code"] is None:
            try:
                record["child_exit_code"] = child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                record["child_exit_code"] = "TIMEOUT"
        stderr_thread.join(timeout=10)
        stdout_thread.join(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()
        if successor is not None:
            successor.close()
        ready = record["ready"]
        if isinstance(ready, dict):
            evidence = {
                "task": "P0-T05R",
                "kind": "process_level_evidence",
                "scope": "补 docs/poc/evidence/poc01-owner.md §6.1-E：进程存活 + 锁已释放",
                "generated_at": datetime.now().astimezone().isoformat(),
                "schema": probe.SCHEMA,
                "t05_verdict": "PASS（限域：单活）",
                "verdict": {
                    "native_saver": "FAIL（进程存活且锁已释放，原生 put 仍被接受）",
                    "fenced_surrogate": "PASS（同进程同场景被 STALE_OWNER_EPOCH 拒绝）",
                },
                "child_process": {
                    "command": [
                        sys.executable,
                        "-c",
                        "<inline child script：DSN 与绑定经环境变量传入>",
                    ],
                    "popen_pid": child.pid,
                    "reported_pid": ready.get("child_pid"),
                    "launcher_indirection": child.pid != ready.get("child_pid"),
                    "nonce_proven": True,
                    "lock_backend_pid": ready.get("lock_backend_pid"),
                    "saver_backend_pid": ready.get("saver_backend_pid"),
                    "lock_backend_is_process_pid": (
                        ready.get("lock_backend_pid") == ready.get("child_pid")
                    ),
                    "lock_backend_is_saver_pid": (
                        ready.get("lock_backend_pid") == ready.get("saver_backend_pid")
                    ),
                    "invocation_id": ready.get("invocation_id"),
                    "owner_epoch_after_claim": ready.get("owner_epoch"),
                    "bootstrap_accepted": ready.get("bootstrap_accepted"),
                    "bootstrap_seq": ready.get("bootstrap_seq"),
                    "exit_code": record["child_exit_code"],
                    "stderr_tail": stderr_lines[-20:],
                },
                "fault_injection": {
                    "sql": "SELECT pg_terminate_backend(<lock_backend_pid>)",
                    "lock_terminated_at": record["lock_terminated_at"],
                    "takeover_proof": "阻塞 pg_advisory_lock 成功返回（非 pg_locks 快照）",
                    "takeover": record["takeover"],
                },
                "native_saver": record["native_saver"],
                "fenced_surrogate": record["fenced_surrogate"],
                "child_alive_at_native_put": record["child_alive_at_native_put"],
                "timeline": record["timeline"],
            }
            evidence_path.write_text(
                json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
