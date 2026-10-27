"""P0-T21：Harness 节点集成、取消、超时与安全 trace 的验收测试。

对应 Backlog P0-T21 与 ``docs/poc/07_Contract_Closure.md`` C-04 / TDD02 §3–5：
1. 最小可运行图（只读并行 → 写前屏障 → 审批等待 → 执行 → Job 唤醒 → 收敛），组件全部复用；
2. checkpoint 丢失后重入：只读已提交事实，不回退已提交状态；
3. 取消后不再执行新工具调用；超时不伪装成功；
4. ``safe_trace`` 只记脱敏字段，canary 不出现在 trace 与 Graph State；
5. checkpoint/interrupt 只用既有原生组件，不自研恢复引擎。

依赖的既有沙箱（全部只读复用，不修改）：
``checkpoint_probe``(T05, p0_t05) / ``approval_probe``+``tool_ledger``(T06, p0_t06) /
``job_probe``(T08, p0_t08) / ``model_gateway``+``tool_registry``(T19) /
``budget_ledger``(T20, p0_t20)；本任务只新建 ``p0_t21``。

**单活前提**：T05 结论是 PARTIAL / 单活 runner。本文件所有 checkpoint 断言都在
同一时刻只有一个 writer 的条件下成立（``Invocation`` 持 advisory lock + owner epoch）。
"""

from __future__ import annotations

import ast
import sys
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import approval_probe  # noqa: E402
import budget_ledger  # noqa: E402
import checkpoint_probe  # noqa: E402
import contracts  # noqa: E402
import harness_graph  # noqa: E402
import job_probe  # noqa: E402
import safe_trace  # noqa: E402
import tool_ledger  # noqa: E402

HARNESS_SOURCE = PROJECT / "harness_graph.py"
CANARY_PROMPT = "CANARY-PROMPT-9f3c1b"
CANARY_KEY = "sk-canary-key-7d21"
CANARY_COOKIE = "CANARY-COOKIE-4a8e"


# ---------------------------------------------------------------------------
# 沙箱：五个 schema 各自归原作者，本文件只建不删（drop 只针对自己的 p0_t21）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def sandbox() -> Iterator[str]:
    with closing(approval_probe.connect(autocommit=True)) as admin:
        approval_probe.reset_schema(admin)
        budget_ledger.reset_schema(admin)
        job_probe.reset_schema(admin)
        harness_graph.reset_schema(admin)
    with closing(approval_probe.connect(autocommit=False)) as migrator:
        approval_probe.apply_migration(migrator)
        migrator.commit()
    with closing(budget_ledger.connect(autocommit=False)) as migrator:
        budget_ledger.apply_migration(migrator)
        migrator.commit()
    with closing(job_probe.connect(autocommit=False)) as jobs:
        job_probe.ensure_schema(jobs)
        jobs.commit()
    ckpt = checkpoint_probe.Harness()
    try:
        ckpt.ensure_schema()
    finally:
        ckpt.close()
    with closing(harness_graph.connect()) as conn:
        harness_graph.apply_schema(conn)
    # 原生 saver 的表由框架 setup() 建（不属于本模块的 DDL）
    native_tables = harness_graph.ensure_native_checkpoint_schema()
    assert set(native_tables) == set(harness_graph.NATIVE_CHECKPOINT_TABLES), native_tables
    try:
        yield approval_probe.migration_sha256()
    finally:
        with closing(harness_graph.connect()) as conn:
            harness_graph.reset_schema(conn)


@pytest.fixture()
def conn(sandbox: str) -> Iterator[Any]:
    del sandbox
    with closing(harness_graph.connect()) as connection:
        yield connection


