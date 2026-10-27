"""P0-T05 探针：原生 Checkpoint 的单写者、失锁后写入拒绝与并发恢复。

验收依据：``docs/poc/03_Phase0_Task_Backlog.md``（P0-T05 任务卡）
契约依据：``docs/poc/07_Contract_Closure.md`` C-03；``docs/tdd/01`` §6.4；``docs/tdd/04`` §7–9

本模块只做一件事：把 C-03 那句"advisory lock 连接释放不能自动 fence 独立
saver 连接"拆成可复跑的实验，并给出**是/否**结论与连接级原始证据。

三条连接（同一次实验里是三个不同的 backend，pid 各自独立）：

* **lock 连接**（``Invocation.lock_conn``）：持 ``pg_advisory_lock``，模拟
  "为 run/thread 拿单写者锁"的那条连接；它被杀掉时 PG 会在 backend 退出时
  自动释放 session advisory lock。
* **writer 连接**（``Invocation.writer_conn``）：模拟 LangGraph 原生
  checkpointer 自己的连接。它**不参与**锁；锁连接死掉不影响它。
* **admin 连接**（``Harness.admin``）：只用于建 schema、读快照、
  ``pg_terminate_backend`` 注入故障。

两个 checkpoint 存储，各自回答不同问题：

1. ``p0_t05.phase0_checkpoints``——**替身存储（surrogate）**。它不是 LangGraph
   的原生 schema，只按原生 saver 的语义（thread → 顺序 checkpoint + parent
   指针 + JSONB 值）建模，用来跑 50 轮循环与并发争抢这类需要确定性顺序的
   实验。fencing 由 ``UPDATE ... WHERE owner_invocation_id = %s AND
   owner_epoch = %s`` 的 CAS 完成（行锁由 PG 提供，客户端不 sleep）。
2. 原生 ``langgraph.checkpoint.postgres.PostgresSaver``（``probe_native_saver``）
   ——只在 ``--with langgraph-checkpoint-postgres`` 注入依赖时存在；用来测
   "杀掉 lock 连接后原生 saver 还能不能写 / 它自己的连接断了会不会自动重连"。
   没有该依赖时结论写 ``NOT_RUN`` 与原始 ImportError，不假称通过。

**本模块不做的**：不改 LangGraph 内部表结构、不自研 snapshot、不宣称
"lease 过期即安全接管"。``old_writer_stopped`` 一律是 ``UNPROVABLE``：数据库
侧看不到"某个连接属于某个 run"，所以锁释放只是接管的**必要**条件。

用法（实验环境，只碰 schema ``p0_t05``）::

    python experiments/phase0/checkpoint_probe.py --rounds 50 --reset
    python experiments/phase0/checkpoint_probe.py --native
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Barrier, Thread
from typing import Any, Final
from urllib.parse import quote
from uuid import UUID, uuid4

import psycopg
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # 命令行直跑时把 experiments/phase0 放进 sys.path
    sys.path.insert(0, str(_HERE))

from scope import read_dsn  # noqa: E402

__all__ = [
    "SCHEMA",
    "Harness",
    "Invocation",
    "OwnerClaim",
    "RunBinding",
    "WriteResult",
    "run_binding_consistency",
    "run_epoch_race",
    "probe_native_saver",
    "run_owner_churn_cycles",
    "run_reconnect_after_writer_loss",
    "run_two_writer_contention",
    "write_checkpoint",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SCHEMA: Final = "p0_t05"
LOCK_CLASSID: Final = 5050
DEFAULT_LOCK_TIMEOUT_S: Final = 10.0
# 行锁/接管等待的上界：把"可能永远等"变成可断言的 LOCK_TIMEOUT，避免探针自己挂死。
CLAIM_LOCK_TIMEOUT_MS: Final = 5000
WRITE_LOCK_TIMEOUT_MS: Final = 5000
GRAPH_VERSION: Final = "rag.v1"
STATE_SCHEMA_VERSION: Final = 1

# 稳定错误码：证据与后续 T06/T21/T23/T30 都按这些字符串断言，不要改。
CODE_ACCEPTED: Final = "ACCEPTED"
CODE_LOCK_HELD: Final = "LOCAL_LOCK_HELD"
CODE_LOCK_TIMEOUT: Final = "LOCK_TIMEOUT"
CODE_LOCK_NOT_HELD: Final = "LOCK_NOT_HELD"
CODE_OWNER_CONFLICT: Final = "OWNER_CLAIM_CONFLICT"
CODE_RUN_NOT_FOUND: Final = "RUN_NOT_FOUND"
CODE_RUN_NOT_RUNNING: Final = "RUN_NOT_RUNNING"
CODE_STALE_EPOCH: Final = "STALE_OWNER_EPOCH"
CODE_OWNER_MISMATCH: Final = "OWNER_MISMATCH"
CODE_INCOMPATIBLE: Final = "CHECKPOINT_INCOMPATIBLE"
CODE_THREAD_CONFLICT: Final = "THREAD_BINDING_CONFLICT"
CODE_RUN_BOUND_ELSEWHERE: Final = "RUN_BOUND_TO_OTHER_THREAD"
CODE_CONNECTION_LOST: Final = "CONNECTION_LOST"
CODE_PARENT_MISSING: Final = "PARENT_CHECKPOINT_MISSING"
CODE_PG_ERROR: Final = "PG_ERROR"


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------
class ProbeError(RuntimeError):
    """探针自身的错误（环境/用法），不是"被 fence"这种业务结论。"""

    code = "PROBE_ERROR"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = type(self).code
        self.detail = detail


class ProbeSetupError(ProbeError):
    code = "PROBE_SETUP"


class LockTimeoutError(ProbeError):
    code = CODE_LOCK_TIMEOUT


class LockNotAcquiredError(ProbeError):
    code = CODE_LOCK_HELD


class OwnerClaimConflict(ProbeError):
    code = CODE_OWNER_CONFLICT


class ThreadBindingConflict(ProbeError):
    code = CODE_THREAD_CONFLICT


class _Rejected(Exception):
    """写入被拒：只用于把事务内的判定结果带出来，不是对外错误类型。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RunBinding:
    """run ↔ thread 的 1:1 绑定（契约：``UNIQUE(workspace_id, thread_id)``）。"""

    workspace_id: UUID
    run_id: UUID
    thread_id: UUID
    graph_type: str = "rag"
    graph_version: str = GRAPH_VERSION
    state_schema_version: int = STATE_SCHEMA_VERSION

    def lock_key(self) -> tuple[int, int]:
        """(classid, objid)，objid 取 blake2b 前 4 字节并对 int4 取模。"""

        digest = hashlib_blake2b(f"{self.workspace_id}:{self.thread_id}")
        return LOCK_CLASSID, digest


@dataclass(frozen=True, slots=True)
class OwnerClaim:
    """一次 invocation 的 fencing token：离开这个 token 就写不进 checkpoint。"""

    binding: RunBinding
    invocation_id: UUID
    owner_epoch: int
    fencing: bool

    def describe(self) -> str:
        return f"invocation={self.invocation_id} epoch={self.owner_epoch}"


@dataclass(frozen=True, slots=True)
class WriteResult:
    accepted: bool
    code: str
    seq: int | None = None
    checkpoint_id: UUID | None = None
    detail: str = ""

    @property
    def rejected(self) -> bool:
        return not self.accepted


@dataclass(frozen=True, slots=True)
class CheckpointRow:
    seq: int
    checkpoint_id: UUID
    parent_checkpoint_id: UUID | None
    invocation_id: UUID
    owner_epoch: int
    checkpoint: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RestoreRead:
    """一次"重启后恢复"读到的内容 + 它与 run 当前 owner 的一致性。"""

    seq: int
    checkpoint_id: UUID
    invocation_id: UUID
    owner_epoch: int
    run_owner_invocation_id: UUID | None
    run_owner_epoch: int

    @property
    def consistent(self) -> bool:
        return (
            self.invocation_id == self.run_owner_invocation_id
            and self.owner_epoch == self.run_owner_epoch
        )


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    checked: int
    consistent: int
    rate: float
    mismatches: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BindingReport:
    runs: int
    rebind_attempts: int
    rebind_rejections: int
    consistency: ConsistencyReport


@dataclass(frozen=True, slots=True)
class CycleRecord:
    round: int
    epoch_before: int
    epoch_after: int
    invocation_before: UUID
    invocation_after: UUID
    seq_before: int
    stale_code: str
    fresh_code: str
    fresh_seq: int | None
    lock_wait_ms: float
    lock_holders_after_kill: int
    lock_holders_after_acquire: int = 1


