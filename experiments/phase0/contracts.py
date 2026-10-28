"""P0-T03 契约：合成 Principal、Workspace Scope、typed ResourceRef、ExecutionBinding。

对应契约（docs/poc/07_Contract_Closure.md）：
- **C-06（DEFINED）**：``ExecutionBinding`` 是「无 Run Job 身份传播」的唯一可信来源。
  Job 恢复时只允许用 ``job_id`` 重新查 binding 与**当前** membership，禁止用队列消息 /
  checkpoint / Job payload 里的 role、workspace、policy snapshot 作授权依据。
- **C-07（BLOCKED）**：生产身份域（全局 User、subject→membership 映射、登录认证方式）
  与 P0 来源审阅（review endpoint / 审阅角色 / 自审）、PRD ``approval_id`` ↔
  实验 ``review_decision_id`` 映射均需产品 + 安全 + 架构决策，**未实现**。
  本模块只提供**合成**主体，不存在生产登录入口，也不自创 review 域模型。
- **C-10（NEEDS_POC）**：asset_bindings 的唯一归属 / 延迟 FK / 插入环不在本模块实现。

可信构造（trusted construction）：
- ``Principal`` / ``WorkspaceScope`` / ``ExecutionBinding`` 的构造函数要求一个模块私有
  令牌；``_trust`` 缺失即 ``UNTRUSTED_CONSTRUCTION``，把「把客户端 JSON 直接塞进
  dataclass」这类误用变成启动期/请求期立刻失败。
- 客户端入口（``from_client_payload`` 等）**永远**抛 ``CLIENT_SCOPE_REJECTED``，
  这不是可配置开关，而是 deny 默认（INV-01：所有访问由后端校验 Principal 与 workspace，
  不得信任客户端提交的作用域）。
- 注意：Python 无法强制模块边界。令牌是「误用即失败」护栏，不是安全边界；真正的边界是
  「不可信数据永不进入这些构造器」，由调用点代码审查与 tests/phase0/test_scope.py 证明。

哈希规范（跨进程/跨库可复算）：
- ``resource_scope_hash = sha256(canonical_json({"v":1,"workspace_id":...,"refs":[...]}))``，
  refs 按 ``(resource_kind, resource_id)`` 升序、去重后参与。
- ``binding_hash`` 覆盖 workspace/binding/principal/policy/operation/resource_scope_hash/
  created_at/expires_at/parent_*，时间统一 ``UTC`` + ``isoformat(timespec="microseconds")``
  （与 PostgreSQL ``timestamptz`` 的微秒精度一致，DB 回读可复算）。
- 所有哈希都是 sha256 十六进制小写 64 位，与迁移 001 的 ``CHECK`` 约束一致。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, Never
from uuid import UUID

__all__ = [
    "AttemptContext",
    "BindingExpiredError",
    "ClientScopeRejectedError",
    "ExecutionBinding",
    "InvalidContractValueError",
    "JobPayloadClaims",
    "Principal",
    "PrincipalInactiveError",
    "ResourceRef",
    "ScopeContractError",
    "UntrustedConstructionError",
    "UntrustedPayloadRejected",
    "WorkspaceScope",
    "backend_constructor_token",
    "compute_binding_hash",
    "compute_resource_scope_hash",
    "issue_scope",
    "synthesize_principal",
]

PrincipalKind = Literal["human", "service"]
PrincipalRole = Literal["owner", "member"]
PrincipalStatus = Literal["active", "disabled"]
ResourceKind = Literal["source", "kb"]

_PRINCIPAL_KINDS: Final[tuple[str, ...]] = ("human", "service")
_PRINCIPAL_ROLES: Final[tuple[str, ...]] = ("owner", "member")
_PRINCIPAL_STATUSES: Final[tuple[str, ...]] = ("active", "disabled")
_RESOURCE_KINDS: Final[tuple[str, ...]] = ("source", "kb")

MAX_OPERATION_LENGTH: Final = 64
_HEX64_RE: Final = re.compile(r"[0-9a-f]{64}")
_OPERATION_RE: Final = re.compile(r"[a-z][a-z0-9_.-]{0,63}")

# Job payload 中允许出现的键；其余键一律记入 ignored，绝不参与授权。
_ALLOWED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {"job_id", "workspace_id", "principal_id", "role"}
)
_CLIENT_DENIAL: Final = (
    "客户端提交的身份/作用域一律拒绝：Scope 只能由后端认证授权路径构造"
    "（INV-01、docs/tdd/01_System_Architecture.md §4）"
)


# ---------------------------------------------------------------------------
# 错误码（证据文件按 code 记录，便于跨任务对齐）
# ---------------------------------------------------------------------------
class ScopeContractError(ValueError):
    """契约错误基类；所有子类带稳定 ``code`` 供证据与断言引用。"""

    code: str = "SCOPE_CONTRACT_ERROR"


class InvalidContractValueError(ScopeContractError):
    code = "INVALID_CONTRACT_VALUE"


class UntrustedConstructionError(ScopeContractError):
    code = "UNTRUSTED_CONSTRUCTION"


class ClientScopeRejectedError(ScopeContractError):
    code = "CLIENT_SCOPE_REJECTED"


class UntrustedPayloadRejected(ScopeContractError):
    code = "UNTRUSTED_JOB_PAYLOAD"


class PrincipalInactiveError(ScopeContractError):
    code = "PRINCIPAL_INACTIVE"


class BindingExpiredError(ScopeContractError):
    code = "BINDING_EXPIRED"


# ---------------------------------------------------------------------------
# 可信构造令牌
# ---------------------------------------------------------------------------
class _TrustedConstructionToken:
    """模块私有构造令牌；只应由受信后端组件（scope 读取路径 / 测试 fixture）持有。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<pyknora.phase0.trusted-construction>"