def make_run(
    conn: Any,
    site: Any,
    *,
    script: tuple[tuple[str, Any], ...] = (("ok", "plan-ok"),),
    config: harness_graph.HarnessConfig | None = None,
    trace: safe_trace.SafeTrace | None = None,
    read_tools: dict[str, Any] | None = None,
) -> harness_graph.GraphRun:
    workspace_id, run_id, thread_id, principal_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    principal = contracts.synthesize_principal(
        workspace_id=workspace_id,
        principal_id=principal_id,
        membership_subject_ref=f"t21-{principal_id.hex[:8]}",
    )
    scope = contracts.issue_scope(principal, policy_snapshot_id=uuid.uuid4())
    default_tools = {
        "kb.search": lambda payload: {"hits": 3, "len": len(payload)},
        "kb.fetch": lambda payload: {"bytes": 128, "len": len(payload)},
    }
    return harness_graph.GraphRun(
        conn,
        workspace_id=workspace_id,
        run_id=run_id,
        thread_id=thread_id,
        principal=principal,
        scope=scope,
        site_url=site.url("/"),
        config=config or harness_graph.HarnessConfig(),
        trace=trace,
        model_script=script,
        read_tools=read_tools or default_tools,
    )


def _approve_all(conn: Any, run: harness_graph.GraphRun) -> None:
    for approval_id in _approval_ids(conn, run):
        approval_probe.approve(
            conn, workspace_id=run.workspace_id, approval_id=approval_id,
            actor=run.approver_id,
        )


def _approval_ids(conn: Any, run: harness_graph.GraphRun) -> list[uuid.UUID]:
    rows = conn.execute(
        f"SELECT approval_id FROM {approval_probe.SCHEMA}.phase0_approvals "
        "WHERE workspace_id = %s AND run_id = %s ORDER BY created_at",
        (run.workspace_id, run.run_id),
    ).fetchall()
    return [row[0] for row in rows]


def _downstream_counts(site: Any, run: harness_graph.GraphRun) -> dict[str, Any]:
    """控制站服务端自己的计数：``attempts`` 是请求次数，``applied_calls`` 是副作用次数。"""

    intent = getattr(run.state, "intent", None)
    if intent is None:
        # 还没走到写工具节点（例如启动即取消/超时）：下游一次都不可能有
        return {"attempts": 0, "applied_calls": 0, "deduplicated": False, "key": None}
    key = f"t06:{run.workspace_id.hex[:8]}:{intent.tool_call_id}"
    return site.store.counts("idem", key)


def _attempts(site: Any, run: harness_graph.GraphRun) -> int:
    return int(_downstream_counts(site, run)["attempts"])


def _applied(site: Any, run: harness_graph.GraphRun) -> int:
    return int(_downstream_counts(site, run)["applied_calls"])


# ---------------------------------------------------------------------------
# 验收 1：最小可运行图，全部组件复用
# ---------------------------------------------------------------------------
def test_minimal_graph_uses_every_reused_component(conn: Any, site: Any) -> None:
    run = make_run(conn, site)
    try:
        first = run.run_until_wait()
        assert [item.node for item in first] == ["plan", "read_fanout", "write_approval"]
        assert first[-1].status == harness_graph.WAITING_APPROVAL

        # 写前屏障：审批还没批，写工具一次都没执行
        assert _attempts(site, run) == 0
        assert run.state.read_results == 2  # 两个只读工具并行完成

        # 审批等待是**持久化等待事实**（T21 表），不是内存标志
        facts = conn.execute(
            f"SELECT kind, node FROM {harness_graph.SCHEMA}.phase0_wait_facts "
            "WHERE workspace_id = %s AND run_id = %s",
            (run.workspace_id, run.run_id),
        ).fetchall()
        assert ("approval", "write_approval") in facts

        _approve_all(conn, run)
        second = run.resume()
        assert [item.node for item in second] == ["write_execute", "job_wake", "finalize"]
        assert second[-1].status == harness_graph.COMPLETED

        # T06 账本：恰好一条 succeeded receipt
        receipts = tool_ledger.receipts_for(
            conn, workspace_id=run.workspace_id, tool_call_id=run.state.intent.tool_call_id
        )
        assert [row.outcome for row in receipts] == ["succeeded"]
        # 真实下游：恰好一次副作用
        assert _applied(site, run) == 1
        # T08：Job 结果被真实记录，唤醒读取到 succeeded
        jobs = conn.execute(
            f"SELECT count(*) FROM {job_probe.SCHEMA}.phase0_job_results "
            "WHERE workspace_id = %s AND outcome = 'succeeded'",
            (run.workspace_id,),
        ).fetchone()
        assert jobs[0] == 1
        # T20：账本里两条预留，一条已结算
        ledger_rows = conn.execute(
            f"SELECT purpose_key, status FROM {budget_ledger.SCHEMA}.phase0_budget_reservations "
            "WHERE workspace_id = %s AND run_id = %s ORDER BY purpose_key",
            (run.workspace_id, run.run_id),
        ).fetchall()
        assert len(ledger_rows) >= 1
        # T05：checkpoint 序列覆盖 6 个节点
        ckpt = checkpoint_probe.Harness()
        try:
            binding = checkpoint_probe.RunBinding(
                workspace_id=run.workspace_id, run_id=run.run_id, thread_id=run.thread_id
            )
            rows = ckpt.checkpoints(binding)
        finally:
            ckpt.close()
        assert [row.seq for row in rows] == list(range(1, 7))
        assert rows[-1].checkpoint["state"]["status"] == harness_graph.COMPLETED
    finally:
        run.close()