@dataclass(frozen=True, slots=True)
class ChurnReport:
    binding: RunBinding
    rounds: int
    fencing: bool
    records: tuple[CycleRecord, ...]
    accepted: int
    rejected: int
    final_seq: int
    expected_final_seq: int
    final_checkpoint_id: UUID | None
    epoch_violations: tuple[str, ...]
    sequence_violations: tuple[str, ...]
    restore_checks: int = 0
    restore_mismatches: tuple[str, ...] = ()
    old_writer_stopped: str = "UNPROVABLE"


@dataclass(frozen=True, slots=True)
class ContentionReport:
    contenders: int
    lock_winners: tuple[int, ...]
    lock_losers: tuple[int, ...]
    lock_loser_codes: tuple[str, ...]
    claim_winners: tuple[int, ...]
    claim_loser_codes: tuple[str, ...]
    write_codes: tuple[str, ...]
    final_epoch: int
    checkpoint_count: int
    epoch_violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EpochRaceReport:
    contenders: int
    claim_winners: tuple[int, ...]
    claim_loser_codes: tuple[str, ...]
    write_codes: tuple[str, ...]
    final_epoch: int
    checkpoint_count: int
    epoch_violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconnectReport:
    writer_pid_before: int | None
    writer_pid_after: int | None
    first_code: str
    second_code: str
    stale_write_accepted: bool
    checkpoint_count: int
    epoch_violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativeStep:
    action: str
    outcome: str
    detail: str


@dataclass(frozen=True, slots=True)
class NativeSaverReport:
    available: bool
    versions: dict[str, str]
    steps: tuple[NativeStep, ...] = ()
    note: str = ""

    def step(self, action: str) -> NativeStep | None:
        for item in self.steps:
            if item.action == action:
                return item
        return None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def hashlib_blake2b(text: str) -> int:
    """由 (workspace, thread) 得到稳定的 int4 advisory lock objid（非负）。"""

    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") % 2_147_483_647


def connect(dsn: str, *, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn, autocommit=autocommit)


def close_quietly(conn: psycopg.Connection[Any] | None) -> None:
    if conn is None:
        return
    with suppress(Exception):
        # 连接已断时 close 可能再抛，不影响结论
        conn.close()


def jsonable(value: Any) -> Any:
    """dataclass → 可 json.dumps 的结构；UUID/datetime 走 default=str。"""

    return asdict(value) if hasattr(value, "__dataclass_fields__") else value


def dump_report(report: Any) -> str:
    return json.dumps(jsonable(report), indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# DDL（本任务不得新建编号迁移，表只在自己 schema 内建）
# ---------------------------------------------------------------------------
_DDL_RUNS: Final = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_runs (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    graph_type text NOT NULL,
    graph_version text NOT NULL,
    state_schema_version integer NOT NULL,
    status text NOT NULL DEFAULT 'running',
    owner_epoch bigint NOT NULL DEFAULT 0,
    owner_invocation_id uuid,
    last_checkpoint_seq bigint NOT NULL DEFAULT 0,
    last_checkpoint_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT phase0_runs_pk PRIMARY KEY (workspace_id, run_id),
    CONSTRAINT phase0_runs_thread_unique UNIQUE (workspace_id, thread_id),
    CONSTRAINT phase0_runs_triple_unique UNIQUE (workspace_id, run_id, thread_id),
    CONSTRAINT phase0_runs_status_check
        CHECK (status IN ('running', 'cancelled', 'completed')),
    CONSTRAINT phase0_runs_epoch_check CHECK (owner_epoch >= 0),
    CONSTRAINT phase0_runs_seq_check CHECK (last_checkpoint_seq >= 0)
)
"""

_DDL_INVOCATIONS: Final = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_invocations (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    invocation_id uuid NOT NULL,
    owner_epoch bigint NOT NULL,
    lock_classid integer NOT NULL,
    lock_objid integer NOT NULL,
    declared_graph_version text NOT NULL,
    declared_state_schema_version integer NOT NULL,
    state text NOT NULL,
    fencing boolean NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state_changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT phase0_invocations_pk
        PRIMARY KEY (workspace_id, run_id, invocation_id),
    CONSTRAINT phase0_invocations_state_check
        CHECK (state IN ('claimed', 'running', 'fenced', 'exited')),
    CONSTRAINT phase0_invocations_epoch_check CHECK (owner_epoch >= 1),
    CONSTRAINT phase0_invocations_run_fk FOREIGN KEY (workspace_id, run_id)
        REFERENCES {SCHEMA}.phase0_runs (workspace_id, run_id) ON DELETE CASCADE
)
"""

# "至多一个有效 writer" 由 DB 结构兜底，而不是靠调用方自觉。
_DDL_INVOCATIONS_SINGLE_WRITER: Final = f"""
CREATE UNIQUE INDEX IF NOT EXISTS phase0_invocations_single_writer
    ON {SCHEMA}.phase0_invocations (workspace_id, run_id)
    WHERE state IN ('claimed', 'running')
"""

_DDL_CHECKPOINTS: Final = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_checkpoints (
    workspace_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    seq bigint NOT NULL,
    checkpoint_id uuid NOT NULL,
    parent_checkpoint_id uuid,
    run_id uuid NOT NULL,
    invocation_id uuid NOT NULL,
    owner_epoch bigint NOT NULL,
    checkpoint jsonb NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT phase0_checkpoints_pk PRIMARY KEY (workspace_id, thread_id, seq),
    CONSTRAINT phase0_checkpoints_id_unique
        UNIQUE (workspace_id, thread_id, checkpoint_id),
    CONSTRAINT phase0_checkpoints_parent_fk
        FOREIGN KEY (workspace_id, thread_id, parent_checkpoint_id)
        REFERENCES {SCHEMA}.phase0_checkpoints (workspace_id, thread_id, checkpoint_id),
    CONSTRAINT phase0_checkpoints_run_thread_fk
        FOREIGN KEY (workspace_id, run_id, thread_id)
        REFERENCES {SCHEMA}.phase0_runs (workspace_id, run_id, thread_id)
)
"""

_DDL_ATTEMPTS: Final = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.phase0_write_attempts (
    attempt_id bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    invocation_id uuid NOT NULL,
    owner_epoch bigint NOT NULL,
    fencing boolean NOT NULL,
    outcome text NOT NULL,
    code text NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT phase0_write_attempts_outcome_check
        CHECK (outcome IN ('accepted', 'rejected'))
)
"""

SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    _DDL_RUNS,
    _DDL_INVOCATIONS,
    _DDL_INVOCATIONS_SINGLE_WRITER,
    _DDL_CHECKPOINTS,
    _DDL_ATTEMPTS,
)

SCHEMA_TABLES: Final[tuple[str, ...]] = (
    "phase0_runs",
    "phase0_invocations",
    "phase0_checkpoints",
    "phase0_write_attempts",
)


