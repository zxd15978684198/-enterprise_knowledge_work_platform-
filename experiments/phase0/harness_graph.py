"""P0-T21 Harness 节点集成、取消、超时与安全 trace（**全部复用** T05/T06/T08/T19/T20）。

本模块不实现任何新引擎，只做「把已有组件接成一张最小可运行图」：

====================  ==================================================  ==========
能力                  委托给                                              复用方式
====================  ==================================================  ==========
checkpoint / 单写者    ``checkpoint_probe``（T05）                         直接调用 Harness/
                      ``Harness`` / ``RunBinding`` / ``Invocation``        Invocation 的
                                                                          acquire_lock /
                                                                          claim_owner /
                                                                          write / restore_read
审批 / 消费 / 账本     ``approval_probe`` + ``tool_ledger``（T06）          ensure_approval /
                                                                          approve /
                                                                          require_approval /
                                                                          execute；receipt 由
                                                                          T06 的 append-only 表落
Job 唤醒               ``job_probe``（T08）                                enqueue /
                                                                          record_result
模型调用               ``model_gateway``（T19）                            ModelGateway.call
                                                                          （**max_retries=0**）
预算预留/结算          ``budget_ledger``（T20）                            reserve_model_call /
                                                                          settle / release
trace 脱敏             ``safe_trace``（本任务）                             allowlist + canary
====================  ==================================================  ==========

**必须显式解决的契约冲突（T20 指出）**
--------------------------------------
``ModelGateway.call`` 内部自带重试（``config.max_retries + 1`` 次），而 T20 的
``purpose_key`` 一条预留对应一次 ``reserve``。两者叠加会出现「3 次真实 provider 调用
只落 1 条预留」——成本无法逐 attempt 归集。

本模块选**方案 ①**：**关掉 gateway 内部重试**（``ProviderConfig(max_retries=0)``），
由 Harness 自己按 attempt 循环，每次 attempt **先** ``reserve_model_call``，其中
``purpose_key = f"{model_step_key}:attempt:{n}"``，失败可重试则 ``release``，
成功则 ``settle``。代价是：退避策略、错误分类、``Retry-After`` 解析都要由 Harness
（而不是 gateway）承担——本模块实现有界退避且不读 ``Retry-After``，这是明确的取舍。
证据见 ``docs/poc/evidence/harness-integration.md`` 与
``tests/phase0/test_harness_integration.py::test_budget_reserves_each_model_attempt``。

**原生能力边界（不自研恢复引擎）**
----------------------------------
``checkpoint`` 只有一条写路径：``Invocation.write``（T05）。本模块不存自己的
node→state 快照，也不实现「从 checkpoint 反序列化 Graph」——恢复时只有两种依据：
① T05 的 checkpoint（存在就用）；② 已提交的业务事实（T06 receipt / approval 状态、
T08 job result）。第二种就是「checkpoint 丢失后重入」，它读的是**事实表**，不回退。

LangGraph 原生 ``interrupt``/``Command(resume=…)`` 在当前实验环境**不可用**
（``langgraph`` 未安装；``--with`` 会改变 ``sys.prefix`` 使环境隔离断言失败，见
``CLAUDE.md`` 已知阻塞）。因此审批等待用**持久化等待事实**表达：``wait_facts`` 表 +
run 状态 ``waiting_approval``，恢复时重入同一节点。这与 TDD04 §7「approval_wait 只等待」
语义一致，但它**不是**原生 interrupt 的等价证明——见证据文件的 PARTIAL 记录。

**单活前提**：T05 的结论是 PARTIAL / 单活 runner。本任务所有 checkpoint 断言都在
同一时刻只有一个 writer 的条件下验证（``Invocation`` 持有 advisory lock + owner epoch）。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Final, TypedDict
from uuid import UUID, uuid4

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.types.json import Jsonb

import approval_probe
import budget_ledger
import checkpoint_probe
import job_probe
import safe_trace
import scope as scope_module
import tool_ledger
from contracts import Principal, WorkspaceScope
from model_gateway import (
    ChatMessage,
    GatewayError,
    ModelCall,
    ModelGateway,
    ProviderConfig,
    TransportNetworkError,
    TransportRequest,
    TransportResponse,
)

__all__ = [
    "GRAPH_VERSION",
    "NODES",
    "SCHEMA",
    "GraphRun",
    "GraphState",
    "HarnessConfig",
    "NATIVE_CHECKPOINT_TABLES",
    "NATIVE_NODES",
    "NativeGraphRun",
    "NativeState",
    "NodeOutcome",
    "RunCancelled",
    "RunTimedOut",
    "StepBudget",
    "apply_schema",
    "ensure_native_checkpoint_schema",
    "ensure_workspace_principals",
    "connect",
    "reset_schema",
]

SCHEMA: Final = "p0_t21"
GRAPH_VERSION: Final = "harness.v1"
STATE_SCHEMA_VERSION: Final = 1
#: 节点顺序固定：只读并行 → 写前屏障 → 审批等待 → 执行 → Job 唤醒 → 收敛。
NODES: Final[tuple[str, ...]] = (
    "plan",
    "read_fanout",
    "write_approval",
    "write_execute",
    "job_wake",
    "finalize",
)
WAITING_APPROVAL: Final = "waiting_approval"
COMPLETED: Final = "completed"
CANCELLED: Final = "cancelled"
TIMED_OUT: Final = "timeout"
FAILED: Final = "failed"


class HarnessError(RuntimeError):
    code = "HARNESS_ERROR"


class RunCancelled(HarnessError):
    """取消是**事实**（DB 里 cancel_requested_at 非空），不是异常控制流的花招。"""

    code = "RUN_CANCELLED"


class RunTimedOut(HarnessError):
    """超时只能进 timeout 终态，绝不允许被写成 completed。"""

    code = "RUN_TIMEOUT"


# ---------------------------------------------------------------------------
# schema（本任务独占 p0_t21；不新建编号迁移，与 T05 的做法一致）
# ---------------------------------------------------------------------------
SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_graph_runs (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    graph_version text NOT NULL,
    state_schema_version integer NOT NULL,
    status text NOT NULL DEFAULT 'idle'
        CHECK (status IN ('idle', 'running', 'waiting_approval', 'waiting_job',
                          'completed', 'cancelled', 'timeout', 'failed')),
    cancel_requested_at timestamptz,
    cancel_reason_code text CHECK (cancel_reason_code IS NULL
                                   OR cancel_reason_code ~ '^[A-Z][A-Z0-9_]{{2,47}}$'),
    deadline_at timestamptz,
    completed_nodes text[] NOT NULL DEFAULT '{{}}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id),
    CONSTRAINT phase0_graph_runs_thread_key UNIQUE (workspace_id, thread_id)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_wait_facts (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    node text NOT NULL,
    fact_key text NOT NULL CHECK (fact_key ~ '^[A-Za-z0-9_.:@-]{{1,128}}$'),
    kind text NOT NULL CHECK (kind IN ('approval', 'job')),
    ref_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, fact_key),
    CONSTRAINT phase0_wait_facts_run_fk FOREIGN KEY (workspace_id, run_id)
        REFERENCES {SCHEMA}.phase0_graph_runs (workspace_id, run_id) ON DELETE CASCADE
)
""",
    f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_node_events (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    seq bigserial,
    node text NOT NULL,
    status text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id, seq),
    CONSTRAINT phase0_node_events_run_fk FOREIGN KEY (workspace_id, run_id)
        REFERENCES {SCHEMA}.phase0_graph_runs (workspace_id, run_id) ON DELETE CASCADE
)
""",
    f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.phase0_reject_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, {SCHEMA}
    AS $fn$
    BEGIN
        RAISE EXCEPTION '节点事件表只允许追加写，拒绝 %', TG_OP
            USING ERRCODE = 'check_violation';
    END
    $fn$
""",
    f"""
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'phase0_node_events_append_only'
    ) THEN
        CREATE TRIGGER phase0_node_events_append_only
            BEFORE UPDATE OR DELETE ON {SCHEMA}.phase0_node_events
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.phase0_reject_event_mutation();
    END IF;
END
$do$
""",
)