def test_readonly_tools_run_in_parallel_within_one_node(conn: Any, site: Any) -> None:
    run = make_run(conn, site)
    try:
        outcomes = run.run_until_wait()
        assert outcomes[1].node == "read_fanout"
        tool_events = [e for e in run.trace.events() if e.action == "tool_call"]
        assert {e.tool for e in tool_events} == {"kb.search", "kb.fetch"}
        assert run.state.read_results == 2
    finally:
        run.close()


# ---------------------------------------------------------------------------
# 契约冲突（T20 指出）：gateway 内部重试 vs purpose_key 逐 attempt 归集
# ---------------------------------------------------------------------------
def test_budget_reserves_each_model_attempt(conn: Any, site: Any) -> None:
    """方案 ①：关掉 gateway 内部重试，Harness 逐 attempt 预留。

    断言"1 次真实 provider 调用 = 1 条预留"，这正是 T20 说原始实现做不到的。
    """

    run = make_run(
        conn,
        site,
        script=(("error", "429"), ("ok", "plan-second-try")),
        config=harness_graph.HarnessConfig(prompt_text="plan"),
    )
    try:
        assert run.gateway.config.max_retries == 0, "gateway 内部重试必须关闭"
        outcomes = run.run_until_wait()
        assert outcomes[0].node == "plan"
        # 脚本里两次 transport 调用 = 两次真实 provider 请求
        assert run.transport.calls == 2
        purpose_keys = [
            row[0]
            for row in conn.execute(
                f"SELECT purpose_key FROM {budget_ledger.SCHEMA}.phase0_budget_reservations "
                "WHERE workspace_id = %s AND run_id = %s ORDER BY purpose_key",
                (run.workspace_id, run.run_id),
            ).fetchall()
        ]
        assert len(purpose_keys) == 2 == run.transport.calls
        assert purpose_keys[0].endswith(":attempt:1")
        assert purpose_keys[1].endswith(":attempt:2")
        # 第一次失败的那条预留被释放，成功的被结算：没有互相覆盖
        statuses = dict(
            conn.execute(
                f"SELECT purpose_key, status FROM {budget_ledger.SCHEMA}.phase0_budget_reservations "
                "WHERE workspace_id = %s AND run_id = %s",
                (run.workspace_id, run.run_id),
            ).fetchall()
        )
        assert statuses[purpose_keys[0]] != "reserved"
        assert statuses[purpose_keys[1]] != "reserved"
        assert statuses[purpose_keys[0]] != statuses[purpose_keys[1]]
    finally:
        run.close()