# ---------------------------------------------------------------------------
# Harness：schema 生命周期 + 主线程读快照
# ---------------------------------------------------------------------------
class Harness:
    """一个实验会话：建/清 schema，读快照。

    ``Harness`` 自己只持一条 admin 连接，**只在主线程用**；所有并发实验里
    每个线程自己开连接（见 :meth:`new_invocation` / :class:`Invocation`）。
    """

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or read_dsn()
        self.admin: psycopg.Connection[Any] = connect(self.dsn, autocommit=True)

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        close_quietly(self.admin)

    def __enter__(self) -> Harness:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def ensure_schema(self) -> None:
        for statement in SCHEMA_STATEMENTS:
            self.admin.execute(statement)

    def drop_schema(self) -> None:
        self.admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")

    def reset_schema(self) -> None:
        self.drop_schema()
        self.ensure_schema()

    def table_names(self) -> tuple[str, ...]:
        rows = self.admin.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (SCHEMA,),
        ).fetchall()
        return tuple(row[0] for row in rows)

    def single_writer_index(self) -> bool:
        row = self.admin.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
            (SCHEMA, "phase0_invocations_single_writer"),
        ).fetchone()
        return row is not None and "WHERE" in row[0].upper()

    # -- run/thread 绑定 --------------------------------------------------
    def create_run(self, binding: RunBinding | None = None, **kwargs: Any) -> RunBinding:
        if binding is None:
            binding = RunBinding(
                workspace_id=kwargs.pop("workspace_id", uuid4()),
                run_id=kwargs.pop("run_id", uuid4()),
                thread_id=kwargs.pop("thread_id", uuid4()),
                **kwargs,
            )
        self.bind(binding)
        return binding

    def bind(self, binding: RunBinding) -> RunBinding:
        try:
            self.admin.execute(
                f"INSERT INTO {SCHEMA}.phase0_runs "
                "(workspace_id, run_id, thread_id, graph_type, graph_version, "
                " state_schema_version) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    binding.workspace_id,
                    binding.run_id,
                    binding.thread_id,
                    binding.graph_type,
                    binding.graph_version,
                    binding.state_schema_version,
                ),
            )
        except pg_errors.UniqueViolation as exc:
            constraint = exc.diag.constraint_name or ""
            if constraint == "phase0_runs_thread_unique":
                raise ThreadBindingConflict(
                    "thread 已绑定到另一个 run", detail=constraint
                ) from exc
            if constraint in {"phase0_runs_pk", "phase0_runs_triple_unique"}:
                raise ThreadBindingConflict(
                    "run 已绑定到另一个 thread", detail=CODE_RUN_BOUND_ELSEWHERE
                ) from exc
            raise
        return binding

    def run_row(self, binding: RunBinding) -> dict[str, Any] | None:
        row = self.admin.execute(
            f"SELECT owner_epoch, owner_invocation_id, status, last_checkpoint_seq, "
            f"last_checkpoint_id, graph_version, state_schema_version "
            f"FROM {SCHEMA}.phase0_runs WHERE workspace_id = %s AND run_id = %s",
            (binding.workspace_id, binding.run_id),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "owner_epoch",
            "owner_invocation_id",
            "status",
            "last_checkpoint_seq",
            "last_checkpoint_id",
            "graph_version",
            "state_schema_version",
        )
        return dict(zip(keys, row, strict=True))

    def epoch_of(self, binding: RunBinding) -> int:
        row = self.run_row(binding)
        return -1 if row is None else int(row["owner_epoch"])

    def live_writers(self, binding: RunBinding) -> int:
        row = self.admin.execute(
            f"SELECT count(*) FROM {SCHEMA}.phase0_invocations "
            "WHERE workspace_id = %s AND run_id = %s AND state IN ('claimed', 'running')",
            (binding.workspace_id, binding.run_id),
        ).fetchone()
        return int(row[0])

    # -- checkpoint 快照 --------------------------------------------------
    def checkpoints(self, binding: RunBinding) -> tuple[CheckpointRow, ...]:
        rows = self.admin.execute(
            f"SELECT seq, checkpoint_id, parent_checkpoint_id, invocation_id, "
            f"owner_epoch, checkpoint FROM {SCHEMA}.phase0_checkpoints "
            "WHERE workspace_id = %s AND thread_id = %s ORDER BY seq",
            (binding.workspace_id, binding.thread_id),
        ).fetchall()
        return tuple(
            CheckpointRow(
                seq=row[0],
                checkpoint_id=row[1],
                parent_checkpoint_id=row[2],
                invocation_id=row[3],
                owner_epoch=row[4],
                checkpoint=row[5],
            )
            for row in rows
        )

    def latest_checkpoint(self, binding: RunBinding) -> CheckpointRow | None:
        rows = self.checkpoints(binding)
        return rows[-1] if rows else None

    def restore_read(self, binding: RunBinding) -> RestoreRead | None:
        """模拟"进程重启后恢复"：用一条全新连接，只凭 (workspace, thread) 读最新 checkpoint。

        只读到"某条"不算恢复成功；必须同时满足
        1. 最新 checkpoint 就是最后被接受的那条（seq / id 对得上）；
        2. 它的 ``invocation_id`` / ``owner_epoch`` 与 run 当前 owner 一致，
           否则说明恢复读到了旧 writer 留下的孤儿状态。
        """

        with closing(connect(self.dsn, autocommit=True)) as conn:
            row = conn.execute(
                f"SELECT c.seq, c.checkpoint_id, c.invocation_id, c.owner_epoch, "
                "r.owner_invocation_id, r.owner_epoch "
                f"FROM {SCHEMA}.phase0_checkpoints c "
                f"JOIN {SCHEMA}.phase0_runs r ON r.workspace_id = c.workspace_id "
                "AND r.thread_id = c.thread_id "
                "WHERE c.workspace_id = %s AND c.thread_id = %s "
                "ORDER BY c.seq DESC LIMIT 1",
                (binding.workspace_id, binding.thread_id),
            ).fetchone()
        if row is None:
            return None
        return RestoreRead(
            seq=int(row[0]),
            checkpoint_id=row[1],
            invocation_id=row[2],
            owner_epoch=int(row[3]),
            run_owner_invocation_id=row[4],
            run_owner_epoch=int(row[5]),
        )

    def epoch_monotonic_violations(self, binding: RunBinding) -> tuple[str, ...]:
        """失锁后仍写入的判定标准：checkpoint 序列里的 ``owner_epoch`` 必须非降。

        旧 invocation 在失锁（epoch 已被推进）之后还能写，就会在序列里留下一个
        **小于前一条**的 epoch；同 epoch 换 invocation 也说明有人共用了 token。
        """

        rows = self.checkpoints(binding)
        violations: list[str] = []
        for previous, current in zip(rows, rows[1:], strict=False):
            if current.owner_epoch < previous.owner_epoch:
                violations.append(
                    f"seq {current.seq}: epoch {current.owner_epoch} < "
                    f"{previous.owner_epoch}（旧 writer 在失锁后写入）"
                )
            elif current.owner_epoch == previous.owner_epoch and (
                current.invocation_id != previous.invocation_id
            ):
                violations.append(
                    f"seq {current.seq}: epoch {current.owner_epoch} 被两个 "
                    f"invocation 使用（{previous.invocation_id} → {current.invocation_id}）"
                )
        return tuple(violations)

    def sequence_violations(self, binding: RunBinding) -> tuple[str, ...]:
        """checkpoint 序列无回退/无空洞，且 runs 的指针指向最新一条。"""

        rows = self.checkpoints(binding)
        violations: list[str] = []
        expected = list(range(1, len(rows) + 1))
        if [row.seq for row in rows] != expected:
            violations.append(f"seq 不连续：{[row.seq for row in rows]}")
        row = self.run_row(binding)
        if row is None:
            return tuple(violations + ["run 行不存在"])
        latest = rows[-1] if rows else None
        if latest is None:
            if int(row["last_checkpoint_seq"]) != 0:
                violations.append("无 checkpoint 但 last_checkpoint_seq != 0")
            return tuple(violations)
        if int(row["last_checkpoint_seq"]) != latest.seq:
            violations.append(
                f"runs.last_checkpoint_seq={row['last_checkpoint_seq']} "
                f"但最新 checkpoint seq={latest.seq}"
            )
        if row["last_checkpoint_id"] != latest.checkpoint_id:
            violations.append(
                f"runs.last_checkpoint_id={row['last_checkpoint_id']} "
                f"但最新 checkpoint_id={latest.checkpoint_id}"
            )
        return tuple(violations)

    def attempt_counts(self, binding: RunBinding) -> dict[str, int]:
        rows = self.admin.execute(
            f"SELECT fencing, outcome, code, count(*) FROM {SCHEMA}.phase0_write_attempts "
            "WHERE workspace_id = %s AND run_id = %s "
            "GROUP BY fencing, outcome, code ORDER BY fencing DESC, outcome, code",
            (binding.workspace_id, binding.run_id),
        ).fetchall()
        counts: dict[str, int] = {}
        for fencing, outcome, code, count in rows:
            key = f"fencing={fencing} {outcome}:{code}"
            counts[key] = int(count)
        return counts

    # -- 单写者不变量 ------------------------------------------------------
    def assert_single_live_writer(self, binding: RunBinding) -> None:
        live = self.live_writers(binding)
        if live > 1:
            raise ProbeError(
                f"run {binding.run_id} 同时有 {live} 个有效 writer", detail="SINGLE_WRITER"
            )

    # -- 并发/循环入口 ----------------------------------------------------
    def new_invocation(
        self, binding: RunBinding, **kwargs: Any
    ) -> Invocation:
        return Invocation(self.dsn, binding, **kwargs)

    # -- advisory lock 观察 ------------------------------------------------
    def advisory_lock_holders(self, binding: RunBinding) -> tuple[int, ...]:
        classid, objid = binding.lock_key()
        rows = self.admin.execute(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid = %s::oid AND objid = %s::oid AND granted ORDER BY pid",
            (classid, objid),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def terminate_backend(self, pid: int) -> bool:
        row = self.admin.execute("SELECT pg_terminate_backend(%s::int)", (pid,)).fetchone()
        return bool(row and row[0])


# ---------------------------------------------------------------------------
# Invocation：一条 lock 连接 + 一条 writer 连接 + 自己的 token
# ---------------------------------------------------------------------------
class Invocation:
    """模拟一次 host invocation。

    连接在第一次使用时才建立，因此"锁连接被杀、writer 连接还活着"这种故障
    窗口可以精确复现：``kill_lock()`` 只动 lock 连接。
    """

    def __init__(
        self,
        dsn: str,
        binding: RunBinding,
        *,
        fencing: bool = True,
        invocation_id: UUID | None = None,
        declared_graph_version: str | None = None,
        declared_state_schema_version: int | None = None,
    ) -> None:
        self.dsn = dsn
        self.binding = binding
        self.fencing = fencing
        self.invocation_id = invocation_id or uuid4()
        self.declared_graph_version = declared_graph_version
        self.declared_state_schema_version = declared_state_schema_version
        self.lock_conn: psycopg.Connection[Any] | None = None
        self.lock_pid: int | None = None
        self.claim_conn: psycopg.Connection[Any] | None = None
        self.writer_conn: psycopg.Connection[Any] | None = None
        self.writer_pid: int | None = None
        self.recorder: psycopg.Connection[Any] | None = None
        self.claim: OwnerClaim | None = None
        self.lock_lost = False
        self.last_seq: int | None = None

    # -- 连接 -------------------------------------------------------------
    def open_lock(self) -> psycopg.Connection[Any]:
        if self.lock_conn is None:
            self.lock_conn = connect(self.dsn, autocommit=True)
            self.lock_pid = int(self.lock_conn.execute("SELECT pg_backend_pid()").fetchone()[0])
        return self.lock_conn

    def open_writer(self) -> psycopg.Connection[Any]:
        if self.writer_conn is None or self.writer_conn.closed:
            # autocommit + 显式 conn.transaction()：每个 with 块是真正的
            # BEGIN…COMMIT。非 autocommit 连接上的第一条语句会隐式开事务，此时
            # conn.transaction() 退化成 savepoint，行锁会漏到事务外（已实测）。
            self.writer_conn = connect(self.dsn, autocommit=True)
            self.writer_pid = int(
                self.writer_conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            )
        return self.writer_conn

    def _recorder_conn(self) -> psycopg.Connection[Any]:
        if self.recorder is None or self.recorder.closed:
            self.recorder = connect(self.dsn, autocommit=True)
        return self.recorder

    # -- advisory lock ----------------------------------------------------
    def try_acquire_lock(self) -> bool:
        conn = self.open_lock()
        row = conn.execute(
            "SELECT pg_try_advisory_lock(%s::int, %s::int)", self.binding.lock_key()
        ).fetchone()
        return bool(row and row[0])

    def acquire_lock(self, timeout_s: float | None = None) -> float:
        """阻塞式获取 advisory lock；等待发生在 PG 服务端（``lock_timeout``）。

        客户端不 sleep：锁一释放，PG 立刻返回。返回等待毫秒数（仅用于证据
        里的"服务端确实等了"读数）。
        """

        wait_s = DEFAULT_LOCK_TIMEOUT_S if timeout_s is None else timeout_s
        conn = self.open_lock()
        started = time.perf_counter()
        try:
            with conn.transaction():
                conn.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (f"{max(1, round(wait_s * 1000))}ms",),
                )
                conn.execute("SELECT pg_advisory_lock(%s::int, %s::int)", self.binding.lock_key())
        except pg_errors.LockNotAvailable as exc:
            raise LockTimeoutError(
                f"{wait_s}s 内未拿到 advisory lock（仍被其它 backend 持有）",
                detail="lock_timeout",
            ) from exc
        return (time.perf_counter() - started) * 1000.0

    def holds_lock(self) -> bool:
        if self.lock_conn is None or self.lock_conn.closed or self.lock_pid is None:
            return False
        classid, objid = self.binding.lock_key()
        row = self.lock_conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid = %s::oid AND objid = %s::oid AND granted AND pid = pg_backend_pid()",
            (classid, objid),
        ).fetchone()
        return bool(row and int(row[0]) == 1)

    def kill_lock(self, harness: Harness) -> int | None:
        """从 admin 连接 terminate 自己的 lock backend：模拟"锁连接断了"。"""

        pid = self.lock_pid
        if pid is None:
            return None
        harness.terminate_backend(pid)
        close_quietly(self.lock_conn)
        self.lock_conn = None
        self.lock_lost = True
        return pid

    # -- owner claim ------------------------------------------------------
    @contextmanager
    def _claim_transaction(self) -> Iterator[psycopg.Connection[Any]]:
        if self.claim_conn is None or self.claim_conn.closed:
            self.claim_conn = connect(self.dsn, autocommit=True)
        with self.claim_conn.transaction():
            self.claim_conn.execute(
                "SELECT set_config('lock_timeout', %s, true)", (f"{CLAIM_LOCK_TIMEOUT_MS}ms",)
            )
            yield self.claim_conn

    def claim_owner(self, expected_owner_epoch: int) -> OwnerClaim:
        """CAS 推进 ``owner_epoch`` 并把上一个 invocation 标记为 fenced。

        成功与否完全由 ``WHERE owner_epoch = %s`` 决定：两个 host 同时拿同一
        个 expected epoch 竞争时，只有一个 UPDATE 能命中行。
        """

        binding = self.binding
        try:
            claim = self._claim_once(binding, expected_owner_epoch)
        except pg_errors.LockNotAvailable as exc:
            raise LockTimeoutError(
                f"claim 等行锁超过 {CLAIM_LOCK_TIMEOUT_MS}ms", detail=str(exc)
            ) from exc
        return claim

    def _claim_once(self, binding: RunBinding, expected_owner_epoch: int) -> OwnerClaim:
        with self._claim_transaction() as conn:
            row = conn.execute(
                f"UPDATE {SCHEMA}.phase0_runs SET owner_epoch = owner_epoch + 1, "
                "owner_invocation_id = %s, updated_at = clock_timestamp() "
                "WHERE workspace_id = %s AND run_id = %s AND owner_epoch = %s "
                "AND status = 'running' "
                "RETURNING owner_epoch, graph_version, state_schema_version",
                (self.invocation_id, binding.workspace_id, binding.run_id, expected_owner_epoch),
            ).fetchone()
            if row is None:
                raise OwnerClaimConflict(
                    f"owner_epoch CAS 失败：期望 {expected_owner_epoch}",
                    detail=self._diagnose_claim(conn, expected_owner_epoch),
                )
            owner_epoch, graph_version, state_schema_version = row
            conn.execute(
                f"UPDATE {SCHEMA}.phase0_invocations SET state = 'fenced', "
                "state_changed_at = clock_timestamp() "
                "WHERE workspace_id = %s AND run_id = %s "
                "AND state IN ('claimed', 'running') AND invocation_id <> %s",
                (binding.workspace_id, binding.run_id, self.invocation_id),
            )
            if self.declared_graph_version is None:
                self.declared_graph_version = graph_version
            if self.declared_state_schema_version is None:
                self.declared_state_schema_version = state_schema_version
            classid, objid = binding.lock_key()
            conn.execute(
                f"INSERT INTO {SCHEMA}.phase0_invocations "
                "(workspace_id, run_id, invocation_id, owner_epoch, lock_classid, lock_objid, "
                " declared_graph_version, declared_state_schema_version, state, fencing) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'running', %s)",
                (
                    binding.workspace_id,
                    binding.run_id,
                    self.invocation_id,
                    int(owner_epoch),
                    classid,
                    objid,
                    self.declared_graph_version,
                    int(self.declared_state_schema_version or 0),
                    self.fencing,
                ),
            )
        self.claim = OwnerClaim(
            binding=binding,
            invocation_id=self.invocation_id,
            owner_epoch=int(owner_epoch),
            fencing=self.fencing,
        )
        return self.claim

    def _diagnose_claim(self, conn: psycopg.Connection[Any], expected: int) -> str:
        row = conn.execute(
            f"SELECT owner_epoch, status FROM {SCHEMA}.phase0_runs "
            "WHERE workspace_id = %s AND run_id = %s",
            (self.binding.workspace_id, self.binding.run_id),
        ).fetchone()
        if row is None:
            return CODE_RUN_NOT_FOUND
        if row[1] != "running":
            return CODE_RUN_NOT_RUNNING
        if int(row[0]) == expected:
            return CODE_OWNER_CONFLICT
        return f"{CODE_OWNER_CONFLICT}: owner_epoch 已推进到 {row[0]}"

    def release_owner(self) -> None:
        if self.claim is None:
            return
        with self._claim_transaction() as conn:
            conn.execute(
                f"UPDATE {SCHEMA}.phase0_runs SET owner_invocation_id = NULL, "
                "updated_at = clock_timestamp() "
                "WHERE workspace_id = %s AND run_id = %s "
                "AND owner_invocation_id = %s AND owner_epoch = %s",
                (
                    self.binding.workspace_id,
                    self.binding.run_id,
                    self.claim.invocation_id,
                    self.claim.owner_epoch,
                ),
            )
            conn.execute(
                f"UPDATE {SCHEMA}.phase0_invocations SET state = 'exited', "
                "state_changed_at = clock_timestamp() "
                "WHERE workspace_id = %s AND run_id = %s AND invocation_id = %s",
                (self.binding.workspace_id, self.binding.run_id, self.claim.invocation_id),
            )

    def close(self, *, state: str = "exited") -> None:
        if self.claim is not None and self.claim_conn is not None and not self.claim_conn.closed:
            try:
                with self._claim_transaction() as conn:
                    conn.execute(
                        f"UPDATE {SCHEMA}.phase0_invocations SET state = %s, "
                        "state_changed_at = clock_timestamp() "
                        "WHERE workspace_id = %s AND run_id = %s AND invocation_id = %s "
                        "AND state IN ('claimed', 'running')",
                        (
                            state,
                            self.binding.workspace_id,
                            self.binding.run_id,
                            self.claim.invocation_id,
                        ),
                    )
            except psycopg.Error:
                pass
        for conn in (self.writer_conn, self.claim_conn, self.lock_conn, self.recorder):
            close_quietly(conn)
        self.writer_conn = None
        self.claim_conn = None
        self.lock_conn = None
        self.recorder = None

    # -- 写 checkpoint ----------------------------------------------------
    def write(
        self,
        payload: Mapping[str, Any],
        *,
        metadata: Mapping[str, Any] | None = None,
        parent_checkpoint_id: UUID | None = None,
        fencing: bool | None = None,
    ) -> WriteResult:
        consequence = write_checkpoint(
            self,
            payload,
            metadata=metadata,
            parent_checkpoint_id=parent_checkpoint_id,
            fencing=fencing,
        )
        return consequence