def connect(*, dsn: str | None = None, autocommit: bool = True) -> psycopg.Connection[Any]:
    """运行期一律 autocommit（T06 的教训：隐式事务会把网络调用包进去）。"""

    return psycopg.connect(dsn or scope_module.read_dsn(), autocommit=autocommit)


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def apply_schema(conn: psycopg.Connection[Any]) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)


# ---------------------------------------------------------------------------
# 配置与值对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """显式配置：一次模型步骤最多几个 attempt、单步预算上限、模型价格。"""

    model_step_key: str = "plan-step-1"
    max_model_attempts: int = 3
    prompt_tokens: int = 64
    max_output_tokens: int = 32
    max_cost_microunits: int = 5_000
    tool_timeout_seconds: float = 5.0
    #: 模型 prompt 文本。**故意可注入**：canary 测试要靠它证明 prompt 不进 trace。
    prompt_text: str = "plan step"
    #: 写工具参数（同样可注入 canary，证明参数原文不进 trace/Graph State）。
    tool_arguments: Mapping[str, Any] = field(default_factory=lambda: {"path": "/idem/write"})


@dataclass
class GraphState:
    """进 checkpoint 的最小状态：**只放引用与计数**，不放 prompt/参数原文。"""

    completed: list[str] = field(default_factory=list)
    read_results: int = 0
    approval_id: str | None = None
    job_id: str | None = None
    status: str = "idle"

    def as_payload(self) -> dict[str, Any]:
        return {
            "completed": list(self.completed),
            "read_results": self.read_results,
            "approval_id": self.approval_id,
            "job_id": self.job_id,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    node: str
    status: str
    detail_code: str = ""
    waiting_for: str | None = None


@dataclass(frozen=True, slots=True)
class StepBudget:
    """一次模型步骤的预算归集结果（逐 attempt）。"""

    reservations: int
    settlements: int
    releases: int
    purpose_keys: tuple[str, ...]


# ---------------------------------------------------------------------------
# 模型调用：gateway 关重试 + Harness 逐 attempt 预留
# ---------------------------------------------------------------------------
class _ScriptedTransport:
    """``ModelTransport`` 的脚本化实现（T19 自己的 seam，不是 mock 掉被测逻辑）。

    每次 ``send`` 消耗脚本里的下一项：``("error", ErrorClass)`` 或 ``("ok", content)``。
    它记录真实调用次数——这正是"3 次 attempt = 3 条预留"的对照。
    """

    def __init__(self, script: Sequence[tuple[str, Any]]) -> None:
        self.script = list(script)
        self.calls = 0
        self.seen_headers: list[dict[str, str]] = []

    def send(self, request: TransportRequest) -> TransportResponse:
        self.calls += 1
        self.seen_headers.append(dict(request.headers))
        if not self.script:
            raise AssertionError("脚本已耗尽：调用次数超出预期")
        kind, payload = self.script.pop(0)
        if kind == "error":
            raise TransportNetworkError(f"脚本化失败 {payload}")
        if kind == "status":
            return TransportResponse(
                status_code=int(payload), body=b"{}", headers={}, request_ref=f"req-{self.calls}"
            )
        body = json.dumps(
            {
                "id": f"chatcmpl-{self.calls}",
                "object": "chat.completion",
                "model": "scripted",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": str(payload)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
            }
        ).encode("utf-8")
        return TransportResponse(
            status_code=200, body=body, headers={}, request_ref=f"req-{self.calls}"
        )


def _build_gateway(transport: Any) -> ModelGateway:
    """``max_retries=0`` 是契约冲突的解决点：gateway 不再自行重试。"""

    config = ProviderConfig(
        base_url="http://127.0.0.1:9",  # 永不被真正连接（transport 已注入）
        model="scripted-model",
        max_retries=0,
        api_key_env="PHASE0_MODEL_API_KEY",
        provider_name="p0_t21-scripted",
    )
    return ModelGateway(config, transport=transport, api_key=_FakeSecret("sk-canary-key"))


class _FakeSecret:
    """满足 ``model_gateway.Secret`` 的最小接口（不落盘、不 repr 泄露）。"""

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<secret ***>"


# ---------------------------------------------------------------------------
# 模型步骤：逐 attempt 预留（两种图共用同一实现）
# ---------------------------------------------------------------------------
@dataclass
class ModelCallContext:
    """跑一次模型步骤所需的全部依赖（原生图与传统节点驱动共用）。"""

    budget: Any
    gateway: ModelGateway
    transport: _ScriptedTransport
    scope: WorkspaceScope
    workspace_id: UUID
    run_id: UUID
    config: HarnessConfig
    trace: safe_trace.SafeTrace
    price: Any = None


def run_model_step_with_budget(
    ctx: ModelCallContext,
    *,
    node: str,
    guard: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """方案 ①：gateway 关内部重试，这里按 attempt 预留/释放/结算。"""

    purpose_keys: list[str] = []
    reservations = settlements = releases = 0
    attempt = 0
    last_error: Exception | None = None
    while attempt < ctx.config.max_model_attempts:
        attempt += 1
        if guard is not None:
            guard(node, f"model_call_attempt_{attempt}")
        # 关键：purpose_key 含 attempt，一次 provider 调用一条预留（方案 ①）
        purpose_key = f"t21:{ctx.config.model_step_key}:attempt:{attempt}"
        reservation = ctx.budget.reserve_model_call(
            scope=ctx.scope,
            run_id=ctx.run_id,
            purpose_key=purpose_key,
            prompt_tokens=ctx.config.prompt_tokens,
            max_output_tokens=ctx.config.max_output_tokens,
            price=ctx.price,
            max_cost_microunits=ctx.config.max_cost_microunits,
        )
        purpose_keys.append(purpose_key)
        reservations += 1
        ctx.trace.record(node=node, action="model_reserve", status="ok",
                         attempt=attempt, counts={"reserved": 1})
        call = ModelCall(
            workspace_id=ctx.workspace_id,
            messages=(ChatMessage(role="user", content=ctx.config.prompt_text),),
            max_output_tokens=ctx.config.max_output_tokens,
            model_step_key=ctx.config.model_step_key,
        )
        try:
            result = ctx.gateway.call(ctx.scope, call)
        except GatewayError as exc:
            last_error = exc
            ctx.budget.release(reservation_id=reservation.reservation_id,
                               reason="RETRYABLE_FAILURE")
            releases += 1
            ctx.trace.record_error(node=node, action="model_call", error=exc)
            if not exc.retryable:
                raise
            continue
        tokens = result.usage.total_tokens if result.usage is not None else 0
        ctx.budget.settle(
            reservation_id=reservation.reservation_id,
            actual=budget_ledger.Cost(tokens=tokens, cost_microunits=0),
            settlement_key=f"{purpose_key}:settled",
            cost_source="provider_reported",
        )
        settlements += 1
        ctx.trace.record(node=node, action="model_call", status="ok", attempt=attempt,
                         counts={"prompt_tokens": ctx.config.prompt_tokens})
        return {
            "text_length": len(result.text or ""),
            "attempts": attempt,
            "budget": StepBudget(reservations, settlements, releases, tuple(purpose_keys)),
        }
    raise HarnessError(f"模型调用 {ctx.config.max_model_attempts} 次均失败：{last_error}")


# ---------------------------------------------------------------------------
# GraphRun：一张最小可运行图
# ---------------------------------------------------------------------------
class GraphRun:
    """单 run 的 Harness。所有状态写入都经已有组件的公开 API。"""

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        workspace_id: UUID,
        run_id: UUID,
        thread_id: UUID,
        principal: Principal,
        scope: WorkspaceScope,
        site_url: str,
        config: HarnessConfig | None = None,
        trace: safe_trace.SafeTrace | None = None,
        model_script: Sequence[tuple[str, Any]] = (),
        read_tools: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
        price: Any = None,
        budget_conn: psycopg.Connection[Any] | None = None,
    ) -> None:
        self.conn = conn
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.thread_id = thread_id
        self.principal = principal
        self.scope = scope
        self.site_url = site_url
        self.config = config or HarnessConfig()
        self.trace = trace or safe_trace.SafeTrace(run_id=run_id)
        self.state = GraphState()
        self.read_tools = dict(read_tools or {})
        self.downstream = approval_probe.Downstream(site_url)
        # BudgetLedger 明确要求 autocommit=False（它自己做事务与 savepoint）；
        # 其余组件要求 autocommit=True。两种连接并存，各自遵守自己的契约。
        self.budget_conn = budget_conn or psycopg.connect(
            scope_module.read_dsn(), autocommit=False
        )
        self._own_budget_conn = budget_conn is None
        self.budget = budget_ledger.BudgetLedger(self.budget_conn)
        self.transport = _ScriptedTransport(model_script)
        self.gateway = _build_gateway(self.transport)
        self.price = price
        self._checkpoint: checkpoint_probe.Harness | None = None
        self._invocation: checkpoint_probe.Invocation | None = None
        self._binding: checkpoint_probe.RunBinding | None = None
        self.requester_id = principal.principal_id
        self.approver_id = _derive(self.principal.principal_id, "approver")
        self.budget_ready = False
        self._trace_lock = threading.Lock()
        self._jobs_conn: psycopg.Connection[Any] | None = None
        self._prepare_principals()

    # -- 初始化 -----------------------------------------------------------
    def _prepare_principals(self) -> None:
        with suppress(psycopg.errors.UniqueViolation):
            approval_probe.register_workspace(
                self.conn, workspace_id=self.workspace_id, slug=f"t21-{self.run_id.hex[:12]}"
            )
        with suppress(psycopg.errors.UniqueViolation):
            approval_probe.register_principal(
                self.conn,
                workspace_id=self.workspace_id,
                principal_id=self.requester_id,
                role="requester",
            )
        with suppress(psycopg.errors.UniqueViolation):
            approval_probe.register_principal(
                self.conn,
                workspace_id=self.workspace_id,
                principal_id=self.approver_id,
                role="approver",
            )
        self.conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_graph_runs "
            "(workspace_id, run_id, thread_id, graph_version, state_schema_version, status) "
            "VALUES (%s, %s, %s, %s, %s, 'idle') ON CONFLICT (workspace_id, run_id) DO NOTHING",
            (
                self.workspace_id,
                self.run_id,
                self.thread_id,
                GRAPH_VERSION,
                STATE_SCHEMA_VERSION,
            ),
        )

    def _ensure_budget(self) -> None:
        if self.budget_ready:
            return
        self.budget.ensure(
            scope=self.scope,
            run_id=self.run_id,
            limits=budget_ledger.BudgetLimits(
                cost_microunits=1_000_000, tokens=100_000, concurrency=2
            ),
        )
        self.budget_ready = True

    # -- 取消 / 超时（事实，不是异常流） -----------------------------------
    def request_cancel(self, *, reason_code: str = "USER_CANCEL") -> None:
        self.conn.execute(
            f"UPDATE {SCHEMA}.phase0_graph_runs SET cancel_requested_at = now(), "
            "cancel_reason_code = %s, updated_at = now() "
            "WHERE workspace_id = %s AND run_id = %s AND cancel_requested_at IS NULL",
            (reason_code, self.workspace_id, self.run_id),
        )
        self.trace.record(node="control", action="cancel_requested", status="ok",
                          note_code=reason_code)

    def set_deadline(self, *, seconds: float) -> None:
        self.conn.execute(
            f"UPDATE {SCHEMA}.phase0_graph_runs "
            "SET deadline_at = now() + make_interval(secs => %s), "
            "updated_at = now() WHERE workspace_id = %s AND run_id = %s",
            (seconds, self.workspace_id, self.run_id),
        )

    def _cancel_requested(self) -> bool:
        row = self.conn.execute(
            f"SELECT cancel_requested_at FROM {SCHEMA}.phase0_graph_runs "
            "WHERE workspace_id = %s AND run_id = %s",
            (self.workspace_id, self.run_id),
        ).fetchone()
        return bool(row and row[0] is not None)

    def _deadline_passed(self) -> bool:
        row = self.conn.execute(
            f"SELECT deadline_at IS NOT NULL AND deadline_at <= now() "
            f"FROM {SCHEMA}.phase0_graph_runs WHERE workspace_id = %s AND run_id = %s",
            (self.workspace_id, self.run_id),
        ).fetchone()
        return bool(row and row[0])

    def _guard(self, *, node: str, action: str) -> None:
        """每个节点、每个工具调用之前都要过这道闸。"""

        if self._cancel_requested():
            self.trace.record(node=node, action=action, status="skipped",
                              note_code="CANCEL_OBSERVED")
            raise RunCancelled(f"{node}:{action} 被取消，后续调用一律不执行")
        if self._deadline_passed():
            self.trace.record(node=node, action=action, status="skipped",
                              note_code="DEADLINE_OBSERVED")
            raise RunTimedOut(f"{node}:{action} 超过 deadline")

    # -- 节点事件（append-only） ------------------------------------------
    def _event(self, node: str, status: str, detail: Mapping[str, Any] | None = None) -> None:
        self.conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_node_events (workspace_id, run_id, node, status, detail) "
            "VALUES (%s, %s, %s, %s, %s)",
            (self.workspace_id, self.run_id, node, status, Jsonb(dict(detail or {}))),
        )

    def _set_status(self, status: str, *, node: str | None = None) -> None:
        nodes = list(self.state.completed)
        self.conn.execute(
            f"UPDATE {SCHEMA}.phase0_graph_runs SET status = %s, completed_nodes = %s, "
            "updated_at = now() WHERE workspace_id = %s AND run_id = %s",
            (status, nodes, self.workspace_id, self.run_id),
        )
        self.state.status = status
        if node is not None:
            self.state.completed.append(node)

    # -- checkpoint（唯一写路径 = T05 的 Invocation.write） ------------------
    def _checkpointer(self) -> tuple[checkpoint_probe.Harness, checkpoint_probe.Invocation]:
        """拿到 T05 的宿主：run 已存在就**接管**它，不存在才创建（进程重启的正常路径）。"""

        if self._invocation is not None:
            return self._checkpoint, self._invocation  # type: ignore[return-value]
        ckpt = checkpoint_probe.Harness(dsn=scope_module.read_dsn())
        ckpt.ensure_schema()
        binding = checkpoint_probe.RunBinding(
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            thread_id=self.thread_id,
        )
        existing = ckpt.epoch_of(binding)
        if existing < 0:  # 还没有这条 run/thread 绑定
            ckpt.create_run(binding)
            expected_epoch = 0
        else:
            expected_epoch = existing
        invocation = ckpt.new_invocation(binding)
        invocation.acquire_lock()
        invocation.claim_owner(expected_epoch)
        self._checkpoint, self._binding, self._invocation = ckpt, binding, invocation
        return ckpt, invocation

    def checkpoint_node(self, node: str) -> int | None:
        """把 ``{node, state}`` 写进 T05 的 checkpoint；返回 seq。

        这里刻意**只调用** ``Invocation.write``：既不碰 T05 的表结构，也不在
        本模块维护第二份快照。
        """

        _, invocation = self._checkpointer()
        payload = {"node": node, "state": self.state.as_payload(),
                   "trace": self.trace.to_state_fragment()}
        result = invocation.write(payload)
        if not result.accepted:
            raise HarnessError(f"checkpoint 写入被拒：{result.code} {result.detail}")
        self.trace.record(node=node, action="checkpoint", status="ok",
                          counts={"seq": int(result.seq or 0)})
        return result.seq

    def latest_checkpoint(self) -> dict[str, Any] | None:
        ckpt, _ = self._checkpointer()
        row = ckpt.restore_read(self._binding)
        if row is None:
            return None
        latest = ckpt.latest_checkpoint(self._binding)
        return latest.checkpoint if latest is not None else None

    # -- 节点 1：plan（模型 + 预算逐 attempt） ----------------------------
    def _model_context(self) -> ModelCallContext:
        return ModelCallContext(
            budget=self.budget,
            gateway=self.gateway,
            transport=self.transport,
            scope=self.scope,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            config=self.config,
            trace=self.trace,
            price=self.price,
        )

    def _call_model_with_budget(self, *, node: str) -> dict[str, Any]:
        self._ensure_budget()
        return run_model_step_with_budget(
            self._model_context(),
            node=node,
            guard=lambda n, action: self._guard(node=n, action=action),
        )

    def node_plan(self) -> NodeOutcome:
        self._guard(node="plan", action="enter")
        info = self._call_model_with_budget(node="plan")
        budget: StepBudget = info["budget"]  # type: ignore[assignment]
        self._event("plan", "ok", {"attempts": info["attempts"],
                                   "reservations": budget.reservations})
        self._set_status("running", node="plan")
        self.checkpoint_node("plan")
        return NodeOutcome("plan", "ok")

    # -- 节点 2：read_fanout（只读工具并行） ------------------------------
    def node_read_fanout(self) -> NodeOutcome:
        self._guard(node="read_fanout", action="enter")
        names = sorted(self.read_tools)
        if not names:
            raise HarnessError("没有注册只读工具")

        def run(name: str) -> dict[str, Any]:
            # 注意：**不在 worker 线程里碰 psycopg 连接**（连接非线程安全）。
            # 取消/超时闸门由主线程在 fan-out 前后各查一次，见下。
            started = time.perf_counter()
            handler = self.read_tools[name]
            payload = handler({"query": "p0-t21"})
            elapsed = int((time.perf_counter() - started) * 1000)
            with self._trace_lock:
                self.trace.record(node="read_fanout", action="tool_call", status="ok",
                                  tool=name, tool_version="1.0.0", duration_ms=elapsed,
                                  counts={"bytes": len(json.dumps(dict(payload)))})
            return {"name": name, "payload": dict(payload)}

        self._guard(node="read_fanout", action="fanout_start")
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            results = list(pool.map(run, names))
        # fan-out 期间可能被取消：回到主线程立刻复查，取消后不再进任何工具
        self._guard(node="read_fanout", action="fanout_end")
        self.state.read_results = len(results)
        self._event("read_fanout", "ok", {"tools": len(results)})
        self._set_status("running", node="read_fanout")
        self.checkpoint_node("read_fanout")
        return NodeOutcome("read_fanout", "ok")

    # -- 节点 3：write_approval（写前屏障 → 等待审批） ---------------------
    def node_write_approval(self) -> NodeOutcome:
        self._guard(node="write_approval", action="enter")
        artifact = approval_probe.persist_response(
            self.conn,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            model_step_key=self.config.model_step_key,
            response_digest=_sha("plan-response"),
        )
        scope = approval_probe.ScopeSpec(
            workspace_id=self.workspace_id,
            principal_id=self.requester_id,
            operation="tool.execute",
            resource_refs=(("kb", str(self.run_id)),),
            grant_scope="p0_t21",
        )
        intent = approval_probe.prepare_call(
            self.conn,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            response_artifact_id=artifact,
            call_ordinal=0,
            name="control.write",
            tool_version="1.0.0",
            arguments={**dict(self.config.tool_arguments), "key": str(self.run_id)},
            scope=scope,
        )
        # T19 的 registry 负责 schema/版本校验；T06 负责审批绑定。两者都必须过。
        self.trace.record(node="write_approval", action="intent_prepared", status="ok",
                          tool=intent.name, tool_version=intent.tool_version,
                          intent_key_prefix=intent.intent_key)
        approval_id = approval_probe.ensure_approval(
            self.conn,
            intent,
            requester_principal_id=self.requester_id,
            approver_principal_id=self.approver_id,
        )
        self.state.approval_id = str(approval_id)
        self.state.intent = intent  # type: ignore[attr-defined]
        self.conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_wait_facts "
            "(workspace_id, run_id, node, fact_key, kind, ref_id) "
            "VALUES (%s, %s, %s, %s, 'approval', %s) "
            "ON CONFLICT (workspace_id, run_id, fact_key) DO NOTHING",
            (self.workspace_id, self.run_id, "write_approval",
             f"approval:{approval_id}", approval_id),
        )
        self._event("write_approval", WAITING_APPROVAL, {"approval_registered": 1})
        self._set_status(WAITING_APPROVAL, node="write_approval")
        self.checkpoint_node("write_approval")
        return NodeOutcome("write_approval", WAITING_APPROVAL, waiting_for="approval")

    # -- 节点 4：write_execute（审批通过后执行；CAS 消费在 T06） ------------
    def node_write_execute(self) -> NodeOutcome:
        self._guard(node="write_execute", action="enter")
        intent = getattr(self.state, "intent", None)
        if intent is None:
            intent = self._recover_intent()
        approval_id = UUID(str(self.state.approval_id or self._recover_approval_id()))
        result = approval_probe.execute(
            self.conn,
            downstream=self.downstream,
            approval_id=approval_id,
            intent=intent,
            requester_principal_id=self.requester_id,
            approver_principal_id=self.approver_id,
        )
        self.trace.record(
            node="write_execute",
            action="tool_call",
            status="ok" if result.outcome in ("succeeded", "replayed") else "rejected",
            tool=intent.name,
            tool_version=intent.tool_version,
            intent_key_prefix=intent.intent_key,
            error_code=result.error_code,
            counts={"replayed": int(result.replayed)},
        )
        self._event("write_execute", result.outcome, {"code": result.error_code or ""})
        if result.outcome == "rejected":
            raise HarnessError(f"写工具被拒：{result.error_code}")
        self._set_status("running", node="write_execute")
        self.checkpoint_node("write_execute")
        return NodeOutcome("write_execute", result.outcome)

    # -- 节点 5：job_wake（T08 enqueue + 结果读取） ------------------------
    def _jobs(self) -> psycopg.Connection[Any]:
        """T08 的连接自成一档：它要求 ``row_factory=dict_row`` + 显式提交。"""

        if self._jobs_conn is None or self._jobs_conn.closed:
            self._jobs_conn = job_probe.connect()
        return self._jobs_conn

    def node_job_wake(self) -> NodeOutcome:
        self._guard(node="job_wake", action="enter")
        job_id = uuid4()
        jobs = self._jobs()
        enqueued = job_probe.enqueue(
            jobs,
            job_probe.EnqueueRequest(
                workspace_id=self.workspace_id,
                target_id=self.run_id,
                job_id=job_id,
                dedup_key=f"t21-{self.run_id}-{job_id}",
                target_name="p0-t21-target",
            ),
        )
        jobs.commit()
        self.state.job_id = str(enqueued.job_id)
        # Worker 侧（T08 的 record_result）——Harness 不判 Job 业务结果，只消费事实。
        job_probe.record_result(
            jobs,
            self.workspace_id,
            enqueued.job_id,
            generation=enqueued.execution_generation,
            outcome="succeeded",
        )
        jobs.commit()
        woke = self._job_result(enqueued.job_id)
        self.conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_wait_facts "
            "(workspace_id, run_id, node, fact_key, kind, ref_id) "
            "VALUES (%s, %s, %s, %s, 'job', %s) "
            "ON CONFLICT (workspace_id, run_id, fact_key) DO NOTHING",
            (self.workspace_id, self.run_id, "job_wake", f"job:{enqueued.job_id}",
             enqueued.job_id),
        )
        self.trace.record(node="job_wake", action="job_result", status=woke or "unknown",
                          counts={"jobs": 1})
        self._event("job_wake", woke or "unknown", {"generation": enqueued.execution_generation})
        self._set_status("running", node="job_wake")
        self.checkpoint_node("job_wake")
        return NodeOutcome("job_wake", woke or "unknown")

    def _job_result(self, job_id: UUID) -> str | None:
        row = self.conn.execute(
            f"SELECT outcome FROM {job_probe.SCHEMA}.phase0_job_results "
            "WHERE workspace_id = %s AND job_id = %s ORDER BY recorded_at DESC LIMIT 1",
            (self.workspace_id, job_id),
        ).fetchone()
        return None if row is None else str(row[0])

    # -- 节点 6：finalize -------------------------------------------------
    def node_finalize(self, *, status: str = COMPLETED) -> NodeOutcome:
        self._set_status(status, node="finalize")
        self.checkpoint_node("finalize")
        self._event("finalize", status, self.trace.to_state_fragment())
        return NodeOutcome("finalize", status)

    # -- 驱动 -------------------------------------------------------------
    def run_until_wait(self) -> list[NodeOutcome]:
        outcomes: list[NodeOutcome] = []
        try:
            outcomes.append(self.node_plan())
            outcomes.append(self.node_read_fanout())
            outcomes.append(self.node_write_approval())
        except RunCancelled:
            outcomes.append(self._finish_cancelled())
        except RunTimedOut:
            outcomes.append(self._finish_timeout())
        return outcomes

    def resume(self) -> list[NodeOutcome]:
        """审批通过后继续；重复 resume 由 T06 的 receipt 保证只执行一次。"""

        outcomes: list[NodeOutcome] = []
        try:
            outcomes.append(self.node_write_execute())
            outcomes.append(self.node_job_wake())
            outcomes.append(self.node_finalize())
        except RunCancelled:
            outcomes.append(self._finish_cancelled())
        except RunTimedOut:
            outcomes.append(self._finish_timeout())
        return outcomes

    def _finish_cancelled(self) -> NodeOutcome:
        self._set_status(CANCELLED, node="finalize")
        self.checkpoint_node("finalize")
        self._event("finalize", CANCELLED, {"reason": "cancel_requested"})
        return NodeOutcome("finalize", CANCELLED)

    def _finish_timeout(self) -> NodeOutcome:
        # 超时绝不写 completed；只写 timeout 终态
        self._set_status(TIMED_OUT, node="finalize")
        self.checkpoint_node("finalize")
        self._event("finalize", TIMED_OUT, {"reason": "deadline"})
        return NodeOutcome("finalize", TIMED_OUT)

    # -- checkpoint 丢失后的恢复（读已提交事实，不回退） -------------------
    def recover_from_facts(self) -> dict[str, Any]:
        """不读 checkpoint，只用已提交事实重建状态（T06 receipt / approval、T08 job）。"""

        approval_id = getattr(self.state, "approval_id", None)
        intent = getattr(self.state, "intent", None)
        if approval_id is None or intent is None:
            intent, approval_id = self._recover_intent_and_approval()
        receipts = tool_ledger.receipts_for(
            self.conn, workspace_id=self.workspace_id, tool_call_id=intent.tool_call_id
        )
        succeeded = [row for row in receipts if row.outcome in ("succeeded", "replayed")]
        self.state.approval_id = str(approval_id)
        self.state.intent = intent  # type: ignore[attr-defined]
        self.state.status = "recovered"
        self.trace.record(node="recover", action="read_facts", status="ok",
                          counts={"receipts": len(receipts), "succeeded": len(succeeded)})
        return {
            "approval_id": str(approval_id),
            "receipts": len(receipts),
            "succeeded": len(succeeded),
            "tool_calls": 1,
        }

    def _recover_intent(self) -> Any:
        row = self.conn.execute(
            f"SELECT tool_call_id FROM {approval_probe.SCHEMA}.phase0_tool_calls "
            "WHERE workspace_id = %s AND run_id = %s ORDER BY created_at LIMIT 1",
            (self.workspace_id, self.run_id),
        ).fetchone()
        if row is None:
            raise HarnessError("恢复失败：没有已提交的工具意图")
        return approval_probe._load_intent(  # noqa: SLF001 - 同一实验包的只读加载
            self.conn, workspace_id=self.workspace_id, tool_call_id=row[0]
        )

    def _recover_approval_id(self) -> UUID:
        row = self.conn.execute(
            f"SELECT approval_id FROM {approval_probe.SCHEMA}.phase0_approvals "
            "WHERE workspace_id = %s AND run_id = %s ORDER BY created_at DESC LIMIT 1",
            (self.workspace_id, self.run_id),
        ).fetchone()
        if row is None:
            raise HarnessError("恢复失败：没有已提交的审批")
        return row[0]

    def _recover_intent_and_approval(self) -> tuple[Any, UUID]:
        return self._recover_intent(), self._recover_approval_id()

    # -- 清理 -------------------------------------------------------------
    def close(self) -> None:
        if self._invocation is not None:
            self._invocation.close()
        if self._checkpoint is not None:
            self._checkpoint.close()
        if self._own_budget_conn and not self.budget_conn.closed:
            self.budget_conn.close()
        if self._jobs_conn is not None and not self._jobs_conn.closed:
            self._jobs_conn.close()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _derive(base: UUID, label: str) -> UUID:
    import hashlib

    digest = hashlib.sha256(f"{base}:{label}".encode()).digest()
    return UUID(bytes=digest[:16], version=4)


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ===========================================================================
# 原生 LangGraph 路径：StateGraph + 原生 interrupt() + 原生 PostgresSaver
# ===========================================================================
# 与上面的 ``GraphRun`` 的分工（见证据 §5）：
#   * **NativeGraphRun 是主路径**：审批等待用 ``langgraph.types.interrupt()``，
#     恢复用 ``Command(resume=...)``，状态由 ``langgraph.checkpoint.postgres.PostgresSaver``
#     持久化；本模块不碰任何 checkpoint 表（只有框架的 ``setup()`` 建表）。
#   * ``GraphRun`` 保留为 **fallback**：它用 ``p0_t21.phase0_wait_facts`` 表达等待，
#     在原生 API 不可用（langgraph 未安装）时仍能跑通同一套业务断言。
#     主路径不读也不写 wait_facts。
NATIVE_NODES: Final[tuple[str, ...]] = (
    "plan",
    "read_fanout",
    "approval_wait",
    "write_execute",
    "job_wake",
    "finalize",
)
#: 由 ``PostgresSaver.setup()`` 创建、由框架读写的表；本模块**不得**直写。
NATIVE_CHECKPOINT_TABLES: Final[tuple[str, ...]] = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)