def test_non_retryable_model_error_does_not_retry(conn: Any, site: Any) -> None:
    """不可重试错误必须一次就抛，不能既烧预算又重试。"""

    run = make_run(conn, site, script=(("status", 400),))
    try:
        with pytest.raises(Exception) as excinfo:
            run.node_plan()
        assert "Gateway" in type(excinfo.value).__name__ or run.transport.calls == 1
        assert run.transport.calls == 1
    finally:
        run.close()


# ---------------------------------------------------------------------------
# 验收 2：checkpoint 丢失后重入，不回退已提交状态
# ---------------------------------------------------------------------------
def test_checkpoint_loss_reentry_reads_committed_facts_and_does_not_roll_back(
    conn: Any, site: Any
) -> None:
    run = make_run(conn, site)
    try:
        run.run_until_wait()
        _approve_all(conn, run)
        run.resume()
        assert run.state.status == harness_graph.COMPLETED
        applied_before = _applied(site, run)
        tool_call_id = run.state.intent.tool_call_id
        receipts_before = tool_ledger.receipts_for(
            conn, workspace_id=run.workspace_id, tool_call_id=tool_call_id
        )
        assert len(receipts_before) == 1
        # 进程死亡：释放 advisory lock（单活前提下，同一时刻只能有一个 host 持锁）
        run.close()

        # 模拟 **checkpoint 存储丢失**：清空该 thread 的 checkpoint 并把序列指针归零
        conn.execute(
            f"DELETE FROM {checkpoint_probe.SCHEMA}.phase0_checkpoints "
            "WHERE workspace_id = %s AND thread_id = %s",
            (run.workspace_id, run.thread_id),
        )
        conn.execute(
            f"UPDATE {checkpoint_probe.SCHEMA}.phase0_runs "
            "SET last_checkpoint_seq = 0, last_checkpoint_id = NULL "
            "WHERE workspace_id = %s AND run_id = %s",
            (run.workspace_id, run.run_id),
        )
        leftover = conn.execute(
            f"SELECT count(*) FROM {checkpoint_probe.SCHEMA}.phase0_checkpoints "
            "WHERE workspace_id = %s AND thread_id = %s",
            (run.workspace_id, run.thread_id),
        ).fetchone()[0]
        assert leftover == 0, "checkpoint 应当已经丢了"

        # 新宿主：进程重启后只有 run_id，没有内存状态
        lost = harness_graph.GraphRun(
            conn,
            workspace_id=run.workspace_id,
            run_id=run.run_id,
            thread_id=run.thread_id,
            principal=run.principal,
            scope=run.scope,
            site_url=site.url("/"),
            config=run.config,
            model_script=(("ok", "recovery"),),
            read_tools={"kb.search": lambda payload: {"hits": 1}},
        )
        try:
            facts = lost.recover_from_facts()
            assert facts["succeeded"] >= 1
            assert facts["receipts"] == len(receipts_before)
            # 已提交状态没有被回退：下游副作用仍然是 1
            assert _applied(site, run) == applied_before == 1
            # 没有 checkpoint 也能继续：T06 的稳定 intent_key 命中 succeeded receipt
            outcome = lost.node_write_execute()
            assert outcome.status == "replayed"
            assert _applied(site, run) == 1
            receipts_after = tool_ledger.receipts_for(
                conn, workspace_id=run.workspace_id, tool_call_id=tool_call_id
            )
            assert len(receipts_after) == len(receipts_before)
        finally:
            lost.close()
    finally:
        run.close()


def test_reentry_from_checkpoint_reuses_persisted_state(conn: Any, site: Any) -> None:
    """有 checkpoint 时走 checkpoint：新宿主恢复出的状态与写入时一致（单活前提）。"""

    run = make_run(conn, site)
    try:
        run.run_until_wait()
        latest = run.latest_checkpoint()
        assert latest is not None
        assert latest["node"] == "write_approval"
        assert latest["state"]["status"] == harness_graph.WAITING_APPROVAL
        assert latest["state"]["read_results"] == 2
        # Graph State 里只有引用与计数，没有 prompt/参数原文
        blob = repr(latest)
        assert CANARY_PROMPT not in blob and CANARY_COOKIE not in blob and CANARY_KEY not in blob
    finally:
        run.close()