def write_checkpoint(
    invocation: Invocation,
    payload: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None = None,
    parent_checkpoint_id: UUID | None = None,
    fencing: bool | None = None,
) -> WriteResult:
    """写一条 checkpoint：per-run 行锁分配 seq，随后校验 owner token。

    顺序是刻意的：

    1. ``SELECT ... FOR UPDATE`` 拿到 run 行锁（DB 侧串行化，客户端不 sleep）；
    2. ``fencing=True`` 时用 ``(owner_invocation_id, owner_epoch)`` 校验 token，
       并用同样的谓词再 UPDATE 一次（CAS 兜底）；
    3. 校验通过才 INSERT checkpoint，同一事务提交。

    ``fencing=False`` 是 C-03 的对照组：**完全不看 token**，等价于一个不知道
    自己被 fence 了的原生 saver 连接。它仍然要拿行锁，只是为了给 seq 一个
    确定的顺序，不代表它具备任何写权限。
    """

    claim = invocation.claim
    if claim is None:
        raise ProbeSetupError("invocation 尚未 claim owner，不能写 checkpoint")
    effective_fencing = invocation.fencing if fencing is None else fencing
    binding = claim.binding
    body = Jsonb(dict(payload))
    extra = Jsonb(dict(metadata or {}))
    result: WriteResult
    try:
        conn = invocation.open_writer()
        with conn.transaction():
            conn.execute(
                "SELECT set_config('lock_timeout', %s, true)", (f"{WRITE_LOCK_TIMEOUT_MS}ms",)
            )
            row = conn.execute(
                f"SELECT last_checkpoint_seq, last_checkpoint_id, owner_invocation_id, "
                f"owner_epoch, status, graph_version, state_schema_version "
                f"FROM {SCHEMA}.phase0_runs "
                "WHERE workspace_id = %s AND run_id = %s FOR UPDATE",
                (binding.workspace_id, binding.run_id),
            ).fetchone()
            if row is None:
                raise _Rejected(CODE_RUN_NOT_FOUND)
            (
                previous_seq,
                previous_checkpoint_id,
                owner_invocation_id,
                owner_epoch,
                status,
                graph_version,
                state_schema_version,
            ) = row
            if status != "running":
                raise _Rejected(CODE_RUN_NOT_RUNNING, str(status))
            if effective_fencing:
                if (
                    invocation.declared_graph_version is not None
                    and graph_version != invocation.declared_graph_version
                ) or (
                    invocation.declared_state_schema_version is not None
                    and int(state_schema_version) != int(invocation.declared_state_schema_version)
                ):
                    raise _Rejected(
                        CODE_INCOMPATIBLE,
                        f"宿主编译版本 {invocation.declared_graph_version}/"
                        f"{invocation.declared_state_schema_version} 与 thread 记录的 "
                        f"{graph_version}/{state_schema_version} 不一致",
                    )
                if int(owner_epoch) != claim.owner_epoch:
                    raise _Rejected(
                        CODE_STALE_EPOCH,
                        f"token epoch={claim.owner_epoch} 已过期，当前={owner_epoch}",
                    )
                if owner_invocation_id != claim.invocation_id:
                    raise _Rejected(CODE_OWNER_MISMATCH, f"owner={owner_invocation_id}")
            new_seq = int(previous_seq) + 1
            checkpoint_id = uuid4()
            parent = (
                parent_checkpoint_id
                if parent_checkpoint_id is not None
                else previous_checkpoint_id
            )
            if parent is not None:
                exists = conn.execute(
                    f"SELECT count(*) FROM {SCHEMA}.phase0_checkpoints "
                    "WHERE workspace_id = %s AND thread_id = %s AND checkpoint_id = %s",
                    (binding.workspace_id, binding.thread_id, parent),
                ).fetchone()
                if not exists or int(exists[0]) != 1:
                    raise _Rejected(CODE_PARENT_MISSING, f"parent {parent} 不存在")
            update_sql = (
                f"UPDATE {SCHEMA}.phase0_runs SET last_checkpoint_seq = %s, "
                "last_checkpoint_id = %s, updated_at = clock_timestamp() "
                "WHERE workspace_id = %s AND run_id = %s"
            )
            params: list[Any] = [new_seq, checkpoint_id, binding.workspace_id, binding.run_id]
            if effective_fencing:
                update_sql += " AND owner_invocation_id = %s AND owner_epoch = %s"
                params.extend([claim.invocation_id, claim.owner_epoch])
            updated = conn.execute(update_sql, params)
            if updated.rowcount != 1:
                raise _Rejected(CODE_STALE_EPOCH, "seq 分配时 token 失效")
            conn.execute(
                f"INSERT INTO {SCHEMA}.phase0_checkpoints "
                "(workspace_id, thread_id, seq, checkpoint_id, parent_checkpoint_id, "
                " run_id, invocation_id, owner_epoch, checkpoint, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    binding.workspace_id,
                    binding.thread_id,
                    new_seq,
                    checkpoint_id,
                    parent,
                    binding.run_id,
                    claim.invocation_id,
                    claim.owner_epoch,
                    body,
                    extra,
                ),
            )
        result = WriteResult(
            accepted=True,
            code=CODE_ACCEPTED,
            seq=new_seq,
            checkpoint_id=checkpoint_id,
        )
        invocation.last_seq = new_seq
    except _Rejected as rejected:
        result = WriteResult(accepted=False, code=rejected.code, detail=rejected.detail)
    except pg_errors.LockNotAvailable:
        # lock_timeout 生效：绝不无限等行锁，把"等不到"变成可记录的拒绝结果
        result = WriteResult(
            accepted=False,
            code=CODE_LOCK_TIMEOUT,
            detail=f"row lock wait > {WRITE_LOCK_TIMEOUT_MS}ms",
        )
    except psycopg.OperationalError as exc:
        close_quietly(invocation.writer_conn)
        invocation.writer_conn = None
        result = WriteResult(
            accepted=False, code=CODE_CONNECTION_LOST, detail=str(exc).strip().splitlines()[0]
        )
    except pg_errors.UniqueViolation as exc:
        result = WriteResult(accepted=False, code=CODE_PG_ERROR, detail=str(exc).strip())
    _record_attempt(invocation, result, fencing=effective_fencing)
    return result


