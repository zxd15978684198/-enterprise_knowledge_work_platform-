"""P0-T20 预算预留与成本账本：整数 micro-units、并发安全、未知费用不归零。

契约来源
--------
* ``docs/tdd/02_Agent_Harness.md`` §7：Budget 按 workspace/run 建同一账本，父 run 是
  成本总归集主体；``reserve(purpose_key)`` 的 ``UNIQUE(workspace_id,run_id,purpose_key)``；
  事务内检查 ``spent + reserved + maximum <= limit``；任何维度不足 → `BUDGET_EXCEEDED`
  且**不发调用**；settle 原子 ``reserved -= maximum, spent += actual``；
  ``UNIQUE(reservation_id, settlement)`` 防重复；未发出可 release；已发出但 usage 未知
  **不得超时自动释放**，转 unsettled 保守保留；actual 超预留记录真实超额并冻结后续。
* ``docs/poc/03_Phase0_Task_Backlog.md`` P0-T20 卡：并发账本重复扣费/绕过上限 0、
  已发未知费用自动释放 0、每 retry 费用可归集。
* ``docs/tdd/01_System_Architecture.md`` §6：共享 UoW 内由服务执行命令，不私自 commit；
  本模块在无外部事务时自行开/提交单个短事务，在调用方事务内则退化为 savepoint。

与 T19 的衔接（T19 交付报告点名的坑）
------------------------------------
``model_gateway.Usage`` 在取不到价格时给出 ``cost_microunits=0`` + ``cost_source=
"unavailable"``。本账本**禁止**把 unavailable 当 0 计入：调用方必须用
:meth:`BudgetLedger.settle_unavailable`，它按**预留的 maximum** 记账并置
``provisional=True``、累计 ``unsettled_cost_microunits``，绝不自动释放。
``estimate_cost`` 返回 ``cost_microunits=None``（不是 0）表示不可计价。

边界（本任务不做）
------------------
* 不做 task 级子额度（T23/T24 需要时增量迁移）；不做价格快照表（应用层传入
  :class:`PriceSnapshot`）；不做自动解冻（只冻结 + 告警，人工命令归后续任务）。
* 不实现模型/工具调用本身：只提供 ``reserve_model_call`` / ``reserve_tool_call``
  两个闸门；调用在预留成功之后才允许发生（T21 串链）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from contracts import WorkspaceScope
from scope import read_dsn

__all__ = [
    "LEDGER_LOCK_ORDER",
    "MIGRATION_ID",
    "MIGRATION_PATH",
    "SCHEMA",
    "BudgetError",
    "BudgetExceededError",
    "BudgetFrozenError",
    "BudgetLedger",
    "BudgetLimits",
    "Cost",
    "CostEstimate",
    "CostSource",
    "DeadlineExceededError",
    "LedgerConfigError",
    "LedgerMismatchError",
    "LedgerState",
    "PriceSnapshot",
    "PricingUnavailableError",
    "ReconcileReport",
    "Reservation",
    "ReservationConflictError",
    "ReservationStateError",
    "Settlement",
    "SettlementConflictError",
    "apply_migration",
    "connect",
    "estimate_cost",
    "migration_sha256",
    "reset_schema",
]

SCHEMA: Final = "p0_t20"
MIGRATION_ID: Final = "005_budget"
MIGRATION_PATH: Final = Path(__file__).resolve().parent / "db" / "migrations" / "005_budget.sql"

#: TDD02 §7 锁序：workspace → run → task。本模块只有 run 级账本，仍按此顺序取锁。
LEDGER_LOCK_ORDER: Final = ("workspace", "run", "task")

_CURRENCY_RE: Final = re.compile(r"^[A-Z]{3}$")
_PURPOSE_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")
_KIND_VALUES: Final = ("model", "tool", "step")
_COST_SOURCES: Final = ("provider_reported", "estimated", "unavailable")
_MICRO_PER_UNIT: Final = 1_000_000

CostSource = Literal["provider_reported", "estimated", "unavailable"]


# ---------------------------------------------------------------------------
# 错误（稳定 code；证据与断言只引用 code + dimension）
# ---------------------------------------------------------------------------
class BudgetError(RuntimeError):
    """预算错误基类；``code`` 稳定，``dimension`` 指明越界维度。"""

    code: str = "BUDGET_ERROR"

    def __init__(self, message: str, *, dimension: str | None = None) -> None:
        super().__init__(message)
        self.dimension = dimension

    def __str__(self) -> str:
        if self.dimension:
            return f"{self.code}: {super().__str__()} (dimension={self.dimension})"
        return f"{self.code}: {super().__str__()}"


class BudgetExceededError(BudgetError):
    code = "BUDGET_EXCEEDED"


class BudgetFrozenError(BudgetError):
    code = "BUDGET_FROZEN"


class DeadlineExceededError(BudgetError):
    code = "DEADLINE_EXCEEDED"


class LedgerConfigError(BudgetError):
    code = "BUDGET_CONFIG_INVALID"


class LedgerMismatchError(BudgetError):
    code = "LEDGER_MISMATCH"


class ReservationStateError(BudgetError):
    code = "RESERVATION_STATE_INVALID"


class SettlementConflictError(BudgetError):
    code = "SETTLEMENT_CONFLICT"


class PricingUnavailableError(BudgetError):
    code = "PRICING_UNAVAILABLE"


class ReservationConflictError(BudgetError):
    code = "RESERVATION_CONFLICT"


# ---------------------------------------------------------------------------
# 整数成本向量与限值
# ---------------------------------------------------------------------------
def _require_int(name: str, value: object, *, minimum: int = 0, maximum: int = 10**15) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerConfigError(
            f"{name} 必须是整数（micro-units / 计数），收到 {type(value).__name__}"
        )
    if not minimum <= value <= maximum:
        raise LedgerConfigError(f"{name} 必须在 [{minimum}, {maximum}]，收到 {value}")
    return value


def _require_aware(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LedgerConfigError(f"{name} 必须是带时区的 datetime")
    return value.astimezone(UTC)


def _require_key(value: object, name: str) -> str:
    if not isinstance(value, str) or _PURPOSE_KEY_RE.match(value) is None:
        raise LedgerConfigError(f"{name} 必须是 1-128 位 [A-Za-z0-9_.:@/-]")
    return value


@dataclass(frozen=True, slots=True)
class Cost:
    """成本向量：全部整数，金额单位为 micro-units（1e-6 货币单位）。"""

    tokens: int = 0
    cost_microunits: int = 0
    pages: int = 0
    bytes: int = 0
    wall_ms: int = 0
    concurrency: int = 0

    def __post_init__(self) -> None:
        _require_int("Cost.tokens", self.tokens)
        _require_int("Cost.cost_microunits", self.cost_microunits)
        _require_int("Cost.pages", self.pages)
        _require_int("Cost.bytes", self.bytes)
        _require_int("Cost.wall_ms", self.wall_ms)
        _require_int("Cost.concurrency", self.concurrency, maximum=4)

    def as_dict(self) -> dict[str, int]:
        return {
            "tokens": self.tokens,
            "cost_microunits": self.cost_microunits,
            "pages": self.pages,
            "bytes": self.bytes,
            "wall_ms": self.wall_ms,
            "concurrency": self.concurrency,
        }

    def plus(self, other: Cost) -> Cost:
        return Cost(
            tokens=self.tokens + other.tokens,
            cost_microunits=self.cost_microunits + other.cost_microunits,
            pages=self.pages + other.pages,
            bytes=self.bytes + other.bytes,
            wall_ms=max(self.wall_ms, other.wall_ms),
            concurrency=self.concurrency + other.concurrency,
        )

    def exceeds(self, other: Cost) -> bool:
        """任一可加维度超过 ``other``；``wall_ms`` 是 deadline 语义，不参与比较。"""

        return (
            self.tokens > other.tokens
            or self.cost_microunits > other.cost_microunits
            or self.pages > other.pages
            or self.bytes > other.bytes
            or self.concurrency > other.concurrency
        )


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    tokens: int = 0
    cost_microunits: int = 0
    pages: int = 0
    bytes: int = 0
    concurrency: int = 0
    currency: str = "USD"
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_int("BudgetLimits.tokens", self.tokens)
        _require_int("BudgetLimits.cost_microunits", self.cost_microunits)
        _require_int("BudgetLimits.pages", self.pages)
        _require_int("BudgetLimits.bytes", self.bytes)
        _require_int("BudgetLimits.concurrency", self.concurrency, maximum=4)
        if not isinstance(self.currency, str) or _CURRENCY_RE.match(self.currency) is None:
            raise LedgerConfigError("currency 必须是 3 位大写币种")
        _require_aware(self.deadline_at, "BudgetLimits.deadline_at")

    def as_cost(self) -> Cost:
        return Cost(
            tokens=self.tokens,
            cost_microunits=self.cost_microunits,
            pages=self.pages,
            bytes=self.bytes,
            concurrency=self.concurrency,
        )


# ---------------------------------------------------------------------------
# 定价快照与成本估算（取不到价格 → None，不是 0）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    """模型定价快照（整数 microunits / 百万 token）；版本化便于审计。"""

    model_profile_id: str
    input_microunits_per_million: int
    output_microunits_per_million: int
    currency: str = "USD"
    version: str = "v1"

    def __post_init__(self) -> None:
        if not isinstance(self.model_profile_id, str) or not 1 <= len(self.model_profile_id) <= 128:
            raise LedgerConfigError("model_profile_id 长度必须在 [1, 128]")
        _require_int("input price", self.input_microunits_per_million)
        _require_int("output price", self.output_microunits_per_million)
        if not isinstance(self.currency, str) or _CURRENCY_RE.match(self.currency) is None:
            raise LedgerConfigError("currency 必须是 3 位大写币种")
        if not isinstance(self.version, str) or not 1 <= len(self.version) <= 32:
            raise LedgerConfigError("version 长度必须在 [1, 32]")


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """成本估算结果；``cost_microunits is None`` 表示不可计价（不得当 0）。"""

    cost_microunits: int | None
    cost_source: CostSource
    currency: str

    def require_cost(self) -> int:
        if self.cost_microunits is None:
            raise PricingUnavailableError(
                "没有可用价格快照：不能把未知成本当 0 计入，请传保守的上限或走 settle_unavailable"
            )
        return self.cost_microunits


def _scale(tokens: int, microunits_per_million: int) -> int:
    """整数向上取整：tokens * 单价 / 1e6，不用浮点。"""

    _require_int("tokens", tokens)
    _require_int("price", microunits_per_million)
    return (tokens * microunits_per_million + _MICRO_PER_UNIT - 1) // _MICRO_PER_UNIT


def estimate_cost(
    *, input_tokens: int, output_tokens: int, price: PriceSnapshot | None
) -> CostEstimate:
    """按定价快照估算成本；快照缺失时返回 ``None + unavailable``，绝不返回 0 冒充已计价。"""

    _require_int("input_tokens", input_tokens)
    _require_int("output_tokens", output_tokens)
    if price is None:
        return CostEstimate(cost_microunits=None, cost_source="unavailable", currency="USD")
    return CostEstimate(
        cost_microunits=_scale(input_tokens, price.input_microunits_per_million)
        + _scale(output_tokens, price.output_microunits_per_million),
        cost_source="estimated",
        currency=price.currency,
    )


# ---------------------------------------------------------------------------
# 事实 DTO
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LedgerState:
    workspace_id: UUID
    run_id: UUID
    currency: str
    limits: Cost
    spent: Cost
    reserved: Cost
    used_concurrency: int
    unsettled_cost_microunits: int
    frozen: bool
    freeze_reason: str | None
    deadline_at: datetime | None
    row_version: int

    def available(self) -> Cost:
        """剩余可预留额度（已结算 + 在途都算占用；冻结时为 0）。"""

        if self.frozen:
            return Cost()
        return Cost(
            tokens=max(0, self.limits.tokens - self.spent.tokens - self.reserved.tokens),
            cost_microunits=max(
                0,
                self.limits.cost_microunits
                - self.spent.cost_microunits
                - self.reserved.cost_microunits,
            ),
            pages=max(0, self.limits.pages - self.spent.pages - self.reserved.pages),
            bytes=max(0, self.limits.bytes - self.spent.bytes - self.reserved.bytes),
            concurrency=max(0, self.limits.concurrency - self.used_concurrency),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "run_id": str(self.run_id),
            "currency": self.currency,
            "limits": self.limits.as_dict(),
            "spent": self.spent.as_dict(),
            "reserved": self.reserved.as_dict(),
            "used_concurrency": self.used_concurrency,
            "unsettled_cost_microunits": self.unsettled_cost_microunits,
            "frozen": self.frozen,
            "freeze_reason": self.freeze_reason,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "row_version": self.row_version,
        }


@dataclass(frozen=True, slots=True)
class Reservation:
    workspace_id: UUID
    run_id: UUID
    reservation_id: UUID
    purpose_key: str
    kind: str
    status: str
    maximum: Cost
    row_version: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": str(self.reservation_id),
            "purpose_key": self.purpose_key,
            "kind": self.kind,
            "status": self.status,
            "maximum": self.maximum.as_dict(),
            "row_version": self.row_version,
        }


@dataclass(frozen=True, slots=True)
class Settlement:
    workspace_id: UUID
    run_id: UUID
    reservation_id: UUID
    settlement_key: str
    actual: Cost
    effective_cost_microunits: int
    cost_source: CostSource
    provisional: bool
    overage: bool
    overage_cost_microunits: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": str(self.reservation_id),
            "settlement_key": self.settlement_key,
            "actual": self.actual.as_dict(),
            "effective_cost_microunits": self.effective_cost_microunits,
            "cost_source": self.cost_source,
            "provisional": self.provisional,
            "overage": self.overage,
            "overage_cost_microunits": self.overage_cost_microunits,
        }


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """内存/派生计数与账本行的一致性报告（用于「账本与 PG 对账」验收）。"""

    workspace_id: UUID
    run_id: UUID
    matches: bool
    ledger_spent: Cost
    derived_spent: Cost
    ledger_reserved: Cost
    derived_reserved: Cost
    ledger_used_concurrency: int
    derived_used_concurrency: int
    unsettled_cost_microunits: int
    reservations: int
    settlements: int

    def require_match(self) -> None:
        if not self.matches:
            raise LedgerMismatchError(
                "账本行与预留/结算事实不一致："
                f"spent={self.ledger_spent.as_dict()} derived={self.derived_spent.as_dict()} "
                f"reserved={self.ledger_reserved.as_dict()} "
                f"derived={self.derived_reserved.as_dict()}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": str(self.workspace_id),
            "run_id": str(self.run_id),
            "matches": self.matches,
            "ledger_spent": self.ledger_spent.as_dict(),
            "derived_spent": self.derived_spent.as_dict(),
            "ledger_reserved": self.ledger_reserved.as_dict(),
            "derived_reserved": self.derived_reserved.as_dict(),
            "ledger_used_concurrency": self.ledger_used_concurrency,
            "derived_used_concurrency": self.derived_used_concurrency,
            "unsettled_cost_microunits": self.unsettled_cost_microunits,
            "reservations": self.reservations,
            "settlements": self.settlements,
        }


# ---------------------------------------------------------------------------
# 连接 / 迁移（与 T03/T07 同构；台账写 p0_t20）
# ---------------------------------------------------------------------------
def connect(*, dsn: str | None = None, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn or read_dsn(), autocommit=autocommit)


def migration_sha256(path: Path = MIGRATION_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migration(
    conn: psycopg.Connection[Any],
    *,
    path: Path = MIGRATION_PATH,
    migration_id: str = MIGRATION_ID,
) -> str:
    """单事务应用迁移 005 并写本 schema 台账；调用方负责 commit。"""

    if conn.autocommit:
        raise LedgerConfigError("apply_migration 需要 autocommit=False 的连接")
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        raise LedgerConfigError("apply_migration 需要干净事务边界（IDLE）")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute(raw.decode("utf-8"))
    conn.execute(
        f"INSERT INTO {SCHEMA}.phase0_schema_migrations (migration_id, filename, sha256) "
        "VALUES (%s, %s, %s)",
        (migration_id, path.name, digest),
    )
    return digest


def reset_schema(conn: psycopg.Connection[Any]) -> None:
    """删除本任务 schema（清理用）；只碰 p0_t20。"""

    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------
_LEDGER_COLUMNS: Final = (
    "workspace_id, run_id, currency, limit_tokens, limit_cost_microunits, "
    "limit_pages, limit_bytes, limit_concurrency, spent_tokens, spent_cost_microunits, "
    "spent_pages, spent_bytes, reserved_tokens, reserved_cost_microunits, reserved_pages, "
    "reserved_bytes, used_concurrency, unsettled_cost_microunits, frozen, freeze_reason, "
    "deadline_at, row_version"
)

_RESERVATION_COLUMNS: Final = (
    "workspace_id, run_id, reservation_id, purpose_key, kind, status, maximum_tokens, "
    "maximum_cost_microunits, maximum_pages, maximum_bytes, reserved_concurrency, row_version"
)

_SETTLEMENT_SELECT: Final = (
    "workspace_id, run_id, reservation_id, settlement_key, actual_tokens, "
    "actual_cost_microunits, actual_pages, actual_bytes, effective_cost_microunits, "
    "cost_source, provisional, overage, overage_cost_microunits"
)


class BudgetLedger:
    """单个 (workspace, run) 的预算账本；每次变更在自身短事务内加行锁。

    事务语义：调用方已在事务中时，本类方法退化为 savepoint（不擅自提交调用方事务）；
    否则自行 BEGIN/COMMIT。任何失败都回滚本次命令，账本行保持上一个一致状态。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        if conn.autocommit:
            raise LedgerConfigError("BudgetLedger 需要 autocommit=False 的连接（行锁 + 单事务）")
        self._conn = conn

    # -- 基础 ---------------------------------------------------------
    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._conn.transaction():
            yield

    def _cursor(self) -> psycopg.Cursor[dict[str, Any]]:
        return self._conn.cursor(row_factory=dict_row)

    def _execute(self, query: str, params: Sequence[object] = ()) -> psycopg.Cursor[Any]:
        return self._conn.execute(query, params)

    @staticmethod
    def _workspace(scope: WorkspaceScope) -> UUID:
        if not isinstance(scope, WorkspaceScope):
            raise LedgerConfigError("预算操作需要服务端签发的 WorkspaceScope")
        return scope.workspace_id

    @staticmethod
    def _require_run(run_id: object) -> UUID:
        if not isinstance(run_id, UUID):
            raise LedgerConfigError("run_id 必须是 UUID")
        return run_id

    def _load(self, workspace_id: UUID, run_id: UUID, *, lock: bool) -> LedgerState:
        suffix = " FOR UPDATE" if lock else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_LEDGER_COLUMNS} FROM {SCHEMA}.phase0_budget_ledgers "
                f"WHERE workspace_id = %s AND run_id = %s{suffix}",
                (workspace_id, run_id),
            )
            row = cur.fetchone()
        if row is None:
            raise LedgerConfigError(
                f"预算账本不存在（workspace={workspace_id} run={run_id}）；先调用 ensure()"
            )
        return _state_from_row(row)

    def _load_reservation(
        self, workspace_id: UUID, run_id: UUID, reservation_id: UUID, *, lock: bool
    ) -> Reservation:
        suffix = " FOR UPDATE" if lock else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_RESERVATION_COLUMNS} FROM {SCHEMA}.phase0_budget_reservations "
                f"WHERE workspace_id = %s AND run_id = %s AND reservation_id = %s{suffix}",
                (workspace_id, run_id, reservation_id),
            )
            row = cur.fetchone()
        if row is None:
            raise ReservationStateError(f"预留不存在: {reservation_id}")
        return _reservation_from_row(row)

    def _existing_reservation(
        self, workspace_id: UUID, run_id: UUID, purpose_key: str
    ) -> Reservation | None:
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_RESERVATION_COLUMNS} FROM {SCHEMA}.phase0_budget_reservations "
                "WHERE workspace_id = %s AND run_id = %s AND purpose_key = %s",
                (workspace_id, run_id, purpose_key),
            )
            row = cur.fetchone()
        return None if row is None else _reservation_from_row(row)

    def _existing_settlement(
        self, workspace_id: UUID, run_id: UUID, reservation_id: UUID, settlement_key: str
    ) -> Settlement | None:
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_SETTLEMENT_SELECT} FROM {SCHEMA}.phase0_budget_settlements "
                "WHERE workspace_id = %s AND run_id = %s AND reservation_id = %s "
                "AND settlement_key = %s",
                (workspace_id, run_id, reservation_id, settlement_key),
            )
            row = cur.fetchone()
        return None if row is None else _settlement_from_row(row)

    def _event(
        self,
        workspace_id: UUID,
        run_id: UUID,
        kind: str,
        *,
        reservation_id: UUID | None = None,
        detail: str | None = None,
    ) -> None:
        self._execute(
            f"INSERT INTO {SCHEMA}.phase0_budget_events "
            "(workspace_id, run_id, event_id, kind, reservation_id, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (workspace_id, run_id, uuid4(), kind, reservation_id, detail),
        )

    # -- ensure / state ------------------------------------------------
    def ensure(
        self,
        *,
        scope: WorkspaceScope,
        run_id: UUID,
        limits: BudgetLimits,
        now: datetime | None = None,
    ) -> LedgerState:
        """幂等创建账本；已存在且限额/币种一致时复用，不一致直接拒绝。"""

        workspace_id = self._workspace(scope)
        self._require_run(run_id)
        if not isinstance(limits, BudgetLimits):
            raise LedgerConfigError("ensure 需要 BudgetLimits")
        del now
        with self._transaction():
            self._execute(
                f"INSERT INTO {SCHEMA}.phase0_budget_ledgers "
                "(workspace_id, run_id, currency, limit_tokens, limit_cost_microunits, "
                " limit_pages, limit_bytes, limit_concurrency, deadline_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (workspace_id, run_id) DO NOTHING",
                (
                    workspace_id,
                    run_id,
                    limits.currency,
                    limits.tokens,
                    limits.cost_microunits,
                    limits.pages,
                    limits.bytes,
                    limits.concurrency,
                    limits.deadline_at,
                ),
            )
            state = self._load(workspace_id, run_id, lock=True)
            if (
                state.limits.tokens != limits.tokens
                or state.limits.cost_microunits != limits.cost_microunits
                or state.limits.pages != limits.pages
                or state.limits.bytes != limits.bytes
                or state.used_concurrency > limits.concurrency
                or state.currency != limits.currency
            ):
                raise LedgerConfigError(
                    "同一 (workspace, run) 的预算限额/币种不可变更；调整必须新 run 或新账本版本"
                )
            self._event(workspace_id, run_id, "ensure", detail="created-or-reused")
        return state

    def state(self, *, scope: WorkspaceScope, run_id: UUID) -> LedgerState:
        return self._load(self._workspace(scope), self._require_run(run_id), lock=False)

    # -- reserve -------------------------------------------------------
    def reserve(
        self,
        *,
        scope: WorkspaceScope,
        run_id: UUID,
        purpose_key: str,
        maximum: Cost,
        kind: str = "step",
        now: datetime | None = None,
    ) -> Reservation:
        """原子预留：同 purpose_key 幂等；任何维度不足 → BUDGET_EXCEEDED，且不留半行。"""

        workspace_id = self._workspace(scope)
        self._require_run(run_id)
        _require_key(purpose_key, "purpose_key")
        if kind not in _KIND_VALUES:
            raise LedgerConfigError(f"kind 必须是 {_KIND_VALUES}")
        if not isinstance(maximum, Cost):
            raise LedgerConfigError("reserve 需要 Cost")
        moment = _require_aware(now or datetime.now(UTC), "now")
        assert moment is not None

        with self._transaction():
            existing = self._existing_reservation(workspace_id, run_id, purpose_key)
            if existing is not None:
                if existing.maximum != maximum or existing.kind != kind:
                    raise ReservationConflictError(
                        f"purpose_key={purpose_key!r} 已存在不同额度/类型的预留（幂等键不得换参数）"
                    )
                return existing

            state = self._load(workspace_id, run_id, lock=True)
            # 行锁拿到后重查一次：并发同 purpose_key 的第二个请求在这里看到已提交行，
            # 走幂等分支而不是撞唯一约束（唯一约束仍是最后防线）。
            existing = self._existing_reservation(workspace_id, run_id, purpose_key)
            if existing is not None:
                if existing.maximum != maximum or existing.kind != kind:
                    raise ReservationConflictError(
                        f"purpose_key={purpose_key!r} 已存在不同额度/类型的预留（幂等键不得换参数）"
                    )
                return existing
            if state.frozen:
                self._event(workspace_id, run_id, "reject", detail=f"frozen:{state.freeze_reason}")
                raise BudgetFrozenError(
                    f"预算已冻结（reason={state.freeze_reason}）：拒绝新的预留", dimension="frozen"
                )
            if state.deadline_at is not None and moment >= state.deadline_at:
                self._event(workspace_id, run_id, "reject", detail="deadline")
                raise DeadlineExceededError(
                    "已超过预算 deadline：拒绝新的调用", dimension="deadline"
                )

            dimension = self._first_exhausted(state, maximum)
            if dimension is not None:
                self._event(workspace_id, run_id, "reject", detail=f"exceeded:{dimension}")
                raise BudgetExceededError(
                    f"预算不足（{dimension}）：spent+reserved+maximum 超过 limit",
                    dimension=dimension,
                )

            reservation_id = uuid4()
            self._execute(
                f"UPDATE {SCHEMA}.phase0_budget_ledgers SET "
                "reserved_tokens = reserved_tokens + %s, "
                "reserved_cost_microunits = reserved_cost_microunits + %s, "
                "reserved_pages = reserved_pages + %s, reserved_bytes = reserved_bytes + %s, "
                "used_concurrency = used_concurrency + %s, "
                "updated_at = now(), row_version = row_version + 1 "
                "WHERE workspace_id = %s AND run_id = %s",
                (
                    maximum.tokens,
                    maximum.cost_microunits,
                    maximum.pages,
                    maximum.bytes,
                    maximum.concurrency,
                    workspace_id,
                    run_id,
                ),
            )
            self._execute(
                f"INSERT INTO {SCHEMA}.phase0_budget_reservations "
                "(workspace_id, run_id, reservation_id, purpose_key, kind, "
                " maximum_tokens, maximum_cost_microunits, maximum_pages, maximum_bytes, "
                " reserved_concurrency) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    run_id,
                    reservation_id,
                    purpose_key,
                    kind,
                    maximum.tokens,
                    maximum.cost_microunits,
                    maximum.pages,
                    maximum.bytes,
                    maximum.concurrency,
                ),
            )
            self._event(
                workspace_id, run_id, "reserve", reservation_id=reservation_id, detail=kind
            )
            return self._load_reservation(
                workspace_id, run_id, reservation_id, lock=False
            )

    @staticmethod
    def _first_exhausted(state: LedgerState, maximum: Cost) -> str | None:
        limits = state.limits
        spent = state.spent
        reserved = state.reserved
        checks = (
            ("tokens", maximum.tokens, spent.tokens + reserved.tokens, limits.tokens),
            (
                "cost_microunits",
                maximum.cost_microunits,
                spent.cost_microunits + reserved.cost_microunits,
                limits.cost_microunits,
            ),
            ("pages", maximum.pages, spent.pages + reserved.pages, limits.pages),
            ("bytes", maximum.bytes, spent.bytes + reserved.bytes, limits.bytes),
            (
                "concurrency",
                maximum.concurrency,
                state.used_concurrency,
                limits.concurrency,
            ),
        )
        for dimension, requested, used, limit in checks:
            if requested > 0 and used + requested > limit:
                return dimension
        return None

    # -- 调用闸门（模型 / 工具） ----------------------------------------
    def reserve_model_call(
        self,
        *,
        scope: WorkspaceScope,
        run_id: UUID,
        purpose_key: str,
        prompt_tokens: int,
        max_output_tokens: int,
        price: PriceSnapshot | None,
        max_cost_microunits: int | None = None,
        now: datetime | None = None,
    ) -> Reservation:
        """模型调用闸门：预算不足/无价格上限时抛错，调用方不得发起请求。"""

        _require_int("prompt_tokens", prompt_tokens)
        _require_int("max_output_tokens", max_output_tokens)
        total_tokens = prompt_tokens + max_output_tokens
        if max_cost_microunits is not None:
            _require_int("max_cost_microunits", max_cost_microunits)
            cost_cap = max_cost_microunits
        else:
            estimate = estimate_cost(
                input_tokens=prompt_tokens, output_tokens=max_output_tokens, price=price
            )
            cost_cap = estimate.require_cost()
        return self.reserve(
            scope=scope,
            run_id=run_id,
            purpose_key=purpose_key,
            maximum=Cost(tokens=total_tokens, cost_microunits=cost_cap),
            kind="model",
            now=now,
        )

    def reserve_tool_call(
        self,
        *,
        scope: WorkspaceScope,
        run_id: UUID,
        purpose_key: str,
        max_bytes: int = 0,
        max_pages: int = 0,
        concurrency: int = 1,
        wall_ms: int = 0,
        now: datetime | None = None,
    ) -> Reservation:
        """工具调用闸门：并发槽/字节/页数不足时抛错，调用方不得执行工具。"""

        _require_int("max_bytes", max_bytes)
        _require_int("max_pages", max_pages)
        _require_int("concurrency", concurrency, maximum=4)
        _require_int("wall_ms", wall_ms)
        return self.reserve(
            scope=scope,
            run_id=run_id,
            purpose_key=purpose_key,
            maximum=Cost(
                pages=max_pages, bytes=max_bytes, wall_ms=wall_ms, concurrency=concurrency
            ),
            kind="tool",
            now=now,
        )

    # -- settle / release ----------------------------------------------
    def settle(
        self,
        *,
        reservation_id: UUID,
        actual: Cost,
        settlement_key: str,
        cost_source: Literal["provider_reported", "estimated"] = "provider_reported",
        now: datetime | None = None,
    ) -> Settlement:
        """已计价结算：actual 超预留则记录真实超额并冻结后续预留。"""

        if cost_source not in ("provider_reported", "estimated"):
            raise LedgerConfigError(
                "settle 只接受 provider_reported/estimated；未知价格用 settle_unavailable"
            )
        if not isinstance(actual, Cost):
            raise LedgerConfigError("settle 需要 Cost")
        _require_key(settlement_key, "settlement_key")
        del now
        return self._apply_settlement(
            reservation_id,
            actual=actual,
            settlement_key=settlement_key,
            cost_source=cost_source,
            provisional=False,
        )

    def settle_unavailable(
        self,
        *,
        reservation_id: UUID,
        settlement_key: str,
        actual: Cost | None = None,
        reason: str = "USAGE_UNAVAILABLE",
        now: datetime | None = None,
    ) -> Settlement:
        """已发出但 usage/价格不可得：按预留 maximum 保守记账，绝不自动释放、绝不计 0。"""

        _require_key(settlement_key, "settlement_key")
        _require_key(reason, "reason")
        if actual is not None and not isinstance(actual, Cost):
            raise LedgerConfigError("actual 必须是 Cost 或 None")
        del now
        return self._apply_settlement(
            reservation_id,
            actual=actual,
            settlement_key=settlement_key,
            cost_source="unavailable",
            provisional=True,
            reason=reason,
        )

    def _apply_settlement(
        self,
        reservation_id: UUID,
        *,
        actual: Cost | None,
        settlement_key: str,
        cost_source: CostSource,
        provisional: bool,
        reason: str | None = None,
    ) -> Settlement:
        with self._transaction():
            reservation = self._reservation_by_id(reservation_id, lock=True)
            workspace_id = reservation.workspace_id
            run_id = reservation.run_id

            effective = actual if actual is not None else reservation.maximum
            if provisional:
                # 取不到价格/usage：按预留 maximum 保守记账，绝不写 0、绝不自动释放。
                effective = Cost(
                    tokens=effective.tokens,
                    cost_microunits=reservation.maximum.cost_microunits,
                    pages=effective.pages,
                    bytes=effective.bytes,
                    wall_ms=effective.wall_ms,
                    concurrency=0,
                )
            effective_cost = effective.cost_microunits

            existing = self._existing_settlement(
                workspace_id, run_id, reservation_id, settlement_key
            )
            if existing is not None:
                if (
                    existing.actual != effective
                    or existing.cost_source != cost_source
                    or existing.effective_cost_microunits != effective_cost
                ):
                    raise SettlementConflictError(
                        "同一 settlement_key 携带了不同 actual/cost_source（幂等键不得换参数）"
                    )
                return existing

            if reservation.status != "reserved":
                raise ReservationStateError(
                    f"预留 {reservation_id} 状态为 {reservation.status}，不能再次结算"
                )

            state = self._load(workspace_id, run_id, lock=True)
            overage_cost = max(0, effective.cost_microunits - reservation.maximum.cost_microunits)
            overage = effective.exceeds(reservation.maximum)
            new_frozen = state.frozen
            freeze_reason = state.freeze_reason
            if overage and not state.frozen:
                new_frozen = True
                freeze_reason = "ACTUAL_EXCEEDS_RESERVATION"

            self._execute(
                f"UPDATE {SCHEMA}.phase0_budget_ledgers SET "
                "reserved_tokens = reserved_tokens - %s, "
                "reserved_cost_microunits = reserved_cost_microunits - %s, "
                "reserved_pages = reserved_pages - %s, reserved_bytes = reserved_bytes - %s, "
                "used_concurrency = used_concurrency - %s, "
                "spent_tokens = spent_tokens + %s, "
                "spent_cost_microunits = spent_cost_microunits + %s, "
                "spent_pages = spent_pages + %s, spent_bytes = spent_bytes + %s, "
                "unsettled_cost_microunits = unsettled_cost_microunits + %s, "
                "frozen = %s, freeze_reason = %s, updated_at = now(), "
                "row_version = row_version + 1 "
                "WHERE workspace_id = %s AND run_id = %s",
                (
                    reservation.maximum.tokens,
                    reservation.maximum.cost_microunits,
                    reservation.maximum.pages,
                    reservation.maximum.bytes,
                    reservation.maximum.concurrency,
                    effective.tokens,
                    effective_cost,
                    effective.pages,
                    effective.bytes,
                    effective_cost if provisional else 0,
                    new_frozen,
                    freeze_reason,
                    workspace_id,
                    run_id,
                ),
            )
            status = "unsettled" if provisional else "settled"
            self._execute(
                f"UPDATE {SCHEMA}.phase0_budget_reservations SET "
                "status = %s, updated_at = now(), row_version = row_version + 1 "
                "WHERE workspace_id = %s AND run_id = %s AND reservation_id = %s",
                (status, workspace_id, run_id, reservation_id),
            )
            self._execute(
                f"INSERT INTO {SCHEMA}.phase0_budget_settlements "
                "(workspace_id, run_id, reservation_id, settlement_key, actual_tokens, "
                " actual_cost_microunits, actual_pages, actual_bytes, "
                " effective_cost_microunits, cost_source, provisional, overage, "
                " overage_cost_microunits) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    run_id,
                    reservation_id,
                    settlement_key,
                    effective.tokens,
                    effective.cost_microunits,
                    effective.pages,
                    effective.bytes,
                    effective_cost,
                    cost_source,
                    provisional,
                    overage,
                    overage_cost,
                ),
            )
            self._event(
                workspace_id,
                run_id,
                "settle",
                reservation_id=reservation_id,
                detail=f"{cost_source}{':provisional' if provisional else ''}"
                + (f":{reason}" if reason else ""),
            )
            if new_frozen and not state.frozen:
                self._event(
                    workspace_id,
                    run_id,
                    "freeze",
                    reservation_id=reservation_id,
                    detail=str(freeze_reason),
                )
            settled = self._existing_settlement(
                workspace_id, run_id, reservation_id, settlement_key
            )
            assert settled is not None
            return settled

    def _reservation_by_id(self, reservation_id: UUID, *, lock: bool) -> Reservation:
        suffix = " FOR UPDATE" if lock else ""
        with self._cursor() as cur:
            cur.execute(
                f"SELECT {_RESERVATION_COLUMNS} FROM {SCHEMA}.phase0_budget_reservations "
                f"WHERE reservation_id = %s{suffix}",
                (reservation_id,),
            )
            rows = cur.fetchall()
        if not rows:
            raise ReservationStateError(f"预留不存在: {reservation_id}")
        if len(rows) > 1:  # pragma: no cover - reservation_id 由 uuid4 生成
            raise ReservationStateError(f"reservation_id 非唯一: {reservation_id}")
        return _reservation_from_row(rows[0])

    def release(self, *, reservation_id: UUID, reason: str = "NOT_SENT") -> Reservation:
        """取消/未发出：归还在途预留，不产生 spent（与未知费用保守保留相反）。"""

        _require_key(reason, "reason")
        with self._transaction():
            reservation = self._reservation_by_id(reservation_id, lock=True)
            workspace_id = reservation.workspace_id
            run_id = reservation.run_id
            if reservation.status == "released":
                return reservation
            if reservation.status != "reserved":
                raise ReservationStateError(
                    f"预留 {reservation_id} 状态为 {reservation.status}，不能释放（终态不可复活）"
                )
            self._load(workspace_id, run_id, lock=True)
            self._execute(
                f"UPDATE {SCHEMA}.phase0_budget_ledgers SET "
                "reserved_tokens = reserved_tokens - %s, "
                "reserved_cost_microunits = reserved_cost_microunits - %s, "
                "reserved_pages = reserved_pages - %s, reserved_bytes = reserved_bytes - %s, "
                "used_concurrency = used_concurrency - %s, updated_at = now(), "
                "row_version = row_version + 1 "
                "WHERE workspace_id = %s AND run_id = %s",
                (
                    reservation.maximum.tokens,
                    reservation.maximum.cost_microunits,
                    reservation.maximum.pages,
                    reservation.maximum.bytes,
                    reservation.maximum.concurrency,
                    workspace_id,
                    run_id,
                ),
            )
            self._execute(
                f"UPDATE {SCHEMA}.phase0_budget_reservations SET "
                "status = 'released', updated_at = now(), row_version = row_version + 1 "
                "WHERE workspace_id = %s AND run_id = %s AND reservation_id = %s",
                (workspace_id, run_id, reservation_id),
            )
            self._event(
                workspace_id, run_id, "release", reservation_id=reservation_id, detail=reason
            )
            return self._load_reservation(workspace_id, run_id, reservation_id, lock=False)

    # -- 对账 ----------------------------------------------------------
    def reconcile(self, *, scope: WorkspaceScope, run_id: UUID) -> ReconcileReport:
        """从预留/结算事实重算，与账本行逐维比对（内存计数与 PG 一致性验收）。"""

        workspace_id = self._workspace(scope)
        state = self._load(workspace_id, self._require_run(run_id), lock=False)
        with self._cursor() as cur:
            cur.execute(
                f"SELECT count(*) AS reservations, "
                "coalesce(sum(maximum_tokens) FILTER (WHERE status = 'reserved'), 0)::bigint "
                "  AS r_tokens, "
                "coalesce(sum(maximum_cost_microunits) "
                "FILTER (WHERE status = 'reserved'), 0)::bigint "
                "  AS r_cost, "
                "coalesce(sum(maximum_pages) FILTER (WHERE status = 'reserved'), 0)::bigint "
                "  AS r_pages, "
                "coalesce(sum(maximum_bytes) FILTER (WHERE status = 'reserved'), 0)::bigint "
                "  AS r_bytes, "
                "coalesce(sum(reserved_concurrency) FILTER (WHERE status = 'reserved'), 0)::bigint "
                "  AS r_concurrency "
                f"FROM {SCHEMA}.phase0_budget_reservations "
                "WHERE workspace_id = %s AND run_id = %s",
                (workspace_id, state.run_id),
            )
            reserved_row = cur.fetchone()
            cur.execute(
                f"SELECT count(*) AS settlements, "
                "coalesce(sum(actual_tokens), 0)::bigint AS s_tokens, "
                "coalesce(sum(effective_cost_microunits), 0)::bigint AS s_cost, "
                "coalesce(sum(actual_pages), 0)::bigint AS s_pages, "
                "coalesce(sum(actual_bytes), 0)::bigint AS s_bytes "
                f"FROM {SCHEMA}.phase0_budget_settlements "
                "WHERE workspace_id = %s AND run_id = %s",
                (workspace_id, state.run_id),
            )
            spent_row = cur.fetchone()
        assert reserved_row is not None and spent_row is not None
        derived_reserved = Cost(
            tokens=reserved_row["r_tokens"],
            cost_microunits=reserved_row["r_cost"],
            pages=reserved_row["r_pages"],
            bytes=reserved_row["r_bytes"],
            concurrency=reserved_row["r_concurrency"],
        )
        derived_spent = Cost(
            tokens=spent_row["s_tokens"],
            cost_microunits=spent_row["s_cost"],
            pages=spent_row["s_pages"],
            bytes=spent_row["s_bytes"],
        )
        matches = (
            state.spent.tokens == derived_spent.tokens
            and state.spent.cost_microunits == derived_spent.cost_microunits
            and state.spent.pages == derived_spent.pages
            and state.spent.bytes == derived_spent.bytes
            and state.reserved == derived_reserved
            and state.used_concurrency == derived_reserved.concurrency
        )
        return ReconcileReport(
            workspace_id=workspace_id,
            run_id=state.run_id,
            matches=matches,
            ledger_spent=state.spent,
            derived_spent=derived_spent,
            ledger_reserved=state.reserved,
            derived_reserved=derived_reserved,
            ledger_used_concurrency=state.used_concurrency,
            derived_used_concurrency=derived_reserved.concurrency,
            unsettled_cost_microunits=state.unsettled_cost_microunits,
            reservations=int(reserved_row["reservations"]),
            settlements=int(spent_row["settlements"]),
        )