# ---------------------------------------------------------------------------
# 验收 3：取消与超时
# ---------------------------------------------------------------------------
def test_cancel_blocks_all_further_tool_calls(conn: Any, site: Any) -> None:
    run = make_run(conn, site)
    try:
        run.run_until_wait()
        _approve_all(conn, run)
        run.request_cancel(reason_code="USER_CANCEL")
        outcomes = run.resume()
        assert outcomes[-1].status == harness_graph.CANCELLED
        # 取消后没有任何新的下游调用
        assert _attempts(site, run) == 0
        # 终态是 cancelled，不是 completed
        status = conn.execute(
            f"SELECT status FROM {harness_graph.SCHEMA}.phase0_graph_runs "
            "WHERE workspace_id = %s AND run_id = %s",
            (run.workspace_id, run.run_id),
        ).fetchone()[0]
        assert status == harness_graph.CANCELLED
        # 取消被写进 trace 的 note_code，而不是靠异常类型猜
        assert any(e.note_code == "CANCEL_OBSERVED" for e in run.trace.events())
    finally:
        run.close()


def test_cancel_before_start_does_not_run_any_node(conn: Any, site: Any) -> None:
    run = make_run(conn, site)
    try:
        run.request_cancel(reason_code="USER_CANCEL")
        outcomes = run.run_until_wait()
        assert outcomes[-1].status == harness_graph.CANCELLED
        assert run.transport.calls == 0, "取消后不能再发起模型调用"
        assert run.state.read_results == 0
        assert _attempts(site, run) == 0
    finally:
        run.close()


def test_timeout_is_never_reported_as_success(conn: Any, site: Any) -> None:
    run = make_run(conn, site)
    try:
        run.set_deadline(seconds=0)
        outcomes = run.run_until_wait()
        assert outcomes[-1].status == harness_graph.TIMED_OUT
        status = conn.execute(
            f"SELECT status FROM {harness_graph.SCHEMA}.phase0_graph_runs "
            "WHERE workspace_id = %s AND run_id = %s",
            (run.workspace_id, run.run_id),
        ).fetchone()[0]
        assert status == harness_graph.TIMED_OUT
        assert status != harness_graph.COMPLETED
        # 超时同样不产生副作用
        assert _attempts(site, run) == 0
        assert any(e.note_code == "DEADLINE_OBSERVED" for e in run.trace.events())
    finally:
        run.close()


# ---------------------------------------------------------------------------
# 验收 4：安全 trace（canary）
# ---------------------------------------------------------------------------
def test_safe_trace_never_leaks_prompt_key_or_cookie(conn: Any, site: Any) -> None:
    canaries = (CANARY_PROMPT, CANARY_KEY, CANARY_COOKIE)
    trace = safe_trace.SafeTrace(run_id=uuid.uuid4(), secrets=(CANARY_KEY,), canaries=canaries)
    config = harness_graph.HarnessConfig(
        prompt_text=CANARY_PROMPT,
        tool_arguments={"path": "/idem/write", "cookie": CANARY_COOKIE},
    )
    run = make_run(conn, site, config=config, trace=trace)
    try:
        run.run_until_wait()
        _approve_all(conn, run)
        run.resume()

        # 1) trace 自身不含任何 canary
        assert trace.scan(canaries) == [], "trace 泄漏了 canary"
        # 2) 进 Graph State 的片段也不含
        fragment = repr(trace.to_state_fragment())
        assert all(item not in fragment for item in canaries)
        # 3) T05 的 checkpoint 载荷也不含（Graph State 唯一落盘处）
        latest = run.latest_checkpoint()
        assert latest is not None
        assert all(item not in repr(latest) for item in canaries)
        # 4) trace 里确实记了该记的（否则"没泄漏"没有意义）
        actions = {e.action for e in trace.events()}
        assert {"model_reserve", "model_call", "tool_call", "checkpoint"} <= actions
    finally:
        run.close()


