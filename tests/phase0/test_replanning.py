"""P0-T24 真实验收：Evaluator 事实判定 + 有界重规划（revision CAS / 复用 / 上限）。

测试全部打真实 PostgreSQL(127.0.0.1:5433)：Task 事实来自 T23 的 ``p0_t23``
（经 T23 的公开 API 领取/完成/失败，不直接改表），本任务的回执、输入指纹、
评估与重规划账目写入独占 schema ``p0_t24``。**没有 mock 替代品**：唯一的替身是
:class:`StaticBudget`，它显式实现「被 release 即失败」以证明重规划不动未知预留。

对应 Backlog P0-T24 量化验收：

* 评估输入必须是事实（Task 事实 + 工具回执），节点自述不足以判成功；
* 未达成才重规划，且只替换/追加受影响子任务，已完成节点不重复登记；
* 新 revision 必须通过 ``dag_contracts`` 校验（无环、边端点、节点上限）；
* 最多 2 次重规划，第 3 次进入 ``failed`` 终态并留「超限」证据；
* 不匹配 input/profile/policy 不复用；已确认副作用的节点不自动重跑；
* 重复评估/重复重规划幂等；``expected_revision`` CAS 冲突显式失败；
* 预算不足显式 ``blocked``，且不释放未知用量预留。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import dag_contracts as dag
import evaluator_replanner as ev
import psycopg
import pytest
import task_scheduler as sch

POLICY = ev.EvaluationPolicy(
    per_kind={
        "graph": ev.Acceptance(required_keys=("result.ok",)),
        "tool": ev.Acceptance(required_keys=("result.ok",), required_receipts=1),
    },
    per_task={
        "B": ev.Acceptance(required_keys=("result.ok",), required_receipts=1),
    },
)
FP = ev.Fingerprint(input_digest="sha256:input-v1", profile="p0-profile", policy="p0-policy")
FP_NEW = ev.Fingerprint(input_digest="sha256:input-v2", profile="p0-profile", policy="p0-policy")


# ---------------------------------------------------------------------------
# fixtures：T23 迁移只确保存在（不重置邻居 schema），T24 schema 本文件独占
# ---------------------------------------------------------------------------
def _ensure_t23(conn: psycopg.Connection[Any]) -> str:
    try:
        return sch.apply_migration(conn)
    except sch.MigrationAlreadyApplied:
        return "already-applied"
    except sch.MigrationStateError:
        # 残留半截 schema（例如上次 kill）：走 T23 官方重建路径，而不是手改表。
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
    finally:
        conn.close()
    yield
    conn = sch.connect()
    try:
        ev.reset_schema(conn)
    finally:
        conn.close()


_PURGE_ORDER = (
    "phase0_task_claim_events",
    "phase0_task_wakes",
    "phase0_task_claims",
    "phase0_task_edges",
    "phase0_tasks",
    "phase0_task_graphs",
)


def _purge(conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID) -> None:
    conn.rollback()
    for table in _PURGE_ORDER:
        conn.execute(
            f"DELETE FROM {sch.SCHEMA}.{table} WHERE workspace_id = %s AND run_id = %s",
            (workspace_id, run_id),
        )
    conn.commit()


@pytest.fixture()
def ctx(t23_migration: str, t24_schema: None) -> Iterator[Ctx]:
    # t24_schema 的返回值是 None（建表成功与否由 fixture 自身失败体现），此处只依赖其副作用。
    assert t23_migration
    conn = sch.connect()
    workspace_id, run_id = uuid4(), uuid4()
    conn.execute(
        f"TRUNCATE {ev.SCHEMA}.phase0_evaluations, {ev.SCHEMA}.phase0_tool_receipts, "
        f"{ev.SCHEMA}.phase0_task_inputs, {ev.SCHEMA}.phase0_runs, {ev.SCHEMA}.phase0_replans"
    )
    conn.commit()
    try:
        yield Ctx(conn, workspace_id, run_id)
    finally:
        try:
            _purge(conn, workspace_id, run_id)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 测试助手：只用 T23 的公开 API 造事实
# ---------------------------------------------------------------------------
@dataclass
class Ctx:
    conn: psycopg.Connection[Any]
    ws: UUID
    run: UUID

    def register(
        self,
        revision: int,
        nodes: Sequence[tuple[str, Sequence[str], str]],
    ) -> dag.TaskGraph:
        graph = dag.TaskGraph(
            nodes=tuple(
                dag.TaskNode(node_id=node_id, depends_on=tuple(deps), kind=kind)
                for node_id, deps, kind in nodes
            ),
            task_graph_revision=revision,
            run_id=str(self.run),
        )
        sch.register_graph(
            self.conn, workspace_id=self.ws, run_id=self.run, revision=revision, graph=graph
        )
        return graph

    def claim(self, revision: int, task_id: str) -> sch.ClaimedTask:
        return sch.claim_task(
            self.conn,
            workspace_id=self.ws,
            run_id=self.run,
            revision=revision,
            task_id=task_id,
            owner_id="t24-test",
        )

    def succeed(self, revision: int, task_id: str, fact: dict[str, Any]) -> None:
        claim = self.claim(revision, task_id)
        sch.complete_task(
            self.conn,
            workspace_id=self.ws,
            run_id=self.run,
            revision=revision,
            task_id=task_id,
            epoch=claim.epoch,
            fact=fact,
            owner_id="t24-test",
        )

    def fail(self, revision: int, task_id: str, code: str = "TOOL_FAILED") -> tuple[str, ...]:
        claim = self.claim(revision, task_id)
        return sch.fail_task(
            self.conn,
            workspace_id=self.ws,
            run_id=self.run,
            revision=revision,
            task_id=task_id,
            epoch=claim.epoch,
            error_code=code,
            owner_id="t24-test",
        )

    def cancel(self, revision: int, task_id: str) -> tuple[str, ...]:
        return sch.cancel_task(
            self.conn,
            workspace_id=self.ws,
            run_id=self.run,
            revision=revision,
            task_id=task_id,
            reason="source-revoked",
        )

    def receipt(
        self,
        revision: int,
        task_id: str,
        *,
        tool: str = "tool",
        status: str = "succeeded",
        external_id: str = "",
        applied_version: int | None = 1,
    ) -> bool:
        return ev.record_receipt(
            self.conn,
            self.ws,
            self.run,
            revision,
            ev.Receipt(
                task_id=task_id,
                tool=tool,
                status=status,  # type: ignore[arg-type]
                applied_version=applied_version,
                external_id=external_id,
            ),
        )

    def inputs(
        self,
        revision: int,
        task_id: str,
        fingerprint: ev.Fingerprint = FP,
        *,
        side_effect_confirmed: bool = False,
    ) -> bool:
        return ev.record_inputs(
            self.conn,
            self.ws,
            self.run,
            revision,
            task_id,
            fingerprint,
            side_effect_confirmed=side_effect_confirmed,
        )

    def outcomes(self, revision: int) -> tuple[ev.TaskOutcome, ...]:
        return ev.load_outcomes(self.conn, self.ws, self.run, revision)

    def evaluate(self, revision: int, policy: ev.EvaluationPolicy | None = None):
        return ev.evaluate_run(self.conn, self.ws, self.run, revision, policy or POLICY)

    def replanner(
        self, policy: ev.EvaluationPolicy | None = None, budget: ev.BudgetPort | None = None
    ) -> ev.Replanner:
        return ev.Replanner(self.conn, policy or POLICY, budget=budget)

    def request(
        self,
        expected_revision: int,
        *,
        budget: int = 0,
        fingerprints: dict[str, ev.Fingerprint] | None = None,
    ) -> ev.ReplanRequest:
        return ev.ReplanRequest(
            workspace_id=self.ws,
            run_id=self.run,
            expected_revision=expected_revision,
            required_budget_microunits=budget,
            expected_fingerprints=fingerprints or {},
        )

    def revisions(self) -> list[int]:
        rows = self.conn.execute(
            f"SELECT revision FROM {sch.SCHEMA}.phase0_task_graphs "
            "WHERE workspace_id = %s AND run_id = %s ORDER BY revision",
            (self.ws, self.run),
        ).fetchall()
        return [int(row["revision"]) for row in rows]

    def node_ids(self, revision: int) -> set[str]:
        rows = self.conn.execute(
            f"SELECT task_id FROM {sch.SCHEMA}.phase0_tasks "
            "WHERE workspace_id = %s AND run_id = %s AND revision = %s",
            (self.ws, self.run, revision),
        ).fetchall()
        return {str(row["task_id"]) for row in rows}

    def replan_rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT decision, replan_index, from_revision, to_revision, reason, result "
            f"FROM {ev.SCHEMA}.phase0_replans WHERE workspace_id = %s AND run_id = %s "
            "ORDER BY created_at",
            (self.ws, self.run),
        ).fetchall()
        return [dict(row) for row in rows]


def _ok_fact(*, version: int = 1, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    fact: dict[str, Any] = {"result": {"ok": True}, "fact_version": version}
    if extra:
        fact.update(extra)
    return fact


def _linear() -> list[tuple[str, Sequence[str], str]]:
    return [
        ("A", (), "graph"),
        ("B", ("A",), "graph"),
        ("C", ("B",), "graph"),
    ]


# ---------------------------------------------------------------------------
# 1) 评估：成功 / 部分成功 / 失败 / 阻塞 / 未运行
# ---------------------------------------------------------------------------
def test_success_is_decided_from_facts_and_receipts(ctx: Ctx) -> None:
    ctx.register(1, [("A", (), "graph"), ("B", ("A",), "graph")])
    ctx.succeed(1, "A", _ok_fact())
    ctx.succeed(1, "B", _ok_fact())
    assert ctx.receipt(1, "B") is True

    report, created = ctx.evaluate(1)
    assert created is True
    assert report.verdict == "satisfied"
    assert report.ok is True
    assert report.replan_required is False
    assert {node.task_id: node.verdict for node in report.nodes} == {
        "A": "satisfied",
        "B": "satisfied",
    }
    assert len(report.evidence_digest) == 64


def test_self_report_without_receipt_is_partial(ctx: Ctx) -> None:
    """节点自述「已完成」但没有工具回执/必需事实键 → 不允许判成功。"""
    ctx.register(1, [("T", (), "tool")])
    ctx.succeed(1, "T", {"claimed": "已完成，绝对成功", "result": {"ok": True}})

    report, _ = ctx.evaluate(1)
    node = report.node("T")
    assert node.verdict == "partial"
    assert any(item.startswith(ev.REASON_RECEIPT_MISSING) for item in node.reasons)
    assert report.verdict == "partial"


def test_missing_fact_key_is_partial(ctx: Ctx) -> None:
    policy = ev.EvaluationPolicy(
        per_task={"A": ev.Acceptance(required_keys=("result.ok", "result.artifact_sha256"))}
    )
    ctx.register(1, [("A", (), "graph")])
    ctx.succeed(1, "A", _ok_fact())
    report, _ = ctx.evaluate(1, policy)
    node = report.node("A")
    assert node.verdict == "partial"
    assert any(ev.REASON_FACT_KEYS_MISSING in item for item in node.reasons)
    assert any("artifact_sha256" in item for item in node.reasons)


def test_fact_version_too_low_is_partial(ctx: Ctx) -> None:
    policy = ev.EvaluationPolicy(per_task={"A": ev.Acceptance(min_fact_version=2)})
    ctx.register(1, [("A", (), "graph")])
    ctx.succeed(1, "A", _ok_fact())
    report, _ = ctx.evaluate(1, policy)
    assert report.node("A").verdict == "partial"
    assert ev.REASON_FACT_VERSION_TOO_LOW in report.node("A").reasons


def test_failed_node_and_blocked_dependents(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    blocked = ctx.fail(1, "B")
    assert list(blocked) == ["C"]

    report, _ = ctx.evaluate(1)
    verdicts = {node.task_id: node.verdict for node in report.nodes}
    assert verdicts == {"A": "satisfied", "B": "failed", "C": "blocked"}
    assert report.verdict == "failed"
    assert report.failed_ids == ("B",)
    assert report.blocked_ids == ("C",)


def test_partial_success_overall_partial(ctx: Ctx) -> None:
    ctx.register(1, [("A", (), "graph"), ("B", (), "graph")])
    ctx.succeed(1, "A", _ok_fact())
    ctx.succeed(1, "B", {"result": {}})  # 缺 result.ok

    report, _ = ctx.evaluate(1)
    assert {node.task_id: node.verdict for node in report.nodes} == {
        "A": "satisfied",
        "B": "partial",
    }
    assert report.verdict == "partial"


def test_pending_node_is_not_run(ctx: Ctx) -> None:
    ctx.register(1, [("A", (), "graph")])
    report, _ = ctx.evaluate(1)
    assert report.node("A").verdict == "not_run"
    assert report.verdict == "partial"


def test_unlabeled_acceptance_is_partial(ctx: Ctx) -> None:
    ctx.register(1, [("A", (), "graph")])
    ctx.succeed(1, "A", _ok_fact())
    report, _ = ctx.evaluate(1, ev.EvaluationPolicy())
    assert report.node("A").verdict == "partial"
    assert ev.REASON_ACCEPTANCE_UNLABELED in report.node("A").reasons


# ---------------------------------------------------------------------------
# 2) 重规划：只动未完成部分、复用已完成、图仍无环
# ---------------------------------------------------------------------------
def test_replan_replaces_only_unfinished_and_reuses_completed(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.fail(1, "B")

    result = ctx.replanner().replan(ctx.request(1))
    assert result.decision == "applied"
    assert result.to_revision == 2
    assert result.reused == ("A",)
    assert result.replacements == {"B": "B.r1"}
    assert result.affected == ("B",)

    # A 不再登记（不复制已完成子任务），C 的依赖改指向 B.r1。
    assert ctx.node_ids(2) == {"B.r1", "C"}
    assert "A" not in ctx.node_ids(2) and "A.r1" not in ctx.node_ids(2)
    snapshot = ev.load_graph_snapshot(ctx.conn, ctx.ws, ctx.run, 2)
    by_id = {node.node_id: node for node in snapshot.graph.nodes}
    assert by_id["C"].depends_on == ("B.r1",)
    assert dag.validate_graph(snapshot.graph).ok is True
    # 旧 revision 不被改写。
    assert ctx.node_ids(1) == {"A", "B", "C"}


def test_replan_second_time_after_new_failure(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.fail(1, "B")
    first = ctx.replanner().replan(ctx.request(1))
    assert first.to_revision == 2

    ctx.fail(2, "B.r1")
    second = ctx.replanner().replan(ctx.request(2))
    assert second.decision == "applied"
    assert second.replan_index == 2
    assert second.to_revision == 3
    assert second.reused == ()
    assert second.replacements == {"B.r1": "B.r1.r2"}
    assert ctx.node_ids(3) == {"B.r1.r2", "C"}


def test_third_replan_hits_limit_and_terminates_failed(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.fail(1, "B")
    ctx.replanner().replan(ctx.request(1))
    ctx.fail(2, "B.r1")
    ctx.replanner().replan(ctx.request(2))
    ctx.fail(3, "B.r1.r2")

    replanner = ctx.replanner()
    third = replanner.replan(ctx.request(3))
    assert third.decision == "limit_exceeded"
    assert third.replan_index == 3
    assert third.terminal_status == "failed"
    assert third.to_revision is None
    assert third.reason == ev.REASON_REPLAN_LIMIT
    # 超限不产生新 revision，也不会静默继续。
    assert ctx.revisions() == [1, 2, 3]
    status = replanner.run_status(ctx.ws, ctx.run)
    assert status is not None
    assert status["terminal_status"] == "failed"
    assert status["replan_count"] == ev.MAX_REPLANS
    rows = ctx.replan_rows()
    assert [row["decision"] for row in rows] == ["applied", "applied", "limit_exceeded"]


def test_duplicate_replan_request_is_idempotent(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.fail(1, "B")
    request = ctx.request(1)
    first = ctx.replanner().replan(request)
    second = ctx.replanner().replan(request)
    assert first.created is True and second.created is False
    assert second.to_revision == first.to_revision == 2
    assert len(ctx.replan_rows()) == 1
    assert ctx.revisions() == [1, 2]


def test_duplicate_evaluation_is_idempotent(ctx: Ctx) -> None:
    ctx.register(1, [("A", (), "graph")])
    ctx.succeed(1, "A", _ok_fact())
    first, created_first = ctx.evaluate(1)
    second, created_second = ctx.evaluate(1)
    assert created_first is True
    assert created_second is False
    assert second.evidence_digest == first.evidence_digest
    assert second.verdict == first.verdict


def test_stale_expected_revision_raises_conflict(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.fail(1, "B")
    identical = ctx.request(1)
    first = ctx.replanner().replan(identical)
    assert first.to_revision == 2

    # 完全相同的请求 = 幂等重试（返回已应用结果，created=False），不算冲突。
    replay = ctx.replanner().replan(identical)
    assert replay.created is False and replay.to_revision == 2

    # 但客户端视角仍停在 revision 1 的**不同**请求，必须被 CAS 拒绝。
    stale = ctx.request(1, fingerprints={"A": FP_NEW})
    with pytest.raises(ev.ReplanConflictError) as excinfo:
        ctx.replanner().replan(stale)
    assert excinfo.value.code == ev.CODE_REVISION_CONFLICT
    assert ctx.revisions() == [1, 2]


def test_concurrent_same_request_has_single_winner(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.fail(1, "B")
    request = ctx.request(1)
    barrier = threading.Barrier(2)
    results: list[ev.ReplanResult] = []
    errors: list[BaseException] = []

    def worker() -> None:
        conn = sch.connect()
        try:
            barrier.wait(timeout=10)
            results.append(ev.Replanner(conn, POLICY).replan(request))
        except BaseException as exc:  # noqa: BLE001 - 线程内异常回传主线程
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors, errors
    assert len(results) == 2
    assert {item.to_revision for item in results} == {2}
    assert sorted(item.created for item in results) == [False, True]
    assert len(ctx.replan_rows()) == 1
    assert ctx.revisions() == [1, 2]


# ---------------------------------------------------------------------------
# 3) 复用闸门：指纹匹配 / 不匹配 / 已确认副作用
# ---------------------------------------------------------------------------
def test_reuse_allowed_when_fingerprint_matches(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.inputs(1, "A", FP, side_effect_confirmed=True)
    ctx.fail(1, "B")

    result = ctx.replanner().replan(ctx.request(1, fingerprints={"A": FP}))
    assert result.decision == "applied"
    assert result.reused == ("A",)
    assert "A.r1" not in result.replacements


def test_fingerprint_mismatch_with_confirmed_side_effect_goes_review(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.inputs(1, "A", FP, side_effect_confirmed=True)
    ctx.fail(1, "B")

    result = ctx.replanner().replan(ctx.request(1, fingerprints={"A": FP_NEW}))
    assert result.decision == "needs_review"
    assert result.terminal_status == "blocked"
    assert ev.REASON_REUSE_FINGERPRINT_MISMATCH in result.reason
    # 已确认副作用重做 0：不建 revision，也不重跑 A。
    assert ctx.revisions() == [1]
    assert ctx.node_ids(1) == {"A", "B", "C"}


def test_fingerprint_mismatch_without_side_effect_replans_node(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.inputs(1, "A", FP, side_effect_confirmed=False)
    ctx.fail(1, "B")

    result = ctx.replanner().replan(ctx.request(1, fingerprints={"A": FP_NEW}))
    assert result.decision == "applied"
    assert result.reused == ()
    assert result.replacements == {"A": "A.r1", "B": "B.r1"}
    assert ctx.node_ids(2) == {"A.r1", "B.r1", "C"}


# ---------------------------------------------------------------------------
# 4) 预算与终态
# ---------------------------------------------------------------------------
def test_budget_insufficient_blocks_without_releasing_reservation(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.succeed(1, "A", _ok_fact())
    ctx.fail(1, "B")
    budget = ev.StaticBudget(remaining=5)
    replanner = ctx.replanner(budget=budget)

    result = replanner.replan(ctx.request(1, budget=100))
    assert result.decision == "budget_exhausted"
    assert result.terminal_status == "blocked"
    assert result.reason == ev.REASON_BUDGET
    assert result.budget_remaining_microunits == 5
    assert budget.release_calls == 0  # 未知用量预留不得释放
    assert ctx.revisions() == [1]
    status = replanner.run_status(ctx.ws, ctx.run)
    assert status is not None and status["terminal_status"] == "blocked"


def test_noop_when_everything_satisfied(ctx: Ctx) -> None:
    ctx.register(1, [("A", (), "graph")])
    ctx.succeed(1, "A", _ok_fact())
    result = ctx.replanner().replan(ctx.request(1))
    assert result.decision == "noop"
    assert result.terminal_status == "succeeded"
    assert result.reason == ev.REASON_NOTHING_TO_REPLAN
    assert ctx.revisions() == [1]


def test_cancelled_node_is_not_silently_rerun(ctx: Ctx) -> None:
    ctx.register(1, _linear())
    ctx.cancel(1, "A")
    report, _ = ctx.evaluate(1)
    assert report.node("A").verdict == "skipped"
    result = ctx.replanner().replan(ctx.request(1))
    assert result.decision == "noop"
    assert ctx.revisions() == [1]
    assert "A.r1" not in ctx.node_ids(1)


# ---------------------------------------------------------------------------
# 5) 图校验：环/悬空边必须在登记前被 T22 拦截
# ---------------------------------------------------------------------------
def test_replan_graph_with_cycle_is_rejected_before_registration() -> None:
    snapshot = ev.GraphSnapshot(
        graph=dag.TaskGraph(
            nodes=(
                dag.TaskNode(node_id="A", depends_on=("B",)),
                dag.TaskNode(node_id="B", depends_on=("A",)),
            ),
            task_graph_revision=1,
        ),
        optional_edges=frozenset(),
        topo_order=("A", "B"),
    )
    report = ev.EvaluationReport(
        revision=1,
        verdict="partial",
        nodes=(
            ev.NodeEvaluation(
                task_id="A", kind="graph", status="pending", verdict="not_run", reasons=()
            ),
            ev.NodeEvaluation(
                task_id="B", kind="graph", status="pending", verdict="not_run", reasons=()
            ),
        ),
        evidence_digest="x" * 64,
    )
    with pytest.raises(ev.ReplanError) as excinfo:
        ev.build_replan_graph(snapshot, report)
    assert excinfo.value.code == ev.CODE_GRAPH_INVALID