class NativeState(TypedDict, total=False):
    """原生图的状态：只放引用与计数（prompt/参数原文永远不进 State）。"""

    completed: list[str]
    read_results: int
    model_attempts: int
    approval_id: str
    tool_call_id: str
    resumed: bool
    write_outcome: str
    job_id: str
    status: str


def native_saver_dsn(dsn: str) -> str:
    """原生 saver 的表落在 ``p0_t21``：用连接级 ``search_path`` 限定，不污染 public。"""

    return dsn


def _native_saver_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        scope_module.read_dsn(),
        autocommit=True,
        options=f"-c search_path={SCHEMA}",
    )


def ensure_native_checkpoint_schema(*, dsn: str | None = None) -> tuple[str, ...]:
    """建 schema 并让 ``PostgresSaver.setup()`` 自己建表；返回框架建的表名。"""

    with closing(psycopg.connect(dsn or scope_module.read_dsn(), autocommit=True)) as admin:
        admin.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    with closing(_native_saver_connection()) as conn:
        PostgresSaver(conn).setup()
    with closing(psycopg.connect(dsn or scope_module.read_dsn(), autocommit=True)) as admin:
        rows = admin.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = ANY(%s) ORDER BY 1",
            (SCHEMA, list(NATIVE_CHECKPOINT_TABLES)),
        ).fetchall()
    return tuple(row[0] for row in rows)