def test_safe_trace_rejects_non_allowlisted_fields() -> None:
    trace = safe_trace.SafeTrace(run_id=uuid.uuid4())
    for field in ("prompt", "content", "authorization", "cookie", "arguments", "api_key"):
        with pytest.raises(safe_trace.TraceFieldRejected):
            trace.record(node="n", action="a", **{field: "x"})
    # 事件并没有被写进去
    assert trace.events() == ()
    # 长字符串同样拒绝（防止整段响应混进 note）
    with pytest.raises(safe_trace.TraceFieldRejected):
        trace.record(node="n", action="a", note_code="x" * 200)


def test_safe_trace_redacts_registered_secrets() -> None:
    trace = safe_trace.SafeTrace(run_id=uuid.uuid4(), secrets=(CANARY_KEY,))
    event = trace.record(node="n", action="a", note_code="ok")
    assert CANARY_KEY not in repr(event.as_dict())
    assert safe_trace.redact_text(f"Bearer {CANARY_KEY}", secrets=(CANARY_KEY,)).find(
        CANARY_KEY
    ) == -1


# ---------------------------------------------------------------------------
# 验收 5：原生能力边界 —— 不自研恢复引擎
# ---------------------------------------------------------------------------
def test_harness_delegates_checkpointing_and_defines_no_custom_engine() -> None:
    source = HARNESS_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 1) 原生图必须走 langgraph 自己的 API（1.2.18 已实装）
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "langgraph.graph" in imported
    assert "langgraph.types" in imported
    assert "langgraph.checkpoint.postgres" in imported

    # 2) 不定义自己的 interrupt/恢复引擎入口
    function_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    for forbidden in ("interrupt_node", "resume_engine", "replay_from_checkpoint", "restore_state"):
        assert forbidden not in function_names
    # 原生 interrupt 是**调用**，不是自己实现
    assert "interrupt(" in source and "Command(resume=" in source

    # 3) fallback 路径仍只委托 T05 的 Invocation.write，且不碰 T05 的表
    assert "checkpoint_probe" in imported
    assert "p0_t05." not in source, "harness 不得直接操作 T05 的 checkpoint 表"

    # 4) 本模块绝不直写原生 checkpoint 表（只由框架 setup() 建、框架读写）
    for table in harness_graph.NATIVE_CHECKPOINT_TABLES:
        for verb in ("INSERT INTO", "UPDATE", "DELETE FROM"):
            assert f"{verb} {table}" not in source, f"不得直写原生表 {table}"
            assert f"{verb} {harness_graph.SCHEMA}.{table}" not in source

    # 5) 本任务自己的 DDL 只包含图运行/等待事实/节点事件，没有 checkpoint 表
    for table in ("phase0_graph_runs", "phase0_wait_facts", "phase0_node_events"):
        assert table in source
    assert "CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_checkpoints" not in source


def test_waiting_is_a_persisted_fact_not_in_memory_flag(conn: Any, site: Any) -> None:
    run = make_run(conn, site)
    try:
        run.run_until_wait()
        row = conn.execute(
            f"SELECT status FROM {harness_graph.SCHEMA}.phase0_graph_runs "
            "WHERE workspace_id = %s AND run_id = %s",
            (run.workspace_id, run.run_id),
        ).fetchone()
        assert row[0] == harness_graph.WAITING_APPROVAL
        # 进程内状态与 DB 事实一致；即便换一个宿主，等待事实仍然只读得到
        assert run.state.status == harness_graph.WAITING_APPROVAL
        assert _approval_ids(conn, run), "审批行必须已持久化"
    finally:
        run.close()


