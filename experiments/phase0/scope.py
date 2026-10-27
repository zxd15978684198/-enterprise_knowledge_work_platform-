"""P0-T03 作用域管理：迁移加载、事务内 SET LOCAL 作用域、无 Run Job 重新授权。

设计要点（依据 docs/tdd/07_Data_API_Security.md §4 与 docs/poc/07_Contract_Closure.md C-06）：

- **作用域只存在于当前事务**：``set_config(name, value, true)`` 等价 ``SET LOCAL``；
  提交/回滚后 GUC 回到空串 ``''``，迁移里的
  ``NULLIF(current_setting('app.workspace_id', true), '')::uuid`` 得到 NULL，
  RLS 的 ``USING``/``WITH CHECK`` 必然不成立 → （默认拒绝）。
- **autocommit 连接一律拒绝**：实测 ``SET LOCAL`` 在 autocommit 下被服务端忽略（值保持
  ``''``），会静默退化成「无作用域」。与其静默，不如 fail closed。
- 连接池复用安全：GUC 是事务级的，归还池子前提交/回滚即清空；本模块在退出路径还会
  显式清空一次（双保险），并由 ``assert_scope_cleared`` 提供可断言的检查。
- **不重复实现 RLS**：拒绝由 SQL 承担；Python 侧只做「把作用域设对」和
  「把不可信声明挡在授权之外」。
- **C-06 重新授权**：``authorize_job`` 只信 ``job_id`` + DB 事实（binding + 当前
  membership）。Job payload 里的 role / workspace / principal 仅用于核对，冲突即拒绝，
  role 声明永远忽略。
- **C-07 BLOCKED**：本模块不实现生产登录/身份域/来源审阅；主体来自
  ``contracts.synthesize_principal`` 的合成工厂。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

from contracts import (
    AttemptContext,
    BindingExpiredError,
    ExecutionBinding,
    InvalidContractValueError,
    JobPayloadClaims,
    Principal,
    PrincipalInactiveError,
    ResourceRef,
    ScopeContractError,
    UntrustedPayloadRejected,
    WorkspaceScope,
    backend_constructor_token,
    issue_scope,
    synthesize_principal,
)

__all__ = [
    "APP_ROLE",
    "MIGRATION_ID",
    "MIGRATION_PATH",
    "SCHEMA",
    "SCOPE_SETTINGS",
    "BindingIntegrityError",
    "BindingNotVisibleError",
    "JobAuthorizationError",
    "JobNotFoundError",
    "JobRoute",
    "MigrationStamp",
    "PayloadClaimMismatchError",
    "ScopeSetupError",
    "ScopeSettings",
    "ScopeState",
    "apply_migration",
    "apply_scope",
    "assert_scope_cleared",
    "authorize_job",
    "clear_scope",
    "connect",
    "dsn_with_credentials",
    "lookup_job_route",
    "migration_sha256",
    "read_dsn",
    "reset_database",
    "role_security_attributes",
    "scope_state",
    "scoped_connection",
    "set_role_password",
]

SCHEMA: Final = "p0_t03"
APP_ROLE: Final = "p0_app"
MIGRATION_ID: Final = "001_scope_core"
MIGRATION_PATH: Final = (
    Path(__file__).resolve().parent / "db" / "migrations" / "001_scope_core.sql"
)
_ENV_DSN_RE: Final = re.compile(r"^PHASE0_PG_DSN=(.+)$", re.MULTILINE)
_GUC_NAME_RE: Final = re.compile(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+")


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------
class ScopeSetupError(RuntimeError):
    """环境/用法错误（非授权结论）：连接模式不对、迁移边界不干净、DSN 缺失等。"""

    code = "SCOPE_SETUP_ERROR"


class JobAuthorizationError(ScopeContractError):
    """authorize_job 的失败一律 fail closed（不返回部分授权）。"""

    code = "JOB_AUTHORIZATION_DENIED"


class JobNotFoundError(JobAuthorizationError):
    code = "JOB_NOT_FOUND"


class BindingNotVisibleError(JobAuthorizationError):
    code = "BINDING_NOT_VISIBLE"


class BindingIntegrityError(JobAuthorizationError):
    code = "BINDING_INTEGRITY"


class PayloadClaimMismatchError(JobAuthorizationError):
    code = "JOB_PAYLOAD_MISMATCH"


# ---------------------------------------------------------------------------
# GUC 设置
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScopeSettings:
    """作用域 GUC 名；只允许 ``app.*`` 形式（不能指向服务端保留参数）。"""

    workspace_guc: str = "app.workspace_id"
    principal_guc: str = "app.principal_id"
    policy_snapshot_guc: str = "app.policy_snapshot_id"
    binding_guc: str = "app.execution_binding_id"

    def __post_init__(self) -> None:
        for name in (
            self.workspace_guc,
            self.principal_guc,
            self.policy_snapshot_guc,
            self.binding_guc,
        ):
            if _GUC_NAME_RE.fullmatch(name) is None or not name.startswith("app."):
                raise ScopeSetupError(f"非法的作用域 GUC 名: {name!r}（只接受 app.* 形式）")


SCOPE_SETTINGS: Final = ScopeSettings()


@dataclass(frozen=True, slots=True)
class ScopeState:
    """从连接读回的作用域 GUC 当前值（未设置为 ``''``，与迁移的 NULL 语义等价）。"""

    workspace_id: str | None
    principal_id: str | None
    policy_snapshot_id: str | None
    execution_binding_id: str | None

    @property
    def is_cleared(self) -> bool:
        return all(
            value in (None, "")
            for value in (
                self.workspace_id,
                self.principal_id,
                self.policy_snapshot_id,
                self.execution_binding_id,
            )
        )


def _require_non_autocommit(conn: psycopg.Connection[Any]) -> None:
    if conn.autocommit:
        raise ScopeSetupError(
            "该连接 autocommit=True：服务端会忽略 SET LOCAL（实测 GUC 保持空串），"
            "作用域静默失效；请使用 autocommit=False 的连接"
        )


def apply_scope(
    conn: psycopg.Connection[Any],
    *,
    workspace_id: UUID,
    principal_id: UUID | None = None,
    policy_snapshot_id: UUID | None = None,
    execution_binding_id: UUID | None = None,
    settings: ScopeSettings = SCOPE_SETTINGS,
) -> None:
    """在当前事务内设置作用域（``SET LOCAL``）；None 字段显式置空（deny 默认）。"""

    _require_non_autocommit(conn)
    if not isinstance(workspace_id, UUID):
        raise InvalidContractValueError(
            f"workspace_id 必须是 uuid.UUID，收到 {type(workspace_id).__name__}"
        )
    pairs = (
        (settings.workspace_guc, str(workspace_id)),
        (settings.principal_guc, "" if principal_id is None else str(principal_id)),
        (
            settings.policy_snapshot_guc,
            "" if policy_snapshot_id is None else str(policy_snapshot_id),
        ),
        (settings.binding_guc, "" if execution_binding_id is None else str(execution_binding_id)),
    )
    parts: list[str] = []
    params: list[str] = []
    for name, value in pairs:
        parts.append("set_config(%s, %s, true)")
        params.extend([name, value])
    conn.execute("SELECT " + ", ".join(parts), tuple(params))


def clear_scope(
    conn: psycopg.Connection[Any], *, settings: ScopeSettings = SCOPE_SETTINGS
) -> None:
    """把本事务的作用域显式清空（空串 → RLS 拒绝），不依赖提交时机。"""

    _require_non_autocommit(conn)
    names = (
        settings.workspace_guc,
        settings.principal_guc,
        settings.policy_snapshot_guc,
        settings.binding_guc,
    )
    conn.execute(
        "SELECT " + ", ".join("set_config(%s, '', true)" for _ in names),
        names,
    )


def scope_state(
    conn: psycopg.Connection[Any], *, settings: ScopeSettings = SCOPE_SETTINGS
) -> ScopeState:
    row = conn.execute(
        "SELECT current_setting(%s, true), current_setting(%s, true), "
        "current_setting(%s, true), current_setting(%s, true)",
        (
            settings.workspace_guc,
            settings.principal_guc,
            settings.policy_snapshot_guc,
            settings.binding_guc,
        ),
    ).fetchone()
    if row is None:  # pragma: no cover - SELECT 恒有行
        raise ScopeSetupError("读取作用域 GUC 失败")
    return ScopeState(*row)


def assert_scope_cleared(
    conn: psycopg.Connection[Any], *, settings: ScopeSettings = SCOPE_SETTINGS
) -> None:
    state = scope_state(conn, settings=settings)
    if not state.is_cleared:
        raise ScopeSetupError(
            "作用域 GUC 未清空，连接被池化复用会串空间: "
            f"workspace={state.workspace_id!r} principal={state.principal_id!r} "
            f"policy={state.policy_snapshot_id!r} binding={state.execution_binding_id!r}"
        )


@contextmanager
def scoped_connection(
    conn: psycopg.Connection[Any],
    scope: WorkspaceScope,
    *,
    commit_on_exit: bool = False,
    clear_on_exit: bool = True,
    settings: ScopeSettings = SCOPE_SETTINGS,
) -> Iterator[psycopg.Connection[Any]]:
    """在限定作用域内使用连接。

    - 连接已在事务中（调用方的 Unit of Work）：只 ``SET LOCAL``；退出时按
      ``clear_on_exit`` 清空（默认清空 → 之后的语句在该事务内也一律拒绝），
      **不**替调用方提交/回滚。
    - 连接空闲：自己开隐式事务；正常退出按 ``commit_on_exit`` 提交或回滚，
      异常一律回滚。两条路径提交/回滚后 GUC 都回到 ``''``。
    """

    _require_non_autocommit(conn)
    if not isinstance(scope, WorkspaceScope):
        raise InvalidContractValueError(
            f"scoped_connection 需要 WorkspaceScope，收到 {type(scope).__name__}"
        )
    nested = conn.info.transaction_status is not TransactionStatus.IDLE
    apply_scope(
        conn,
        workspace_id=scope.workspace_id,
        principal_id=scope.principal_id,
        policy_snapshot_id=scope.policy_snapshot_id,
        settings=settings,
    )
    try:
        yield conn
    except BaseException:
        if nested:
            if clear_on_exit:
                _clear_if_transaction_alive(conn, settings=settings)
        else:
            conn.rollback()
        raise
    else:
        if nested:
            if clear_on_exit:
                _clear_if_transaction_alive(conn, settings=settings)
        elif commit_on_exit:
            conn.commit()
        else:
            conn.rollback()


def _clear_if_transaction_alive(
    conn: psycopg.Connection[Any], *, settings: ScopeSettings = SCOPE_SETTINGS
) -> None:
    """只在事务仍然健康时清空；已中止的事务由调用方的回滚负责。"""

    if conn.info.transaction_status is TransactionStatus.INTRANS:
        clear_scope(conn, settings=settings)


# ---------------------------------------------------------------------------
# 连接与 DSN
# ---------------------------------------------------------------------------
def read_dsn(env_file: Path | None = None) -> str:
    """读 PG DSN：``PHASE0_PG_DSN`` 环境变量 > ``experiments/phase0/.env.example``。

    只读取占位凭据文件；真实凭据不进 Git，也不写入证据。
    """

    env_value = os.environ.get("PHASE0_PG_DSN")
    if env_value:
        return env_value.strip()
    path = env_file if env_file is not None else Path(__file__).resolve().parent / ".env.example"
    text = path.read_text(encoding="utf-8")
    match = _ENV_DSN_RE.search(text)
    if match is None:
        raise ScopeSetupError(f"{path} 缺少 PHASE0_PG_DSN")
    return match.group(1).strip()


def connect(*, dsn: str | None = None, autocommit: bool = False) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn or read_dsn(), autocommit=autocommit)


def dsn_with_credentials(dsn: str, *, user: str, password: str) -> str:
    """替换 DSN 的用户名/密码（host/port/db 不变），用于以非超级用户角色连接。"""

    parts = urlsplit(dsn)
    if parts.scheme not in ("postgresql", "postgres") or parts.hostname is None:
        raise ScopeSetupError(f"无法解析 DSN: scheme={parts.scheme!r} host={parts.hostname!r}")
    credentials = f"{quote(user, safe='')}:{quote(password, safe='')}"
    netloc = f"{credentials}@{parts.hostname}"
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ---------------------------------------------------------------------------
# 迁移与实验角色
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MigrationStamp:
    migration_id: str
    filename: str
    sha256: str
    size_bytes: int


def migration_sha256(path: Path = MIGRATION_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_migration(
    conn: psycopg.Connection[Any],
    *,
    path: Path = MIGRATION_PATH,
    migration_id: str = MIGRATION_ID,
) -> MigrationStamp:
    """在**单个事务**内应用迁移并写台账；调用方负责 commit。

    迁移文件自身不含 BEGIN/COMMIT；``CREATE SCHEMA`` 不带 IF NOT EXISTS，重复应用会显式
    失败（防静默跳过）。
    """

    _require_non_autocommit(conn)
    if conn.info.transaction_status is not TransactionStatus.IDLE:
        raise ScopeSetupError("apply_migration 需要干净的事务边界（IDLE），否则无法保证原子性")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute(raw.decode("utf-8"))
    conn.execute(
        "INSERT INTO p0_t03.phase0_schema_migrations (migration_id, filename, sha256) "
        "VALUES (%s, %s, %s)",
        (migration_id, path.name, digest),
    )
    return MigrationStamp(
        migration_id=migration_id,
        filename=path.name,
        sha256=digest,
        size_bytes=len(raw),
    )


def reset_database(conn: psycopg.Connection[Any], *, role: str = APP_ROLE) -> bool:
    """删除实验 schema 与角色（清理用）；返回角色此前是否存在。"""

    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCHEMA)))
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if exists is None:
        return False
    conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
    conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    return True


def set_role_password(conn: psycopg.Connection[Any], *, role: str, password: str) -> None:
    """运行期给实验角色设随机密码；密码只存在内存，不进 Git/日志。"""

    conn.execute(
        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )


def role_security_attributes(
    conn: psycopg.Connection[Any], *, role: str = APP_ROLE
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin "
        "FROM pg_roles WHERE rolname = %s",
        (role,),
    ).fetchone()
    if row is None:
        raise ScopeSetupError(f"角色 {role} 不存在")
    keys = ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolcanlogin")
    return dict(zip(keys, row, strict=True))


# ---------------------------------------------------------------------------
# 无 Run Job 的重新授权（C-06）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class JobRoute:
    workspace_id: UUID
    execution_binding_id: UUID
    run_id: UUID | None


_BINDING_SQL: Final = (
    "SELECT workspace_id, execution_binding_id, principal_id, policy_snapshot_id, operation, "
    "resource_scope_hash, binding_hash, created_at, expires_at, parent_run_id, parent_task_id, "
    "task_graph_revision FROM p0_t03.phase0_execution_bindings "
    "WHERE workspace_id = %s AND execution_binding_id = %s"
)
_BINDING_RESOURCE_SQL: Final = (
    "SELECT resource_kind, CASE resource_kind WHEN 'source' THEN source_id ELSE kb_id END "
    "FROM p0_t03.phase0_execution_binding_resources "
    "WHERE workspace_id = %s AND execution_binding_id = %s ORDER BY resource_ordinal"
)
_PRINCIPAL_SQL: Final = (
    "SELECT principal_id, membership_subject_ref, kind, role, status, membership_revision "
    "FROM p0_t03.phase0_principals WHERE workspace_id = %s AND principal_id = %s"
)


def lookup_job_route(conn: psycopg.Connection[Any], job_id: UUID) -> JobRoute | None:
    """只凭 ``job_id`` 解析 (workspace, binding, run_id)：走 SECURITY DEFINER 窄函数。"""

    row = conn.execute(
        "SELECT workspace_id, execution_binding_id, run_id "
        "FROM p0_t03.phase0_lookup_job_route(%s)",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return JobRoute(workspace_id=row[0], execution_binding_id=row[1], run_id=row[2])


def _load_binding(conn: psycopg.Connection[Any], route: JobRoute) -> ExecutionBinding:
    row = conn.execute(_BINDING_SQL, (route.workspace_id, route.execution_binding_id)).fetchone()
    if row is None:
        raise BindingNotVisibleError(
            f"binding {route.execution_binding_id} 在 workspace {route.workspace_id} 不可见"
        )
    resource_rows = conn.execute(
        _BINDING_RESOURCE_SQL, (route.workspace_id, route.execution_binding_id)
    ).fetchall()
    refs = tuple(
        ResourceRef(workspace_id=row[0], resource_kind=kind, resource_id=resource_id)
        for kind, resource_id in resource_rows
    )
    try:
        return ExecutionBinding(
            workspace_id=row[0],
            execution_binding_id=row[1],
            principal_id=row[2],
            policy_snapshot_id=row[3],
            operation=row[4],
            resource_refs=refs,
            resource_scope_hash=row[5],
            binding_hash=row[6],
            created_at=row[7],
            expires_at=row[8],
            parent_run_id=row[9],
            parent_task_id=row[10],
            task_graph_revision=row[11],
            _trust=backend_constructor_token(),
        )
    except ScopeContractError as exc:
        raise BindingIntegrityError(
            f"binding {route.execution_binding_id} 完整性校验失败（DB 行与哈希不一致）: {exc}"
        ) from exc


def _load_principal(
    conn: psycopg.Connection[Any], *, workspace_id: UUID, principal_id: UUID
) -> Principal:
    row = conn.execute(_PRINCIPAL_SQL, (workspace_id, principal_id)).fetchone()
    if row is None:
        raise JobAuthorizationError(
            f"principal {principal_id} 在 workspace {workspace_id} 不存在（fail closed）"
        )
    try:
        return synthesize_principal(
            workspace_id=workspace_id,
            principal_id=row[0],
            membership_subject_ref=row[1],
            kind=row[2],
            role=row[3],
            status=row[4],
            membership_revision=row[5],
        )
    except ScopeContractError as exc:
        raise JobAuthorizationError(f"principal 行不合法（fail closed）: {exc}") from exc


def _coerce_claims(payload: Mapping[str, object] | JobPayloadClaims | None) -> JobPayloadClaims:
    if payload is None:
        return JobPayloadClaims(job_id=None)
    if isinstance(payload, JobPayloadClaims):
        return payload
    if isinstance(payload, Mapping):
        return JobPayloadClaims.from_untrusted_payload(payload)
    raise UntrustedPayloadRejected(
        f"payload 必须是 Mapping / JobPayloadClaims / None，收到 {type(payload).__name__}"
    )


def _check_claims_against_route(
    claims: JobPayloadClaims, *, job_id: UUID, route: JobRoute
) -> None:
    if claims.job_id is not None and claims.job_id != job_id:
        raise PayloadClaimMismatchError(
            f"payload.job_id={claims.job_id} 与入参 job_id={job_id} 不一致（拒绝）"
        )
    if (
        claims.claimed_workspace_id is not None
        and claims.claimed_workspace_id != route.workspace_id
    ):
        raise PayloadClaimMismatchError(
            f"payload.workspace_id={claims.claimed_workspace_id} 与 DB 事实 "
            f"{route.workspace_id} 不一致（拒绝）"
        )


def _ignored_field_names(claims: JobPayloadClaims) -> tuple[str, ...]:
    names = set(claims.ignored_fields)
    if claims.declared_role is not None:
        names.add("role")
    return tuple(sorted(names))


def authorize_job(
    conn: psycopg.Connection[Any],
    job_id: UUID,
    *,
    payload: Mapping[str, object] | JobPayloadClaims | None = None,
    settings: ScopeSettings = SCOPE_SETTINGS,
) -> AttemptContext:
    """无 Run Job 场景下的重新授权（C-06 / F13）。

    顺序：``job_id`` → 窄路由函数 → 事务内读 binding（哈希自校验）→ 读**当前**
    membership（role/status/revision）→ 校验有效期 → 返回 AttemptContext。
    任何一步失败都抛错（不返回部分结果）；payload 只参与核对，role 声明永不生效。
    """

    if not isinstance(job_id, UUID):
        raise InvalidContractValueError(f"job_id 必须是 uuid.UUID，收到 {type(job_id).__name__}")
    _require_non_autocommit(conn)
    claims = _coerce_claims(payload)
    route = lookup_job_route(conn, job_id)
    if route is None:
        raise JobNotFoundError(f"job {job_id} 不存在（fail closed）")
    _check_claims_against_route(claims, job_id=job_id, route=route)
    attempt: AttemptContext
    with conn.transaction():
        apply_scope(conn, workspace_id=route.workspace_id, settings=settings)
        binding = _load_binding(conn, route)
        if (
            claims.claimed_principal_id is not None
            and claims.claimed_principal_id != binding.principal_id
        ):
            raise PayloadClaimMismatchError(
                f"payload.principal_id={claims.claimed_principal_id} 与 binding 主体 "
                f"{binding.principal_id} 不一致（拒绝）"
            )
        principal = _load_principal(
            conn, workspace_id=binding.workspace_id, principal_id=binding.principal_id
        )
        if not principal.is_active:
            raise PrincipalInactiveError(
                f"principal {principal.principal_id} 状态为 {principal.status}"
                f"（membership_revision={principal.membership_revision}），拒绝执行"
            )
        now = datetime.now(UTC)
        if binding.expired(now=now):
            raise BindingExpiredError(
                f"binding {binding.execution_binding_id} 已于 {binding.expires_at} 过期"
            )
        apply_scope(
            conn,
            workspace_id=binding.workspace_id,
            principal_id=binding.principal_id,
            policy_snapshot_id=binding.policy_snapshot_id,
            execution_binding_id=binding.execution_binding_id,
            settings=settings,
        )
        scope = issue_scope(principal, policy_snapshot_id=binding.policy_snapshot_id)
        attempt = AttemptContext(
            scope=scope,
            binding=binding,
            job_id=job_id,
            run_id=route.run_id,
            principal_role=principal.role,
            membership_revision=principal.membership_revision,
            authorized_at=now,
            ignored_payload_fields=_ignored_field_names(claims),
        )
        clear_scope(conn, settings=settings)
    return attempt