def ensure_workspace_principals(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    requester_id: UUID,
    approver_id: UUID,
) -> None:
    with suppress(psycopg.errors.UniqueViolation):
        approval_probe.register_workspace(
            conn, workspace_id=workspace_id, slug=f"t21w-{workspace_id.hex[:10]}"
        )
    with suppress(psycopg.errors.UniqueViolation):
        approval_probe.register_principal(
            conn, workspace_id=workspace_id, principal_id=requester_id, role="requester"
        )
    with suppress(psycopg.errors.UniqueViolation):
        approval_probe.register_principal(
            conn, workspace_id=workspace_id, principal_id=approver_id, role="approver"
        )


class NativeGraphRun:
    """原生 ``StateGraph`` 的一次运行。

    **每次** ``start``/``resume``/``get_state`` 都新开连接（业务连接 + saver 连接），
    所以"跨连接恢复"是结构保证：没有任何 Python 对象被复用。
    """

    def __init__(
        self,
        *,
        thread_id: UUID,
        workspace_id: UUID,
        run_id: UUID,
        principal: Principal,
        scope: WorkspaceScope,
        site_url: str,
        config: HarnessConfig | None = None,
        trace: safe_trace.SafeTrace | None = None,
        model_script: Sequence[tuple[str, Any]] = (("ok", "plan-ok"),),
        read_tools: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
        price: Any = None,
        dsn: str | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.principal = principal
        self.scope = scope
        self.site_url = site_url
        self.config = config or HarnessConfig()
        self.trace = trace or safe_trace.SafeTrace(run_id=run_id)
        self.read_tools = dict(
            read_tools
            or {
                "kb.search": lambda payload: {"hits": 2, "len": len(payload)},
                "kb.fetch": lambda payload: {"bytes": 64, "len": len(payload)},
            }
        )
        self.price = price
        self.dsn = dsn or scope_module.read_dsn()
        self.requester_id = principal.principal_id
        self.approver_id = _derive(principal.principal_id, "approver")
        self.transport = _ScriptedTransport(model_script)
        self.gateway = _build_gateway(self.transport)
        self.downstream = approval_probe.Downstream(site_url)
        # 只读工具在线程池里并行，trace 写入要串行（SafeTrace 本身非线程安全）
        self._trace_lock = threading.Lock()
        with closing(psycopg.connect(self.dsn, autocommit=True)) as conn:
            ensure_workspace_principals(
                conn,
                workspace_id=workspace_id,
                requester_id=self.requester_id,
                approver_id=self.approver_id,
            )

    # -- 配置/连接 --------------------------------------------------------
    @property
    def langgraph_config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": str(self.thread_id)}}

    def _open(self) -> tuple[
        psycopg.Connection[Any],
        budget_ledger.BudgetLedger,
        psycopg.Connection[Any],
        psycopg.Connection[Any],
    ]:
        biz = psycopg.connect(self.dsn, autocommit=True)
        ledger_conn = psycopg.connect(self.dsn, autocommit=False)
        jobs = job_probe.connect()
        return biz, budget_ledger.BudgetLedger(ledger_conn), ledger_conn, jobs

    def _ensure_budget(
        self, ledger: budget_ledger.BudgetLedger, biz: psycopg.Connection[Any]
    ) -> None:
        try:
            ledger.ensure(
                scope=self.scope,
                run_id=self.run_id,
                limits=budget_ledger.BudgetLimits(
                    cost_microunits=1_000_000, tokens=100_000, concurrency=2
                ),
            )
        except budget_ledger.LedgerConfigError:
            raise

    # -- 节点 -------------------------------------------------------------
    def _node_plan(self, state: NativeState, *, biz: Any, ledger: Any) -> NativeState:
        self._ensure_budget(ledger, biz)
        ctx = ModelCallContext(
            budget=ledger,
            gateway=self.gateway,
            transport=self.transport,
            scope=self.scope,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            config=self.config,
            trace=self.trace,
            price=self.price,
        )
        info = run_model_step_with_budget(ctx, node="plan")
        return {
            "completed": [*state.get("completed", []), "plan"],
            "model_attempts": int(info["attempts"]),
        }

    def _node_read_fanout(self, state: NativeState) -> NativeState:
        names = sorted(self.read_tools)

        def run(name: str) -> int:
            handler = self.read_tools[name]
            payload = handler({"query": "p0-t21"})
            started = time.perf_counter()
            elapsed = int((time.perf_counter() - started) * 1000)
            with self._trace_lock:
                self.trace.record(
                    node="read_fanout", action="tool_call", status="ok", tool=name,
                    tool_version="1.0.0", duration_ms=elapsed,
                    counts={"bytes": len(json.dumps(dict(payload)))},
                )
            return 1

        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            done = list(pool.map(run, names))
        return {
            "completed": [*state.get("completed", []), "read_fanout"],
            "read_results": int(sum(done)),
        }

    def _node_approval_wait(self, state: NativeState, *, biz: Any) -> NativeState:
        """**原生 interrupt**：先幂等登记意图/审批，再暂停等外部决定。

        ``interrupt()`` 所在节点在 resume 时会**从头重跑**（LangGraph 文档行为），
        所以 ``prepare_call`` / ``ensure_approval`` 必须幂等——这正是 C-04 §7 的要求，
        也由 T06 的 partial UNIQUE 兜底。
        """

        artifact = approval_probe.persist_response(
            biz,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            model_step_key=self.config.model_step_key,
            response_digest=_sha("native-plan-response"),
        )
        seat = approval_probe.ScopeSpec(
            workspace_id=self.workspace_id,
            principal_id=self.requester_id,
            operation="tool.execute",
            resource_refs=(("kb", str(self.run_id)),),
            grant_scope="p0_t21",
        )
        intent = approval_probe.prepare_call(
            biz,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            response_artifact_id=artifact,
            call_ordinal=0,
            name="control.write",
            tool_version="1.0.0",
            arguments={**dict(self.config.tool_arguments), "key": str(self.run_id)},
            scope=seat,
        )
        approval_id = approval_probe.ensure_approval(
            biz,
            intent,
            requester_principal_id=self.requester_id,
            approver_principal_id=self.approver_id,
        )
        self.trace.record(
            node="approval_wait", action="intent_prepared", status="ok",
            tool=intent.name, tool_version=intent.tool_version,
            intent_key_prefix=intent.intent_key,
        )
        decision = interrupt(
            {
                "kind": "approval",
                "approval_id": str(approval_id),
                "tool_call_id": str(intent.tool_call_id),
                "intent_key_prefix": intent.intent_key[:12],
            }
        )
        if not (isinstance(decision, Mapping) and decision.get("decision") == "approved"):
            raise HarnessError(f"approval_wait 收到非批准决定：{decision!r}")
        self.trace.record(node="approval_wait", action="resumed", status="ok",
                          note_code="NATIVE_INTERRUPT")
        return {
            "completed": [*state.get("completed", []), "approval_wait"],
            "approval_id": str(approval_id),
            "tool_call_id": str(intent.tool_call_id),
            "resumed": True,
        }

    def _node_write_execute(self, state: NativeState, *, biz: Any) -> NativeState:
        intent = approval_probe._load_intent(  # noqa: SLF001 - 同包只读加载
            biz, workspace_id=self.workspace_id, tool_call_id=UUID(state["tool_call_id"])
        )
        result = approval_probe.execute(
            biz,
            downstream=self.downstream,
            approval_id=UUID(state["approval_id"]),
            intent=intent,
            requester_principal_id=self.requester_id,
            approver_principal_id=self.approver_id,
        )
        self.trace.record(
            node="write_execute", action="tool_call",
            status="ok" if result.outcome in ("succeeded", "replayed") else "rejected",
            tool=intent.name, tool_version=intent.tool_version,
            intent_key_prefix=intent.intent_key, error_code=result.error_code,
            counts={"replayed": int(result.replayed)},
        )
        if result.outcome == "rejected":
            raise HarnessError(f"写工具被拒：{result.error_code}")
        return {
            "completed": [*state.get("completed", []), "write_execute"],
            "write_outcome": result.outcome,
        }

    def _node_job_wake(self, state: NativeState, *, jobs: Any) -> NativeState:
        job_id = uuid4()
        enqueued = job_probe.enqueue(
            jobs,
            job_probe.EnqueueRequest(
                workspace_id=self.workspace_id,
                target_id=self.run_id,
                job_id=job_id,
                dedup_key=f"t21n-{self.run_id}-{job_id}",
                target_name="p0-t21-native-target",
            ),
        )
        jobs.commit()
        job_probe.record_result(
            jobs, self.workspace_id, enqueued.job_id,
            generation=enqueued.execution_generation, outcome="succeeded",
        )
        jobs.commit()
        self.trace.record(node="job_wake", action="job_result", status="succeeded",
                          counts={"jobs": 1})
        return {
            "completed": [*state.get("completed", []), "job_wake"],
            "job_id": str(enqueued.job_id),
        }

    def _node_finalize(self, state: NativeState) -> NativeState:
        self.trace.record(node="finalize", action="finalize", status="ok",
                          counts={"completed_nodes": len(state.get("completed", [])) + 1})
        return {
            "completed": [*state.get("completed", []), "finalize"],
            "status": COMPLETED,
        }

    # -- 编译与调用 -------------------------------------------------------
    def _compile(
        self,
        saver: Any,
        *,
        biz: Any,
        ledger: Any,
        jobs: Any,
    ) -> Any:
        graph: Any = StateGraph(NativeState)
        graph.add_node("plan", partial(self._node_plan, biz=biz, ledger=ledger))
        graph.add_node("read_fanout", self._node_read_fanout)
        graph.add_node("approval_wait", partial(self._node_approval_wait, biz=biz))
        graph.add_node("write_execute", partial(self._node_write_execute, biz=biz))
        graph.add_node("job_wake", partial(self._node_job_wake, jobs=jobs))
        graph.add_node("finalize", self._node_finalize)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "read_fanout")
        graph.add_edge("read_fanout", "approval_wait")
        graph.add_edge("approval_wait", "write_execute")
        graph.add_edge("write_execute", "job_wake")
        graph.add_edge("job_wake", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=saver)

    def _invoke(self, input_or_command: Any) -> dict[str, Any]:
        biz, ledger, ledger_conn, jobs = self._open()
        with closing(_native_saver_connection()) as saver_conn:
            try:
                graph = self._compile(
                    PostgresSaver(saver_conn), biz=biz, ledger=ledger, jobs=jobs
                )
                return dict(graph.invoke(input_or_command, self.langgraph_config))
            finally:
                biz.close()
                ledger_conn.close()
                jobs.close()

    def start(self) -> dict[str, Any]:
        """跑到原生 ``interrupt()`` 暂停；返回值里含 ``__interrupt__``。"""

        return self._invoke({"completed": [], "read_results": 0, "model_attempts": 0,
                             "resumed": False, "status": "running"})

    def resume(self, value: Any) -> dict[str, Any]:
        """用 ``Command(resume=...)`` 恢复；**新连接 + 新 saver 实例**。"""

        return self._invoke(Command(resume=value))

    def get_state(self) -> dict[str, Any]:
        with closing(_native_saver_connection()) as saver_conn:
            graph = self._compile(PostgresSaver(saver_conn), biz=None, ledger=None, jobs=None)
            snapshot = graph.get_state(self.langgraph_config)
        return {
            "values": dict(snapshot.values),
            "next": tuple(snapshot.next),
            "interrupts": tuple(
                getattr(item, "value", item) for item in (snapshot.interrupts or ())
            ),
        }

    def checkpoint_tables(self) -> tuple[str, ...]:
        """框架在 ``p0_t21`` 里建的表（用于证明 checkpoint 归框架管理）。"""

        with closing(psycopg.connect(self.dsn, autocommit=True)) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY 1",
                (SCHEMA,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def checkpoint_rows(self) -> int:
        with closing(_native_saver_connection()) as conn:
            row = conn.execute(f"SELECT count(*) FROM {SCHEMA}.checkpoints").fetchone()
        return int(row[0])