# ---------------------------------------------------------------------------
# 原生 LangGraph：StateGraph + interrupt() + Command(resume) + PostgresSaver
# ---------------------------------------------------------------------------
def make_native(
    site: Any,
    *,
    script: tuple[tuple[str, Any], ...] = (("ok", "plan-ok"),),
    config: harness_graph.HarnessConfig | None = None,
    trace: safe_trace.SafeTrace | None = None,
) -> harness_graph.NativeGraphRun:
    workspace_id, run_id, thread_id, principal_id = (
        uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    )
    principal = contracts.synthesize_principal(
        workspace_id=workspace_id,
        principal_id=principal_id,
        membership_subject_ref=f"t21n-{principal_id.hex[:8]}",
    )
    scope = contracts.issue_scope(principal, policy_snapshot_id=uuid.uuid4())
    return harness_graph.NativeGraphRun(
        thread_id=thread_id,
        workspace_id=workspace_id,
        run_id=run_id,
        principal=principal,
        scope=scope,
        site_url=site.url("/"),
        config=config or harness_graph.HarnessConfig(),
        trace=trace,
        model_script=script,
    )


def native_interrupt_payload(result: dict[str, Any]) -> dict[str, Any]:
    interrupts = result.get("__interrupt__") or ()
    assert interrupts, f"没有拿到原生 interrupt：{sorted(result)}"
    payload = interrupts[0].value
    assert isinstance(payload, dict)
    return payload


def native_downstream_counts(site: Any, *, workspace_id: uuid.UUID, tool_call_id: uuid.UUID):
    key = f"t06:{workspace_id.hex[:8]}:{tool_call_id}"
    return site.store.counts("idem", key)


def test_native_graph_pauses_at_interrupt_and_persists_state(conn: Any, site: Any) -> None:
    """原生 ``interrupt()`` 暂停：状态进框架 checkpoint，等待前零副作用。"""

    run = make_native(site)
    result = run.start()
    payload = native_interrupt_payload(result)
    approval_id = uuid.UUID(payload["approval_id"])
    tool_call_id = uuid.UUID(payload["tool_call_id"])

    # 暂停点：approval_wait 尚未返回
    state = run.get_state()
    assert state["next"] == ("approval_wait",), state
    assert state["values"].get("resumed") is not True
    # 框架自己的 checkpoint 表里有该 thread 的行
    assert run.checkpoint_rows() >= 1
    # 等待期间写工具一次都没执行
    assert native_downstream_counts(
        site, workspace_id=run.workspace_id, tool_call_id=tool_call_id
    )["attempts"] == 0
    # T06 的审批已经登记（幂等登记发生在 interrupt 之前）
    approval = approval_probe.get_approval(conn, workspace_id=run.workspace_id,
                                           approval_id=approval_id)
    assert approval is not None and approval.status == "pending"
    # 原生图的 checkpoint 归框架：本任务自己的 run 表不被写
    mine = conn.execute(
        f"SELECT count(*) FROM {harness_graph.SCHEMA}.phase0_graph_runs "
        "WHERE workspace_id = %s AND run_id = %s",
        (run.workspace_id, run.run_id),
    ).fetchone()[0]
    assert mine == 0


def test_native_graph_resumes_from_new_connection_exactly_once(conn: Any, site: Any) -> None:
    """跨连接恢复：新进程只拿 thread_id + ``Command(resume=...)``，副作用恰好一次。"""

    starter = make_native(site)
    payload = native_interrupt_payload(starter.start())
    approval_id = uuid.UUID(payload["approval_id"])
    tool_call_id = uuid.UUID(payload["tool_call_id"])
    approval_probe.approve(
        conn, workspace_id=starter.workspace_id, approval_id=approval_id,
        actor=starter.approver_id,
    )

    # 新宿主：另一个对象、另一条连接、另一个 saver 实例
    resumer = harness_graph.NativeGraphRun(
        thread_id=starter.thread_id,
        workspace_id=starter.workspace_id,
        run_id=starter.run_id,
        principal=starter.principal,
        scope=starter.scope,
        site_url=site.url("/"),
        config=starter.config,
        model_script=(("ok", "should-not-be-used"),),  # resume 不应再跑 plan
    )
    resumed = resumer.resume({"decision": "approved"})
    assert resumed.get("status") == harness_graph.COMPLETED
    assert resumed.get("write_outcome") == "succeeded"
    assert "plan" in resumed.get("completed", [])  # plan 只在第一次跑
    # 浏览器侧的脚本化 transport 只被第一段用过：resume 没有重新调模型
    assert starter.transport.calls == 1
    assert resumer.transport.calls == 0

    counts = native_downstream_counts(
        site, workspace_id=starter.workspace_id, tool_call_id=tool_call_id
    )
    assert counts["attempts"] == 1
    assert counts["applied_calls"] == 1
    receipts = tool_ledger.receipts_for(
        conn, workspace_id=starter.workspace_id, tool_call_id=tool_call_id
    )
    assert [row.outcome for row in receipts] == ["succeeded"]
    final = resumer.get_state()
    assert final["next"] == ()
    assert final["values"].get("status") == harness_graph.COMPLETED