def _record_attempt(invocation: Invocation, result: WriteResult, *, fencing: bool) -> None:
    """把本次尝试写进账本（独立 autocommit 连接，不受 writer 事务回滚影响）。"""

    claim = invocation.claim
    if claim is None:
        return
    try:
        conn = invocation._recorder_conn()
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_write_attempts "
            "(workspace_id, run_id, invocation_id, owner_epoch, fencing, outcome, code) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                claim.binding.workspace_id,
                claim.binding.run_id,
                claim.invocation_id,
                claim.owner_epoch,
                fencing,
                "accepted" if result.accepted else "rejected",
                result.code,
            ),
        )
    except Exception:  # noqa: BLE001 - 账本是观测，不能反过来改变实验结论
        pass


# ---------------------------------------------------------------------------
# 实验一：run/thread 绑定一致率
# ---------------------------------------------------------------------------
def run_binding_consistency(
    harness: Harness, *, runs: int = 100, workspace_id: UUID | None = None
) -> BindingReport:
    """建 ``runs`` 个 run/thread 绑定，再逐个尝试把它们重新绑到别的 run/thread。"""

    workspace = workspace_id or uuid4()
    bindings: list[RunBinding] = []
    for _ in range(runs):
        bindings.append(
            harness.create_run(
                RunBinding(workspace_id=workspace, run_id=uuid4(), thread_id=uuid4())
            )
        )
    attempts = 0
    rejections = 0
    for binding in bindings:
        attempts += 1
        try:
            harness.bind(
                RunBinding(workspace_id=workspace, run_id=uuid4(), thread_id=binding.thread_id)
            )
        except ThreadBindingConflict:
            rejections += 1
        # 同一个 run 换一个 thread 也必须被拒（否则 1:1 变成 1:N）
        attempts += 1
        try:
            harness.bind(
                RunBinding(workspace_id=workspace, run_id=binding.run_id, thread_id=uuid4())
            )
        except ThreadBindingConflict:
            rejections += 1
        # 同 run + 同 thread 重复插入：主键挡掉，也不算新建绑定
        attempts += 1
        try:
            harness.bind(binding)
        except ThreadBindingConflict:
            rejections += 1
    return BindingReport(
        runs=runs,
        rebind_attempts=attempts,
        rebind_rejections=rejections,
        consistency=harness_binding_consistency(harness, workspace),
    )