# ---------------------------------------------------------------------------
# 行 → DTO
# ---------------------------------------------------------------------------
def _state_from_row(row: Mapping[str, Any]) -> LedgerState:
    return LedgerState(
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        currency=row["currency"],
        limits=Cost(
            tokens=row["limit_tokens"],
            cost_microunits=row["limit_cost_microunits"],
            pages=row["limit_pages"],
            bytes=row["limit_bytes"],
            concurrency=row["limit_concurrency"],
        ),
        spent=Cost(
            tokens=row["spent_tokens"],
            cost_microunits=row["spent_cost_microunits"],
            pages=row["spent_pages"],
            bytes=row["spent_bytes"],
        ),
        reserved=Cost(
            tokens=row["reserved_tokens"],
            cost_microunits=row["reserved_cost_microunits"],
            pages=row["reserved_pages"],
            bytes=row["reserved_bytes"],
        ),
        used_concurrency=row["used_concurrency"],
        unsettled_cost_microunits=row["unsettled_cost_microunits"],
        frozen=row["frozen"],
        freeze_reason=row["freeze_reason"],
        deadline_at=row["deadline_at"],
        row_version=row["row_version"],
    )


def _reservation_from_row(row: Mapping[str, Any]) -> Reservation:
    return Reservation(
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        reservation_id=row["reservation_id"],
        purpose_key=row["purpose_key"],
        kind=row["kind"],
        status=row["status"],
        maximum=Cost(
            tokens=row["maximum_tokens"],
            cost_microunits=row["maximum_cost_microunits"],
            pages=row["maximum_pages"],
            bytes=row["maximum_bytes"],
            concurrency=row["reserved_concurrency"],
        ),
        row_version=row["row_version"],
    )


def _settlement_from_row(row: Mapping[str, Any]) -> Settlement:
    return Settlement(
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        reservation_id=row["reservation_id"],
        settlement_key=row["settlement_key"],
        actual=Cost(
            tokens=row["actual_tokens"],
            cost_microunits=row["actual_cost_microunits"],
            pages=row["actual_pages"],
            bytes=row["actual_bytes"],
        ),
        effective_cost_microunits=row["effective_cost_microunits"],
        cost_source=row["cost_source"],
        provisional=row["provisional"],
        overage=row["overage"],
        overage_cost_microunits=row["overage_cost_microunits"],
    )
