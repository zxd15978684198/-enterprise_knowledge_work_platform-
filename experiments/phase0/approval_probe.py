"""P0-T06：审批绑定、工具调用账本与未知写核查（真实 PostgreSQL + 真实 HTTP 下游）。

验收依据
--------
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T06 任务卡（lines 99–114）；
* ``docs/poc/07_Contract_Closure.md`` C-04（稳定工具意图、审批与外部提交，NEEDS_POC）；
* ``docs/tdd/02_Agent_Harness.md`` §3（副作用屏障）/§4（审批绑定与执行顺序）/§5（崩溃恢复）；
* ``docs/poc/05_Phase0_Acceptance.md`` 故障矩阵 F3（外部成功、receipt 前崩溃）、
  F4（批准后撤权/改参）、F5（节点重入/重复 resume）；
* ``docs/adr/0003_Approval_Side_Effects.md``：**提议**状态。本模块只产出实验证据，
  没有把任何 ADR 改成"已接受"，也没有宣称生产可用。

契约事实与命名映射（差异写在这里，不改 ADR/TDD）
------------------------------------------------
* **who / action / params / scope / expiry / idempotency_key 六项**（C-04 §4、TDD02 §4）落成
  ``phase0_approvals`` 的列：``requester_principal_id`` + ``approver_principal_id``（who）、
  ``name`` + ``tool_version``（action）、``arguments_hash``（params）、``scope_hash`` +
  ``resource_refs``（scope）、``expires_at``（有效期）、``idempotency_key``（稳定幂等键）。
* ``intent_key`` 的字段顺序严格按 C-04 §4；实现见 ``tool_ledger.intent_key``。
* 审批与 ToolCall 的 CAS **同一事务**（C-04 §6：approved→consumed 与 prepared→executing
  同事务），下游调用在事务外，receipt 与结果在**另一个**事务里（TDD02 §4.5）。
* ``unknown_outcome`` 的 ``downstream_sent`` 取值为 **True**，语义是"**可能已发出、
  无法证明未发出**"（TDD02 §5：无法证明未发送就按未知窗口处理）。迁移 002 §7 的 CHECK
  就是为此存在：崩溃窗口不允许被记成"零副作用"。

崩溃窗口（真实 ``os._exit``，不是"假装抛异常"）
-----------------------------------------------
``CRASH_WINDOWS`` 三档，对应任务卡"executing 提交后 / 下游成功后 receipt 前 / receipt 后"：

1. ``after_executing_commit``——CAS 已提交，请求还没发出；
2. ``after_downstream_success``——下游已成功，receipt 未提交；
3. ``after_receipt``——receipt 已提交，checkpoint（本实验没有 checkpoint）未写。

注入方式是**子进程真崩溃**：``python approval_probe.py --worker ... --crash-at X`` 在窗口处
``os._exit(9)``。父进程随后用**新连接**跑 ``reconcile``/``resume``，因此"恢复者看不到
崩溃进程的内存状态"是结构保证，而不是靠代码自觉。

隔离
----
schema 独占 ``p0_t06``（W4 资源槽），迁移编号 002 归本任务；下游用 T04 控制站的
``/idem/write``（支持按 key 去重）与 ``/nonidem/write``（不支持）两条真实 HTTP 路径，
因此 DoD 的"支持/不支持外部幂等两条路线"是两套真跑结果，不是推断。

禁止事项：不使用 mock 冒充下游；不新生成 key 掩盖重复；不把 ``failed`` 说成"没执行"。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import tool_ledger as ledger  # noqa: E402
from scope import read_dsn  # noqa: E402

#: 控制站（T04）只在真正发 HTTP / 起站点时才导入。
#: 崩溃窗口 worker 每个进程都要付一次 import 成本，实测 ``control_site`` 约 +0.42 s/进程；
#: 150 轮注入时这 60 秒不值得，所以做成惰性导入。行为完全一致（同一个客户端实现）。
_CONTROL_SITE: list[Any] = []


def _http_request(*args: Any, **kwargs: Any) -> Any:
    from control_site.client import request

    return request(*args, **kwargs)


def _control_site_class() -> Any:
    if not _CONTROL_SITE:
        from control_site import ControlSite

        _CONTROL_SITE.append(ControlSite)
    return _CONTROL_SITE[0]

__all__ = [
    "CRASH_WINDOWS",
    "EXECUTING_LEASE_S",
    "MIGRATION_ID",
    "SCHEMA",
    "SURFACES",
    "ApprovalBinding",
    "ApprovalError",
    "ApprovalExpiredError",
    "ApprovalMissingError",
    "ApprovalMismatchError",
    "ApprovalNotExecutableError",
    "ApprovalRevokedError",
    "Downstream",
    "ExecutionResult",
    "ForbiddenError",
    "IntentMismatchError",
    "ProbeSetupError",
    "RecoveryResult",
    "ScopeSpec",
    "ToolIntent",
    "apply_migration",
    "approve",
    "connect",
    "ensure_approval",
    "execute",
    "prepare_call",
    "reconcile",
    "register_principal",
    "register_workspace",
    "reset_schema",
    "resume",
    "revoke_approval",
    "revoke_principal",
]

SCHEMA: Final = "p0_t06"
MIGRATION_ID: Final = "002_approvals"
MIGRATION_PATH: Final = _HERE / "db" / "migrations" / "002_approvals.sql"

#: executing 租约：过期且无法证明未发出 → unknown_outcome。用 DB 的 now() 判定，不 sleep。
EXECUTING_LEASE_S: Final = 60.0
#: 崩溃窗口（真实 os._exit）。
CRASH_WINDOWS: Final[tuple[str, ...]] = (
    "after_executing_commit",
    "after_downstream_success",
    "after_receipt",
)
#: 下游能力面：idem 支持按 key 去重（可查单），nonidem 不支持（不可查单、不可重放）。
SURFACES: Final[tuple[str, str]] = ("idem", "nonidem")
DEFAULT_DOWNSTREAM_TIMEOUT_S: Final = 5.0


# ---------------------------------------------------------------------------
# 错误（稳定 code，与 tool_ledger.ERROR_CODES 对齐）
# ---------------------------------------------------------------------------
class ApprovalError(RuntimeError):
    code = "APPROVAL_ERROR"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = type(self).code
        self.detail = detail


class ProbeSetupError(ApprovalError):
    code = "PROBE_SETUP"


class ApprovalMissingError(ApprovalError):
    code = "APPROVAL_MISSING"


class ApprovalMismatchError(ApprovalError):
    code = "APPROVAL_MISMATCH"


class ApprovalExpiredError(ApprovalError):
    code = "APPROVAL_EXPIRED"


class ApprovalNotExecutableError(ApprovalError):
    """状态不允许执行（consumed/rejected/revoked/expired）。"""

    code = "APPROVAL_CONSUMED"


class ApprovalRevokedError(ApprovalError):
    code = "APPROVAL_REVOKED"


class ForbiddenError(ApprovalError):
    code = "FORBIDDEN"


class WorkspaceMismatchError(ApprovalError):
    code = "WORKSPACE_MISMATCH"


class IntentMismatchError(ApprovalError):
    code = "INTENT_KEY_MISMATCH"


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MigrationStamp:
    migration_id: str
    filename: str
    sha256: str
    size_bytes: int


def connect(*, dsn: str | None = None, autocommit: bool = True) -> psycopg.Connection[Any]:
    """运行期连接默认 ``autocommit=True``。

    **踩过的坑**：非 autocommit 连接上第一条语句就会隐式开启事务，之后 ``with
    conn.transaction()`` 只能退化成 savepoint；预检读出来的行锁会一直挂到进程退出，
    而且网络调用会被包进一个没有人提交的事务里。运行期一律 autocommit + 显式事务块，
    迁移另用 ``connect(autocommit=False)``。
    """

    return psycopg.connect(dsn or read_dsn(), autocommit=autocommit)


def _require_autocommit(conn: psycopg.Connection[Any]) -> None:
    if not conn.autocommit:
        raise ProbeSetupError(
            "运行期连接必须 autocommit=True（否则隐式事务会跨网络调用，见 connect() 注释）"
        )


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    """删除本任务 schema（清理用）；不碰 p0_t03/p0_t07/public。"""

    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def apply_migration(
    conn: psycopg.Connection[Any],
    *,
    path: Path = MIGRATION_PATH,
    migration_id: str = MIGRATION_ID,
) -> MigrationStamp:
    """单事务应用 002；台账写 ``p0_t06.phase0_schema_migrations``（与 003 同构）。"""

    if conn.autocommit:
        raise ProbeSetupError("apply_migration 需要 autocommit=False 的连接")
    if conn.info.transaction_status.name != "IDLE":
        raise ProbeSetupError("apply_migration 需要干净事务边界（IDLE）")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute(raw.decode("utf-8"))
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_schema_migrations (migration_id, filename, sha256) "
        "VALUES (%s, %s, %s)",
        (migration_id, path.name, digest),
    )
    return MigrationStamp(
        migration_id=migration_id, filename=path.name, sha256=digest, size_bytes=len(raw)
    )


def migration_sha256(path: Path = MIGRATION_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 最小工作区/主体替身
# ---------------------------------------------------------------------------
def register_workspace(conn: psycopg.Connection[Any], *, workspace_id: UUID, slug: str) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_workspaces (workspace_id, slug) VALUES (%s, %s)",
        (workspace_id, slug),
    )


def register_principal(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, principal_id: UUID, role: str
) -> None:
    if role not in ("requester", "approver", "member"):
        raise ProbeSetupError(f"未知 role {role!r}")
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_principals (workspace_id, principal_id, role) "
        "VALUES (%s, %s, %s)",
        (workspace_id, principal_id, role),
    )


def revoke_principal(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, principal_id: UUID
) -> None:
    """撤权：不删行，只置 revoked_at（执行前必须重读，不能信审批时快照）。"""

    conn.execute(
        f"UPDATE {SCHEMA}.phase0_principals SET revoked_at = now() "
        "WHERE workspace_id = %s AND principal_id = %s AND revoked_at IS NULL",
        (workspace_id, principal_id),
    )


def _principal_revoked(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, principal_id: UUID
) -> bool:
    row = conn.execute(
        f"SELECT revoked_at FROM {SCHEMA}.phase0_principals "
        "WHERE workspace_id = %s AND principal_id = %s",
        (workspace_id, principal_id),
    ).fetchone()
    if row is None:
        raise ForbiddenError(f"主体 {principal_id} 在 workspace {workspace_id} 不存在")
    return row[0] is not None


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScopeSpec:
    """授权范围（参与 scope_hash）：主体 + 操作 + 资源 + 授权范围。"""

    workspace_id: UUID
    principal_id: UUID
    operation: str
    resource_refs: tuple[tuple[str, str], ...] = ()
    grant_scope: str | None = None

    def digest(self) -> str:
        return ledger.scope_hash(
            workspace_id=self.workspace_id,
            principal_id=self.principal_id,
            operation=self.operation,
            resource_refs=list(self.resource_refs),
            grant_scope=self.grant_scope,
        )


@dataclass(frozen=True, slots=True)
class ToolIntent:
    """一条持久化的工具意图（``phase0_tool_calls`` 的只读视图）。"""

    workspace_id: UUID
    tool_call_id: UUID
    run_id: UUID
    response_artifact_id: UUID
    call_ordinal: int
    name: str
    tool_version: str
    arguments_hash: str
    scope_hash: str
    canonicalization_version: str
    intent_key: str


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    """审批行的只读快照；校验时逐项比对，缺一即拒绝。"""

    workspace_id: UUID
    approval_id: UUID
    run_id: UUID
    tool_call_id: UUID
    requester_principal_id: UUID
    approver_principal_id: UUID
    name: str
    tool_version: str
    arguments_hash: str
    scope_hash: str
    idempotency_key: str
    expires_at: datetime
    status: str
    consumed_at: datetime | None
    revoked_at: datetime | None
    row_version: int


@dataclass(frozen=True, slots=True)
class DownstreamResult:
    ok: bool
    status: int | None
    payload: dict[str, Any]
    elapsed_ms: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    outcome: str
    replayed: bool
    receipt: ledger.Receipt | None
    downstream: DownstreamResult | None
    error_code: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    state: str
    receipt: ledger.Receipt | None
    rewrote_receipt: bool
    queried_downstream: bool
    detail: str = ""


# ---------------------------------------------------------------------------
# 下游：T04 控制站（真实 HTTP），两条能力面
# ---------------------------------------------------------------------------
class Downstream:
    """下游连接器替身：``idem`` 支持按 key 去重 + 可查单；``nonidem`` 两者皆无。"""

    def __init__(self, base_url: str, *, timeout: float = DEFAULT_DOWNSTREAM_TIMEOUT_S) -> None:
        if not base_url.startswith("http://127.0.0.1:"):
            raise ProbeSetupError("下游只允许本机 127.0.0.1（实验栈不对外网暴露）")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, *, surface: str, key: str) -> DownstreamResult:
        if surface not in SURFACES:
            raise ProbeSetupError(f"未知 surface {surface!r}")
        path = "/idem/write" if surface == "idem" else "/nonidem/write"
        return self._post(path, key)

    def query(self, *, surface: str, key: str) -> dict[str, Any] | None:
        if surface not in SURFACES:
            raise ProbeSetupError(f"未知 surface {surface!r}")
        path = "/idem/count" if surface == "idem" else "/nonidem/count"
        result = _http_request(
            "GET", f"{self.base_url}{path}?key={key}", timeout=self.timeout
        )
        if not result.ok:
            return None
        try:
            return dict(json.loads(result.body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _post(self, path: str, key: str) -> DownstreamResult:
        started = time.perf_counter()
        result = _http_request(
            "POST", f"{self.base_url}{path}", body={"key": key}, timeout=self.timeout
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        payload: dict[str, Any] = {}
        if result.body:
            try:
                payload = dict(json.loads(result.body.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
        return DownstreamResult(
            ok=result.ok,
            status=result.status,
            payload=payload,
            elapsed_ms=elapsed,
            error=result.error,
        )


def counts_for(downstream: Downstream, *, surface: str, key: str) -> dict[str, Any]:
    """下游侧计数：``applied_calls`` 就是"真实副作用次数"（服务端记账，不信客户端）。"""

    return downstream.query(surface=surface, key=key) or {}


# ---------------------------------------------------------------------------
# 意图登记（C-04 §4：响应先持久化，重入读回同一个 artifact）
# ---------------------------------------------------------------------------
def persist_response(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    model_step_key: str,
    response_digest: str,
    response_artifact_id: UUID | None = None,
    schema_version: int = 1,
) -> UUID:
    """登记模型响应 artifact；同一 ``(run, model_step_key)`` 重复登记返回原 id。"""

    existing = conn.execute(
        f"SELECT response_artifact_id FROM {SCHEMA}.phase0_response_artifacts "
        "WHERE workspace_id = %s AND run_id = %s AND model_step_key = %s",
        (workspace_id, run_id, model_step_key),
    ).fetchone()
    if existing is not None:
        return existing[0]
    artifact_id = response_artifact_id or uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_response_artifacts "
        "(workspace_id, response_artifact_id, run_id, model_step_key, schema_version, "
        " response_digest) VALUES (%s, %s, %s, %s, %s, %s)",
        (workspace_id, artifact_id, run_id, model_step_key, schema_version, response_digest),
    )
    return artifact_id


def prepare_call(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    run_id: UUID,
    response_artifact_id: UUID,
    call_ordinal: int,
    name: str,
    tool_version: str,
    arguments: Mapping[str, Any],
    scope: ScopeSpec,
    defaults: Mapping[str, Any] | None = None,
) -> ToolIntent:
    """幂等登记工具意图。

    * 同一 ``intent_key`` 重复 ``prepare`` → 返回原行（节点可安全重入）；
    * 同一 ``(artifact, ordinal)`` 但 ``intent_key`` 不同 → ``INTENT_KEY_MISMATCH``
      （这是"换参绕过意图唯一键"的攻击面，C-04 §7 明确禁止）。
    """

    if scope.workspace_id != workspace_id:
        raise WorkspaceMismatchError("scope.workspace_id 与入参 workspace 不一致")
    args_hash = ledger.arguments_hash(arguments, defaults=defaults)
    scope_digest = scope.digest()
    key = ledger.intent_key(
        run_id=run_id,
        response_artifact_id=response_artifact_id,
        call_ordinal=call_ordinal,
        name=name,
        tool_version=tool_version,
        arguments_hash=args_hash,
        scope_hash=scope_digest,
    )
    try:
        with conn.transaction():
            existing = conn.execute(
                f"SELECT tool_call_id, intent_key FROM {SCHEMA}.phase0_tool_calls "
                "WHERE workspace_id = %s AND run_id = %s AND intent_key = %s",
                (workspace_id, run_id, key),
            ).fetchone()
            if existing is not None:
                return _load_intent(conn, workspace_id=workspace_id, tool_call_id=existing[0])

            ordinal_row = conn.execute(
                f"SELECT tool_call_id, intent_key FROM {SCHEMA}.phase0_tool_calls "
                "WHERE workspace_id = %s AND run_id = %s AND response_artifact_id = %s "
                "AND call_ordinal = %s",
                (workspace_id, run_id, response_artifact_id, call_ordinal),
            ).fetchone()
            if ordinal_row is not None:
                raise IntentMismatchError(
                    f"ordinal {call_ordinal} 已有不同意图（{ordinal_row[1]} != {key}）："
                    "换参必须新建意图/审批，禁止复用旧 ordinal",
                    detail=ordinal_row[1],
                )

            tool_call_id = uuid4()
            conn.execute(
                f"INSERT INTO {SCHEMA}.phase0_tool_calls "
                "(workspace_id, tool_call_id, run_id, response_artifact_id, call_ordinal, "
                " name, tool_version, arguments_hash, scope_hash, canonicalization_version, "
                " intent_key, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'prepared')",
                (
                    workspace_id,
                    tool_call_id,
                    run_id,
                    response_artifact_id,
                    call_ordinal,
                    name,
                    tool_version,
                    args_hash,
                    scope_digest,
                    ledger.CANONICALIZATION_VERSION,
                    key,
                ),
            )
    except psycopg.errors.UniqueViolation as exc:  # 并发 prepare 的兜底
        raise IntentMismatchError(f"意图登记唯一键冲突：{exc}") from exc
    return _load_intent(conn, workspace_id=workspace_id, tool_call_id=tool_call_id)


def _load_intent(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, tool_call_id: UUID
) -> ToolIntent:
    row = conn.execute(
        f"SELECT tool_call_id, run_id, response_artifact_id, call_ordinal, name, tool_version, "
        "arguments_hash, scope_hash, canonicalization_version, intent_key "
        f"FROM {SCHEMA}.phase0_tool_calls WHERE workspace_id = %s AND tool_call_id = %s",
        (workspace_id, tool_call_id),
    ).fetchone()
    if row is None:
        raise ApprovalMissingError(f"tool_call {tool_call_id} 不存在")
    return ToolIntent(
        workspace_id=workspace_id,
        tool_call_id=row[0],
        run_id=row[1],
        response_artifact_id=row[2],
        call_ordinal=row[3],
        name=row[4],
        tool_version=row[5],
        arguments_hash=row[6],
        scope_hash=row[7],
        canonicalization_version=row[8],
        intent_key=row[9],
    )


# ---------------------------------------------------------------------------
# 审批（ApprovalManager 的唯一写入面）
# ---------------------------------------------------------------------------
_APPROVAL_COLUMNS: Final = (
    "workspace_id, approval_id, run_id, tool_call_id, requester_principal_id, "
    "approver_principal_id, name, tool_version, arguments_hash, scope_hash, "
    "idempotency_key, expires_at, status, consumed_at, revoked_at, row_version"
)


def _approval_from_row(row: Sequence[Any]) -> ApprovalBinding:
    return ApprovalBinding(
        workspace_id=row[0],
        approval_id=row[1],
        run_id=row[2],
        tool_call_id=row[3],
        requester_principal_id=row[4],
        approver_principal_id=row[5],
        name=row[6],
        tool_version=row[7],
        arguments_hash=row[8],
        scope_hash=row[9],
        idempotency_key=row[10],
        expires_at=row[11],
        status=row[12],
        consumed_at=row[13],
        revoked_at=row[14],
        row_version=row[15],
    )


def get_approval(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, approval_id: UUID
) -> ApprovalBinding | None:
    row = conn.execute(
        f"SELECT {_APPROVAL_COLUMNS} FROM {SCHEMA}.phase0_approvals "
        "WHERE workspace_id = %s AND approval_id = %s",
        (workspace_id, approval_id),
    ).fetchone()
    return None if row is None else _approval_from_row(row)


def ensure_approval(
    conn: psycopg.Connection[Any],
    intent: ToolIntent,
    *,
    requester_principal_id: UUID,
    approver_principal_id: UUID,
    expires_in_s: float = 600.0,
    policy_snapshot_id: UUID | None = None,
    policy_version: str | None = None,
    resource_refs: Sequence[tuple[str, str] | Mapping[str, Any]] = (),
) -> UUID:
    """登记（或复用）一条有效审批；pending/approved 期间同绑定只有一条（TDD02 §4.1）。"""

    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in_s)
    with conn.transaction():
        existing = conn.execute(
            f"SELECT approval_id FROM {SCHEMA}.phase0_approvals "
            "WHERE workspace_id = %s AND tool_call_id = %s AND arguments_hash = %s "
            "AND scope_hash = %s AND tool_version = %s AND status IN ('pending', 'approved')",
            (
                intent.workspace_id,
                intent.tool_call_id,
                intent.arguments_hash,
                intent.scope_hash,
                intent.tool_version,
            ),
        ).fetchone()
        if existing is not None:
            _record_event(
                conn,
                workspace_id=intent.workspace_id,
                approval_id=existing[0],
                actor=requester_principal_id,
                action="created",
                reason_code="REUSED_EXISTING",
            )
            return existing[0]

        approval_id = uuid4()
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_approvals "
            "(workspace_id, approval_id, run_id, tool_call_id, requester_principal_id, "
            " approver_principal_id, name, tool_version, arguments_hash, scope_hash, "
            " resource_refs, policy_snapshot_id, policy_version, idempotency_key, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                intent.workspace_id,
                approval_id,
                intent.run_id,
                intent.tool_call_id,
                requester_principal_id,
                approver_principal_id,
                intent.name,
                intent.tool_version,
                intent.arguments_hash,
                intent.scope_hash,
                Jsonb([dict(item) for item in _refs(resource_refs)]),
                policy_snapshot_id,
                policy_version,
                ledger.external_idempotency_key(
                    workspace_id=intent.workspace_id, tool_call_id=intent.tool_call_id
                ),
                expires_at,
            ),
        )
        _record_event(
            conn,
            workspace_id=intent.workspace_id,
            approval_id=approval_id,
            actor=requester_principal_id,
            action="created",
            reason_code=None,
        )
        _set_tool_call_status(
            conn, workspace_id=intent.workspace_id, tool_call_id=intent.tool_call_id,
            new_status="awaiting_approval",
        )
    return approval_id


def _refs(
    resource_refs: Sequence[tuple[str, str] | Mapping[str, Any]]
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for ref in resource_refs:
        if isinstance(ref, Mapping):
            items.append({"kind": str(ref.get("kind")), "id": str(ref.get("id"))})
        else:
            kind, rid = ref
            items.append({"kind": str(kind), "id": str(rid)})
    return items


def _record_event(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    approval_id: UUID,
    actor: UUID | None,
    action: str,
    reason_code: str | None,
) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_approval_events "
        "(workspace_id, approval_id, actor_principal_id, action, reason_code) "
        "VALUES (%s, %s, %s, %s, %s)",
        (workspace_id, approval_id, actor, action, reason_code),
    )


def _set_tool_call_status(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    tool_call_id: UUID,
    new_status: str,
) -> None:
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_tool_calls SET status = %s, "
        # 离开 executing 必须清空租约：002 §4 的 CHECK 要求
        # (status='executing') = (owner/lease 均非空)。
        "executing_owner = CASE WHEN %s = 'executing' THEN executing_owner ELSE NULL END, "
        "executing_lease_expires_at = CASE WHEN %s = 'executing' "
        "THEN executing_lease_expires_at ELSE NULL END, "
        "updated_at = now(), row_version = row_version + 1 "
        "WHERE workspace_id = %s AND tool_call_id = %s AND status <> %s",
        (new_status, new_status, new_status, workspace_id, tool_call_id, new_status),
    )


def _transition_approval(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    approval_id: UUID,
    from_status: str,
    to_status: str,
    actor: UUID | None,
    reason_code: str | None = None,
) -> bool:
    """CAS 审批状态；``rowcount=0`` 说明状态已被别人改过（返回 False，绝不覆盖）。"""

    extra = ""
    if to_status == "consumed":
        extra = ", consumed_at = now()"
    elif to_status == "revoked":
        extra = ", revoked_at = now()"
    elif to_status in ("approved", "rejected"):
        extra = ", decided_at = now()"
    cursor = conn.execute(
        f"UPDATE {SCHEMA}.phase0_approvals SET status = %s{extra}, updated_at = now(), "
        "row_version = row_version + 1 "
        "WHERE workspace_id = %s AND approval_id = %s AND status = %s",
        (to_status, workspace_id, approval_id, from_status),
    )
    if cursor.rowcount != 1:
        return False
    _record_event(
        conn,
        workspace_id=workspace_id,
        approval_id=approval_id,
        actor=actor,
        action=to_status,
        reason_code=reason_code,
    )
    return True


def approve(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, approval_id: UUID, actor: UUID
) -> None:
    """pending → approved（薄测试 API）。批准本身不执行工具（C-04 §6）。"""

    with conn.transaction():
        if not _transition_approval(
            conn,
            workspace_id=workspace_id,
            approval_id=approval_id,
            from_status="pending",
            to_status="approved",
            actor=actor,
        ):
            raise ApprovalNotExecutableError(f"approval {approval_id} 不是 pending，无法批准")


def reject(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, approval_id: UUID, actor: UUID
) -> None:
    with conn.transaction():
        if not _transition_approval(
            conn,
            workspace_id=workspace_id,
            approval_id=approval_id,
            from_status="pending",
            to_status="rejected",
            actor=actor,
            reason_code="USER_REJECTED",
        ):
            raise ApprovalNotExecutableError(f"approval {approval_id} 不是 pending，无法拒绝")


def revoke_approval(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    approval_id: UUID,
    actor: UUID | None = None,
) -> None:
    """撤销：pending/approved → revoked。已 consumed 的不可撤（终态）。"""

    with conn.transaction():
        for from_status in ("pending", "approved"):
            if _transition_approval(
                conn,
                workspace_id=workspace_id,
                approval_id=approval_id,
                from_status=from_status,
                to_status="revoked",
                actor=actor,
                reason_code="MANUAL_REVOKE",
            ):
                return
    raise ApprovalNotExecutableError(f"approval {approval_id} 已终态，无法撤销")


def expire_if_due(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, approval_id: UUID
) -> bool:
    """把到期的 approved/pending 审批 CAS 成 expired；返回是否真的改了一条。"""

    with conn.transaction():
        for from_status in ("pending", "approved"):
            cursor = conn.execute(
                f"UPDATE {SCHEMA}.phase0_approvals SET status = 'expired', updated_at = now(), "
                "row_version = row_version + 1 "
                "WHERE workspace_id = %s AND approval_id = %s AND status = %s "
                "AND expires_at <= now()",
                (workspace_id, approval_id, from_status),
            )
            if cursor.rowcount == 1:
                _record_event(
                    conn,
                    workspace_id=workspace_id,
                    approval_id=approval_id,
                    actor=None,
                    action="expired",
                    reason_code="EXPIRES_AT_PASSED",
                )
                return True
    return False


def require_approval(
    conn: psycopg.Connection[Any],
    *,
    approval_id: UUID,
    intent: ToolIntent,
    requester_principal_id: UUID,
    approver_principal_id: UUID,
    now: datetime | None = None,
) -> ApprovalBinding:
    """执行前的逐项校验（C-04 §5）。任何一项不符都抛错，且**零下游调用**。

    校验顺序刻意固定，便于证据里逐项引用：
    workspace → 存在性 → 绑定身份 → 状态 → who → 有效期 → params/scope/版本。
    """

    approval = get_approval(
        conn, workspace_id=intent.workspace_id, approval_id=approval_id
    )
    if approval is None:
        # 分清"根本没有这条审批"与"审批在别的 workspace"：后者是越权尝试，必须单独报。
        other = conn.execute(
            f"SELECT workspace_id FROM {SCHEMA}.phase0_approvals WHERE approval_id = %s",
            (approval_id,),
        ).fetchone()
        if other is not None:
            raise WorkspaceMismatchError(
                f"approval {approval_id} 属于 workspace {other[0]}，"
                f"不能用于 workspace {intent.workspace_id}"
            )
        raise ApprovalMissingError(f"approval {approval_id} 在 workspace 内不存在")
    if approval.tool_call_id != intent.tool_call_id:
        raise ApprovalMismatchError(
            f"审批绑定 tool_call {approval.tool_call_id}，本次意图是 {intent.tool_call_id}"
        )
    if approval.requester_principal_id != requester_principal_id:
        raise ForbiddenError(
            f"请求者不匹配：审批登记 {approval.requester_principal_id}，"
            f"本次 {requester_principal_id}"
        )
    if approval.approver_principal_id != approver_principal_id:
        raise ForbiddenError(
            f"审批者不匹配：审批登记 {approval.approver_principal_id}，本次 {approver_principal_id}"
        )
    if approval.status == "revoked":
        raise ApprovalRevokedError(f"approval {approval_id} 已被撤销")
    if approval.status == "expired":
        raise ApprovalExpiredError(f"approval {approval_id} 已过期（状态已置 expired）")
    if approval.status in ("consumed", "rejected"):
        raise ApprovalNotExecutableError(
            f"approval 状态 {approval.status} 不可执行", detail=approval.status
        )
    if _principal_revoked(
        conn, workspace_id=intent.workspace_id, principal_id=requester_principal_id
    ):
        raise ForbiddenError(f"请求者 {requester_principal_id} 已被撤权")
    if _principal_revoked(
        conn, workspace_id=intent.workspace_id, principal_id=approver_principal_id
    ):
        raise ForbiddenError(f"审批者 {approver_principal_id} 已被撤权")

    # 生产取 DB now()；实验注入未来时刻，避免用 sleep 等过期（比较逻辑完全相同）。
    current = now if now is not None else conn.execute("SELECT now()").fetchone()[0]
    if approval.expires_at <= current:
        raise ApprovalExpiredError(f"approval 已于 {approval.expires_at.isoformat()} 过期")
    if approval.status != "approved":
        raise ApprovalNotExecutableError(
            f"approval 状态 {approval.status} 不是 approved（批准后才能执行）",
            detail=approval.status,
        )
    if approval.name != intent.name or approval.tool_version != intent.tool_version:
        raise ApprovalMismatchError(
            f"action 不匹配：审批 {approval.name}@{approval.tool_version}，"
            f"本次 {intent.name}@{intent.tool_version}"
        )
    if approval.arguments_hash != intent.arguments_hash:
        raise ApprovalMismatchError(
            "params hash 不匹配（参数被改）：审批 "
            f"{approval.arguments_hash[:12]}… vs 意图 {intent.arguments_hash[:12]}…"
        )
    if approval.scope_hash != intent.scope_hash:
        raise ApprovalMismatchError(
            "scope hash 不匹配（作用域被改）：审批 "
            f"{approval.scope_hash[:12]}… vs 意图 {intent.scope_hash[:12]}…"
        )
    expected_idem_key = ledger.external_idempotency_key(
        workspace_id=intent.workspace_id, tool_call_id=intent.tool_call_id
    )
    if approval.idempotency_key != expected_idem_key:
        raise ApprovalMismatchError(
            f"幂等键不匹配：审批 {approval.idempotency_key!r} != 派生 {expected_idem_key!r}"
        )
    return approval


# ---------------------------------------------------------------------------
# 执行（ToolExecutor 的唯一写入面）
# ---------------------------------------------------------------------------
def _now_ms() -> float:
    return time.perf_counter()


def _die() -> None:
    """真实硬崩溃：不跑 finally、不回滚、不写日志（模拟进程被杀）。"""

    os._exit(9)


def execute(
    conn: psycopg.Connection[Any],
    *,
    downstream: Downstream,
    approval_id: UUID,
    intent: ToolIntent,
    requester_principal_id: UUID,
    approver_principal_id: UUID,
    surface: str = "idem",
    lease_seconds: float = EXECUTING_LEASE_S,
    crash_at: str | None = None,
    crash_hook: Any = None,
    now: datetime | None = None,
) -> ExecutionResult:
    """执行一次工具调用：预检 → CAS 消费+executing → 事务外调用 → receipt。

    ``crash_at`` 只应由 worker 子进程使用；测试要模拟崩溃时传 ``crash_hook``
    （默认仍是 ``os._exit``），从而在不杀测试进程的前提下命中同一代码路径。
    """

    if crash_at is not None and crash_at not in CRASH_WINDOWS:
        raise ProbeSetupError(f"未知崩溃窗口 {crash_at!r}（允许 {CRASH_WINDOWS}）")
    killer = crash_hook or _die
    workspace_id = intent.workspace_id

    # 0) 已有成功 receipt → 直接复用（C-04 §8：succeeded 读原结果，不重新执行）
    existing = ledger.has_succeeded_receipt(
        conn, workspace_id=workspace_id, intent_key_value=intent.intent_key
    )
    if existing is not None:
        return ExecutionResult(
            outcome="replayed",
            replayed=True,
            receipt=existing,
            downstream=None,
            detail="稳定 intent_key 已命中 succeeded receipt",
        )

    # 1) 逐项预检（零下游调用）
    attempt_no = _next_attempt(conn, intent)
    try:
        require_approval(
            conn,
            approval_id=approval_id,
            intent=intent,
            requester_principal_id=requester_principal_id,
            approver_principal_id=approver_principal_id,
            now=now,
        )
    except ApprovalError as exc:
        receipt = _record_rejection(
            conn,
            intent=intent,
            approval_id=approval_id,
            error_code=_license_code(exc),
            attempt_no=attempt_no,
        )
        return ExecutionResult(
            outcome="rejected",
            replayed=False,
            receipt=receipt,
            downstream=None,
            error_code=_license_code(exc),
            detail=str(exc),
        )

    # 2) CAS：approved→consumed 与 prepared/awaiting→executing 同一事务（C-04 §6）
    with conn.transaction():
        if not _transition_approval(
            conn,
            workspace_id=workspace_id,
            approval_id=approval_id,
            from_status="approved",
            to_status="consumed",
            actor=approver_principal_id,
        ):
            raise ApprovalNotExecutableError(
                f"approval {approval_id} 消费 CAS 失败（并发执行或状态已变）"
            )
        row = conn.execute(
            f"UPDATE {SCHEMA}.phase0_tool_calls SET status = 'executing', "
            "executing_owner = %s, "
            "executing_lease_expires_at = now() + make_interval(secs => %s), "
            "attempt_count = attempt_count + 1, updated_at = now(), "
            "row_version = row_version + 1 "
            "WHERE workspace_id = %s AND tool_call_id = %s "
            "AND status IN ('prepared', 'awaiting_approval') "
            "RETURNING attempt_count",
            (f"t06-exec-{os.getpid()}", lease_seconds, workspace_id, intent.tool_call_id),
        ).fetchone()
        if row is None:
            raise ApprovalNotExecutableError(
                f"tool_call {intent.tool_call_id} 不在可执行状态（可能已在 executing）"
            )
    if crash_at == "after_executing_commit":
        killer()

    # 3) 事务外调用下游（DB 锁内不做网络 I/O，TDD01 §6.5）
    external_key = ledger.external_idempotency_key(
        workspace_id=workspace_id, tool_call_id=intent.tool_call_id
    )
    started = _now_ms()
    result = downstream.call(surface=surface, key=external_key)
    duration_ms = int((_now_ms() - started) * 1000)
    if crash_at == "after_downstream_success":
        killer()

    # 4) receipt + 结果同一事务（TDD02 §4.5）
    outcome, error_code = _classify(result)
    summary = ledger.summarize_result(result.payload)
    with conn.transaction():
        ledger.append_receipt(
            conn,
            ledger.Receipt(
                workspace_id=workspace_id,
                tool_call_id=intent.tool_call_id,
                approval_id=approval_id,
                intent_key=intent.intent_key,
                attempt_no=attempt_no,
                arguments_hash=intent.arguments_hash,
                scope_hash=intent.scope_hash,
                tool_version=intent.tool_version,
                outcome=outcome,
                downstream_sent=True,
                external_idempotency_key=external_key,
                result_digest=ledger.result_digest(result.payload) if outcome != "failed" else None,
                result_summary=summary,
                duration_ms=duration_ms,
                error_code=error_code,
            ),
        )
        _set_tool_call_status(
            conn,
            workspace_id=workspace_id,
            tool_call_id=intent.tool_call_id,
            new_status="succeeded" if outcome == "succeeded" else outcome,
        )
    if crash_at == "after_receipt":
        killer()
    receipt = ledger.latest_receipt(
        conn, workspace_id=workspace_id, tool_call_id=intent.tool_call_id
    )
    return ExecutionResult(
        outcome=outcome,
        replayed=False,
        receipt=receipt,
        downstream=result,
        error_code=error_code,
        detail="" if result.ok else (result.error or f"status={result.status}"),
    )


def _classify(result: DownstreamResult) -> tuple[str, str | None]:
    if not result.ok:
        return "failed", "DOWNSTREAM_FAILED"
    applied = result.payload.get("applied")
    if applied is True:
        return "succeeded", None
    # 幂等面返回 applied=False 说明"这次没有产生新副作用"，但调用本身成功。
    return "succeeded", None


def _license_code(exc: ApprovalError) -> str:
    code = getattr(exc, "code", "APPROVAL_ERROR")
    return code if code in ledger.ERROR_CODES else "APPROVAL_MISMATCH"


def _next_attempt(conn: psycopg.Connection[Any], intent: ToolIntent) -> int:
    row = conn.execute(
        f"SELECT coalesce(max(attempt_no), -1) FROM {SCHEMA}.phase0_tool_receipts "
        "WHERE workspace_id = %s AND tool_call_id = %s",
        (intent.workspace_id, intent.tool_call_id),
    ).fetchone()
    return int(row[0]) + 1


def _record_rejection(
    conn: psycopg.Connection[Any],
    *,
    intent: ToolIntent,
    approval_id: UUID,
    error_code: str,
    attempt_no: int,
) -> ledger.Receipt:
    """拒绝也要留账：``downstream_sent=False`` 才能说"零副作用"。"""

    with conn.transaction():
        return ledger.append_receipt(
            conn,
            ledger.Receipt(
                workspace_id=intent.workspace_id,
                tool_call_id=intent.tool_call_id,
                approval_id=approval_id,
                intent_key=intent.intent_key,
                attempt_no=attempt_no,
                arguments_hash=intent.arguments_hash,
                scope_hash=intent.scope_hash,
                tool_version=intent.tool_version,
                outcome="rejected",
                downstream_sent=False,
                result_summary={"reason": error_code},
                duration_ms=0,
                error_code=error_code,
            ),
        )


# ---------------------------------------------------------------------------
# 恢复（Reconciler / 重复 resume）
# ---------------------------------------------------------------------------
def reconcile(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    tool_call_id: UUID,
    downstream: Downstream | None = None,
    surface: str = "idem",
    can_query_downstream: bool = True,
) -> RecoveryResult:
    """崩溃后的对账：**只读账本 + 只读下游**，绝不自动重放写入。

    判定顺序（TDD02 §5）：
    1. 已有 succeeded/replayed receipt → 复用，不重发；
    2. tool_call 仍 executing 且租约未过 → 什么都不做（另一个执行者还活着）；
    3. 租约已过：不能证明未发出 → ``unknown_outcome``；能查单且下游确认已应用 →
       补记一条 succeeded receipt（**不重新发请求**）。
    """

    row = conn.execute(
        f"SELECT status, executing_lease_expires_at, intent_key, arguments_hash, scope_hash, "
        "tool_version, attempt_count "
        f"FROM {SCHEMA}.phase0_tool_calls WHERE workspace_id = %s AND tool_call_id = %s",
        (workspace_id, tool_call_id),
    ).fetchone()
    if row is None:
        raise ApprovalMissingError(f"tool_call {tool_call_id} 不存在")
    (
        status,
        lease_expires_at,
        intent_key_value,
        args_hash,
        scope_digest,
        tool_version,
        attempts,
    ) = row

    existing = ledger.latest_receipt(
        conn, workspace_id=workspace_id, tool_call_id=tool_call_id
    )
    if existing is not None and existing.outcome in ("succeeded", "replayed"):
        return RecoveryResult(
            state="succeeded",
            receipt=existing,
            rewrote_receipt=False,
            queried_downstream=False,
            detail="已有 succeeded receipt，复用原结果",
        )

    if status != "executing":
        return RecoveryResult(
            state=status,
            receipt=existing,
            rewrote_receipt=False,
            queried_downstream=False,
            detail=f"没有未知窗口（状态 {status}）",
        )

    expired = conn.execute(
        "SELECT %s <= now()", (lease_expires_at,)
    ).fetchone()[0]
    if not expired:
        return RecoveryResult(
            state="executing_lease_active",
            receipt=existing,
            rewrote_receipt=False,
            queried_downstream=False,
            detail="租约未过期：另一个执行者可能还活着，不接管",
        )

    external_key = ledger.external_idempotency_key(
        workspace_id=workspace_id, tool_call_id=tool_call_id
    )
    queried = False
    if downstream is not None and can_query_downstream and surface == "idem":
        counts = counts_for(downstream, surface=surface, key=external_key)
        queried = True
        if int(counts.get("applied_calls", 0)) >= 1:
            with conn.transaction():
                receipt = ledger.append_receipt(
                    conn,
                    ledger.Receipt(
                        workspace_id=workspace_id,
                        tool_call_id=tool_call_id,
                        intent_key=intent_key_value,
                        attempt_no=int(attempts) + 1,
                        arguments_hash=args_hash,
                        scope_hash=scope_digest,
                        tool_version=tool_version,
                        outcome="succeeded",
                        downstream_sent=True,
                        external_idempotency_key=external_key,
                        result_digest=ledger.result_digest(
                            {
                                "source": "reconcile_query",
                                "applied_calls": counts.get("applied_calls"),
                            }
                        ),
                        result_summary={"source": "reconcile_query",
                                        "applied_calls": counts.get("applied_calls")},
                        duration_ms=0,
                    ),
                )
                _set_tool_call_status(
                    conn,
                    workspace_id=workspace_id,
                    tool_call_id=tool_call_id,
                    new_status="succeeded",
                )
            return RecoveryResult(
                state="succeeded",
                receipt=receipt,
                rewrote_receipt=True,
                queried_downstream=True,
                detail="下游查单确认已应用：补记 receipt，未重发请求",
            )

    # 无法证明未发出 → unknown_outcome（downstream_sent=True 表示"可能已发出"）
    with conn.transaction():
        receipt = ledger.append_receipt(
            conn,
            ledger.Receipt(
                workspace_id=workspace_id,
                tool_call_id=tool_call_id,
                intent_key=intent_key_value,
                attempt_no=int(attempts) + 1,
                arguments_hash=args_hash,
                scope_hash=scope_digest,
                tool_version=tool_version,
                outcome="unknown_outcome",
                downstream_sent=True,
                external_idempotency_key=external_key,
                result_summary={"reason": "lease_expired_no_query"},
                duration_ms=0,
                error_code="UNKNOWN_OUTCOME",
            ),
        )
        _set_tool_call_status(
            conn,
            workspace_id=workspace_id,
            tool_call_id=tool_call_id,
            new_status="unknown_outcome",
        )
    return RecoveryResult(
        state="unknown_outcome",
        receipt=receipt,
        rewrote_receipt=True,
        queried_downstream=queried,
        detail="租约过期且无法证明未发出：按未知写处理，不自动重放",
    )


def resume(
    conn: psycopg.Connection[Any],
    *,
    downstream: Downstream,
    approval_id: UUID,
    intent: ToolIntent,
    requester_principal_id: UUID,
    approver_principal_id: UUID,
    surface: str = "idem",
) -> ExecutionResult:
    """重复 resume 的入口：等价于再执行一次 ``execute``，由账本决定是否真的发请求。"""

    return execute(
        conn,
        downstream=downstream,
        approval_id=approval_id,
        intent=intent,
        requester_principal_id=requester_principal_id,
        approver_principal_id=approver_principal_id,
        surface=surface,
    )


# ---------------------------------------------------------------------------
# 实验报告
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CrashWindowReport:
    window: str
    rounds: int
    downstream_calls: int
    receipts: dict[str, int]
    states: dict[str, int]
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayReport:
    rounds: int
    downstream_applied: int
    replayed_rounds: int
    receipts: int
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LaunchSummary:
    binding_checks: dict[str, str]
    replay: ReplayReport
    invalid: dict[str, dict[str, Any]]
    crash: tuple[CrashWindowReport, ...]
    routes: dict[str, dict[str, Any]]


def _jsonable(value: Any) -> Any:
    return asdict(value) if hasattr(value, "__dataclass_fields__") else value


def dump_report(value: Any) -> str:
    return json.dumps(_jsonable(value), indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 崩溃窗口 worker（子进程入口）
# ---------------------------------------------------------------------------
def _worker(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="P0-T06 崩溃窗口 worker")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--tool-call", required=True)
    parser.add_argument("--approval", required=True)
    parser.add_argument("--requester", required=True)
    parser.add_argument("--approver", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--surface", default="idem")
    parser.add_argument("--crash-at", default=None)
    parser.add_argument("--lease-seconds", type=float, default=0.0)
    options = parser.parse_args(list(argv))
    workspace_id = UUID(options.workspace)
    with closing(connect()) as conn:
        intent = _load_intent(
            conn, workspace_id=workspace_id, tool_call_id=UUID(options.tool_call)
        )
        execute(
            conn,
            downstream=Downstream(options.site),
            approval_id=UUID(options.approval),
            intent=intent,
            requester_principal_id=UUID(options.requester),
            approver_principal_id=UUID(options.approver),
            surface=options.surface,
            lease_seconds=options.lease_seconds,
            crash_at=options.crash_at,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if "--worker" in args:
        return _worker(args)
    parser = argparse.ArgumentParser(description="P0-T06 审批/账本探针")
    parser.add_argument("--rounds", type=int, default=50, help="每个崩溃窗口/重放轮数")
    parser.add_argument("--reset", action="store_true", help="先重建 schema p0_t06")
    options = parser.parse_args(args)

    if options.reset:
        with closing(connect()) as admin:
            reset_schema(admin)
    with closing(connect(autocommit=False)) as migrator:
        apply_migration(migrator)
        migrator.commit()

    site = _control_site_class()(allow_fault_injection=False)
    site.start(timeout=5.0)
    try:
        base_url = site.url("/")
        with closing(connect()) as conn:
            summary = run_launch_checks(
                conn, Downstream(base_url), site_url=base_url, rounds=options.rounds
            )
    finally:
        site.stop()
    print(dump_report(summary))
    return 0


# ---------------------------------------------------------------------------
# 实验夹具（每轮一套全新 workspace / 主体 / 意图 / 审批）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Fixture:
    workspace_id: UUID
    run_id: UUID
    response_artifact_id: UUID
    requester_principal_id: UUID
    approver_principal_id: UUID
    approval_id: UUID
    intent: ToolIntent
    external_key: str


def _slug(text: str) -> str:
    """把任意标签压成 002 迁移允许的 slug（``[a-z0-9-]{2,32}``）。

    截断会撞车（``crash-after-downstream-success-1`` 与 ``...-10`` 前 32 字符相同），
    所以用「前缀 + 全文短哈希」保证唯一；哈希同时防止不同测试共享 slug。
    """

    cleaned = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:18]}-{digest}"


def make_fixture(
    conn: psycopg.Connection[Any],
    *,
    slug: str,
    expires_in_s: float = 600.0,
    tool_version: str = "1.0.0",
    arguments: Mapping[str, Any] | None = None,
    call_ordinal: int = 0,
    with_approval: bool = True,
) -> Fixture:
    """建一套完整夹具：workspace + 两个主体 + 响应 artifact + 意图 + pending 审批。"""

    # 叠加随机后缀：同一个标签可能被多个用例/多次重跑复用，slug 必须全局唯一。
    slug = _slug(f"{slug}-{uuid4().hex}")
    workspace_id, run_id = uuid4(), uuid4()
    requester, approver = uuid4(), uuid4()
    register_workspace(conn, workspace_id=workspace_id, slug=slug)
    register_principal(conn, workspace_id=workspace_id, principal_id=requester, role="requester")
    register_principal(conn, workspace_id=workspace_id, principal_id=approver, role="approver")
    artifact_id = persist_response(
        conn,
        workspace_id=workspace_id,
        run_id=run_id,
        model_step_key=f"step-{slug}",
        response_digest=hashlib.sha256(slug.encode()).hexdigest(),
    )
    seat = ScopeSpec(
        workspace_id=workspace_id,
        principal_id=requester,
        operation="tool.execute",
        resource_refs=(("kb", str(run_id)),),
        grant_scope="p0_t06",
    )
    intent = prepare_call(
        conn,
        workspace_id=workspace_id,
        run_id=run_id,
        response_artifact_id=artifact_id,
        call_ordinal=call_ordinal,
        name="control.write",
        tool_version=tool_version,
        arguments=dict(arguments or {"path": "/idem/write", "key": slug}),
        scope=seat,
    )
    approval_id = (
        ensure_approval(
            conn,
            intent,
            requester_principal_id=requester,
            approver_principal_id=approver,
            expires_in_s=expires_in_s,
        )
        if with_approval
        else uuid4()
    )
    external_key = ledger.external_idempotency_key(
        workspace_id=workspace_id, tool_call_id=intent.tool_call_id
    )
    return Fixture(
        workspace_id=workspace_id,
        run_id=run_id,
        response_artifact_id=artifact_id,
        requester_principal_id=requester,
        approver_principal_id=approver,
        approval_id=approval_id,
        intent=intent,
        external_key=external_key,
    )


def _run(conn: psycopg.Connection[Any], fixture: Fixture, downstream: Downstream,
         *, surface: str = "idem", approval_id: UUID | None = None,
         requester: UUID | None = None, approver: UUID | None = None,
         crash_hook: Any = None, crash_at: str | None = None,
         now: datetime | None = None) -> ExecutionResult:
    return execute(
        conn,
        downstream=downstream,
        approval_id=approval_id or fixture.approval_id,
        intent=fixture.intent,
        requester_principal_id=requester or fixture.requester_principal_id,
        approver_principal_id=approver or fixture.approver_principal_id,
        surface=surface,
        crash_hook=crash_hook,
        crash_at=crash_at,
        now=now,
    )


# ---------------------------------------------------------------------------
# 实验一：六项绑定字段「缺一即拒绝」
# ---------------------------------------------------------------------------
def check_binding_fields(
    conn: psycopg.Connection[Any], downstream: Downstream, *, surface: str = "idem"
) -> dict[str, dict[str, Any]]:
    """逐项破坏 who/action/params/scope/expiry/idempotency_key，断言全部拒绝且零副作用。"""

    report: dict[str, dict[str, Any]] = {}

    def case(
        name: str,
        broken: str,
        expected: str,
        observed: str,
        attempts_before: int,
        attempts_after: int,
    ) -> None:
        report[name] = {
            "broken_field": broken,
            "expected_code": expected,
            "observed_code": observed,
            "downstream_attempts_before": attempts_before,
            "downstream_attempts_after": attempts_after,
        }

    # 0) 基线：六项齐全时确实能执行一次（否则"拒绝"没有意义）
    base = make_fixture(conn, slug="bind-base")
    approve(conn, workspace_id=base.workspace_id, approval_id=base.approval_id,
            actor=base.approver_principal_id)
    before = counts_for(downstream, surface=surface, key=base.external_key).get("attempts", 0)
    ok = _run(conn, base, downstream, surface=surface)
    after = counts_for(downstream, surface=surface, key=base.external_key).get("attempts", 0)
    report["baseline_complete"] = {
        "broken_field": "(none)",
        "expected_code": "ACCEPTED",
        "observed_code": ok.outcome,
        "outcome": ok.outcome,
        "downstream_attempts_before": before,
        "downstream_attempts_after": after,
    }

    # 1) who：审批者被撤权（执行前重读，不信审批时快照）
    who = make_fixture(conn, slug="bind-who")
    approve(conn, workspace_id=who.workspace_id, approval_id=who.approval_id,
            actor=who.approver_principal_id)
    revoke_principal(conn, workspace_id=who.workspace_id, principal_id=who.approver_principal_id)
    b = counts_for(downstream, surface=surface, key=who.external_key).get("attempts", 0)
    res = _run(conn, who, downstream, surface=surface)
    case("who_revoked_approver", "who", "FORBIDDEN", res.error_code or res.outcome, b,
         counts_for(downstream, surface=surface, key=who.external_key).get("attempts", 0))

    # 2) action：工具版本被换（approval 绑定 0.9.0，意图声明 1.0.0）
    action = make_fixture(conn, slug="bind-action", tool_version="0.9.0")
    approve(conn, workspace_id=action.workspace_id, approval_id=action.approval_id,
            actor=action.approver_principal_id)
    b = counts_for(downstream, surface=surface, key=action.external_key).get("attempts", 0)
    tampered_intent = replace_tool_version(action.intent, "1.0.0")
    res = execute(
        conn, downstream=downstream, approval_id=action.approval_id, intent=tampered_intent,
        requester_principal_id=action.requester_principal_id,
        approver_principal_id=action.approver_principal_id, surface=surface,
    )
    case("action_version_changed", "action", "APPROVAL_MISMATCH",
         res.error_code or res.outcome, b,
         counts_for(downstream, surface=surface, key=action.external_key).get("attempts", 0))

    # 3) params：参数被改（arguments_hash 变，意图与审批不再匹配）
    params = make_fixture(conn, slug="bind-params")
    approve(conn, workspace_id=params.workspace_id, approval_id=params.approval_id,
            actor=params.approver_principal_id)
    b = counts_for(downstream, surface=surface, key=params.external_key).get("attempts", 0)
    tampered = replace_arguments_hash(params.intent, "0" * 64)
    res = execute(
        conn, downstream=downstream, approval_id=params.approval_id, intent=tampered,
        requester_principal_id=params.requester_principal_id,
        approver_principal_id=params.approver_principal_id, surface=surface,
    )
    case("params_tampered", "params", "APPROVAL_MISMATCH",
         res.error_code or res.outcome, b,
         counts_for(downstream, surface=surface, key=params.external_key).get("attempts", 0))

    # 4) scope：作用域被换
    scope = make_fixture(conn, slug="bind-scope")
    approve(conn, workspace_id=scope.workspace_id, approval_id=scope.approval_id,
            actor=scope.approver_principal_id)
    b = counts_for(downstream, surface=surface, key=scope.external_key).get("attempts", 0)
    tampered = replace_scope_hash(scope.intent, "1" * 64)
    res = execute(
        conn, downstream=downstream, approval_id=scope.approval_id, intent=tampered,
        requester_principal_id=scope.requester_principal_id,
        approver_principal_id=scope.approver_principal_id, surface=surface,
    )
    case("scope_tampered", "scope", "APPROVAL_MISMATCH",
         res.error_code or res.outcome, b,
         counts_for(downstream, surface=surface, key=scope.external_key).get("attempts", 0))

    # 5) expiry：审批已过期（expires_in_s=0 → expires_at 早于 now）
    expiry = make_fixture(conn, slug="bind-expiry", expires_in_s=1.0)
    approve(conn, workspace_id=expiry.workspace_id, approval_id=expiry.approval_id,
            actor=expiry.approver_principal_id)
    b = counts_for(downstream, surface=surface, key=expiry.external_key).get("attempts", 0)
    res = _run(conn, expiry, downstream, surface=surface,
               now=datetime.now(UTC) + timedelta(seconds=30))
    case("expiry_passed", "expiry", "APPROVAL_EXPIRED",
         res.error_code or res.outcome, b,
         counts_for(downstream, surface=surface, key=expiry.external_key).get("attempts", 0))

    # 6) idempotency_key：伪造审批行（合法 INSERT 但幂等键与派生值不符）
    # 先不建合法审批，否则 partial UNIQUE 会挡住伪造行（这本身也是一条不变式）。
    idem = make_fixture(conn, slug="bind-idem", with_approval=False)
    forged_id = uuid4()
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_approvals "
        "(workspace_id, approval_id, run_id, tool_call_id, requester_principal_id, "
        " approver_principal_id, name, tool_version, arguments_hash, scope_hash, "
        " idempotency_key, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + interval '10 minutes')",
        (idem.workspace_id, forged_id, idem.run_id, idem.intent.tool_call_id,
         idem.requester_principal_id, idem.approver_principal_id, idem.intent.name,
         idem.intent.tool_version, idem.intent.arguments_hash, idem.intent.scope_hash,
         "forged-idempotency-key"),
    )
    conn.execute(
        f"UPDATE {SCHEMA}.phase0_approvals SET status = 'approved', decided_at = now(), "
        "row_version = row_version + 1 WHERE workspace_id = %s AND approval_id = %s",
        (idem.workspace_id, forged_id),
    )
    b = counts_for(downstream, surface=surface, key=idem.external_key).get("attempts", 0)
    res = _run(conn, idem, downstream, surface=surface, approval_id=forged_id)
    case("idempotency_key_forged", "idempotency_key", "APPROVAL_MISMATCH",
         res.error_code or res.outcome, b,
         counts_for(downstream, surface=surface, key=idem.external_key).get("attempts", 0))

    # 7) 缺审批：approval 不存在
    missing = make_fixture(conn, slug="bind-missing")
    b = counts_for(downstream, surface=surface, key=missing.external_key).get("attempts", 0)
    res = _run(conn, missing, downstream, surface=surface, approval_id=uuid4())
    case("approval_missing", "approval", "APPROVAL_MISSING",
         res.error_code or res.outcome, b,
         counts_for(downstream, surface=surface, key=missing.external_key).get("attempts", 0))

    return report


def replace_tool_version(intent: ToolIntent, tool_version: str) -> ToolIntent:
    """测试用：构造一个"声明的工具版本与审批绑定的不同"的意图（不写库）。"""

    from dataclasses import replace

    return replace(intent, tool_version=tool_version)


def replace_arguments_hash(intent: ToolIntent, value: str) -> ToolIntent:
    from dataclasses import replace

    return replace(intent, arguments_hash=value)


def replace_scope_hash(intent: ToolIntent, value: str) -> ToolIntent:
    from dataclasses import replace

    return replace(intent, scope_hash=value)


# ---------------------------------------------------------------------------
# 实验二：重复 resume 只产生一次副作用
# ---------------------------------------------------------------------------
def run_replay(
    conn: psycopg.Connection[Any], downstream: Downstream, *, rounds: int = 50,
    surface: str = "idem",
) -> ReplayReport:
    fixture = make_fixture(conn, slug="replay")
    approve(conn, workspace_id=fixture.workspace_id, approval_id=fixture.approval_id,
            actor=fixture.approver_principal_id)
    first = _run(conn, fixture, downstream, surface=surface)
    violations: list[str] = []
    if first.outcome != "succeeded":
        violations.append(f"首次执行未成功：{first.outcome}/{first.error_code}")
    replayed = 0
    for index in range(2, rounds + 1):
        again = _run(conn, fixture, downstream, surface=surface)
        if again.outcome == "replayed" and again.replayed:
            replayed += 1
        else:
            violations.append(f"第 {index} 次 resume 返回 {again.outcome}（应为 replayed）")
    counts = counts_for(downstream, surface=surface, key=fixture.external_key)
    receipts = ledger.receipts_for(
        conn, workspace_id=fixture.workspace_id, tool_call_id=fixture.intent.tool_call_id
    )
    return ReplayReport(
        rounds=rounds,
        downstream_applied=int(counts.get("applied_calls", 0)),
        replayed_rounds=replayed,
        receipts=len(receipts),
        violations=tuple(violations),
    )


# ---------------------------------------------------------------------------
# 实验三：过期 / 篡改 / 跨 workspace / 撤销 → 全部拒绝且零副作用
# ---------------------------------------------------------------------------
def run_invalid_approvals(
    conn: psycopg.Connection[Any], downstream: Downstream, *, surface: str = "idem"
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}

    def record(name: str, res: ExecutionResult, key: str, attempts_before: int) -> None:
        results[name] = {
            "outcome": res.outcome,
            "error_code": res.error_code,
            "downstream_attempts_before": attempts_before,
            "downstream_attempts_after": counts_for(
                downstream, surface=surface, key=key
            ).get("attempts", 0),
        }

    # a) 过期
    expired = make_fixture(conn, slug="invalid-expired", expires_in_s=1.0)
    approve(conn, workspace_id=expired.workspace_id, approval_id=expired.approval_id,
            actor=expired.approver_principal_id)
    b = counts_for(downstream, surface=surface, key=expired.external_key).get("attempts", 0)
    record(
        "expired",
        _run(conn, expired, downstream, surface=surface,
             now=datetime.now(UTC) + timedelta(seconds=30)),
        expired.external_key,
        b,
    )

    # b) 参数被篡改
    tampered = make_fixture(conn, slug="invalid-tampered")
    approve(conn, workspace_id=tampered.workspace_id, approval_id=tampered.approval_id,
            actor=tampered.approver_principal_id)
    b = counts_for(downstream, surface=surface, key=tampered.external_key).get("attempts", 0)
    res = execute(
        conn, downstream=downstream, approval_id=tampered.approval_id,
        intent=replace_arguments_hash(tampered.intent, "f" * 64),
        requester_principal_id=tampered.requester_principal_id,
        approver_principal_id=tampered.approver_principal_id, surface=surface,
    )
    record("params_tampered", res, tampered.external_key, b)

    # c) 跨 workspace：用 A 的 approval 去跑 B 的意图
    ws_a = make_fixture(conn, slug="invalid-ws-a")
    ws_b = make_fixture(conn, slug="invalid-ws-b")
    approve(conn, workspace_id=ws_a.workspace_id, approval_id=ws_a.approval_id,
            actor=ws_a.approver_principal_id)
    b = counts_for(downstream, surface=surface, key=ws_b.external_key).get("attempts", 0)
    res = execute(
        conn, downstream=downstream, approval_id=ws_a.approval_id, intent=ws_b.intent,
        requester_principal_id=ws_b.requester_principal_id,
        approver_principal_id=ws_b.approver_principal_id, surface=surface,
    )
    record("cross_workspace", res, ws_b.external_key, b)

    # d) 已撤销
    revoked = make_fixture(conn, slug="invalid-revoked")
    approve(conn, workspace_id=revoked.workspace_id, approval_id=revoked.approval_id,
            actor=revoked.approver_principal_id)
    revoke_approval(conn, workspace_id=revoked.workspace_id, approval_id=revoked.approval_id,
                    actor=revoked.requester_principal_id)
    b = counts_for(downstream, surface=surface, key=revoked.external_key).get("attempts", 0)
    record("revoked", _run(conn, revoked, downstream, surface=surface), revoked.external_key, b)

    return results


# ---------------------------------------------------------------------------
# 实验四：崩溃窗口（真实 os._exit 子进程）
# ---------------------------------------------------------------------------
def crash_worker_command(
    *, fixture: Fixture, site_url: str, surface: str, window: str, lease_seconds: float
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--workspace", str(fixture.workspace_id),
        "--tool-call", str(fixture.intent.tool_call_id),
        "--approval", str(fixture.approval_id),
        "--requester", str(fixture.requester_principal_id),
        "--approver", str(fixture.approver_principal_id),
        "--site", site_url,
        "--surface", surface,
        "--crash-at", window,
        "--lease-seconds", str(lease_seconds),
    ]


def run_crash_window(
    conn: psycopg.Connection[Any],
    downstream: Downstream,
    *,
    window: str,
    rounds: int = 50,
    surface: str = "idem",
    site_url: str,
    can_query_downstream: bool = True,
) -> CrashWindowReport:
    """每一轮：真实子进程在窗口处崩溃 → 父进程用新连接对账 → 断言不重复写。"""

    import subprocess

    if window not in CRASH_WINDOWS:
        raise ProbeSetupError(f"未知崩溃窗口 {window!r}")
    violations: list[str] = []
    states: dict[str, int] = {}
    receipts: dict[str, int] = {}
    total_calls = 0
    for index in range(rounds):
        fixture = make_fixture(conn, slug=f"crash-{window}-{index}".lower())
        approve(conn, workspace_id=fixture.workspace_id, approval_id=fixture.approval_id,
                actor=fixture.approver_principal_id)
        command = crash_worker_command(
            fixture=fixture, site_url=site_url, surface=surface, window=window,
            lease_seconds=0.0,
        )
        completed = subprocess.run(command, capture_output=True, timeout=120, check=False)
        if completed.returncode != 9:
            violations.append(
                f"round {index}: worker 未在窗口 {window} 崩溃（rc={completed.returncode}）"
            )
        recovery = reconcile(
            conn,
            workspace_id=fixture.workspace_id,
            tool_call_id=fixture.intent.tool_call_id,
            downstream=downstream,
            surface=surface,
            can_query_downstream=can_query_downstream,
        )
        states[recovery.state] = states.get(recovery.state, 0) + 1
        receipt = recovery.receipt
        key = receipt.outcome if receipt is not None else "none"
        receipts[key] = receipts.get(key, 0) + 1
        counts = counts_for(downstream, surface=surface, key=fixture.external_key)
        calls = int(counts.get("attempts", 0))
        total_calls += calls
        # 断言 1：不许出现"第二个副作用"——幂等面 applied_calls 最多 1；
        #         非幂等面 attempts 也必须停在崩溃前的那一次（没有自动重放）。
        applied = int(counts.get("applied_calls", 0))
        if applied > 1:
            violations.append(f"round {index}: 下游 applied_calls={applied} > 1（重复写）")
        if calls > 1:
            violations.append(f"round {index}: 下游 attempts={calls} > 1（发生自动重放）")
        # 断言 2：unknown_outcome 必须被显式记录，不能记成"没执行"
        if window == "after_executing_commit" and recovery.state not in (
            "unknown_outcome",
            "succeeded",
        ):
            violations.append(f"round {index}: 崩溃窗口被静默吞掉（state={recovery.state}）")
    return CrashWindowReport(
        window=window,
        rounds=rounds,
        downstream_calls=total_calls,
        receipts=receipts,
        states=states,
        violations=tuple(violations),
    )


# ---------------------------------------------------------------------------
# 实验五：支持 / 不支持外部幂等两条路线
# ---------------------------------------------------------------------------
def run_idempotency_routes(
    conn: psycopg.Connection[Any], downstream: Downstream, *, site_url: str
) -> dict[str, dict[str, Any]]:
    """同一崩溃窗口（下游成功、receipt 前），两条下游能力面的处置不同。"""

    import subprocess

    report: dict[str, dict[str, Any]] = {}

    # 路线 A：下游支持按 key 去重 + 可查单 → 可安全处置（查单补记，不重发）
    fixture_idem = make_fixture(conn, slug="route-idem")
    approve(conn, workspace_id=fixture_idem.workspace_id, approval_id=fixture_idem.approval_id,
            actor=fixture_idem.approver_principal_id)
    subprocess.run(
        crash_worker_command(
            fixture=fixture_idem, site_url=site_url, surface="idem",
            window="after_downstream_success", lease_seconds=0.0,
        ),
        capture_output=True, timeout=120, check=False,
    )
    rec = reconcile(
        conn, workspace_id=fixture_idem.workspace_id,
        tool_call_id=fixture_idem.intent.tool_call_id,
        downstream=downstream, surface="idem", can_query_downstream=True,
    )
    counts = counts_for(downstream, surface="idem", key=fixture_idem.external_key)
    # 路线 A 的安全重放：再调用一次同 key，下游去重，applied 仍为 1
    replay = downstream.call(surface="idem", key=fixture_idem.external_key)
    counts_after = counts_for(downstream, surface="idem", key=fixture_idem.external_key)
    report["route_idempotent"] = {
        "reconcile_state": rec.state,
        "queried_downstream": rec.queried_downstream,
        "applied_after_crash": int(counts.get("applied_calls", 0)),
        "replay_applied": bool(replay.payload.get("applied")),
        "applied_after_replay": int(counts_after.get("applied_calls", 0)),
        "attempts_after_replay": int(counts_after.get("attempts", 0)),
        "conclusion": "支持下自动重试：同 key 重放不增加副作用",
    }

    # 路线 B：下游不支持去重/查单 → 未知写必须停手；对照实验证明"重放会翻倍"
    fixture_non = make_fixture(conn, slug="route-nonidem")
    approve(conn, workspace_id=fixture_non.workspace_id, approval_id=fixture_non.approval_id,
            actor=fixture_non.approver_principal_id)
    subprocess.run(
        crash_worker_command(
            fixture=fixture_non, site_url=site_url, surface="nonidem",
            window="after_downstream_success", lease_seconds=0.0,
        ),
        capture_output=True, timeout=120, check=False,
    )
    rec_non = reconcile(
        conn, workspace_id=fixture_non.workspace_id,
        tool_call_id=fixture_non.intent.tool_call_id,
        downstream=downstream, surface="nonidem", can_query_downstream=False,
    )
    counts_non = counts_for(downstream, surface="nonidem", key=fixture_non.external_key)
    # 负向对照：如果无视 unknown_outcome 强行重放，副作用会变成 2
    forced = downstream.call(surface="nonidem", key=fixture_non.external_key)
    counts_non_after = counts_for(downstream, surface="nonidem", key=fixture_non.external_key)
    report["route_non_idempotent"] = {
        "reconcile_state": rec_non.state,
        "queried_downstream": rec_non.queried_downstream,
        "applied_after_crash": int(counts_non.get("applied_calls", 0)),
        "forced_replay_applied": bool(forced.payload.get("applied")),
        "applied_after_forced_replay": int(counts_non_after.get("applied_calls", 0)),
        "conclusion": "不支持外部幂等：禁止自动重放（强放会变成 2 次副作用）",
    }
    return report


# ---------------------------------------------------------------------------
# 汇总 / CLI
# ---------------------------------------------------------------------------
def run_launch_checks(
    conn: psycopg.Connection[Any],
    downstream: Downstream,
    *,
    site_url: str,
    rounds: int = 50,
) -> LaunchSummary:
    """完整实验：绑定字段 / 重放 / 非法审批 / 三个崩溃窗口 / 两条下游路线。"""

    binding = check_binding_fields(conn, downstream)
    replay = run_replay(conn, downstream, rounds=rounds)
    invalid = run_invalid_approvals(conn, downstream)
    crash = tuple(
        run_crash_window(
            conn, downstream, window=window, rounds=rounds, site_url=site_url,
            # 三个窗口都允许查单；"查不了单"的路线由 run_idempotency_routes 的非幂等面覆盖。
            can_query_downstream=True,
        )
        for window in CRASH_WINDOWS
    )
    routes = run_idempotency_routes(conn, downstream, site_url=site_url)
    return LaunchSummary(
        binding_checks={
            name: str(item.get("observed_code") or item.get("outcome") or "")
            for name, item in binding.items()
        },
        replay=replay,
        invalid=invalid,
        crash=crash,
        routes=routes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