def harness_binding_consistency(harness: Harness, workspace_id: UUID) -> ConsistencyReport:
    """逐 thread 检查：绑定唯一、checkpoint 全部落在绑定的 run 上。"""

    rows = harness.admin.execute(
        f"SELECT run_id, thread_id FROM {SCHEMA}.phase0_runs WHERE workspace_id = %s",
        (workspace_id,),
    ).fetchall()
    run_of_thread = {row[1]: row[0] for row in rows}
    mismatches: list[str] = []
    consistent = 0
    for thread_id, run_id in run_of_thread.items():
        foreign = harness.admin.execute(
            f"SELECT count(DISTINCT run_id) FROM {SCHEMA}.phase0_checkpoints "
            "WHERE workspace_id = %s AND thread_id = %s AND run_id <> %s",
            (workspace_id, thread_id, run_id),
        ).fetchone()
        duplicates = harness.admin.execute(
            f"SELECT count(*) FROM {SCHEMA}.phase0_runs "
            "WHERE workspace_id = %s AND thread_id = %s",
            (workspace_id, thread_id),
        ).fetchone()
        if int(foreign[0]) != 0:
            mismatches.append(f"thread {thread_id} 的 checkpoint 出现在别的 run 上")
        elif int(duplicates[0]) != 1:
            mismatches.append(f"thread {thread_id} 绑定了 {duplicates[0]} 个 run")
        else:
            consistent += 1
    checked = len(run_of_thread)
    rate = 100.0 if checked == 0 else round(consistent * 100.0 / checked, 6)
    return ConsistencyReport(
        checked=checked, consistent=consistent, rate=rate, mismatches=tuple(mismatches)
    )


# ---------------------------------------------------------------------------
# 实验二：失锁 → 重连循环（每轮旧 writer 必须被拒）
# ---------------------------------------------------------------------------
def run_owner_churn_cycles(
    harness: Harness,
    binding: RunBinding | None = None,
    *,
    rounds: int = 50,
    fencing: bool = True,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
) -> ChurnReport:
    """``rounds`` 轮"锁连接被杀 → 新 host 接管 → 旧 writer 再写"的循环。

    每轮都做完整四步，缺一不算通过：

    1. 杀掉旧 host 的 lock 连接（writer 连接**故意保持存活**）；
    2. 新 host 用阻塞式 ``pg_advisory_lock`` 等锁——等待发生在 PG 服务端，
       客户端没有 sleep；
    3. 新 host CAS 推进 owner_epoch 接管；
    4. 旧 writer 尝试写（必须被拒）、新 writer 尝试写（必须成功）。
    """

    binding = binding or harness.create_run()
    records: list[CycleRecord] = []
    accepted = 0
    rejected = 0
    restore_checks = 0
    restore_mismatches: list[str] = []
    invocation = harness.new_invocation(binding, fencing=fencing)
    invocation.acquire_lock(lock_timeout_s)
    invocation.claim_owner(0)
    first = invocation.write({"round": 0, "writer": "bootstrap"})
    if not first.accepted:
        raise ProbeSetupError(f"首条 checkpoint 写入失败：{first.code} {first.detail}")
    accepted += 1
    expected_final = 1

    for index in range(1, rounds + 1):
        assert invocation.claim is not None  # noqa: S101 - 上一步刚刚成功
        epoch_before = invocation.claim.owner_epoch
        invocation_before = invocation.claim.invocation_id
        seq_before = int(invocation.last_seq or 0)
        invocation.kill_lock(harness)
        holders_after_kill = len(harness.advisory_lock_holders(binding))
        successor = harness.new_invocation(binding, fencing=fencing)
        lock_wait_ms = successor.acquire_lock(lock_timeout_s)
        successor.claim_owner(epoch_before)
        assert successor.claim is not None  # noqa: S101
        holders_after_acquire = len(harness.advisory_lock_holders(binding))
        stale = invocation.write({"round": index, "writer": "stale"})
        fresh = successor.write({"round": index, "writer": "fresh"})
        # "恢复后读到最新一致 checkpoint"：每轮都用新连接重读一次，不能靠内存里的变量
        restore = harness.restore_read(binding)
        restore_checks += 1
        if restore is None:
            restore_mismatches.append(f"round {index}: 恢复读不到任何 checkpoint")
        else:
            if restore.seq != (fresh.seq or -1):
                restore_mismatches.append(
                    f"round {index}: 恢复读到 seq {restore.seq}，最后接受的是 {fresh.seq}"
                )
            if restore.checkpoint_id != fresh.checkpoint_id:
                restore_mismatches.append(f"round {index}: checkpoint_id 不是最后写入的那条")
            if not restore.consistent:
                restore_mismatches.append(
                    f"round {index}: checkpoint owner {restore.invocation_id}"
                    f"/{restore.owner_epoch} != run owner "
                    f"{restore.run_owner_invocation_id}/{restore.run_owner_epoch}"
                )
        accepted += int(stale.accepted) + int(fresh.accepted)
        rejected += int(stale.rejected) + int(fresh.rejected)
        expected_final += int(stale.accepted) + int(fresh.accepted)
        invocation.close(state="fenced")
        records.append(
            CycleRecord(
                round=index,
                epoch_before=epoch_before,
                epoch_after=successor.claim.owner_epoch,
                invocation_before=invocation_before,
                invocation_after=successor.claim.invocation_id,
                seq_before=seq_before,
                stale_code=stale.code,
                fresh_code=fresh.code,
                fresh_seq=fresh.seq,
                lock_wait_ms=round(lock_wait_ms, 3),
                lock_holders_after_kill=holders_after_kill,
                lock_holders_after_acquire=holders_after_acquire,
            )
        )
        invocation = successor

    harness.assert_single_live_writer(binding)
    rows = harness.checkpoints(binding)
    report = ChurnReport(
        binding=binding,
        rounds=rounds,
        fencing=fencing,
        records=tuple(records),
        accepted=accepted,
        rejected=rejected,
        final_seq=rows[-1].seq if rows else 0,
        expected_final_seq=expected_final,
        final_checkpoint_id=rows[-1].checkpoint_id if rows else None,
        epoch_violations=harness.epoch_monotonic_violations(binding),
        sequence_violations=harness.sequence_violations(binding),
        restore_checks=restore_checks,
        restore_mismatches=tuple(restore_mismatches),
    )
    invocation.close()
    return report


