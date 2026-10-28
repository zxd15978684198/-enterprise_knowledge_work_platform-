"""P0-T24：Evaluator（事实判定）与有界重规划（revision CAS + 产物复用）。

契约来源
--------
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T24：确定性硬规则 + 结构化评价 →
  ``expected_revision`` CAS → 旧执行节点安全收尾 → 保留兼容成功引用/新节点 →
  次数/预算停止；量化验收「revision 只有一活动指针、已确认副作用重做 0、
  未知用量预留不释放、最多 2 次重规划后停止、不匹配 input/profile/policy 不复用」。
* ``docs/tdd/03_Task_Orchestration.md`` §4/§6：revision 是计划产物，Task 事实由
  OrchestrationService 唯一写入；Evaluator 只消费事实与回执，不重定义状态机。
* ``experiments/phase0/task_scheduler.py``（T23）：复用 ``register_graph`` 的
  单事务冻结、``TaskState`` 事实字段与 ``RevisionConflict``；**本模块不修改也不
  重新实现 Task 写入**。

边界（本任务不做）
------------------
* 不执行任务、不调度、不新建编号迁移：调度与领取归 T23，本模块只读 T23 的事实。
* 不做预算账本本身：预算只经 :class:`BudgetPort` 读取剩余额度，且**绝不调用
  release**——未知用量的预留必须保守保留（T20 契约）。
* 不实现人工评审 UI：无法安全自动重规划时返回 ``needs_review`` 并进入终态
  ``blocked``，由人工介入，绝不无限循环。

关键设计
--------
1. **评估输入是事实**：:func:`evaluate_run` 只从 T23 的 Task 事实（``fact`` +
   ``fact_version`` + 状态）和本模块的工具回执表读取；节点自述文本不参与判定，
   ``succeeded`` 但缺回执/缺必需事实键 → ``partial``。
2. **唯一活动指针**：revision 以 T23 ``phase0_task_graphs`` 的最大 revision 为
   权威，``p0_t24.phase0_runs.active_revision`` 只做审计镜像；同 revision 同内容
   幂等（T23 语义），不同内容抛 ``RevisionConflict``。
3. **只重规划未完成部分**：新 revision 只登记「未满足（failed/partial）」的替换
   节点与「未完成（blocked/not_run）」的延续节点；已满足节点**不重复登记**，
   只以 ``reused`` 清单复用其事实/回执引用，因此不会复制已完成子任务、也不会
   重做已确认副作用。
4. **次数上限硬约束**：第 1、2 次可 ``applied``，第 3 次起 ``limit_exceeded`` 并
   把 run 置 ``failed``；``phase0_runs.replan_count`` 带 ``CHECK BETWEEN 0 AND 2``，
   数据库层同样拒绝超限。
5. **指纹复用闸门**：已完成节点只有在 ``input_digest/profile/policy`` 与本次
   请求声明的指纹一致时才进 ``reused``；已确认副作用的节点指纹不匹配时**不重跑**，
   直接 ``needs_review``。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol
from uuid import UUID

import psycopg
from psycopg import errors as pg_errors
from psycopg.pq import TransactionStatus

import dag_contracts as dag
import task_scheduler as sch

__all__ = [
    "MAX_REPLANS",
    "REDIS_DB",
    "SCHEMA",
    "Acceptance",
    "BudgetPort",
    "EvaluationError",
    "EvaluationPolicy",
    "EvaluationReport",
    "Fingerprint",
    "GraphSnapshot",
    "NodeEvaluation",
    "Receipt",
    "ReplanConflictError",
    "ReplanError",
    "ReplanNeedsReview",
    "ReplanPlan",
    "ReplanRequest",
    "ReplanResult",
    "Replanner",
    "StaticBudget",
    "TaskInputs",
    "TaskOutcome",
    "build_replan_graph",
    "ensure_schema",
    "evaluate_outcomes",
    "evaluate_run",
    "evaluation_digest",
    "load_graph_snapshot",
    "load_inputs",
    "load_outcomes",
    "load_receipts",
    "outcome_from_state",
    "persist_evaluation",
    "record_inputs",
    "record_receipt",
    "replan_request_digest",
    "reset_schema",
]

#: 本任务独占的 PG schema（wave record 的 resource_slot）。
SCHEMA: Final = "p0_t24"
#: wave record 分配的 Redis DB；本模块暂无 Redis 写入需求，保留常量便于对账。
REDIS_DB: Final = 10
#: 有界重规划的上限（Backlog 量化验收：最多 2 次后停止）。
MAX_REPLANS: Final = 2

VERDICTS: Final = ("satisfied", "partial", "failed", "blocked", "not_run", "skipped")
OVERALL_VERDICTS: Final = ("satisfied", "partial", "failed", "blocked")
DECISIONS: Final = ("applied", "noop", "needs_review", "limit_exceeded", "budget_exhausted")
TERMINALS: Final = ("running", "succeeded", "failed", "blocked")

#: 稳定原因码：证据与断言只引用这些值，不比对自然语言。
REASON_OK: Final = "OK"
REASON_FACT_MISSING: Final = "FACT_MISSING"
REASON_FACT_KEYS_MISSING: Final = "FACT_KEYS_MISSING"
REASON_FACT_VERSION_TOO_LOW: Final = "FACT_VERSION_TOO_LOW"
REASON_RECEIPT_MISSING: Final = "RECEIPT_MISSING"
REASON_RECEIPT_FAILED: Final = "RECEIPT_NOT_SUCCEEDED"
REASON_ACCEPTANCE_UNLABELED: Final = "ACCEPTANCE_UNLABELED"
REASON_STATUS_FAILED: Final = "STATUS_FAILED"
REASON_STATUS_BLOCKED: Final = "STATUS_BLOCKED"
REASON_STATUS_CANCELLED: Final = "STATUS_CANCELLED"
REASON_STATUS_UNSETTLED: Final = "STATUS_UNSETTLED"
REASON_EMPTY: Final = "EMPTY_EVALUATION"
REASON_REUSE_FINGERPRINT_MISMATCH: Final = "REUSE_FINGERPRINT_MISMATCH"
REASON_REUSE_INPUTS_UNRECORDED: Final = "REUSE_INPUTS_UNRECORDED"
REASON_SIDE_EFFECT_CONFIRMED: Final = "SIDE_EFFECT_ALREADY_CONFIRMED"
REASON_DEPENDENCY_CANCELLED: Final = "DEPENDENCY_CANCELLED"
REASON_REPLAN_LIMIT: Final = "REPLAN_LIMIT_EXCEEDED"
REASON_BUDGET: Final = "BUDGET_INSUFFICIENT"
REASON_NOTHING_TO_REPLAN: Final = "NOTHING_TO_REPLAN"

CODE_GRAPH_INVALID: Final = "REPLAN_GRAPH_INVALID"
CODE_REVISION_CONFLICT: Final = "REPLAN_REVISION_CONFLICT"
CODE_NEEDS_REVIEW: Final = "REPLAN_NEEDS_REVIEW"
CODE_INVALID_REQUEST: Final = "REPLAN_INVALID_REQUEST"
CODE_SCHEMA_MISSING: Final = "SCHEMA_MISSING"


# ---------------------------------------------------------------------------
# 错误：稳定 code + detail，便于证据断言
# ---------------------------------------------------------------------------
class EvaluationError(RuntimeError):
    """评估阶段错误（请求非法、schema 缺失等）。"""

    code: str = "EVALUATION_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ReplanError(EvaluationError):
    """重规划错误基类。"""

    code = "REPLAN_ERROR"


class ReplanConflictError(ReplanError):
    """``expected_revision`` 与当前活动 revision 不一致（CAS 失败）。"""

    code = CODE_REVISION_CONFLICT


class ReplanNeedsReview(ReplanError):
    """无法安全自动重规划：转人工评审，不创建新 revision。"""

    code = CODE_NEEDS_REVIEW


# ---------------------------------------------------------------------------
# 事实/回执/策略数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """T23 落库事实的快照；评估只信这里，不信任何自述文本。"""

    task_id: str
    kind: str
    status: str
    fact: Mapping[str, Any] | None
    fact_version: int
    required: bool = True
    weight: int = 1
    last_error: str | None = None


def outcome_from_state(state: sch.TaskState) -> TaskOutcome:
    """把 T23 ``TaskState`` 转成只读快照。"""

    return TaskOutcome(
        task_id=state.task_id,
        kind=state.kind,
        status=state.status,
        fact=state.fact,
        fact_version=state.fact_version,
        required=state.required,
        weight=state.weight,
        last_error=state.last_error,
    )


@dataclass(frozen=True, slots=True)
class Receipt:
    """工具/子流程回执：既是证据也是去重键（``external_id``）。"""

    task_id: str
    tool: str
    status: Literal["succeeded", "failed"]
    applied_version: int | None = None
    external_id: str = ""


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """产物复用闸门的输入指纹：输入、profile、policy 三者一致才允许复用。"""

    input_digest: str
    profile: str
    policy: str

    def matches(self, other: Fingerprint) -> bool:
        return (
            self.input_digest == other.input_digest
            and self.profile == other.profile
            and self.policy == other.policy
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "input_digest": self.input_digest,
            "profile": self.profile,
            "policy": self.policy,
        }


@dataclass(frozen=True, slots=True)
class TaskInputs:
    """某节点在某 revision 的输入指纹 + 副作用是否已确认。"""

    fingerprint: Fingerprint
    side_effect_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class Acceptance:
    """单节点验收规格；``required_keys`` 支持 ``a.b.c`` 形式的嵌套键。"""

    required_keys: tuple[str, ...] = ()
    min_fact_version: int = 1
    required_receipts: int = 0
    accepted_receipt_status: Literal["succeeded"] = "succeeded"


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    """评价策略：先按 task id、再按 kind 查；都没有 → 未标注（partial + 人工评审）。"""

    per_task: Mapping[str, Acceptance] = field(default_factory=dict)
    per_kind: Mapping[str, Acceptance] = field(default_factory=dict)
    default: Acceptance | None = None

    def acceptance_for(self, task_id: str, kind: str) -> Acceptance | None:
        if task_id in self.per_task:
            return self.per_task[task_id]
        if kind in self.per_kind:
            return self.per_kind[kind]
        return self.default

    def digest_payload(self) -> dict[str, Any]:
        def dump(acceptance: Acceptance | None) -> dict[str, Any] | None:
            if acceptance is None:
                return None
            return {
                "required_keys": list(acceptance.required_keys),
                "min_fact_version": acceptance.min_fact_version,
                "required_receipts": acceptance.required_receipts,
                "accepted_receipt_status": acceptance.accepted_receipt_status,
            }

        return {
            "per_task": {key: dump(value) for key, value in sorted(self.per_task.items())},
            "per_kind": {key: dump(value) for key, value in sorted(self.per_kind.items())},
            "default": dump(self.default),
        }


@dataclass(frozen=True, slots=True)
class NodeEvaluation:
    """单节点判定结果；``reasons`` 为空表示 ``OK``。"""

    task_id: str
    kind: str
    status: str
    verdict: Literal["satisfied", "partial", "failed", "blocked", "not_run", "skipped"]
    reasons: tuple[str, ...]
    fact_version: int = 0
    receipts_seen: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "fact_version": self.fact_version,
            "receipts_seen": self.receipts_seen,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NodeEvaluation:
        return cls(
            task_id=str(payload["task_id"]),
            kind=str(payload["kind"]),
            status=str(payload["status"]),
            verdict=str(payload["verdict"]),  # type: ignore[arg-type]
            reasons=tuple(str(item) for item in payload.get("reasons", ())),
            fact_version=int(payload.get("fact_version", 0)),
            receipts_seen=int(payload.get("receipts_seen", 0)),
        )


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """一次评估的完整结果；``evidence_digest`` 同时是幂等键。"""

    revision: int
    verdict: Literal["satisfied", "partial", "failed", "blocked"]
    nodes: tuple[NodeEvaluation, ...]
    evidence_digest: str
    workspace_id: UUID | None = None
    run_id: UUID | None = None
    evaluated_at: datetime | None = None

    def node(self, task_id: str) -> NodeEvaluation:
        for item in self.nodes:
            if item.task_id == task_id:
                return item
        raise KeyError(task_id)

    @property
    def ok(self) -> bool:
        return self.verdict == "satisfied"

    @property
    def failed_ids(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.nodes if item.verdict == "failed")

    @property
    def partial_ids(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.nodes if item.verdict == "partial")

    @property
    def blocked_ids(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.nodes if item.verdict == "blocked")

    @property
    def not_run_ids(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.nodes if item.verdict == "not_run")

    @property
    def skipped_ids(self) -> tuple[str, ...]:
        return tuple(item.task_id for item in self.nodes if item.verdict == "skipped")

    @property
    def replan_required(self) -> bool:
        return bool(self.failed_ids or self.partial_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "verdict": self.verdict,
            "evidence_digest": self.evidence_digest,
            "workspace_id": str(self.workspace_id) if self.workspace_id else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "evaluated_at": (self.evaluated_at or datetime.now(UTC)).isoformat(),
            "nodes": [item.to_dict() for item in self.nodes],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvaluationReport:
        workspace = payload.get("workspace_id")
        run = payload.get("run_id")
        evaluated = payload.get("evaluated_at")
        return cls(
            revision=int(payload["revision"]),
            verdict=str(payload["verdict"]),  # type: ignore[arg-type]
            nodes=tuple(NodeEvaluation.from_dict(item) for item in payload["nodes"]),
            evidence_digest=str(payload["evidence_digest"]),
            workspace_id=UUID(str(workspace)) if workspace else None,
            run_id=UUID(str(run)) if run else None,
            evaluated_at=datetime.fromisoformat(str(evaluated)) if evaluated else None,
        )


# ---------------------------------------------------------------------------
# 纯函数：评估
# ---------------------------------------------------------------------------
def _resolve_key(payload: Mapping[str, Any], dotted: str) -> bool:
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False
        current = current[part]
    return True


def _receipts_by_task(receipts: Sequence[Receipt]) -> dict[str, list[Receipt]]:
    grouped: dict[str, list[Receipt]] = {}
    for receipt in receipts:
        grouped.setdefault(receipt.task_id, []).append(receipt)
    return grouped


def _evaluate_node(
    outcome: TaskOutcome,
    receipts: Sequence[Receipt],
    policy: EvaluationPolicy,
) -> NodeEvaluation:
    acceptance = policy.acceptance_for(outcome.task_id, outcome.kind)
    seen = len(receipts)
    base = {"task_id": outcome.task_id, "kind": outcome.kind, "status": outcome.status,
            "fact_version": outcome.fact_version, "receipts_seen": seen}

    if outcome.status == "failed":
        return NodeEvaluation(verdict="failed", reasons=(REASON_STATUS_FAILED,), **base)
    if outcome.status == "cancelled":
        return NodeEvaluation(verdict="skipped", reasons=(REASON_STATUS_CANCELLED,), **base)
    if outcome.status == "blocked":
        return NodeEvaluation(verdict="blocked", reasons=(REASON_STATUS_BLOCKED,), **base)
    if outcome.status in {"pending", "running"}:
        return NodeEvaluation(verdict="not_run", reasons=(REASON_STATUS_UNSETTLED,), **base)
    if outcome.status != "succeeded":
        return NodeEvaluation(verdict="partial", reasons=(REASON_STATUS_UNSETTLED,), **base)

    if acceptance is None:
        return NodeEvaluation(verdict="partial", reasons=(REASON_ACCEPTANCE_UNLABELED,), **base)
    if outcome.fact is None:
        return NodeEvaluation(verdict="partial", reasons=(REASON_FACT_MISSING,), **base)

    reasons: list[str] = []
    missing = tuple(key for key in acceptance.required_keys if not _resolve_key(outcome.fact, key))
    if missing:
        reasons.append(f"{REASON_FACT_KEYS_MISSING}:{','.join(missing)}")
    if outcome.fact_version < acceptance.min_fact_version:
        reasons.append(REASON_FACT_VERSION_TOO_LOW)
    if acceptance.required_receipts > 0:
        ok_receipts = [
            item
            for item in receipts
            if item.status == acceptance.accepted_receipt_status
        ]
        if len(ok_receipts) < acceptance.required_receipts:
            failed_receipts = [item for item in receipts if item.status == "failed"]
            reasons.append(REASON_RECEIPT_FAILED if failed_receipts else REASON_RECEIPT_MISSING)

    if reasons:
        return NodeEvaluation(verdict="partial", reasons=tuple(reasons), **base)
    return NodeEvaluation(verdict="satisfied", reasons=(REASON_OK,), **base)


def _overall(
    nodes: Sequence[NodeEvaluation],
) -> Literal["satisfied", "partial", "failed", "blocked"]:
    verdicts = {item.verdict for item in nodes}
    if "failed" in verdicts:
        return "failed"
    if "blocked" in verdicts:
        return "blocked"
    if verdicts - {"satisfied", "skipped"}:
        return "partial"
    return "satisfied"


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evaluation_digest(
    outcomes: Sequence[TaskOutcome], receipts: Sequence[Receipt], policy: EvaluationPolicy
) -> str:
    """证据摘要 = 事实 + 回执 + 策略的规范化哈希（同输入必同 digest）。"""

    payload = {
        "outcomes": [
            {
                "task_id": item.task_id,
                "kind": item.kind,
                "status": item.status,
                "fact": dict(item.fact) if item.fact is not None else None,
                "fact_version": item.fact_version,
                "required": item.required,
                "weight": item.weight,
            }
            for item in sorted(outcomes, key=lambda node: node.task_id)
        ],
        "receipts": [
            {
                "task_id": item.task_id,
                "tool": item.tool,
                "status": item.status,
                "applied_version": item.applied_version,
                "external_id": item.external_id,
            }
            for item in sorted(
                receipts, key=lambda r: (r.task_id, r.tool, r.external_id, r.status)
            )
        ],
        "policy": policy.digest_payload(),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def evaluate_outcomes(
    outcomes: Sequence[TaskOutcome],
    receipts: Sequence[Receipt] = (),
    policy: EvaluationPolicy | None = None,
    *,
    revision: int = 1,
) -> EvaluationReport:
    """按事实 + 回执 + 策略判定（纯函数，无 I/O，无自述采信）。"""

    if not outcomes:
        raise EvaluationError("评估输入为空：没有任何 Task 事实", code=REASON_EMPTY)
    active_policy = policy or EvaluationPolicy()
    grouped = _receipts_by_task(receipts)
    nodes = tuple(
        _evaluate_node(outcome, grouped.get(outcome.task_id, ()), active_policy)
        for outcome in sorted(outcomes, key=lambda node: node.task_id)
    )
    return EvaluationReport(
        revision=revision,
        verdict=_overall(nodes),
        nodes=nodes,
        evidence_digest=evaluation_digest(outcomes, receipts, active_policy),
        evaluated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# schema（无编号迁移：本任务只在自有 schema 内建表）
# ---------------------------------------------------------------------------
_DDL: Final[tuple[str, ...]] = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_evaluations (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        revision integer NOT NULL,
        input_digest text NOT NULL,
        verdict text NOT NULL CHECK (verdict IN ('satisfied', 'partial', 'failed', 'blocked')),
        report jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workspace_id, run_id, revision, input_digest)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_tool_receipts (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        revision integer NOT NULL,
        task_id text NOT NULL,
        tool text NOT NULL,
        status text NOT NULL CHECK (status IN ('succeeded', 'failed')),
        applied_version bigint,
        external_id text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workspace_id, run_id, revision, task_id, tool, external_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_task_inputs (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        revision integer NOT NULL,
        task_id text NOT NULL,
        input_digest text NOT NULL,
        profile text NOT NULL,
        policy text NOT NULL,
        side_effect_confirmed boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workspace_id, run_id, revision, task_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_runs (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        active_revision integer NOT NULL,
        replan_count integer NOT NULL DEFAULT 0 CHECK (replan_count BETWEEN 0 AND {MAX_REPLANS}),
        terminal_status text NOT NULL DEFAULT 'running'
            CHECK (terminal_status IN ('running', 'succeeded', 'failed', 'blocked')),
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workspace_id, run_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_replans (
        workspace_id uuid NOT NULL,
        run_id uuid NOT NULL,
        request_digest text NOT NULL,
        expected_revision integer NOT NULL,
        decision text NOT NULL
            CHECK (decision IN ('applied', 'noop', 'needs_review', 'limit_exceeded',
                                'budget_exhausted')),
        replan_index integer NOT NULL,
        from_revision integer NOT NULL,
        to_revision integer,
        affected jsonb NOT NULL,
        reused jsonb NOT NULL,
        replacements jsonb NOT NULL,
        skipped jsonb NOT NULL DEFAULT '[]'::jsonb,
        reason text NOT NULL DEFAULT '',
        result jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workspace_id, run_id, request_digest)
    )
    """,
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS phase0_replans_applied_uq
        ON {SCHEMA}.phase0_replans (workspace_id, run_id, replan_index)
        WHERE decision = 'applied'
    """,
)


def _ensure_clean(conn: psycopg.Connection[Any]) -> None:
    """T23 教训：调用方遗留只读事务时 ``transaction()`` 会退化成 SAVEPOINT。"""

    if conn.info.transaction_status != TransactionStatus.IDLE:
        conn.commit()


def ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """幂等建 schema/表/索引（本任务不新增编号迁移）。"""

    _ensure_clean(conn)
    try:
        with conn.transaction():
            for statement in _DDL:
                conn.execute(statement)
    finally:
        _ensure_clean(conn)


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    """仅供测试清理：丢掉 p0_t24 全部对象。"""

    _ensure_clean(conn)
    try:
        with conn.transaction():
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    finally:
        _ensure_clean(conn)


# ---------------------------------------------------------------------------
# 读写：事实 / 回执 / 指纹 / 评估
# ---------------------------------------------------------------------------
_TASK_COLUMNS: Final = (
    "task_id, kind, executor_kind, status, epoch, attempt, weight, required, "
    "lease_owner, lease_until, job_id, fact, fact_version, graph_state_ref, "
    "state_version, last_error"
)


def load_outcomes(
    conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int
) -> tuple[TaskOutcome, ...]:
    """只读加载 T23 的 Task 事实快照（不写、不领、不改状态）。"""

    rows = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM {sch.SCHEMA}.phase0_tasks "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s ORDER BY task_id",
        (workspace_id, run_id, revision),
    ).fetchall()
    return tuple(
        TaskOutcome(
            task_id=str(row["task_id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            fact=row["fact"],
            fact_version=int(row["fact_version"]),
            required=bool(row["required"]),
            weight=int(row["weight"]),
            last_error=row["last_error"],
        )
        for row in rows
    )


def record_receipt(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    receipt: Receipt,
) -> bool:
    """落一条工具回执；重复 ``external_id`` 幂等返回 ``False``。"""

    _ensure_clean(conn)
    with conn.transaction():
        cursor = conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_tool_receipts "
            "(workspace_id, run_id, revision, task_id, tool, status, applied_version, external_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (
                workspace_id,
                run_id,
                revision,
                receipt.task_id,
                receipt.tool,
                receipt.status,
                receipt.applied_version,
                receipt.external_id,
            ),
        )
        return cursor.rowcount == 1


def load_receipts(
    conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int
) -> tuple[Receipt, ...]:
    rows = conn.execute(
        f"SELECT task_id, tool, status, applied_version, external_id "
        f"FROM {SCHEMA}.phase0_tool_receipts "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s "
        "ORDER BY task_id, tool, external_id",
        (workspace_id, run_id, revision),
    ).fetchall()
    return tuple(
        Receipt(
            task_id=str(row["task_id"]),
            tool=str(row["tool"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            applied_version=int(row["applied_version"])
            if row["applied_version"] is not None
            else None,
            external_id=str(row["external_id"]),
        )
        for row in rows
    )


def record_inputs(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    task_id: str,
    fingerprint: Fingerprint,
    *,
    side_effect_confirmed: bool = False,
) -> bool:
    """登记节点输入指纹（同 revision 同节点不可改：重复且不同内容抛 IntegrityError）。"""

    _ensure_clean(conn)
    with conn.transaction():
        cursor = conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_task_inputs "
            "(workspace_id, run_id, revision, task_id, input_digest, profile, policy, "
            "side_effect_confirmed) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (workspace_id, run_id, revision, task_id) DO UPDATE "
            "SET input_digest = EXCLUDED.input_digest, profile = EXCLUDED.profile, "
            "policy = EXCLUDED.policy, side_effect_confirmed = EXCLUDED.side_effect_confirmed "
            "WHERE phase0_task_inputs.input_digest <> EXCLUDED.input_digest "
            "OR phase0_task_inputs.profile <> EXCLUDED.profile "
            "OR phase0_task_inputs.policy <> EXCLUDED.policy "
            "OR phase0_task_inputs.side_effect_confirmed <> EXCLUDED.side_effect_confirmed",
            (
                workspace_id,
                run_id,
                revision,
                task_id,
                fingerprint.input_digest,
                fingerprint.profile,
                fingerprint.policy,
                side_effect_confirmed,
            ),
        )
        return cursor.rowcount == 1


def load_inputs(
    conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int
) -> dict[str, TaskInputs]:
    rows = conn.execute(
        f"SELECT task_id, input_digest, profile, policy, side_effect_confirmed "
        f"FROM {SCHEMA}.phase0_task_inputs "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s ORDER BY task_id",
        (workspace_id, run_id, revision),
    ).fetchall()
    return {
        str(row["task_id"]): TaskInputs(
            fingerprint=Fingerprint(
                input_digest=str(row["input_digest"]),
                profile=str(row["profile"]),
                policy=str(row["policy"]),
            ),
            side_effect_confirmed=bool(row["side_effect_confirmed"]),
        )
        for row in rows
    }


def persist_evaluation(conn: psycopg.Connection[Any], report: EvaluationReport) -> bool:
    """按 ``evidence_digest`` 幂等落库；已存在返回 ``False``（不重算、不覆盖）。"""

    if report.workspace_id is None or report.run_id is None:
        raise EvaluationError("persist_evaluation 需要 workspace_id/run_id")
    _ensure_clean(conn)
    with conn.transaction():
        cursor = conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_evaluations "
            "(workspace_id, run_id, revision, input_digest, verdict, report) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                report.workspace_id,
                report.run_id,
                report.revision,
                report.evidence_digest,
                report.verdict,
                json.dumps(report.to_dict(), ensure_ascii=False),
            ),
        )
        return cursor.rowcount == 1


def _load_persisted_evaluation(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    digest: str,
) -> EvaluationReport | None:
    row = conn.execute(
        f"SELECT report FROM {SCHEMA}.phase0_evaluations "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s AND input_digest = %s",
        (workspace_id, run_id, revision, digest),
    ).fetchone()
    if row is None:
        return None
    return EvaluationReport.from_dict(row["report"])


def evaluate_run(
    conn: psycopg.Connection[Any],
    workspace_id: UUID,
    run_id: UUID,
    revision: int,
    policy: EvaluationPolicy | None = None,
) -> tuple[EvaluationReport, bool]:
    """从 T23 事实 + 本模块回执取值评估并幂等落库，返回 ``(report, created)``。"""

    outcomes = load_outcomes(conn, workspace_id, run_id, revision)
    receipts = load_receipts(conn, workspace_id, run_id, revision)
    report = evaluate_outcomes(outcomes, receipts, policy, revision=revision)
    report = EvaluationReport(
        revision=report.revision,
        verdict=report.verdict,
        nodes=report.nodes,
        evidence_digest=report.evidence_digest,
        workspace_id=workspace_id,
        run_id=run_id,
        evaluated_at=report.evaluated_at,
    )
    created = persist_evaluation(conn, report)
    if not created:
        stored = _load_persisted_evaluation(
            conn, workspace_id, run_id, revision, report.evidence_digest
        )
        if stored is not None:
            return stored, False
    return report, created


# ---------------------------------------------------------------------------
# 图快照与重规划计划（纯函数部分）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """从 T23 只读重建的任务图 + optional 边 + 拓扑序。"""

    graph: dag.TaskGraph
    optional_edges: frozenset[tuple[str, str]]
    topo_order: tuple[str, ...]


def load_graph_snapshot(
    conn: psycopg.Connection[Any], workspace_id: UUID, run_id: UUID, revision: int
) -> GraphSnapshot:
    row = sch.graph_row(conn, workspace_id, run_id, revision)
    if row is None:
        raise ReplanError(
            f"revision {revision} 未在 {sch.SCHEMA}.phase0_task_graphs 登记",
            code=CODE_INVALID_REQUEST,
        )
    tasks = conn.execute(
        f"SELECT task_id, kind, weight, required FROM {sch.SCHEMA}.phase0_tasks "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s",
        (workspace_id, run_id, revision),
    ).fetchall()
    edges = conn.execute(
        f"SELECT from_task, to_task, optional FROM {sch.SCHEMA}.phase0_task_edges "
        "WHERE workspace_id = %s AND run_id = %s AND revision = %s",
        (workspace_id, run_id, revision),
    ).fetchall()
    dependencies: dict[str, list[str]] = {str(item["task_id"]): [] for item in tasks}
    optional: set[tuple[str, str]] = set()
    for edge in edges:
        source, target = str(edge["from_task"]), str(edge["to_task"])
        dependencies.setdefault(target, []).append(source)
        if bool(edge["optional"]):
            optional.add((source, target))
    topo = tuple(str(item) for item in row["topo_order"])
    missing = [task_id for task_id in topo if task_id not in dependencies]
    if missing:
        raise ReplanError(
            f"topo_order 含未知节点：{missing}", code=CODE_INVALID_REQUEST
        )
    task_by_id = {str(item["task_id"]): item for item in tasks}
    nodes = tuple(
        dag.TaskNode(
            node_id=task_id,
            depends_on=tuple(dependencies[task_id]),
            kind=str(task_by_id[task_id]["kind"]),
            weight=int(task_by_id[task_id]["weight"]),
            required=bool(task_by_id[task_id]["required"]),
        )
        for task_id in topo
    )
    return GraphSnapshot(
        graph=dag.TaskGraph(nodes=nodes, task_graph_revision=revision, run_id=str(run_id)),
        optional_edges=frozenset(optional),
        topo_order=topo,
    )


@dataclass(frozen=True, slots=True)
class ReplanPlan:
    """新 revision 的图与账目；``graph`` 已通过 T22 校验。"""

    graph: dag.TaskGraph
    replacements: Mapping[str, str]
    reused: tuple[str, ...]
    affected: tuple[str, ...]
    skipped: tuple[str, ...]
    optional_edges: frozenset[tuple[str, str]]
    dropped_dependencies: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.node_id for node in self.graph.nodes],
            "edges": [list(edge) for edge in self.graph.edges],
            "replacements": dict(self.replacements),
            "reused": list(self.reused),
            "affected": list(self.affected),
            "skipped": list(self.skipped),
            "dropped_dependencies": [list(item) for item in self.dropped_dependencies],
        }


def _replacement_id(task_id: str, replan_index: int) -> str:
    candidate = f"{task_id}.r{replan_index}"
    if not dag.NODE_ID_PATTERN.match(candidate):
        raise ReplanError(
            f"替换节点 id 非法：{candidate!r}（需匹配 {dag.NODE_ID_PATTERN.pattern}）",
            code=CODE_GRAPH_INVALID,
        )
    return candidate


def build_replan_graph(
    snapshot: GraphSnapshot,
    report: EvaluationReport,
    inputs: Mapping[str, TaskInputs] | None = None,
    expected_fingerprints: Mapping[str, Fingerprint] | None = None,
    *,
    replan_index: int = 1,
    limits: dag.GraphLimits | None = None,
) -> ReplanPlan:
    """只把未完成/未满足节点放进新 revision；已完成节点只进 ``reused``。

    规则：
    * ``satisfied`` → 复用事实引用，不重复登记（指纹不匹配时按副作用状态分流）；
    * ``failed``/``partial`` → 生成 ``<id>.r<n>`` 替换节点，依赖沿用原节点；
    * ``blocked``/``not_run`` → 延续为 pending，依赖指向替换节点；
    * ``cancelled`` → 永久跳过，其必需下游一并跳过（不静默重跑）。
    """

    verdicts = {item.task_id: item.verdict for item in report.nodes}
    by_id = {node.node_id: node for node in snapshot.graph.nodes}
    supplied_inputs = inputs or {}
    expected = expected_fingerprints or {}

    def reuse_problem(task_id: str) -> str | None:
        if not expected:
            return None
        if task_id not in expected:
            return None
        stored = supplied_inputs.get(task_id)
        if stored is None:
            return REASON_REUSE_INPUTS_UNRECORDED
        if not stored.fingerprint.matches(expected[task_id]):
            return REASON_REUSE_FINGERPRINT_MISMATCH
        return None

    reused: list[str] = []
    affected: list[str] = []
    skipped: list[str] = sorted(
        task_id for task_id, verdict in verdicts.items() if verdict == "skipped"
    )
    needs_review_reason: str | None = None

    for task_id in snapshot.topo_order:
        verdict = verdicts.get(task_id, "not_run")
        node = by_id[task_id]
        if verdict == "satisfied":
            problem = reuse_problem(task_id)
            if problem is None:
                reused.append(task_id)
                continue
            stored = supplied_inputs.get(task_id)
            if stored is not None and stored.side_effect_confirmed:
                needs_review_reason = f"{problem}:{task_id}"
                skipped.append(task_id)
            else:
                affected.append(task_id)
            continue
        if verdict in {"failed", "partial"}:
            affected.append(task_id)

    if needs_review_reason:
        raise ReplanNeedsReview(
            f"已完成节点的输入/策略指纹不匹配且副作用已确认，禁止自动重跑：{needs_review_reason}"
        )

    replacements = {
        task_id: _replacement_id(task_id, replan_index)
        for task_id in sorted(affected, key=snapshot.topo_order.index)
    }
    reused_set = set(reused)
    skipped_set = set(skipped)

    # cancelled/被跳过的必需下游一并跳过（required 边往下传播）。
    changed = True
    while changed:
        changed = False
        for node in snapshot.graph.nodes:
            if node.node_id in skipped_set:
                continue
            for dependency in node.depends_on:
                if dependency in skipped_set and node.required:
                    skipped_set.add(node.node_id)
                    changed = True
                    break
    if skipped_set - set(skipped):
        skipped = sorted(skipped_set)

    def map_dependency(task_id: str, dependency: str) -> str | None:
        if dependency in replacements:
            return replacements[dependency]
        if dependency in reused_set:
            return None
        if dependency in skipped_set:
            return None
        return dependency

    dropped: set[tuple[str, str]] = set()
    new_nodes: list[dag.TaskNode] = []

    # 先按原拓扑序保留未完成节点。
    for task_id in snapshot.topo_order:
        if task_id in replacements or task_id in reused_set or task_id in skipped_set:
            continue
        node = by_id[task_id]
        deps: list[str] = []
        for dependency in node.depends_on:
            if dependency in skipped_set and node.required:
                skipped_set.add(task_id)
                deps = []
                break
            mapped = map_dependency(task_id, dependency)
            if mapped is None:
                dropped.add((dependency, task_id))
                continue
            deps.append(mapped)
        if task_id in skipped_set:
            continue
        new_nodes.append(
            dag.TaskNode(
                node_id=node.node_id,
                depends_on=tuple(deps),
                kind=node.kind,
                weight=node.weight,
                required=node.required,
                title=node.title,
            )
        )

    # 再追加替换节点（依赖同样映射；替换已完成节点时其依赖已满足 → 丢弃）。
    for task_id in affected:
        source = by_id[task_id]
        deps = []
        for dependency in source.depends_on:
            if dependency in skipped_set and source.required:
                raise ReplanNeedsReview(
                    f"替换节点 {task_id} 的必需前置 {dependency} 已取消，无法安全重规划"
                )
            mapped = map_dependency(task_id, dependency)
            if mapped is None:
                dropped.add((dependency, task_id))
                continue
            deps.append(mapped)
        new_nodes.append(
            dag.TaskNode(
                node_id=replacements[task_id],
                depends_on=tuple(deps),
                kind=source.kind,
                weight=source.weight,
                required=source.required,
                title=f"{source.title or source.node_id} (retry r{replan_index})",
            )
        )

    new_ids = {node.node_id for node in new_nodes}
    optional = frozenset(
        (
            replacements.get(source, source),
            replacements.get(target, target),
        )
        for source, target in snapshot.optional_edges
        if (source in new_ids or source in replacements)
        and (target in new_ids or target in replacements)
        and replacements.get(source, source) in new_ids
        and replacements.get(target, target) in new_ids
    )
    graph = dag.TaskGraph(
        nodes=tuple(new_nodes),
        task_graph_revision=snapshot.graph.task_graph_revision + 1,
        run_id=snapshot.graph.run_id,
    )

    validation = dag.validate_graph(graph, limits or dag.DEFAULT_LIMITS)
    try:
        validation.raise_if_invalid()
    except dag.ContractViolation as exc:
        raise ReplanError(f"重规划图未通过 T22 校验：{exc}", code=CODE_GRAPH_INVALID) from exc
    # optional 边必须是真实存在的边，否则 T23 会拒绝登记。
    unknown_optional = {edge for edge in optional if edge not in set(graph.edges)}
    if unknown_optional:
        raise ReplanError(
            f"optional 边不在新图中：{sorted(unknown_optional)}", code=CODE_GRAPH_INVALID
        )
    return ReplanPlan(
        graph=graph,
        replacements=replacements,
        reused=tuple(reused),
        affected=tuple(sorted(affected, key=snapshot.topo_order.index)),
        skipped=tuple(skipped),
        optional_edges=optional,
        dropped_dependencies=tuple(sorted(dropped)),
    )


# ---------------------------------------------------------------------------
# 预算端口（只读剩余额度；禁止 release）
# ---------------------------------------------------------------------------
class BudgetPort(Protocol):
    """预算端口：只暴露只读剩余额度；未知用量预留不在此释放。"""

    def remaining_microunits(self, workspace_id: UUID, run_id: UUID) -> int: ...


@dataclass
class StaticBudget:
    """测试/演练用静态预算；``release`` 一旦被调用即失败（证明重规划不释放预留）。"""

    remaining: int
    release_calls: int = 0

    def remaining_microunits(self, workspace_id: UUID, run_id: UUID) -> int:
        _ = workspace_id, run_id
        return self.remaining

    def release(self, reservation_id: UUID) -> None:
        self.release_calls += 1
        raise AssertionError(
            f"重规划不得释放未知用量的预留（reservation={reservation_id}）"
        )


# ---------------------------------------------------------------------------
# 请求/结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReplanRequest:
    """一次重规划请求；``expected_revision`` 是 CAS 前置条件。"""

    workspace_id: UUID
    run_id: UUID
    expected_revision: int
    required_budget_microunits: int = 0
    expected_fingerprints: Mapping[str, Fingerprint] = field(default_factory=dict)
    fixed_job_kinds: tuple[str, ...] = ("fetch", "ingest")
    limits: dag.GraphLimits | None = None


@dataclass(frozen=True, slots=True)
class ReplanResult:
    """一次重规划的决定与账目；``created`` 为 False 表示幂等重入。"""

    decision: Literal["applied", "noop", "needs_review", "limit_exceeded", "budget_exhausted"]
    replan_index: int
    from_revision: int
    to_revision: int | None
    affected: tuple[str, ...]
    reused: tuple[str, ...]
    replacements: Mapping[str, str]
    skipped: tuple[str, ...]
    terminal_status: Literal["running", "succeeded", "failed", "blocked"]
    request_digest: str
    created: bool = True
    reason: str = ""
    budget_remaining_microunits: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "replan_index": self.replan_index,
            "from_revision": self.from_revision,
            "to_revision": self.to_revision,
            "affected": list(self.affected),
            "reused": list(self.reused),
            "replacements": dict(self.replacements),
            "skipped": list(self.skipped),
            "terminal_status": self.terminal_status,
            "request_digest": self.request_digest,
            "reason": self.reason,
            "budget_remaining_microunits": self.budget_remaining_microunits,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, created: bool) -> ReplanResult:
        return cls(
            decision=str(payload["decision"]),  # type: ignore[arg-type]
            replan_index=int(payload["replan_index"]),
            from_revision=int(payload["from_revision"]),
            to_revision=int(payload["to_revision"]) if payload["to_revision"] is not None else None,
            affected=tuple(str(item) for item in payload.get("affected", ())),
            reused=tuple(str(item) for item in payload.get("reused", ())),
            replacements={str(k): str(v) for k, v in dict(payload.get("replacements", {})).items()},
            skipped=tuple(str(item) for item in payload.get("skipped", ())),
            terminal_status=str(payload.get("terminal_status", "running")),  # type: ignore[arg-type]
            request_digest=str(payload["request_digest"]),
            created=created,
            reason=str(payload.get("reason", "")),
            budget_remaining_microunits=payload.get("budget_remaining_microunits"),
        )


def replan_request_digest(
    request: ReplanRequest, evidence_digest: str, affected: Sequence[str]
) -> str:
    """请求摘要：workspace/run/期望 revision/评估证据/受影响节点/指纹/预算。"""

    payload = {
        "workspace_id": str(request.workspace_id),
        "run_id": str(request.run_id),
        "expected_revision": request.expected_revision,
        "evidence_digest": evidence_digest,
        "affected": sorted(affected),
        "required_budget_microunits": request.required_budget_microunits,
        "expected_fingerprints": {
            key: value.as_dict() for key, value in sorted(request.expected_fingerprints.items())
        },
        "fixed_job_kinds": list(request.fixed_job_kinds),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 重规划器
# ---------------------------------------------------------------------------
class Replanner:
    """有界重规划器：评估 → CAS → 只登记未完成部分 → 次数/预算停止。"""

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        policy: EvaluationPolicy | None = None,
        *,
        budget: BudgetPort | None = None,
        max_replans: int = MAX_REPLANS,
    ) -> None:
        if max_replans != MAX_REPLANS:
            raise ReplanError(
                f"max_replans 固定为 {MAX_REPLANS}（Backlog 硬约束），实际 {max_replans}",
                code=CODE_INVALID_REQUEST,
            )
        self._conn = conn
        self._policy = policy or EvaluationPolicy()
        self._budget = budget

    # -- 只读镜像 ---------------------------------------------------------
    def current_revision(self, workspace_id: UUID, run_id: UUID) -> int:
        row = self._conn.execute(
            f"SELECT max(revision) AS revision FROM {sch.SCHEMA}.phase0_task_graphs "
            "WHERE workspace_id = %s AND run_id = %s",
            (workspace_id, run_id),
        ).fetchone()
        revision = row["revision"] if row else None
        if revision is None:
            raise ReplanError("run 尚未登记任何 TaskGraph", code=CODE_INVALID_REQUEST)
        return int(revision)

    def replan_count(self, workspace_id: UUID, run_id: UUID) -> int:
        row = self._conn.execute(
            f"SELECT count(*) AS applied FROM {SCHEMA}.phase0_replans "
            "WHERE workspace_id = %s AND run_id = %s AND decision = 'applied'",
            (workspace_id, run_id),
        ).fetchone()
        return int(row["applied"])

    def run_status(self, workspace_id: UUID, run_id: UUID) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT active_revision, replan_count, terminal_status FROM {SCHEMA}.phase0_runs "
            "WHERE workspace_id = %s AND run_id = %s",
            (workspace_id, run_id),
        ).fetchone()
        return dict(row) if row else None

    def evaluate(self, workspace_id: UUID, run_id: UUID, revision: int):
        return evaluate_run(self._conn, workspace_id, run_id, revision, self._policy)

    # -- 主流程 -----------------------------------------------------------
    @staticmethod
    def _lock_key(workspace_id: UUID, run_id: UUID) -> int:
        raw = hashlib.blake2b(f"{workspace_id}:{run_id}".encode(), digest_size=8).digest()
        return int.from_bytes(raw, "big", signed=True)

    def replan(
        self,
        request: ReplanRequest,
        *,
        report: EvaluationReport | None = None,
    ) -> ReplanResult:
        """对同一 ``(workspace, run)`` 串行化重规划判定。

        同一 run 的并发请求（双 Replanner、客户端重试）用 PostgreSQL 会话级
        advisory lock 串行化：先到者建新 revision 并记账，后到者读到同一
        ``request_digest`` 的已应用记录，返回 ``created=False`` 的同一结果。
        因此「重复请求幂等」和「revision 只有一个活动指针」可以同时成立，
        调用方也不会看到 ``UniqueViolation``。
        """
        _ensure_clean(self._conn)
        key = self._lock_key(request.workspace_id, request.run_id)
        self._conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        self._conn.commit()
        try:
            return self._replan_locked(request, report=report)
        finally:
            try:
                _ensure_clean(self._conn)
                self._conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
                self._conn.commit()
            except psycopg.Error:
                # 连接已不可用：会话级 advisory lock 随连接关闭由服务端释放。
                pass

    def _replan_locked(
        self,
        request: ReplanRequest,
        *,
        report: EvaluationReport | None = None,
    ) -> ReplanResult:
        _ensure_clean(self._conn)
        workspace_id, run_id = request.workspace_id, request.run_id
        if request.expected_revision < 1:
            raise ReplanError("expected_revision 必须 >= 1", code=CODE_INVALID_REQUEST)

        if self.run_status(workspace_id, run_id) is None:
            self._ensure_run(workspace_id, run_id)

        if report is None:
            report = evaluate_run(
                self._conn, workspace_id, run_id, request.expected_revision, self._policy
            )[0]
        affected_ids = tuple(
            sorted(item.task_id for item in report.nodes if item.verdict in {"failed", "partial"})
        )
        digest = replan_request_digest(request, report.evidence_digest, affected_ids)

        existing = self._load_replan(workspace_id, run_id, digest)
        if existing is not None:
            return ReplanResult.from_dict(existing, created=False)

        applied = self.replan_count(workspace_id, run_id)
        current = self.current_revision(workspace_id, run_id)
        if request.expected_revision != current:
            raise ReplanConflictError(
                f"expected_revision={request.expected_revision} 与活动 revision={current} 不一致"
            )
        next_index = applied + 1

        if self._budget is not None and request.required_budget_microunits > 0:
            remaining = self._budget.remaining_microunits(workspace_id, run_id)
            if remaining < request.required_budget_microunits:
                result = ReplanResult(
                    decision="budget_exhausted",
                    replan_index=next_index,
                    from_revision=request.expected_revision,
                    to_revision=None,
                    affected=affected_ids,
                    reused=(),
                    replacements={},
                    skipped=(),
                    terminal_status="blocked",
                    request_digest=digest,
                    reason=REASON_BUDGET,
                    budget_remaining_microunits=remaining,
                )
                return self._record(result, workspace_id, run_id)

        if not affected_ids:
            # 没有「未满足」的节点：要么全绿，要么剩余节点只是还没跑完。
            result = ReplanResult(
                decision="noop",
                replan_index=applied,
                from_revision=request.expected_revision,
                to_revision=None,
                affected=(),
                reused=tuple(
                    item.task_id for item in report.nodes if item.verdict == "satisfied"
                ),
                replacements={},
                skipped=(),
                terminal_status="succeeded" if report.verdict == "satisfied" else "running",
                request_digest=digest,
                reason=REASON_NOTHING_TO_REPLAN,
            )
            return self._record(result, workspace_id, run_id)

        if applied >= MAX_REPLANS:
            result = ReplanResult(
                decision="limit_exceeded",
                replan_index=next_index,
                from_revision=request.expected_revision,
                to_revision=None,
                affected=affected_ids,
                reused=(),
                replacements={},
                skipped=(),
                terminal_status="failed",
                request_digest=digest,
                reason=REASON_REPLAN_LIMIT,
            )
            return self._record(result, workspace_id, run_id)

        snapshot = load_graph_snapshot(self._conn, workspace_id, run_id, request.expected_revision)
        inputs = load_inputs(self._conn, workspace_id, run_id, request.expected_revision)
        try:
            plan = build_replan_graph(
                snapshot,
                report,
                inputs,
                request.expected_fingerprints,
                replan_index=next_index,
                limits=request.limits,
            )
        except ReplanNeedsReview as exc:
            result = ReplanResult(
                decision="needs_review",
                replan_index=0,
                from_revision=request.expected_revision,
                to_revision=None,
                affected=affected_ids,
                reused=(),
                replacements={},
                skipped=(),
                terminal_status="blocked",
                request_digest=digest,
                reason=f"{exc.code}:{exc}",
            )
            return self._record(result, workspace_id, run_id)

        if plan.graph.node_ids == snapshot.graph.node_ids and not plan.replacements:
            result = ReplanResult(
                decision="noop",
                replan_index=applied,
                from_revision=request.expected_revision,
                to_revision=None,
                affected=(),
                reused=plan.reused,
                replacements={},
                skipped=plan.skipped,
                terminal_status="succeeded" if report.verdict == "satisfied" else "blocked",
                request_digest=digest,
                reason=REASON_NOTHING_TO_REPLAN,
            )
            return self._record(result, workspace_id, run_id)

        try:
            sch.register_graph(
                self._conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=request.expected_revision + 1,
                graph=plan.graph,
                optional_dependencies=set(plan.optional_edges),
                fixed_job_kinds=request.fixed_job_kinds,
                limits=request.limits,
            )
        except sch.RevisionConflict as exc:
            raise ReplanConflictError(
                f"revision {request.expected_revision + 1} 已存在且内容不同：{exc}"
            ) from exc

        result = ReplanResult(
            decision="applied",
            replan_index=next_index,
            from_revision=request.expected_revision,
            to_revision=request.expected_revision + 1,
            affected=plan.affected,
            reused=plan.reused,
            replacements=plan.replacements,
            skipped=plan.skipped,
            terminal_status="running",
            request_digest=digest,
        )
        return self._record(result, workspace_id, run_id)

    # -- 持久化 -----------------------------------------------------------
    def _ensure_run(self, workspace_id: UUID, run_id: UUID) -> None:
        current = self.current_revision(workspace_id, run_id)
        _ensure_clean(self._conn)
        with self._conn.transaction():
            self._conn.execute(
                f"INSERT INTO {SCHEMA}.phase0_runs "
                "(workspace_id, run_id, active_revision, replan_count, terminal_status) "
                "VALUES (%s, %s, %s, 0, 'running') ON CONFLICT DO NOTHING",
                (workspace_id, run_id, current),
            )

    def _load_replan(
        self, workspace_id: UUID, run_id: UUID, digest: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT result FROM {SCHEMA}.phase0_replans "
            "WHERE workspace_id = %s AND run_id = %s AND request_digest = %s",
            (workspace_id, run_id, digest),
        ).fetchone()
        return dict(row["result"]) if row else None

    def _record(self, result: ReplanResult, workspace_id: UUID, run_id: UUID) -> ReplanResult:
        """落账 + 更新活动指针；并发同摘要时返回已存在记录（幂等）。"""

        _ensure_clean(self._conn)
        applied_increment = 1 if result.decision == "applied" else 0
        try:
            with self._conn.transaction():
                cursor = self._conn.execute(
                    f"INSERT INTO {SCHEMA}.phase0_replans "
                    "(workspace_id, run_id, request_digest, expected_revision, decision, "
                    "replan_index, from_revision, to_revision, affected, reused, replacements, "
                    "skipped, reason, result) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        workspace_id,
                        run_id,
                        result.request_digest,
                        result.from_revision,
                        result.decision,
                        result.replan_index,
                        result.from_revision,
                        result.to_revision,
                        json.dumps(list(result.affected), ensure_ascii=False),
                        json.dumps(list(result.reused), ensure_ascii=False),
                        json.dumps(dict(result.replacements), ensure_ascii=False),
                        json.dumps(list(result.skipped), ensure_ascii=False),
                        result.reason,
                        json.dumps(result.to_dict(), ensure_ascii=False),
                    ),
                )
                created = cursor.rowcount == 1
                if created:
                    self._conn.execute(
                        f"INSERT INTO {SCHEMA}.phase0_runs "
                        "(workspace_id, run_id, active_revision, replan_count, terminal_status, "
                        "updated_at) VALUES (%s, %s, %s, %s, %s, now()) "
                        "ON CONFLICT (workspace_id, run_id) DO UPDATE SET "
                        "active_revision = GREATEST("
                        "phase0_runs.active_revision, EXCLUDED.active_revision), "
                        "replan_count = phase0_runs.replan_count + %s, "
                        "terminal_status = CASE WHEN EXCLUDED.terminal_status = 'running' "
                        "THEN phase0_runs.terminal_status ELSE EXCLUDED.terminal_status END, "
                        "updated_at = now()",
                        (
                            workspace_id,
                            run_id,
                            result.to_revision or result.from_revision,
                            applied_increment,
                            result.terminal_status,
                            applied_increment,
                        ),
                    )
        except pg_errors.UniqueViolation:
            _ensure_clean(self._conn)
            existing = self._load_replan(workspace_id, run_id, result.request_digest)
            if existing is None:  # pragma: no cover - 唯一冲突必然有行
                raise
            return ReplanResult.from_dict(existing, created=False)
        if not created:
            _ensure_clean(self._conn)
            existing = self._load_replan(workspace_id, run_id, result.request_digest)
            if existing is not None:
                return ReplanResult.from_dict(existing, created=False)
        return result
