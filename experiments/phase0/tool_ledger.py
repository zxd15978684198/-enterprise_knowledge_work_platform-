"""P0-T06 工具调用账本：规范化 / 稳定意图键 / append-only receipt。

本模块是**纯账本层**：只做「参数怎么规范化、intent_key 怎么算、receipt 怎么写和怎么读」。
审批状态机、下游调用、崩溃恢复属于 ``approval_probe``；T21（恢复）与 T29（越权/安全）
可以直接复用本模块，不需要拉起审批实验。

设计约束（``docs/poc/07_Contract_Closure.md`` C-04、``docs/tdd/02_Agent_Harness.md`` §3–5）
------------------------------------------------------------------
* **intent_key 必须稳定**：``SHA256(canonical(schema_version, run_id, response_artifact_id,
  call_ordinal, name, tool_version, arguments_hash, scope_hash))``（C-04 §4）。键由**已持久**
  的响应 artifact 与 ordinal 派生，不从内存计数器或 trace 生成。
* **换参不得绕过唯一键**：参数变了 → ``arguments_hash`` 变 → ``intent_key`` 变 → 新意图 +
  新审批（C-04 §7）。本模块不提供「改参数复用旧 key」的入口，数据库侧另有触发器冻结身份列。
* **账本 append-only**：receipt 只 INSERT；``UPDATE``/``DELETE`` 由 ``002_approvals.sql``
  的触发器拒绝（与角色无关，表 owner 也拦得住）。这里额外在应用层把「改已有 receipt」
  这种调用做成不存在——只暴露 append/read。
* **不伪称「失败 = 未执行」**：receipt 用 ``downstream_sent`` 明确区分「没发出去」与
  「发出去了但结果未知」（``unknown_outcome``）。未发出才允许说「零副作用」。

规范化规则（``CANONICALIZATION_VERSION = "v1"``，与 002 迁移的 CHECK 一致）
--------------------------------------------------------------------------
1. 参数必须是 mapping，key 必须是 str；递归只接受 JSON 标量 / list / mapping。
2. 排序 key、``separators=(",", ":")``、``ensure_ascii=False``、``allow_nan=False``。
3. ``NaN``/``±Inf``/``Decimal``/``bytes``/``set`` 一律拒绝（模糊数值不能进哈希）。
4. ``bool`` 与 ``int`` 不混同（``True`` 不等于 ``1``）；``1`` 与 ``1.0`` 也不混同——
   调用方必须显式给出类型，不能靠隐式转换"猜"。
5. 缺省值必须显式化：``defaults`` 只做**补全**，补全后仍缺 ``required_keys`` 即报错。
6. 规范化文本超过 ``MAX_CANONICAL_BYTES`` 直接拒绝（避免哈希一个巨型载荷）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

__all__ = [
    "CANONICALIZATION_VERSION",
    "ERROR_CODES",
    "INTENT_SCHEMA_VERSION",
    "MAX_CANONICAL_BYTES",
    "OUTCOMES",
    "SCHEMA",
    "ArgumentCanonicalizationError",
    "LedgerError",
    "Receipt",
    "ReceiptConflict",
    "ReceiptMutationRejected",
    "append_receipt",
    "arguments_hash",
    "canonical_json",
    "canonicalize_arguments",
    "external_idempotency_key",
    "has_succeeded_receipt",
    "intent_key",
    "latest_receipt",
    "receipts_for",
    "result_digest",
    "scope_hash",
    "summarize_result",
]

SCHEMA: Final = "p0_t06"
CANONICALIZATION_VERSION: Final = "v1"
INTENT_SCHEMA_VERSION: Final = 1
MAX_CANONICAL_BYTES: Final = 64 * 1024
MAX_SUMMARY_STRING: Final = 200

#: receipt.outcome 的完整枚举（与 002 迁移的 CHECK 一致）。
OUTCOMES: Final[frozenset[str]] = frozenset(
    {"succeeded", "failed", "unknown_outcome", "replayed", "rejected"}
)
FINAL_OUTCOMES: Final[frozenset[str]] = frozenset({"succeeded", "failed", "unknown_outcome"})

#: 稳定错误码：证据与 T21/T29 按这些字符串断言。
ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "APPROVAL_MISSING",
        "APPROVAL_MISMATCH",
        "APPROVAL_EXPIRED",
        "APPROVAL_REVOKED",
        "APPROVAL_CONSUMED",
        "APPROVAL_REJECTED",
        "DOWNSTREAM_FAILED",
        "FORBIDDEN",
        "WORKSPACE_MISMATCH",
        "INTENT_KEY_MISMATCH",
        "UNKNOWN_OUTCOME",
    }
)


class LedgerError(ValueError):
    """账本层错误基类；带稳定 ``code`` 供测试与证据引用。"""

    code = "LEDGER_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = type(self).code


class ArgumentCanonicalizationError(LedgerError):
    """参数无法规范化（NaN/子类型/超长/缺必填）——不能拿一个"猜"出来的哈希去审批。"""

    code = "ARGUMENTS_NOT_CANONICAL"


class ReceiptConflict(LedgerError):
    """同一 (tool_call_id, attempt_no) 已有 receipt：重复写入必须显式失败而不是覆盖。"""

    code = "RECEIPT_CONFLICT"


class ReceiptMutationRejected(LedgerError):
    """试图修改/删除已有 receipt（append-only）。"""

    code = "RECEIPT_APPEND_ONLY"


# ---------------------------------------------------------------------------
# 规范化与哈希
# ---------------------------------------------------------------------------
def _reject_constant(name: str) -> Any:
    raise ArgumentCanonicalizationError(f"参数含非法 JSON 常量 {name}（禁止 NaN/Inf）")


def _check_json_value(value: Any, *, path: str, depth: int = 0) -> None:
    if depth > 32:
        raise ArgumentCanonicalizationError(f"{path}: 嵌套过深（>32）")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ArgumentCanonicalizationError(f"{path}: 禁止 NaN/Inf")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ArgumentCanonicalizationError(f"{path}: key 必须是 str，收到 {key!r}")
            _check_json_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _check_json_value(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    raise ArgumentCanonicalizationError(
        f"{path}: 不支持的类型 {type(value).__name__}（只接受 JSON 标量/数组/对象）"
    )


def canonical_json(value: Any) -> str:
    """稳定 JSON 文本：排序 key、紧凑分隔、禁止 NaN。"""

    _check_json_value(value, path="$")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _merge_defaults(
    arguments: Mapping[str, Any] | Any,
    *,
    defaults: Mapping[str, Any] | None,
    required_keys: Iterable[str],
) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        raise ArgumentCanonicalizationError(
            f"arguments 必须是 mapping，收到 {type(arguments).__name__}"
        )
    merged = dict(arguments)
    for key, value in (defaults or {}).items():
        merged.setdefault(key, value)
    missing = sorted(key for key in required_keys if key not in merged)
    if missing:
        raise ArgumentCanonicalizationError(f"缺少必填参数 {missing}（缺省值必须显式化）")
    return merged


def canonicalize_arguments(
    arguments: Mapping[str, Any],
    *,
    defaults: Mapping[str, Any] | None = None,
    required_keys: Iterable[str] = (),
) -> str:
    """把工具参数规范化成稳定文本；任何模糊/非法输入都拒绝而不是"猜着修"。"""

    merged = _merge_defaults(arguments, defaults=defaults, required_keys=required_keys)
    text = canonical_json(merged)
    if len(text.encode("utf-8")) > MAX_CANONICAL_BYTES:
        raise ArgumentCanonicalizationError(
            f"规范化参数超过 {MAX_CANONICAL_BYTES} 字节（{len(text)} 字符），拒绝哈希"
        )
    return text


def arguments_hash(
    arguments: Mapping[str, Any],
    *,
    defaults: Mapping[str, Any] | None = None,
    required_keys: Iterable[str] = (),
) -> str:
    """``sha256(canonicalize_arguments(...))``，64 位小写十六进制。"""

    return _sha256_text(
        canonicalize_arguments(arguments, defaults=defaults, required_keys=required_keys)
    )


def _typed_refs(
    resource_refs: Iterable[tuple[str, str] | Mapping[str, Any]],
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for ref in resource_refs:
        if isinstance(ref, Mapping):
            kind, rid = ref.get("kind"), ref.get("id")
        else:
            kind, rid = ref
        if not isinstance(kind, str) or not isinstance(rid, str):
            raise LedgerError(f"resource_ref 必须是 (kind, id) 字符串对，收到 {ref!r}")
        items.append({"kind": kind, "id": rid})
    # 去重 + 排序：作用域哈希与参数顺序无关。
    unique = {(item["kind"], item["id"]): item for item in items}
    return [unique[key] for key in sorted(unique)]


def scope_hash(
    *,
    workspace_id: UUID,
    principal_id: UUID,
    operation: str,
    resource_refs: Sequence[tuple[str, str] | Mapping[str, Any]] = (),
    grant_scope: str | None = None,
) -> str:
    """授权范围哈希：覆盖主体、具体资源、操作与授权范围（C-04 §4）。"""

    for name, value in (("workspace_id", workspace_id), ("principal_id", principal_id)):
        if not isinstance(value, UUID):
            raise LedgerError(f"{name} 必须是 uuid.UUID，收到 {type(value).__name__}")
    if not isinstance(operation, str) or not operation:
        raise LedgerError("operation 必须是非空字符串")
    if grant_scope is not None and not isinstance(grant_scope, str):
        raise LedgerError("grant_scope 必须是 str 或 None")
    payload = {
        "v": INTENT_SCHEMA_VERSION,
        "workspace_id": str(workspace_id),
        "principal_id": str(principal_id),
        "operation": operation,
        "resource_refs": _typed_refs(resource_refs),
        "grant_scope": grant_scope,
    }
    return _sha256_text(canonical_json(payload))


def _require_hex64(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise LedgerError(f"{name} 必须是 64 位小写十六进制 sha256，收到 {value!r}")
    if any(ch not in "0123456789abcdef" for ch in value):
        raise LedgerError(f"{name} 必须是 64 位小写十六进制 sha256，收到 {value!r}")
    return value


def intent_key(
    *,
    schema_version: int = INTENT_SCHEMA_VERSION,
    run_id: UUID,
    response_artifact_id: UUID,
    call_ordinal: int,
    name: str,
    tool_version: str,
    arguments_hash: str,
    scope_hash: str,
) -> str:
    """C-04 §4 的稳定意图键：字段顺序与契约逐字一致，不从内存计数派生。

    同一 ``(run, artifact, ordinal)`` 只要参数、工具版本、作用域任一变化，
    key 就会变——这正是「换参必须新意图/新审批」的实现方式。
    """

    for label, value in (("run_id", run_id), ("response_artifact_id", response_artifact_id)):
        if not isinstance(value, UUID):
            raise LedgerError(f"{label} 必须是 uuid.UUID，收到 {type(value).__name__}")
    if not isinstance(call_ordinal, int) or isinstance(call_ordinal, bool) or call_ordinal < 0:
        raise LedgerError(f"call_ordinal 必须是 >= 0 的整数，收到 {call_ordinal!r}")
    if not isinstance(schema_version, int) or schema_version < 1:
        raise LedgerError(f"schema_version 必须是 >= 1 的整数，收到 {schema_version!r}")
    if not isinstance(name, str) or not name:
        raise LedgerError("name 必须是非空字符串")
    if not isinstance(tool_version, str) or not tool_version:
        raise LedgerError("tool_version 必须是非空字符串")
    _require_hex64(arguments_hash, name="arguments_hash")
    _require_hex64(scope_hash, name="scope_hash")
    payload = {
        "schema_version": schema_version,
        "run_id": str(run_id),
        "response_artifact_id": str(response_artifact_id),
        "call_ordinal": call_ordinal,
        "name": name,
        "tool_version": tool_version,
        "arguments_hash": arguments_hash,
        "scope_hash": scope_hash,
    }
    return _sha256_text(canonical_json(payload))


def external_idempotency_key(*, workspace_id: UUID, tool_call_id: UUID) -> str:
    """稳定外部幂等键：由持久 ``tool_call_id`` 派生（C-04 §4），跨重试/重连不变。

    形状受控制站 ``[A-Za-z0-9._:-]{1,128}`` 白名单约束，长度固定 < 64。
    """

    for name, value in (("workspace_id", workspace_id), ("tool_call_id", tool_call_id)):
        if not isinstance(value, UUID):
            raise LedgerError(f"{name} 必须是 uuid.UUID，收到 {type(value).__name__}")
    return f"t06:{workspace_id.hex[:8]}:{tool_call_id}"


# ---------------------------------------------------------------------------
# 结果摘要
# ---------------------------------------------------------------------------
def summarize_result(
    result: Mapping[str, Any] | None,
    *,
    keys: Sequence[str] = ("status", "applied", "applied_calls", "attempts", "deduplicated"),
) -> dict[str, Any]:
    """只保留 allowlist 字段且截断长字符串：账本里不出现响应原文/凭据。"""

    if result is None:
        return {}
    if not isinstance(result, Mapping):
        raise LedgerError(f"result 必须是 mapping 或 None，收到 {type(result).__name__}")
    summary: dict[str, Any] = {}
    for key in keys:
        if key not in result:
            continue
        value = result[key]
        if isinstance(value, str) and len(value) > MAX_SUMMARY_STRING:
            value = value[:MAX_SUMMARY_STRING]
        if isinstance(value, str | int | float | bool) or value is None:
            summary[key] = value
    return summary


def result_digest(result: Mapping[str, Any] | None) -> str | None:
    """结果摘要的 sha256；``None`` 表示没有可摘要的结果（例如拒绝/未发出）。"""

    if result is None:
        return None
    return _sha256_text(canonical_json(dict(result)))


# ---------------------------------------------------------------------------
# Receipt（append-only）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Receipt:
    """一条调用账本记录。字段顺序与 002 迁移 §7 一致。"""

    workspace_id: UUID
    tool_call_id: UUID
    intent_key: str
    attempt_no: int
    arguments_hash: str
    scope_hash: str
    tool_version: str
    outcome: str
    downstream_sent: bool
    duration_ms: int
    receipt_id: UUID | None = None
    approval_id: UUID | None = None
    external_idempotency_key: str | None = None
    result_digest: str | None = None
    result_summary: Mapping[str, Any] | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise LedgerError(f"未知 outcome {self.outcome!r}（允许 {sorted(OUTCOMES)}）")
        _require_hex64(self.intent_key, name="Receipt.intent_key")
        _require_hex64(self.arguments_hash, name="Receipt.arguments_hash")
        _require_hex64(self.scope_hash, name="Receipt.scope_hash")
        if self.result_digest is not None:
            _require_hex64(self.result_digest, name="Receipt.result_digest")
        if self.attempt_no < 0:
            raise LedgerError("attempt_no 必须 >= 0")
        if self.duration_ms < 0:
            raise LedgerError("duration_ms 必须 >= 0")
        if self.error_code is not None and self.error_code not in ERROR_CODES:
            raise LedgerError(f"未登记的 error_code {self.error_code!r}")
        # 与迁移里的 CHECK 同构，在 Python 侧提前给出可读错误。
        if self.downstream_sent and self.external_idempotency_key is None:
            raise LedgerError(
                "downstream_sent=True 必须带 external_idempotency_key（否则无法查单）"
            )
        if not self.downstream_sent and self.external_idempotency_key is not None:
            raise LedgerError("downstream_sent=False 不应带 external_idempotency_key")
        if self.outcome == "unknown_outcome" and not self.downstream_sent:
            raise LedgerError("unknown_outcome 必须 downstream_sent=True（未发出不算未知写）")

    def with_receipt_id(self, receipt_id: UUID | None = None) -> Receipt:
        return Receipt(
            workspace_id=self.workspace_id,
            tool_call_id=self.tool_call_id,
            intent_key=self.intent_key,
            attempt_no=self.attempt_no,
            arguments_hash=self.arguments_hash,
            scope_hash=self.scope_hash,
            tool_version=self.tool_version,
            outcome=self.outcome,
            downstream_sent=self.downstream_sent,
            duration_ms=self.duration_ms,
            receipt_id=receipt_id or self.receipt_id or uuid4(),
            approval_id=self.approval_id,
            external_idempotency_key=self.external_idempotency_key,
            result_digest=self.result_digest,
            result_summary=self.result_summary,
            error_code=self.error_code,
        )


_RECEIPT_COLUMNS: Final = (
    "workspace_id, receipt_id, tool_call_id, approval_id, intent_key, attempt_no, "
    "arguments_hash, scope_hash, tool_version, outcome, downstream_sent, "
    "external_idempotency_key, result_digest, result_summary, duration_ms, error_code"
)


def append_receipt(conn: psycopg.Connection[Any], receipt: Receipt) -> Receipt:
    """追加一条 receipt；同一 ``(tool_call_id, attempt_no)`` 重复必须抛错。"""

    stamped = receipt.with_receipt_id()
    try:
        conn.execute(
            f"INSERT INTO {SCHEMA}.phase0_tool_receipts ({_RECEIPT_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                stamped.workspace_id,
                stamped.receipt_id,
                stamped.tool_call_id,
                stamped.approval_id,
                stamped.intent_key,
                stamped.attempt_no,
                stamped.arguments_hash,
                stamped.scope_hash,
                stamped.tool_version,
                stamped.outcome,
                stamped.downstream_sent,
                stamped.external_idempotency_key,
                stamped.result_digest,
                Jsonb(dict(stamped.result_summary or {})),
                stamped.duration_ms,
                stamped.error_code,
            ),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise ReceiptConflict(
            f"tool_call {stamped.tool_call_id} attempt {stamped.attempt_no} 已有 receipt"
        ) from exc
    except (psycopg.errors.CheckViolation, psycopg.errors.RaiseException) as exc:
        # 触发器（append-only / 不可变列）与 CHECK 约束都归到这里：账本拒绝改写。
        raise ReceiptMutationRejected(str(exc).strip()) from exc
    return stamped


def receipts_for(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, tool_call_id: UUID
) -> list[Receipt]:
    rows = conn.execute(
        f"SELECT {_RECEIPT_COLUMNS} FROM {SCHEMA}.phase0_tool_receipts "
        "WHERE workspace_id = %s AND tool_call_id = %s ORDER BY attempt_no",
        (workspace_id, tool_call_id),
    ).fetchall()
    return [_receipt_from_row(row) for row in rows]


def latest_receipt(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, tool_call_id: UUID
) -> Receipt | None:
    rows = receipts_for(conn, workspace_id=workspace_id, tool_call_id=tool_call_id)
    return rows[-1] if rows else None


def has_succeeded_receipt(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, intent_key_value: str
) -> Receipt | None:
    """按**稳定 intent_key** 查是否已有成功执行（C-04 §8：succeeded 复用 receipt）。"""

    row = conn.execute(
        f"SELECT {_RECEIPT_COLUMNS} FROM {SCHEMA}.phase0_tool_receipts "
        "WHERE workspace_id = %s AND intent_key = %s AND outcome = 'succeeded' "
        "ORDER BY attempt_no LIMIT 1",
        (workspace_id, intent_key_value),
    ).fetchone()
    return None if row is None else _receipt_from_row(row)


def _receipt_from_row(row: Sequence[Any]) -> Receipt:
    (
        workspace_id,
        receipt_id,
        tool_call_id,
        approval_id,
        intent_key_value,
        attempt_no,
        arguments_hash_value,
        scope_hash_value,
        tool_version,
        outcome,
        downstream_sent,
        external_key,
        result_digest_value,
        result_summary,
        duration_ms,
        error_code,
    ) = row
    return Receipt(
        workspace_id=workspace_id,
        tool_call_id=tool_call_id,
        intent_key=intent_key_value,
        attempt_no=attempt_no,
        arguments_hash=arguments_hash_value,
        scope_hash=scope_hash_value,
        tool_version=tool_version,
        outcome=outcome,
        downstream_sent=downstream_sent,
        duration_ms=duration_ms,
        receipt_id=receipt_id,
        approval_id=approval_id,
        external_idempotency_key=external_key,
        result_digest=result_digest_value,
        result_summary=result_summary or {},
        error_code=error_code,
    )