# ---------------------------------------------------------------------------
# 实验三：两个 host 争抢
# ---------------------------------------------------------------------------
def run_two_writer_contention(
    harness: Harness,
    binding: RunBinding | None = None,
    *,
    contenders: int = 2,
    fencing: bool = True,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
) -> ContentionReport:
    """``contenders`` 个 host 同时抢同一把 advisory lock。

    阶段一：所有 host 并发 ``pg_try_advisory_lock``，恰好一个成功；
    阶段二：赢家 CAS 推进 epoch 并写一条 checkpoint；
    阶段三：主线程用一个**跳过锁直接 claim** 的 host 验证 epoch CAS 仍然拦住
    它（这条不能放进线程里，否则它会先抢到 CAS，把赢家的 claim 挤掉）。
    """

    binding = binding or harness.create_run()
    base_epoch = harness.epoch_of(binding)
    barrier = Barrier(contenders)
    lock_winner: list[int] = []
    lock_loser: list[int] = []
    lock_loser_codes: list[str] = []
    claim_winner: list[int] = []
    claim_loser_codes: list[str] = []
    write_codes: list[str] = []
    invocations: list[Invocation] = []
    errors: list[str] = []

    def contend(index: int) -> None:
        invocation = harness.new_invocation(binding, fencing=fencing)
        try:
            barrier.wait(timeout=lock_timeout_s)
            if invocation.try_acquire_lock():
                lock_winner.append(index)
                invocation.claim_owner(base_epoch)
                claim_winner.append(index)
                write_codes.append(invocation.write({"contender": index}).code)
            else:
                lock_loser.append(index)
                lock_loser_codes.append(CODE_LOCK_HELD)
        except OwnerClaimConflict as exc:
            claim_loser_codes.append(exc.code)
        except Exception as exc:  # noqa: BLE001 - 线程异常要带回去，不能静默
            errors.append(f"contender {index}: {type(exc).__name__}: {exc}")
        finally:
            invocations.append(invocation)

    threads = [
        Thread(target=contend, args=(index,), name=f"contender-{index}")
        for index in range(contenders)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=lock_timeout_s * 4)
    if any(thread.is_alive() for thread in threads):
        raise ProbeSetupError("争抢线程未在超时内结束")
    if errors:
        raise ProbeSetupError("; ".join(errors))
    # 阶段三：一个"跳过锁、直接拿过期 epoch 来抢"的 host。它必须被 epoch CAS 拒绝，
    # 否则"只要敢抢就能写"，单写者就只剩锁这一层、锁失效即裸奔。
    skip_lock = harness.new_invocation(binding, fencing=fencing)
    invocations.append(skip_lock)
    try:
        skip_lock.claim_owner(base_epoch)
        claim_winner.append(-999)
    except OwnerClaimConflict as exc:
        claim_loser_codes.append(exc.code)
    rows = harness.checkpoints(binding)
    report = ContentionReport(
        contenders=contenders,
        lock_winners=tuple(sorted(lock_winner)),
        lock_losers=tuple(sorted(lock_loser)),
        lock_loser_codes=tuple(lock_loser_codes),
        claim_winners=tuple(sorted(claim_winner)),
        claim_loser_codes=tuple(claim_loser_codes),
        write_codes=tuple(write_codes),
        final_epoch=harness.epoch_of(binding),
        checkpoint_count=len(rows),
        epoch_violations=harness.epoch_monotonic_violations(binding),
    )
    for invocation in invocations:
        invocation.close()
    return report


def run_epoch_race(
    harness: Harness,
    binding: RunBinding | None = None,
    *,
    contenders: int = 4,
    fencing: bool = True,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
) -> EpochRaceReport:
    """绕开 advisory lock，只让 ``owner_epoch`` 的 CAS 决定赢家。

    这一条回答"如果锁机制本身失效/被绕过，DB 里的 epoch 还能不能兜住单写者"。
    """

    binding = binding or harness.create_run()
    base_epoch = harness.epoch_of(binding)
    barrier = Barrier(contenders)
    claim_winner: list[int] = []
    claim_loser_codes: list[str] = []
    write_codes: list[str] = []
    invocations: list[Invocation] = []
    errors: list[str] = []

    def contend(index: int) -> None:
        invocation = harness.new_invocation(binding, fencing=fencing)
        try:
            barrier.wait(timeout=lock_timeout_s)
            invocation.claim_owner(base_epoch)
            claim_winner.append(index)
            write_codes.append(invocation.write({"contender": index}).code)
        except OwnerClaimConflict as exc:
            claim_loser_codes.append(exc.code)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"contender {index}: {type(exc).__name__}: {exc}")
        finally:
            invocations.append(invocation)

    threads = [
        Thread(target=contend, args=(index,), name=f"race-{index}") for index in range(contenders)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=lock_timeout_s * 4)
    if any(thread.is_alive() for thread in threads):
        raise ProbeSetupError("CAS 竞争线程未在超时内结束")
    if errors:
        raise ProbeSetupError("; ".join(errors))
    rows = harness.checkpoints(binding)
    report = EpochRaceReport(
        contenders=contenders,
        claim_winners=tuple(sorted(claim_winner)),
        claim_loser_codes=tuple(claim_loser_codes),
        write_codes=tuple(write_codes),
        final_epoch=harness.epoch_of(binding),
        checkpoint_count=len(rows),
        epoch_violations=harness.epoch_monotonic_violations(binding),
    )
    for invocation in invocations:
        invocation.close()
    return report


# ---------------------------------------------------------------------------
# 实验四：writer 连接断开后自动重连，仍然写不进去
# ---------------------------------------------------------------------------
def run_reconnect_after_writer_loss(
    harness: Harness,
    binding: RunBinding | None = None,
    *,
    fencing: bool = True,
) -> ReconnectReport:
    """旧 invocation 的 writer 连接被杀 → 自动重连 → 依然被 fence 拦住。"""

    binding = binding or harness.create_run()
    old = harness.new_invocation(binding, fencing=fencing)
    old.acquire_lock()
    old.claim_owner(0)
    first = old.write({"round": 0})
    if not first.accepted:
        raise ProbeSetupError(f"首条 checkpoint 写入失败：{first.code}")
    pid_before = old.writer_pid
    assert pid_before is not None  # noqa: S101
    harness.terminate_backend(pid_before)
    first_after_kill = old.write({"round": 1, "writer": "old-after-writer-kill"})
    old.kill_lock(harness)
    successor = harness.new_invocation(binding, fencing=fencing)
    successor.acquire_lock()
    successor.claim_owner(1)
    second = old.write({"round": 2, "writer": "old-after-reconnect"})
    pid_after = old.writer_pid
    rows = harness.checkpoints(binding)
    report = ReconnectReport(
        writer_pid_before=pid_before,
        writer_pid_after=pid_after,
        first_code=first_after_kill.code,
        second_code=second.code,
        stale_write_accepted=bool(first_after_kill.accepted or second.accepted),
        checkpoint_count=len(rows),
        epoch_violations=harness.epoch_monotonic_violations(binding),
    )
    old.close(state="fenced")
    successor.close()
    return report


# ---------------------------------------------------------------------------
# 实验五：原生 langgraph PostgresSaver（可选依赖，缺失时结构化 NOT_RUN）
# ---------------------------------------------------------------------------
_NATIVE_PACKAGES: Final = (
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-checkpoint-postgres",
    "psycopg",
    "psycopg-pool",
)