_TRUSTED_TOKEN = _TrustedConstructionToken()


def backend_constructor_token() -> object:
    """返回受信后端使用的构造令牌。

    仅用于 ``scope.authorize_job`` 的 DB 回读路径与测试 fixture。**不是安全边界**：
    不可信数据（HTTP body、Job payload、队列消息、模型输出）在任何情况下都不得
    进入这些构造器。
    """

    return _TRUSTED_TOKEN


def _require_trust(token: object, *, kind: str) -> None:
    if token is not _TRUSTED_TOKEN:
        raise UntrustedConstructionError(
            f"{kind} 只能由后端可信路径构造（contracts 工厂 / scope.authorize_job）；"
            "禁止从请求体、队列消息、checkpoint 或模型输出直接构造"
        )


def _reject_client(source: str) -> Never:
    raise ClientScopeRejectedError(f"{source}: {_CLIENT_DENIAL}")


# ---------------------------------------------------------------------------
# 值校验
# ---------------------------------------------------------------------------
def _require_uuid(value: object, *, name: str) -> UUID:
    if not isinstance(value, UUID):
        raise InvalidContractValueError(
            f"{name} 必须是 uuid.UUID，收到 {type(value).__name__}（不做字符串隐式转换）"
        )
    return value


def _require_optional_uuid(value: object, *, name: str) -> UUID | None:
    if value is None:
        return None
    return _require_uuid(value, name=name)


