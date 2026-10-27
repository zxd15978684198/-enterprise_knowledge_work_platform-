"""P0-T23 真实验收：DependencyScheduler、Task 事实、Job 对账与迁移 006。

每个用例都打真实 PostgreSQL(127.0.0.1:5433, schema ``p0_t23``) 与 Redis DB 8；
需要 T08 Job 端口时再连真实 ``job_probe``（schema ``p0_t08``，只读状态 + 官方写路径）。
**没有 mock 的替代品伪装通过**：Job 端口要么是真实 T08，要么是测试里显式的替身
（用于故障注入），两者在用例 docstring 里区分。

对应 Backlog P0-T23 量化验收：

* ready 之外 0 执行；失败按必需依赖传播 blocked，optional 依赖不阻断；
* claim lease+epoch：旧 epoch 领取/完成/引用写入必须失败并留痕；双 scheduler 并发
  每逻辑节点每 epoch 只领一次；
* 结果先事实、checkpoint 引用随后：引用失败可重入恢复，旧 state 不得覆盖事实；
* 固定作业只走 T08 Job 端口；Job 重复完成只唤醒一次；
* 重启对账：按 Task 事实 + Job 状态重算 ready，卡住 running 可回收，已完成不重跑；
* 迁移 006：表/约束/索引齐全，重复 apply 显式失败，空库可重建。

单活前提：T05 结论是 ``PostgresSaver`` 不能 fence 独立 saver 连接 → ``单活 runner``；
本测试只证明单活前提下的 epoch CAS 与对账，不声明多实例并发安全。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import dag_contracts as dag
import job_probe as jp
import psycopg
import pytest
import task_executor as tex
import task_scheduler as sch

SCHEMA = sch.SCHEMA
EXPECTED_TABLES = {
    "phase0_schema_migrations",
    "phase0_task_graphs",
    "phase0_tasks",
    "phase0_task_edges",
    "phase0_task_claims",
    "phase0_task_claim_events",
    "phase0_task_wakes",
}
EXPECTED_INDEXES = {
    "phase0_tasks_ready_idx",
    "phase0_tasks_lease_idx",
    "phase0_tasks_job_idx",
    "phase0_task_edges_to_idx",
    "phase0_task_claims_active_uq",
    "phase0_task_claims_lease_idx",
    "phase0_task_claim_events_task_idx",
    "phase0_task_claim_events_outcome_idx",
}


# ---------------------------------------------------------------------------
# fixtures（conftest.py 属他人文件，DB/Redis fixture 写在用例文件里）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def migration_digest() -> Iterator[str]:
    conn = sch.connect()
    try:
        sch.reset_schema(conn)
        digest = sch.apply_migration(conn)
    finally:
        conn.close()
    assert len(digest) == 64
    yield digest
    conn = sch.connect()
    try:
        sch.reset_schema(conn)
    finally:
        conn.close()


@pytest.fixture()
def pg(migration_digest: str) -> Iterator[psycopg.Connection[Any]]:
    assert migration_digest
    conn = sch.connect()
    conn.execute(f"TRUNCATE {SCHEMA}.phase0_task_graphs CASCADE")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def redis_broker() -> Iterator[Any]:
    broker = sch.redis_client()
    try:
        yield broker
    finally:
        broker.close()


@pytest.fixture()
def scope() -> tuple[UUID, UUID]:
    return uuid4(), uuid4()


# ---------------------------------------------------------------------------
# 测试辅助（真实对象，不是 mock 框架）
# ---------------------------------------------------------------------------
def build_graph(
    nodes: list[tuple[str, tuple[str, ...]]],
    kinds: dict[str, str] | None = None,
    weights: dict[str, int] | None = None,
) -> dag.TaskGraph:
    enriched = []
    for node_id, dependencies in nodes:
        kind = (kinds or {}).get(node_id, "task")
        weight = (weights or {}).get(node_id, 1)
        enriched.append(
            dag.TaskNode(node_id=node_id, depends_on=dependencies, kind=kind, weight=weight)
        )
    return dag.TaskGraph(nodes=tuple(enriched))


def register(
    pg: psycopg.Connection[Any],
    workspace_id: UUID,
    run_id: UUID,
    nodes: list[tuple[str, tuple[str, ...]]] | None = None,
    *,
    revision: int = 1,
    kinds: dict[str, str] | None = None,
    optional: tuple[tuple[str, str], ...] = (),
) -> sch.RegisteredGraph:
    graph = build_graph(nodes or [("A", ()), ("B", ("A",))], kinds)
    return sch.register_graph(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=revision,
        graph=graph,
        optional_dependencies=optional,
    )


class CountingExecutor:
    """显式测试替身：记录执行次数，按需制造失败；不伪装成真实 Graph。"""

    def __init__(self, *, fail_on: frozenset[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    def execute(
        self,
        conn: psycopg.Connection[Any],
        *,
        workspace_id: UUID,
        run_id: UUID,
        revision: int,
        task: sch.TaskState,
        claim: sch.ClaimedTask,
    ) -> None:
        self.calls.append(task.task_id)
        if task.task_id in self.fail_on:
            raise RuntimeError(f"injected failure: {task.task_id}")
        sch.complete_task(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task.task_id,
            epoch=claim.epoch,
            fact={"task_id": task.task_id, "result": "ok"},
            owner_id=claim.owner_id,
        )


class FakeJobPort:
    """测试替身：只为故障注入与路由断言；真实 T08 集成见专用用例。"""

    def __init__(self) -> None:
        self.enqueued: list[sch.JobRequest] = []
        self.states: dict[UUID, str] = {}
        self.applied: dict[UUID, int] = {}

    def enqueue(self, request: sch.JobRequest) -> sch.JobHandle:
        self.enqueued.append(request)
        handle = sch.JobHandle(workspace_id=request.workspace_id, job_id=uuid4())
        self.states[handle.job_id] = "running"
        return handle

    def status(self, handle: sch.JobHandle) -> sch.JobSnapshot:
        state = self.states.get(handle.job_id, "pending")
        return sch.JobSnapshot(
            job_id=handle.job_id, state=state, applied_version=self.applied.get(handle.job_id)
        )


class FakeCheckpointPort:
    """checkpoint 替身：模拟 LangGraph 写引用失败/成功；替身边界在用例里注明。"""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.requests: list[tex.StateRefRequest] = []
        self.version = 0

    def write_state_ref(self, request: tex.StateRefRequest) -> tex.StateRef:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise tex.CheckpointUnavailable("injected: checkpoint store unavailable")
        self.requests.append(request)
        self.version += 1
        return tex.StateRef(
            thread_id=request.thread_id,
            checkpoint_id=f"ckpt-{self.version:04d}",
            version=self.version,
        )


# ---------------------------------------------------------------------------
# 1. 迁移 006：表/约束/索引、重复 apply、空库重建
# ---------------------------------------------------------------------------
def test_migration_tables_constraints_and_indexes(pg: psycopg.Connection[Any]) -> None:
    tables = {
        row["table_name"]
        for row in pg.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (SCHEMA,),
        ).fetchall()
    }
    assert tables == EXPECTED_TABLES, f"表集合不符：{tables ^ EXPECTED_TABLES}"

    indexes = {
        row["indexname"]
        for row in pg.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s", (SCHEMA,)
        ).fetchall()
    }
    assert EXPECTED_INDEXES <= indexes, f"缺少索引：{EXPECTED_INDEXES - indexes}"

    fks = pg.execute(
        "SELECT count(*) AS n FROM pg_constraint "
        "WHERE connamespace = %s::regnamespace AND contype = 'f'",
        (SCHEMA,),
    ).fetchone()
    checks = pg.execute(
        "SELECT count(*) AS n FROM pg_constraint "
        "WHERE connamespace = %s::regnamespace AND contype = 'c'",
        (SCHEMA,),
    ).fetchone()
    assert int(fks["n"]) >= 7, "Task/边/claim/wake/事件的外键必须齐全"
    assert int(checks["n"]) >= 10, "状态/epoch/lease/自环等 CHECK 必须齐全"

    ledger = pg.execute(
        f"SELECT migration_id, filename, sha256 FROM {SCHEMA}.phase0_schema_migrations"
    ).fetchall()
    assert len(ledger) == 1
    assert ledger[0]["migration_id"] == sch.MIGRATION_ID
    assert ledger[0]["sha256"] == sch.schema_digest()


def test_duplicate_apply_fails_explicitly(pg: psycopg.Connection[Any]) -> None:
    with pytest.raises(sch.MigrationAlreadyApplied) as excinfo:
        sch.apply_migration(pg)
    assert excinfo.value.code == sch.CODE_MIGRATION_ALREADY_APPLIED
    assert sch.MIGRATION_FILENAME in str(excinfo.value)
    count = pg.execute(
        f"SELECT count(*) AS n FROM {SCHEMA}.phase0_schema_migrations"
    ).fetchone()
    assert int(count["n"]) == 1, "重复 apply 不得插入第二条台账"


def test_migration_constraints_reject_illegal_rows(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            f"UPDATE {SCHEMA}.phase0_tasks SET status = 'bogus' "
            "WHERE workspace_id = %s AND run_id = %s",
            (workspace_id, run_id),
        )
    pg.rollback()

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg.execute(
            f"INSERT INTO {SCHEMA}.phase0_task_edges "
            "(workspace_id, run_id, revision, from_task, to_task) "
            "VALUES (%s, %s, 1, 'GHOST', 'A')",
            (workspace_id, run_id),
        )
    pg.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            f"INSERT INTO {SCHEMA}.phase0_task_edges "
            "(workspace_id, run_id, revision, from_task, to_task) "
            "VALUES (%s, %s, 1, 'A', 'A')",
            (workspace_id, run_id),
        )
    pg.rollback()

    # claim：同一 epoch 只有一行（PK），活跃 claim 唯一（部分唯一索引）。
    claim = sch.claim_ready(
        pg, workspace_id=workspace_id, run_id=run_id, revision=1, owner_id="w1"
    )[0]
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute(
            f"INSERT INTO {SCHEMA}.phase0_task_claims "
            "(workspace_id, run_id, revision, task_id, epoch, owner_id, lease_until) "
            "VALUES (%s, %s, 1, %s, %s, 'w2', now() + interval '30 seconds')",
            (workspace_id, run_id, claim.task_id, claim.epoch),
        )
    pg.rollback()


def test_empty_schema_rebuild_after_reset(migration_digest: str) -> None:
    conn = sch.connect()
    try:
        sch.reset_schema(conn)
        rebuilt = sch.apply_migration(conn)
        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (SCHEMA,),
            ).fetchall()
        }
    finally:
        conn.close()
    assert rebuilt == migration_digest
    assert tables == EXPECTED_TABLES


# ---------------------------------------------------------------------------
# 2. 图登记：复用 T22 校验、revision 不可变
# ---------------------------------------------------------------------------
def test_register_graph_uses_t22_validation(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    registered = register(
        pg,
        workspace_id,
        run_id,
        [("A", ()), ("B", ("A",)), ("C", ("A", "B"))],
        kinds={"C": "fetch"},
    )
    assert registered.created is True
    assert (registered.node_count, registered.edge_count, registered.depth) == (3, 3, 3)
    stats = dag.validate_graph(build_graph([("A", ()), ("B", ("A",)), ("C", ("A", "B"))]))
    assert registered.graph_sha256 == dag_graph_sha(stats)
    states = sch.task_states(pg, workspace_id, run_id, 1)
    assert set(states) == {"A", "B", "C"}
    assert states["C"].executor_kind == sch.EXECUTOR_FIXED_JOB
    assert states["A"].executor_kind == sch.EXECUTOR_GRAPH
    assert states["A"].status == "pending"


def dag_graph_sha(stats: Any) -> str:
    """按 register_graph 的同一规范重算图摘要（只用于断言一致性）。"""

    import hashlib
    import json

    payload = dag.payload_from_graph(
        build_graph([("A", ()), ("B", ("A",)), ("C", ("A", "B"))], {"C": "fetch"})
    )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_register_graph_rejects_invalid_dag(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    cyclic = build_graph([("A", ("B",)), ("B", ("A",))])
    with pytest.raises(dag.ContractViolation) as excinfo:
        sch.register_graph(
            pg, workspace_id=workspace_id, run_id=run_id, revision=1, graph=cyclic
        )
    assert excinfo.value.first_error.code is dag.ErrorCode.CYCLE
    count = pg.execute(f"SELECT count(*) AS n FROM {SCHEMA}.phase0_tasks").fetchone()
    assert int(count["n"]) == 0, "非法图必须在登记前拒绝，不得落 Task 行"


def test_register_graph_revision_is_immutable(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    first = register(pg, workspace_id, run_id)
    assert first.created is True
    again = register(pg, workspace_id, run_id)
    assert again.created is False, "同 revision 同内容应幂等"
    with pytest.raises(sch.RevisionConflict) as excinfo:
        register(pg, workspace_id, run_id, [("A", ()), ("B", ()), ("C", ("A",))])
    assert excinfo.value.code == sch.CODE_REVISION_CONFLICT


def test_register_graph_rejects_unknown_optional_edge(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    with pytest.raises(sch.SchedulerError) as excinfo:
        register(pg, workspace_id, run_id, [("A", ())], optional=(("A", "B"),))
    assert excinfo.value.code == sch.CODE_GRAPH_INVALID


# ---------------------------------------------------------------------------
# 3. ready 语义：未满足依赖 0 执行
# ---------------------------------------------------------------------------
def test_unmet_dependencies_never_execute(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ()), ("B", ("A",))])
    executor = CountingExecutor()
    assert sch.ready_task_ids(pg, workspace_id, run_id, 1) == ("A",), "B 依赖未满足时不得 ready"
    report = sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=executor,
    )
    assert report.claimed == ("A",)
    assert report.executed == ("A",)
    assert executor.calls == ["A"], "依赖未满足的 B 必须 0 执行"
    states = sch.task_states(pg, workspace_id, run_id, 1)
    assert states["B"].status == "pending"
    assert states["B"].attempt == 0
    assert states["B"].epoch == 0, "B 从未被领取"
    assert sch.ready_task_ids(pg, workspace_id, run_id, 1) == ("B",), "A 成功后 B 才 ready"

    second = sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=executor,
    )
    assert second.executed == ("B",)
    assert executor.calls == ["A", "B"]


def test_claim_task_refuses_unready_node(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ()), ("B", ("A",))])
    with pytest.raises(sch.SchedulerError) as excinfo:
        sch.claim_task(
            pg,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=1,
            task_id="B",
            owner_id="w1",
        )
    assert excinfo.value.code == sch.CODE_NOT_READY


def test_required_failure_blocks_transitively(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ()), ("B", ("A",)), ("C", ("B",))])
    executor = CountingExecutor(fail_on=frozenset({"A"}))
    report = sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=executor,
    )
    assert report.failures == ("A",)
    states = sch.task_states(pg, workspace_id, run_id, 1)
    assert states["A"].status == "failed"
    assert states["B"].status == "blocked"
    assert states["C"].status == "blocked", "阻断必须递归传播"
    assert states["C"].last_error is not None

    again = sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=executor,
    )
    assert again.executed == (), "blocked 节点不得再执行"
    assert executor.calls == ["A"]


def test_optional_dependency_failure_does_not_block(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(
        pg,
        workspace_id,
        run_id,
        [("A", ()), ("B", ("A",))],
        optional=(("A", "B"),),
    )
    executor = CountingExecutor(fail_on=frozenset({"A"}))
    first = sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=executor,
    )
    assert first.claimed == ("A",) and first.executed == () and first.failures == ("A",), (
        "失败节点计入 failures，不算 executed"
    )
    states = sch.task_states(pg, workspace_id, run_id, 1)
    assert states["B"].status == "pending", "optional 依赖失败不得阻断下游"

    second = sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=executor,
    )
    assert second.executed == ("B",)
    assert sch.task_states(pg, workspace_id, run_id, 1)["B"].status == "succeeded"


def test_cancel_blocks_dependents_and_late_completion_rejected(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ()), ("B", ("A",))])
    blocked = sch.cancel_task(
        pg, workspace_id=workspace_id, run_id=run_id, revision=1, task_id="A", reason="user"
    )
    assert blocked == ("B",)
    states = sch.task_states(pg, workspace_id, run_id, 1)
    assert states["A"].status == "cancelled"
    assert states["B"].status == "blocked"
    with pytest.raises(sch.SchedulerError):
        sch.complete_task(
            pg,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=1,
            task_id="A",
            epoch=1,
            fact={"late": True},
        )


# ---------------------------------------------------------------------------
# 4. claim lease + epoch：旧 epoch 失败留痕、并发单领
# ---------------------------------------------------------------------------
def test_claim_increments_epoch_and_records_ledger(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ())])
    claim = sch.claim_task(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        task_id="A",
        owner_id="w1",
        expected_epoch=0,
    )
    assert claim.epoch == 1
    state = sch.task_state(pg, workspace_id, run_id, 1, "A")
    assert state.status == "running" and state.lease_owner == "w1"
    claims = pg.execute(
        f"SELECT epoch, owner_id, status FROM {SCHEMA}.phase0_task_claims "
        "WHERE workspace_id = %s AND task_id = 'A'",
        (workspace_id,),
    ).fetchall()
    assert [(row["epoch"], row["owner_id"], row["status"]) for row in claims] == [(1, "w1", "active")]


def test_stale_epoch_claim_rejected_with_audit(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ())])
    first = sch.claim_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        lease_seconds=0.01,
    )[0]
    assert first.epoch == 1
    reclaimed = sch.reclaim_expired(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        now=datetime.now(UTC) + timedelta(seconds=5),
    )
    assert reclaimed == ("A",)
    second = sch.claim_task(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        task_id="A",
        owner_id="w2",
    )
    assert second.epoch == 2
    with pytest.raises(sch.StaleEpoch) as excinfo:
        sch.claim_task(
            pg,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=1,
            task_id="A",
            owner_id="w3",
            expected_epoch=1,
        )
    assert excinfo.value.code == sch.CODE_STALE_EPOCH
    events = sch.list_events(pg, workspace_id, run_id, 1, task_id="A")
    stale = [event for event in events if event["outcome"] == "stale_epoch"]
    assert stale, "旧 epoch 领取必须留痕"
    assert any("claim_expected=1" in str(event["detail"]) for event in stale)


def test_old_epoch_completion_and_attach_rejected(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ())])
    first = sch.claim_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        lease_seconds=0.01,
    )[0]
    sch.reclaim_expired(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        now=datetime.now(UTC) + timedelta(seconds=5),
    )
    second = sch.claim_ready(
        pg, workspace_id=workspace_id, run_id=run_id, revision=1, owner_id="w2"
    )[0]
    assert (first.epoch, second.epoch) == (1, 2)

    with pytest.raises(sch.StaleEpoch):
        sch.complete_task(
            pg,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=1,
            task_id="A",
            epoch=1,
            fact={"stale": True},
        )
    state = sch.task_state(pg, workspace_id, run_id, 1, "A")
    assert state.status == "running" and state.epoch == 2
    assert state.fact is None, "旧 epoch 的完成不得写入事实"

    sch.complete_task(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        task_id="A",
        epoch=2,
        fact={"ok": True},
    )
    with pytest.raises(sch.StaleStateRef) as excinfo:
        sch.attach_state_ref(
            pg,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=1,
            task_id="A",
            epoch=1,
            state_ref={"thread_id": "stale", "checkpoint_id": "old", "version": 1},
        )
    assert excinfo.value.code == sch.CODE_STALE_STATE_REF
    state = sch.task_state(pg, workspace_id, run_id, 1, "A")
    assert state.fact == {"ok": True}, "旧 state 不得覆盖新事实"
    assert state.graph_state_ref is None


def test_concurrent_double_scheduler_claims_each_node_once(
    pg: psycopg.Connection[Any], migration_digest: str
) -> None:
    assert migration_digest
    rounds = 30
    duplicates: list[str] = []
    for round_index in range(rounds):
        workspace_id, run_id = uuid4(), uuid4()
        register(
            pg,
            workspace_id,
            run_id,
            [("A", ()), ("B", ()), ("C", ()), ("D", ())],
        )
        barrier = threading.Barrier(2)
        results: list[list[str]] = []
        lock = threading.Lock()

        def worker(owner: str) -> None:
            conn = sch.connect()
            try:
                barrier.wait(timeout=10)
                claimed = sch.claim_ready(
                    conn,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    revision=1,
                    owner_id=owner,
                    limit=10,
                )
                with lock:
                    results.append([task.task_id for task in claimed])
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=(f"w{index}",), daemon=True)
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive(), "并发 claim 线程未在超时内结束"
        flat = [task for batch in results for task in batch]
        if len(flat) != 4 or len(set(flat)) != 4:
            duplicates.append(f"round={round_index} claimed={flat}")
    assert not duplicates, f"每逻辑节点每 epoch 必须只领一次：{duplicates}"

    rows = pg.execute(
        f"SELECT count(*) AS n FROM {SCHEMA}.phase0_task_claims"
    ).fetchone()
    assert int(rows["n"]) == rounds * 4, (
        "每轮 4 个节点各领一次 = 120 条 claim；出现更多即为双领取"
    )


def test_lease_expiry_reclaims_without_reexecuting_completed(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ()), ("B", ("A",))])
    executor = CountingExecutor()
    sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=executor,
    )  # A done
    stuck = sch.claim_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        lease_seconds=0.01,
    )[0]
    assert stuck.task_id == "B"
    reclaimed = sch.reclaim_expired(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        now=datetime.now(UTC) + timedelta(seconds=5),
    )
    assert reclaimed == ("B",)
    state = sch.task_state(pg, workspace_id, run_id, 1, "B")
    assert state.status == "pending" and state.epoch == 1
    assert sch.ready_task_ids(pg, workspace_id, run_id, 1) == ("B",)
    assert "A" not in sch.ready_task_ids(pg, workspace_id, run_id, 1)
    claims = pg.execute(
        f"SELECT status FROM {SCHEMA}.phase0_task_claims "
        "WHERE workspace_id = %s AND task_id = 'B'",
        (workspace_id,),
    ).fetchall()
    assert [row["status"] for row in claims] == ["expired"]
    assert executor.calls == ["A"], "已完成的 A 不得重跑"


# ---------------------------------------------------------------------------
# 5. 事实先行、checkpoint 引用随后
# ---------------------------------------------------------------------------
def test_fact_committed_when_checkpoint_write_fails_then_recovers(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ())])
    runs: list[str] = []
    port = FakeCheckpointPort(fail_times=1)
    executor = tex.TaskExecutor(lambda task: runs.append(task.task_id) or {"value": 1}, port)
    claim = sch.claim_ready(
        pg, workspace_id=workspace_id, run_id=run_id, revision=1, owner_id="w1"
    )[0]
    state = sch.task_state(pg, workspace_id, run_id, 1, "A")
    outcome = executor.execute(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        task=state,
        claim=claim,
    )
    assert outcome.fact_committed is True
    assert outcome.ref_pending is True
    assert outcome.error_code == tex.CODE_CHECKPOINT_UNAVAILABLE
    assert runs == ["A"], "节点只执行一次"

    fact_state = sch.task_state(pg, workspace_id, run_id, 1, "A")
    assert fact_state.status == "succeeded", "checkpoint 失败不得回滚事实"
    assert fact_state.fact is not None
    assert fact_state.graph_state_ref is None
    assert fact_state.last_error is not None and "CHECKPOINT_UNAVAILABLE" in fact_state.last_error

    recovered = executor.resume_pending_state_refs(
        pg, workspace_id=workspace_id, run_id=run_id, revision=1
    )
    assert len(recovered) == 1
    assert recovered[0].state_ref is not None
    final = sch.task_state(pg, workspace_id, run_id, 1, "A")
    assert final.graph_state_ref is not None
    assert final.graph_state_ref["thread_id"] == tex.run_thread_id(run_id)
    assert final.fact == fact_state.fact, "重入不得改事实"
    assert runs == ["A"], "重入只补引用，不得重新执行"
    events = [event["outcome"] for event in sch.list_events(pg, workspace_id, run_id, 1, task_id="A")]
    assert "state_ref_pending" in events and "state_ref_attached" in events


def test_stale_checkpoint_cannot_overwrite_fact(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ())])
    port = FakeCheckpointPort()
    executor = tex.TaskExecutor(lambda task: {"value": "fresh"}, port)
    claim = sch.claim_ready(
        pg, workspace_id=workspace_id, run_id=run_id, revision=1, owner_id="w1"
    )[0]
    state = sch.task_state(pg, workspace_id, run_id, 1, "A")
    executor.execute(
        pg, workspace_id=workspace_id, run_id=run_id, revision=1, task=state, claim=claim
    )
    before = sch.task_state(pg, workspace_id, run_id, 1, "A")
    assert before.graph_state_ref is not None
    with pytest.raises(sch.StaleStateRef):
        sch.attach_state_ref(
            pg,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=1,
            task_id="A",
            epoch=claim.epoch - 1,
            state_ref={"thread_id": "run:old", "checkpoint_id": "old", "version": 0},
        )
    after = sch.task_state(pg, workspace_id, run_id, 1, "A")
    assert after.fact == before.fact
    assert after.graph_state_ref == before.graph_state_ref
    outcomes = [
        event["outcome"]
        for event in sch.list_events(pg, workspace_id, run_id, 1, task_id="A")
    ]
    assert "stale_state_ref" in outcomes


# ---------------------------------------------------------------------------
# 6. Job 路径：固定作业走 T08 port；重复完成只唤醒一次
# ---------------------------------------------------------------------------
def test_fixed_job_is_routed_through_job_port(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(
        pg,
        workspace_id,
        run_id,
        [("G", ()), ("J1", ("G",))],
        kinds={"J1": "fetch"},
    )
    port = FakeJobPort()
    executor = CountingExecutor()
    report = sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=executor,
        job_port=port,
        limit=10,
    )
    assert report.executed == ("G",)
    assert report.job_dispatched == (), "J1 依赖 G，首轮不应派发"
    second = sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=executor,
        job_port=port,
        limit=10,
    )
    assert second.job_dispatched == ("J1",)
    assert len(port.enqueued) == 1, "固定作业必须且只能走一次 Job port"
    assert "J1" not in executor.calls, "固定作业不得走 Graph executor"
    state = sch.task_state(pg, workspace_id, run_id, 1, "J1")
    assert state.job_id is not None and state.status == "running"
    assert sch.ready_task_ids(pg, workspace_id, run_id, 1) == (), "等 Job 的节点不是 ready"


def test_duplicate_job_completion_wakes_once(
    pg: psycopg.Connection[Any],
    scope: tuple[UUID, UUID],
    redis_broker: Any,
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("J1", ())], kinds={"J1": "fetch"})
    port = FakeJobPort()
    sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        job_port=port,
        limit=10,
    )
    state = sch.task_state(pg, workspace_id, run_id, 1, "J1")
    wake_key = f"job:{state.job_id}:succeeded"
    first = sch.wake_once(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        task_id="J1",
        epoch=state.epoch,
        wake_key=wake_key,
        broker=redis_broker,
    )
    second = sch.wake_once(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        task_id="J1",
        epoch=state.epoch,
        wake_key=wake_key,
        broker=redis_broker,
    )
    assert (first, second) == (True, False), "Job 重复完成只能唤醒一次"
    wakes = pg.execute(
        f"SELECT count(*) AS n FROM {SCHEMA}.phase0_task_wakes WHERE task_id = 'J1'"
    ).fetchone()
    assert int(wakes["n"]) == 1
    key = sch.wake_redis_key(workspace_id, run_id, 1, "J1", state.epoch, wake_key)
    assert redis_broker.get(key) == "1"
    outcomes = [event["outcome"] for event in sch.list_events(pg, workspace_id, run_id, 1)]
    assert outcomes.count("wake") == 1 and outcomes.count("wake_duplicate") == 1
    redis_broker.delete(key)


def test_reconcile_completes_task_from_job_success(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("J1", ())], kinds={"J1": "fetch"})
    port = FakeJobPort()
    sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        job_port=port,
        limit=10,
    )
    state = sch.task_state(pg, workspace_id, run_id, 1, "J1")
    assert state.job_id is not None
    port.states[state.job_id] = "succeeded"
    port.applied[state.job_id] = 7
    report = sch.reconcile(
        pg, workspace_id=workspace_id, run_id=run_id, revision=1, job_port=port
    )
    assert report.completed_from_job == ("J1",)
    done = sch.task_state(pg, workspace_id, run_id, 1, "J1")
    assert done.status == "succeeded"
    assert done.fact is not None and done.fact["applied_version"] == 7


def test_real_t08_job_port_integration(pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]) -> None:
    """真实 T08：enqueue/record_result 走 job_probe 官方函数，status 只读 p0_t08。"""

    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("J1", ())], kinds={"J1": "fetch"})
    jp_conn = jp.connect()
    try:
        jp.ensure_schema(jp_conn)
        port = sch.JobServicePort(jp_conn)
        sch.dispatch_ready(
            pg,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=1,
            owner_id="w1",
            job_port=port,
            limit=10,
        )
        state = sch.task_state(pg, workspace_id, run_id, 1, "J1")
        assert state.job_id is not None
        handle = sch.JobHandle(workspace_id=workspace_id, job_id=state.job_id)
        running = port.status(handle)
        assert running.state in ("pending", "running")
        recorded = port.record_result(handle, outcome="succeeded", applied_version=11)
        assert recorded is True
        assert port.record_result(handle, outcome="succeeded", applied_version=11) is False, (
            "T08 同一 generation 重复结果必须被拒绝"
        )
        report = sch.reconcile(
            pg, workspace_id=workspace_id, run_id=run_id, revision=1, job_port=port
        )
        assert report.completed_from_job == ("J1",)
        done = sch.task_state(pg, workspace_id, run_id, 1, "J1")
        assert done.status == "succeeded"
        assert done.fact is not None and done.fact["applied_version"] == 11
        # 真实 Job 端口下，重复唤醒同样只生效一次。
        assert (
            sch.wake_once(
                pg,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=1,
                task_id="J1",
                epoch=done.epoch,
                wake_key=f"job:{state.job_id}:succeeded",
            )
            is True
        )
        assert (
            sch.wake_once(
                pg,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=1,
                task_id="J1",
                epoch=done.epoch,
                wake_key=f"job:{state.job_id}:succeeded",
            )
            is False
        )
    finally:
        jp_conn.close()


# ---------------------------------------------------------------------------
# 7. 重启对账：按事实重算 ready，卡住 running 可回收，已完成不重跑
# ---------------------------------------------------------------------------
def test_reconcile_recomputes_ready_after_restart(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID], migration_digest: str
) -> None:
    assert migration_digest
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ()), ("B", ("A",)), ("C", ("B",))])
    first_executor = CountingExecutor()
    sch.dispatch_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        executor=first_executor,
    )
    # 模拟调度器重启：丢弃内存状态，只用 DB 事实重新对账。
    restarted = sch.connect()
    try:
        report = sch.reconcile(restarted, workspace_id=workspace_id, run_id=run_id, revision=1)
        assert report.ready == ("B",)
        assert report.reclaimed == ()
        second_executor = CountingExecutor()
        sch.dispatch_ready(
            restarted,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=1,
            owner_id="w2",
            executor=second_executor,
        )
        assert second_executor.calls == ["B"]
        third = sch.reconcile(restarted, workspace_id=workspace_id, run_id=run_id, revision=1)
        assert third.ready == ("C",)
        assert "A" not in third.ready and "B" not in third.ready
    finally:
        restarted.close()


def test_reconcile_reclaims_stuck_running_but_not_completed(
    pg: psycopg.Connection[Any], scope: tuple[UUID, UUID]
) -> None:
    workspace_id, run_id = scope
    register(pg, workspace_id, run_id, [("A", ()), ("B", ())])
    executor = CountingExecutor()
    first = sch.claim_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        limit=1,
        lease_seconds=0.01,
    )[0]
    assert first.task_id == "A"
    sch.complete_task(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        task_id="A",
        epoch=first.epoch,
        fact={"done": True},
        owner_id="w1",
    )
    stuck = sch.claim_ready(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        owner_id="w1",
        limit=1,
        lease_seconds=0.01,
    )[0]
    assert stuck.task_id == "B"
    report = sch.reconcile(
        pg,
        workspace_id=workspace_id,
        run_id=run_id,
        revision=1,
        now=datetime.now(UTC) + timedelta(seconds=5),
    )
    assert report.reclaimed == ("B",)
    assert report.ready == ("B",)
    assert "A" not in report.ready
    assert executor.calls == []


def test_redis_slot_isolation(redis_broker: Any) -> None:
    db = redis_broker.connection_pool.connection_kwargs.get("db")
    assert db == sch.REDIS_DB == 8
    other = sch.redis_client.__globals__["os"].environ.get("PHASE0_T23_REDIS_URL")
    del other
    import redis as redis_module

    wrong = redis_module.Redis.from_url("redis://127.0.0.1:6380/0")
    try:
        with pytest.raises(sch.SchedulerError) as excinfo:
            sch.ensure_redis_slot(wrong)
        assert excinfo.value.code == sch.CODE_REDIS_SLOT
    finally:
        wrong.close()


def test_no_cross_domain_writes_in_scheduler_and_executor() -> None:
    """静态守卫：Task 模块不得写其他域的表（Job=p0_t08、checkpoint=p0_t05 等）。"""

    scheduler_source = (sch.__file__ or "").replace("\\", "/")
    with open(scheduler_source, encoding="utf-8") as handle:
        source = handle.read()
    for foreign in ("p0_t03", "p0_t05", "p0_t07", "p0_t08"):
        assert f"INSERT INTO {foreign}" not in source
        assert f"UPDATE {foreign}" not in source
        assert f"DELETE FROM {foreign}" not in source
    executor_source = (tex.__file__ or "").replace("\\", "/")
    with open(executor_source, encoding="utf-8") as handle:
        executor_text = handle.read()
    assert "INSERT INTO" not in executor_text, "executor 不得直接写任何表，只能调用调度器函数"