def probe_native_saver(harness: Harness, binding: RunBinding | None = None) -> NativeSaverReport:
    """测原生 ``PostgresSaver``：锁连接死了它还写不写、它自己断连会不会重连。"""

    steps: list[NativeStep] = []
    versions: dict[str, str] = {}
    try:
        from importlib.metadata import PackageNotFoundError, version

        for name in _NATIVE_PACKAGES:
            try:
                versions[name] = version(name)
            except PackageNotFoundError:
                versions[name] = "NOT_INSTALLED"
        import langgraph.checkpoint.postgres as native_postgres
    except Exception as exc:  # noqa: BLE001 - 依赖缺失就是 NOT_RUN，但要说清原因
        steps.append(NativeStep("import", "not_run", f"{type(exc).__name__}: {exc}"))
        return NativeSaverReport(
            available=False,
            versions=versions,
            steps=tuple(steps),
            note="未注入 langgraph-checkpoint-postgres；原生 saver 行为记 NOT_RUN，不推断",
        )

    binding = binding or harness.create_run()
    thread_id = str(binding.thread_id)
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    search_path_dsn = (
        f"{harness.dsn}{'&' if '?' in harness.dsn else '?'}options="
        f"{quote(f'-csearch_path={SCHEMA}', safe='')}"
    )
    saver = None
    saver_conn: psycopg.Connection[Any] | None = None
    lock_conn: psycopg.Connection[Any] | None = None
    try:
        saver_conn = psycopg.connect(search_path_dsn, autocommit=True)
        saver = native_postgres.PostgresSaver(saver_conn)
        saver.setup()
        steps.append(NativeStep("setup", "ok", f"native_pid={_backend_pid(saver_conn)}"))
        lock_conn = connect(harness.dsn, autocommit=True)
        lock_pid = _backend_pid(lock_conn)
        held = lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s::int, %s::int)", binding.lock_key()
        ).fetchone()
        steps.append(
            NativeStep(
                "lock_acquired",
                "ok",
                f"lock_pid={lock_pid} acquired={held[0]} "
                f"lock_is_not_saver_pid={lock_pid != _backend_pid(saver_conn)}",
            )
        )
        checkpoint = _native_checkpoint(step=0)
        metadata = {"source": "input", "step": 0, "writes": None, "parents": {}}
        saver.put(config, checkpoint, metadata, {})
        steps.append(NativeStep("put_with_lock_alive", "ok", "原生 saver 写入成功"))
        harness.terminate_backend(lock_pid)
        close_quietly(lock_conn)
        lock_conn = None
        steps.append(
            NativeStep(
                "lock_terminated",
                "ok",
                f"锁 backend {lock_pid} 已被 terminate；lock_holders="
                f"{len(harness.advisory_lock_holders(binding))}",
            )
        )
        second = _native_checkpoint(step=1)
        try:
            saver.put(config, second, metadata, {})
            steps.append(
                NativeStep(
                    "put_after_lock_loss",
                    "ok",
                    "锁连接已死、saver 连接仍活 → 原生写入仍然成功（C-03 缺口被证实）",
                )
            )
        except Exception as exc:  # noqa: BLE001
            steps.append(
                NativeStep("put_after_lock_loss", "error", f"{type(exc).__name__}: {exc}")
            )
        saver_pid = _backend_pid(saver_conn) if not saver_conn.closed else None
        if saver_pid is not None:
            harness.terminate_backend(saver_pid)
            steps.append(
                NativeStep("saver_killed", "ok", f"saver backend {saver_pid} 已 terminate")
            )
            try:
                saver.put(config, _native_checkpoint(step=2), metadata, {})
                steps.append(
                    NativeStep(
                        "put_after_saver_kill",
                        "ok",
                        "saver 连接被杀后仍能写入（说明库内部重连或复用了新连接）",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                steps.append(
                    NativeStep("put_after_saver_kill", "error", f"{type(exc).__name__}: {exc}")
                )
        # 原生层的"恢复读取"：全新连接 + 新 saver 实例，必须读到已提交的最新 checkpoint
        read_conn: psycopg.Connection[Any] | None = None
        try:
            read_conn = psycopg.connect(search_path_dsn, autocommit=True)
            reader = native_postgres.PostgresSaver(read_conn)
            found = reader.get_tuple(config)
            if found is None:
                steps.append(
                    NativeStep("get_after_loss", "error", "新 saver 实例读不到任何 checkpoint")
                )
            else:
                steps.append(
                    NativeStep(
                        "get_after_loss",
                        "ok",
                        f"新连接读到 checkpoint_id={found.checkpoint['id']} "
                        f"channel_values={list(found.checkpoint.get('channel_values', {}))}",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            steps.append(NativeStep("get_after_loss", "error", f"{type(exc).__name__}: {exc}"))
        finally:
            close_quietly(read_conn)
        return NativeSaverReport(
            available=True,
            versions=versions,
            steps=tuple(steps),
            note="原生结论只描述观测到的版本行为，不替代 C-03 的部署约束",
        )
    except Exception as exc:  # noqa: BLE001
        steps.append(NativeStep("native_probe", "error", f"{type(exc).__name__}: {exc}"))
        return NativeSaverReport(available=True, versions=versions, steps=tuple(steps))
    finally:
        for conn in (lock_conn, saver_conn):
            close_quietly(conn)


def _native_checkpoint(*, step: int) -> dict[str, Any]:
    stamp = datetime.now().astimezone().isoformat()
    return {
        "v": 1,
        "id": str(uuid4()),
        "ts": stamp,
        "channel_values": {"messages": [{"role": "user", "content": f"p0-t05 step {step}"}]},
        "channel_versions": {"messages": step},
        "versions_seen": {"__start__": {"messages": step}},
    }


def _backend_pid(conn: psycopg.Connection[Any]) -> int:
    return int(conn.execute("SELECT pg_backend_pid()").fetchone()[0])


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LabReport:
    binding: BindingReport
    churn_fenced: ChurnReport
    churn_control: ChurnReport
    contention: ContentionReport
    epoch_race: EpochRaceReport
    reconnect: ReconnectReport
    native: NativeSaverReport | None = None
    attempt_counts: dict[str, int] = field(default_factory=dict)


def run_baseline_lab(harness: Harness, *, rounds: int = 50) -> LabReport:
    harness.reset_schema()
    binding_report = run_binding_consistency(harness)
    churn_fenced = run_owner_churn_cycles(harness, rounds=rounds, fencing=True)
    churn_control = run_owner_churn_cycles(harness, rounds=rounds, fencing=False)
    contention = run_two_writer_contention(harness)
    epoch_race = run_epoch_race(harness)
    reconnect = run_reconnect_after_writer_loss(harness)
    return LabReport(
        binding=binding_report,
        churn_fenced=churn_fenced,
        churn_control=churn_control,
        contention=contention,
        epoch_race=epoch_race,
        reconnect=reconnect,
        attempt_counts=harness.attempt_counts(churn_fenced.binding),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0-T05 checkpoint 单写者探针")
    parser.add_argument("--rounds", type=int, default=50, help="失锁/重连循环轮数")
    parser.add_argument("--reset", action="store_true", help="先 DROP schema p0_t05")
    parser.add_argument("--native", action="store_true", help="只跑原生 saver 观测")
    parser.add_argument("--dsn", default=None)
    options = parser.parse_args(list(argv) if argv is not None else None)
    with closing(Harness(options.dsn)) as harness:
        if options.reset:
            harness.reset_schema()
        else:
            harness.ensure_schema()
        if options.native:
            harness.ensure_schema()
            print(dump_report(probe_native_saver(harness)))
            return 0
        report = run_baseline_lab(harness, rounds=options.rounds)
        payload = jsonable(report)
        payload["native"] = classification_snapshot(report)
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def _wait_stats(report: ChurnReport) -> dict[str, float]:
    waits = sorted(record.lock_wait_ms for record in report.records)
    if not waits:
        return {"min": 0.0, "median": 0.0, "max": 0.0}
    mid = waits[len(waits) // 2]
    return {"min": round(waits[0], 3), "median": round(mid, 3), "max": round(waits[-1], 3)}


def classification_snapshot(report: LabReport) -> dict[str, Any]:
    """给证据用的判据快照：每条验收点的机器可读结论。"""

    return {
        "single_writer": {
            "fenced_epoch_violations": len(report.churn_fenced.epoch_violations),
            "control_epoch_violations": len(report.churn_control.epoch_violations),
            "control_stale_writes_accepted": sum(
                1 for record in report.churn_control.records if record.stale_code == CODE_ACCEPTED
            ),
            "fenced_stale_writes_accepted": sum(
                1 for record in report.churn_fenced.records if record.stale_code == CODE_ACCEPTED
            ),
        },
        "binding_consistency": {
            "rate": report.binding.consistency.rate,
            "checked": report.binding.consistency.checked,
            "rejected_rebinds": report.binding.rebind_rejections,
        },
        "churn": {
            "rounds": report.churn_fenced.rounds,
            "accepted": report.churn_fenced.accepted,
            "rejected": report.churn_fenced.rejected,
            "final_seq": report.churn_fenced.final_seq,
            "expected_final_seq": report.churn_fenced.expected_final_seq,
            "sequence_violations": len(report.churn_fenced.sequence_violations),
            "restore_checks": report.churn_fenced.restore_checks,
            "restore_mismatches": list(report.churn_fenced.restore_mismatches),
            "lock_wait_ms": _wait_stats(report.churn_fenced),
        },
        "contention": {
            "lock_winners": list(report.contention.lock_winners),
            "lock_loser_codes": list(report.contention.lock_loser_codes),
            "claim_winners": list(report.contention.claim_winners),
            "claim_loser_codes": list(report.contention.claim_loser_codes),
            "write_codes": list(report.contention.write_codes),
            "checkpoint_count": report.contention.checkpoint_count,
        },
        "epoch_race": {
            "claim_winners": list(report.epoch_race.claim_winners),
            "claim_loser_codes": list(report.epoch_race.claim_loser_codes),
            "checkpoint_count": report.epoch_race.checkpoint_count,
            "epoch_violations": len(report.epoch_race.epoch_violations),
        },
        "reconnect": {
            "first_code": report.reconnect.first_code,
            "second_code": report.reconnect.second_code,
            "stale_write_accepted": report.reconnect.stale_write_accepted,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
