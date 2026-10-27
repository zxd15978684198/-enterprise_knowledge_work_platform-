"""P0-T23 TaskExecutor：结果先落 Task 事实，Graph 状态引用随后（可恢复）。

为什么必须这个顺序（P0-T23 量化验收第 3 条）：

1. 节点执行产生结果 → **先** CAS 写 Task 事实（``succeeded`` + ``fact``）；
2. **再**写 Graph 状态引用（``graph_state_ref`` = thread / checkpoint id / version）；
3. 若第 2 步失败（LangGraph checkpoint 不可用），Task 事实**已经**提交：
   不回滚事实、不把节点退回 pending（否则会重复执行副作用），只留
   ``state_ref_pending`` 事件；重入时 :meth:`TaskExecutor.resume_pending_state_refs`
   只用事实表里的指针需求重试第 2 步，**不再执行** 节点。
4. 旧 state 迟到（旧 epoch / 已有不同引用）由 :func:`task_scheduler.attach_state_ref`
   的 epoch + ``graph_state_ref IS NULL`` CAS 拒绝，旧 checkpoint 不能覆盖新事实。

所有权：checkpoint **内容**归 LangGraph（T05，schema ``p0_t05``）；
本模块只通过 :class:`CheckpointPort` 请求写引用，从不写 checkpoint 表、不存 state
payload。单活前提同 :mod:`task_scheduler`（T05 结论：单活 runner）；多实例安全
不在本任务声明范围。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import psycopg

import task_scheduler as sch

__all__ = [
    "CODE_CHECKPOINT_UNAVAILABLE",
    "CheckpointPort",
    "CheckpointUnavailable",
    "ExecutionOutcome",
    "StateRef",
    "StateRefRequest",
    "TaskExecutor",
    "TaskRunner",
    "fact_digest",
]

CODE_CHECKPOINT_UNAVAILABLE: Final = "CHECKPOINT_UNAVAILABLE"

TaskRunner = Callable[[sch.TaskState], Mapping[str, Any]]


class CheckpointUnavailable(RuntimeError):
    """LangGraph checkpoint 写暂不可用（可重试；事实已提交，不得回滚）。"""


@dataclass(frozen=True, slots=True)
class StateRefRequest:
    workspace_id: Any
    run_id: Any
    revision: int
    task_id: str
    epoch: int
    thread_id: str
    fact_digest: str


@dataclass(frozen=True, slots=True)
class StateRef:
    thread_id: str
    checkpoint_id: str
    version: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "checkpoint_id": self.checkpoint_id,
            "version": self.version,
        }


class CheckpointPort(Protocol):
    """LangGraph checkpoint 的最小写入口（生产适配 T05 的 saver/线程绑定）。"""

    def write_state_ref(self, request: StateRefRequest) -> StateRef: ...


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    task_id: str
    epoch: int
    status: str
    fact_committed: bool
    state_ref: dict[str, Any] | None
    error_code: str | None = None

    @property
    def ref_pending(self) -> bool:
        return self.fact_committed and self.state_ref is None


def fact_digest(fact: Mapping[str, Any]) -> str:
    """事实的稳定摘要：同一事实跨进程得到同一 hash（用于 checkpoint 绑定）。"""

    canonical = json.dumps(dict(fact), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_thread_id(run_id: Any) -> str:
    """Task 复用 run 的 Graph thread（run↔thread 1:1 归 T05/TDD01 §4）。"""

    return f"run:{run_id}"


class TaskExecutor:
    """执行 graph 节点：事实先行，引用随后，失败可恢复。"""

    def __init__(self, runner: TaskRunner, checkpoint_port: CheckpointPort) -> None:
        self.runner = runner
        self.checkpoint_port = checkpoint_port

    def execute(
        self,
        conn: psycopg.Connection[Any],
        *,
        workspace_id: Any,
        run_id: Any,
        revision: int,
        task: sch.TaskState,
        claim: sch.ClaimedTask,
    ) -> ExecutionOutcome:
        """跑一次节点：先提交事实，再写引用；引用失败只留 pending。"""

        produced = dict(self.runner(task))
        fact = {
            "task_id": task.task_id,
            "kind": task.kind,
            "executor": sch.EXECUTOR_GRAPH,
            "epoch": claim.epoch,
            "result": produced,
        }
        committed = sch.complete_task(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task.task_id,
            epoch=claim.epoch,
            fact=fact,
            owner_id=claim.owner_id,
        )
        request = StateRefRequest(
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task.task_id,
            epoch=claim.epoch,
            thread_id=run_thread_id(run_id),
            fact_digest=fact_digest(fact),
        )
        try:
            ref = self.checkpoint_port.write_state_ref(request)
        except CheckpointUnavailable as exc:
            sch.record_state_ref_pending(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=task.task_id,
                epoch=claim.epoch,
                detail=str(exc) or CODE_CHECKPOINT_UNAVAILABLE,
                owner_id=claim.owner_id,
            )
            return ExecutionOutcome(
                task_id=task.task_id,
                epoch=claim.epoch,
                status=committed.status,
                fact_committed=True,
                state_ref=None,
                error_code=CODE_CHECKPOINT_UNAVAILABLE,
            )
        attached = sch.attach_state_ref(
            conn,
            workspace_id=workspace_id,
            run_id=run_id,
            revision=revision,
            task_id=task.task_id,
            epoch=claim.epoch,
            state_ref=ref.as_dict(),
            owner_id=claim.owner_id,
        )
        return ExecutionOutcome(
            task_id=task.task_id,
            epoch=claim.epoch,
            status=attached.status,
            fact_committed=True,
            state_ref=attached.graph_state_ref,
        )

    def resume_pending_state_refs(
        self,
        conn: psycopg.Connection[Any],
        *,
        workspace_id: Any,
        run_id: Any,
        revision: int,
    ) -> tuple[ExecutionOutcome, ...]:
        """重入：只补写缺失的 Graph 引用，绝不重新执行已完成节点。"""

        outcomes: list[ExecutionOutcome] = []
        for state in sch.tasks_pending_state_ref(conn, workspace_id, run_id, revision):
            fact = state.fact or {}
            request = StateRefRequest(
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=state.task_id,
                epoch=state.epoch,
                thread_id=run_thread_id(run_id),
                fact_digest=fact_digest(fact),
            )
            try:
                ref = self.checkpoint_port.write_state_ref(request)
            except CheckpointUnavailable as exc:
                sch.record_state_ref_pending(
                    conn,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    revision=revision,
                    task_id=state.task_id,
                    epoch=state.epoch,
                    detail=str(exc) or CODE_CHECKPOINT_UNAVAILABLE,
                    owner_id="resumer",
                )
                outcomes.append(
                    ExecutionOutcome(
                        task_id=state.task_id,
                        epoch=state.epoch,
                        status=state.status,
                        fact_committed=True,
                        state_ref=None,
                        error_code=CODE_CHECKPOINT_UNAVAILABLE,
                    )
                )
                continue
            attached = sch.attach_state_ref(
                conn,
                workspace_id=workspace_id,
                run_id=run_id,
                revision=revision,
                task_id=state.task_id,
                epoch=state.epoch,
                state_ref=ref.as_dict(),
                owner_id="resumer",
            )
            outcomes.append(
                ExecutionOutcome(
                    task_id=state.task_id,
                    epoch=state.epoch,
                    status=attached.status,
                    fact_committed=True,
                    state_ref=attached.graph_state_ref,
                )
            )
        return tuple(outcomes)
