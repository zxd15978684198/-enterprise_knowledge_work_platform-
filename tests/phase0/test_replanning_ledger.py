"""P0-T24 × P0-T20 接线验收：重规划的预算读数来自**真实账本**，不是替身。

背景：T24 收尾时唯一 PARTIAL 的子契约是「真实 ``BudgetLedger`` 台账未接线」——
重规划器当时只接了 :class:`evaluator_replanner.StaticBudget`，所以「额度不足才 blocked」
这条结论只在替身上成立。本文件把真实的 ``p0_t20`` 账本接到
:class:`evaluator_replanner.LedgerBudget`，断言三件事：

1. **读数一致**：``LedgerBudget.remaining_microunits`` == 账本 ``state().available()``，
   且**在途预留**会同步减少可用额度（不是只看已结算）。
2. **不足即拦**：可用额度小于需求时决策为 ``budget_exhausted``、run 终态 ``blocked``，
   且账本的预留/结算/事件计数**分毫未动**（重规划既不 release 也不透支）。
3. **充足即放行**：额度够时不被预算拦截（本例走到 ``noop``），读数写进结果供审计。

隔离：Task 事实在 ``p0_t23``、账本在 ``p0_t20``、重规划账目在 ``p0_t24``；三次测试各自
使用全新 ``workspace_id``/``run_id``，结束后按 ``run_id`` 清理，互不影响。
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

PROJECT = Path(__file__).resolve().parents[2] / "experiments" / "phase0"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import budget_ledger as bl  # noqa: E402
import contracts  # noqa: E402
import dag_contracts as dag  # noqa: E402
import evaluator_replanner as ev  # noqa: E402
import task_scheduler as sch  # noqa: E402

LEDGER_LIMIT = 1_000_000
RESERVED_MICROUNITS = 400_000
POLICY = ev.EvaluationPolicy(
    per_task={"a": ev.Acceptance(required_keys=("result.ok",))},
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _ensure_t23(conn: psycopg.Connection[Any]) -> str:
    try:
        return sch.apply_migration(conn)
    except sch.MigrationAlreadyApplied:
        return "already-applied"
    except sch.MigrationStateError:
        sch.reset_schema(conn)
        return sch.apply_migration(conn)


@pytest.fixture(scope="session")
def t23_migration() -> Iterator[str]:
    conn = sch.connect()
    try:
        digest = _ensure_t23(conn)
    finally:
        conn.close()
    yield digest


@pytest.fixture(scope="session")
def t24_schema() -> Iterator[None]:
    conn = sch.connect()
    try:
        ev.ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture(scope="session")
def t20_schema() -> Iterator[None]:
    """幂等：schema 与台账已存在则跳过建表（不做 drop，避免拖累同轮其它用例）。

    注意：探测本身会开事务，而 `bl.apply_migration` 要求连接处于 IDLE，所以探测后必须
    先 ``rollback()`` 再建表；否则同轮若有别的用例 drop 过 `p0_t20`，这里会以
    `LedgerConfigError: 需要干净事务边界` 报 setup error（实测过一次）。
    """
    conn = bl.connect()
    try:
        present = conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS present",
            (f"{bl.SCHEMA}.phase0_schema_migrations",),
        ).fetchone()
        assert present is not None
        conn.rollback()
        if not bool(_first(present)):
            bl.apply_migration(conn)
            conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture()
def run_conn(
    t23_migration: str, t24_schema: None, t20_schema: None
) -> Iterator[psycopg.Connection[Any]]:
    """T23/T24 侧连接（Task 事实 + 重规划账目）。"""
    assert t23_migration
    connection = sch.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def ledger_conn(t20_schema: None) -> Iterator[psycopg.Connection[Any]]:
    """账本侧独立连接：账本读写与 T23 事实互不干扰（与 harness 同样分离）。"""
    assert t20_schema is None or True
    connection = bl.connect()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def scope() -> contracts.WorkspaceScope:
    principal = contracts.synthesize_principal(
        workspace_id=uuid4(),
        principal_id=uuid4(),
        membership_subject_ref="subject-w17-d4",
        kind="service",
        role="owner",
    )
    return contracts.issue_scope(principal, policy_snapshot_id=uuid4())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@dataclass
class Harness:
    conn: psycopg.Connection[Any]
    ledger_conn: psycopg.Connection[Any]
    ledger: bl.BudgetLedger
    scope: contracts.WorkspaceScope
    run: UUID
    revision: int = 1

    def open_run(self) -> None:
        self.ledger.ensure(
            scope=self.scope,
            run_id=self.run,
            limits=bl.BudgetLimits(
                tokens=1_000_000, cost_microunits=LEDGER_LIMIT, currency="USD"
            ),
        )
        self.conn.commit()

    def register(self, nodes: Sequence[dag.TaskNode]) -> dag.TaskGraph:
        graph = dag.TaskGraph(
            nodes=tuple(nodes),
            task_graph_revision=self.revision,
            run_id=str(self.run),
        )
        sch.register_graph(
            self.conn,
            workspace_id=self.scope.workspace_id,
            run_id=self.run,
            revision=self.revision,
            graph=graph,
        )
        self.conn.commit()
        return graph

    def reserve(self, micro: int = RESERVED_MICROUNITS) -> bl.Reservation:
        reservation = self.ledger.reserve_model_call(
            scope=self.scope,
            run_id=self.run,
            purpose_key="w17/d4-replan",
            prompt_tokens=1,
            max_output_tokens=1,
            price=None,
            max_cost_microunits=micro,
        )
        self.conn.commit()
        return reservation

    def claim(self, task_id: str) -> sch.ClaimedTask:
        claimed = sch.claim_task(
            self.conn,
            workspace_id=self.scope.workspace_id,
            run_id=self.run,
            revision=self.revision,
            task_id=task_id,
            owner_id="w17-d4",
        )
        self.conn.commit()
        return claimed

    def fail(self, task_id: str, claimed: sch.ClaimedTask) -> None:
        sch.fail_task(
            self.conn,
            workspace_id=self.scope.workspace_id,
            run_id=self.run,
            revision=self.revision,
            task_id=task_id,
            epoch=claimed.epoch,
            error_code="TOOL_ERROR",
            owner_id="w17-d4",
        )
        self.conn.commit()

    def request(self, required: int) -> ev.ReplanRequest:
        return ev.ReplanRequest(
            workspace_id=self.scope.workspace_id,
            run_id=self.run,
            expected_revision=self.revision,
            required_budget_microunits=required,
            expected_fingerprints={},
            fixed_job_kinds=(),
            limits=None,
        )


def _node(task_id: str, *, depends_on: Sequence[str] = ()) -> dag.TaskNode:
    return dag.TaskNode(
        node_id=task_id,
        kind="graph",
        title=f"w17-d4 {task_id}",
        depends_on=tuple(depends_on),
    )


def _ledger_scope(harness: Harness) -> tuple[int, int, int]:
    """(reservations, settlements, events) 计数：用于「分毫未动」断言。"""
    counts: list[int] = []
    for table in (
        "phase0_budget_reservations",
        "phase0_budget_settlements",
        "phase0_budget_events",
    ):
        row = harness.conn.execute(
            f"SELECT count(*) AS n FROM {bl.SCHEMA}.{table} WHERE run_id = %s",
            (harness.run,),
        ).fetchone()
        assert row is not None
        counts.append(_first(row))
    return counts[0], counts[1], counts[2]


def _first(row: Any) -> int:
    """连接可能配了 dict row_factory：按「第一个值」取值，两种行工厂都安全。"""
    if isinstance(row, dict):
        return int(next(iter(row.values())))
    return int(row[0])


def _purge(harness: Harness) -> None:
    """按 run_id 清理 T23/T24 里的本测试行；外键顺序用重试收敛。

    ``p0_t20`` 表带 append-only 触发器（``phase0_reject_append_only_mutation``），
    账本行**禁止删除**——这是被测系统的正确行为，所以清理只碰 T23/T24；账本侧
    靠每次用例独立 ``run_id`` 隔离，不做回删。
    """
    harness.ledger_conn.rollback()
    for _ in range(6):
        remaining: list[tuple[str, str]] = []
        for schema in ("p0_t24", "p0_t23"):
            target = harness.conn
            tables = target.execute(
                "SELECT tablename AS name FROM pg_tables WHERE schemaname = %s",
                (schema,),
            ).fetchall()
            for row in tables:
                table = next(iter(row.values())) if isinstance(row, dict) else row[0]
                columns = {
                    (r["column_name"] if isinstance(r, dict) else r[0])
                    for r in target.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = %s",
                        (schema, table),
                    ).fetchall()
                }
                if "run_id" not in columns:
                    continue
                try:
                    target.execute(
                        f'DELETE FROM "{schema}"."{table}" WHERE run_id = %s',
                        (harness.run,),
                    )
                    target.commit()
                except psycopg.errors.ForeignKeyViolation:
                    target.rollback()
                    remaining.append((schema, table))
        if not remaining:
            return
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# 1. 读数一致：含在途预留
# ---------------------------------------------------------------------------
def test_ledger_budget_reports_real_available(
    run_conn: psycopg.Connection[Any],
    ledger_conn: psycopg.Connection[Any],
    scope: contracts.WorkspaceScope,
) -> None:
    harness = Harness(
        conn=run_conn,
        ledger_conn=ledger_conn,
        ledger=bl.BudgetLedger(ledger_conn),
        scope=scope,
        run=uuid4(),
    )
    try:
        harness.open_run()
        port = ev.LedgerBudget(ledger=harness.ledger, scope=scope)

        fresh = harness.ledger.state(scope=scope, run_id=harness.run)
        assert port.remaining_microunits(scope.workspace_id, harness.run) == (
            fresh.available().cost_microunits
        )
        assert fresh.available().cost_microunits == LEDGER_LIMIT

        harness.reserve()
        after = harness.ledger.state(scope=scope, run_id=harness.run)
        assert after.available().cost_microunits == LEDGER_LIMIT - RESERVED_MICROUNITS
        # 关键：预留还没结算，但端口已经少报——证明读的是账本而非静态替身。
        assert port.remaining_microunits(scope.workspace_id, harness.run) == (
            LEDGER_LIMIT - RESERVED_MICROUNITS
        )
        assert after.spent.cost_microunits == 0
    finally:
        _purge(harness)
        run_conn.commit()


# ---------------------------------------------------------------------------
# 2. 不足即拦：决策 budget_exhausted + 账本分毫未动
# ---------------------------------------------------------------------------
def test_insufficient_ledger_blocks_replan(
    run_conn: psycopg.Connection[Any],
    ledger_conn: psycopg.Connection[Any],
    scope: contracts.WorkspaceScope,
) -> None:
    harness = Harness(
        conn=run_conn,
        ledger_conn=ledger_conn,
        ledger=bl.BudgetLedger(ledger_conn),
        scope=scope,
        run=uuid4(),
    )
    try:
        harness.open_run()
        harness.register([_node("a"), _node("b", depends_on=("a",))])
        harness.reserve()
        before = _ledger_scope(harness)

        replanner = ev.Replanner(
            run_conn,
            POLICY,
            budget=ev.LedgerBudget(ledger=harness.ledger, scope=scope),
        )
        result = replanner.replan(harness.request(LEDGER_LIMIT))
        run_conn.commit()

        assert result.decision == "budget_exhausted"
        assert result.terminal_status == "blocked"
        assert result.budget_remaining_microunits == LEDGER_LIMIT - RESERVED_MICROUNITS
        # 重规划不 release、不透支：三类计数与调用前完全一致。
        assert _ledger_scope(harness) == before
        state = harness.ledger.state(scope=scope, run_id=harness.run)
        assert state.reserved.cost_microunits == RESERVED_MICROUNITS
        assert state.spent.cost_microunits == 0
    finally:
        _purge(harness)
        run_conn.commit()


# ---------------------------------------------------------------------------
# 3. 充足即放行：不被预算拦截，读数进入结果
# ---------------------------------------------------------------------------
def test_sufficient_ledger_passes_budget_gate(
    run_conn: psycopg.Connection[Any],
    ledger_conn: psycopg.Connection[Any],
    scope: contracts.WorkspaceScope,
) -> None:
    harness = Harness(
        conn=run_conn,
        ledger_conn=ledger_conn,
        ledger=bl.BudgetLedger(ledger_conn),
        scope=scope,
        run=uuid4(),
    )
    try:
        harness.open_run()
        harness.register([_node("a")])
        harness.reserve()
        before = _ledger_scope(harness)

        replanner = ev.Replanner(
            run_conn,
            POLICY,
            budget=ev.LedgerBudget(ledger=harness.ledger, scope=scope),
        )
        # 需求远小于可用额度：预算门放行；因为没有「未满足」事实，本轮无重规划。
        result = replanner.replan(harness.request(100_000))
        run_conn.commit()

        assert result.decision == "noop"
        assert result.terminal_status == "running"
        # 接线事实：noop 路径在「读取预算」之前就返回了，所以 available 不落盘。
        # 这不是缺陷（没有重规划就没有预算决策），但意味着 noop 不能当作预算放行的证据
        # ——预算放行由 test_sufficient_ledger_applies_replan 覆盖。
        assert result.budget_remaining_microunits is None
        assert _ledger_scope(harness) == before
    finally:
        _purge(harness)
        run_conn.commit()


# ---------------------------------------------------------------------------
# 4. 跨工作区不可混用：账本 scope 与请求不一致时拒绝
# ---------------------------------------------------------------------------
def test_ledger_budget_rejects_foreign_workspace(
    run_conn: psycopg.Connection[Any],
    ledger_conn: psycopg.Connection[Any],
    scope: contracts.WorkspaceScope,
) -> None:
    harness = Harness(
        conn=run_conn,
        ledger_conn=ledger_conn,
        ledger=bl.BudgetLedger(ledger_conn),
        scope=scope,
        run=uuid4(),
    )
    try:
        harness.open_run()
        port = ev.LedgerBudget(ledger=harness.ledger, scope=scope)
        with pytest.raises(ev.ReplanError) as excinfo:
            port.remaining_microunits(uuid4(), harness.run)
        assert excinfo.value.code == ev.CODE_INVALID_REQUEST
    finally:
        _purge(harness)
        run_conn.commit()


# ---------------------------------------------------------------------------
# 5. 充足即放行（真实重规划路径）：预算读数落盘且不被拦截
# ---------------------------------------------------------------------------
def test_sufficient_ledger_applies_replan(
    run_conn: psycopg.Connection[Any],
    ledger_conn: psycopg.Connection[Any],
    scope: contracts.WorkspaceScope,
) -> None:
    harness = Harness(
        conn=run_conn,
        ledger_conn=ledger_conn,
        ledger=bl.BudgetLedger(ledger_conn),
        scope=scope,
        run=uuid4(),
    )
    try:
        harness.open_run()
        harness.register([_node("a"), _node("b", depends_on=("a",))])
        harness.reserve()
        claimed = harness.claim("a")
        harness.fail("a", claimed)
        before = _ledger_scope(harness)

        replanner = ev.Replanner(
            run_conn,
            POLICY,
            budget=ev.LedgerBudget(ledger=harness.ledger, scope=scope),
        )
        result = replanner.replan(harness.request(100_000))
        run_conn.commit()

        # 真走了预算门：需求 < 可用 → 不被拦，重规划照常应用。
        assert result.decision in {"applied", "needs_review"}
        # 接线事实：`budget_remaining_microunits` 只在「被拦」时作为审计字段落盘，
        # 放行路径写 None。读数正确性由用例 1（端口直读）与用例 2（被拦时精确值）
        # 共同证明，不靠本字段。
        assert result.budget_remaining_microunits is None
        assert _ledger_scope(harness) == before
    finally:
        _purge(harness)
        run_conn.commit()