def _require_text(
    value: object,
    *,
    name: str,
    min_length: int = 1,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise InvalidContractValueError(f"{name} 必须是 str，收到 {type(value).__name__}")
    if not min_length <= len(value) <= max_length:
        raise InvalidContractValueError(
            f"{name} 长度必须在 [{min_length}, {max_length}]，收到 {len(value)}"
        )
    if pattern is not None and pattern.fullmatch(value) is None:
        raise InvalidContractValueError(f"{name} 不匹配 {pattern.pattern!r}: {value!r}")
    return value


def _require_choice(value: object, *, name: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise InvalidContractValueError(f"{name} 必须是 {choices} 之一，收到 {value!r}")
    return value


def _require_positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidContractValueError(f"{name} 必须是 >= 1 的整数，收到 {value!r}")
    return value


def _require_aware_datetime(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise InvalidContractValueError(f"{name} 必须是 datetime，收到 {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidContractValueError(f"{name} 必须带时区（推荐 datetime.now(UTC)）: {value!r}")
    return value.astimezone(UTC)


def _require_hex64(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise InvalidContractValueError(f"{name} 必须是 64 位小写十六进制 sha256，收到 {value!r}")
    return value


def canonical_timestamp(value: datetime | None) -> str | None:
    """把时间规范化为 UTC 微秒 ISO8601，保证 DB 回读后可复算哈希。"""

    if value is None:
        return None
    return _require_aware_datetime(value, name="timestamp").isoformat(timespec="microseconds")


def _sha256_of_canonical_json(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# typed resource ref
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ResourceRef:
    """typed 资源引用；workspace_id 必须与所属 binding 一致（复合 FK 同源）。

    ``resource_kind`` 只允许 ``source`` / ``kb``：本迁移（001）只为这两类建了复合 FK；
    ``source_version`` / ``document`` / ``document_version`` 由 T11/T12 的增量迁移新增
    列与 FK 后再扩展本 Literal，不在此预留假类型。
    """

    workspace_id: UUID
    resource_kind: ResourceKind
    resource_id: UUID

    def __post_init__(self) -> None:
        _require_uuid(self.workspace_id, name="ResourceRef.workspace_id")
        _require_uuid(self.resource_id, name="ResourceRef.resource_id")
        _require_choice(
            self.resource_kind, name="ResourceRef.resource_kind", choices=_RESOURCE_KINDS
        )

    def as_payload(self) -> dict[str, str]:
        return {"kind": self.resource_kind, "id": str(self.resource_id)}

    def sort_key(self) -> tuple[str, str]:
        return (self.resource_kind, str(self.resource_id))

    @classmethod
    def from_client_payload(cls, *_args: object, **_kwargs: object) -> Never:
        """deny 默认：客户端不得自报资源引用。"""

        _reject_client("ResourceRef.from_client_payload")


# ---------------------------------------------------------------------------
# 合成 Principal（C-07 BLOCKED：不是生产身份）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Principal:
    """合成主体：只表达「某 workspace 内的某 principal_id + 当前 membership 状态」。

    C-07 BLOCKED：``membership_subject_ref`` 是身份域给出的**受控 opaque 引用**，
    不是邮箱/姓名/OIDC subject 的实现；生产身份域、登录方式、subject→membership 映射
    仍缺产品 + 安全 + 架构决策。
    """

    workspace_id: UUID
    principal_id: UUID
    membership_subject_ref: str
    kind: PrincipalKind
    role: PrincipalRole
    status: PrincipalStatus
    membership_revision: int
    _trust: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_trust(self._trust, kind="Principal")
        _require_uuid(self.workspace_id, name="Principal.workspace_id")
        _require_uuid(self.principal_id, name="Principal.principal_id")
        _require_text(
            self.membership_subject_ref,
            name="Principal.membership_subject_ref",
            max_length=128,
        )
        _require_choice(self.kind, name="Principal.kind", choices=_PRINCIPAL_KINDS)
        _require_choice(self.role, name="Principal.role", choices=_PRINCIPAL_ROLES)
        _require_choice(self.status, name="Principal.status", choices=_PRINCIPAL_STATUSES)
        _require_positive_int(self.membership_revision, name="Principal.membership_revision")

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def identity(self) -> tuple[UUID, UUID]:
        """(workspace_id, principal_id)：复合 FK 与 RLS 使用同一对键。"""

        return (self.workspace_id, self.principal_id)

    @classmethod
    def from_client_payload(cls, *_args: object, **_kwargs: object) -> Never:
        """deny 默认：客户端不得自报 Principal（否则等于自选角色/空间）。"""

        _reject_client("Principal.from_client_payload")


def synthesize_principal(
    *,
    workspace_id: UUID,
    principal_id: UUID,
    membership_subject_ref: str,
    kind: PrincipalKind = "human",
    role: PrincipalRole = "member",
    status: PrincipalStatus = "active",
    membership_revision: int = 1,
) -> Principal:
    """合成主体工厂（**仅实验用**；C-07 BLOCKED，不是生产认证实现）。"""

    return Principal(
        workspace_id=workspace_id,
        principal_id=principal_id,
        membership_subject_ref=membership_subject_ref,
        kind=kind,
        role=role,
        status=status,
        membership_revision=membership_revision,
        _trust=_TRUSTED_TOKEN,
    )


# ---------------------------------------------------------------------------
# Workspace Scope（TDD01 §4：(workspace_id, principal_id, policy_snapshot_id)）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    """一次请求/一次作业的执行作用域。

    - 只能由后端构造；``policy_snapshot_id`` 仅作审计输入，**不授予**历史权限
      （当前角色每次从 membership 事实行重算）。
    - 主体被撤销（status=disabled）时不得签发新 scope。
    """

    workspace_id: UUID
    principal_id: UUID
    policy_snapshot_id: UUID
    _trust: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_trust(self._trust, kind="WorkspaceScope")
        _require_uuid(self.workspace_id, name="WorkspaceScope.workspace_id")
        _require_uuid(self.principal_id, name="WorkspaceScope.principal_id")
        _require_uuid(self.policy_snapshot_id, name="WorkspaceScope.policy_snapshot_id")

    @property
    def key(self) -> tuple[UUID, UUID, UUID]:
        return (self.workspace_id, self.principal_id, self.policy_snapshot_id)

    def as_settings(self) -> dict[str, str]:
        """SET LOCAL 用的 GUC 值（键名由 scope.SCOPE_SETTINGS 定义）。"""

        return {
            "workspace_id": str(self.workspace_id),
            "principal_id": str(self.principal_id),
            "policy_snapshot_id": str(self.policy_snapshot_id),
        }

    @classmethod
    def from_client_payload(cls, *_args: object, **_kwargs: object) -> Never:
        """deny 默认：客户端不得自报 scope。"""

        _reject_client("WorkspaceScope.from_client_payload")

    @classmethod
    def from_client_headers(cls, *_args: object, **_kwargs: object) -> Never:
        """deny 默认：请求头里的 workspace/principal 一律不可信。"""

        _reject_client("WorkspaceScope.from_client_headers")


def issue_scope(principal: Principal, *, policy_snapshot_id: UUID) -> WorkspaceScope:
    """由**已验证**的 Principal 签发 scope；disabled 主体直接拒绝（fail closed）。"""

    if not isinstance(principal, Principal):
        raise InvalidContractValueError(
            f"issue_scope 需要已验证的 Principal，收到 {type(principal).__name__}"
        )
    if not principal.is_active:
        raise PrincipalInactiveError(
            f"principal {principal.principal_id} 状态为 {principal.status}，拒绝签发 scope"
        )
    _require_uuid(policy_snapshot_id, name="policy_snapshot_id")
    return WorkspaceScope(
        workspace_id=principal.workspace_id,
        principal_id=principal.principal_id,
        policy_snapshot_id=policy_snapshot_id,
        _trust=_TRUSTED_TOKEN,
    )


# ---------------------------------------------------------------------------
# 哈希
# ---------------------------------------------------------------------------
def compute_resource_scope_hash(
    *, workspace_id: UUID, resource_refs: Iterable[ResourceRef]
) -> str:
    """对 typed resource_refs 求稳定哈希（顺序无关、去重后参与）。"""

    _require_uuid(workspace_id, name="workspace_id")
    refs = tuple(resource_refs)
    for ref in refs:
        if not isinstance(ref, ResourceRef):
            raise InvalidContractValueError(f"resource_refs 含非 ResourceRef: {type(ref).__name__}")
        if ref.workspace_id != workspace_id:
            raise InvalidContractValueError(
                f"resource_ref 属于 workspace {ref.workspace_id}，与 binding workspace "
                f"{workspace_id} 不一致"
            )
    ordered = sorted({ref.sort_key(): ref for ref in refs}.values(), key=ResourceRef.sort_key)
    return _sha256_of_canonical_json(
        {
            "v": 1,
            "workspace_id": str(workspace_id),
            "refs": [ref.as_payload() for ref in ordered],
        }
    )


def compute_binding_hash(
    *,
    workspace_id: UUID,
    execution_binding_id: UUID,
    principal_id: UUID,
    policy_snapshot_id: UUID,
    operation: str,
    resource_scope_hash: str,
    created_at: datetime,
    expires_at: datetime | None,
    parent_run_id: UUID | None,
    parent_task_id: UUID | None,
    task_graph_revision: int | None,
) -> str:
    """对 binding 的授权语义字段求稳定哈希（用于篡改检测与 DB 回读复算）。"""

    return _sha256_of_canonical_json(
        {
            "v": 1,
            "workspace_id": str(workspace_id),
            "execution_binding_id": str(execution_binding_id),
            "principal_id": str(principal_id),
            "policy_snapshot_id": str(policy_snapshot_id),
            "operation": operation,
            "resource_scope_hash": resource_scope_hash,
            "created_at": canonical_timestamp(created_at),
            "expires_at": canonical_timestamp(expires_at),
            "parent_run_id": None if parent_run_id is None else str(parent_run_id),
            "parent_task_id": None if parent_task_id is None else str(parent_task_id),
            "task_graph_revision": task_graph_revision,
        }
    )


# ---------------------------------------------------------------------------
# ExecutionBinding（C-06 不可变授权事实）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    """不可变 execution binding：主体、操作、资源范围、有效期。

    - 创建后不允许改 principal/operation/resource_refs；重试必须新建 binding（C-09）。
    - ``resource_scope_hash`` 与 ``binding_hash`` 在构造时**自算比对**：任何字段被篡改
      （包括从 DB 读回的行被改写）都会在构造期失败，而不是静默放行。
    - ``expires_at`` 只是有效窗口；是否仍可执行还要在 authorize 时按当前 membership
      重新判定（撤销即时生效，不依赖 binding 过期）。
    """

    workspace_id: UUID
    execution_binding_id: UUID
    principal_id: UUID
    policy_snapshot_id: UUID
    operation: str
    resource_refs: tuple[ResourceRef, ...]
    resource_scope_hash: str
    binding_hash: str
    created_at: datetime
    expires_at: datetime | None
    parent_run_id: UUID | None
    parent_task_id: UUID | None
    task_graph_revision: int | None
    _trust: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_trust(self._trust, kind="ExecutionBinding")
        _require_uuid(self.workspace_id, name="ExecutionBinding.workspace_id")
        _require_uuid(self.execution_binding_id, name="ExecutionBinding.execution_binding_id")
        _require_uuid(self.principal_id, name="ExecutionBinding.principal_id")
        _require_uuid(self.policy_snapshot_id, name="ExecutionBinding.policy_snapshot_id")
        _require_text(
            self.operation,
            name="ExecutionBinding.operation",
            max_length=MAX_OPERATION_LENGTH,
            pattern=_OPERATION_RE,
        )
        _require_hex64(self.resource_scope_hash, name="ExecutionBinding.resource_scope_hash")
        _require_hex64(self.binding_hash, name="ExecutionBinding.binding_hash")
        _require_aware_datetime(self.created_at, name="ExecutionBinding.created_at")
        if self.expires_at is not None:
            _require_aware_datetime(self.expires_at, name="ExecutionBinding.expires_at")
            if self.expires_at <= self.created_at:
                raise InvalidContractValueError(
                    "ExecutionBinding.expires_at 必须晚于 created_at"
                )
        _require_optional_uuid(self.parent_run_id, name="ExecutionBinding.parent_run_id")
        _require_optional_uuid(self.parent_task_id, name="ExecutionBinding.parent_task_id")
        self._validate_parent_chain()
        self._validate_resource_refs()
        self._validate_hashes()

    def _validate_parent_chain(self) -> None:
        revision = self.task_graph_revision
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
        ):
            raise InvalidContractValueError(
                f"ExecutionBinding.task_graph_revision 必须是 >= 1 的整数，收到 {revision!r}"
            )
        if revision is not None and self.parent_task_id is None:
            raise InvalidContractValueError("task_graph_revision 有值时 parent_task_id 必填")
        if self.parent_task_id is not None and (self.parent_run_id is None or revision is None):
            raise InvalidContractValueError(
                "parent_task_id 有值时 parent_run_id 与 task_graph_revision 必须同时给出"
            )

    def _validate_resource_refs(self) -> None:
        if not isinstance(self.resource_refs, tuple):
            raise InvalidContractValueError("resource_refs 必须是 tuple（不可变）")
        seen: set[tuple[str, str]] = set()
        for ref in self.resource_refs:
            if not isinstance(ref, ResourceRef):
                raise InvalidContractValueError(
                    f"resource_refs 含非 ResourceRef: {type(ref).__name__}"
                )
            if ref.workspace_id != self.workspace_id:
                raise InvalidContractValueError(
                    f"resource_ref workspace {ref.workspace_id} 与 binding workspace "
                    f"{self.workspace_id} 不一致（跨空间引用一律拒绝）"
                )
            key = ref.sort_key()
            if key in seen:
                raise InvalidContractValueError(f"resource_refs 重复: {key}")
            seen.add(key)

    def _validate_hashes(self) -> None:
        expected_scope_hash = compute_resource_scope_hash(
            workspace_id=self.workspace_id, resource_refs=self.resource_refs
        )
        if expected_scope_hash != self.resource_scope_hash:
            raise InvalidContractValueError(
                "resource_scope_hash 与 resource_refs 不一致（资源范围被篡改）"
            )
        expected_binding_hash = compute_binding_hash(
            workspace_id=self.workspace_id,
            execution_binding_id=self.execution_binding_id,
            principal_id=self.principal_id,
            policy_snapshot_id=self.policy_snapshot_id,
            operation=self.operation,
            resource_scope_hash=self.resource_scope_hash,
            created_at=self.created_at,
            expires_at=self.expires_at,
            parent_run_id=self.parent_run_id,
            parent_task_id=self.parent_task_id,
            task_graph_revision=self.task_graph_revision,
        )
        if expected_binding_hash != self.binding_hash:
            raise InvalidContractValueError(
                "binding_hash 与 binding 字段不一致（授权事实被篡改）"
            )

    def expired(self, *, now: datetime) -> bool:
        if self.expires_at is None:
            return False
        return _require_aware_datetime(now, name="now") >= self.expires_at

    @classmethod
    def create(
        cls,
        *,
        principal: Principal,
        policy_snapshot_id: UUID,
        execution_binding_id: UUID,
        operation: str,
        resource_refs: Iterable[ResourceRef],
        created_at: datetime,
        expires_at: datetime | None = None,
        parent_run_id: UUID | None = None,
        parent_task_id: UUID | None = None,
        task_graph_revision: int | None = None,
    ) -> ExecutionBinding:
        """可信创建入口：由已验证 Principal 派生，并自算两个哈希。"""

        if not isinstance(principal, Principal):
            raise InvalidContractValueError(
                f"create 需要已验证的 Principal，收到 {type(principal).__name__}"
            )
        if not principal.is_active:
            raise PrincipalInactiveError(
                f"principal {principal.principal_id} 状态为 {principal.status}，拒绝创建 binding"
            )
        refs = tuple(resource_refs)
        scope_hash = compute_resource_scope_hash(
            workspace_id=principal.workspace_id, resource_refs=refs
        )
        binding_hash = compute_binding_hash(
            workspace_id=principal.workspace_id,
            execution_binding_id=execution_binding_id,
            principal_id=principal.principal_id,
            policy_snapshot_id=policy_snapshot_id,
            operation=operation,
            resource_scope_hash=scope_hash,
            created_at=created_at,
            expires_at=expires_at,
            parent_run_id=parent_run_id,
            parent_task_id=parent_task_id,
            task_graph_revision=task_graph_revision,
        )
        return cls(
            workspace_id=principal.workspace_id,
            execution_binding_id=execution_binding_id,
            principal_id=principal.principal_id,
            policy_snapshot_id=policy_snapshot_id,
            operation=operation,
            resource_refs=refs,
            resource_scope_hash=scope_hash,
            binding_hash=binding_hash,
            created_at=created_at,
            expires_at=expires_at,
            parent_run_id=parent_run_id,
            parent_task_id=parent_task_id,
            task_graph_revision=task_graph_revision,
            _trust=_TRUSTED_TOKEN,
        )

    @classmethod
    def from_client_payload(cls, *_args: object, **_kwargs: object) -> Never:
        """deny 默认：客户端不得构造/替换 binding。"""

        _reject_client("ExecutionBinding.from_client_payload")


# ---------------------------------------------------------------------------
# 不可信 Job 载荷声明（C-06：只允许核对，不允许授权）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class JobPayloadClaims:
    """从队列消息/checkpoint 解析出的**声明**，永不作为授权依据。

    允许的键：``job_id`` / ``workspace_id`` / ``principal_id`` / ``role``。
    - ``job_id`` / ``workspace_id`` / ``principal_id`` 只能用于与 DB 事实**核对**，
      冲突即 fail closed。
    - ``role`` 只记录为 ignored，不参与任何判断（撤销/降级必须来自 membership 事实行）。
    """

    job_id: UUID | None
    claimed_workspace_id: UUID | None = None
    claimed_principal_id: UUID | None = None
    declared_role: str | None = None
    ignored_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_optional_uuid(self.job_id, name="JobPayloadClaims.job_id")
        _require_optional_uuid(
            self.claimed_workspace_id, name="JobPayloadClaims.claimed_workspace_id"
        )
        _require_optional_uuid(
            self.claimed_principal_id, name="JobPayloadClaims.claimed_principal_id"
        )
        if self.declared_role is not None and not isinstance(self.declared_role, str):
            raise InvalidContractValueError("JobPayloadClaims.declared_role 必须是 str 或 None")
        if not isinstance(self.ignored_fields, tuple):
            raise InvalidContractValueError("JobPayloadClaims.ignored_fields 必须是 tuple")
        for name in self.ignored_fields:
            if not isinstance(name, str):
                raise InvalidContractValueError("ignored_fields 只能包含 str")

    @classmethod
    def from_untrusted_payload(cls, payload: Mapping[str, object]) -> JobPayloadClaims:
        """解析不可信载荷：只读允许的键，其余记入 ignored；永不信任内容。"""

        if not isinstance(payload, Mapping):
            raise UntrustedPayloadRejected(
                f"Job payload 必须是映射，收到 {type(payload).__name__}"
            )
        unknown = tuple(sorted(str(key) for key in payload if key not in _ALLOWED_PAYLOAD_KEYS))
        return cls(
            job_id=_parse_untrusted_uuid(payload.get("job_id"), name="job_id"),
            claimed_workspace_id=_parse_untrusted_uuid(
                payload.get("workspace_id"), name="workspace_id"
            ),
            claimed_principal_id=_parse_untrusted_uuid(
                payload.get("principal_id"), name="principal_id"
            ),
            declared_role=payload.get("role") if isinstance(payload.get("role"), str) else None,
            ignored_fields=unknown,
        )


def _parse_untrusted_uuid(value: object, *, name: str) -> UUID | None:
    """不可信输入允许 JSON 字符串 UUID；仅做格式解析，不代表可信。"""

    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise UntrustedPayloadRejected(f"Job payload.{name} 不是合法 UUID: {value!r}") from exc
    raise UntrustedPayloadRejected(
        f"Job payload.{name} 必须是 UUID 字符串，收到 {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# 授权结果（authorize_job 的返回值）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AttemptContext:
    """一次「重新授权」的结果：绑定 + 当前成员事实 + 作业标识。

    组合校验代替额外令牌：``scope``/``binding`` 本身必须通过可信构造，二者必须同源，
    因此伪造本对象需要先伪造其依赖（会失败）。
    """

    scope: WorkspaceScope
    binding: ExecutionBinding
    job_id: UUID
    run_id: UUID | None
    principal_role: PrincipalRole
    membership_revision: int
    authorized_at: datetime
    attempt_id: UUID | None = None
    ignored_payload_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scope, WorkspaceScope):
            raise InvalidContractValueError("AttemptContext.scope 必须是 WorkspaceScope")
        if not isinstance(self.binding, ExecutionBinding):
            raise InvalidContractValueError("AttemptContext.binding 必须是 ExecutionBinding")
        if self.scope.workspace_id != self.binding.workspace_id:
            raise InvalidContractValueError("AttemptContext.scope 与 binding 的 workspace 不一致")
        if self.scope.principal_id != self.binding.principal_id:
            raise InvalidContractValueError("AttemptContext.scope 与 binding 的 principal 不一致")
        if self.scope.policy_snapshot_id != self.binding.policy_snapshot_id:
            raise InvalidContractValueError("AttemptContext.scope 与 binding 的 policy 不一致")
        _require_uuid(self.job_id, name="AttemptContext.job_id")
        _require_optional_uuid(self.run_id, name="AttemptContext.run_id")
        _require_optional_uuid(self.attempt_id, name="AttemptContext.attempt_id")
        _require_choice(
            self.principal_role, name="AttemptContext.principal_role", choices=_PRINCIPAL_ROLES
        )
        _require_positive_int(
            self.membership_revision, name="AttemptContext.membership_revision"
        )
        _require_aware_datetime(self.authorized_at, name="AttemptContext.authorized_at")
        for name in self.ignored_payload_fields:
            if not isinstance(name, str):
                raise InvalidContractValueError("ignored_payload_fields 只能包含 str")

    @property
    def execution_binding_id(self) -> UUID:
        return self.binding.execution_binding_id

    @property
    def workspace_id(self) -> UUID:
        return self.scope.workspace_id

    @property
    def principal_id(self) -> UUID:
        return self.scope.principal_id