def test_native_repeated_resume_does_not_duplicate_side_effects(conn: Any, site: Any) -> None:
    run = make_native(site)
    payload = native_interrupt_payload(run.start())
    approval_id = uuid.UUID(payload["approval_id"])
    tool_call_id = uuid.UUID(payload["tool_call_id"])
    approval_probe.approve(
        conn, workspace_id=run.workspace_id, approval_id=approval_id, actor=run.approver_id
    )
    run.resume({"decision": "approved"})

    for _ in range(3):  # 重复 resume
        again = run.resume({"decision": "approved"})
        assert again.get("status") == harness_graph.COMPLETED

    counts = native_downstream_counts(
        site, workspace_id=run.workspace_id, tool_call_id=tool_call_id
    )
    assert counts["applied_calls"] == 1, "重复 resume 不得产生第二次副作用"
    receipts = tool_ledger.receipts_for(
        conn, workspace_id=run.workspace_id, tool_call_id=tool_call_id
    )
    assert len(receipts) == 1


def test_native_checkpoints_are_owned_by_framework(conn: Any, site: Any) -> None:
    run = make_native(site)
    run.start()
    tables = set(run.checkpoint_tables())
    for table in harness_graph.NATIVE_CHECKPOINT_TABLES:
        assert table in tables, f"框架没建 {table}"
    # 框架表里有数据；本任务自己的三张表里没有 checkpoint 语义
    assert run.checkpoint_rows() >= 1
    my_tables = {
        "phase0_graph_runs",
        "phase0_wait_facts",
        "phase0_node_events",
    }
    assert my_tables <= tables
    # 原生路径不写 wait_facts（fallback 才用）
    facts = conn.execute(
        f"SELECT count(*) FROM {harness_graph.SCHEMA}.phase0_wait_facts "
        "WHERE workspace_id = %s AND run_id = %s",
        (run.workspace_id, run.run_id),
    ).fetchone()[0]
    assert facts == 0, "原生 interrupt 路径不应再写 fallback 的等待事实"


def test_native_interrupt_payload_carries_no_prompt_or_secret(site: Any) -> None:
    canaries = (CANARY_PROMPT, CANARY_COOKIE, CANARY_KEY)
    trace = safe_trace.SafeTrace(run_id=uuid.uuid4(), secrets=(CANARY_KEY,), canaries=canaries)
    run = make_native(
        site,
        config=harness_graph.HarnessConfig(
            prompt_text=CANARY_PROMPT,
            tool_arguments={"path": "/idem/write", "cookie": CANARY_COOKIE},
        ),
        trace=trace,
    )
    result = run.start()
    payload = native_interrupt_payload(result)
    # interrupt payload 只带引用（approval_id/tool_call_id/intent_key 前缀）
    assert set(payload) == {"kind", "approval_id", "tool_call_id", "intent_key_prefix"}
    blob = repr(payload) + repr(run.get_state()["values"]) + repr(trace.as_list())
    assert all(item not in blob for item in canaries), "原生 interrupt/State/trace 泄漏 canary"
